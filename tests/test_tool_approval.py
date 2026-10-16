from __future__ import annotations

from conftest import task_service

from mica.models import (
    AgentTaskPlan,
    ApprovalDecision,
    ApprovalKind,
    Project,
    ResourcePlan,
    TaskContract,
    TaskEnvelope,
    TaskStatus,
    ToolPlan,
    ToolProposal,
    ToolStatus,
)


class ToolHarness:
    async def plan(self, *, task_id, envelope, context):
        proposal = ToolProposal(
            id="tool-proposal-exact",
            name="ExampleTool",
            source_url="https://example.org/example-tool.git",
            version="1.2.3",
            commit_sha="a" * 40,
            license="MIT",
            install_recipe="pip install example-tool==1.2.3",
            why_needed="No approved tool provides this format.",
        )
        return AgentTaskPlan(
            contract=TaskContract(objective="Suggest a tool", requires_execution=False),
            resource_plan=ResourcePlan(),
            tool_plan=ToolPlan(proposed_tool_ids=[proposal.id], proposals=[proposal]),
            response="This exact tool version needs approval.",
        )

    async def compact_session(self, conversation_id: str) -> None:
        return None


async def test_tool_approval_is_exact_and_does_not_imply_availability(repository, settings) -> None:
    project = await repository.create_project(
        Project(slug="tools", name="Tools", owner="alice@example.org")
    )
    service = task_service(repository, settings, ToolHarness())
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="tool-conv",
            principal_id="alice@example.org",
            prompt="Find a tool for this format.",
        )
    )
    task = await service.process(task.id)
    assert task.status == TaskStatus.NEEDS_APPROVAL
    approval = next(item for item in task.approvals if item.kind == ApprovalKind.TOOL_ADD)
    task = await service.decide_approval(
        task.id,
        approval.id,
        "alice@example.org",
        ApprovalDecision(approved=True),
    )
    tool = await repository.get_tool("tool-proposal-exact")
    assert task.status == TaskStatus.NEEDS_INPUT
    assert tool and tool.status == ToolStatus.APPROVED
    assert tool.commit_sha == "a" * 40
