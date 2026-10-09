"""Trusted worker catalog. Agent input never becomes an arbitrary Batch policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .config import Settings

TierName = Literal["standard", "large", "xlarge"]


@dataclass(frozen=True)
class Tier:
    name: TierName
    vcpus: int
    memory_gib: int
    disk_gb: int
    disk_type: str
    max_seconds: int

    @property
    def machine(self) -> str:
        return f"n2-highmem-{self.vcpus}"

    @property
    def workspace_gb(self) -> int:
        # Existing cache allowance + image/runtime + checkpoint staging reserve.
        return self.disk_gb - 450


TIERS: dict[str, Tier] = {
    "standard": Tier("standard", 8, 64, 500, "pd-balanced", 604800),
    "large": Tier("large", 32, 256, 1000, "pd-balanced", 172800),
    "xlarge": Tier("xlarge", 64, 512, 2000, "pd-ssd", 86400),
}
ORDER = tuple(TIERS)


def resolve(name: str, settings: Settings) -> Tier:
    if name not in TIERS or ORDER.index(name) > ORDER.index(settings.max_tier):
        raise ValueError("This worker tier is unavailable.")
    if name != "standard" and not settings.tiers_enabled:
        raise ValueError("Worker resizing is disabled.")
    tier = TIERS[name]
    return replace(tier, max_seconds=settings.max_run_seconds) if name == "standard" else tier
