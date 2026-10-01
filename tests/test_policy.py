from __future__ import annotations

import json

import pytest
from agents.strict_schema import ensure_strict_json_schema

from pistachio.models import (
    AgentTaskPlan,
    ApprovalKind,
    HyperparameterSetting,
    HyperparameterSource,
    ProvisioningModel,
    ResourcePlan,
    ResourceProfile,
    ScientificDecisionProposal,
    TaskContract,
    ToolInvocation,
    ToolPlan,
)
from pistachio.services.policy import ExecutionPolicy, PolicyViolation


def test_compute_and_scientific_approval_gates(settings) -> None:
    policy = ExecutionPolicy(settings)
    contract = TaskContract(
        objective="Compare cohorts",
        missing_scientific_decisions=["Which test?"],
        requires_execution=True,
    )
    resources = ResourcePlan(
        profile=ResourceProfile.MEMORY_LARGE,
        cpu=16,
        memory_gib=128,
        disk_gib=500,
        timeout_hours=12,
        estimated_cost_usd=25,
    )
    approvals = policy.approvals_for("task-1", contract, resources, ToolPlan())
    assert [item.kind for item in approvals] == [ApprovalKind.SCIENTIFIC_STEP]


def test_resource_profiles_and_spot_are_exact(settings) -> None:
    policy = ExecutionPolicy(settings)
    with pytest.raises(PolicyViolation, match="must use"):
        policy.validate_resource_plan(ResourcePlan(profile=ResourceProfile.CPU_SMALL, cpu=3))
    with pytest.raises(PolicyViolation, match="checkpointable"):
        policy.validate_resource_plan(ResourcePlan(provisioning_model=ProvisioningModel.SPOT))


def test_named_resource_profile_is_canonicalized_before_validation(settings) -> None:
    policy = ExecutionPolicy(settings)
    model_plan = ResourcePlan(
        profile=ResourceProfile.MEMORY_LARGE,
        cpu=16,
        memory_gib=128,
        disk_gib=500,
        timeout_hours=24,
    )

    canonical = policy.canonicalize_resource_plan(model_plan)

    assert (
        canonical.cpu,
        canonical.memory_gib,
        canonical.gpu_count,
        canonical.disk_gib,
        canonical.timeout_hours,
    ) == (16, 128, 0, 500, 12.0)
    policy.validate_resource_plan(canonical)


def test_tool_hyperparameters_require_source_backed_user_confirmation(settings) -> None:
    policy = ExecutionPolicy(settings)
    tools = ToolPlan(
        approved_tool_ids=["tool-azizilab-decipher"],
        invocations=[
            ToolInvocation(
                tool_id="tool-azizilab-decipher",
                tool_name="Decipher",
                version="0.1.3",
                entrypoint="decipher train",
                review_complete=True,
                review_evidence=["https://github.com/azizilab/decipher/tree/9610b31"],
                hyperparameters=[
                    HyperparameterSetting(
                        name="latent_dim",
                        value=16,
                        default_value=16,
                        source=HyperparameterSource.TOOL_DEFAULT,
                        scientific_impact=(
                            "Changes representation capacity and downstream clusters."
                        ),
                        requires_user_confirmation=True,
                        user_question="Use latent_dim=16 or choose another value?",
                    )
                ],
            )
        ],
    )
    approvals = policy.approvals_for(
        "task-parameters",
        TaskContract(objective="Run Decipher", requires_execution=True),
        ResourcePlan(),
        tools,
    )
    approval = next(item for item in approvals if item.kind == ApprovalKind.HYPERPARAMETER)
    assert approval.detail == "Review 1 tool settings before execution."
    assert approval.payload["parameters"][0]["tool_name"] == "Decipher"
    assert approval.payload["fields"][0]["proposed_value"] == 16


def test_scientific_approval_is_compact_and_structured(settings) -> None:
    policy = ExecutionPolicy(settings)
    contract = TaskContract(
        objective="Cluster neighborhood compositions",
        missing_scientific_decisions=["Confirm the sample key."],
        proposed_scientific_decisions=[
            ScientificDecisionProposal(
                id="sample_key",
                label="Sample column",
                proposed_value="sample_id",
                options=["sample_id", "batch"],
                rationale="sample_id contains the four requested samples.",
                source="verified H5AD metadata",
                confidence=0.95,
            )
        ],
        requires_execution=True,
    )

    approval = policy.approvals_for("task-schema", contract, ResourcePlan(), ToolPlan())[0]

    assert approval.detail == "Review 1 proposed settings, edit any value, then confirm."
    assert approval.payload["fields"][0]["proposed_value"] == "sample_id"


def test_confirmed_hyperparameter_plan_does_not_reprompt(settings) -> None:
    policy = ExecutionPolicy(settings)
    tools = ToolPlan(
        approved_tool_ids=["tool-1"],
        invocations=[
            ToolInvocation(
                tool_id="tool-1",
                tool_name="Tool",
                version="1.0",
                entrypoint="tool run",
                review_complete=True,
                review_evidence=["https://example.org/tool/1.0/parameters"],
                hyperparameters=[
                    HyperparameterSetting(
                        name="resolution",
                        value=0.5,
                        source=HyperparameterSource.USER_SPECIFIED,
                        tunable=True,
                        scientific_impact="Changes the granularity of the result.",
                        requires_user_confirmation=True,
                        confirmed_by_user=True,
                    )
                ],
            )
        ],
    )
    approvals = policy.approvals_for(
        "task-confirmed",
        TaskContract(objective="Run Tool", requires_execution=True),
        ResourcePlan(),
        tools,
    )
    assert all(item.kind != ApprovalKind.HYPERPARAMETER for item in approvals)


def test_agent_plan_has_strict_sdk_output_schema() -> None:
    schema = ensure_strict_json_schema(AgentTaskPlan.model_json_schema())
    assert schema["additionalProperties"] is False
    assert "uniqueItems" not in json.dumps(schema)
