from __future__ import annotations

import asyncio

import pytest
from conftest import task_service
from test_planner_v2 import StagedHarness

from mica.execution.backends import LocalExecutor
from mica.execution.workspace import WorkspaceStore, digest
from mica.models import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    DecisionScope,
    DecisionSet,
    DevelopmentAction,
    DevelopmentOperation,
    DevelopmentSession,
    MasterPlan,
    Project,
    ResourcePlan,
    TaskContract,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskStatus,
    ToolPlan,
    ToolRecord,
    ToolStatus,
)
from mica.services.development import DevelopmentConflict, DevelopmentService


async def development_task(repository, settings):
    task = TaskRecord(
        builder_protocol_version=2,
        planner_version=2,
        status=TaskStatus.PLANNING,
        envelope=TaskEnvelope(
            project_id="p", conversation_id="c", principal_id="alice", prompt="Sum"
        ),
        contract=TaskContract(objective="Sum", requires_execution=True),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
    )
    task.master_plan = MasterPlan(contract=task.contract)
    task.decision_set = DecisionSet(
        task_id=task.id, master_plan_id=task.master_plan.id, master_plan_revision=1
    )
    store = WorkspaceStore(settings, task.id)
    task.development = DevelopmentSession(
        interaction_revision=0,
        authority_hash=DevelopmentService.authority(task),
        source_hash=digest({}),
        source_ref=await store.put({}),
        operations=[
            DevelopmentOperation(
                id="probe",
                phase="probe",
                source_hash=digest({}),
                attempt=1,
                status="completed",
                exit_code=0,
            )
        ],
    )
    await repository.create_task(task)
    return task, store


@pytest.mark.parametrize(
    "reason",
    [
        "Grant another source revision; methods and outputs remain unchanged.",
        "Select a compatible scikit-learn version and fix the API call.",
        "Fix a 3.69e-9 float roundoff difference with a dtype-appropriate tolerance.",
    ],
)
async def test_technical_amendments_stay_with_developer(repository, settings, reason):
    task, store = await development_task(repository, settings)
    service = DevelopmentService(repository, settings, object(), LocalExecutor(settings))
    result = await service.apply_action(
        task,
        DevelopmentAction(
            action="propose_amendment",
            reason=reason,
        ),
        {},
        store,
    )
    assert result.status == TaskStatus.PLANNING
    assert result.error is None
    assert result.development.state == "developing"
    assert "without asking the user" in result.development.feedback


async def test_only_concrete_scientific_amendments_pause(repository, settings):
    task, store = await development_task(repository, settings)
    service = DevelopmentService(repository, settings, object(), LocalExecutor(settings))
    result = await service.apply_action(
        task,
        DevelopmentAction(
            action="propose_amendment", amendment_kind="scientific", reason="A choice is needed"
        ),
        {},
        store,
    )
    assert result.status == TaskStatus.PLANNING
    impact = (
        "Replace all-cell Silhouette evaluation with sampling, changing the selection estimate."
    )
    result = await service.apply_action(
        task,
        DevelopmentAction(
            action="propose_amendment",
            amendment_kind="scientific",
            scientific_impact=impact,
            decision_scope=DecisionScope.RESEARCH_DESIGN,
        ),
        {},
        store,
    )
    assert result.status == TaskStatus.NEEDS_INPUT and result.error == impact


async def test_duplicate_probe_is_reused_without_new_compute(repository, settings):
    task, store = await development_task(repository, settings)
    service = DevelopmentService(repository, settings, object(), LocalExecutor(settings))
    result = await service.apply_action(task, DevelopmentAction(action="inspect_inputs"), {}, store)
    assert len(result.development.operations) == 1
    assert result.development.reserved_seconds == 0
    assert "already succeeded" in result.development.feedback


async def test_incomplete_replan_does_not_mark_development_failed(repository, settings):
    task, _ = await development_task(repository, settings)
    settings = settings.model_copy(update={"development_enabled": True})
    service = DevelopmentService(repository, settings, object(), LocalExecutor(settings))
    task.resource_plan = None
    await repository.update_task(task)
    with pytest.raises(DevelopmentConflict):
        await service.advance(task)
    saved = await repository.get_task(task.id)
    assert saved.status == TaskStatus.PLANNING and saved.error is None
    assert saved.development.state == "developing"


async def test_reconciler_and_duplicate_completion_cannot_interrupt_replanning(
    repository, settings
):
    entered, release = asyncio.Event(), asyncio.Event()

    class Harness(StagedHarness):
        async def create_master_plan(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().create_master_plan(**kwargs)

    class BatchBackend:
        async def get_job_status(self, job_name):
            raise AssertionError("no active job")

        async def load_job_result(self, task_id, attempt):
            raise AssertionError("no active job")

    settings = settings.model_copy(update={"planner_version": 2, "development_enabled": True})
    task, _ = await development_task(repository, settings)
    await repository.create_project(Project(id="p", slug="p", name="P", owner="alice"))
    task.interaction_revision = 1
    task.development.state = "needs_input"
    task.master_plan = task.contract = task.resource_plan = task.decision_set = None
    await repository.update_task(task)
    harness = Harness()
    first = task_service(repository, settings, harness)
    other = task_service(repository, settings, harness)
    other.executor = BatchBackend()
    ongoing = asyncio.create_task(first.process(task.id))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        counts = await other.reconcile_batch_jobs()
        assert counts["failed"] == 0
        result = await other.complete_batch(
            task.id,
            task.attempts,
            TaskResult(operation_id="probe", summary="duplicate", exit_code=0),
        )
        assert result.status == TaskStatus.PLANNING and result.error is None
        assert result.development.state == "needs_input"
    finally:
        release.set()
    result = await ongoing
    assert result.status == TaskStatus.NEEDS_APPROVAL
    assert harness.master_calls == 1
    assert result.error is None


@pytest.mark.parametrize("configured", [True, False])
async def test_package_build_is_automatic_or_technical_failure(repository, settings, configured):
    class Harness(StagedHarness):
        async def create_master_plan(self, **kwargs):
            plan, metrics = await super().create_master_plan(**kwargs)
            plan.parameters = []
            plan.primary_tool_ids = ["tool-example"]
            return plan, metrics

    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(Project(slug="tools", name="Tools", owner="alice"))
    await repository.put_tool(
        ToolRecord(
            id="tool-example",
            name="Example",
            source_url="https://example.org/example",
            version="1.2.3",
            commit_sha="a" * 40,
            license="MIT",
            install_recipe="pip install example==1.2.3",
            status=ToolStatus.CATALOGED,
        )
    )
    service = task_service(repository, settings, Harness())
    calls = []

    async def start(tool):
        calls.append(tool.id)
        if configured:
            tool.status = ToolStatus.BUILDING
            await repository.put_tool(tool)
        return configured

    service.tool_builds.start = start
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id, conversation_id="c", principal_id="alice", prompt="Use Example."
        )
    )
    task = await service.process(task.id)
    assert calls == ["tool-example"]
    assert not task.approvals
    if configured:
        assert task.status == TaskStatus.PLANNING
        assert task.pending_tool_builds == ["tool-example"]
        tool = await repository.get_tool("tool-example")
        tool.status = ToolStatus.AVAILABLE
        await repository.put_tool(tool)
        task = await service.process(task.id)
        assert task.status == TaskStatus.SUCCEEDED and not task.pending_tool_builds
    else:
        assert task.status == TaskStatus.FAILED
        assert "build infrastructure" in task.error


def test_compute_reuse_ignores_prose_but_not_increased_resources():
    from mica.services.tasks import TaskService

    old = ApprovalRequest(
        task_id="t",
        kind=ApprovalKind.COMPUTE,
        title="Compute",
        detail="",
        status="approved",
        payload={"cpu": 16, "rationale": "old"},
    )
    same = old.model_copy(
        update={"status": ApprovalStatus.PENDING, "payload": {"cpu": 16, "rationale": "new"}}
    )
    assert TaskService._carry_forward_compute_approvals([old], [same])[0].status.value == "approved"
    larger = same.model_copy(update={"payload": {"cpu": 32, "rationale": "new"}})
    assert (
        TaskService._carry_forward_compute_approvals([old], [larger])[0].status.value == "pending"
    )
