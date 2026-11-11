"""Non-blocking request check-ins and requester-bound cancellation/reporting."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from mica.storage.failures import Observation, digest

from . import (
    access,
    agent_session,
    approvals,
    job_lifecycle,
    memory_workspace,
    observability,
    surface,
)
from .config import CHECKPOINT_PREFIX
from .storage import Storage

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

    from .batch import BatchBridge

PREFIX = "mica_request_"
REASONS = {
    "routing_slow": "I'm still working out how to handle this request after two minutes.",
    "routing_steps": "This request needs more investigation before I can finish or start the work.",
    "routing_retry": "I'm retrying a service problem and keeping the information already gathered.",
    "routing_day": "This request has been routing for 24 hours and needs attention.",
    "worker_effort": "This analysis has needed more agent steps than usual.",
    "worker_spend": "This analysis has passed its model-usage notification threshold.",
}


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid request")
    return f"write-access/request-controls/{event_id}.json"


async def cancelled(storage: Storage, event_id: str) -> bool:
    return await asyncio.to_thread(memory_workspace.cancelled, storage, event_id)


async def reconcile_cancel(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> bool:
    """Only the caller owning the request's poll lease updates its main receipt."""
    if not await cancelled(bridge.storage, event_id):
        return False
    if record.get("status") in {"complete", "failed"}:
        return True
    record.update(cancel_requested=True, stop_reason="cancelled")
    record.setdefault("cancel_requested_at", time.time())
    if not record.get("job"):
        record["status"] = "failed"
        await bridge.notify(
            event_id, record, "cancelled",
            "GCS operations cancelled. Any completed changes are retained."
            if record.get("route") == "storage" else "Cancelled before starting.",
        )
    return True


def reasons(record: dict[str, Any]) -> list[str]:
    found = []
    if (
        record.get("routing_started")
        and not record.get("route")
        and time.time() - record["routing_started"] >= 120
    ):
        found.append("routing_slow")
    if record.get("routing_calls", 0) >= 6:
        found.append("routing_steps")
    if record.get("routing_failures", 0) >= 3:
        found.append("routing_retry")
    if (
        record.get("routing_started")
        and not record.get("route")
        and (time.time() - record["routing_started"] >= 86400)
    ):
        found.append("routing_day")
    for reason in ("worker_effort", "worker_spend"):
        if record.get(reason):
            found.append(reason)
    return found


def blocks(
    event_id: str, text: str, active: bool, *, native_session: bool = False,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "plain_text", "text": text}}
    ]
    if active:
        result.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "action_id": PREFIX + action,
                        "value": event_id,
                    }
                    for action, label in (
                        ("continue", "Keep working"),
                        ("cancel", "Cancel task"),
                        ("report", "Report a problem"),
                    )
                    if action != "cancel" or not native_session
                ],
            }
        )
    return result


async def sync(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    if not record.get("flexible_harness") or surface.is_email(record) or record.get("ambient"):
        return
    storage = bridge.storage
    if "attempt" in record:
        from .recovery import event_prefix, read_json

        telemetry = (
            await asyncio.to_thread(
                read_json,
                storage,
                f"{event_prefix(event_id)}/attention-a{record['attempt']}.json",
            )
            or {}
        )
        if telemetry.get("event_id") == event_id and telemetry.get("attempt") == record["attempt"]:
            for reason in ("worker_effort", "worker_spend"):
                if telemetry.get(reason) is True:
                    record[reason] = True
    active = record.get("status") not in {"complete", "failed"}
    current = reasons(record)
    async with approvals.lease(bridge, f"request-controls:{event_id}"):
        saved = await asyncio.to_thread(approvals.load_json, storage, key(event_id))
        state, generation = saved if saved else ({"reasons": [], "acknowledged": []}, None)
        if not saved and (not current or not active):
            return
        if state.get("report_pending"):
            await report(bridge, event_id, record)
            state.update(reported=True, report_pending=False)
            generation = await asyncio.to_thread(
                approvals.save, storage, key(event_id), state, generation
            )
        state["reasons"] = sorted(set(state["reasons"]) | set(current))
        remaining = [r for r in state["reasons"] if r not in state["acknowledged"]]
        if active and not remaining and not state.get("cancel"):
            text = "Continuing your request. Your saved progress is kept."
        elif active:
            text = " ".join(REASONS[r] for r in remaining)
            text += " Work continues unless you cancel. Reporting a problem does not stop it."
        else:
            text = "This request has finished or stopped. Its saved results and reports are kept."
        if state.get("reported"):
            text += " Your problem report is recorded for the Mica operators."
        if state.get("cancel") and active:
            text = "Cancellation requested. Waiting for active work to stop."
        native_session = agent_session.eligible(bridge, record)
        signature = json.dumps([text, active, state.get("cancel", False), native_session])
        if signature == state.get("rendered"):
            return
        event = record["payload"]["event"]
        client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5, retry_handlers=[])
        message_id = str(uuid5(NAMESPACE_URL, "mica-request-controls:" + event_id))
        if not state.get("ts") and state.get("sending"):
            # Recover an accepted-but-unacknowledged post instead of blindly posting again.
            history = await client.conversations_replies(
                channel=event["channel"],
                ts=event.get("thread_ts") or event["ts"],
                limit=1000,
            )
            who = await client.auth_test()
            messages: list[dict[str, Any]] = history.get("messages", [])
            match = next(
                (
                    m
                    for m in messages
                    if m.get("client_msg_id") == message_id and m.get("user") == who["user_id"]
                ),
                None,
            )
            if not match:
                return  # Preserve ambiguous delivery for the operator; never duplicate it.
            state["ts"] = match["ts"]
        if state.get("ts"):
            await client.chat_update(
                channel=event["channel"],
                ts=state["ts"],
                text=text,
                blocks=blocks(
                    event_id, text, active and not state.get("cancel"),
                    native_session=native_session,
                ),
            )
        else:
            state["sending"] = True
            generation = await asyncio.to_thread(
                approvals.save, storage, key(event_id), state, generation
            )
            try:
                result = await client.chat_postMessage(
                    channel=event["channel"],
                    thread_ts=event.get("thread_ts") or event["ts"],
                    text=text,
                    blocks=blocks(event_id, text, active, native_session=native_session),
                    client_msg_id=message_id,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            except SlackApiError as exc:
                if exc.response.get("error") in {
                    "ratelimited",
                    "not_in_channel",
                    "channel_not_found",
                    "missing_scope",
                    "invalid_auth",
                    "account_inactive",
                    "token_revoked",
                }:
                    # These responses affirm rejection; ambiguous delivery stays unreplayed.
                    state["sending"] = False
                    await asyncio.to_thread(
                        approvals.save, storage, key(event_id), state, generation
                    )
                raise
            state["ts"] = result["ts"]
        state["rendered"] = signature
        await asyncio.to_thread(approvals.save, storage, key(event_id), state, generation)


async def report(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    # This is a user action, not best-effort telemetry: acknowledge only durable writes.
    await bridge.repository.record_failure(
        Observation(
            scope=observability.request_scope(bridge.app),
            kind="request",
            cause="request_reported",
            occurrence=digest(f"{event_id}:requester"),
            event_id=event_id,
            **observability.request_context(record),
        ),
        time.time(),
    )


async def handle(
    bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: BackgroundTasks
) -> dict[str, Any]:
    action = payload["actions"][0]
    event_id = str(action.get("value", ""))
    key(event_id)
    operation = str(action.get("action_id", "")).removeprefix(PREFIX)
    if operation not in {"continue", "cancel", "report"}:
        raise ValueError("Unknown request action")
    async with approvals.lease(bridge, f"request-controls:{event_id}"):
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        saved = await asyncio.to_thread(approvals.load_json, storage, key(event_id))
        if not record or not saved:
            raise ValueError("This request check-in is unavailable")
        state, generation = saved
        approvals.requester(payload, {**record, "notice_ts": state.get("ts")})
        grant = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, storage, record
        )
        if not grant.email or (
            record.get("project_id")
            and (not grant.allowed or grant.project_id != record["project_id"])
        ):
            raise PermissionError("Project access changed")
        if operation == "report":
            # Replays use the same incident occurrence; a pending report is retried by sync.
            state["report_pending"] = True
        elif record.get("status") not in {"complete", "failed"}:
            if operation == "cancel":
                await asyncio.to_thread(
                    storage.put,
                    f"{CHECKPOINT_PREFIX}/events/{event_id}/memory-cancelled.json",
                    b"true",
                )
                state["cancel"] = True
            else:
                state["acknowledged"] = list(state["reasons"])
        generation = await asyncio.to_thread(
            approvals.save, storage, key(event_id), state, generation
        )
        if state.get("report_pending"):
            # An independent durable delivery also retries reports after the task finishes.
            await job_lifecycle.enqueue(
                bridge.app,
                f"request-report-{event_id}",
                f"/api/v1/internal/slack-analysis/approvals/request/{event_id}",
            )
            await report(bridge, event_id, record)
            state.update(reported=True, report_pending=False)
            await asyncio.to_thread(approvals.save, storage, key(event_id), state, generation)
    background.add_task(observability.flush, bridge.repository, bridge.app, bridge.settings)
    background.add_task(sync, bridge, event_id, record)
    return {}


async def expire(bridge: BatchBridge, storage: Storage, event_id: str) -> None:
    # Reuse the authenticated control delivery. This check-in has no expiry or task deadline.
    record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    if record:
        await sync(bridge, event_id, record)
        await observability.flush(bridge.repository, bridge.app, bridge.settings)


KIND = approvals.Kind("request", (PREFIX,), key, None, handle, expire)
