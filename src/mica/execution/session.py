"""Control-owned, generation-fenced command manifests for task-scoped Batch workers."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from google.api_core.exceptions import NotFound, PreconditionFailed

from mica.models import utcnow


def manifest_key(task_id: str) -> str:
    return f"tasks/{task_id}/worker-session.json"


def read_manifest(bucket: Any, task_id: str) -> tuple[dict[str, Any], int]:
    for _ in range(5):
        blob = bucket.blob(manifest_key(task_id))
        try:
            blob.reload()
            generation = int(blob.generation)
            return json.loads(blob.download_as_text(if_generation_match=generation)), generation
        except NotFound:
            return {}, 0
        except PreconditionFailed:
            # The control plane can advance the command between metadata and content reads.
            # Read the new generation without treating this normal race as worker loss.
            continue
    raise RuntimeError("worker command changed repeatedly during read")


def write_manifest(bucket: Any, task_id: str, manifest: dict[str, Any]) -> None:
    """An older session/command cannot overwrite a newer one, including cancellation."""
    for _ in range(5):
        previous, generation = read_manifest(bucket, task_id)
        if previous:
            old = (previous["epoch"], previous["sequence"])
            new = (manifest["epoch"], manifest["sequence"])
            if old > new or (
                previous.get("stop") and new[0] <= old[0] and not manifest.get("stop")
            ):
                raise ValueError("worker command was superseded")
            if old == new and previous != manifest and not manifest.get("stop"):
                raise ValueError("worker command identity conflict")
            if previous == manifest:
                return
        try:
            bucket.blob(manifest_key(task_id)).upload_from_string(
                json.dumps(manifest, sort_keys=True),
                content_type="application/json",
                if_generation_match=generation,
            )
            return
        except PreconditionFailed:
            continue
    raise RuntimeError("worker command changed concurrently")


def stop_manifest(bucket: Any, task_id: str, session_id: str = "") -> None:
    previous, _ = read_manifest(bucket, task_id)
    if previous and (not session_id or previous["session_id"] == session_id):
        write_manifest(bucket, task_id, {**previous, "stop": True})


def command_active(manifest: dict[str, Any], session_id: str, now: datetime | None = None) -> bool:
    return bool(
        manifest
        and manifest.get("session_id") == session_id
        and not manifest.get("stop")
        and datetime.fromisoformat(manifest["deadline"]) > (now or utcnow())
    )
