from __future__ import annotations

from pathlib import Path

import pytest

from pistachio.agents.context import ContextBuilder
from pistachio.agents.memory import MemoryManager, ProjectCompactor
from pistachio.config import Settings
from pistachio.execution.backends import LocalExecutor
from pistachio.services.tasks import TaskService
from pistachio.storage.memory import InMemoryRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        store_backend="memory",
        execution_backend="local",
        planner_version=1,
        artifact_root=tmp_path / "artifacts",
        compaction_task_threshold=5,
    )


@pytest.fixture
def repository() -> InMemoryRepository:
    return InMemoryRepository()


def task_service(repository, settings: Settings, harness) -> TaskService:
    memory = MemoryManager(repository, settings)
    return TaskService(
        repository,
        settings,
        harness,
        ContextBuilder(repository, memory, settings),
        memory,
        ProjectCompactor(repository, settings),
        LocalExecutor(settings),
    )
