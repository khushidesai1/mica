"""Controller-owned quotes and spending authorization for one logical Slack request."""

from __future__ import annotations

import math
import time
from typing import Any, Literal

from pydantic import Field

from .compute_quotes import Estimate, Quote, Rates, Record, affordable_seconds, quote
from .config import Settings
from .tiers import ORDER, resolve


class ComputeCall(Record):
    attempt: int = Field(ge=0, le=20)
    action: Literal["estimate", "request"]
    estimate: Estimate | None = None
    quote_id: str = Field(default="", max_length=32)
    handoff: str = Field(default="", max_length=3000)


def active_cost(record: dict[str, Any], now: float) -> float:
    """Conservative accrued reservation, not a claim about final provider billing."""
    rate = float(record.get("compute_hourly", 0))
    elapsed = max(0, now - float(record.get("started", now)))
    cost = float(record.get("compute_spent", 0)) + rate * elapsed / 3600
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("Compute spending could not be reconciled.")
    return cost


def initialize(record: dict[str, Any], settings: Settings, rates: Rates) -> None:
    record.update(
        compute_version=1,
        tier="standard",
        attempt=0,
        compute_spent=0.0,
        model_spent=0.0,
        compute_limit=settings.compute_auto_budget_usd,
        model_limit=settings.max_budget_usd_per_turn,
        compute_probe_seconds=settings.compute_probe_seconds,
        compute_probe_budget=settings.compute_probe_budget_usd,
        quote_only=settings.compute_quote_only,
        escalations=0,
        preemptions=0,
        rates=rates.model_dump(mode="json"),
        compute_approved=False,
        request_deadline=time.time() + settings.max_run_seconds,
    )


def attempt_limits(
    record: dict[str, Any], settings: Settings, rates: Rates, now: float
) -> tuple[int, float]:
    shape = resolve(
        record.get("tier", "standard"), settings.model_copy(update={"tiers_enabled": True})
    )
    rate = sum(rates.hourly(shape.name))
    record["compute_hourly"] = float(rate)
    budget = float(record["compute_limit"]) - float(record["compute_spent"]) - 2
    seconds = affordable_seconds(
        rates,
        shape.name,
        budget,
        ceiling=min(shape.max_seconds, max(0, int(record["request_deadline"] - now))),
    )
    if record.get("quote_only"):
        seconds = min(seconds, settings.compute_probe_seconds + 600)
    model = float(record["model_limit"]) - float(record["model_spent"])
    if seconds < 600 or model < 0.1:
        raise ValueError("This request has exhausted its remaining compute or model allowance.")
    return seconds, model


def operate(
    event_id: str,
    record: dict[str, Any],
    body: ComputeCall,
    settings: Settings,
    rates: Rates,
    now: float,
) -> dict[str, Any]:
    if record.get("compute_version") != 1:
        raise ValueError("Worker resizing is unavailable for this request.")
    if body.attempt != record.get("attempt", 0) or record.get("cancel_requested"):
        raise ValueError("This worker attempt is no longer current.")
    identity = f"{event_id}:{body.attempt}:{record['project_id']}:{record['principal']}"
    if body.action == "request" and body.quote_id == record.get("compute_request_id"):
        return dict(record["compute_request_result"])
    if body.action == "estimate":
        if record.get("compute_pending"):
            raise ValueError("A worker continuation is already pending.")
        if body.estimate is None:
            raise ValueError("A measured work estimate is required.")
        shape = resolve(body.estimate.tier, settings.model_copy(update={"tiers_enabled": True}))
        if ORDER.index(shape.name) < ORDER.index(record.get("tier", "standard")):
            raise ValueError("Downsizing is not supported during a request.")
        if shape.name != record.get("tier", "standard") and record.get("escalations", 0) >= 1:
            raise ValueError("This request already resized; revise the plan before another change.")
        q = quote(
            body.estimate,
            rates,
            spent_usd=active_cost(record, now),
            limit_usd=record["compute_limit"],
            now=now,
            identity=identity,
        )
        record["compute_quote"] = q.model_dump(mode="json")
        record["rates"] = rates.model_dump(mode="json")
        return {"status": "quoted", **q.model_dump(mode="json"), "notice": q.notice()}
    q = Quote.model_validate(record.get("compute_quote", {}))
    if body.quote_id != q.id or now >= q.expires_at:
        raise ValueError(
            "The compute quote is missing, changed, or expired. Request a fresh quote."
        )
    if not body.handoff.strip():
        raise ValueError("Describe completed work, saved checkpoints and the remaining steps.")
    if record.get("compute_pending"):
        return {"status": "checkpoint", "notice": q.notice()}
    # Never trust the quote's arithmetic at launch/approval; recalculate at current prices.
    fresh = quote(
        q.estimate,
        rates,
        spent_usd=active_cost(record, now),
        limit_usd=record["compute_limit"],
        now=now,
        identity=identity,
    )
    record["compute_quote"] = fresh.model_dump(mode="json")
    record["rates"] = rates.model_dump(mode="json")
    if record.get("quote_only"):
        return {
            "status": "quote_only",
            "notice": fresh.notice(),
            "message": "Quote-only rollout: stop after reporting this estimate.",
        }
    same = q.estimate.tier == record.get("tier", "standard")
    remaining = record.get("started", now) + record.get("max_run_seconds", 0) - now
    if same and not fresh.requires_approval and fresh.duration_seconds <= remaining:
        record["compute_approved"] = True
        record["compute_run_deadline"] = now + fresh.duration_seconds
        result = {
            "status": "continue",
            "deadline": record["compute_run_deadline"],
            "notice": fresh.notice(),
        }
        record.update(compute_request_id=body.quote_id, compute_request_result=result)
        return result
    record["compute_pending"] = {
        "quote": fresh.model_dump(mode="json"),
        "handoff": body.handoff,
        "attempt": body.attempt,
        "created": now,
    }
    result = {
        "status": "checkpoint",
        "notice": fresh.notice(),
        "message": "The application will checkpoint this attempt and continue the same request.",
    }
    record.update(compute_request_id=body.quote_id, compute_request_result=result)
    return result


def settle(record: dict[str, Any], *, model_cost: float | None, now: float) -> None:
    attempt = record.get("attempt", 0)
    ledger = record.setdefault("attempt_ledger", [])
    if any(row["attempt"] == attempt for row in ledger):
        return
    # Includes queue uncertainty and 10 minutes teardown; later billing may be lower.
    compute = active_cost(record, now) - float(record.get("compute_spent", 0))
    compute += float(record["compute_hourly"]) / 6
    remaining_model = max(0, record["model_limit"] - record["model_spent"])
    if model_cost is None:
        model = remaining_model  # Lost usage is reserved, never assumed free.
    elif math.isfinite(model_cost) and 0 <= model_cost <= remaining_model + 0.5:
        model = model_cost
    else:
        raise ValueError("Invalid worker model accounting.")
    ledger.append(
        {
            "attempt": attempt,
            "tier": record["tier"],
            "compute_usd": compute,
            "model_usd": model,
            "model_unknown": model_cost is None,
        }
    )
    record["compute_spent"] += compute
    record["model_spent"] += model
