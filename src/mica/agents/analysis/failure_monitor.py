"""Bounded detection on existing request/incident records; never restart work."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mica.config import Settings as AppSettings
from mica.storage.base import Repository
from mica.storage.failure_catalog import CATALOG
from mica.storage.failures import Incident, Observation, digest

from . import failure_health, outcome_review
from .config import Settings

log = logging.getLogger("mica.failure_reporter")
STALL_SECONDS = 900
BREADTH_SECONDS = 1800


def waiting(record: dict[str, Any]) -> bool:
    return bool(
        record.get("status") == "awaiting_approval"
        or record.get("approval_expired")
        or (
            record.get("amendable")
            and not record.get("amendment_choice")
            and record.get("conversation_amendable")
        )
    )


async def tick(repository: Repository, app: AppSettings, settings: Settings, event_id: str) -> None:
    """Called while the process tick owns its poll lease, including early/error exits."""
    try:
        async with asyncio.timeout(3):
            key = f"claude-event:{event_id}"
            record = await repository.get_connector_state(key)
            if not record:
                return
            now = time.time()
            terminal = record.get("status") in {"complete", "failed"}
            updates: dict[str, Any] = {}
            remove: tuple[str, ...] = ()
            if terminal or waiting(record):
                remove = tuple(k for k in ("stall_check_at", "stall_check_tick") if k in record)
            else:
                updates.update(
                    stall_check_at=now + STALL_SECONDS, stall_check_tick=record.get("tick", 0)
                )
            previous = record.get("outcome_review_due")
            await outcome_review.enqueue(repository, app, settings, event_id, record, now)
            if record.get("outcome_review_due") != previous:
                updates["outcome_review_due"] = record["outcome_review_due"]
            if updates or remove:
                await repository.patch_connector_state(key, updates, remove)
    except Exception:
        # Detection must never change the original request's success/error response.
        log.warning("failure_reporter monitor_stamp_unavailable")


async def stalls(repository: Repository, app: AppSettings, now: float) -> dict[str, int]:
    from .observability import request_context, request_scope

    count = 0
    for key, _ in await repository.due_connector_states("stall_check_at", now, limit=20):
        if not key.startswith("claude-event:"):
            continue
        event_id = key.removeprefix("claude-event:")
        mutex = f"claude-poll:{event_id}"
        claim = await repository.begin_event(mutex, lease_seconds=30)
        if claim.status != "claimed":
            continue
        try:
            record = await repository.get_connector_state(key)
            if not record or record.get("stall_check_at", float("inf")) > now:
                continue
            if record.get("status") not in {"complete", "failed"} and not waiting(record):
                await repository.record_failure(
                    Observation(
                        scope=request_scope(app),
                        kind="request",
                        cause="request_stalled",
                        occurrence=digest(
                            event_id,
                            "stall",
                            str(record.get("stall_check_tick", record.get("tick", 0))),
                        ),
                        event_id=event_id,
                        **request_context(record),
                    ),
                    now,
                )
                count += 1
            await repository.patch_connector_state(key, {}, ("stall_check_at", "stall_check_tick"))
        finally:
            await repository.release_event(mutex, claim.token)
    return {"requests_stalled": count}


def breadth(rows: list[Incident], scope: str, now: float) -> dict[str, int]:
    groups: dict[str, set[str]] = {}
    for row in rows:
        o = row.observation
        if row.resolved or row.request_status in failure_health.QUIET_STATUSES:
            continue
        if not row.user_related or not o.event_id or o.scope != scope:
            continue
        # Legacy incidents have only their latest cause's timestamp. Do not pretend
        # older causes happened again just because another operation failed recently.
        causes = row.cause_seen or {o.cause: row.last_seen}
        for cause, seen in causes.items():
            if cause == "request_blocked" or cause not in CATALOG or seen < now - BREADTH_SECONDS:
                continue
            groups.setdefault(CATALOG[cause].kind, set()).add(o.event_id)
    return {kind: len(events) for kind, events in groups.items() if len(events) >= 3}


async def escalations(repository: Repository, app: AppSettings, now: float) -> dict[str, int]:
    from .observability import request_scope

    scope = request_scope(app)
    rows = await repository.recent_failures(now - BREADTH_SECONDS)

    async def actionable(row: Incident) -> bool:
        try:
            outcome, record = await failure_health.inspect(repository, row)
        except Exception:
            outcome, record = "unknown", None
        return failure_health.should_notify(row, outcome, record, now)

    checks = await asyncio.gather(*(actionable(row) for row in rows))
    selected = [row for row, include in zip(rows, checks, strict=True) if include]
    groups = breadth(selected, scope, now)
    for kind, count in groups.items():
        await repository.record_failure(
            Observation(
                scope=f"{scope}:breadth:{kind}",
                kind="service",
                cause="requests_failing",
                occurrence=digest(kind, str(int(now // BREADTH_SECONDS))),
                affected_kind=kind,
                affected_requests=count,
            ),
            now,
        )
    return {"failure_breadth_groups": len(groups)}
