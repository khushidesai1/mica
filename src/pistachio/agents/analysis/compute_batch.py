"""Attempt transitions; called under BatchBridge's per-event lease."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from typing import TYPE_CHECKING, Any

from . import access, compute_control, pricing
from .compute_quotes import Quote, Rates, affordable_seconds, quote
from .config import CHECKPOINT_PREFIX, Settings
from .storage import GCSStorage, Storage

if TYPE_CHECKING:
    from .batch import BatchBridge


def event_prefix(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid compute event ID.")
    return f"{CHECKPOINT_PREFIX}/events/{event_id}"


async def prepare_attempt(
    bridge: BatchBridge, event_id: str, record: dict[str, Any]
) -> tuple[str, int, int] | None:
    store = GCSStorage(bridge.app.gcs_bucket)
    rates = await asyncio.to_thread(pricing.current, store, bridge.app.gcp_region)
    if not record.get("compute_version"):
        compute_control.initialize(record, bridge.settings, rates)
    if record.get("compute_quote") and record.get("resume"):
        old = Quote.model_validate(record["compute_quote"])
        new = quote(
            old.estimate,
            rates,
            spent_usd=record["compute_spent"],
            limit_usd=record["compute_limit"],
            now=time.time(),
            identity=f"{event_id}:resume",
        )
        record["compute_quote"] = new.model_dump(mode="json")
        if new.requires_approval:
            record.update(status="awaiting_approval", approval_expires=time.time() + 86400)
            await show_approval(bridge, event_id, record)
            return None
    record["rates"] = rates.model_dump(mode="json")
    seconds, model = compute_control.attempt_limits(record, bridge.settings, rates, time.time())
    suffix = f"-a{record['attempt']}" if record["attempt"] else ""
    record["job"] = (
        f"projects/{bridge.app.gcp_project}/locations/{bridge.app.gcp_region}/jobs/"
        f"claude-{event_id}{suffix}"
    )
    record["model_reserved"] = model
    pool = "claude-slot" if record["tier"] == "standard" else "claude-large-slot"
    slots = (
        bridge.settings.max_concurrent_runs
        if pool == "claude-slot"
        else bridge.settings.max_concurrent_large_runs
    )
    return pool, slots, seconds


def worker_manifest(record: dict[str, Any], settings: Settings) -> dict[str, Any]:
    rates = Rates.model_validate(record["rates"])
    q = Quote.model_validate(record["compute_quote"]) if record.get("compute_quote") else None
    return {
        "compute": {
            "version": 1,
            "tier": record["tier"],
            "attempt": record["attempt"],
            "approved": record.get("compute_approved", False),
            "limit": record["compute_limit"],
            "spent": record["compute_spent"],
            "rates": record["rates"],
            "model_spent": record["model_spent"],
            "probe_seconds": record["compute_probe_seconds"],
            "probe_affordable_seconds": affordable_seconds(
                rates,
                record["tier"],
                record["compute_probe_budget"],
                ceiling=record["compute_probe_seconds"],
            ),
            "duration": q.duration_seconds if q else settings.compute_probe_seconds,
        },
        "resume": record.get("resume"),
    }


def read_attempt(store: Storage, event_id: str, attempt: int) -> dict[str, Any] | None:
    key = f"{event_prefix(event_id)}/attempt-a{attempt}.json"
    try:
        raw = store.read_prefix(key, 64001)
    except FileNotFoundError:
        return None
    if len(raw) > 64000:
        raise ValueError("Oversized compute attempt record.")
    data: dict[str, Any] = json.loads(raw)
    if data.get("event_id") != event_id or data.get("attempt") != attempt:
        raise ValueError("Compute result belongs to a different attempt.")
    if data.get("archive") and not re.fullmatch(
        re.escape(event_prefix(event_id)) + rf"/handoff-a{attempt}-[a-f0-9]{{32}}\.tar\.gz",
        data["archive"],
    ):
        raise ValueError("Invalid continuation archive.")
    return data


async def release(bridge: BatchBridge, record: dict[str, Any]) -> None:
    if record.get("slot_token"):
        await bridge.repository.release_event(
            f"{record.get('slot_pool', 'claude-slot')}:{record['slot']}", record["slot_token"]
        )
        for key in ("slot", "slot_token", "slot_pool", "submitted", "started"):
            record.pop(key, None)


async def terminal(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], client: Any, state: str
) -> bool:
    store = GCSStorage(bridge.app.gcs_bucket)
    try:
        result = await asyncio.to_thread(read_attempt, store, event_id, record["attempt"])
        cost = result.get("model_cost") if result else None
        compute_control.settle(record, model_cost=cost, now=time.time())
    except (ValueError, TypeError, KeyError):
        result = None
        compute_control.settle(record, model_cost=None, now=time.time())
        record["compute_failure"] = "The worker's continuation record could not be verified."
    if record.get("cancel_requested"):
        return False
    pending = record.get("compute_pending")
    if pending and state == "SUCCEEDED" and result and result.get("outcome") == "handoff":
        if pending.get("attempt") != record["attempt"] or not result.get("archive"):
            raise ValueError("Continuation did not checkpoint this attempt.")
        q = Quote.model_validate(pending["quote"])
        old_tier = record["tier"]
        record["resume"] = {
            **result,
            "handoff": pending["handoff"],
            "from_tier": old_tier,
            "reason": "resized" if q.estimate.tier != old_tier else "continued",
        }
        record["compute_quote"] = q.model_dump(mode="json")
        record["escalations"] += int(q.estimate.tier != old_tier)
        record["tier"] = q.estimate.tier
        record["attempt"] += 1
        record["compute_approved"] = True
        for key in ("compute_pending", "compute_request_id", "compute_request_result"):
            record.pop(key, None)
        await release(bridge, record)
        record["status"] = "pending"
        await bridge.notify(event_id, record, "starting", "Saved progress. " + q.notice())
        return True
    if pending or (state == "SUCCEEDED" and (not result or result.get("outcome") != "complete")):
        record["status"] = "failed"
        await release(bridge, record)
        await bridge.notify(
            event_id,
            record,
            "failed",
            "The worker stopped before its completion/checkpoint could be verified. "
            "No replacement worker was launched; saved stages remain available.",
        )
        return True
    if state == "FAILED":
        code = await asyncio.to_thread(exit_code, client, record["job"])
        if code == 50005:
            record["compute_failure"] = (
                "This attempt reached its configured time/budget deadline "
                f"({record['max_run_seconds'] / 3600:g} hours). "
                "Saved completed stages remain available."
            )
        elif code == 50001:
            # Until interrupted model usage can be reconciled, its full invocation allowance
            # remains spent. Do not reset it, silently switch to on-demand, or duplicate delivery.
            record["compute_failure"] = (
                "The Spot worker was preempted. Completed saved stages are retained. "
                "Unreported model usage remains reserved, so automatic restart could not be "
                "authorized within this request's remaining model budget."
            )
    return False


def exit_code(client: Any, job: str) -> int | None:
    try:
        codes = [
            event.task_execution.exit_code
            for task in client.list_tasks(parent=f"{job}/taskGroups/group0")
            for event in task.status.status_events
            if event.task_execution.exit_code
        ]
        return int(codes[-1]) if codes else None
    except Exception:
        return None


async def show_approval(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    q = Quote.model_validate(record["compute_quote"])
    limit = math.ceil(q.request_high_usd)
    text = q.notice() + f" Approve a compute limit of ${limit} to continue this request."
    buttons = [
        {
            "type": "button",
            "action_id": f"pistachio_compute_{action}",
            "text": {"type": "plain_text", "text": label},
            "value": f"{event_id}:{q.id}",
        }
        for action, label in [("approve", f"Run within ${limit}"), ("cancel", "Cancel")]
    ]
    event = record["payload"]["event"]
    await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_update(
        channel=event["channel"],
        ts=record["notice_ts"],
        text=text,
        blocks=[
            {"type": "section", "text": {"type": "plain_text", "text": text}},
            {"type": "actions", "elements": buttons},
        ],
    )
    record["notice_text"] = text


async def awaiting_approval(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    store = GCSStorage(bridge.app.gcs_bucket)
    grant = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, store, record)
    if (
        record.get("cancel_requested")
        or time.time() >= record.get("approval_expires", 0)
        or not grant.allowed
        or grant.signature != record["access_signature"]
    ):
        record["status"] = "failed"
        await bridge.notify(
            event_id, record, "cancelled", "The pending computation was cancelled or expired."
        )
