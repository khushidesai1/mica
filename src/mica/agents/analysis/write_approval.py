"""Requester-bound confirmation for replacements, moves and deletes under a write grant."""

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

from . import access, agent_session, approvals, gcs_operations, surface, write_access
from .storage import Conflict, Storage
from .write_broker import relative_path

if TYPE_CHECKING:
    from .batch import BatchBridge

ACTION_PREFIX = "mica_overwrite_"
ACTION_PREFIXES = (ACTION_PREFIX, "pistachio_overwrite_")


class Approval(BaseModel):
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str
    grant_id: str
    principal: str
    access_signature: str
    paths: list[str]
    inspected_at: float
    expires_at: float
    status: Literal["pending", "approved", "cancelled", "expired"] = "pending"
    approved_at: float = 0
    operations: list[gcs_operations.Operation] = Field(default_factory=list)
    snapshot_id: str = ""
    plan_ready: bool = False
    operation_counts: list[int] = Field(default_factory=list)


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid overwrite request")
    return f"write-access/approvals/{event_id}.json"


def load(storage: Storage, event_id: str) -> tuple[Approval, str] | None:
    saved = approvals.load_json(storage, key(event_id))
    if saved is None:
        return None
    if "generations" in saved[0]:
        raise ValueError("This overwrite request expired during an update. Send it again.")
    return Approval.model_validate(saved[0]), saved[1]


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


def inspect(paths: list[str]) -> list[str]:
    """Probe each requested path once; approval size is independent of object count."""
    from google.cloud import storage

    client = storage.Client()
    found = []
    for path in paths:
        target = urlsplit(path)
        prefix = target.path.lstrip("/")
        if path.endswith("/"):
            probe = client.list_blobs(
                target.netloc, prefix=prefix, max_results=1, page_size=1, timeout=20, retry=None
            )
            if next(iter(probe), None) is not None:
                found.append(path)
        else:
            try:
                client.bucket(target.netloc).blob(prefix).reload(timeout=20, retry=None)
            except NotFound:
                continue
            found.append(path)
    return found


def allowed_replacement(approval: Approval | None, uri: str, blob: Any) -> int:
    if approval is None or not any(
        uri.startswith(path) if path.endswith("/") else uri == path for path in approval.paths
    ):
        return 0
    if approval.status != "approved" or not 0 < approval.approved_at <= approval.expires_at:
        return 0
    if blob.time_created is None or blob.time_created.timestamp() > approval.inspected_at:
        raise FileExistsError("The destination changed since approval. Request a new run.")
    return int(blob.generation)


def validate(
    bridge: BatchBridge, storage: Storage, record: dict[str, Any], approval: Approval
) -> None:
    if record.get("ambient"):
        raise ValueError("Background runs publish new objects only")
    current = access.resolve(bridge.app, bridge.settings, storage, record)
    if (
        not current.allowed
        or current.project_id != approval.project_id
        or current.email != approval.principal
        or current.signature != approval.access_signature
        or record.get("write_grant", {}).get("id") != approval.grant_id
    ):
        raise ValueError("Project access changed. Send a new request.")
    grant = write_access.require_active(
        storage, approval.project_id, approval.grant_id, bridge.settings, approval.event_id
    )
    if isinstance(grant, write_access.EventWriteGrant):
        raise ValueError("Background runs publish new objects only")
    if approval.operations:
        requested = [gcs_operations.Operation.model_validate(op) for op in
                     record.get("direct_decision", {}).get("write_operations", [])]
        if (surface.is_email(record) or record.get("route") != "storage"
                or gcs_operations.normalized(requested, grant) != approval.operations):
            raise ValueError("The requested GCS operations changed. Send a new request.")


def blocks(approval: Approval, grant_uri: str) -> tuple[str, list[dict[str, Any]]]:
    lines = ["Confirm these GCS operations:"]
    if approval.operations:
        for operation, count in zip(approval.operations, approval.operation_counts, strict=True):
            lines.append(operation.describe() + f" ({count} inspected objects)")
        lines.append(
            "Moves require an empty destination and delete each source only after its copy is "
            "verified. Deletes remove the inspected objects. Changed or newly added objects "
            "are preserved. Folder operations can finish partially if interrupted."
        )
    else:
        for path in approval.paths:
            target = escape(path, quote=False)
            lines.append(
                f"Replace matching files within folder `{target}` with this run's outputs."
                if path.endswith("/") else f"Replace file `{target}` with this run's output."
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
    if (record.get("ambient") or {}).get("kind") == "session_import":
        if record.get("write_grant"):
            raise PermissionError("Import passes cannot publish data outputs")
        record.update(overwrite_checked=True, overwrite_paths=[])
        return True
    if record.get("ambient"):
        frozen = record.get("write_grant") or {}
        grant = await asyncio.to_thread(
            write_access.require_active, storage, record["project_id"],
            frozen.get("id", ""), bridge.settings, event_id,
        )
        if not isinstance(grant, write_access.EventWriteGrant):
            raise PermissionError("Background publishing requires its event grant")
        record.update(overwrite_checked=True, overwrite_paths=[])
        return True
    operations = record.get("direct_decision", {}).get("write_operations", [])
    if record.get("overwrite_checked") and not operations:
        if record.get("overwrite_paths"):
            saved = await asyncio.to_thread(load, storage, event_id)
            if saved is None:
                raise ValueError("Overwrite confirmation is unavailable. Send a new request.")
            await asyncio.to_thread(validate, bridge, storage, record, saved[0])
        return True
    frozen = record.get("write_grant")
    paths = record.get("direct_decision", {}).get("write_paths", [])
    if not frozen or (not paths and not operations):
        record["overwrite_checked"] = True
        return True
    grant = write_access.WriteGrant.model_validate(frozen)
    await asyncio.to_thread(
        write_access.require_active, storage, record["project_id"], grant.id, bridge.settings
    )
    saved = await asyncio.to_thread(load, storage, event_id)
    if saved is None:
        normalized = targets(paths, grant) if not operations else []
        inspected_at = time.time()
        existing = await asyncio.to_thread(inspect, normalized) if not operations else []
        if not existing and not operations:
            record["overwrite_checked"] = True
            return True
        approval = Approval(
            event_id=event_id,
            project_id=record["project_id"],
            grant_id=grant.id,
            principal=record["principal"],
            access_signature=record["access_signature"],
            paths=existing,
            inspected_at=inspected_at,
            expires_at=KIND.deadline(),
            operations=gcs_operations.normalized(
                [gcs_operations.Operation.model_validate(op) for op in operations], grant
            ) if operations else [],
            snapshot_id=gcs_operations.new_snapshot() if operations else "",
        )
        try:
            generation = await asyncio.to_thread(
                approvals.save, storage, key(event_id), approval, None
            )
        except Conflict:
            saved = await asyncio.to_thread(load, storage, event_id)
            assert saved is not None
            approval, generation = saved
    else:
        approval, generation = saved
    await asyncio.to_thread(validate, bridge, storage, record, approval)
    if (approval.operations and approval.status == "pending" and not approval.plan_ready
        and not await gcs_operations.prepare_approval(
            bridge, storage, record, approval, generation
        )):
        return False
    if approval.status == "approved" and 0 < approval.approved_at <= approval.expires_at:
        record.update(status="pending", overwrite_checked=True, overwrite_paths=approval.paths)
        return True
    if approval.status == "expired" or approvals.expired(approval.expires_at):
        await expire_record(bridge, storage, event_id, record)
        return False
    if approval.status == "cancelled":
        record["status"] = "failed"
        record["cancel_requested"] = approval.status == "cancelled"
        await bridge.notify(
            event_id, record, "failed", "GCS operation request cancelled. Nothing started."
        )
        return False
    await approvals.watch(bridge, KIND, event_id, approval.expires_at, record)
    record.update(status="awaiting_approval", overwrite_approval=True)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    text, content = blocks(approval, grant.uri)
    if record.get("notice_text") != text:
        await approvals.request_card(
            bridge, event_id, record,
            AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
            text, content,
        )
        record.update(notice_text=text, notice_phase="overwrite")
    return False


def is_action(payload: dict[str, Any]) -> bool:
    return KIND.matches(payload)



async def _handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", [])
    if len(actions) != 1 or actions[0].get("action_id") not in {
        prefix + action for prefix in ACTION_PREFIXES for action in ("approve", "cancel", "again")
    }:
        raise ValueError("Invalid overwrite action")
    event_id = str(actions[0].get("value", ""))
    key(event_id)
    async with approvals.lease(bridge, f"claude-poll:{event_id}"):
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        saved = await asyncio.to_thread(load, storage, event_id)
        if not record or not saved:
            raise ValueError("Unknown overwrite request")
        event = record["payload"]["event"]
        approvals.requester(payload, record)
        approval, generation = saved
        await asyncio.to_thread(validate, bridge, storage, record, approval)
        if actions[0]["action_id"].endswith("again"):
            if approval.status not in {"expired", "pending"}:
                return {}
            async with approvals.lease(bridge, "claude-intake:" + record["thread_key"]):
                if record["status"] != "awaiting_approval":
                    await approvals.renew_run(bridge, event_id, record)
                if approval.status == "expired":
                    approval.inspected_at = time.time()
                    if approval.operations:
                        approval.snapshot_id = gcs_operations.new_snapshot()
                        approval.plan_ready = False
                        approval.operation_counts = []
                    else:
                        await asyncio.to_thread(inspect, approval.paths)
                    approval.status, approval.expires_at = "pending", KIND.deadline()
                    await asyncio.to_thread(
                        approvals.save, storage, key(event_id), approval, generation
                    )
                await ready(bridge, event_id, record, storage)
                await approvals.save_record(bridge, event_id, record)
                await bridge.schedule(event_id, record["tick"])
            return {}
        if approval.status == "expired":
            await expire_record(bridge, storage, event_id, record)
            return {}
        if record["status"] != "awaiting_approval":
            return {}  # A duplicate click must never restart a run.
        if approval.status == "pending":
            if approval.operations and not approval.plan_ready:
                raise ValueError("Inspection is not finished. Wait for the operation confirmation.")
            if approvals.expired(approval.expires_at):
                await expire_record(bridge, storage, event_id, record)
                raise ValueError(
                    "This overwrite request expired. Use Request again for fresh confirmation."
                )
            approval.status = (
                "approved" if actions[0]["action_id"].endswith("approve") else "cancelled"
            )
            approval.approved_at = time.time() if approval.status == "approved" else 0
            await asyncio.to_thread(
                approvals.save, storage, key(event_id), approval, generation
            )
        record["status"] = "pending" if approval.status == "approved" else "failed"
        record["cancel_requested"] = approval.status == "cancelled"
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        # Keep the existing queued poll as a durable fallback if this immediate enqueue fails.
        if approval.status == "approved":
            await bridge.schedule(event_id, record["tick"])
            receipt = (
                "Approved operations:\n" + "\n".join(
                    op.describe() + f" ({count} inspected objects)"
                    for op, count in zip(
                        approval.operations, approval.operation_counts, strict=True
                    )
                ) if approval.operations else
                "Approved replacement targets:\n" + "\n".join(approval.paths)
            )
            await approvals.resumed_card(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
                event["channel"], record["notice_ts"], "Preparing approved changes…", receipt,
                native_session=agent_session.eligible(bridge, record),
            )
        else:
            await approvals.card(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
                event["channel"], record["notice_ts"], "Cancelled. Nothing started.",
                state="cancelled",
            )
        return {}


async def expire_record(
    bridge: BatchBridge, storage: Storage, event_id: str, record: dict[str, Any]
) -> None:
    saved = await asyncio.to_thread(load, storage, event_id)
    if saved:
        await approvals.expire_run(bridge, storage, KIND, event_id, record, *saved)


async def expire(bridge: BatchBridge, storage: Storage, event_id: str) -> None:
    async with approvals.lease(bridge, f"claude-poll:{event_id}"):
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if record and record.get("overwrite_approval"):
            await expire_record(bridge, storage, event_id, record)


async def decide(bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: Any
                 ) -> dict[str, Any]:
    return await _handle(bridge, storage, payload)


KIND = approvals.Kind("write", ACTION_PREFIXES, key, 3600, decide, expire)


async def handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    from fastapi import BackgroundTasks

    return await approvals.handle(bridge, storage, payload, BackgroundTasks())
