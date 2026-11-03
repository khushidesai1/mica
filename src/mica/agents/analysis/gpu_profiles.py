"""Assessed GPU sizing over trusted provider shapes, never arbitrary VM policies."""

from __future__ import annotations

import math
import re
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from .tiers import GPU_TIERS, GPUType, Record, Tier, WorkerUnavailable, available_gpus, gpu_estimate

if TYPE_CHECKING:
    from .config import Settings
    from .direct import WorkerChoice


class Sizing(Record):
    host_memory_gib: int = Field(ge=1, le=1_048_576)
    gpu_memory_gib: int = Field(ge=1, le=1_048_576)
    vcpus: int = Field(ge=1, le=1024)
    gpu_count: int = Field(default=1, ge=1, le=16)
    basis: Literal["user_requirement", "measured", "estimated"]
    evidence: str = Field(min_length=20, max_length=700)
    multi_gpu_reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def parallelism(self) -> Sizing:
        if self.gpu_count > 1 and len(self.multi_gpu_reason.strip()) < 20:
            raise ValueError("Multiple GPUs require a multi_gpu_reason describing parallelism.")
        return self


def _shape(family: GPUType, machine: str, cpus: int, ram: int, count: int, vram: int) -> Tier:
    return replace(
        GPU_TIERS["l4"],
        machine_type=machine,
        vcpus=cpus,
        memory_gib=ram,
        gpu_type=family,
        gpu_count=count,
        gpu_memory_gib=vram,
        profile_id=machine,
        spot_only=family == "h100" and count < 8,
    )


def catalog() -> list[Tier]:
    shapes = [t4_shape(8, 30, 1)] + [
        _shape("l4", f"g2-standard-{cpu}", cpu, ram, count, 24)
        for cpu, ram, count in (
            (4, 16, 1),
            (8, 32, 1),
            (12, 48, 1),
            (16, 64, 1),
            (24, 96, 2),
            (32, 128, 1),
            (48, 192, 4),
            (96, 384, 8),
        )
    ]
    for count in (1, 2, 4, 8):
        shapes.extend(
            [
                _shape("a100", f"a2-highgpu-{count}g", 12 * count, 85 * count, count, 40),
                _shape("a100-80gb", f"a2-ultragpu-{count}g", 12 * count, 170 * count, count, 80),
                _shape("h100", f"a3-highgpu-{count}g", 26 * count, 234 * count, count, 80),
            ]
        )
    shapes.append(_shape("a100", "a2-megagpu-16g", 96, 1360, 16, 40))
    return shapes


def t4_shape(cpus: int, ram: int, count: int) -> Tier:
    # N1 custom uses even CPU counts and <=6.5 GiB per vCPU. T4 attachment limits
    # are 48 CPUs/312 GiB for 1-2 GPUs, 96 CPUs/624 GiB for 4 GPUs.
    limit = 96 if count == 4 else 48
    if count not in {1, 2, 4} or cpus % 2 or not 2 <= cpus <= limit:
        raise ValueError("Unsupported T4 CPU/GPU count")
    if not math.ceil(cpus * 0.9) <= ram <= cpus * 6.5:
        raise ValueError("Unsupported T4 host memory")
    return replace(
        GPU_TIERS["t4"],
        vcpus=cpus,
        memory_gib=ram,
        gpu_count=count,
        machine_type=f"n1-custom-{cpus}-{ram * 1024}",
        profile_id=f"t4-{count}-{cpus}-{ram}",
    )


def profile(identifier: str) -> Tier:
    for shape in catalog():
        if shape.profile_id == identifier:
            return shape
    match = re.fullmatch(r"t4-(1|2|4)-([0-9]{1,2})-([0-9]{1,3})", identifier)
    if match:
        count, cpus, ram = map(int, match.groups())
        return t4_shape(cpus, ram, count)
    raise ValueError("Unknown GPU profile; use the deployed resource catalog.")


def launch(worker: WorkerChoice, settings: Settings) -> dict[str, object]:
    assessment = worker.gpu_sizing
    if assessment is None:
        raise ValueError(
            "Assess GPU resources first: supply gpu_sizing with host_memory_gib, "
            "gpu_memory_gib per device, vcpus, gpu_count, basis and evidence. Nothing started."
        )
    # Leave room for the trusted controller and installed scientific runtime.
    ram = max(16, assessment.host_memory_gib, worker.min_memory_gib)
    vram = max(assessment.gpu_memory_gib, worker.min_gpu_memory_gib)
    enabled = available_gpus(settings)
    if not worker.allow_gpu_fallback:
        enabled = [worker.gpu_type or settings.gpu_default_type]
        enabled = [family for family in enabled if family in available_gpus(settings)]
    shapes = catalog()
    cpus = 2 * math.ceil(max(4, assessment.vcpus, ram / 6.5) / 2)
    with suppress(ValueError):  # Other families can satisfy larger host-memory requirements.
        shapes.append(t4_shape(cpus, max(ram, math.ceil(cpus * 0.9)), assessment.gpu_count))
    fitting = [
        shape
        for shape in shapes
        if shape.gpu_type in enabled
        and shape.gpu_count == assessment.gpu_count
        and shape.memory_gib >= ram
        and shape.gpu_memory_gib >= vram
        and shape.vcpus >= assessment.vcpus
        and not (worker.on_demand is True and shape.spot_only)
    ]
    fitting.sort(
        key=lambda shape: (
            bool(worker.gpu_type and shape.gpu_type != worker.gpu_type),
            gpu_estimate(settings, shape),
            shape.memory_gib,
            shape.vcpus,
            shape.profile_id,
        )
    )
    # One fitting shape per family, at most one approved alternative. This keeps
    # placement bounded and never changes hardware after computation starts.
    chosen: list[Tier] = []
    for shape in fitting:
        if shape.gpu_type not in {item.gpu_type for item in chosen}:
            chosen.append(shape)
        if len(chosen) == 2:
            break
    if not chosen:
        raise WorkerUnavailable(
            f"I can't provide {assessment.gpu_count} GPU(s), {vram} GiB VRAM per GPU, "
            f"{ram} GiB host RAM and {assessment.vcpus} CPUs with the enabled families "
            f"({', '.join(enabled) or 'none'}) and requested provisioning. "
            "No worker was started. H100 configurations with 1–4 GPUs require Spot. "
            "A suitable configuration or owner quota/configuration change is needed; "
            "I won't substitute a smaller machine."
        )
    modes = {
        shape.profile_id: "SPOT" if worker.on_demand is False or shape.spot_only else "STANDARD"
        for shape in chosen
    }
    rate = max(gpu_estimate(settings, shape) for shape in chosen)
    ceiling = min(settings.max_run_seconds, settings.gpu_max_seconds)
    seconds = min(
        ceiling, int(worker.gpu_hours * 3600) if worker.gpu_hours else settings.gpu_default_seconds
    )
    if worker.compute_budget_usd is not None:
        seconds = min(
            ceiling,
            int(worker.compute_budget_usd / rate * 3600),
            seconds if worker.gpu_hours else ceiling,
        )
    if seconds < 600:
        raise ValueError("Choose at least ten minutes of GPU time, including startup.")
    primary = chosen[0]
    return {
        "tier": "gpu",
        "gpu_type": primary.gpu_type,
        "gpu_profile": primary.profile_id,
        "gpu_candidates": [shape.profile_id for shape in chosen],
        "gpu_candidate_modes": modes,
        "gpu_sizing": assessment.model_dump(),
        "requested_gpu_type": worker.gpu_type,
        "max_seconds": seconds,
        "flexible_compute": True,
        "min_gpu_memory_gib": vram,
        "provisioning": modes[primary.profile_id],
        "category": worker.category,
        "reason": worker.reason,
        "constraints": {"min_memory_gib": ram, "provisioning": modes[primary.profile_id]},
        **({"workload": worker.workload} if worker.workload else {}),
    }
