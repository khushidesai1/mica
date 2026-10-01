from __future__ import annotations

import re

from pistachio.connectors.presentation import (
    TaskCommunicationView,
    TaskConversationPresenter,
)
from pistachio.models import (
    ParameterClass,
    ParameterSpec,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskStatus,
)


def _task(*, status: TaskStatus, result: TaskResult | None = None) -> TaskRecord:
    return TaskRecord(
        id="task_f918aa87688540da8fc96a2ec1068114",
        envelope=TaskEnvelope(
            project_id="prj_bfa6bd920d4c4bb98a8aafb3f411cdfa",
            conversation_id="slack:T123:D123",
            thread_id="1712345678.000002",
            principal_id="alice@example.org",
            prompt="Cluster spatial neighborhoods.",
        ),
        status=status,
        attempts=1,
        result=result,
    )


def test_public_name_is_stable_and_human_readable() -> None:
    first = _task(status=TaskStatus.RECEIVED)
    restored = TaskRecord.model_validate(first.model_dump(mode="json", exclude={"public_name"}))

    assert first.public_name == restored.public_name
    assert re.fullmatch(r"[a-z]+-[a-z]+-\d{4}", first.public_name)
    assert "task_" not in first.public_name


def test_queued_update_hides_backend_job_and_internal_identifiers() -> None:
    task = _task(
        status=TaskStatus.QUEUED,
        result=TaskResult(
            summary=(
                "Submitted Google Cloud Batch job projects/azizilab-aml/locations/"
                "us-central1/jobs/task-f918aa87688540da8fc96a2ec1068114."
            )
        ),
    )

    message = TaskConversationPresenter().render(task)
    view = TaskCommunicationView.from_task(task)

    assert message == "The analysis is queued. I’ll start it as soon as compute is ready."
    assert task.id not in message
    assert task.envelope.project_id not in message
    assert "Google Cloud Batch" not in message
    assert not hasattr(view, "task_id")
    assert not hasattr(view, "project_id")


def test_completed_settings_are_readable_and_not_dumped_as_backend_json() -> None:
    task = _task(
        status=TaskStatus.SUCCEEDED,
        result=TaskResult(
            summary="The neighborhood analysis is complete.",
            effective_parameters=[
                ParameterSpec(
                    id="neighbors",
                    label="Local spatial neighborhood",
                    classification=ParameterClass.SCIENTIFIC,
                    component_id="spatial-clustering",
                    proposed_value="50 nearest cells within each sample",
                    source="Proposed analysis setting",
                ),
                ParameterSpec(
                    id="dpi",
                    label="Figure DPI",
                    classification=ParameterClass.PRESENTATION,
                    component_id="figures",
                    proposed_value=300,
                    source="Automatic presentation setting",
                ),
            ],
        ),
    )

    message = TaskConversationPresenter().render(task)

    assert "Done — The neighborhood analysis is complete." in message
    assert "*Settings used*" in message
    assert "• *Local spatial neighborhood:* 50 nearest cells within each sample" in message
    assert "Figure DPI" not in message
    assert "Proposed analysis setting" not in message
    assert '{\n  "' not in message
    assert task.id not in message
    assert task.envelope.project_id not in message



async def test_replan_agent_receives_followup_and_grounded_decision(monkeypatch, settings):
    import json
    from types import SimpleNamespace

    from pistachio.connectors.presentation import TaskConversationAgent, UserFacingMessage
    from pistachio.models import MasterPlan, TaskContextUpdate, TaskContract, TaskSource

    task = _task(status=TaskStatus.PLANNING)
    task.context_updates = [TaskContextUpdate(
        principal_id="alice", source=TaskSource.WEB, external_event_id="followup",
        message="Could you install an optimized package for gs://private/data.h5ad?",
    )]
    task.master_plan = MasterPlan(
        contract=TaskContract(objective="Cluster neighborhoods"),
        amendment_explanation=(
            "The installed tools support the requested approximation, so I plan to use them "
            "without a new installation."
        ),
    )
    task.plan_response = "Approval is needed for the Silhouette sample size."
    agent = TaskConversationAgent(settings)
    agent.agent = object()
    converted = "I’ll use the installed tools for approximation; no new installation is needed."

    async def run(agent, prompt, **kwargs):
        payload = json.loads(prompt)
        assert payload["communication_kind"] == "replan_explanation"
        assert "optimized package" in payload["latest_followup"]
        assert "gs://" not in prompt
        assert task.id not in prompt and task.envelope.project_id not in prompt
        assert payload["planner_response"] == task.master_plan.amendment_explanation
        assert "Approval is needed" not in prompt
        return SimpleNamespace(final_output=UserFacingMessage(text=converted))

    monkeypatch.setattr("pistachio.connectors.presentation.Runner.run", run)
    assert await agent.render_replan(task) == converted

    async def unavailable(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("pistachio.connectors.presentation.Runner.run", unavailable)
    assert await agent.render_replan(task) == task.master_plan.amendment_explanation


async def test_replan_explanation_stays_visible_beside_approval_and_progress(settings):
    from pistachio.connectors.presentation import TaskConversationAgent
    from pistachio.models import ApprovalKind, ApprovalRequest, StageKind

    task = _task(status=TaskStatus.NEEDS_APPROVAL)
    task.followup_response = "I’ll keep the installed package because it supports approximation."
    task.approvals = [ApprovalRequest(
        task_id=task.id, kind=ApprovalKind.SCIENTIFIC_STEP,
        title="Confirm sampling", detail="Use 100,000 cells for Silhouette evaluation.",
    )]
    agent = TaskConversationAgent(settings)
    message = await agent.render(task)
    assert task.followup_response in message
    assert "Confirm sampling" in message and "100,000" in message
    task.status = TaskStatus.PLANNING
    task.current_stage = StageKind.RESOURCE_PLAN
    message = await agent.render(task)
    assert task.followup_response in message and "compute setup" in message


async def test_substantive_reply_preserves_paragraphs_and_grounded_objective(monkeypatch, settings):
    import json
    from types import SimpleNamespace

    from pistachio.connectors.presentation import TaskConversationAgent, UserFacingMessage
    from pistachio.models import TaskContract

    task = _task(status=TaskStatus.SUCCEEDED)
    task.contract = TaskContract(objective="Compare spatial neighborhoods across samples.")
    agent = TaskConversationAgent(settings)
    agent.agent = object()

    async def run(agent, prompt, **kwargs):
        assert json.loads(prompt)["objective"] == task.contract.objective
        return SimpleNamespace(final_output=UserFacingMessage(
            text="The analysis is complete.\n\nThe results are ready for review."
        ))

    monkeypatch.setattr("pistachio.connectors.presentation.Runner.run", run)
    assert await agent.render(task) == (
        "The analysis is complete.\n\nThe results are ready for review."
    )


async def test_internal_evidence_failure_never_becomes_a_user_reformatting_request(
    monkeypatch, settings,
):
    import json
    from types import SimpleNamespace

    from pistachio.connectors.presentation import TaskConversationAgent, UserFacingMessage
    from pistachio.models import ConversationMessage, TaskContextUpdate, TaskSource
    from pistachio.services.amendments import AMENDMENT_FAILURE_MESSAGE

    task = _task(status=TaskStatus.FAILED)
    task.error = "ValueError: sample_size: amendment evidence must quote the actual exchange"
    task.followup_response = "Please supply the amendment evidence in the required format."
    message = 'Use 10k uniformly sampled cells; choose a seed. Save the settings as JSON.'
    task.context_updates = [TaskContextUpdate(
        message=message, principal_id="alice", source=TaskSource.SLACK, external_event_id="reply",
    )]
    task.conversation = [
        ConversationMessage(role="user", text=message, event_key="reply", kind="input"),
        ConversationMessage(text=task.error, event_key="failure", kind="error"),
    ]
    view = TaskCommunicationView.from_task(task)
    assert view.latest_followup == message
    assert view.recent_dialogue[0]["text"] == message
    assert view.recent_dialogue[1]["text"] == AMENDMENT_FAILURE_MESSAGE
    assert view.error == view.followup_response == AMENDMENT_FAILURE_MESSAGE
    agent = TaskConversationAgent(settings)
    agent.agent = object()

    async def run(agent, prompt, **kwargs):
        assert json.loads(prompt)["error"] == AMENDMENT_FAILURE_MESSAGE
        assert "must quote the actual exchange" not in prompt
        return SimpleNamespace(final_output=UserFacingMessage(
            text="Please provide evidence_refs with your reply."
        ))

    monkeypatch.setattr("pistachio.connectors.presentation.Runner.run", run)
    response = await agent.render(task)
    assert AMENDMENT_FAILURE_MESSAGE in response
    assert "evidence_refs" not in response and "ValueError" not in response


async def test_delegated_choice_reply_is_grounded_in_current_settings_and_recent_dialogue(
    monkeypatch, settings,
):
    import json
    from types import SimpleNamespace

    from pistachio.connectors.presentation import TaskConversationAgent, UserFacingMessage
    from pistachio.models import (
        ConversationMessage,
        MasterPlan,
        TaskContextUpdate,
        TaskContract,
        TaskSource,
    )

    task = _task(status=TaskStatus.PLANNING)
    reply = "10k uniformly sampled cells, choose the seed"
    task.context_updates = [TaskContextUpdate(
        message=reply, principal_id="alice", source=TaskSource.SLACK, external_event_id="reply",
    )]
    task.conversation = [ConversationMessage(
        role="user", text=reply, event_key="reply", kind="input",
    )]
    confirmation = (
        "I’ll use 10,000 uniformly sampled cells and a fixed seed. "
        "Clustering will still include every cell."
    )
    task.master_plan = MasterPlan(
        contract=TaskContract(objective="Cluster all cells"),
        amendment_explanation=confirmation,
        parameters=[ParameterSpec(
            id="sample", label="Silhouette sample size", component_id="cluster",
            classification=ParameterClass.SCIENTIFIC, proposed_value=10000, confirmed_by_user=True,
        )],
    )
    agent = TaskConversationAgent(settings)
    agent.agent = object()

    async def run(agent, prompt, **kwargs):
        payload = json.loads(prompt)
        assert payload["recent_dialogue"][-1] == {"role": "user", "text": reply}
        assert payload["plan_settings"][0]["value"] == "10000"
        return SimpleNamespace(final_output=UserFacingMessage(text=confirmation))

    monkeypatch.setattr("pistachio.connectors.presentation.Runner.run", run)
    assert await agent.render_replan(task) == confirmation
