"""Anonymous, bounded measurements shared across requests; never scientific results."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from .compute_quotes import Provisioning, Quote, Record
from .config import CHECKPOINT_PREFIX
from .storage import Conflict, Storage
from .tiers import TierName

PREFIX = f"{CHECKPOINT_PREFIX}/calibration/"
log = logging.getLogger(__name__)


class Estimated(Record):
    hours_high: float = Field(gt=0, le=168)
    longest_step_minutes: float = Field(gt=0, le=10080)
    peak_gib: float = Field(gt=0, le=4096)


class Actual(Record):
    hours: float = Field(ge=0)
    longest_step_minutes: float = Field(ge=0)
    peak_gib: float = Field(ge=0)


class Row(Record):
    category: Literal["analysis", "training", "full_segmentation", "full_ingest"]
    label: Literal["analysis", "training", "segmentation", "ingest", "plot", "checkpoint"]
    tier: TierName
    provisioning: Provisioning
    estimated: Estimated
    actual: Actual


def capture(storage: Storage, event_id: str, record: dict[str, Any]) -> None:
    if not record.get("compute_approved") or not record.get("compute_quote"):
        return
    q = Quote.model_validate(record.get("approved_compute_quote") or record["compute_quote"])
    # A quote for the next worker must never describe work done on this one.
    if q.estimate.tier != record["tier"] or q.provisioning != record.get("provisioning", "SPOT"):
        return
    attempt = record["attempt"]
    if not re.fullmatch(r"[a-f0-9]{32}", event_id) or not 0 <= attempt <= 20:
        raise ValueError("Invalid calibration attempt.")
    try:
        raw = storage.read_prefix(
            f"{CHECKPOINT_PREFIX}/events/{event_id}/resources-a{attempt}.json", 64001
        )
    except FileNotFoundError:
        return
    if len(raw) > 64000:
        raise ValueError("Oversized resource measurements.")
    observed = json.loads(raw)
    actual = Actual(
        hours=observed["wall_s"] / 3600,
        longest_step_minutes=max((c["wall_s"] for c in observed.get("commands", [])), default=0)
        / 60,
        peak_gib=observed["peak_gib"],
    )
    row = Row(
        category=record.get("launch_category", "analysis"),
        label=q.estimate.label,
        tier=record["tier"],
        provisioning=record.get("provisioning", "SPOT"),
        estimated=Estimated(
            hours_high=q.estimate.hours_high,
            longest_step_minutes=q.estimate.longest_step_minutes or q.estimate.hours_high * 60,
            peak_gib=q.estimate.peak_gib,
        ),
        actual=actual,
    )
    # The worker's persisted finish time also makes retries after controller crashes idempotent.
    finished = observed.get("finished_at", record.get("started", 0) + observed["wall_s"])
    stamp = datetime.fromtimestamp(finished, UTC).strftime("%Y%m%dT%H%M%S")
    key = f"{PREFIX}{stamp}-{event_id}-a{attempt}.json"
    with suppress(Conflict):
        storage.put_if(key, row.model_dump_json().encode(), None)


def recent(storage: Storage, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    if limit <= 0:
        return rows
    try:
        for key in sorted(storage.keys(PREFIX), reverse=True):
            if not re.fullmatch(
                re.escape(PREFIX) + r"\d{8}T\d{6}-[a-f0-9]{32}-a\d{1,2}\.json", key
            ):
                continue
            try:
                raw = storage.read_prefix(key, 4097)
                if len(raw) > 4096:
                    continue
                row = Row.model_validate_json(raw)
            except (FileNotFoundError, ValueError):
                continue
            if counts[row.category] >= 3:
                continue
            rows.append(row.model_dump(mode="json"))
            counts[row.category] += 1
            if len(rows) >= min(limit, 20) or len(rows) == 12:
                break
    except Exception:
        # History is optional. Never expose paths, provider details, or user data in the warning.
        log.warning("Calibration history could not be read; continuing without it.")
    return rows


def lines(rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for data in rows:
        row = Row.model_validate(data)
        if row.category in seen:
            continue
        seen.add(row.category)
        mode = "on-demand" if row.provisioning == "STANDARD" else "Spot"
        result.append(
            f"{row.category} on {row.tier} ({mode}): longest step "
            f"{row.actual.longest_step_minutes:g} min, total {row.actual.hours:g} h, "
            f"peak {row.actual.peak_gib:g} GiB"
        )
    return result


def history(storage: Storage) -> list[str]:
    return lines(recent(storage))
