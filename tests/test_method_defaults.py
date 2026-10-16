from __future__ import annotations

import json

import pytest
from test_technical_autonomy import development_task

from mica.execution.backends import LocalExecutor
from mica.execution.workspace import digest
from mica.models import (
    CandidateReview,
    DecisionScope,
    DecisionSet,
    DevelopmentAction,
    HyperparameterSetting,
    MasterPlan,
    OutputSpec,
    ParameterClass,
    ParameterSpec,
    TaskContract,
    TaskStatus,
    ToolInvocation,
    ToolPlan,
    ToolRecord,
    ToolStatus,
)
from mica.services.development import DevelopmentService
from mica.services.planning import effective_parameter_manifest, resolve_registered_tools
from mica.services.policy import ExecutionPolicy
from mica.services.tasks import TaskService


def default_parameter(**updates):
    fields = dict(
        id="use_weights", label="Louvain edge weights", component_id="louvain",
        tool_id="scanpy", classification=ParameterClass.SCIENTIFIC,
        proposed_value=False, default_value=False, requires_approval=True,
        decision_scope=DecisionScope.METHOD_DEFAULT,
        source="scanpy.tl.louvain standard default; pinned Scanpy 1.11.1",
        scientific_impact="Edge weights may affect connectivity and labels.",
    )
    return ParameterSpec(**(fields | updates))


def test_planner_default_does_not_block_but_research_question_does(settings):
    plan = MasterPlan(
        contract=TaskContract(objective="Run Louvain"),
        parameters=[
            default_parameter(),
            default_parameter(
                id="cohort", decision_scope=DecisionScope.RESEARCH_DESIGN,
                proposed_value="treated only", label="Research population",
            ),
        ],
    )
    policy = ExecutionPolicy(settings)
    # Both the public policy entrypoint and orchestration reject an erroneous approval flag.
    for candidate in [plan, TaskService._enforce_parameter_policy(plan)]:
        approval = policy.approvals_for_master_plan("task", candidate)[0]
        assert approval.payload["parameter_ids"] == ["cohort"]
    assert not TaskService._enforce_parameter_policy(plan).parameters[0].requires_approval


@pytest.mark.parametrize("approver", ["system:no-approval-required", "alice"])
def test_defaults_keep_provenance_without_claiming_user_confirmation(approver):
    plan = MasterPlan(
        contract=TaskContract(objective="Louvain"), primary_tool_ids=["scanpy"],
        parameters=[default_parameter()],
    )
    decisions = DecisionSet(
        task_id="task", master_plan_id=plan.id, master_plan_revision=1,
        values={"use_weights": False}, approved_by=approver,
    )
    manifest = effective_parameter_manifest(plan, decisions)
    assert not manifest[0].confirmed_by_user
    record = ToolRecord(
        id="scanpy", name="Scanpy", source_url="https://github.com/scverse/scanpy",
        version="1.11.1", commit_sha="a" * 40, license="BSD-3-Clause",
        install_recipe="scanpy==1.11.1", status=ToolStatus.AVAILABLE,
    )
    tools, unknown, unavailable = resolve_registered_tools(plan, decisions, [record])
    assert not unknown and not unavailable
    setting = tools.invocations[0].hyperparameters[0]
    assert setting.source == "tool_default"
    assert setting.decision_scope == DecisionScope.METHOD_DEFAULT
    assert not setting.confirmed_by_user and not setting.requires_user_confirmation


def test_legacy_tool_review_default_does_not_become_user_question(settings):
    tools = ToolPlan(
        approved_tool_ids=["scanpy"], invocations=[ToolInvocation(
            tool_id="scanpy", tool_name="Scanpy", version="1.11.1",
            entrypoint="scanpy.tl.louvain", review_complete=True,
            review_evidence=["Pinned API signature"],
            hyperparameters=[HyperparameterSetting(
                name="directed", value=True, default_value=True,
                scientific_impact="May affect connectivity.", requires_user_confirmation=True,
                decision_scope=DecisionScope.METHOD_DEFAULT,
            )],
        )],
    )
    assert ExecutionPolicy._hyperparameter_approvals(
        "task", TaskContract(objective="Louvain", requires_execution=True), tools
    ) == []


@pytest.mark.parametrize("scope", [DecisionScope.IMPLEMENTATION, DecisionScope.METHOD_DEFAULT])
async def test_output_affecting_routine_amendment_returns_to_coder(repository, settings, scope):
    task, store = await development_task(repository, settings)
    service = DevelopmentService(repository, settings, object(), LocalExecutor(settings))
    task = await service.apply_action(task, DevelopmentAction(
        action="propose_amendment", amendment_kind="scientific", decision_scope=scope,
        scientific_impact="Graph weighting and conversion may change resolution and labels.",
    ), {}, store)
    assert task.status == TaskStatus.PLANNING and task.error is None
    assert "standard package workflow" in task.development.feedback


@pytest.mark.parametrize("scope", list(DecisionScope))
async def test_candidate_review_routes_custom_graph_repair_internally(repository, settings, scope):
    class Reviewer:
        async def review_candidate(self, *args):
            return CandidateReview(
                approved=False, issues=["Custom graph weighting must be corrected."],
                scientific_amendment_required=True, decision_scope=scope,
                scientific_impact="Graph weighting and conversion can change labels.",
            )

    task, store = await development_task(repository, settings)
    task.development.output_specs = [OutputSpec(
        path="labels.json", format="json", requested_output="Labels",
    )]
    service = DevelopmentService(repository, settings, Reviewer(), LocalExecutor(settings))
    task = await service.apply_action(
        task, DevelopmentAction(action="submit_candidate"), {"analysis.py": "pass"}, store
    )
    if scope == DecisionScope.RESEARCH_DESIGN:
        assert task.status == TaskStatus.NEEDS_INPUT
    else:
        assert task.status == TaskStatus.PLANNING
        assert task.error is None and task.development.state == "developing"
        assert "standard package defaults" in task.development.feedback


async def test_new_scope_preserves_legacy_authority_hashes(repository, settings):
    task, _ = await development_task(repository, settings)
    task.master_plan.parameters = [default_parameter(decision_scope=DecisionScope.RESEARCH_DESIGN)]
    before = DevelopmentService.authority(task)
    serialized = json.loads(task.model_dump_json())
    assert "decision_scope" not in serialized["master_plan"]["parameters"][0]
    restored = type(task).model_validate(serialized)
    assert DevelopmentService.authority(restored) == before
    task.master_plan.parameters[0].decision_scope = DecisionScope.METHOD_DEFAULT
    assert DevelopmentService.authority(task) != before
    assert digest(task.master_plan.model_dump(mode="json")) != digest(
        restored.master_plan.model_dump(mode="json")
    )
