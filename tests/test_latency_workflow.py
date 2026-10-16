from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from test_development import ANALYSIS, REGRESSION, configuration, make_bundle
from test_initial_quality import Reviewer, plan

from mica.execution.backends import LocalExecutor, parse_progress_line
from mica.execution.input_cache import InputCache
from mica.execution.runtime_support import parameter, positional_rows, require_memory
from mica.execution.session import command_active, write_manifest
from mica.execution.workspace import WorkspaceStore
from mica.models import (
    CandidateReview,
    DecisionSet,
    DevelopmentAction,
    MasterPlan,
    ParameterBinding,
    SourceFile,
    TaskEnvelope,
    TaskRecord,
    utcnow,
)
from mica.services.acceptance import compact_observation
from mica.services.development import DevelopmentService


async def candidate(repository, settings, tmp_path):
    settings = configuration(settings).model_copy(
        update={
            "development_latency_enabled": True,
            "worker_input_cache": tmp_path / "cache",
        }
    )
    bundle = make_bundle(tmp_path)
    task = await repository.create_task(
        TaskRecord(
            id=bundle.task_id,
            builder_protocol_version=2,
            envelope=TaskEnvelope(
                project_id="p",
                conversation_id="c",
                principal_id="a",
                prompt="Sum",
                attachments=bundle.input_attachments,
            ),
            contract=bundle.contract,
            master_plan=MasterPlan(contract=bundle.contract, parameters=bundle.parameter_manifest),
            decision_set=DecisionSet(
                task_id=bundle.task_id, master_plan_id="m", master_plan_revision=1
            ),
            resource_plan=bundle.resource_plan,
            tool_plan=bundle.tool_plan,
        )
    )
    reviewer = Reviewer()
    service = DevelopmentService(repository, settings, reviewer, LocalExecutor(settings))
    task = await service.advance(task)
    store = WorkspaceStore(settings, task.id)
    design = plan(task.development.operations[-1].id)
    design.parameter_bindings = [
        ParameterBinding(parameter_id=p.id, consumer="transform", api_adaptation="unchanged")
        for p in bundle.parameter_manifest
    ]
    task = await service.apply_action(
        task,
        DevelopmentAction(
            action="write_candidate",
            base_revision=task.development.source_hash,
            implementation_plan=design,
            output_specs=design.output_specs,
            files=[
                SourceFile(path="analysis.py", content=ANALYSIS),
                SourceFile(path="tests/test_analysis.py", content=REGRESSION),
            ],
        ),
        {},
        store,
    )
    return service, task, reviewer, store


async def test_one_candidate_one_validation_automatic_full_run(repository, settings, tmp_path):
    service, task, reviewer, store = await candidate(repository, settings, tmp_path)
    assert not reviewer.calls  # no separate design-review call
    assert task.development.preparation_version == 2
    task = await service.advance(task)
    assert task.development.state == "verified"
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0]["execution_evidence_required"] is False
    assert reviewer.calls[0]["resource_plan"] == task.resource_plan.model_dump(mode="json")
    assert task.development.first_candidate_outcome == "passed"
    result = await store.get(task.development.operations[-1].result_ref)
    assert result["timings"]["input_cache_hit"] == 1
    assert result["verification"]["validation_suite_hash"]
    task = await service.advance(task)
    assert task.development.state == "reviewing_results"
    assert [o.phase for o in task.development.operations] == ["probe", "verify", "run"]
    run = await store.get(task.development.pending_result_ref)
    assert run["exit_code"] == 0
    assert "tests_seconds" not in run["timings"]  # certified unchanged suite is not repeated
    assert len(reviewer.calls) == 1


async def test_review_and_tests_run_concurrently_and_merge_findings(repository, settings, tmp_path):
    service, task, reviewer, store = await candidate(repository, settings, tmp_path)
    original = service.executor.execute
    testing = asyncio.Event()
    reviewing = asyncio.Event()

    async def execute(bundle, progress_callback=None):
        testing.set()
        await asyncio.wait_for(reviewing.wait(), 2)
        return await original(bundle, progress_callback)

    async def review(*args):
        reviewing.set()
        await asyncio.wait_for(testing.wait(), 2)
        return CandidateReview(approved=False, issues=["bindings: concrete API mismatch"])

    service.executor.execute = execute
    reviewer.review_candidate = review
    task = await service.advance(task)
    assert task.development.operations[-1].exit_code == 0
    assert task.development.state == "developing"
    assert not task.development.verified_hash
    assert "API mismatch" in task.development.feedback
    assert "checks passed" in task.development.feedback
    assert task.development.first_check_passed is False


async def test_superseded_first_candidate_is_counted(repository, settings, tmp_path):
    service, task, _, store = await candidate(repository, settings, tmp_path)
    files = await store.get(task.development.source_ref)
    await service.apply_action(
        task,
        DevelopmentAction(
            action="edit_files",
            base_revision=task.development.source_hash,
            files=[SourceFile(path="analysis.py", content=ANALYSIS + "\n# revised\n")],
        ),
        files,
        store,
    )
    assert task.development.first_check_passed is False
    assert task.development.first_candidate_outcome == "revised_before_validation"


async def test_repair_review_receives_prior_findings_and_fixed_checklist(
    repository, settings, tmp_path
):
    service, task, reviewer, store = await candidate(repository, settings, tmp_path)
    reviewer.approved = False
    task = await service.advance(task)
    original_hash = task.development.source_hash
    original_checklist = list(task.development.acceptance_checklist)
    files = await store.get(task.development.source_ref)
    task = await service.apply_action(
        task,
        DevelopmentAction(
            action="edit_files", base_revision=original_hash,
            files=[SourceFile(path="analysis.py", content=ANALYSIS + "\n# repaired\n")],
        ),
        files,
        store,
    )
    reviewer.approved = True
    task = await service.advance(task)
    review = reviewer.calls[-1]
    assert review["previous_review"]["source_hash"] == original_hash
    assert review["previous_review"]["review"]["approved"] is False
    assert len(review["previous_review"]["review"]["issues"]) == 2
    assert review["changed_paths"] == ["analysis.py"]
    assert review["acceptance_checklist"] == original_checklist
    assert review["initial_effort"] is False
    assert review["execution_evidence_required"] is False


def test_input_cache_reuses_verified_bytes_and_detects_corruption(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path)
    cache = InputCache(tmp_path / "cache")
    count = 0
    stage = LocalExecutor._stage_inputs

    def counted(*args):
        nonlocal count
        count += 1
        stage(*args)

    monkeypatch.setattr(LocalExecutor, "_stage_inputs", counted)
    for name in ("a", "b"):
        dest = tmp_path / name
        dest.mkdir()
        hashes, timing = cache.stage(bundle, dest)
        assert hashes["values.json"] == bundle.input_attachments[0].checksum_sha256
        assert timing["input_cache_hit"] == (name == "b")
    assert count == 1
    data = next((tmp_path / "cache").glob("*/values.json"))
    data.chmod(0o644)
    data.write_text("[9, 9, 9]")
    dest = tmp_path / "c"
    dest.mkdir()
    with pytest.raises(ValueError, match="checksum"):
        cache.stage(bundle, dest)


def test_generation_changes_invalidate_cache(tmp_path):
    bundle = make_bundle(tmp_path)
    cache = InputCache(tmp_path / "cache")
    for generation in ("1", "2"):
        bundle.input_attachments[0].generation = generation
        dest = tmp_path / generation
        dest.mkdir()
        _, timings = cache.stage(bundle, dest)
        assert timings["input_cache_hit"] == 0


def test_runtime_helpers_preserve_types_and_handle_missing_proc(monkeypatch):
    import sys
    from types import SimpleNamespace

    config = {"parameters": {"k": 50}, "resources": {"memory_gib": 128}}
    assert parameter(config, "k", int) == 50
    config["parameters"]["k"] = True
    with pytest.raises(ValueError):
        parameter(config, "k", int)

    def unavailable():
        raise FileNotFoundError("/proc/meminfo")

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(virtual_memory=unavailable))
    require_memory(40, config)
    with pytest.raises(MemoryError):
        require_memory(129, config)

    class Frame:
        iloc = ["row0", "row1"]

        def __getitem__(self, _):
            raise AssertionError("column indexing used")

    assert positional_rows(Frame(), 1) == "row1"
    assert positional_rows(["row0", "row1"], 1) == "row1"


def test_manifest_stale_commands_and_cancellation(monkeypatch):
    from mica.execution import session

    state = {}

    class Blob:
        def upload_from_string(self, payload, **kwargs):
            state.update(json.loads(payload))

    class Bucket:
        def blob(self, _):
            return Blob()

    monkeypatch.setattr(session, "read_manifest", lambda *a: (dict(state), 1))
    command = dict(
        session_id="current",
        epoch=2,
        sequence=3,
        stop=False,
        deadline=(utcnow() + timedelta(minutes=10)).isoformat(),
    )
    write_manifest(Bucket(), "task", command)
    write_manifest(Bucket(), "task", command)  # duplicate is harmless
    assert command_active(state, "current")
    assert not command_active(state, "previous")
    with pytest.raises(ValueError, match="superseded"):
        write_manifest(Bucket(), "task", {**command, "epoch": 1})
    write_manifest(Bucket(), "task", {**command, "stop": True})
    assert not command_active(state, "current")
    with pytest.raises(ValueError, match="superseded"):
        write_manifest(Bucket(), "task", command)
    assert not command_active(command, "current", utcnow() + timedelta(hours=1))


def test_manifest_read_retries_a_generation_race():
    from google.api_core.exceptions import PreconditionFailed

    from mica.execution.session import read_manifest

    class Blob:
        generation = 0

        def reload(self):
            self.generation += 1

        def download_as_text(self, if_generation_match):
            if if_generation_match == 1:
                raise PreconditionFailed("command advanced")
            return '{"sequence": 2}'

    class Bucket:
        value = Blob()

        def blob(self, _):
            return self.value

    assert read_manifest(Bucket(), "task") == ({"sequence": 2}, 2)


def test_compact_evidence_and_untrusted_progress():
    raw = {
        "stdout": json.dumps({"inputs": ["structure"], "packages": ["huge"]}),
        "stderr": "irrelevant",
        "exit_code": 0,
    }
    assert "packages" not in compact_observation(raw, "probe")["runtime"]
    progress = parse_progress_line(
        'MICA_PROGRESS {"status":"started","message":"hi","trusted":true}', 1
    )
    assert not progress.trusted


@pytest.mark.parametrize("exit_mode", ["idle", "superseded", "expired"])
async def test_supervisor_shutdown_and_duplicate_command(tmp_path, monkeypatch, exit_mode):
    from types import SimpleNamespace

    from mica.worker import session as supervisor

    bundle = make_bundle(tmp_path)
    bundle.runtime_image = "image"
    command = dict(
        session_id="s",
        epoch=1,
        sequence=1,
        operation_id=bundle.operation_id,
        bundle_uri="gs://b/bundle",
        source_hash=bundle.source_hash,
        stop=False,
        deadline=(utcnow() + timedelta(hours=1)).isoformat(),
    )
    if exit_mode == "expired":
        command["deadline"] = (utcnow() - timedelta(seconds=1)).isoformat()
    monkeypatch.setenv("MICA_GCS_BUCKET", "b")
    monkeypatch.setenv("MICA_BATCH_IMAGE", "image")
    monkeypatch.setattr(supervisor, "Client", lambda: SimpleNamespace(bucket=lambda _: object()))
    monkeypatch.setattr(supervisor, "read_manifest", lambda *a: (dict(command), 1))
    monkeypatch.setattr(supervisor, "download_bundle", lambda _: bundle)
    monkeypatch.setattr(supervisor, "_publish_progress", lambda *a: None)
    calls = []
    cancelled = asyncio.Event()

    async def execute(*args, **kwargs):
        calls.append(args)
        if exit_mode == "superseded":
            command["session_id"] = "new"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return 1  # a failed analysis does not crash the reusable supervisor

    monkeypatch.setattr(supervisor, "execute_bundle", execute)
    clock = [0.0]
    sleep = asyncio.sleep

    async def advance(seconds):
        clock[0] += seconds
        await sleep(0)

    monkeypatch.setattr(supervisor.asyncio, "sleep", advance)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    await supervisor.supervise("task_dev", "s", idle_seconds=6)
    assert len(calls) == (0 if exit_mode == "expired" else 1)
    if exit_mode == "superseded":
        assert cancelled.is_set()


async def test_changed_suite_cannot_reuse_validation(settings, tmp_path, monkeypatch):
    from mica.execution import development

    settings = configuration(settings)
    executor = LocalExecutor(settings)
    verified_bundle = make_bundle(tmp_path, phase="verify").model_copy(
        update={"workflow_version": 2}
    )
    verified = await executor.execute(verified_bundle)
    assert verified.exit_code == 0
    monkeypatch.setattr(development, "validation_suite_hash", lambda: "changed")
    run = verified_bundle.model_copy(
        update={
            "attempt": 2,
            "operation_id": "changed-suite",
            "operation_phase": "run",
            "verification": verified.verification,
        }
    )
    result = await executor.execute(run)
    assert result.exit_code != 0
    assert result.failure.phase == "verification"


async def test_completion_during_review_is_not_acknowledged(repository, settings, monkeypatch):
    from conftest import task_service

    from mica.models import TaskResult
    from mica.services.development import DevelopmentConflict

    task = await repository.create_task(
        TaskRecord(
            envelope=TaskEnvelope(project_id="p", conversation_id="c", principal_id="a", prompt="x")
        )
    )
    service = task_service(repository, settings, Reviewer())

    async def busy(*args):
        raise DevelopmentConflict("review active")

    monkeypatch.setattr(service.development_service, "advance", busy)
    with pytest.raises(DevelopmentConflict):
        await service._advance_development(task, TaskResult(summary="completed", operation_id="op"))
    assert (await service._advance_development(task)).id == task.id


def test_trusted_helper_signatures_are_given_to_author():
    from mica.services.acceptance import helper_contracts

    contracts = helper_contracts()
    assert "expected_type" in contracts["parameter"]
    assert "config" in contracts["require_memory"]
    assert "positions" in contracts["positional_rows"]


@pytest.mark.parametrize("state", ["RUNNING", "FAILED", "missing"])
async def test_worker_loss_replaces_session_and_charges_elapsed_time(
    repository, settings, tmp_path, monkeypatch, state
):
    from google.api_core.exceptions import NotFound

    from mica.execution.backends import BatchJobStatus, GcpBatchExecutor

    service, task, _, _ = await candidate(repository, settings, tmp_path)
    service.executor = GcpBatchExecutor(service.settings.model_copy(update={
        "gcp_project": "fixture-project", "gcs_bucket": "fixture-bucket",
        "batch_service_account": "fixture@example.org", "batch_image": "fixture-image",
    }))
    session = task.development
    session.worker_reuse_enabled = True
    session.worker_session_id = "previous"
    session.worker_session_job = "previous-job"
    session.worker_session_started_at = utcnow() - timedelta(seconds=30)
    session.worker_session_deadline = utcnow() + timedelta(minutes=10)

    async def status(_):
        if state == "missing":
            raise NotFound("worker lost")
        return BatchJobStatus(state=state)

    monkeypatch.setattr(service.executor, "get_job_status", status)
    command = await service.ensure_worker_session(task, session.operations[-1])
    if state == "RUNNING":
        assert command["session_id"] == "previous"
        assert session.worker_seconds_consumed == 0
    else:
        assert command["session_id"] != "previous"
        assert session.worker_seconds_consumed >= 30
        assert session.worker_session_job == ""


async def test_model_timing_includes_wall_timestamps(monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    from mica.agents.harness import AgentHarness, Runner

    async def run(*args, **kwargs):
        return SimpleNamespace(raw_responses=[], new_items=[])

    monkeypatch.setattr(Runner, "run", run)
    before = utcnow()
    _, metrics = await AgentHarness._run_bounded(
        SimpleNamespace(name="fixture", model="fixture"), "input", None, timeout=1, max_turns=1
    )
    assert before <= datetime.fromisoformat(metrics["model_started_at"])
    assert datetime.fromisoformat(metrics["model_started_at"]) <= datetime.fromisoformat(
        metrics["model_completed_at"]
    ) <= utcnow()
