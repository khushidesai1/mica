from __future__ import annotations

from pathlib import Path

import pytest

from mica.agents.context import ContextBuilder
from mica.agents.memory import MemoryManager, ProjectCompactor
from mica.config import Settings
from mica.execution.backends import LocalExecutor
from mica.services.tasks import TaskService
from mica.storage.memory import InMemoryRepository


@pytest.fixture(autouse=True)
def isolate_local_dotenv(monkeypatch):
    """Keep workstation .env settings out of test-created configurations."""
    from mica.agents.analysis.config import Settings as AnalysisSettings

    for settings_type in (Settings, AnalysisSettings):
        monkeypatch.setitem(settings_type.model_config, "env_file", None)


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
