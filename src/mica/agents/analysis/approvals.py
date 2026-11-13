"""Approval dispatch, durable documents and presentation shared across approval kinds.

Kinds retain their domain authorization and idempotency checks. Existing object generations
and repository leases are kept: moving an approval must not weaken its concurrency boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel
from slack_sdk.web.async_client import AsyncWebClient

from .slack_format import NoticeState, status_blocks
from .storage import Storage
from .transport import SlackTransport

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

    from .batch import BatchBridge

Handler = Callable[["BatchBridge", Storage, dict[str, Any], "BackgroundTasks"],
                   Awaitable[dict[str, Any]]]
Expiry = Callable[["BatchBridge", Storage, str], Awaitable[None]]


@dataclass(frozen=True)
class Kind:
    name: str
    prefixes: tuple[str, ...]
    key: Callable[[str], str]
    lifetime: int | None
    decide: Handler
    expire: Expiry
    callback: str = ""
    error_block: str = "project"

    def matches(self, payload: dict[str, Any]) -> bool:
        if payload.get("type") == "view_submission":
            return bool(
                self.callback and payload.get("view", {}).get("callback_id") == self.callback
            )
        if payload.get("type") != "block_actions":
            return False
        return any(
            isinstance(action, dict)
            and str(action.get("action_id", "")).startswith(self.prefixes)
            for action in payload.get("actions", [])
        )

    def deadline(self) -> float:
        if self.lifetime is None:
            raise ValueError("This approval ends with its predecessor, not a timer.")
        return time.time() + self.lifetime


def kinds() -> tuple[Kind, ...]:
    # Lazy imports preserve the existing public modules and avoid registration-order coupling.
    from . import (
        access_requests,
        amendments,
        cloud_access,
        command_approval,
        compute_approval,
        project_picker,
        request_controls,
        session_import,
        write_approval,
    )

    return tuple(module.KIND for module in (
        compute_approval, command_approval, write_approval,
        access_requests, amendments, cloud_access, session_import, request_controls, project_picker,
    ))


def resolve(name: str) -> Kind:
    return next((kind for kind in kinds() if kind.name == name), None) or _unknown()


def _unknown() -> Kind:
    raise ValueError("Unknown approval kind")


def is_action(payload: dict[str, Any]) -> bool:
    return any(kind.matches(payload) for kind in kinds())


def modal_error(payload: dict[str, Any], error: Exception) -> dict[str, Any]:
    kind = next(kind for kind in kinds() if kind.matches(payload))
    return {"response_action": "errors", "errors": {
        getattr(error, "block_id", kind.error_block): str(error)
    }}


async def handle(
    bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: BackgroundTasks
) -> dict[str, Any]:
    matched = [kind for kind in kinds() if kind.matches(payload)]
    if len(matched) != 1 or (
        payload.get("type") != "view_submission" and len(payload.get("actions", [])) != 1
    ):
        raise ValueError("Choose one approval action from its original card.")
    return await matched[0].decide(bridge, storage, payload, background)


def load_json(storage: Storage, path: str) -> tuple[dict[str, Any], str] | None:
    try:
        raw, generation = storage.get(path)
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid saved approval")
    return value, generation


def load_model[Model: BaseModel](
    storage: Storage, path: str, model: type[Model]
) -> tuple[Model, str] | None:
    saved = load_json(storage, path)
    return (model.model_validate(saved[0]), saved[1]) if saved else None


def save(
    storage: Storage, path: str, value: BaseModel | dict[str, Any], generation: str | None
) -> str:
    """Every update is conditional; None means create-only, never an unconditional write."""
    raw = (
        value.model_dump_json().encode()
        if isinstance(value, BaseModel) else json.dumps(value).encode()
    )
    return storage.put_if(path, raw, generation)


@asynccontextmanager
async def lease(
    bridge: BatchBridge, key: str, *, seconds: int = 60,
    busy: str = "Request is being updated. Try again shortly.",
    busy_error: type[Exception] = ValueError,
) -> AsyncIterator[None]:
    claim = await bridge.repository.begin_event(key, lease_seconds=seconds)
    if claim.status != "claimed":
        raise busy_error(busy)
    try:
        yield
    finally:
        await bridge.repository.release_event(key, claim.token)


def expired(deadline: float) -> bool:
    return not math.isfinite(deadline) or time.time() >= deadline


def requester(payload: dict[str, Any], record: dict[str, Any]) -> None:
    event = record["payload"]["event"]
    if (
        payload.get("team", {}).get("id") != record["payload"]["team_id"]
        or payload.get("user", {}).get("id") != event["user"]
        or payload.get("channel", {}).get("id") != event["channel"]
        or payload.get("message", {}).get("ts") != record.get("notice_ts")
    ):
        raise ValueError("Only the requester can confirm in the original thread.")


async def card(
    client: AsyncWebClient, channel: str, ts: str, text: str,
    blocks: list[dict[str, Any]] | None = None,
    *, state: NoticeState | None = None, detail: str = "",
) -> None:
    if blocks is None:
        blocks = status_blocks("", text, state=state, detail=detail) if state is not None else [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        ]
    await SlackTransport(client).update(
        channel=channel, ts=ts, text=text, blocks=blocks,
    )


async def request_card(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], client: AsyncWebClient,
    text: str, blocks: list[dict[str, Any]],
) -> None:
    """An approval can be the first real content in a native session."""
    event = record["payload"]["event"]
    if record.get("notice_ts"):
        await card(client, event["channel"], record["notice_ts"], text, blocks)
        return
    response = await SlackTransport(client).post(
        channel=event["channel"], thread_ts=event.get("thread_ts") or event["ts"],
        text=text, blocks=blocks, unfurl_links=False, unfurl_media=False,
        client_msg_id=str(uuid5(NAMESPACE_URL, f"pistachio-receipt:{event_id}")),
    )
    record["notice_ts"] = response["ts"]
    record.pop("notice_pending_phase", None)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)


async def resumed_card(
    client: AsyncWebClient, channel: str, ts: str, title: str, detail: str,
    *, native_session: bool = False,
) -> None:
    """The decision and resumption must already be durable; UI failure cannot undo them."""
    try:
        await card(
            client, channel, ts, title,
            status_blocks("", title, detail=detail, native_session=native_session),
        )
    except Exception:
        # The scheduled controller owns subsequent status recovery. Never repeat the operation.
        log.warning("Approved request status could not be delivered", exc_info=True)


def expiry_blocks(kind: Kind, identifier: str, text: str) -> list[dict[str, Any]]:
    return [
        *status_blocks("", text, state="expired"),
        {"type": "actions", "elements": [{
            "type": "button", "text": {"type": "plain_text", "text": "Request again"},
            "action_id": kind.prefixes[0] + "again", "value": identifier,
        }]},
    ]


async def expired_card(
    bridge: BatchBridge, kind: Kind, identifier: str, record: dict[str, Any],
    *, text: str = ("This approval expired. Your request and saved work are kept. "
                    "Request again for fresh confirmation."),
) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    event = record["payload"]["event"]
    await card(
        AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5), event["channel"],
        record["notice_ts"], text, expiry_blocks(kind, identifier, text),
    )
    record.update(notice_text=text, notice_phase="approval_expired", approval_expired=kind.name)


async def watch(
    bridge: BatchBridge, kind: Kind, identifier: str, deadline: float,
    record: dict[str, Any] | None = None,
    *, remind: bool = True,
) -> None:
    """One durable timer per confirmation; old timers cannot expire a renewed request."""
    from . import approval_reminders
    from .job_lifecycle import enqueue

    kind.key(identifier)
    stamp = math.ceil(deadline)
    if remind and kind.lifetime is not None:
        await approval_reminders.schedule(bridge, kind.name, identifier, deadline)
    if record is not None and record.get("approval_timers", {}).get(kind.name) == stamp:
        return
    await enqueue(
        bridge.app, f"approval-{kind.name}-{identifier}-{stamp}",
        f"/api/v1/internal/slack-analysis/approvals/{kind.name}/{identifier}",
        max(0, stamp - int(time.time())),
    )
    if record is not None:
        record.setdefault("approval_timers", {})[kind.name] = stamp


async def renew_run(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    """A saved confirmation can resume only while it still owns its original queue position."""
    if record.get("job") or record.get("cancel_requested"):
        raise ValueError("This run already started or was cancelled. Send a fresh request.")
    tail = await bridge.repository.get_connector_state("claude-thread:" + record["thread_key"])
    if not tail or tail.get("tail") != event_id:
        raise ValueError(
            "This conversation moved on. Send a fresh request; saved work is still available."
        )
    record.update(status="awaiting_approval", tick=record.get("tick", 0) + 1)
    for field in ("approval_expired", "terminal_recorded", "failure_cause"):
        record.pop(field, None)


async def save_record(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    await bridge.repository.set_connector_state("claude-event:" + event_id, record)


async def expire_run(
    bridge: BatchBridge, storage: Storage, kind: Kind, event_id: str, record: dict[str, Any],
    row: BaseModel, generation: str,
) -> None:
    """Expire an unstarted run without deleting its request, checkpoints or output references."""
    value = row.model_dump(mode="json")
    if value["status"] not in {"pending", "expired"} or record.get("job"):
        return
    if not expired(value["expires_at"]):
        return
    value["status"] = "expired"
    await asyncio.to_thread(save, storage, kind.key(event_id), value, generation)
    record.update(status="failed", approval_expired=kind.name)
    await save_record(bridge, event_id, record)
    await expired_card(bridge, kind, event_id, record)
    await save_record(bridge, event_id, record)
