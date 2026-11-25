"""Concurrent replies, mixed worker versions, and silent channel turns."""
# ruff: noqa: F811
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401
from test_conversation_tools import Client, reply, tool

from mica.agents.analysis import (
    access,
    conversation,
    direct,
    memory_tasks,
    thread_checkpoint,
)
from mica.agents.analysis.registry import Registry
from mica.agents.analysis.storage import LocalStorage
from mica.api import slack_analysis


async def running(bridge, *, confirmed=True):
    bridge.settings.side_replies = True
    eid = await bridge.accept(payload(text="Run a tiny calculation"), principal="alice@example.com")
    assert await bridge.process(eid, 0) == "running"
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    if confirmed:
        record["checkpoint_merge_attempt"] = record["attempt"]
        await bridge.repository.set_connector_state("claude-event:" + eid, record)
    return eid, record


def model(monkeypatch, *frames):
    client = Client(*frames)
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    return client


async def test_busy_reply_and_queued_work_reuses_decision(bridge, monkeypatch):
    first, active = await running(bridge)
    client = model(monkeypatch, reply("The tiny calculation is running."),
                   tool("start_worker", task="Then print today's date"))
    second = await bridge.accept(payload("2.0", "1.0", "What are you running?"))
    assert await bridge.process(second, 0) == "complete"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert record["control_bypass"]
    context = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
    assert context["active_work"]["request"] == "Run a tiny calculation"
    third = await bridge.accept(payload("3.0", "1.0", "Then print the date"))
    assert await bridge.process(third, 0) == "pending"
    assert bridge.test_client.create_job.call_count == 1
    active["status"] = "complete"
    await bridge.repository.set_connector_state("claude-event:" + first, active)
    assert await bridge.process(third, 1) == "running"
    assert len(client.calls) == 2
    assert bridge.test_client.create_job.call_count == 2


@pytest.mark.parametrize("confirmed,enabled", [(False, True), (True, False)])
async def test_old_or_disabled_worker_queues_without_history_loss(
    bridge, monkeypatch, confirmed, enabled,
):
    first, active = await running(bridge, confirmed=confirmed)
    bridge.settings.side_replies = enabled
    path = access.checkpoint_key(active["thread_key"])
    old_snapshot = json.loads(bridge.store.get(path)[0])
    client = model(monkeypatch, reply("Busy answer must be discarded."), reply("Finished answer."))
    second = await bridge.accept(payload("2.0", "1.0", "How is it going?"))
    assert await bridge.process(second, 0) == "pending"
    assert await bridge.process(second, 1) == "pending"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert "direct_decision" not in record and not record.get("control_bypass")
    assert len(client.calls) == 1
    assert all(m.get("id") != second for m in json.loads(bridge.store.get(path)[0])["messages"])
    # An old image writes the snapshot it took at startup, unconditionally.
    direct.record_exchange(old_snapshot, first, "Calculate", "Done", seq=active["seq"])
    bridge.store.put(path, json.dumps(old_snapshot).encode())
    active["status"] = "complete"
    await bridge.repository.set_connector_state("claude-event:" + first, active)
    assert await bridge.process(second, 2) == "complete"
    rows = json.loads(bridge.store.get(path)[0])["messages"]
    assert [m["id"] for m in rows] == [first, second]
    assert len(client.calls) == 2


async def test_old_worker_behind_queued_followup_is_still_unsafe(bridge, monkeypatch):
    _, active = await running(bridge, confirmed=False)
    model(monkeypatch, tool("start_worker", task="Print date after this"), reply("Still busy."))
    second = await bridge.accept(payload("2.0", "1.0", "After this print date"))
    assert await bridge.process(second, 0) == "pending"
    third = await bridge.accept(payload("3.0", "1.0", "How is it going?"))
    assert await bridge.process(third, 0) == "pending"
    record = await bridge.repository.get_connector_state("claude-event:" + third)
    assert not record["side_replies_allowed"]
    assert record["active_work"]["identity"][0] == active["event_id"]


async def test_recovery_attempt_must_confirm_again(bridge, monkeypatch):
    first, active = await running(bridge)
    active["attempt"] += 1
    await bridge.repository.set_connector_state("claude-event:" + first, active)
    client = model(monkeypatch, reply("Wait"), reply("Now safe"))
    second = await bridge.accept(payload("2.0", "1.0", "How is it going?"))
    assert await bridge.process(second, 0) == "pending"
    active["checkpoint_merge_attempt"] = active["attempt"]
    await bridge.repository.set_connector_state("claude-event:" + first, active)
    assert await bridge.process(second, 1) == "complete"
    assert len(client.calls) == 2


@pytest.mark.parametrize("route,expected", [("compute", True), ("reply", False)])
async def test_busy_amendments_follow_compute_decision(bridge, route, expected):
    bridge.settings.amendments = True
    first, active = await running(bridge)
    active["amendments_enabled"] = True
    await bridge.repository.set_connector_state("claude-event:" + first, active)
    bridge.decision.return_value = conversation.Decision(route=route, reply="Requested work")
    second = await bridge.accept(payload("2.0", "1.0", "Another request"),
                                 principal="alice@example.com")
    await bridge.process(second, 0)
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert record["conversation_amendable"] is expected
    if expected:
        assert record.get("amendable") and record["notice_phase"] == "waiting"


async def test_unsafe_busy_preference_is_not_applied(bridge, monkeypatch):
    await running(bridge, confirmed=False)
    client = model(monkeypatch, tool("propose_command", operation="scripts", preference="on"))
    second = await bridge.accept(payload("2.0", "1.0", "Always return scripts"))
    assert await bridge.process(second, 0) == "pending"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert not record.get("operations") and len(client.calls) == 1


def test_merge_interleaved_worker_final_and_conflict(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    path = access.checkpoint_key("thread")
    old = {"context": {"session_id": "old"}, "turn": 1, "messages": []}
    storage.put(path, json.dumps(old).encode())
    worker = copy.deepcopy(old)
    side = copy.deepcopy(old)
    direct.record_exchange(side, "side", "How?", "Running", speaker="U2", seq=2)
    original = storage.put_if
    writes = []

    def conflict(key, data, generation):
        if not writes:
            writes.append(True)
            thread_checkpoint.merge(storage, key, side, messages_only=True)
        return original(key, data, generation)

    monkeypatch.setattr(storage, "put_if", conflict)
    worker.update(context={"session_id": "worker-final"}, turn=2)
    direct.record_exchange(worker, "worker", "Compute", "Done", speaker="U1", seq=1)
    result = thread_checkpoint.merge(storage, path, worker)
    assert [m["id"] for m in result["messages"]] == ["worker", "side"]
    assert result["context"]["session_id"] == "worker-final" and result["turn"] == 2
    assert result["messages"][1]["speaker"] == "U2"
    assert thread_checkpoint.merge(storage, path, side, messages_only=True) == result


async def test_no_reply_silent_and_speaker_in_next_context(bridge, monkeypatch):
    def publish(doc):
        doc.publish(bridge.pid, "alice@example.com", "T1", "C1")
        doc.bind("T1", "C1", "alice@example.com", bridge.pid, private=False)
        return bridge.pid

    Registry(bridge.store).update("publish", "alice@example.com", "publish", publish)
    model(monkeypatch, reply("Ready"))
    first = await bridge.accept(payload(channel="C1", channel_type="channel", kind="app_mention"))
    assert await bridge.process(first, 0) == "complete"
    bridge.test_slack.reset_mock()
    client = model(monkeypatch, tool("no_reply"), reply("They asked a colleague to stop."))
    second = await bridge.accept(payload("2.0", "1.0", "stop", channel="C1",
                                         channel_type="channel"))
    assert await bridge.process(second, 0) == "complete"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert not record["addressed"] and not record.get("cancel_control")
    assert not memory_tasks.eligible(bridge, record)
    assert not memory_tasks.conversation_turn(record)
    bridge.test_slack.chat_postMessage.assert_not_called()
    bridge.test_slack.chat_update.assert_not_called()
    assert "no_reply" in {t["name"] for t in client.calls[0]["tools"]}
    third = await bridge.accept(payload("3.0", "1.0", "What did they ask?", channel="C1",
                                        channel_type="channel", kind="app_mention"))
    assert await bridge.process(third, 0) == "complete"
    context = json.loads(client.calls[1]["messages"][0]["content"][0]["text"])
    row = next(m for m in context["recent_messages"] if m["id"] == second)
    assert row["speaker"] == "U1" and row["user"] == "stop" and row["assistant"] == ""
    assert "no_reply" not in {t["name"] for t in client.calls[1]["tools"]}


@pytest.mark.parametrize("attempt,authenticated", [(0, True), (1, True), (0, False)])
async def test_credentials_confirmation_is_authenticated_and_attempt_bound(
    bridge, monkeypatch, attempt, authenticated,
):
    eid, record = await running(bridge, confirmed=False)
    services = SimpleNamespace(settings=bridge.app, repository=bridge.repository)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer identity")],
                       "query_string": f"checkpoint_protocol=2&attempt={attempt}".encode(),
                       "app": SimpleNamespace(state=SimpleNamespace(services=services))})
    verify = Mock(return_value={"email": bridge.app.batch_service_account,
                                "email_verified": authenticated})
    monkeypatch.setattr(slack_analysis.id_token, "verify_oauth2_token", verify)
    if authenticated:
        await slack_analysis.credentials(eid, request, Response())
    else:
        with pytest.raises(HTTPException):
            await slack_analysis.credentials(eid, request, Response())
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    assert (record.get("checkpoint_merge_attempt") == 0) is (authenticated and attempt == 0)


async def test_new_attempt_during_routing_cannot_receive_a_side_exchange(bridge, monkeypatch):
    first, active = await running(bridge)
    path = access.checkpoint_key(active["thread_key"])

    async def recovery_during_reply(*args, **kwargs):
        active["attempt"] += 1
        await bridge.repository.set_connector_state("claude-event:" + first, active)
        return conversation.Decision(route="reply", reply="This decision must wait.")

    monkeypatch.setattr(conversation, "run", recovery_during_reply)
    second = await bridge.accept(payload("2.0", "1.0", "How is it going?"))
    assert await bridge.process(second, 0) == "pending"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert not record.get("control_bypass") and "direct_decision" not in record
    assert not any(m["id"] == second for m in json.loads(bridge.store.get(path)[0])["messages"])


async def test_checkpoint_write_serializes_with_launch_and_recovery(bridge, monkeypatch):
    first, active = await running(bridge)
    bridge.decision.return_value = conversation.Decision(route="reply", reply="Still working.")
    path = access.checkpoint_key(active["thread_key"])
    original = bridge.store.put_if
    checked = []

    def put(key, data, generation):
        if key == path and json.loads(data).get("messages"):
            assert "claude-poll:" + first in bridge.repository.event_claims
            checked.append(True)
        return original(key, data, generation)

    monkeypatch.setattr(bridge.store, "put_if", put)
    second = await bridge.accept(payload("2.0", "1.0", "How is it going?"))
    assert await bridge.process(second, 0) == "complete"
    assert checked


async def test_busy_routing_failure_defers_history_until_old_worker_finishes(bridge):
    first, active = await running(bridge, confirmed=False)
    bridge.decision.side_effect = ValueError("Cannot complete this request")
    second = await bridge.accept(payload("2.0", "1.0", "How is it going?"))
    assert await bridge.process(second, 0) == "failed"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert record["control_bypass"] and record["terminal_history_waiting"]
    path = access.checkpoint_key(active["thread_key"])
    assert not json.loads(bridge.store.get(path)[0])["messages"]
    active["status"] = "complete"
    await bridge.repository.set_connector_state("claude-event:" + first, active)
    assert await bridge.process(second, 1) == "failed"
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    assert record["terminal_recorded"] and not record.get("terminal_history_waiting")
    assert json.loads(bridge.store.get(path)[0])["messages"][0]["id"] == second
