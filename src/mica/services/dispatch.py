"""Local and Cloud Tasks dispatch without giving agents queue credentials."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from google.auth import default
from google.auth.transport.requests import AuthorizedSession

from mica.config import Settings
from mica.models import TaskStatus
from mica.services.tasks import TaskService


class TaskDispatcher(Protocol):
    async def enqueue(self, task_id: str) -> None: ...


class LocalTaskDispatcher:
    def __init__(self, tasks: TaskService) -> None:
        self.tasks = tasks
        self._running: set[asyncio.Task[object]] = set()

    async def enqueue(self, task_id: str) -> None:
        async def continue_task() -> None:
            while True:
                task = await self.tasks.process(task_id)
                if not (task.builder_protocol_version == 2 and task.development
                    and not task.pending_tool_builds
                        and task.status in {TaskStatus.PLANNING, TaskStatus.SUBMITTING}):
                    return
                await asyncio.sleep(0.1 if task.status == TaskStatus.PLANNING else 2)

        job = asyncio.create_task(continue_task())
        self._running.add(job)
        job.add_done_callback(self._running.discard)


class CloudTasksDispatcher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not all(
            (
                settings.gcp_project,
                settings.gcp_region,
                settings.control_plane_url,
                settings.cloud_tasks_service_account,
            )
        ):
            raise ValueError("Cloud Tasks settings are incomplete")

    async def enqueue(self, task_id: str) -> None:
        parent = (
            f"projects/{self.settings.gcp_project}/locations/{self.settings.gcp_region}/"
            f"queues/{self.settings.cloud_tasks_queue}"
        )
        endpoint = f"https://cloudtasks.googleapis.com/v2/{parent}/tasks"
        target = (
            f"{self.settings.control_plane_url.rstrip('/')}/api/v1/internal/tasks/{task_id}/process"
        )
        payload: dict[str, Any] = {
            "task": {
                "dispatchDeadline": "900s",
                "httpRequest": {
                    "httpMethod": "POST",
                    "url": target,
                    "oidcToken": {
                        "serviceAccountEmail": self.settings.cloud_tasks_service_account,
                        "audience": self.settings.control_plane_url,
                    },
                },
            }
        }

        def submit() -> None:
            credentials, _ = default(  # type: ignore[no-untyped-call]
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
            response = session.post(endpoint, json=payload, timeout=30)
            response.raise_for_status()

        await asyncio.to_thread(submit)


def build_dispatcher(settings: Settings, tasks: TaskService) -> TaskDispatcher:
    if settings.env in {"staging", "production"}:
        return CloudTasksDispatcher(settings)
    return LocalTaskDispatcher(tasks)
