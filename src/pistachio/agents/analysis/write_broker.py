"""Stream staged outputs through a create-only, object-scoped cloud credential."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound, PreconditionFailed

from . import write_access
from .config import CHECKPOINT_PREFIX, Settings
from .storage import Conflict, GCSStorage, Storage

CHUNK_SIZE = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def manifest_stage(event_id: str, publication_id: str = "") -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id) or (
        publication_id and not re.fullmatch(r"[a-f0-9]{32}", publication_id)
    ):
        raise ValueError("Invalid publication identifier")
    suffix = f"{publication_id}/manifest.json" if publication_id else "manifest.json"
    return f"{CHECKPOINT_PREFIX}/events/{event_id}/write-manifests/{suffix}"


def archive_manifests(storage: Storage, event_id: str, grant_uri: str) -> None:
    """Archive worker reports through the controller; per-object receipts remain authority."""

    def read(key: str) -> tuple[bytes, dict[str, Any]]:
        raw = storage.read_prefix(key, MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Publication manifest exceeds the archive limit")
        doc = json.loads(raw)
        if doc.get("event_id") != event_id or not all(
            isinstance(uri, str) and uri.startswith(grant_uri) for uri in doc.get("uploaded", [])
        ):
            raise ValueError("Publication manifest does not match this run")
        return raw, doc

    try:
        raw, index = read(manifest_stage(event_id))
    except FileNotFoundError:
        return
    prefix = f"write-access/uploads/{event_id}/"
    keys = index.get("publications", [])
    if (
        not isinstance(keys, list)
        or len(keys) > 5000
        or any(
            not isinstance(key, str)
            or not re.fullmatch(re.escape(prefix) + r"publications/[a-f0-9]{32}/manifest.json", key)
            for key in keys
        )
    ):
        raise ValueError("Invalid publication archive paths")
    try:
        previous_raw = storage.get(prefix + "manifest.json")[0]
        if previous_raw == raw:
            return
        previous = set(json.loads(previous_raw).get("publications", []))
    except FileNotFoundError:
        previous = set()
    if not previous.issubset(keys):
        raise ValueError("Publication index cannot discard earlier reports")
    for key in keys:
        if key in previous:
            continue
        identifier = key.removeprefix(prefix + "publications/").split("/")[0]
        report, _ = read(manifest_stage(event_id, identifier))
        try:
            storage.put_if(key, report, None)
        except Conflict:
            if storage.get(key)[0] != report:
                raise ValueError("An archived publication cannot change") from None
    storage.put(prefix + "manifest.json", raw)


def relative_path(raw: str) -> str:
    if (
        not raw
        or len(raw.encode()) > 512
        or any(p in {"", ".", ".."} for p in raw.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 or c in "\\%<>|`" for c in raw)
    ):
        raise ValueError("Use a relative output filename without traversal or control characters.")
    return raw


def staged_key(event_id: str, relative: str) -> str:
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid event identifier.")
    return f"{CHECKPOINT_PREFIX}/events/{event_id}/write-exports/{relative_path(relative)}"


def boundary(uri: str) -> Any:
    from google.auth import downscoped

    target = urlsplit(uri)
    name = f"projects/_/buckets/{target.netloc}/objects/{target.path.lstrip('/')}"
    return downscoped.CredentialAccessBoundary(  # type: ignore[no-untyped-call]
        rules=[
            downscoped.AccessBoundaryRule(  # type: ignore[no-untyped-call]
                available_resource=f"//storage.googleapis.com/projects/_/buckets/{target.netloc}",
                available_permissions=[
                    "inRole:roles/storage.objectCreator",
                    "inRole:roles/storage.objectViewer",
                ],
                availability_condition=downscoped.AvailabilityCondition(  # type: ignore[no-untyped-call]
                    expression=f"resource.name == {json.dumps(name)}"
                ),
            )
        ]
    )


def destination_blob(settings: Settings, uri: str) -> Any:
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
    credentials = downscoped.Credentials(writer, boundary(uri))  # type: ignore[no-untyped-call]
    target = urlsplit(uri)
    return (
        storage.Client(project=project, credentials=credentials)
        .bucket(target.netloc)
        .blob(target.path.lstrip("/"))
    )


def upload(
    storage: GCSStorage,
    settings: Settings,
    project_id: str,
    event_id: str,
    relative: str,
    grant_id: str,
    authorized: Callable[[], None],
) -> dict[str, str]:
    relative = relative_path(relative)
    deadline = time.monotonic() + settings.write_upload_seconds

    def check() -> write_access.WriteGrant:
        if time.monotonic() >= deadline:
            raise ValueError("Upload timed out. Use smaller files and retry.")
        authorized()
        return write_access.require_active(storage, project_id, grant_id, settings)

    grant = check()
    uri = grant.uri + relative
    if len(urlsplit(uri).path.lstrip("/").encode()) > 1024:
        raise ValueError("The destination object name is too long.")
    source = storage.bucket.blob(staged_key(event_id, relative))
    source.reload(timeout=30, retry=None)
    if source.size is None or source.size > settings.write_max_mb * 1024 * 1024:
        raise ValueError(f"GCS uploads are limited to {settings.write_max_mb} MB per file.")
    generation = int(source.generation)
    metadata = {
        "pistachio-event": event_id,
        "pistachio-grant": grant_id,
        "pistachio-source-generation": str(generation),
        "pistachio-source": hashlib.sha256(source.name.encode()).hexdigest(),
    }
    destination = destination_blob(settings, uri)

    def already_uploaded() -> bool:
        try:
            destination.reload(timeout=30, retry=None)
        except NotFound:
            return False
        if (
            destination.metadata != metadata
            or destination.size != source.size
            or destination.crc32c != source.crc32c
        ):
            raise FileExistsError("The destination already exists. Use a new filename.")
        return True

    if not already_uploaded():
        destination.metadata = metadata
        try:
            # Generation preconditions pin the source and prohibit replacement of any target.
            with (
                source.open(
                    "rb",
                    chunk_size=CHUNK_SIZE,
                    if_generation_match=generation,
                    timeout=30,
                    retry=None,
                ) as reader,
                destination.open(
                    "wb", chunk_size=CHUNK_SIZE, if_generation_match=0, timeout=30, retry=None
                ) as writer,
            ):
                while True:
                    check()
                    chunk = reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    check()
                    writer.write(chunk)
                check()  # Abort the resumable upload on revocation, including before commit.
        except PreconditionFailed:
            # A lost response or concurrent retry may have completed this exact upload.
            if not already_uploaded():
                raise
    result = {"uri": uri, "status": "uploaded"}
    receipt = {
        **result,
        "project_id": project_id,
        "grant_id": grant_id,
        "event_id": event_id,
        "relative_path": relative,
        "source_generation": generation,
        "bytes": source.size,
        "at": write_access.now().isoformat(),
    }
    storage.put(
        f"write-access/uploads/{event_id}/{hashlib.sha256(relative.encode()).hexdigest()}.json",
        json.dumps(receipt).encode(),
    )
    return result
