from __future__ import annotations

import json

import pytest
from conftest import task_service
from test_planner_v2 import StagedHarness

from pistachio.connectors.presentation import TaskConversationPresenter
from pistachio.execution.workspace import WorkspaceStore, digest
from pistachio.models import (
    ApprovalDecision,
    ApprovalKind,
    DecisionSet,
    DevelopmentAction,
    DevelopmentOperation,
    DevelopmentSession,
    MasterPlan,
    Project,
    ResourcePlan,
    StageKind,
    StageStatus,
    TaskContract,
    TaskEnvelope,
    TaskProgress,
    TaskSource,
    TaskStatus,
    ToolPlan,
)


@pytest.mark.parametrize("needs_approval", [True, False])
@pytest.mark.parametrize("defer_replan", [True, False])
@pytest.mark.parametrize("needs_compute_approval", [True, False])
async def test_followup_rebuilds_plan_before_resuming_retained_workspace(
    repository, settings, needs_approval, defer_replan, needs_compute_approval
):
    settings = settings.model_copy(update={
        "planner_version": 2, "builder_protocol_version": 2, "development_enabled": True,
        "compute_approval_usd": 0.001 if needs_compute_approval else 20,
    })
    explanation = (
        "I’ll use the installed scikit-learn implementation for the approximate search. "
        "It supports the requested method, so no new package installation is needed."
        if needs_approval else
        "I’ll adopt your suggestion and use approximate neighbor search in the revised analysis."
    )

    class Harness(StagedHarness):
        developer_calls = 0

        async def create_master_plan(self, **kwargs):
            assert "Approximate methods would also work" in kwargs["envelope"].prompt
            plan, metrics = await super().create_master_plan(**kwargs)
            plan.contract.requires_execution = True
            plan.response = "Confirm the proposed settings." if needs_approval else "Plan ready."
            plan.amendment_explanation = explanation
            if not needs_approval:
                for p in plan.parameters:
                    p.confirmed_by_user = True
                    p.requires_approval = False
            return plan, metrics

        async def development_action(self, task_id, envelope, payload):
            self.developer_calls += 1
            assert payload["master_plan"]["amendment_explanation"] == explanation
            assert payload["files"] == {"analysis.py": "# retained source"}
            return DevelopmentAction(action="read_observation", observation_path="stdout")

    class Notifier:
        def __init__(self):
            self.messages = []

        async def notify(self, task):
            self.messages.append(TaskConversationPresenter().render(task))

    project = await repository.create_project(Project(slug="replan", name="Replan", owner="alice"))
    harness = Harness()
    service = task_service(repository, settings, harness)
    notifier = Notifier()
    service.notifier = notifier
    task, _ = await service.create(TaskEnvelope(
        project_id=project.id, conversation_id="c", principal_id="alice", prompt="Cluster cells."
    ))
    # A task paused after a successful probe, with an existing workspace and spent budget.
    task.status = TaskStatus.NEEDS_INPUT
    task.contract = TaskContract(objective="Cluster cells.", requires_execution=True)
    task.master_plan = MasterPlan(contract=task.contract)
    task.resource_plan = ResourcePlan()
    task.tool_plan = ToolPlan()
    task.decision_set = DecisionSet(
        task_id=task.id, master_plan_id=task.master_plan.id, master_plan_revision=1
    )
    store = WorkspaceStore(settings, task.id)
    files = {"analysis.py": "# retained source"}
    task.development = DevelopmentSession(
        interaction_revision=0, authority_hash=service.development_service.authority(task),
        state="needs_input", source_ref=await store.put(files), source_hash=digest(files),
        model_turns=2, code_revisions=1, reserved_seconds=300, reserved_cost_usd=0.2,
        operations=[DevelopmentOperation(
            id="probe", phase="probe", attempt=1, source_hash=digest(files), status="completed",
            result_ref=await store.put({"summary": "probe passed", "stdout": "dataset inspected"}),
        )],
    )
    task.followup_response = "Old explanation"
    task.progress_updates = [TaskProgress(attempt=1, status="info", message="Old progress")]
    await repository.update_task(task)
    revised, added = await service.add_context(
        task.id, "alice", "Can you use an optimized package? Approximate methods would also work",
        source=TaskSource.WEB, external_event_id="followup-1",
    )
    assert added and revised.interaction_revision == 1
    assert revised.resource_plan is None and revised.master_plan is None
    assert not revised.progress_updates and not revised.followup_response
    assert revised.development.source_hash == digest(files)
    duplicate, added = await service.add_context(
        task.id, "alice", "duplicate", source=TaskSource.WEB, external_event_id="followup-1"
    )
    assert not added and duplicate.interaction_revision == 1

    revised = await service.process(task.id)
    assert revised.followup_response == explanation
    assert any(explanation in message for message in notifier.messages)
    if needs_approval:
        assert revised.status == TaskStatus.NEEDS_APPROVAL
        assert revised.resource_plan is None
        assert harness.developer_calls == 0
        # Duplicate dispatch while awaiting approval must not enter development.
        revised = await service.process(task.id)
        assert revised.status == TaskStatus.NEEDS_APPROVAL
        approval = revised.approvals[0]
        revised = await service.decide_approval(
            task.id, approval.id, "alice",
            ApprovalDecision(approved=True, response=json.dumps({"values": {
                field["id"]: field["proposed_value"] for field in approval.payload["fields"]
            }})), defer_replan=defer_replan,
        )
        if defer_replan:
            # Simulate a fresh dispatcher/service instance after the approval.
            service = task_service(repository, settings, harness)
            revised = await service.process(task.id)
    if needs_compute_approval:
        assert revised.status == TaskStatus.NEEDS_APPROVAL
        assert harness.developer_calls == 0
        approval = revised.approvals[-1]
        assert approval.kind == ApprovalKind.COMPUTE
        # Even with all planning artifacts present, compute approval gates development.
        revised = await service.process(task.id)
        assert revised.status == TaskStatus.NEEDS_APPROVAL
        assert harness.developer_calls == 0
        revised = await service.decide_approval(
            task.id, approval.id, "alice", ApprovalDecision(approved=True),
            defer_replan=defer_replan,
        )
        if defer_replan:
            revised = await service.process(task.id)
    assert revised.status == TaskStatus.PLANNING, revised.error
    assert revised.resource_plan is not None and revised.tool_plan is not None
    assert {s.kind for s in revised.stage_summaries if s.status == StageStatus.COMPLETED} >= {
        StageKind.TOOL_RESOLUTION, StageKind.RESOURCE_PLAN, StageKind.FIGURE_PLAN,
    }
    assert harness.master_calls == 1 and harness.figure_calls == 1
    assert harness.developer_calls == 1
    assert revised.development.interaction_revision == revised.interaction_revision == 1
    assert revised.development.authority_hash == service.development_service.authority(revised)
    assert revised.development.model_turns == 3
    assert revised.development.code_revisions == 1
    assert revised.development.reserved_seconds == 300
    assert revised.followup_response == explanation
    revised = await service.process(task.id)
    assert harness.master_calls == 1 and harness.figure_calls == 1
    assert harness.developer_calls == 2
