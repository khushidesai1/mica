"""Stream staged outputs through a create-only, object-scoped cloud credential."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from google.api_core.exceptions import NotFound, PreconditionFailed

from . import write_access
from .config import CHECKPOINT_PREFIX, Settings
from .storage import GCSStorage

CHUNK_SIZE = 8 * 1024 * 1024


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
    # Neither the impersonated token nor this per-object token leaves the control service.
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

    def check() -> write_access.WriteGrant:
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
        started = time.monotonic()
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
                    if time.monotonic() - started > 240:
                        raise ValueError("Upload timed out. Use smaller files and retry.")
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
