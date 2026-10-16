from __future__ import annotations

from agents import AgentOutputSchema

from mica.agents.harness import AgentHarness
from mica.models import (
    AgentTaskPlan,
    CandidateReview,
    CodeBuildResult,
    DevelopmentAction,
    FigurePlan,
    FigureSpec,
    MasterPlan,
    MasterPlanAmendment,
    ResourcePlan,
    TaskEnvelope,
    ToolPlan,
)


def test_every_agent_output_type_supports_strict_json_schema() -> None:
    output_types = (
        MasterPlan,
        MasterPlanAmendment,
        FigurePlan,
        CodeBuildResult,
        ToolPlan,
        ResourcePlan,
        AgentTaskPlan,
        DevelopmentAction,
        CandidateReview,
    )

    for output_type in output_types:
        schema = AgentOutputSchema(output_type, strict_json_schema=True).json_schema()
        assert schema["type"] == "object", output_type.__name__
        assert schema["additionalProperties"] is False, output_type.__name__


def test_legacy_figure_setting_maps_are_migrated_to_strict_records() -> None:
    figure = FigureSpec.model_validate(
        {
            "id": "spatial",
            "title": "Spatial neighborhoods",
            "kind": "spatial",
            "semantic": {"shared_scale": True},
            "presentation": {"dpi": 200},
        }
    )

    assert figure.semantic[0].id == "shared_scale"
    assert figure.semantic[0].value is True
    assert figure.presentation[0].id == "dpi"
    assert figure.presentation[0].value == 200


def test_offline_plan_does_not_invent_output_files() -> None:
    no_output = AgentHarness.offline_plan(
        TaskEnvelope(
            project_id="project",
            conversation_id="conversation",
            principal_id="alice@example.org",
            prompt="Analyze the supplied data.",
        )
    )
    requested = AgentHarness.offline_plan(
        TaskEnvelope(
            project_id="project",
            conversation_id="conversation",
            principal_id="alice@example.org",
            prompt="Analyze the data and save summary.json and overview.png.",
        )
    )

    assert no_output.contract.requested_outputs == []
    assert "result.json" not in no_output.script
    assert requested.contract.requested_outputs == ["summary.json", "overview.png"]
    assert "summary.json" in requested.script
