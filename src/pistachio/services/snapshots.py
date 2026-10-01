"""Durable encrypted-at-rest pause snapshots for approval and job resume."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from pistachio.config import Settings
from pistachio.models import TaskRecord


class SnapshotStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def save(self, task: TaskRecord) -> str:
        payload = task.model_dump_json(indent=2)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        name = f"paused/{task.id}/{task.updated_at.timestamp():.0f}-{digest[:12]}.json"
        if self.settings.gcs_bucket:
            from google.cloud.storage import Client

            def upload() -> str:
                blob = (
                    Client(project=self.settings.gcp_project)
                    .bucket(self.settings.gcs_bucket)
                    .blob(name)
                )
                blob.metadata = {"sha256": digest, "task_id": task.id}
                blob.upload_from_string(
                    payload,
                    content_type="application/json",
                    if_generation_match=0,
                )
                return f"gs://{self.settings.gcs_bucket}/{name}#{blob.generation}"

            return await asyncio.to_thread(upload)
        path = self.settings.artifact_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return Path(path).resolve().as_uri()
