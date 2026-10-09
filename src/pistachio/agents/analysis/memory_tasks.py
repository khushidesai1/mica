"""Durable post-delivery memory work, independent of the worker slot and poll lease."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import commands, job_lifecycle, user_memory
from .storage import Storage

if TYPE_CHECKING:
    from .batch import BatchBridge


async def enqueue(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    if (
        not bridge.settings.memory_auto_edit
        or record.get("status") != "complete"
        or record.get("cancel_requested")
        or record.get("memory_editor_done")
        or "memory_reply" not in record
        or commands.parse(record["payload"]["event"].get("text", ""))
    ):
        return
    await job_lifecycle.enqueue(
        bridge.app,
        f"claude-memory-{event_id}",
        f"/api/v1/internal/slack-analysis/memory/{event_id}",
    )


async def process(bridge: BatchBridge, event_id: str, storage: Storage) -> None:
    # Separate from claude-poll: this task cannot hold up a worker slot or the next turn.
    mutex = f"claude-memory:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=170)
    if claim.status != "claimed":
        raise RuntimeError("Memory editor busy; retry")
    try:
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if record and "memory_reply" in record:
            await user_memory.after_turn(bridge, event_id, record, storage, record["memory_reply"])
    finally:
        await bridge.repository.release_event(mutex, claim.token)
