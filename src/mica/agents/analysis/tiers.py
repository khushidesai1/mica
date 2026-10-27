"""Trusted worker catalog. Agent input never becomes an arbitrary Batch policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .config import Settings
    from .direct import WorkerChoice

TierName = Literal["standard", "large", "xlarge", "gpu"]
Provisioning = Literal["SPOT", "STANDARD"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


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
    "gpu": Tier("gpu", 8, 32, 500, "pd-balanced", 7200, "g2-standard-8", False),
}
ORDER = tuple(name for name, tier in TIERS.items() if tier.escalatable)


def resolve(name: str, settings: Settings) -> Tier:
    if name == "gpu":
        if not settings.gpu_tier_enabled or not settings.gpu_image:
            raise ValueError("GPU workers require an enabled tier and a verified GPU image.")
        return replace(
            TIERS[name], max_seconds=min(settings.max_run_seconds, settings.gpu_max_seconds)
        )
    if name not in TIERS or ORDER.index(name) > ORDER.index(settings.max_tier):
        raise ValueError("This worker tier is unavailable.")
    tier = TIERS[name]
    return replace(tier, max_seconds=settings.max_run_seconds)


def launch_choice(worker: WorkerChoice | None, settings: Settings) -> dict[str, object]:
    if worker is None:
        return {"tier": "standard", "provisioning": "SPOT", "category": "analysis", "reason": ""}
    tier = worker.tier
    if worker.category != "analysis" and tier == "standard" and not worker.tier_explicit:
        tier = settings.launch_heavy_tier
    resolve(tier, settings)
    constraints: dict[str, object] = {}
    if tier == "gpu":
        if worker.max_tier:
            raise ValueError("A GPU request cannot satisfy an explicit CPU-only tier ceiling.")
        mode = "SPOT" if worker.on_demand is False else "STANDARD"
        return {
            "tier": "gpu",
            "provisioning": mode,
            "category": worker.category,
            "reason": worker.reason,
            "constraints": {"provisioning": mode},
            **({"workload": worker.workload} if worker.workload else {}),
        }
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
