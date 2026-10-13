"""Run-scoped human confirmation for replacing existing objects under a write grant."""

from __future__ import annotations

import asyncio
import re
import time
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound
from pydantic import BaseModel, Field
from slack_sdk.web.async_client import AsyncWebClient

from . import access, write_access
from .storage import Conflict, Storage
from .write_broker import relative_path

if TYPE_CHECKING:
    from .batch import BatchBridge

ACTION_PREFIX = "pistachio_overwrite_"


class Approval(BaseModel):
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str
    grant_id: str
    principal: str
    access_signature: str
    paths: list[str]
    generations: dict[str, int]
    expires_at: float
    status: Literal["pending", "approved", "cancelled"] = "pending"
    approved_at: float = 0


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid overwrite request")
    return f"write-access/approvals/{event_id}.json"


def load(storage: Storage, event_id: str) -> tuple[Approval, str] | None:
    try:
        raw, generation = storage.get(key(event_id))
    except FileNotFoundError:
        return None
    return Approval.model_validate_json(raw), generation


def targets(paths: list[str], grant: write_access.WriteGrant) -> list[str]:
    normalized = []
    for raw in paths:
        canonical = write_access.scope(raw)
        if raw.rstrip("/") != canonical.rstrip("/") or not canonical.startswith(grant.uri):
            raise ValueError("Choose output paths inside this run's write folder.")
        relative = canonical.removeprefix(grant.uri).rstrip("/")
        if not relative:
            raise ValueError("Name specific output files or subfolders, not the whole write root.")
        relative_path(relative)
        normalized.append(grant.uri + relative + ("/" if raw.endswith("/") else ""))
    result: list[str] = []
    for path in sorted(set(normalized), key=lambda p: (len(p), p)):
        if not any(parent.endswith("/") and path.startswith(parent) for parent in result):
            result.append(path)
    return result


def inspect(paths: list[str], grant: write_access.WriteGrant, maximum: int) -> dict[str, int]:
    from google.cloud import storage

    client = storage.Client()
    deadline = time.monotonic() + 20
    found: dict[str, int] = {}

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ValueError("Overwrite check timed out. Name a smaller output folder and retry.")
        return seconds

    def add(blob: Any, bucket: str) -> None:
        remaining()
        uri = f"gs://{bucket}/{blob.name}"
        if not uri.startswith(grant.uri) or not blob.generation:
            raise ValueError("Could not verify the existing output version.")
        found[relative_path(uri.removeprefix(grant.uri))] = int(blob.generation)
        if len(found) > maximum:
            raise ValueError(
                f"Confirm at most {maximum:,} existing files; choose a smaller folder."
            )

    for path in paths:
        target = urlsplit(path)
        prefix = target.path.lstrip("/")
        if not path.endswith("/"):
            blob = client.bucket(target.netloc).blob(prefix)
            try:
                blob.reload(timeout=remaining(), retry=None)
            except NotFound:
                continue
            add(blob, target.netloc)
            continue
        token = None
        while True:
            iterator = client.list_blobs(
                target.netloc,
                prefix=prefix,
                page_size=1000,
                page_token=token,
                timeout=remaining(),
                retry=None,
            )
            page = next(iter(iterator.pages), None)
            if page is not None:
                for blob in page:
                    if not blob.name.endswith("/"):
                        add(blob, target.netloc)
            remaining()
            token = iterator.next_page_token
            if not token:
                break
    return found


def expected_generation(
    storage: Storage, event_id: str, project_id: str, grant: write_access.WriteGrant, relative: str
) -> int:
    saved = load(storage, event_id)
    if not saved:
        return 0
    approval = saved[0]
    if (
        approval.event_id != event_id
        or approval.project_id != project_id
        or approval.grant_id != grant.id
        or approval.status != "approved"
        or not 0 < approval.approved_at <= approval.expires_at
    ):
        return 0
    return approval.generations.get(relative, 0)


def validate(
    bridge: BatchBridge, storage: Storage, record: dict[str, Any], approval: Approval
) -> None:
    current = access.resolve(bridge.app, bridge.settings, storage, record)
    if (
        not current.allowed
        or current.project_id != approval.project_id
        or current.email != approval.principal
        or current.signature != approval.access_signature
        or record.get("write_grant", {}).get("id") != approval.grant_id
    ):
        raise ValueError("Project access changed. Send a new request.")
    write_access.require_active(storage, approval.project_id, approval.grant_id, bridge.settings)


def blocks(approval: Approval, grant_uri: str) -> tuple[str, list[dict[str, Any]]]:
    lines = ["Before I start, approve overwriting matching files in:"]
    for path in approval.paths:
        count = sum(
            (grant_uri + name).startswith(path) if path.endswith("/") else grant_uri + name == path
            for name in approval.generations
        )
        if count:
            lines.append(
                f"`{escape(path, quote=False)}` — {count:,} existing "
                + ("file" if count == 1 else "files")
            )
    lines.append("Other existing files stay unchanged. This approval applies to this run only.")
    content: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": line}} for line in lines
    ]
    content.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label},
                    "action_id": ACTION_PREFIX + action,
                    "value": approval.event_id,
                    **({"style": "primary"} if action == "approve" else {}),
                }
                for action, label in [("approve", "Approve and run"), ("cancel", "Cancel")]
            ],
        }
    )
    return "\n".join(lines), content


async def ready(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage
) -> bool:
    if record.get("overwrite_checked"):
        if record.get("overwrite_paths"):
            saved = await asyncio.to_thread(load, storage, event_id)
            if saved is None:
                raise ValueError("Overwrite confirmation is unavailable. Send a new request.")
            await asyncio.to_thread(validate, bridge, storage, record, saved[0])
        return True
    frozen = record.get("write_grant")
    paths = record.get("direct_decision", {}).get("write_paths", [])
    if not frozen or not paths:
        record["overwrite_checked"] = True
        return True
    grant = write_access.WriteGrant.model_validate(frozen)
    await asyncio.to_thread(
        write_access.require_active, storage, record["project_id"], grant.id, bridge.settings
    )
    saved = await asyncio.to_thread(load, storage, event_id)
    if saved is None:
        normalized = targets(paths, grant)
        generations = await asyncio.to_thread(
            inspect, normalized, grant, bridge.settings.write_max_files
        )
        if not generations:
            record["overwrite_checked"] = True
            return True
        approval = Approval(
            event_id=event_id,
            project_id=record["project_id"],
            grant_id=grant.id,
            principal=record["principal"],
            access_signature=record["access_signature"],
            paths=normalized,
            generations=generations,
            expires_at=time.time() + 3600,
        )
        try:
            await asyncio.to_thread(
                storage.put_if, key(event_id), approval.model_dump_json().encode(), None
            )
        except Conflict:
            saved = await asyncio.to_thread(load, storage, event_id)
            assert saved is not None
            approval = saved[0]
    else:
        approval = saved[0]
    await asyncio.to_thread(validate, bridge, storage, record, approval)
    if approval.status == "approved" and 0 < approval.approved_at <= approval.expires_at:
        record.update(status="pending", overwrite_checked=True, overwrite_paths=approval.paths)
        return True
    if approval.status == "cancelled" or time.time() >= approval.expires_at:
        record["status"] = "failed"
        await bridge.notify(
            event_id, record, "failed", "Overwrite request cancelled or expired. Nothing started."
        )
        return False
    record.update(status="awaiting_approval", overwrite_approval=True)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    text, content = blocks(approval, grant.uri)
    if record.get("notice_text") != text:
        await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_update(
            channel=record["payload"]["event"]["channel"],
            ts=record["notice_ts"],
            text=text,
            blocks=content,
        )
        record.update(notice_text=text, notice_phase="overwrite")
    return False


def is_action(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "block_actions" and any(
        str(a.get("action_id", "")).startswith(ACTION_PREFIX) for a in payload.get("actions", [])
    )


async def handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", [])
    if len(actions) != 1 or actions[0].get("action_id") not in {
        ACTION_PREFIX + "approve",
        ACTION_PREFIX + "cancel",
    }:
        raise ValueError("Invalid overwrite action")
    event_id = str(actions[0].get("value", ""))
    key(event_id)
    mutex = f"claude-poll:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=60)
    if claim.status != "claimed":
        raise ValueError("Request is being updated. Try again shortly.")
    try:
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        saved = await asyncio.to_thread(load, storage, event_id)
        if not record or not saved:
            raise ValueError("Unknown overwrite request")
        event = record["payload"]["event"]
        if (
            payload.get("team", {}).get("id") != record["payload"]["team_id"]
            or payload.get("user", {}).get("id") != event["user"]
            or payload.get("channel", {}).get("id") != event["channel"]
            or payload.get("message", {}).get("ts") != record.get("notice_ts")
        ):
            raise ValueError("Only the requester can confirm this run in its original thread.")
        approval, generation = saved
        await asyncio.to_thread(validate, bridge, storage, record, approval)
        if record["status"] != "awaiting_approval":
            return {}  # A duplicate click must never restart a run.
        if approval.status == "pending":
            if time.time() >= approval.expires_at:
                raise ValueError("This overwrite request expired. Send it again.")
            approval.status = (
                "approved" if actions[0]["action_id"].endswith("approve") else "cancelled"
            )
            approval.approved_at = time.time() if approval.status == "approved" else 0
            await asyncio.to_thread(
                storage.put_if, key(event_id), approval.model_dump_json().encode(), generation
            )
        record["status"] = "pending" if approval.status == "approved" else "failed"
        text = (
            "Overwrite approved. Starting this run…"
            if approval.status == "approved"
            else "Cancelled. Nothing started."
        )
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        # Keep the existing queued poll as a durable fallback if this immediate enqueue fails.
        if approval.status == "approved":
            await bridge.schedule(event_id, record["tick"])
        await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_update(
            channel=event["channel"],
            ts=record["notice_ts"],
            text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
        return {}
    finally:
        await bridge.repository.release_event(mutex, claim.token)
