"""Command responses fit Slack and retain their authorized failure context."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from analysis_helpers import seed_project
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import access, command_intent, direct, observability
from mica.agents.analysis.help_text import COMMAND_HELP
from mica.agents.analysis.registry import Registry
from mica.agents.analysis.slack_format import chunks


async def test_help_is_split_without_truncation_or_a_worker(chat):
    event_id = await chat.accept(payload(text="help"))
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    record["notice_ts"] = "9.0"
    await chat.repository.set_connector_state("claude-event:" + event_id, record)
    chat.test_slack.reset_mock()
    assert await chat.process(event_id, 0) == "complete"
    delivered = [chat.test_slack.chat_update.call_args.kwargs["text"]] + [
        call.kwargs["text"] for call in chat.test_slack.chat_postMessage.call_args_list
    ]
    assert delivered == list(chunks(COMMAND_HELP))
    assert all(len(part) <= 3500 for part in delivered)
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    assert record["notice_text"] == COMMAND_HELP
    assert record["failure_project_id"] == chat.pid
    assert record["failure_project_name"] == "liver"
    chat.test_client.create_job.assert_not_called()


async def test_partial_slack_reply_resumes_confirmed_parts_after_reload(bridge):
    text = "a" * 3500 + "b" * 3500 + "c" * 200
    record = {"payload": payload(), "status": "pending", "notice_ts": "9.0"}
    bridge.test_slack.chat_postMessage.side_effect = [
        {"ts": "10.0"},
        RuntimeError("temporary rejection"),
        {"ts": "11.0"},
    ]
    await bridge.notify("event", record, "complete", text)
    assert "notice_text" not in record
    saved = await bridge.repository.get_connector_state("claude-event:event")
    saved = json.loads(json.dumps(saved))  # Simulate a new Cloud Tasks process.
    assert saved["notice_chunks"]["sent"] == 2
    await bridge.notify("event", saved, "complete", text)
    assert saved["notice_text"] == text
    assert "notice_chunks" not in saved
    assert bridge.test_slack.chat_update.await_count == 1
    calls = bridge.test_slack.chat_postMessage.call_args_list
    assert [c.kwargs["text"] for c in calls] == ["b" * 3500, "c" * 200, "c" * 200]
    assert calls[1].kwargs["client_msg_id"] == calls[2].kwargs["client_msg_id"]
    await bridge.notify("event", saved, "complete", text)
    assert bridge.test_slack.chat_postMessage.await_count == 3


async def test_command_delivery_failure_has_active_project_without_changing_reply_scope(chat):
    chat.test_slack.chat_postMessage.side_effect = RuntimeError("delivery failed")
    event_id = await chat.accept(payload(text="help"))
    with pytest.raises(RuntimeError, match="reply delivery failed"):
        await chat.process(event_id, 0)
    observation = next(iter(chat.repository.failures.values())).observation
    assert observation.project_id == chat.pid
    assert observation.project_name == "liver"
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    assert record["failure_project_id"] == chat.pid
    assert "project_id" not in record  # Account help does not become a project turn.


async def test_failure_context_prefers_thread_project_over_new_default(chat):
    event_id = await chat.accept(payload(text="help"))
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    chat.store.put(
        access.checkpoint_key(record["thread_key"]),
        json.dumps({"context": {"project_slug": chat.pid}}).encode(),
    )
    other = seed_project(chat.store, name="other")
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert grant.project_id == chat.pid != other
    assert observability.capture_project(record, grant)
    assert record["failure_project_id"] == chat.pid
    other_grant = access.Grant(
        grant.email, True, {}, Registry(chat.store).load(), other, True, grant.channel_key
    )
    assert not observability.capture_project(record, other_grant)
    assert record["failure_project_id"] == chat.pid


async def test_natural_language_failure_records_context_before_classifier(chat, monkeypatch):
    event_id = await chat.accept(payload(text="Please show my project choices"))
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    monkeypatch.setattr(
        command_intent, "classify", AsyncMock(side_effect=direct.RoutingUnavailable())
    )
    with pytest.raises(direct.RoutingUnavailable):
        await command_intent.prepare(chat, event_id, record, chat.store)
    saved = await chat.repository.get_connector_state("claude-event:" + event_id)
    assert saved["failure_project_id"] == chat.pid


async def test_unauthorized_or_missing_project_is_not_invented(chat):
    record = {"payload": payload(user="U2"), "thread_key": "unassigned"}
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert not grant.allowed
    assert not observability.capture_project(record, grant)
    assert "failure_project_id" not in record
    await observability.record_failure(
        chat.repository, chat.app, "slack_delivery_failed", occurrence="denied", record=record
    )
    assert next(iter(chat.repository.failures.values())).observation.project_id == ""


async def test_actual_request_project_wins_over_early_active_context(chat):
    await observability.record_failure(
        chat.repository,
        chat.app,
        "slack_delivery_failed",
        occurrence="init",
        record={
            "project_id": "created",
            "project_name": "New project",
            "failure_project_id": chat.pid,
            "failure_project_name": "liver",
        },
    )
    observation = next(iter(chat.repository.failures.values())).observation
    assert (observation.project_id, observation.project_name) == ("created", "New project")


@pytest.mark.parametrize("command", ["use", "owners"])
async def test_explicit_command_target_overrides_active_project(chat, command):
    other = seed_project(chat.store, name="other", channel="D2")
    # Existing thread remains on liver while the command targets other.
    event_id = await chat.accept(payload(text=f"{command} other"))
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    chat.store.put(
        access.checkpoint_key(record["thread_key"]),
        json.dumps({"context": {"project_slug": chat.pid}}).encode(),
    )
    # Legacy/direct retries may already carry execution context. The command's
    # explicit authorized target must win in incident metadata without rerouting it.
    record.update(project_id=chat.pid, project_name="liver")
    await chat.repository.set_connector_state("claude-event:" + event_id, record)
    chat.test_slack.chat_update.side_effect = RuntimeError("delivery failed")
    with pytest.raises(RuntimeError, match="reply delivery failed"):
        await chat.process(event_id, 0)
    observation = next(iter(chat.repository.failures.values())).observation
    assert (observation.project_id, observation.project_name) == (other, "other")


async def test_denied_command_target_never_replaces_authorized_context(chat):
    other = seed_project(chat.store, name="secret", owner="bob@example.com", channel="D2")
    record = {"payload": payload(), "thread_key": "request"}
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert observability.capture_project(record, grant)
    assert not observability.capture_project(record, grant, target=other)
    assert record["failure_project_id"] == chat.pid
