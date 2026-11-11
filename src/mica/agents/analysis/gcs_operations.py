"""Confirmed GCS moves/deletes, executed by the controller with exact-object credentials."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from google.api_core.exceptions import GoogleAPICallError, NotFound, PreconditionFailed
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import access, approvals, memory_workspace, surface, write_access, write_broker
from .config import Settings
from .storage import Conflict, Storage

if TYPE_CHECKING:
    from .batch import BatchBridge
    from .write_approval import Approval

PAGE_SIZE = 500
SLICE_SECONDS = 120


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["move", "delete"]
    source: str = Field(min_length=1, max_length=2048)
    destination: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def destinations(self) -> Operation:
        if (self.action == "move") != bool(self.destination):
            raise ValueError("A move needs a destination; a delete must not have one.")
        if self.destination and self.source.endswith("/") != self.destination.endswith("/"):
            raise ValueError("Use trailing slashes for both folders, or neither for files.")
        return self

    def describe(self) -> str:
        kind = "folder" if self.source.endswith("/") else "file"
        source = escape(self.source, quote=False)
        if self.action == "delete":
            return f"Delete {kind} `{source}`"
        return f"Move {kind} `{source}` → `{escape(self.destination, quote=False)}`"


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[Operation] = Field(min_length=1, max_length=10)


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    destination: str
    generation: int = Field(gt=0)
    metageneration: int = Field(gt=0)
    size: int = Field(ge=0)
    crc32c: str = Field(min_length=1)


def checked_entry(operation: Operation, raw: dict[str, Any]) -> dict[str, Any]:
    entry = Entry.model_validate(raw)
    if (
        entry.source.startswith(operation.source)
        if operation.source.endswith("/")
        else entry.source == operation.source
    ):
        suffix = entry.source.removeprefix(operation.source)
        expected = operation.destination + suffix if operation.destination else ""
        if entry.destination == expected:
            return entry.model_dump()
    raise ValueError("An inventory object does not match the confirmed operation.")


def normalized(operations: list[Operation], grant: write_access.WriteGrant) -> list[Operation]:
    from .write_approval import targets

    if isinstance(grant, write_access.EventWriteGrant):
        raise ValueError("Background runs cannot move or delete data.")
    Request(operations=operations)
    paths: list[str] = []
    for operation in operations:
        for path in (operation.source, operation.destination):
            if not path:
                continue
            if targets([path], grant) != [path]:
                raise ValueError("Use exact GCS paths inside the active write grant.")
            if any(c in path for c in "*?[]"):
                raise ValueError("Wildcards are not allowed in GCS operations.")
            if len(urlsplit(path).path.lstrip("/").encode()) > 1024:
                raise ValueError("The GCS object name is too long.")
            for other in paths:
                # Reject file/folder aliases too, including A versus A/child.
                a, b = path.rstrip("/"), other.rstrip("/")
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    raise ValueError("Source and destination paths must not overlap.")
            paths.append(path)
    return operations


def root(event_id: str, snapshot: str) -> str:
    if not all(re.fullmatch(r"[a-f0-9]{32}", value) for value in (event_id, snapshot)):
        raise ValueError("Invalid GCS operation identifier.")
    return f"write-access/operations/{event_id}/{snapshot}/"


def read(storage: Storage, path: str) -> tuple[dict[str, Any], str | None]:
    saved = approvals.load_json(storage, path)
    return saved if saved else ({}, None)


def blob_at(client: Any, uri: str) -> Any:
    target = urlsplit(uri)
    return client.bucket(target.netloc).blob(target.path.lstrip("/"))


def exists(blob: Any) -> bool:
    try:
        blob.reload(timeout=20, retry=None)
    except NotFound:
        return False
    return True


def occupied(client: Any, uri: str) -> bool:
    """A file, a folder marker or any child reserves the exact destination name."""
    target = urlsplit(uri)
    prefix = target.path.lstrip("/").rstrip("/")
    if exists(client.bucket(target.netloc).blob(prefix)):
        return True
    return (
        next(
            iter(
                client.list_blobs(
                    target.netloc,
                    prefix=prefix + "/",
                    max_results=1,
                    page_size=1,
                    timeout=20,
                    retry=None,
                )
            ),
            None,
        )
        is not None
    )


def inspect_page(
    client: Any, operation: Operation, token: str | None
) -> tuple[list[Any], str | None]:
    if not operation.source.endswith("/"):
        blob = blob_at(client, operation.source)
        if not exists(blob):
            raise ValueError(f"Source file does not exist: {operation.source}")
        # Do not interpret a file path as permission to modify a same-named folder.
        target = urlsplit(operation.source)
        if (
            next(
                iter(
                    client.list_blobs(
                        target.netloc,
                        prefix=target.path.lstrip("/") + "/",
                        max_results=1,
                        page_size=1,
                        timeout=20,
                        retry=None,
                    )
                ),
                None,
            )
            is not None
        ):
            raise ValueError("The source names both a file and a folder; use an unambiguous path.")
        return [blob], None
    if exists(blob_at(client, operation.source.rstrip("/"))):
        raise ValueError("The source names both a file and a folder; use an unambiguous path.")
    source = urlsplit(operation.source)
    iterator = client.list_blobs(
        source.netloc,
        prefix=source.path.lstrip("/"),
        page_token=token,
        max_results=PAGE_SIZE,
        page_size=PAGE_SIZE,
        timeout=20,
        retry=None,
    )
    page = next(iter(iterator.pages), ())
    return list(page), iterator.next_page_token


def prepare(
    storage: Storage,
    event_id: str,
    snapshot: str,
    operations: list[Operation],
    authorized: Callable[[], None],
    *,
    deadline: float,
) -> dict[str, Any]:
    """Freeze paged source generations before showing a confirmation; no data writes."""
    from google.cloud import storage as gcs

    base = root(event_id, snapshot)
    path = base + "manifest.json"
    state, generation = read(storage, path)
    serialized = [op.model_dump() for op in operations]
    if not state:
        state = dict(
            operations=serialized,
            operation=0,
            token=None,
            shards=0,
            counts=[0] * len(operations),
            bytes=[0] * len(operations),
            complete=False,
        )
        generation = approvals.save(storage, path, state, None)
    if state["operations"] != serialized:
        raise ValueError("The requested GCS operations changed. Request a new confirmation.")
    if state["complete"]:
        return state
    client = gcs.Client()
    for operation in operations:
        authorized()
        if operation.destination and occupied(client, operation.destination):
            raise ValueError(f"Move destination already exists: {operation.destination}")
    while state["operation"] < len(operations) and time.monotonic() < deadline - 25:
        authorized()
        index = state["operation"]
        operation = operations[index]
        shard_path = base + f"shards/{state['shards']:08d}.json"
        shard, _ = read(storage, shard_path)
        if not shard:
            blobs, token = inspect_page(client, operation, state["token"])
            entries = []
            source = urlsplit(operation.source)
            prefix = source.path.lstrip("/")
            for blob in blobs:
                if not blob.crc32c or blob.size is None or not blob.generation:
                    raise ValueError("Source metadata is incomplete; retry the inspection.")
                suffix = blob.name.removeprefix(prefix) if operation.source.endswith("/") else ""
                if operation.source.endswith("/") and not blob.name.startswith(prefix):
                    raise ValueError("The source listing escaped its requested folder.")
                destination = operation.destination + suffix if operation.destination else ""
                # Validate every derived name, including zero-byte directory markers.
                write_broker.relative_path(blob.name.rstrip("/"))
                if destination:
                    name = urlsplit(destination).path.lstrip("/")
                    write_broker.relative_path(name.rstrip("/"))
                    if len(name.encode()) > 1024:
                        raise ValueError("A renamed object would exceed GCS's name limit.")
                entries.append(
                    dict(
                        source=f"gs://{source.netloc}/{blob.name}",
                        destination=destination,
                        generation=int(blob.generation),
                        metageneration=int(blob.metageneration),
                        size=int(blob.size),
                        crc32c=blob.crc32c,
                    )
                )
            shard = dict(operation=index, entries=entries, token=token)
            approvals.save(storage, shard_path, shard, None)
        if shard["operation"] != index:
            raise ValueError("The saved operation inventory is inconsistent.")
        state["counts"][index] += len(shard["entries"])
        state["bytes"][index] += sum(entry["size"] for entry in shard["entries"])
        state["shards"] += 1
        state["token"] = shard["token"]
        if not shard["token"]:
            if not state["counts"][index]:
                raise ValueError(f"Source folder is empty or absent: {operation.source}")
            state["operation"] += 1
        generation = approvals.save(storage, path, state, generation)
    if state["operation"] == len(operations):
        state["complete"] = True
        state["inspected_at"] = time.time()
        approvals.save(storage, path, state, generation)
    return state


def mutate(
    storage: Storage,
    settings: Settings,
    base: str,
    identifier: str,
    entry: dict[str, Any],
    authorized: Callable[[], None],
    *,
    deadline: float,
) -> bool:
    """One generation-pinned mutation. Durable copy/delete intents recover ambiguous responses."""
    path = base + "receipts/" + identifier + ".json"
    receipt, revision = read(storage, path)
    if receipt and receipt["entry"] != entry:
        raise ValueError("The saved GCS operation changed.")
    if receipt.get("done"):
        return True
    authorized()
    source = write_broker.destination_blob(settings, entry["source"], overwrite=True)
    source_exists = exists(source)
    if source_exists and (
        int(source.generation) != entry["generation"]
        or int(source.metageneration) != entry["metageneration"]
        or source.size != entry["size"]
        or source.crc32c != entry["crc32c"]
    ):
        raise ValueError(f"Source changed since confirmation; preserved: {entry['source']}")
    if not receipt:
        receipt = dict(entry=entry, token=None, copied=False, deleting=False, done=False)
        revision = approvals.save(storage, path, receipt, None)
    if not source_exists:
        if not receipt["deleting"]:
            raise ValueError(f"Source disappeared before this operation: {entry['source']}")
        # Delete intent was durable before the request. Absence reconciles a lost response.
        receipt["done"] = True
        approvals.save(storage, path, receipt, revision)
        return True
    if entry["destination"]:
        destination = write_broker.destination_blob(
            settings, entry["destination"], source_uri=entry["source"]
        )
        marker = hashlib.sha256((base + identifier).encode()).hexdigest()

        def verified() -> bool:
            return bool(
                exists(destination)
                and (destination.metadata or {}).get("mica-operation") == marker
                and destination.size == entry["size"]
                and destination.crc32c == entry["crc32c"]
                and (
                    not receipt.get("destination_generation")
                    or int(destination.generation) == receipt["destination_generation"]
                )
            )

        if not verified():
            if destination.generation is not None or receipt["copied"]:
                raise ValueError(f"Move destination exists or changed: {entry['destination']}")
            destination.metadata = {**(source.metadata or {}), "mica-operation": marker}
            # Delete must target the live object, not explicitly delete a historical version.
            live_source = source.bucket.blob(source.name)
            while time.monotonic() < deadline - 35:
                authorized()
                try:
                    token, _, _ = destination.rewrite(
                        live_source,
                        token=receipt["token"],
                        if_generation_match=0,
                        if_source_generation_match=entry["generation"],
                        if_source_metageneration_match=entry["metageneration"],
                        timeout=30,
                        retry=None,
                    )
                except PreconditionFailed:
                    if not verified():
                        raise ValueError("Source or destination changed during the move.") from None
                    token = None
                receipt["token"] = token
                revision = approvals.save(storage, path, receipt, revision)
                if token is None:
                    break
            else:
                return False
            if not verified():
                raise ValueError(
                    "The copied object could not be verified; the source is preserved."
                )
        receipt.update(copied=True, destination_generation=int(destination.generation))
        revision = approvals.save(storage, path, receipt, revision)
        # Recheck the verified destination just before the source delete, also on retry.
        if not verified():
            raise ValueError("The copied destination changed; the source is preserved.")
    authorized()
    receipt["deleting"] = True
    revision = approvals.save(storage, path, receipt, revision)
    live_source = source.bucket.blob(source.name)
    try:
        live_source.delete(
            if_generation_match=entry["generation"],
            if_metageneration_match=entry["metageneration"],
            timeout=30,
            retry=None,
        )
    except NotFound:
        pass
    except PreconditionFailed:
        raise ValueError(f"Source changed before deletion; preserved: {entry['source']}") from None
    receipt["done"] = True
    approvals.save(storage, path, receipt, revision)
    return True


def execute(
    storage: Storage,
    settings: Settings,
    approval: Approval,
    authorized: Callable[[], None],
    *,
    deadline: float,
) -> dict[str, Any]:
    base = root(approval.event_id, approval.snapshot_id)
    manifest, _ = read(storage, base + "manifest.json")
    if not manifest.get("complete") or manifest["operations"] != [
        op.model_dump() for op in approval.operations
    ]:
        raise ValueError("The confirmed operation inventory is unavailable or changed.")
    state, generation = read(storage, base + "execution.json")
    if not state:
        state = dict(shard=0, entry=0, completed=0, started=False, done=False)
        generation = approvals.save(storage, base + "execution.json", state, None)
    if state["done"]:
        return state
    authorized()
    from google.cloud import storage as gcs

    client = gcs.Client()
    if not state["started"]:
        # One preflight over every destination before any copies or deletions start.
        for operation in approval.operations:
            if operation.destination and occupied(client, operation.destination):
                raise ValueError(f"Move destination already exists: {operation.destination}")
        state["started"] = True
        generation = approvals.save(storage, base + "execution.json", state, generation)
    while state["shard"] < manifest["shards"] and time.monotonic() < deadline - 35:
        shard, _ = read(storage, base + f"shards/{state['shard']:08d}.json")
        if not shard or not 0 <= shard.get("operation", -1) < len(approval.operations):
            raise ValueError("The confirmed operation inventory is incomplete.")
        for index in range(state["entry"], len(shard["entries"])):
            if time.monotonic() >= deadline - 35:
                return state
            if not mutate(
                storage,
                settings,
                base,
                f"{state['shard']:08d}-{index:04d}",
                checked_entry(approval.operations[shard["operation"]], shard["entries"][index]),
                authorized,
                deadline=deadline,
            ):
                return state
            state.update(entry=index + 1, completed=state["completed"] + 1)
            generation = approvals.save(storage, base + "execution.json", state, generation)
        state.update(shard=state["shard"] + 1, entry=0)
        generation = approvals.save(storage, base + "execution.json", state, generation)
    if state["shard"] == manifest["shards"]:
        authorized()
        state["remaining"] = [
            op.source for op in approval.operations if occupied(client, op.source)
        ]
        state["done"] = True
        approvals.save(storage, base + "execution.json", state, generation)
    return state


async def prepare_approval(
    bridge: BatchBridge,
    storage: Storage,
    record: dict[str, Any],
    approval: Approval,
    generation: str,
) -> bool:
    from . import write_approval

    if approval.plan_ready:
        return True
    await bridge.notify(
        approval.event_id, record, "replying", "Inspecting the requested GCS operations…"
    )
    manifest = await asyncio.to_thread(
        prepare,
        storage,
        approval.event_id,
        approval.snapshot_id,
        approval.operations,
        lambda: write_approval.validate(bridge, storage, record, approval),
        deadline=time.monotonic() + SLICE_SECONDS,
    )
    if not manifest["complete"]:
        record["status"] = "pending"
        return False
    approval.plan_ready = True
    approval.operation_counts = manifest["counts"]
    approval.inspected_at = manifest["inspected_at"]
    approval.expires_at = write_approval.KIND.deadline()
    await asyncio.to_thread(
        approvals.save, storage, write_approval.key(approval.event_id), approval, generation
    )
    return True


async def advance(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage
) -> None:
    from . import write_approval
    from .direct import finish

    try:
        if surface.is_email(record) or record.get("ambient"):
            raise ValueError(
                "GCS move/delete confirmations require Slack and an active write grant."
            )
        current = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, storage, record
        )
        if not current.allowed or record.get("decision_access") != current.signature:
            raise ValueError("Project access changed. Send a new request.")
        if "write_grant" not in record:
            grant = await asyncio.to_thread(
                write_access.active, storage, current.project_id, bridge.settings, event_id
            )
            if grant is None:
                raise ValueError(
                    "An active project write grant is required to move or delete data."
                )
            record.update(
                write_grant=grant.model_dump(mode="json"),
                principal=current.email,
                project_id=current.project_id,
                access_signature=current.signature,
            )
            await approvals.save_record(bridge, event_id, record)
        if not await write_approval.ready(bridge, event_id, record, storage):
            return
        saved = await asyncio.to_thread(write_approval.load, storage, event_id)
        if saved is None or not saved[0].operations or not saved[0].plan_ready:
            raise ValueError("Exact operation confirmation is required.")
        approval = saved[0]

        def authorized() -> None:
            write_approval.validate(bridge, storage, record, approval)
            if (
                record.get("cancel_requested")
                or memory_workspace.cancelled(storage, event_id)
                or approval.status != "approved"
                or not 0 < approval.approved_at <= approval.expires_at
            ):
                raise ValueError("The GCS operation was cancelled or is not approved.")

        state = await asyncio.to_thread(
            execute,
            storage,
            bridge.settings,
            approval,
            authorized,
            deadline=time.monotonic() + SLICE_SECONDS,
        )
        record["gcs_operations_completed"] = state["completed"]
        if not state["done"]:
            await bridge.notify(
                event_id,
                record,
                "replying",
                f"GCS operations in progress: {state['completed']} objects completed.",
            )
            return
        text = (
            (
                "Processed the confirmed objects for these GCS operations:\n"
                if state["remaining"]
                else "Completed the confirmed GCS operations:\n"
            )
            + "\n".join(op.describe() for op in approval.operations)
            + f"\n{state['completed']} inspected objects processed."
        )
        if state["remaining"]:
            text += "\nThese source paths still contain objects outside the confirmed inventory: "
            text += ", ".join(f"`{escape(path, quote=False)}`" for path in state["remaining"])
        await finish(bridge, event_id, record, text)
    except (ValueError, PermissionError, Conflict, GoogleAPICallError) as exc:
        if isinstance(exc, GoogleAPICallError) and (exc.code == 429 or (exc.code or 0) >= 500):
            raise  # Cloud Tasks resumes the protected inventory and per-object receipts.
        record["status"] = "failed"
        detail = (
            "Cloud Storage refused the operation; check bucket permissions or retention."
            if isinstance(exc, GoogleAPICallError)
            else str(exc)
        )
        text = f"GCS operation stopped: {escape(detail, quote=False)}"
        saved = await asyncio.to_thread(write_approval.load, storage, event_id)
        if saved and saved[0].snapshot_id:
            state, _ = await asyncio.to_thread(
                read, storage, root(event_id, saved[0].snapshot_id) + "execution.json"
            )
            text += f" {state.get('completed', 0)} objects confirmed completed before stopping."
        text += " No further changes will run. Any already completed changes are retained."
        await bridge.notify(event_id, record, "failed", text)


def new_snapshot() -> str:
    return uuid4().hex
