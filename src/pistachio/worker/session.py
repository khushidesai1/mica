"""One trusted supervisor, isolated operation processes, task-local read-only input cache."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from google.cloud.storage import Client

from pistachio.execution.session import command_active, read_manifest
from pistachio.models import TaskProgress
from pistachio.worker.main import _publish_progress, download_bundle, execute_bundle


async def supervise(task_id: str, session_id: str, idle_seconds: int = 600) -> None:
    bucket = Client().bucket(os.environ["PISTACHIO_GCS_BUCKET"])
    running: asyncio.Task[int] | None = None
    active: dict[str, Any] = {}
    completed: set[str] = set()
    last_activity = time.monotonic()
    heartbeat = 0.0
    operation_deadline = 0.0
    # Only the trusted parent sees credentials, the cache, or the command manifest.
    with tempfile.TemporaryDirectory(prefix="pistachio-session-") as directory:
        try:
            while True:
                manifest, _ = await asyncio.to_thread(read_manifest, bucket, task_id)
                if not command_active(manifest, session_id):
                    return
                if running and time.monotonic() >= operation_deadline:
                    raise TimeoutError("operation exceeded its execution and publication allowance")
                if running and running.done():
                    # Publication errors are infrastructure failures, not silent retries.
                    running.result()
                    completed.add(active["operation_id"])
                    running = None
                    last_activity = time.monotonic()
                if running and manifest["operation_id"] != active["operation_id"]:
                    # New commands may arrive immediately after the durable completion event.
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    completed.add(active["operation_id"])
                    running = None
                if not running and manifest["operation_id"] not in completed:
                    bundle = await asyncio.to_thread(download_bundle, manifest["bundle_uri"])
                    if (
                        bundle.task_id != task_id
                        or bundle.operation_id != manifest["operation_id"]
                        or bundle.runtime_image != os.environ["PISTACHIO_BATCH_IMAGE"]
                        or bundle.source_hash != manifest["source_hash"]
                        or bundle.attempt != manifest["sequence"]
                    ):
                        raise ValueError("session command/bundle identity mismatch")
                    operation_deadline = time.monotonic() + bundle.command_timeout_seconds + 300
                    active = manifest
                    running = asyncio.create_task(
                        execute_bundle(
                            manifest["bundle_uri"], cache_root=Path(directory) / "inputs"
                        )
                    )
                    last_activity = time.monotonic()
                if running and time.monotonic() - heartbeat >= 30:
                    heartbeat = time.monotonic()
                    with suppress(Exception):
                        await asyncio.to_thread(
                            _publish_progress,
                            bundle,
                            TaskProgress(
                                attempt=bundle.attempt,
                                operation_id=bundle.operation_id,
                                worker_session_id=session_id,
                                trusted=True,
                                status="info",
                                message="Worker active; waiting for the next analysis milestone",
                                item="heartbeat",
                            ),
                        )
                if not running and time.monotonic() - last_activity >= idle_seconds:
                    return
                await asyncio.sleep(2)
        finally:
            if running:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
