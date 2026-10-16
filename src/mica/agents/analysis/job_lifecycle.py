"""Cloud Tasks scheduling shared by analysis and transfer jobs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from google.auth import default
from google.auth.transport.requests import AuthorizedSession

from mica.config import Settings


async def enqueue(app: Settings, task_id: str, path: str, delay: int = 0) -> None:
    parent = f"projects/{app.gcp_project}/locations/{app.gcp_region}/queues/{app.cloud_tasks_queue}"
    task: dict[str, Any] = {
        "name": f"{parent}/tasks/{task_id}",
        "dispatchDeadline": "180s",
        "httpRequest": {
            "httpMethod": "POST",
            "url": f"{app.control_plane_url}{path}",
            "oidcToken": {
                "serviceAccountEmail": app.cloud_tasks_service_account,
                "audience": app.control_plane_url,
            },
        },
    }
    if delay:
        task["scheduleTime"] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()

    def submit() -> None:
        credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])  # type: ignore[no-untyped-call]
        with AuthorizedSession(credentials) as session:  # type: ignore[no-untyped-call]
            response = session.post(
                f"https://cloudtasks.googleapis.com/v2/{parent}/tasks",
                json={"task": task},
                timeout=20,
            )
            if response.status_code != 409:
                response.raise_for_status()

    await asyncio.to_thread(submit)
