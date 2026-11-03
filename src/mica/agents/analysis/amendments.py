"""Optional Slack addenda, reconciled in the existing per-thread queue."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from . import access, project_memory, surface
from .config import CHECKPOINT_PREFIX
from .storage import Storage, update_model

if TYPE_CHECKING:
    from .batch import BatchBridge

ACTION_PREFIX = "mica_amend_"
MAX_ITEMS = 8
MAX_BYTES = 4000
WAITING = "Still working on your previous request. Add this to it, or keep it as a follow-up."
FOLLOW_UP = "Keeping this as a follow-up; I'll start after the earlier request finishes."
QUEUED = "I'll add this when the running task next checks for updates."
LATE = "The earlier request finished before it picked this up. Keeping this as a follow-up."


class Addendum(BaseModel):
    text: str = Field(max_length=MAX_BYTES)
    principal: str
    signature: str
    created: float
    consumed: bool = False
    consumed_attempt: int = -1


class Addenda(BaseModel):
    closed: bool = False
    items: dict[str, Addendum] = Field(default_factory=dict, max_length=MAX_ITEMS)


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid amendment request")
    return f"{CHECKPOINT_PREFIX}/events/{event_id}/addenda.json"


def load(storage: Storage, target: str) -> Addenda:
    try:
        raw, _ = storage.get(key(target))
        return Addenda.model_validate_json(raw)
    except FileNotFoundError:
        return Addenda()


def close(storage: Storage, target: str) -> Addenda:
    return update_model(storage, key(target), Addenda, lambda doc: setattr(doc, "closed", True))


def add(storage: Storage, target: str, event_id: str, item: Addendum) -> None:
    key(event_id)

    def change(doc: Addenda) -> None:
        if event_id in doc.items:
            return
        if doc.closed:
            raise ValueError(LATE)
        if len(doc.items) >= MAX_ITEMS or len(item.text.encode()) > MAX_BYTES:
            raise ValueError("This addition is too large. Keep it as a follow-up instead.")
        doc.items[event_id] = item

    update_model(storage, key(target), Addenda, change)


def consume(storage: Storage, target: str, principal: str, signature: str, attempt: int = 0) -> str:
    selected: list[str] = []

    def change(doc: Addenda) -> None:
        selected.clear()  # A failed compare-and-swap retries against fresh state.
        if doc.closed:
            return
        for item in doc.items.values():
            if not item.consumed and item.principal == principal and item.signature == signature:
                selected.append(item.text)
                item.consumed = True
                item.consumed_attempt = attempt

    update_model(storage, key(target), Addenda, change)
    if not selected:
        return ""
    return (
        "Additional user context for the current request (same requester and project). "
        "Use it where compatible with work already completed; do not restart or cancel work. "
        "If it contradicts the task, explain the limitation in your final answer.\n"
        + "\n\n".join(selected)
    )


def eligible(bridge: BatchBridge, record: dict[str, Any], previous: dict[str, Any]) -> bool:
    event = record["payload"]["event"]
    return bool(
        bridge.settings.amendments
        and not surface.is_email(record)
        and not surface.is_email(previous)
        and previous.get("amendments_enabled")
        and previous.get("status") == "running"
        and previous.get("route") == "compute"
        and previous.get("job")
        and not previous.get("cancel_requested")
        and record.get("intake_principal")
        and record.get("intake_principal") == previous.get("principal")
        and record.get("thread_key") == previous.get("thread_key")
        and event.get("text", "").strip()
        and len(event.get("text", "").encode()) <= MAX_BYTES
        and not event.get("files")
        and not record.get("write_control")
    )


def buttons(event_id: str) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": WAITING}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label},
                    "action_id": ACTION_PREFIX + action,
                    "value": event_id,
                }
                for action, label in [("add", "Add this to it"), ("follow", "Keep as a follow-up")]
            ],
        },
    ]


def is_action(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "block_actions" and any(
        str(action.get("action_id", "")).startswith(ACTION_PREFIX)
        for action in payload.get("actions", [])
    )


async def handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", [])
    if len(actions) != 1 or actions[0].get("action_id") not in {
        ACTION_PREFIX + "add",
        ACTION_PREFIX + "follow",
    }:
        raise ValueError("Invalid amendment action")
    event_id = str(actions[0].get("value", ""))
    key(event_id)
    mutex = f"claude-poll:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=60)
    if claim.status != "claimed":
        raise ValueError("Request is being updated. Try again shortly.")
    try:
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if not record or not record.get("amendable"):
            raise ValueError("This request is no longer available to amend.")
        event = record["payload"]["event"]
        if (
            payload.get("team", {}).get("id") != record["payload"]["team_id"]
            or payload.get("user", {}).get("id") != event["user"]
            or payload.get("channel", {}).get("id") != event["channel"]
            or payload.get("message", {}).get("ts") != record.get("notice_ts")
        ):
            raise ValueError("Only the requester can choose in the original thread.")
        if record.get("amendment_choice") or record["status"] != "pending":
            return {}
        target = record["amendable"]
        existing = await asyncio.to_thread(load, storage, target)
        if event_id in existing.items:
            record["amendment_choice"] = "add"
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
            return {}  # First accepted choice wins, including a crash before receipt save.
        if (
            "conversation_amendable" not in record
            and record.get("notice_phase") == "waiting"
            and record.get("notice_text") == WAITING
        ):
            # The previously delivered choice is the migration witness; do not reuse
            # a retired classifier result or permit newly forged command actions.
            record["conversation_amendable"] = True
        if record.get("conversation_amendable") is not True:
            raise ValueError("Commands keep their normal follow-up flow.")
        # Serialize click eligibility with completion/recovery of the running request.
        parent_mutex = f"claude-poll:{target}"
        parent_claim = await bridge.repository.begin_event(parent_mutex, lease_seconds=60)
        if parent_claim.status != "claimed":
            raise ValueError("The earlier request is being updated. Try again shortly.")
        try:
            previous = await bridge.repository.get_connector_state(f"claude-event:{target}")
            text = FOLLOW_UP
            record["amendment_choice"] = "follow"
            if actions[0]["action_id"].endswith("add"):
                if previous and eligible(bridge, record, previous):
                    grant = await asyncio.to_thread(
                        access.resolve, bridge.app, bridge.settings, storage, record
                    )
                    if (
                        not grant.allowed
                        or grant.email != previous.get("principal")
                        or grant.signature != previous.get("access_signature")
                        or grant.project_id != previous.get("project_id")
                    ):
                        raise ValueError("Project access changed. Keep this as a follow-up.")
                    raw = event.get("text", "")
                    scrubbed = (
                        "[Credential-like content removed from this addition.]"
                        if project_memory.SECRET.search(raw)
                        else raw
                    )
                    await asyncio.to_thread(
                        add,
                        storage,
                        target,
                        event_id,
                        Addendum(
                            text=scrubbed,
                            principal=grant.email,
                            signature=grant.signature,
                            created=time.time(),
                        ),
                    )
                    record["amendment_choice"] = "add"
                    text = QUEUED
                else:
                    text = LATE
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
            await bridge.notify(event_id, record, "waiting", text)
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        finally:
            await bridge.repository.release_event(parent_mutex, parent_claim.token)
        return {}
    finally:
        await bridge.repository.release_event(mutex, claim.token)


async def reconcile(
    bridge: BatchBridge, event_id: str, record: dict[str, Any]
) -> Literal["wait", "complete", "follow"]:
    """Hold the original queue slot until the predecessor ends; never rewire its tail."""
    target = record.get("amendable")
    if not target or record.get("amendment_resolved"):
        return "follow"
    previous = await bridge.repository.get_connector_state(f"claude-event:{target}")
    done = previous is None or previous["status"] in {"complete", "failed"}
    doc = await asyncio.to_thread(close if done else load, bridge.storage, target)
    item = doc.items.get(event_id)
    if item:
        record["amendment_choice"] = "add"  # Repairs a crash after the CAS, before receipt save.
        if not done:
            await bridge.notify(
                event_id,
                record,
                "waiting",
                "Added to the running task; waiting for its answer." if item.consumed else QUEUED,
            )
            return "wait"
        if item.consumed:
            uncertain = (
                not previous
                or previous["status"] == "failed"
                or (item.consumed_attempt != int(previous.get("attempt", 0)))
            )
            record.update(
                status="failed" if uncertain else "complete",
                consumed_into=target,
                terminal_recorded=True,
            )
            await bridge.notify(
                event_id,
                record,
                record["status"],
                "The earlier worker stopped or restarted after receiving this addition. "
                "I can't confirm it was completed. Check its result and resend this message "
                "if it still needs handling."
                if uncertain
                else "Added to your earlier request.",
            )
            return "complete"
        record.update(amendment_choice="follow", amendment_resolved=True)
        await bridge.notify(event_id, record, "waiting", LATE)
    elif done and not record.get("amendment_choice"):
        record["amendment_choice"] = "follow"
        await bridge.notify(event_id, record, "waiting", FOLLOW_UP)
    return "follow"
