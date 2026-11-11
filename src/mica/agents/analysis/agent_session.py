"""Slack session presentation, reconciled independently of analysis and delivery."""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from html import unescape
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import uuid4

from slack_sdk.web.async_client import AsyncWebClient

from . import approvals, identity, job_lifecycle, surface
from .data_links import message_text
from .transport import SlackTransport

if TYPE_CHECKING:
    from .batch import BatchBridge

log = logging.getLogger(__name__)
DUE: Final = "agent_session_due"
REFRESH_SECONDS = 1800
TERMINAL = {"complete", "failed"}
Status = Literal["processing", "suspended", "active"]


def timestamp(value: Any) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d{1,6}", value):
        raise ValueError("Invalid Slack timestamp")
    return Decimal(value)


def stop_as_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Adapt only a signed, well-formed DM control; retain its original delivery identity."""
    event = payload.get("event")
    if payload.get("type") != "event_callback" or not isinstance(event, dict):
        return None
    if event.get("type") != "agent_session_stopped" or event.get("bot_id"):
        return None
    for value, pattern in (
        (payload.get("team_id"), r"T[A-Z0-9]+"),
        (event.get("channel"), r"D[A-Z0-9]+"),
        (event.get("user"), r"[UW][A-Z0-9]+"),
    ):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            return None
    if not isinstance(payload.get("event_id"), str) or not payload["event_id"]:
        return None
    try:
        if timestamp(event.get("thread_ts")) > timestamp(event.get("event_ts")):
            return None
    except ValueError:
        return None
    return {
        **{k: payload[k] for k in ("type", "team_id", "event_id", "authorizations")
           if k in payload},
        "event": {
            "type": "message", "channel_type": "im", "channel": event["channel"],
            "user": event["user"], "ts": event["event_ts"],
            "thread_ts": event["thread_ts"], "text": "cancel",
        },
    }


def eligible(bridge: BatchBridge, record: dict[str, Any]) -> bool:
    return bool(
        bridge.settings.slack_sessions
        and bridge.app.slack_agent_backend == "claude_batch"
        and not surface.is_email(record)
        and not record.get("ambient")
        and identity.is_private(record.get("payload", {}).get("event", {}))
    )


def mark(bridge: BatchBridge, record: dict[str, Any]) -> None:
    """Persist with the work receipt, before dispatch, so a process crash cannot lose the UI."""
    if eligible(bridge, record):
        record[DUE] = time.time()


async def enqueue(bridge: BatchBridge, record: dict[str, Any]) -> bool:
    if not eligible(bridge, record):
        return False
    try:
        await job_lifecycle.enqueue(
            bridge.app, f"mica-agent-session-{uuid4().hex}",
            f"/api/v1/internal/slack-analysis/agent-session/{record['event_id']}",
        )
        return True
    except Exception as exc:
        # The receipt's due marker remains for the five-minute maintenance reconciler.
        log.warning("Slack session dispatch pending: %s", type(exc).__name__)
        return False


def desired(records: list[dict[str, Any]]) -> Status:
    """Records are newest first; a blocked follow-up does not override its predecessor."""
    active = [r for r in reversed(records) if r.get("status") not in TERMINAL
              and not r.get("control_bypass") and not r.get("ambient")]
    if not active:
        return "active"
    if any(r.get("cancel_control") or r.get("cancel_requested")
           or r.get("payload", {}).get("event", {}).get("text", "").strip() == "cancel"
           for r in active):
        return "processing"
    # A later amendment choice does not suspend the worker already running before it.
    if any(r.get("status") in {"running", "submitting"} for r in active):
        return "processing"
    first = active[0]
    waiting = first.get("status") == "awaiting_approval" or (
        first.get("amendable") and first.get("conversation_amendable") is True
        and not first.get("amendment_choice")
    )
    return "suspended" if waiting else "processing"


async def sync(bridge: BatchBridge, event_id: str) -> None:
    if not bridge.settings.slack_sessions or bridge.app.slack_agent_backend != "claude_batch":
        return  # Queued tasks must honor rollback too.
    trigger = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    if not trigger or not eligible(bridge, trigger):
        return
    thread_key = trigger["thread_key"]
    async with approvals.lease(bridge, f"claude-intake:{thread_key}", seconds=180):
        tail = await bridge.repository.get_connector_state(f"claude-thread:{thread_key}") or {}
        cursor = tail.get("tail")
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(100):
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Cyclic Slack session queue")
            seen.add(cursor)
            record = await bridge.repository.get_connector_state(f"claude-event:{cursor}")
            if not record or record.get("thread_key") != thread_key:
                raise ValueError("Incomplete Slack session queue")
            records.append(record)
            cursor = record.get("previous")
            if (record.get("status") in TERMINAL and not record.get("control_bypass")
                    and not record.get("cancel_control") and not record.get("ambient")):
                break  # Same completed predecessor boundary as the work queue.
        else:
            raise ValueError("Slack session queue exceeds the safe limit")
        if not records or not eligible(bridge, records[0]):
            return
        event = records[0]["payload"]["event"]
        thread = event.get("thread_ts") or event["ts"]
        state_key = f"slack-session:{thread_key}"
        state = await bridge.repository.get_connector_state(state_key) or {}
        status = desired(records)
        if state.get("status") != status or (
            status == "processing" and time.time() - state.get("sent_at", 0) >= REFRESH_SECONDS
        ):
            options: dict[str, Any] = {}
            if not state:
                from .batch import digest

                root_id = digest(f"{records[0]['payload']['team_id']}:{event['channel']}:{thread}")
                root = await bridge.repository.get_connector_state(f"claude-event:{root_id}")
                root_event = (root or records[-1])["payload"]["event"]
                title = unescape(message_text(root_event.get("text", "")))
                title = " ".join(title.split())[:200] or "Mica analysis"
                options.update(title=title, initiator_user_id=root_event["user"])
            client = SlackTransport(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5, retry_handlers=[])
            )
            await client.set_status(
                channel_id=event["channel"], thread_ts=thread, status=status, **options
            )
            state = {"status": status, "sent_at": time.time()}
            await bridge.repository.set_connector_state(state_key, state)
        # Only patch projection fields: never overwrite a concurrent controller's receipt.
        for record in [*records, trigger]:
            if DUE not in record:
                continue
            key = f"claude-event:{record['event_id']}"
            if record.get("status") in TERMINAL:
                mutex = f"claude-poll:{record['event_id']}"
                claim = await bridge.repository.begin_event(mutex, lease_seconds=30)
                if claim.status != "claimed":
                    continue
                try:
                    current = await bridge.repository.get_connector_state(key)
                    if current and current.get("status") in TERMINAL:
                        await bridge.repository.patch_connector_state(key, {}, remove=(DUE,))
                finally:
                    await bridge.repository.release_event(mutex, claim.token)
            # Nonterminal markers stay due. Maintenance checks every five minutes, while
            # confirmed Slack status refreshes only every thirty. Moving due into the future
            # could overwrite a concurrent completion's urgent marker.


async def maintenance(bridge: BatchBridge) -> int:
    if not bridge.settings.slack_sessions or bridge.app.slack_agent_backend != "claude_batch":
        return 0
    rows = await bridge.repository.due_connector_states(DUE, time.time(), limit=100)
    dispatched: set[str] = set()
    for key, record in rows:
        if (key.startswith("claude-event:") and eligible(bridge, record)
                and record["thread_key"] not in dispatched and await enqueue(bridge, record)):
            dispatched.add(record["thread_key"])
            # Rotate successful dispatches out of this bounded scan, so one long-running
            # queue cannot starve newer outboxes. An enqueue failure retains its due marker.
            await bridge.repository.patch_connector_state(key, {DUE: time.time() + 300})
    return len(dispatched)
