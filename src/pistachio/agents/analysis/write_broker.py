"""Publish staged outputs with resumable, object-scoped server-side GCS rewrites."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound, PreconditionFailed

from . import write_access
from .config import CHECKPOINT_PREFIX, Settings
from .storage import Conflict, GCSStorage, Storage

if TYPE_CHECKING:
    from .write_approval import Approval

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
BATCH_SIZE = 500


def manifest_stage(event_id: str, publication_id: str = "") -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id) or (
        publication_id and not re.fullmatch(r"[a-f0-9]{32}", publication_id)
    ):
        raise ValueError("Invalid publication identifier")
    suffix = f"{publication_id}/manifest.json" if publication_id else "manifest.json"
    return f"{CHECKPOINT_PREFIX}/events/{event_id}/write-manifests/{suffix}"


def valid_shard(event_id: str, key: str) -> bool:
    prefix = manifest_stage(event_id).removesuffix("manifest.json") + "shards/"
    return bool(re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}\.jsonl", key))


def stopped_summary(storage: Storage, event_id: str, grant_uri: str) -> str:
    """Count only controller receipts, including copies whose response was lost."""
    try:
        index = json.loads(storage.get(manifest_stage(event_id))[0])
    except FileNotFoundError:
        return ""
    paths: dict[str, str] = {}
    for key in index.get("shards", []):
        if not valid_shard(event_id, key):
            raise ValueError("Invalid publication state shard")
        with storage.open_read(key) as stream:
            for line in stream:
                row = json.loads(line)
                path = relative_path(row["relative_path"])
                folder = relative_path(row["folder"].rstrip("/")) + "/" if row["folder"] else ""
                if not path.startswith(folder):
                    raise ValueError("Publication folder does not contain its object")
                paths[path] = folder
    receipts = set(storage.keys(f"write-access/uploads/{event_id}/"))
    folders: dict[str, list[int]] = {}
    for path, folder in paths.items():
        counts = folders.setdefault(folder, [0, 0])
        counts[1] += 1
        counts[0] += receipt_key(event_id, path) in receipts
    return "\n".join(
        f"`{grant_uri}{folder}`: {done:,} of {total:,} files published"
        + ("; republish before use." if done < total else ".")
        for folder, (done, total) in sorted(folders.items())
    )


def archive_manifests(storage: Storage, event_id: str, grant_uri: str) -> None:
    """Stream immutable worker shards into controller-owned audit storage."""
    import tempfile
    from pathlib import Path

    prefix = f"write-access/uploads/{event_id}/"

    def read(key: str) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(storage.get(key)[0])
        if doc.get("event_id") != event_id or not all(
            isinstance(uri, str) and uri.startswith(grant_uri) for uri in doc.get("uploaded", [])
        ):
            raise ValueError("Publication manifest does not match this run")
        shards = doc.get("shards", [])
        if not isinstance(shards, list) or any(
            not isinstance(key, str) or not valid_shard(event_id, key) for key in shards
        ):
            raise ValueError("Invalid publication shard paths")
        return doc

    def archived(key: str) -> str:
        return prefix + "shards/" + key.rsplit("/", 1)[-1]

    def encode(doc: dict[str, Any]) -> bytes:
        doc = dict(doc)
        if "shards" in doc:
            doc["shards"] = [archived(key) for key in doc["shards"]]
        return json.dumps(doc).encode()

    try:
        index = read(manifest_stage(event_id))
    except FileNotFoundError:
        return
    keys = index.get("publications", [])
    if not isinstance(keys, list) or any(
        not isinstance(key, str)
        or not re.fullmatch(re.escape(prefix) + r"publications/[a-f0-9]{32}/manifest.json", key)
        for key in keys
    ):
        raise ValueError("Invalid publication archive paths")
    try:
        previous = json.loads(storage.get(prefix + "manifest.json")[0])
    except FileNotFoundError:
        previous = {}
    if not set(previous.get("publications", [])).issubset(keys) or not set(
        previous.get("shards", [])
    ).issubset(archived(key) for key in index.get("shards", [])):
        raise ValueError("Publication index cannot discard earlier reports")
    for key in index.get("shards", []):
        target = archived(key)
        if target in previous.get("shards", []):
            continue
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shard.jsonl"
            with storage.open_read(key) as source, path.open("wb") as output:
                while line := source.readline(65537):
                    if len(line) > 65536:
                        raise ValueError("Invalid publication shard row")
                    row = json.loads(line)
                    relative = relative_path(row["relative_path"])
                    if row.get("uri") is not None and row["uri"] != grant_uri + relative:
                        raise ValueError("Publication shard does not match this grant")
                    if not isinstance(row.get("bytes"), int) or row["bytes"] < 0:
                        raise ValueError("Invalid publication byte count")
                    output.write(line)
            try:
                storage.upload_new(target, path)
            except Conflict:
                # One bounded batch, never the entire run's object list.
                with storage.open_read(target) as old, path.open("rb") as new:
                    while chunk := new.read(1024**2):
                        if old.read(len(chunk)) != chunk:
                            raise ValueError("An archived publication cannot change") from None
                    if old.read(1):
                        raise ValueError("An archived publication cannot change") from None
    for key in keys:
        if key in previous.get("publications", []):
            continue
        identifier = key.removeprefix(prefix + "publications/").split("/")[0]
        report = read(manifest_stage(event_id, identifier))
        if not set(report.get("shards", [])).issubset(index.get("shards", [])):
            raise ValueError("Publication references an unindexed shard")
        raw = encode(report)
        try:
            storage.put_if(key, raw, None)
        except Conflict:
            if storage.get(key)[0] != raw:
                raise ValueError("An archived publication cannot change") from None
    storage.put(prefix + "manifest.json", encode(index))


def relative_path(raw: str) -> str:
    if (
        not raw
        or len(raw.encode()) > 1024
        or any(p in {"", ".", ".."} for p in raw.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 or c in "\\%<>|`?#" for c in raw)
    ):
        raise ValueError("Use a relative output filename without traversal or control characters.")
    return raw


def staged_key(event_id: str, relative: str) -> str:
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid event identifier.")
    return f"{CHECKPOINT_PREFIX}/events/{event_id}/write-exports/{relative_path(relative)}"


def boundary(uri: str, *, source_uri: str = "", overwrite: bool = False) -> Any:
    from google.auth import downscoped

    target = urlsplit(uri)
    name = f"projects/_/buckets/{target.netloc}/objects/{target.path.lstrip('/')}"
    result = downscoped.CredentialAccessBoundary(  # type: ignore[no-untyped-call]
        rules=[
            downscoped.AccessBoundaryRule(  # type: ignore[no-untyped-call]
                available_resource=f"//storage.googleapis.com/projects/_/buckets/{target.netloc}",
                available_permissions=(
                    ["inRole:roles/storage.objectAdmin"]
                    if overwrite
                    else [
                        "inRole:roles/storage.objectCreator",
                        "inRole:roles/storage.objectViewer",
                    ]
                ),
                availability_condition=downscoped.AvailabilityCondition(  # type: ignore[no-untyped-call]
                    expression=f"resource.name == {json.dumps(name)}"
                ),
            )
        ]
    )

    if source_uri:
        source = urlsplit(source_uri)
        source_name = f"projects/_/buckets/{source.netloc}/objects/{source.path.lstrip('/')}"
        result.add_rule(
            downscoped.AccessBoundaryRule(  # type: ignore[no-untyped-call]
                available_resource=f"//storage.googleapis.com/projects/_/buckets/{source.netloc}",
                available_permissions=["inRole:roles/storage.objectViewer"],
                availability_condition=downscoped.AvailabilityCondition(  # type: ignore[no-untyped-call]
                    expression=f"resource.name == {json.dumps(source_name)}"
                ),
            )
        )
    return result


def destination_blob(
    settings: Settings, uri: str, *, source_uri: str = "", overwrite: bool = False
) -> Any:
    import google.auth
    from google.auth import downscoped, impersonated_credentials
    from google.cloud import storage

    source, project = google.auth.default(  # type: ignore[no-untyped-call]
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    writer = impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
        source_credentials=source,
        target_principal=settings.write_service_account,
        target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        lifetime=300,
    )
    # Keep refreshable credentials, not a frozen access token: AuthorizedSession calls
    # before_request for each resumable-upload request. Refreshing the downscoped token
    # also refreshes its 300-second impersonated source. Neither token leaves this service.
    credentials = downscoped.Credentials(
        writer, boundary(uri, source_uri=source_uri, overwrite=overwrite)
    )  # type: ignore[no-untyped-call]
    target = urlsplit(uri)
    return (
        storage.Client(project=project, credentials=credentials)
        .bucket(target.netloc)
        .blob(target.path.lstrip("/"))
    )


def receipt_key(event_id: str, relative: str) -> str:
    staged_key(event_id, relative)  # Validate both identifiers before constructing protected keys.
    return f"write-access/uploads/{event_id}/{hashlib.sha256(relative.encode()).hexdigest()}.json"


def upload(
    storage: GCSStorage,
    settings: Settings,
    project_id: str,
    event_id: str,
    relative: str,
    grant_id: str,
    authorized: Callable[[], None],
    *,
    approval: Approval | None = None,
    deadline: float | None = None,
) -> dict[str, str]:
    from .write_approval import allowed_replacement

    relative = relative_path(relative)
    if deadline is None:
        deadline = time.monotonic() + settings.write_upload_seconds

    def check() -> write_access.WriteGrant:
        authorized()
        return write_access.require_active(storage, project_id, grant_id, settings)

    grant = check()
    uri = grant.uri + relative
    if len(urlsplit(uri).path.lstrip("/").encode()) > 1024:
        raise ValueError("The destination object name is too long.")
    if approval is not None and (
        approval.event_id != event_id
        or approval.project_id != project_id
        or approval.grant_id != grant_id
    ):
        approval = None
    source = storage.bucket.blob(staged_key(event_id, relative))
    source_uri = f"gs://{storage.bucket.name}/{source.name}"
    overwrite = bool(
        approval
        and approval.status == "approved"
        and 0 < approval.approved_at <= approval.expires_at
        and any(uri.startswith(p) if p.endswith("/") else uri == p for p in approval.paths)
    )
    destination = destination_blob(settings, uri, source_uri=source_uri, overwrite=overwrite)
    audit_key = receipt_key(event_id, relative)
    progress_key = audit_key.removesuffix(".json") + ".rewrite.json"

    def existing() -> Any | None:
        blob = destination.bucket.blob(destination.name)
        try:
            blob.reload(timeout=30, retry=None)
        except NotFound:
            return None
        return blob

    def cleanup(generation: int) -> None:
        with suppress(NotFound, PreconditionFailed):
            # Use controller authority for source cleanup; the writer remains source-read-only.
            source.delete(if_generation_match=generation, timeout=30, retry=None)

    try:
        receipt = json.loads(storage.get(audit_key)[0])
    except FileNotFoundError:
        receipt = None
    if receipt:
        current = existing()
        if (
            receipt["grant_id"] != grant_id
            or receipt["uri"] != uri
            or current is None
            or int(current.generation) != receipt.get("destination_generation")
        ):
            raise FileExistsError("The published destination changed. Request a new run.")
        try:
            source.reload(timeout=30, retry=None)
        except NotFound:
            pass
        else:
            if source.size != receipt["bytes"] or source.crc32c != receipt["crc32c"]:
                raise ValueError("The staged output changed. Choose a new destination path.")
            cleanup(int(source.generation))
        return {"uri": uri, "status": "uploaded"}

    source.reload(timeout=30, retry=None)
    if source.size is None or source.size > 5 * 1024**4:
        raise ValueError("GCS objects must be at most 5 TiB.")
    generation = int(source.generation)
    metadata = {
        "pistachio-event": event_id,
        "pistachio-grant": grant_id,
        "pistachio-source-generation": str(generation),
        "pistachio-source": hashlib.sha256(source.name.encode()).hexdigest(),
    }

    def matches(blob: Any | None) -> bool:
        return bool(
            blob is not None
            and blob.metadata == metadata
            and blob.size == source.size
            and blob.crc32c == source.crc32c
        )

    current = existing()
    try:
        progress = json.loads(storage.get(progress_key)[0])
    except FileNotFoundError:
        progress = None
    if progress is not None:
        if progress["source_generation"] != generation or progress["grant_id"] != grant_id:
            raise ValueError("The staged output changed. Choose a new destination path.")
        expected = int(progress["expected_generation"])
        token = progress["token"]
    else:
        expected = (
            allowed_replacement(approval, uri, current)
            if current is not None and not matches(current)
            else 0
        )
        token = None
    if not matches(current):
        if current is not None and (not expected or int(current.generation) != expected):
            raise FileExistsError(
                "The destination changed since approval or exists without approval. "
                "Request a new run naming this output path, or use a new filename."
            )
        if current is None and expected:
            raise FileExistsError("The destination changed since approval. Request a new run.")
        if expected and (not overwrite or current is None):
            raise FileExistsError("The destination no longer has overwrite approval.")
        destination.metadata = metadata
        storage.put(
            progress_key,
            json.dumps(
                {
                    "source_generation": generation,
                    "grant_id": grant_id,
                    "expected_generation": expected,
                    "token": token,
                }
            ).encode(),
        )
        while True:
            check()
            if deadline - time.monotonic() < min(35, settings.write_upload_seconds / 10):
                return {"uri": uri, "status": "in_progress"}
            try:
                token, copied, total = destination.rewrite(
                    source,
                    token=token,
                    if_source_generation_match=generation,
                    if_generation_match=expected,
                    timeout=30,
                    retry=None,
                )
            except PreconditionFailed:
                current = existing()
                if not matches(current):
                    raise FileExistsError(
                        "The destination changed since approval. Request a new run."
                    ) from None
                break
            logging.basicConfig()  # Install a sink without changing the root/SDK log level.
            log.info(
                "gcs_rewrite event=%s object=%s bytes=%s total=%s pending=%s",
                event_id,
                hashlib.sha256(relative.encode()).hexdigest()[:12],
                copied,
                total,
                bool(token),
            )
            if token is None:
                current = existing()
                if not matches(current):
                    raise FileExistsError("The destination changed during publication.")
                break
            storage.put(
                progress_key,
                json.dumps(
                    {
                        "source_generation": generation,
                        "grant_id": grant_id,
                        "expected_generation": expected,
                        "token": token,
                    }
                ).encode(),
            )
    assert current is not None
    result = {"uri": uri, "status": "uploaded"}
    receipt = {
        **result,
        "project_id": project_id,
        "grant_id": grant_id,
        "event_id": event_id,
        "relative_path": relative,
        "source_generation": generation,
        "destination_generation": int(current.generation),
        "replaced_generation": expected or None,
        "bytes": source.size,
        "crc32c": source.crc32c,
        "at": write_access.now().isoformat(),
    }
    storage.put(audit_key, json.dumps(receipt).encode())
    cleanup(generation)
    return result
