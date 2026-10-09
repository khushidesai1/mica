"""Pure, offline feasibility and price arithmetic; no agent-controlled infrastructure."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .tiers import TIERS, TierName

Provisioning = Literal["SPOT", "STANDARD"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Rates(Record):
    region: str = Field(min_length=1, max_length=50)
    currency: Literal["USD"] = "USD"
    provisioning: Provisioning = "SPOT"
    fetched_at: float = Field(ge=0)
    expires_at: float = Field(gt=0)
    cpu_hour: Decimal = Field(gt=0, le=10)
    memory_gib_hour: Decimal = Field(gt=0, le=10)
    balanced_gb_hour: Decimal = Field(gt=0, le=1)
    ssd_gb_hour: Decimal = Field(gt=0, le=1)
    sku_ids: list[str] = Field(min_length=4, max_length=4)
    source: str = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"

    def validate_time(self, now: float, region: str | None = None) -> None:
        if not self.fetched_at <= now < self.expires_at <= self.fetched_at + 86400:
            raise ValueError("Compute prices are expired or invalid; refresh before continuing.")
        if region is not None and self.region != region:
            raise ValueError("Compute prices do not match the worker region.")

    def hourly(self, tier: str) -> tuple[Decimal, Decimal]:
        shape = TIERS[tier]
        disk = self.balanced_gb_hour if shape.disk_type == "pd-balanced" else self.ssd_gb_hour
        return (
            shape.vcpus * self.cpu_hour + shape.memory_gib * self.memory_gib_hour,
            shape.disk_gb * disk,
        )


class Estimate(Record):
    tier: TierName
    label: Literal["analysis", "ingest", "segmentation", "plot", "checkpoint", "training"] = (
        "analysis"
    )
    hours_low: float = Field(gt=0, le=168)
    hours_high: float = Field(gt=0, le=168)
    longest_step_minutes: float | None = Field(default=None, gt=0, le=10080)
    peak_gib: float = Field(gt=0, le=4096)
    task_peak_gib: float = Field(gt=0, le=4096)
    disk_gb: float = Field(ge=0, le=10000)
    basis: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def ordered(self) -> Estimate:
        if self.hours_low > self.hours_high or self.task_peak_gib > self.peak_gib:
            raise ValueError("Estimate ranges or per-task memory are inconsistent.")
        if (
            self.longest_step_minutes is not None
            and self.longest_step_minutes > self.hours_high * 60
        ):
            raise ValueError("The longest step cannot exceed the total estimated time.")
        return self


class Quote(Record):
    id: str
    estimate: Estimate
    provisioning: Provisioning = "SPOT"
    reason: str = ""
    expires_at: float
    vm_hour: float
    disk_hour: float
    remaining_low_usd: float
    remaining_high_usd: float
    spent_usd: float
    request_high_usd: float
    limit_usd: float
    requires_approval: bool
    duration_seconds: int
    storage_reserve_usd: float = 2
    notes: str = (
        "USD public rates; VM and provisioned disk, 30% runtime margin, 15 minutes for "
        "startup/checkpoint, and $2 reserved for artifacts. Model usage is separate. "
        "Queue time is additional. Retries use the same request budget and are requoted. "
        "Taxes and shared logging/network costs are not included; this is not a billing guarantee."
    )

    def notice(self) -> str:
        e, t = self.estimate, TIERS[self.estimate.tier]
        mode = "Spot" if self.provisioning == "SPOT" else "on-demand"
        return (
            (self.reason + " " if self.reason else "")
            + f"{t.name}: {t.vcpus} CPUs, {t.memory_gib} GiB RAM ({mode}). "
            f"Estimated {e.hours_low:g}–{e.hours_high:g} hours plus startup; "
            f"additional compute ${self.remaining_low_usd:.2f}–${self.remaining_high_usd:.2f}. "
            f"${self.spent_usd:.2f} used/reserved so far; "
            f"estimated request total up to ${self.request_high_usd:.2f} "
            f"against the ${self.limit_usd:.2f} compute limit. "
            "Model costs are separate. Completed checkpoints survive; running work may be lost."
        )


def cents(value: Decimal) -> float:
    return float(value.quantize(Decimal(".01"), rounding=ROUND_CEILING))


def quote(
    estimate: Estimate,
    rates: Rates,
    *,
    spent_usd: float,
    limit_usd: float,
    now: float,
    identity: str = "",
    reason: str = "",
) -> Quote:
    rates.validate_time(now)
    if not math.isfinite(spent_usd) or not 0 <= spent_usd <= 100000:
        raise ValueError("Prior compute spending is invalid or unknown.")
    if not math.isfinite(limit_usd) or not 0 < limit_usd <= 10000:
        raise ValueError("Compute spending limit is invalid.")
    shape = TIERS[estimate.tier]
    if estimate.peak_gib * 1.3 + 6 > shape.memory_gib:
        raise ValueError("No fit on this tier: aggregate memory plus 30% headroom exceeds RAM.")
    if estimate.task_peak_gib * 1.3 + 6 > shape.memory_gib:
        raise ValueError("No fit on this tier: a single task exceeds available RAM.")
    if estimate.disk_gb * 1.15 > shape.workspace_gb:
        raise ValueError(
            "No fit on this tier: workspace, spill and checkpoint disk need more space."
        )
    hours = Decimal(str(estimate.hours_high)) * Decimal("1.3") + Decimal(".25")
    seconds = max(600, math.ceil(hours * 3600))
    if seconds > shape.max_seconds:
        raise ValueError("No fit on this tier: the estimate exceeds its time ceiling.")
    vm, disk = rates.hourly(estimate.tier)
    low = (vm + disk) * (Decimal(str(estimate.hours_low)) + Decimal(".25"))
    high = (vm + disk) * hours + Decimal("2")
    total = cents(high + Decimal(str(spent_usd)))
    fingerprint = json.dumps(
        [identity, estimate.model_dump(mode="json"), rates.model_dump(mode="json")], sort_keys=True
    )
    return Quote(
        id=hashlib.sha256(fingerprint.encode()).hexdigest()[:32],
        estimate=estimate,
        provisioning=rates.provisioning,
        reason=reason,
        expires_at=min(rates.expires_at, now + 3600),
        vm_hour=float(vm),
        disk_hour=float(disk),
        remaining_low_usd=cents(low),
        remaining_high_usd=cents(high),
        spent_usd=spent_usd,
        request_high_usd=total,
        limit_usd=limit_usd,
        requires_approval=total > limit_usd,
        duration_seconds=seconds,
    )


def affordable_seconds(rates: Rates, tier: str, remaining: float, *, ceiling: int) -> int:
    """Reserve 10 minutes of teardown; account for disk as well as VM time."""
    if not math.isfinite(remaining) or remaining <= 0:
        return 0
    vm, disk = rates.hourly(tier)
    seconds = int(Decimal(str(remaining)) / (vm + disk) * 3600) - 600
    return max(0, min(seconds, ceiling))
