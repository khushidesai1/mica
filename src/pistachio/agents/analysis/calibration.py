"""Bounded project-scoped resource history, including failed attempts."""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import Any, Literal

from pydantic import Field

from .config import CHECKPOINT_PREFIX
from .storage import Conflict, Storage
from .tiers import Provisioning, Record, TierName

PREFIX = f"{CHECKPOINT_PREFIX}/calibration/"


class Row(Record):
    version: Literal[2] = 2
    event_id: str
    attempt: int = Field(ge=0, le=2)
    project_id: str
    category: Literal["analysis", "training", "full_segmentation", "full_ingest"]
    tier: TierName
    provisioning: Provisioning
    outcome: Literal["complete", "failed", "oom", "preempted", "resized"]
    cause: str = ""
    workload: str = ""
    hours: float | None = Field(default=None, ge=0)
    longest_step_minutes: float | None = Field(default=None, ge=0)
    peak_gib: float | None = Field(default=None, ge=0)
    telemetry_complete: bool = False
    started: float = 0


def capture(storage: Storage, event_id: str, record: dict[str, Any], outcome: str) -> None:
    from .recovery import measurements

    attempt = record["attempt"]
    observed = measurements(storage, event_id, attempt)
    wall = observed.get("wall_s")
    row = Row(
        event_id=event_id,
        attempt=attempt,
        project_id=record["project_id"],
        category=record.get("launch_category", "analysis"),
        tier=record["tier"],
        provisioning=record.get("provisioning", "SPOT"),
        outcome=outcome,
        cause=record.get("recovery_pending", {}).get("cause", record.get("recovery_cause", "")),
        workload=record.get("launch", {}).get("workload", ""),
        hours=wall / 3600 if isinstance(wall, (int, float)) else None,
        longest_step_minutes=max((c["wall_s"] for c in observed.get("commands", [])), default=0)
        / 60
        if observed
        else None,
        peak_gib=observed.get("peak_gib"),
        telemetry_complete=bool(observed.get("finished_at")),
        started=record.get("started", 0),
    )
    with suppress(Conflict):
        storage.put_if(
            f"{PREFIX}{record['project_id']}/{event_id}-a{attempt}.json",
            row.model_dump_json().encode(),
            None,
        )


def recent(storage: Storage, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[Row] = []
    try:
        for key in storage.keys(f"{PREFIX}{project_id}/"):
            try:
                raw = storage.read_prefix(key, 4097)
                if len(raw) > 4096:
                    continue
                row = Row.model_validate_json(raw)
                if row.project_id == project_id:
                    rows.append(row)
            except (FileNotFoundError, ValueError):
                continue
    except Exception:
        logging.getLogger(__name__).warning("Worker history unavailable; continuing without it.")
    return [
        r.model_dump()
        for r in sorted(rows, key=lambda r: (r.started, r.event_id, r.attempt), reverse=True)[
            : max(0, min(limit, 20))
        ]
    ]


def lines(rows: list[dict[str, Any]]) -> list[str]:
    return [json.dumps(row, sort_keys=True) for row in rows]


def history(storage: Storage, project_id: str) -> list[str]:
    return lines(recent(storage, project_id))
