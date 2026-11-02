"""Trusted worker catalog. Agent input never becomes an arbitrary Batch policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .config import Settings
    from .direct import WorkerChoice

TierName = Literal["standard", "large", "xlarge", "gpu"]
Provisioning = Literal["SPOT", "STANDARD"]
GPUType = Literal["t4", "l4", "a100", "a100-80gb", "h100"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class WorkerUnavailable(ValueError):
    """A requirement cannot be honored; explain it instead of lowering that requirement."""


@dataclass(frozen=True)
class Tier:
    name: TierName
    vcpus: int
    memory_gib: int
    disk_gb: int
    disk_type: str
    max_seconds: int
    machine_type: str = ""
    escalatable: bool = True
    gpu_type: GPUType | None = None
    gpu_memory_gib: int = 0
    accelerator_type: str = ""
    gpu_count: int = 0
    profile_id: str = ""
    spot_only: bool = False

    @property
    def machine(self) -> str:
        return self.machine_type or f"n2-highmem-{self.vcpus}"

    @property
    def workspace_gb(self) -> int:
        # Existing cache allowance + image/runtime + checkpoint staging reserve.
        return self.disk_gb - 450


TIERS: dict[str, Tier] = {
    "standard": Tier("standard", 8, 64, 500, "pd-balanced", 604800),
    "large": Tier("large", 32, 256, 1000, "pd-balanced", 604800),
    "xlarge": Tier("xlarge", 64, 512, 2000, "pd-ssd", 604800),
    "gpu": Tier(
        "gpu", 8, 32, 500, "pd-balanced", 7200, "g2-standard-8", False, "l4", 24, gpu_count=1
    ),
}
GPU_TIERS = {
    "l4": TIERS["gpu"],
    "t4": replace(
        TIERS["gpu"],
        machine_type="n1-standard-8",
        memory_gib=30,
        gpu_type="t4",
        gpu_memory_gib=16,
        accelerator_type="nvidia-tesla-t4",
    ),
}
ORDER = tuple(name for name, tier in TIERS.items() if tier.escalatable)

CAPABILITY_POLICY = """
compute_capabilities is the current deployment's authoritative resource catalog, refreshed for
this turn. It describes enabled choices, not guaranteed cloud capacity. Do not substitute old
thread/project memory or general cloud knowledge for these limits. Host RAM and GPU VRAM are
different requirements. Carry requested/established minimum host RAM in min_memory_gib and
minimum GPU VRAM in min_gpu_memory_gib, even when the task text already mentions them.
Before start_worker/compute, check that a listed worker satisfies all requirements together.
If none does, clearly say what Mica cannot currently provide, state the actual limiting capacity,
and explain that enabling a larger worker requires an owner configuration change. Offer supported
alternatives, but do not silently allocate less RAM, drop the GPU or reduce the scientific task.
A worker cannot change its allocated machine by following a prose instruction. You may select
enabled resources and request an allowance within the configured ceilings and approval rules;
you cannot change those ceilings, enable machines or grant yourself additional permissions.
When gpu_sizing_required is true, provide gpu_sizing before requesting a GPU: host_memory_gib,
gpu_memory_gib PER DEVICE, vcpus, gpu_count, basis and evidence. Assess actual input dimensions,
model/batch size, measured usage and modest headroom; compressed file size alone is insufficient.
Use inventory first when necessary. Start from one GPU; larger families are available for a
demonstrated need, not a default. Multiple GPUs require multi_gpu_reason explaining supported
parallel execution; their VRAM is not automatically pooled. Preserve explicit user minima.
The application selects the least-cost fitting shape and asks approval for exact hardware,
fallbacks, provisioning modes and estimated cost. Do not claim quota or capacity is guaranteed.
"""


def available_gpus(settings: Settings) -> list[str]:
    return (
        sorted(settings.csv("gpu_types"))
        if settings.gpu_tier_enabled and settings.gpu_image
        else []
    )


def gpu_estimate(settings: Settings, shape: Tier) -> float:
    if shape.profile_id:
        base = {
            "t4": settings.gpu_t4_hourly_estimate_usd,
            "l4": settings.gpu_hourly_estimate_usd,
            "a100": 5.0,
            "a100-80gb": 7.0,
            "h100": 12.0,
        }[str(shape.gpu_type)]
        # Conservative planning allowances include disk; not a live quote or billing cap.
        return round(
            base * shape.gpu_count
            + (max(0, shape.vcpus - 8) * 0.06 + max(0, shape.memory_gib - 32) * 0.008
               if shape.gpu_type in {"t4", "l4"} else 0),
            4,
        )
    return (
        settings.gpu_t4_hourly_estimate_usd
        if shape.gpu_type == "t4"
        else settings.gpu_hourly_estimate_usd
    )


def gpu_choices(record: dict[str, Any]) -> list[str]:
    """Approved alternatives; old requests remain bound to their single GPU."""
    launch = record["launch"]
    return list(launch.get("gpu_candidates", [launch.get("gpu_type", "l4")]))


def selected_gpu(record: dict[str, Any]) -> str:
    return str(record.get("gpu_type", record.get("launch", {}).get("gpu_type", "l4")))


def selected_gpu_choice(record: dict[str, Any]) -> str:
    return str(record.get("gpu_profile", record.get("launch", {}).get(
        "gpu_profile", selected_gpu(record)
    )))


def gpu_mode(record: dict[str, Any], choice: str | None = None) -> Provisioning:
    launch = record["launch"]
    value = launch.get("gpu_candidate_modes", {}).get(
        choice or selected_gpu_choice(record), launch["provisioning"]
    )
    if value not in {"SPOT", "STANDARD"}:
        raise ValueError("Invalid GPU provisioning mode")
    return "SPOT" if value == "SPOT" else "STANDARD"


def gpu_seconds(record: dict[str, Any], settings: Settings) -> int:
    """A new request's selected envelope; legacy approvals keep their recorded duration."""
    ceiling = min(settings.max_run_seconds, settings.gpu_max_seconds)
    return min(int(record["launch"].get("max_seconds", 7200)), ceiling)


def resolve(name: str, settings: Settings, gpu_type: str = "l4") -> Tier:
    if name == "gpu":
        if not settings.gpu_tier_enabled or not settings.gpu_image:
            raise ValueError("GPU workers require an enabled tier and a verified GPU image.")
        if gpu_type in GPU_TIERS:
            shape = GPU_TIERS[gpu_type]
        elif settings.gpu_flexible_sizing:
            from .gpu_profiles import profile

            shape = profile(gpu_type)
        else:
            raise ValueError("Flexible GPU sizing is unavailable.")
        if shape.gpu_type not in available_gpus(settings):
            raise ValueError(
                f"GPU {gpu_type} is unavailable; "
                f"enabled choices: {', '.join(available_gpus(settings))}."
            )
        return replace(
            shape, max_seconds=min(settings.max_run_seconds, settings.gpu_max_seconds)
        )
    if name not in TIERS or ORDER.index(name) > ORDER.index(settings.max_tier):
        raise ValueError("This worker tier is unavailable.")
    tier = TIERS[name]
    return replace(tier, max_seconds=settings.max_run_seconds)


def capabilities(settings: Settings) -> dict[str, Any]:
    """Describe exactly the enabled launch catalog without probing or allocating resources."""
    from .gpu_profiles import catalog

    gpu_shapes = (
        [resolve("gpu", settings, s.profile_id)
         for s in catalog() if s.gpu_type in available_gpus(settings)]
        if settings.gpu_flexible_sizing
        else [resolve("gpu", settings, gpu) for gpu in available_gpus(settings)]
    )
    shapes = [
        resolve(name, settings)
        for name in ORDER[: ORDER.index(settings.max_tier) + 1]
    ] + gpu_shapes
    return {
        "workers": [
            {
                "tier": shape.name,
                "machine_type": shape.machine,
                "vcpus": shape.vcpus,
                "host_memory_gib": shape.memory_gib,
                "gpu_type": shape.gpu_type,
                "gpu_memory_gib": shape.gpu_memory_gib,
                "gpu_count": shape.gpu_count,
                "spot_only": shape.spot_only,
                "workspace_gb": shape.workspace_gb,
                "max_seconds": shape.max_seconds,
                "can_resize": shape.escalatable and shape.name != settings.max_tier,
            }
            for shape in shapes
        ],
        "gpu_approval_required": True,
        "gpu_sizing_required": settings.gpu_flexible_sizing,
        "gpu_selection": "Least estimated cost meeting assessed needs; exact GPU count",
        "t4_custom_host_limits": (
            {"1_or_2_gpus": {"vcpus": 48, "memory_gib": 312},
             "4_gpus": {"vcpus": 96, "memory_gib": 624}}
            if settings.gpu_flexible_sizing and "t4" in available_gpus(settings) else None
        ),
        "gpu_default_seconds": min(
            settings.gpu_default_seconds, settings.gpu_max_seconds, settings.max_run_seconds
        ),
        "provider_capacity_confirmed": False,
        "can_change_limits": False,
    }


def launch_choice(worker: WorkerChoice | None, settings: Settings) -> dict[str, object]:
    if worker is None:
        return {"tier": "standard", "provisioning": "SPOT", "category": "analysis", "reason": ""}
    tier = worker.tier
    if worker.category != "analysis" and tier == "standard" and not worker.tier_explicit:
        tier = settings.launch_heavy_tier
    gpu_type = worker.gpu_type or settings.gpu_default_type
    if worker.gpu_type and tier != "gpu":
        raise ValueError("A GPU model requires the GPU worker tier.")
    if worker.gpu_sizing and tier != "gpu":
        raise ValueError("A GPU sizing assessment requires the GPU worker tier.")
    constraints: dict[str, object] = {}
    if worker.min_memory_gib:
        constraints["min_memory_gib"] = worker.min_memory_gib
    if tier == "gpu":
        if worker.max_tier:
            raise ValueError("A GPU request cannot satisfy an explicit CPU-only tier ceiling.")
        if settings.gpu_flexible_sizing:
            from .gpu_profiles import launch

            return launch(worker, settings)
        candidates = [gpu_type] + (
            [name for name in available_gpus(settings) if name != gpu_type]
            if worker.allow_gpu_fallback
            else []
        )
        candidates = [
            name
            for name in candidates
            if name in available_gpus(settings)
            and GPU_TIERS[name].gpu_memory_gib >= worker.min_gpu_memory_gib
            and GPU_TIERS[name].memory_gib >= worker.min_memory_gib
        ]
        if not candidates:
            enabled = available_gpus(settings)
            if not enabled or (not worker.allow_gpu_fallback and gpu_type not in enabled):
                resolve(tier, settings, gpu_type)
            choices = "; ".join(
                f"{name.upper()}: {GPU_TIERS[name].memory_gib} GiB host RAM, "
                f"{GPU_TIERS[name].gpu_memory_gib} GiB GPU VRAM"
                for name in enabled
            )
            requirements = " and ".join(
                f"at least {amount} GiB {kind}"
                for amount, kind in (
                    (worker.min_memory_gib, "host RAM"),
                    (worker.min_gpu_memory_gib, "GPU VRAM"),
                )
                if amount
            )
            raise WorkerUnavailable(
                f"I can't launch a GPU worker with {requirements} under Mica's current limits. "
                f"Enabled GPU workers: {choices}. No worker was started. "
                "The owner needs to enable a suitable machine before I can run this request."
            )
        mode = "SPOT" if worker.on_demand is False else "STANDARD"
        ceiling = min(settings.max_run_seconds, settings.gpu_max_seconds)
        seconds = min(
            ceiling,
            int(worker.gpu_hours * 3600) if worker.gpu_hours else settings.gpu_default_seconds,
        )
        if worker.compute_budget_usd is not None:
            rate = max(gpu_estimate(settings, GPU_TIERS[name]) for name in candidates)
            seconds = min(
                ceiling,
                int(worker.compute_budget_usd / rate * 3600),
                seconds if worker.gpu_hours else ceiling,
            )
        if seconds < 600:
            raise ValueError("Choose at least ten minutes of GPU time, including startup.")
        return {
            "tier": "gpu",
            "gpu_type": candidates[0],
            "requested_gpu_type": gpu_type,
            "gpu_candidates": candidates,
            "max_seconds": seconds,
            "flexible_compute": True,
            "min_gpu_memory_gib": worker.min_gpu_memory_gib,
            "provisioning": mode,
            "category": worker.category,
            "reason": worker.reason,
            "constraints": {**constraints, "provisioning": mode},
            **({"workload": worker.workload} if worker.workload else {}),
        }
    shape = resolve(tier, settings, gpu_type)
    if worker.min_gpu_memory_gib:
        raise WorkerUnavailable(
            "A CPU worker cannot satisfy a GPU VRAM requirement. Nothing started."
        )
    if shape.memory_gib < worker.min_memory_gib:
        choices = ", ".join(
            f"{name}: {TIERS[name].memory_gib} GiB"
            for name in ORDER[: ORDER.index(settings.max_tier) + 1]
        )
        raise WorkerUnavailable(
            f"The selected {tier} CPU worker has {shape.memory_gib} GiB host RAM, below "
            f"the requested {worker.min_memory_gib} GiB. No worker was started. "
            f"Enabled CPU workers: {choices}. A suitable enabled worker is required."
        )
    if worker.max_tier:
        constraints["max_tier"] = worker.max_tier
        if ORDER.index(tier) > ORDER.index(worker.max_tier):
            raise ValueError("The required worker exceeds this request's tier limit.")
    if not worker.allow_resize:
        constraints["max_tier"] = tier
    if worker.on_demand is not None:
        constraints["provisioning"] = "STANDARD" if worker.on_demand else "SPOT"
    on_demand = worker.on_demand if worker.on_demand is not None else worker.category != "analysis"
    return {
        "tier": tier,
        "provisioning": "STANDARD" if on_demand else "SPOT",
        "category": worker.category,
        "reason": worker.reason,
        **({"constraints": constraints} if constraints else {}),
        **({"workload": worker.workload} if worker.workload else {}),
    }
