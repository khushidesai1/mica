from __future__ import annotations

from datetime import timedelta

from pistachio.agents.context import ContextBuilder
from pistachio.agents.memory import MemoryManager, ProjectCompactor
from pistachio.agents.session import session_key
from pistachio.models import (
    MemoryItem,
    MemoryKind,
    Project,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskStatus,
    utcnow,
)
from pistachio.services.tasks import TaskService


async def test_five_tasks_compact_without_losing_durable_memory(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="compact", name="Compact", owner="alice@example.org")
    )
    memory = MemoryManager(repository, settings)
    correction = await memory.promote(
        MemoryItem(
            project_id=project.id,
            kind=MemoryKind.USER_CORRECTION,
            content="Sample S2 is a control.",
            provenance="user correction",
        )
    )
    for index in range(5):
        await repository.create_task(
            TaskRecord(
                envelope=TaskEnvelope(
                    project_id=project.id,
                    conversation_id="conv",
                    principal_id="alice@example.org",
                    prompt=f"Task {index}",
                ),
                status=TaskStatus.SUCCEEDED,
                result=TaskResult(summary=f"Artifact {index}", exit_code=0),
            )
        )
    compactor = ProjectCompactor(repository, settings)
    assert await compactor.should_compact(project.id)
    summary = await compactor.compact(project.id)
    assert summary is not None
    assert len(summary.source_task_ids) == 5
    assert "Artifact 4" in summary.summary
    assert (await repository.get_memory(correction.id)) is not None


async def test_idle_project_closes_transcript_but_keeps_memory(repository, settings) -> None:
    project = await repository.create_project(
        Project(
            slug="idle",
            name="Idle",
            owner="alice@example.org",
            last_active_at=utcnow() - timedelta(days=15),
        )
    )
    await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="idle-conv",
                principal_id="alice@example.org",
                prompt="Old task",
            ),
            status=TaskStatus.SUCCEEDED,
            result=TaskResult(summary="Old result", exit_code=0),
        )
    )
    scoped_key = session_key(
        TaskEnvelope(
            project_id=project.id,
            principal_id="alice@example.org",
            conversation_id="idle-conv",
            prompt="Old task",
        )
    )
    await repository.replace_session_items(scoped_key, [{"role": "user", "content": "raw"}])
    await repository.replace_session_items("idle-conv", [{"role": "user", "content": "legacy"}])
    memory = MemoryManager(repository, settings)
    item = await memory.promote(
        MemoryItem(
            project_id=project.id,
            kind=MemoryKind.DATASET_FACT,
            content="Dataset checksum abc.",
            provenance="verified",
        )
    )

    class Harness:
        async def compact_session(self, conversation_id: str) -> None:
            return None

    service = TaskService(
        repository,
        settings,
        Harness(),
        ContextBuilder(repository, memory, settings),
        memory,
        ProjectCompactor(repository, settings),
        executor=None,
    )
    assert await service.close_idle_projects() == 1
    assert await repository.get_session_items(scoped_key) == []
    assert await repository.get_session_items("idle-conv") != []
    assert (await repository.get_memory(item.id)) is not None


def test_session_key_isolates_project_and_user():
    envelope = TaskEnvelope(
        project_id="a", principal_id="alice", conversation_id="same", prompt="hi"
    )
    keys = {
        session_key(envelope),
        session_key(envelope.model_copy(update={"project_id": "b"})),
        session_key(envelope.model_copy(update={"principal_id": "bob"})),
    }
    assert len(keys) == 3
    assert session_key(envelope) == session_key(envelope.model_copy(update={"prompt": "next"}))
