"""Typed access to the application service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import Request

from mica.agents.harness import AgentHarness
from mica.agents.memory import MemoryManager, ProjectCompactor
from mica.config import Settings
from mica.services.dispatch import TaskDispatcher
from mica.services.projects import ProjectService
from mica.services.tasks import TaskService
from mica.services.uploads import UploadService
from mica.storage.base import Repository


@dataclass(slots=True)
class Services:
    settings: Settings
    repository: Repository
    projects: ProjectService
    tasks: TaskService
    memory: MemoryManager
    compactor: ProjectCompactor
    harness: AgentHarness
    uploads: UploadService
    dispatcher: TaskDispatcher


def get_services(request: Request) -> Services:
    return cast(Services, request.app.state.services)
