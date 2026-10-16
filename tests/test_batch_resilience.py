from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import task_service
from google.api_core.exceptions import AlreadyExists, PreconditionFailed
from google.cloud import batch_v1
from google.cloud import storage as google_storage

from mica.config import Settings
from mica.execution.backends import BatchJobStatus, GcpBatchExecutor
from mica.models import (
    ExecutionBundle,
    Project,
    ResourcePlan,
    StageKind,
    StageStatus,
    TaskContract,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskStage,
    TaskStatus,
    ToolPlan,
)
from mica.services.tasks import ExecutionSubmissionPending
from mica.worker.main import _publish_outputs

SCRIPT = '''"""Write one deterministic output."""
import argparse
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
'''


class NoopHarness:
    async def compact_session(self, conversation_id: str) -> None:
        return None


async def test_nested_sandbox_exception_is_rejected_in_production(tmp_path) -> None:
    image = "us-central1-docker.pkg.dev/example/runtime@sha256:" + "a" * 64
    settings = Settings(
        env="production", auth_mode="iap", artifact_root=tmp_path, batch_image=image,
        batch_nested_sandbox_opt_in=True,
        gcp_project="test-project", gcs_bucket="test-bucket",
        batch_service_account="test@example.iam.gserviceaccount.com",
    )
    bundle = ExecutionBundle(
        builder_protocol_version=2, runtime_image=image, task_id="task_scope_check",
        script=SCRIPT, contract=TaskContract(objective="Scope check"),
        resource_plan=ResourcePlan(), tool_plan=ToolPlan(),
    )
    with pytest.raises(ValueError, match="restricted to staging"):
        await GcpBatchExecutor(settings).execute(bundle)


@pytest.mark.parametrize("builder_protocol", [1, 2])
@pytest.mark.parametrize("nested_opt_in", [False, True])
@pytest.mark.parametrize("previous_worker", [False, True])
async def test_batch_jobs_use_private_google_access_without_external_ip(
    monkeypatch, tmp_path, builder_protocol, nested_opt_in, previous_worker
) -> None:
    captured: dict[str, object] = {}

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def upload_from_string(
            self, payload: str, content_type: str, if_generation_match: int
        ) -> None:
            assert if_generation_match == 0
            captured["payload"] = payload

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            captured["blob_name"] = name
            return FakeBlob(name)

    class FakeStorageClient:
        def __init__(self, project: str) -> None:
            pass

        def bucket(self, name: str) -> FakeBucket:
            return FakeBucket()

    class FakeBatchClient:
        def create_job(self, *, parent: str, job_id: str, job: object) -> object:
            captured["job"] = job
            return SimpleNamespace(name=f"{parent}/jobs/{job_id}")

    monkeypatch.setattr(google_storage, "Client", FakeStorageClient)
    monkeypatch.setattr(batch_v1, "BatchServiceClient", FakeBatchClient)
    settings = Settings(
        env="test",
        execution_backend="gcp_batch",
        artifact_root=tmp_path,
        gcp_project="azizilab-aml",
        gcp_region="us-central1",
        gcs_bucket="artifacts",
        batch_service_account="worker@example.iam.gserviceaccount.com",
        batch_image="us-central1-docker.pkg.dev/azizilab-aml/mica/science@sha256:123",
        batch_nested_sandbox_opt_in=nested_opt_in,
        batch_network="default",
        batch_subnetwork="default",
    )
    bundle = ExecutionBundle(
        builder_protocol_version=builder_protocol,
        runtime_image="science@sha256:previous" if previous_worker else settings.batch_image,
        task_id="task_private",
        script=SCRIPT,
        contract=TaskContract(objective="Test private Batch", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
    )

    if previous_worker:
        if builder_protocol == 2:
            with pytest.raises(ValueError, match="runtime differs"):
                await GcpBatchExecutor(settings).execute(bundle)
        settings = settings.model_copy(update={
            "batch_compatible_images": ("science@sha256:previous",),
        })
    result = await GcpBatchExecutor(settings).execute(bundle)

    assert result.exit_code is None
    assert captured["blob_name"] == "tasks/task_private/attempt-1/execution_bundle.json"
    assert "/jobs/task-private-a1-" in result.output_text
    job = captured["job"]
    runnable = job.task_groups[0].task_spec.runnables[0]
    expected_image = bundle.runtime_image if builder_protocol == 2 else settings.batch_image
    assert runnable.container.image_uri == expected_image
    assert runnable.environment.variables["MICA_BATCH_IMAGE"] == expected_image
    assert runnable.container.options == (
        "--security-opt seccomp=unconfined --security-opt apparmor=unconfined"
        if builder_protocol == 2 and nested_opt_in else ""
    )
    interface = job.allocation_policy.network.network_interfaces[0]  # type: ignore[attr-defined]
    assert interface.no_external_ip_address
    assert interface.network == "projects/azizilab-aml/global/networks/default"
    assert interface.subnetwork == ("projects/azizilab-aml/regions/us-central1/subnetworks/default")
    environment = job.task_groups[0].task_spec.runnables[0].environment.variables  # type: ignore[attr-defined]
    assert environment["HOME"] == "/tmp/mica-home"
    assert environment["XDG_CONFIG_HOME"] == "/tmp/mica-config"
    assert environment["XDG_CACHE_HOME"] == "/tmp/mica-cache"
    assert environment["MPLCONFIGDIR"] == "/tmp/mica-matplotlib"
    assert environment["MPLBACKEND"] == "Agg"


def test_batch_worker_publishes_only_user_outputs_and_completion_record(
    monkeypatch, tmp_path: Path
) -> None:
    uploaded: list[str] = []

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def upload_from_filename(self, path: Path, if_generation_match: int) -> None:
            assert Path(path).is_file()
            assert if_generation_match == 0
            uploaded.append(self.name)

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            return FakeBlob(name)

    class FakeStorageClient:
        def __init__(self, project: str | None = None) -> None:
            del project

        def bucket(self, name: str) -> FakeBucket:
            assert name == "configured-artifacts"
            return FakeBucket()

    attempt_dir = tmp_path / "attempt-1"
    outputs = attempt_dir / "outputs"
    outputs.mkdir(parents=True)
    (attempt_dir / "worker-result.json").write_text("{}")
    (attempt_dir / "task.py").write_text("print('internal')")
    (attempt_dir / "execution_bundle.json").write_text("{}")
    (attempt_dir / "stderr.log").write_text("internal")
    (outputs / "figure.png").write_bytes(b"figure")
    (outputs / "generated_analysis.py").write_text("print('delivered')")
    monkeypatch.setattr("mica.worker.main.StorageClient", FakeStorageClient)
    monkeypatch.setenv("MICA_GCS_BUCKET", "configured-artifacts")
    monkeypatch.delenv("MICA_COMPLETION_TOPIC", raising=False)
    bundle = ExecutionBundle(
        task_id="task-scoped",
        script=SCRIPT,
        contract=TaskContract(objective="Publish outputs", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        attempt=1,
    )

    _publish_outputs(bundle, attempt_dir, 0)

    assert uploaded == [
        "tasks/task-scoped/attempt-1/worker-result.json",
        "tasks/task-scoped/attempt-1/outputs/figure.png",
        "tasks/task-scoped/attempt-1/outputs/generated_analysis.py",
    ]


async def test_reconciliation_marks_prestart_batch_failure(repository, settings) -> None:
    class FailedBatchExecutor:
        async def execute(self, bundle: ExecutionBundle) -> TaskResult:
            raise AssertionError("reconciliation must not submit another job")

        async def get_job_status(self, job_name: str) -> BatchJobStatus:
            return BatchJobStatus(
                state="FAILED",
                detail="Constraint constraints/compute.vmExternalIpAccess violated",
            )

        async def load_job_result(self, task_id: str, attempt: int) -> TaskResult:
            raise AssertionError("failed jobs do not have worker results")

    project = await repository.create_project(
        Project(slug="batch-failure", name="Batch failure", owner="alice@example.org")
    )
    stage = TaskStage(kind=StageKind.EXECUTION, status=StageStatus.RUNNING)
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-batch",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            status=TaskStatus.QUEUED,
            attempts=1,
            execution_job_name="projects/p/locations/r/jobs/task-batch",
            result=TaskResult(summary="Submitted", exit_code=None),
            planner_version=2,
            current_stage=StageKind.EXECUTION,
            stage_summaries=[stage],
        )
    )
    service = task_service(repository, settings, NoopHarness())
    service.executor = FailedBatchExecutor()

    counts = await service.reconcile_batch_jobs()
    saved = await repository.get_task(task.id)

    assert counts == {
        "checked": 1,
        "recovered": 0,
        "succeeded": 0,
        "failed": 1,
        "errors": 0,
    }
    assert saved is not None
    assert saved.status == TaskStatus.FAILED
    assert "could not be repaired automatically" in (saved.error or "")
    assert saved.stage_summaries[0].status == StageStatus.FAILED
    assert "vmExternalIpAccess" in saved.stage_summaries[0].error


async def test_batch_submission_is_attempt_scoped_and_idempotent(
    monkeypatch, tmp_path
) -> None:
    blobs: dict[str, str] = {}
    jobs: dict[str, object] = {}

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def upload_from_string(
            self, payload: str, content_type: str, if_generation_match: int
        ) -> None:
            del content_type
            assert if_generation_match == 0
            if self.name in blobs:
                raise PreconditionFailed("exists")
            blobs[self.name] = payload

        def download_as_text(self) -> str:
            return blobs[self.name]

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            return FakeBlob(name)

    class FakeStorageClient:
        def __init__(self, project: str) -> None:
            del project

        def bucket(self, name: str) -> FakeBucket:
            del name
            return FakeBucket()

    class FakeBatchClient:
        def create_job(self, *, parent: str, job_id: str, job: object) -> object:
            name = f"{parent}/jobs/{job_id}"
            if name in jobs:
                raise AlreadyExists("exists")
            response = SimpleNamespace(name=name, labels=dict(job.labels))  # type: ignore[attr-defined]
            jobs[name] = response
            return response

        def get_job(self, *, name: str) -> object:
            return jobs[name]

    monkeypatch.setattr(google_storage, "Client", FakeStorageClient)
    monkeypatch.setattr(batch_v1, "BatchServiceClient", FakeBatchClient)
    batch_settings = Settings(
        env="test",
        execution_backend="gcp_batch",
        artifact_root=tmp_path,
        gcp_project="azizilab-aml",
        gcp_region="us-central1",
        gcs_bucket="artifacts",
        batch_service_account="worker@example.iam.gserviceaccount.com",
        batch_image="science@sha256:123",
    )
    executor = GcpBatchExecutor(batch_settings)
    base = ExecutionBundle(
        task_id="task_retry",
        script=SCRIPT,
        contract=TaskContract(objective="Retry safely", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        attempt=1,
    )

    first = await executor.execute(base)
    duplicate = await executor.execute(base)
    second = await executor.execute(base.model_copy(update={"attempt": 2}))

    assert first.output_text == duplicate.output_text
    assert first.output_text != second.output_text
    assert "/jobs/task-retry-a1-" in first.output_text
    assert "/jobs/task-retry-a2-" in second.output_text
    assert len(jobs) == 2


async def test_orphaned_queue_resubmits_the_reserved_attempt(repository, settings) -> None:
    class RecoveringBatchExecutor:
        def __init__(self) -> None:
            self.attempts: list[int] = []

        async def execute(self, bundle: ExecutionBundle, progress_callback=None) -> TaskResult:
            del progress_callback
            self.attempts.append(bundle.attempt)
            return TaskResult(
                summary="Submitted",
                output_text=f"projects/p/locations/r/jobs/task-a{bundle.attempt}",
                exit_code=None,
            )

        async def get_job_status(self, job_name: str) -> BatchJobStatus:
            raise AssertionError(job_name)

        async def load_job_result(self, task_id: str, attempt: int) -> TaskResult:
            raise AssertionError((task_id, attempt))

    settings = settings.model_copy(update={"execution_backend": "gcp_batch"})
    project = await repository.create_project(
        Project(slug="orphaned-queue", name="Orphaned queue", owner="alice@example.org")
    )
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-orphan",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            status=TaskStatus.QUEUED,
            attempts=2,
            contract=TaskContract(objective="Run", requires_execution=True),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(),
            generated_script=SCRIPT,
            planner_version=2,
            current_stage=StageKind.EXECUTION,
        )
    )
    executor = RecoveringBatchExecutor()
    service = task_service(repository, settings, NoopHarness())
    service.executor = executor

    counts = await service.reconcile_batch_jobs()
    saved = await repository.get_task(task.id)

    assert counts == {
        "checked": 1,
        "recovered": 1,
        "succeeded": 0,
        "failed": 0,
        "errors": 0,
    }
    assert executor.attempts == [2]
    assert saved is not None
    assert saved.status == TaskStatus.QUEUED
    assert saved.attempts == 2
    assert saved.execution_job_name and saved.execution_job_name.endswith("task-a2")


async def test_failed_submission_keeps_attempt_resumable(repository, settings) -> None:
    class FlakyBatchExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, bundle: ExecutionBundle, progress_callback=None) -> TaskResult:
            del progress_callback
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary control-plane failure")
            return TaskResult(
                summary="Submitted",
                output_text=f"projects/p/locations/r/jobs/task-a{bundle.attempt}",
                exit_code=None,
            )

    settings = settings.model_copy(update={"execution_backend": "gcp_batch"})
    project = await repository.create_project(
        Project(slug="submission-retry", name="Submission retry", owner="alice@example.org")
    )
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-submit",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            contract=TaskContract(objective="Run", requires_execution=True),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(),
            generated_script=SCRIPT,
            planner_version=2,
        )
    )
    executor = FlakyBatchExecutor()
    service = task_service(repository, settings, NoopHarness())
    service.executor = executor

    try:
        await service._execute_or_complete(task)
    except ExecutionSubmissionPending:
        pass
    else:
        raise AssertionError("submission failure was not made retryable")
    reserved = await repository.get_task(task.id)
    assert reserved is not None
    assert reserved.status == TaskStatus.SUBMITTING
    assert reserved.attempts == 1
    assert reserved.execution_job_name is None

    queued = await service._execute_or_complete(reserved)

    assert queued.status == TaskStatus.QUEUED
    assert queued.attempts == 1
    assert queued.submission_attempts == 0
    assert queued.execution_job_name and queued.execution_job_name.endswith("task-a1")


async def test_submission_retries_stop_with_a_visible_failure(repository, settings) -> None:
    class UnavailableBatchExecutor:
        async def execute(self, bundle: ExecutionBundle, progress_callback=None) -> TaskResult:
            del bundle, progress_callback
            raise RuntimeError("batch API unavailable")

    settings = settings.model_copy(update={"execution_backend": "gcp_batch"})
    project = await repository.create_project(
        Project(slug="submission-limit", name="Submission limit", owner="alice@example.org")
    )
    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(
                project_id=project.id,
                conversation_id="conv-submit-limit",
                principal_id="alice@example.org",
                prompt="Run an analysis.",
            ),
            contract=TaskContract(objective="Run", requires_execution=True),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(),
            generated_script=SCRIPT,
            planner_version=2,
        )
    )
    service = task_service(repository, settings, NoopHarness())
    service.executor = UnavailableBatchExecutor()

    current = task
    for _ in range(service.MAX_SUBMISSION_ATTEMPTS - 1):
        try:
            await service._execute_or_complete(current)
        except ExecutionSubmissionPending:
            current = await repository.get_task(task.id)
            assert current is not None
        else:
            raise AssertionError("submission should still be retryable")
    failed = await service._execute_or_complete(current)

    assert failed.status == TaskStatus.FAILED
    assert failed.attempts == 1
    assert failed.submission_attempts == service.MAX_SUBMISSION_ATTEMPTS
    assert failed.error and "could not be submitted" in failed.error
