"""Controller-owned GPU placement fallback, before computation starts."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import NotFound
from google.cloud import batch_v1

from . import compute_approval, recovery
from .storage import Conflict, Storage
from .tiers import gpu_choices, selected_gpu

if TYPE_CHECKING:
    from .batch import BatchBridge


def claim_start(storage: Storage, event_id: str, attempt: int, decision: str) -> bool:
    """First writer wins: either issue credentials or retire this unstarted attempt."""
    if decision not in {"start", "fallback"} or not 0 <= attempt <= 2:
        raise ValueError("Invalid GPU startup decision")
    key = compute_approval.key(event_id).removesuffix(".json") + f"/start-a{attempt}"
    value = decision.encode()
    try:
        storage.put_if(key, value, None)
    except Conflict:
        return storage.get(key)[0] == value
    return True


def capacity_error(message: str) -> bool:
    # Only provider resource errors, never a generic model, permission or transport failure.
    return any(
        code in message
        for code in (
            "ZONE_RESOURCE_POOL_EXHAUSTED",
            "POOL_CAPACITY_INSUFFICIENT",
            "STOCKOUT",
        )
    )


async def fallback(
    bridge: BatchBridge,
    event_id: str,
    record: dict[str, Any],
    client: Any,
    job: batch_v1.Job | None,
    state: str,
    *,
    submission_error: str = "",
) -> bool:
    """Return true while retiring a placement or after scheduling its one alternative."""
    if record.get("tier") != "gpu" or not record.get("recovery_version"):
        return False
    choices = gpu_choices(record)
    pending = record.get("gpu_switch_pending")
    if (
        len(choices) < 2
        or record.get("gpu_fallback_count", 0)
        or record.get("cancel_requested")
        or time.time() >= record["request_deadline"]
    ):
        return False
    if not pending:
        if record["attempt"] != 0:
            return False  # A resumed computation keeps its existing hardware/checkpoint.
        if state not in {"QUEUED", "SCHEDULED", "FAILED", "MISSING"}:
            return False
        descriptions = " ".join(e.description for e in job.status.status_events) if job else ""
        resource_error = capacity_error(submission_error + descriptions)
        queued_too_long = (
            state in {"QUEUED", "SCHEDULED"}
            and time.time() - record["submitted"] >= bridge.settings.gpu_queue_wait_seconds
        )
        if not resource_error and not queued_too_long:
            return False
        # This atomic receipt races the credential endpoint. Once it issues credentials,
        # a job can have side effects and must never be replayed as a placement fallback.
        if not await asyncio.to_thread(
            claim_start, bridge.storage, event_id, record["attempt"], "fallback"
        ):
            return False
        pending = next(name for name in choices if name != selected_gpu(record))
        record["gpu_switch_pending"] = pending
        record["gpu_fallback_reason"] = "provider_capacity" if resource_error else "queue_timeout"
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if state != "MISSING":
        # Wait for deletion to be observed before publishing a successor's manifest.
        if state != "DELETION_IN_PROGRESS":
            with suppress(NotFound):
                await asyncio.to_thread(client.delete_job, name=record["job"])
        await bridge.notify(
            event_id,
            record,
            "starting",
            f"{selected_gpu(record).upper()} hasn't become available. "
            f"Releasing that reservation before trying {pending.upper()} automatically.",
        )
        return True
    previous = selected_gpu(record)
    recovery.settle(record, 0.0)  # The credential fence proves computation never started.
    record.update(
        gpu_type=pending,
        gpu_fallback_count=1,
        attempt=record["attempt"] + 1,
        status="pending",
        recovery_cause="gpu_capacity",
        resume=None,
        gpu_placement_history=[
            {
                "job": record["job"],
                "gpu_type": previous,
                "reason": record["gpu_fallback_reason"],
            }
        ],
    )
    record.pop("gpu_switch_pending", None)
    await recovery.release(bridge, record)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    await bridge.notify(
        event_id,
        record,
        "starting",
        f"Continuing on NVIDIA {pending.upper()} because {previous.upper()} was unavailable. "
        "The original time and cost allowance still applies.",
    )
    return True
