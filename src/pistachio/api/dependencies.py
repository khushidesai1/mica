"""Typed access to the application service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import Request

from pistachio.agents.harness import AgentHarness
from pistachio.agents.memory import MemoryManager, ProjectCompactor
from pistachio.config import Settings
from pistachio.services.dispatch import TaskDispatcher
from pistachio.services.projects import ProjectService
from pistachio.services.tasks import TaskService
from pistachio.services.uploads import UploadService
from pistachio.storage.base import Repository


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
