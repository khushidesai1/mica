"""One durable conversation and cadence, independent of the delivery surface."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import task_service

from mica.connectors.notifications import ChannelNotifier
from mica.models import (
    ApprovalKind,
    ApprovalRequest,
    ConversationMessage,
    ProgressStatus,
    Project,
    StageKind,
    TaskEnvelope,
    TaskProgress,
    TaskSource,
    TaskStatus,
)


@pytest.fixture
def slack(monkeypatch, settings):
    settings.slack_bot_token = "test"
    sent = SimpleNamespace(posts=[], fail=False)

    class Client:
        def __init__(self, **kwargs):
            pass

        def chat_postMessage(self, **kwargs):
            if sent.fail:
                raise RuntimeError("temporary delivery failure")
            sent.posts.append(kwargs)
            return {"ts": str(len(sent.posts))}

        def chat_update(self, **kwargs):
            raise AssertionError("Conversation turns must be separate posts")

    monkeypatch.setattr("mica.connectors.notifications.WebClient", Client)
    return sent


async def setup_task(repository, settings):
    project = await repository.create_project(Project(name="Test", slug="test", owner="alice"))
    service = task_service(repository, settings, None)
    service.notifier = ChannelNotifier(settings, repository)
    task, _ = await service.create(TaskEnvelope(
        project_id=project.id, principal_id="alice", source=TaskSource.SLACK,
        conversation_id="slack:T1:D1", thread_id="original", prompt="Analyze samples",
    ))
    return service, task


def replies(task):
    return [message for message in task.conversation if message.role == "assistant"]


async def test_shared_transcript_cadence_and_worker_restart(
    slack, settings, repository, monkeypatch,
):
    service, task = await setup_task(repository, settings)
    now = task.conversation[-1].created_at
    monkeypatch.setattr("mica.services.tasks.utcnow", lambda: now)
    task.status = TaskStatus.PLANNING
    for stage in [StageKind.DATASET_INSPECTION, StageKind.MASTER_PLAN, StageKind.TOOL_RESOLUTION]:
        task.current_stage = stage
        await service._notify(task, event_key=stage.value)
    assert len(slack.posts) == 1  # Internal stage churn stays out of both conversations.
    task.status = TaskStatus.RUNNING
    task.attempts = 1
    await service._notify(task)
    for index in range(1, 5):
        task.progress_updates.append(TaskProgress(
            attempt=1, status=ProgressStatus.SUCCEEDED, message="Sample compositions complete",
            current=index, total=4,
        ))
        await service._notify(task)
    assert len(slack.posts) == 2
    now += timedelta(seconds=121)
    # New service instance resumes both transcript and Slack delivery cursors.
    service = task_service(repository, settings, None)
    service.notifier = ChannelNotifier(settings, repository)
    task = await repository.get_task(task.id)
    await service._notify(task)
    assert "4 of 4" in slack.posts[-1]["text"]
    task.status = TaskStatus.SUCCEEDED
    await service._notify(task)
    saved = await repository.get_task(task.id)
    assert [post["text"] for post in slack.posts] == [m.text for m in replies(saved)]
    assert len(slack.posts) == 4
    assert all(post["thread_ts"] == "original" for post in slack.posts)


async def test_approval_and_replan_bypass_cadence(slack, settings, repository):
    service, task = await setup_task(repository, settings)
    task.status = TaskStatus.PLANNING
    task.followup_response = "I’ll use the installed package because it supports approximation."
    await service._notify(task, event_key="replan:1")
    await service._notify(task, event_key="replan:1")
    task.status = TaskStatus.NEEDS_APPROVAL
    task.approvals = [ApprovalRequest(
        task_id=task.id, kind=ApprovalKind.COMPUTE, title="Compute", detail="Approve this run.",
    )]
    await service._notify(task)
    assert len(slack.posts) == 3
    assert any(block["type"] == "actions" for block in slack.posts[-1]["blocks"])
    assert [p["text"] for p in slack.posts] == [m.text for m in replies(task)]
    assert sum(task.followup_response in p["text"] for p in slack.posts) == 1


async def test_failure_keeps_original_words_and_retries_only_missing_turns(
    slack, settings, repository,
):
    service, task = await setup_task(repository, settings)
    calls = []

    async def render(task):
        calls.append(task.id)
        return "I couldn’t finish.\n\nThe input has no spatial coordinates."

    service.conversation_agent.render = render
    task.status = TaskStatus.FAILED
    task.error = "No spatial coordinates"
    slack.fail = True
    await service._notify(task)
    saved = await repository.get_task(task.id)
    assert saved.conversation[-1].text.startswith("I couldn’t finish.")
    slack.fail = False
    await service._notify(saved)
    await service._notify(saved)
    assert len(calls) == 1
    assert len(slack.posts) == 2
    assert slack.posts[-1]["text"] == saved.conversation[-1].text


async def test_web_user_followup_and_reply_share_slack_stream(slack, settings, repository):
    service, task = await setup_task(repository, settings)
    updated, _ = await service.add_context(
        task.id, "alice", "Keep the same colors.", source=TaskSource.WEB,
        external_event_id="web-followup",
    )
    assert [p["text"] for p in slack.posts] == [
        m.text for m in updated.conversation if m.source != TaskSource.SLACK
    ]
    assert slack.posts[-2]["text"] == "Keep the same colors."


async def test_stale_worker_cannot_erase_or_rewrite_published_words(slack, settings, repository):
    service, task = await setup_task(repository, settings)
    old = task.model_copy(deep=True)
    task.conversation.append(ConversationMessage(event_key="example", text="Published words"))
    await repository.update_task(task)
    old.conversation.append(ConversationMessage(event_key="example", text="Different wording"))
    saved = await repository.update_task(old)
    assert saved.conversation[-1].text == "Published words"


async def test_web_only_task_records_replies_without_notifier(settings, repository):
    project = await repository.create_project(Project(name="Test", slug="test", owner="alice"))
    service = task_service(repository, settings, None)
    task, _ = await service.create(TaskEnvelope(
        project_id=project.id, principal_id="alice", source=TaskSource.WEB,
        conversation_id="web", prompt="Analyze samples",
    ))
    assert [m.role for m in task.conversation] == ["user", "assistant"]
    assert (await repository.get_task(task.id)).conversation == task.conversation


async def test_rejected_running_followup_is_shared_without_changing_execution(
    slack, settings, repository,
):
    from mica.services.tasks import TaskInteractionConflict

    service, task = await setup_task(repository, settings)
    task.status = TaskStatus.RUNNING
    await repository.update_task(task)
    with pytest.raises(TaskInteractionConflict):
        await service.add_context(
            task.id, "alice", "Change the method", source=TaskSource.WEB,
            external_event_id="late-followup",
        )
    saved = await repository.get_task(task.id)
    assert saved.status == TaskStatus.RUNNING
    assert not saved.context_updates
    assert [post["text"] for post in slack.posts[-2:]] == [
        message.text for message in saved.conversation[-2:]
    ]
