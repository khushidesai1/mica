from __future__ import annotations

import hashlib
import json

from conftest import task_service

from mica.execution.backends import LocalExecutor, parse_progress_line
from mica.models import (
    AgentTaskPlan,
    AttachmentRef,
    ExecutionBundle,
    ProgressStatus,
    Project,
    ResourcePlan,
    StageKind,
    StageStatus,
    TaskContract,
    TaskEnvelope,
    TaskProgress,
    TaskRecord,
    TaskStage,
    TaskStatus,
    ToolPlan,
)


def real_data_preflight_script(expected_rows: int) -> str:
    return f'''"""Validate the staged real input before full execution."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    payload = json.loads((args.input_dir / "shape.json").read_text())
    rows = payload["rows"]
    if args.preflight:
        assert len(rows) == {expected_rows}, f"real input shape mismatch: {{len(rows)}}"
        assert all(len(row) == 2 for row in rows), "coordinate width must be 2"
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "result.json").write_text(json.dumps({{"rows": len(rows)}}))

if __name__ == "__main__":
    main()
'''


def progress_script() -> str:
    return '''"""Exercise structured execution progress."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
for current, sample in enumerate(("sample-a", "sample-b"), start=1):
    print("MICA_PROGRESS " + json.dumps({
        "status": "succeeded",
        "message": "Segmentation completed.",
        "item": sample,
        "current": current,
        "total": 2,
    }), flush=True)
(args.output_dir / "result.json").write_text(json.dumps({"ok": True}))
'''


async def test_local_executor_streams_each_structured_progress_event(settings) -> None:
    updates = []

    async def record(update) -> None:
        updates.append(update)

    bundle = ExecutionBundle(
        task_id="task-progress",
        script=progress_script(),
        contract=TaskContract(objective="Segment two samples", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        attempt=1,
    )

    result = await LocalExecutor(settings).execute(bundle, progress_callback=record)

    assert result.exit_code == 0
    assert [item.item for item in updates] == ["sample-a", "sample-b"]
    assert [item.current for item in updates] == [1, 2]
    assert all(item.status == ProgressStatus.SUCCEEDED for item in updates)
    assert result.stdout.count("MICA_PROGRESS ") == 2


async def test_local_executor_preflights_the_staged_real_input(settings, tmp_path) -> None:
    source = tmp_path / "shape.json"
    payload = json.dumps({"rows": [[0, 1], [2, 3], [4, 5]]}).encode()
    source.write_bytes(payload)
    attachment = AttachmentRef(
        filename=source.name,
        uri=source.resolve().as_uri(),
        size_bytes=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        quarantined=False,
    )
    bundle = ExecutionBundle(
        task_id="task-real-preflight",
        script=real_data_preflight_script(expected_rows=3),
        contract=TaskContract(objective="Validate real coordinates", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        input_attachments=[attachment],
        attempt=1,
    )

    result = await LocalExecutor(settings).execute(bundle)

    assert result.exit_code == 0
    assert sorted(artifact.name for artifact in result.artifacts) == [
        "generated_analysis.py",
        "result.json",
    ]
    preflight_log = (
        settings.artifact_root
        / bundle.task_id
        / "attempt-1"
        / "preflight.stderr.log"
    )
    assert preflight_log.exists()


async def test_real_data_preflight_blocks_full_analysis_on_shape_failure(
    settings, tmp_path
) -> None:
    source = tmp_path / "shape.json"
    payload = json.dumps({"rows": [[0, 1], [2, 3], [4, 5]]}).encode()
    source.write_bytes(payload)
    attachment = AttachmentRef(
        filename=source.name,
        uri=source.resolve().as_uri(),
        size_bytes=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        quarantined=False,
    )
    bundle = ExecutionBundle(
        task_id="task-real-preflight-failure",
        script=real_data_preflight_script(expected_rows=4),
        contract=TaskContract(objective="Validate real coordinates", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        input_attachments=[attachment],
        attempt=1,
    )

    result = await LocalExecutor(settings).execute(bundle)

    assert result.exit_code != 0
    assert "Real-data preflight failed" in result.stderr
    assert "real input shape mismatch: 3" in result.stderr
    assert result.artifacts == []


def test_progress_parser_rejects_malformed_or_inconsistent_events() -> None:
    assert parse_progress_line("ordinary log output", 1) is None
    assert parse_progress_line("MICA_PROGRESS not-json", 1) is None
    assert (
        parse_progress_line(
            'MICA_PROGRESS {"status":"succeeded","message":"done",'
            '"current":3,"total":2}',
            1,
        )
        is None
    )


class ProgressHarness:
    async def plan(self, *, task_id, envelope, context):
        return AgentTaskPlan(
            contract=TaskContract(
                objective="Segment two samples",
                authorized_scientific_actions=["Segment sample-a and sample-b."],
                requires_execution=True,
            ),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(),
            script=progress_script(),
            response="Ready.",
        )

    async def compact_session(self, conversation_id: str) -> None:
        return None


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.seen: set[str] = set()

    async def notify(self, task) -> None:
        for message in task.conversation:
            if message.role == "assistant" and message.id not in self.seen:
                self.messages.append(message.text)
                self.seen.add(message.id)


async def test_task_service_persists_and_delivers_per_item_progress(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="progress", name="Progress", owner="alice@example.org")
    )
    service = task_service(repository, settings, ProgressHarness())
    notifier = RecordingNotifier()
    service.notifier = notifier
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-progress",
            principal_id="alice@example.org",
            prompt="Segment sample-a and sample-b.",
        )
    )

    completed = await service.process(task.id)

    assert completed.status == TaskStatus.SUCCEEDED
    assert [item.item for item in completed.progress_updates] == ["sample-a", "sample-b"]
    # Every sample remains in diagnostics; rapid milestones do not each become a chat turn.
    assert sum("Segmentation completed" in message for message in notifier.messages) <= 1
    assert [m.text for m in completed.conversation if m.role == "assistant"] == notifier.messages
    serialized = json.loads(completed.model_dump_json())
    assert serialized["progress_updates"][1]["message"] == "Segmentation completed."


async def test_batch_progress_ignores_stale_attempts(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="batch-progress", name="Batch progress", owner="alice@example.org")
    )
    service = task_service(repository, settings, ProgressHarness())
    active = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-batch-progress",
                principal_id="alice@example.org",
                prompt="Segment samples.",
            ),
            status=TaskStatus.QUEUED,
            attempts=2,
        )
    )

    await service.record_batch_progress(
        active.id,
        1,
        TaskProgress(
            attempt=1,
            status=ProgressStatus.SUCCEEDED,
            message="Old attempt finished a sample.",
            item="sample-old",
        ),
    )
    saved = await service.record_batch_progress(
        active.id,
        2,
        TaskProgress(
            attempt=2,
            status=ProgressStatus.SUCCEEDED,
            message="  Segmentation\ncompleted.  ",
            item=" sample-current ",
        ),
    )

    assert len(saved.progress_updates) == 1
    assert saved.progress_updates[0].item == "sample-current"
    assert saved.progress_updates[0].message == "Segmentation completed."


async def test_progress_notification_is_delivered_once_per_event(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="progress-dedupe", name="Progress dedupe", owner="alice@example.org")
    )
    service = task_service(repository, settings, ProgressHarness())
    notifier = RecordingNotifier()
    service.notifier = notifier
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-progress-dedupe",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            status=TaskStatus.PLANNING,
            attempts=1,
            progress_updates=[
                TaskProgress(
                    attempt=1,
                    status=ProgressStatus.INFO,
                    message="Repairing the generated code.",
                )
            ],
        )
    )

    await service._notify_progress(task)
    task.current_stage = "CODE_GENERATION"
    await service._notify_progress(task)
    task.current_stage = "VALIDATION"
    await service._notify_progress(task)

    assert notifier.messages == ["Repairing the generated code."]
    saved = await repository.get_task(task.id)
    assert saved is not None
    assert saved.last_notified_progress_id == task.progress_updates[0].id
    assert saved.notified_event_keys == [f"progress:{task.progress_updates[0].id}"]


async def test_stage_notification_is_not_repeated_after_an_intervening_status(
    repository, settings
) -> None:
    project = await repository.create_project(
        Project(slug="stage-dedupe", name="Stage dedupe", owner="alice@example.org")
    )
    service = task_service(repository, settings, ProgressHarness())
    notifier = RecordingNotifier()
    service.notifier = notifier
    stage = TaskStage(
        kind=StageKind.VALIDATION,
        status=StageStatus.RUNNING,
        version=1,
    )
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-stage-dedupe",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            status=TaskStatus.PLANNING,
            current_stage=StageKind.VALIDATION,
            stage_summaries=[stage],
        )
    )

    await service._notify(task)
    task.status = TaskStatus.SUBMITTING
    await service._notify(task)
    task.status = TaskStatus.PLANNING
    await service._notify(task)

    assert notifier.messages == [
        "I’m checking that the analysis follows the agreed plan before running it.",
    ]


async def test_queued_notification_is_delivered_once_per_attempt(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="queue-dedupe", name="Queue dedupe", owner="alice@example.org")
    )
    service = task_service(repository, settings, ProgressHarness())
    notifier = RecordingNotifier()
    service.notifier = notifier
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-queue-dedupe",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            status=TaskStatus.QUEUED,
            attempts=1,
        )
    )

    await service._notify(task)
    await service._notify(task)

    assert notifier.messages == [
        "The analysis is queued. I’ll start it as soon as compute is ready."
    ]
