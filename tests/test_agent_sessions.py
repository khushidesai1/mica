"""Native controls preserve the durable queue; Slack presentation cannot govern execution."""

# ruff: noqa: F811
import copy
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge as base_bridge  # noqa: F401
from test_analysis_batch import payload
from test_migration_admission import (  # noqa: F401
    AUTH,
    buffered,
    event,
    runtime,
    set_mode,
    signed,
)

from mica.agents.analysis import agent_session, job_lifecycle
from mica.api import slack_analysis
from mica.connectors import slack


@pytest.fixture
def bridge(base_bridge, monkeypatch):
    monkeypatch.setattr(agent_session, "AsyncWebClient", lambda **kw: base_bridge.test_slack)
    return base_bridge


def stop(ts="3.000001", channel="D1", user="U1"):
    return {
        "type": "event_callback", "team_id": "T1", "event_id": "EvStop",
        "authorizations": [{"user_id": "UBOT", "is_bot": True}],
        "event": {"type": "agent_session_stopped", "channel": channel, "user": user,
                  "event_ts": ts, "thread_ts": "1.0", "streaming_message_ts": []},
    }


async def receipt(bridge, ts="1.0", status="pending", **updates):
    bridge.settings.slack_sessions = True
    identifier = await bridge.accept(payload(ts, thread=None if ts == "1.0" else "1.0"))
    record = await bridge.repository.get_connector_state("claude-event:" + identifier)
    record.update(status=status, **updates)
    await bridge.repository.set_connector_state("claude-event:" + identifier, record)
    return identifier, record


def test_stop_shape_and_exact_timestamp():
    original = stop()
    saved = copy.deepcopy(original)
    mapped = agent_session.stop_as_message(original)
    assert original == saved
    assert mapped["event_id"] == original["event_id"]
    assert mapped["event"]["text"] == "cancel"
    assert mapped["event"]["ts"] == "3.000001"
    assert mapped["event"]["thread_ts"] == "1.0"
    assert mapped["authorizations"] == original["authorizations"]
    assert agent_session.timestamp("1783536983.000002") > agent_session.timestamp(
        "1783536983.000001"
    )


@pytest.mark.parametrize("updates", [
    {"channel": "C1"}, {"channel": "G1"}, {"user": None}, {"user": "U/1"},
    {"event_ts": "NaN"}, {"event_ts": "0.9"}, {"thread_ts": None},
    {"event_ts": 3}, {"bot_id": "B1"},
])
def test_invalid_or_shared_channel_stop_is_not_mapped(updates):
    body = stop()
    body["event"].update(updates)
    assert agent_session.stop_as_message(body) is None


async def test_pending_routing_gets_status_before_status_string_changes(bridge):
    identifier, record = await receipt(bridge)
    assert agent_session.DUE in record
    assert any("/agent-session/" in call.args[2]
               for call in job_lifecycle.enqueue.call_args_list)
    await agent_session.sync(bridge, identifier)
    call = bridge.test_slack.api_call.call_args
    assert call.args == ("agents.sessions.setStatus",)
    assert call.kwargs["json"] == {
        "channel_id": "D1", "thread_ts": "1.0", "status": "processing",
        "title": "plot", "initiator_user_id": "U1",
    }
    assert (await bridge.repository.get_connector_state("claude-event:" + identifier))[
        "status"
    ] == "pending"


async def test_approval_blocks_queued_followup_but_amendment_does_not_hide_worker(bridge):
    first, a = await receipt(bridge, status="awaiting_approval")
    second, b = await receipt(bridge, "2.0")
    await agent_session.sync(bridge, second)
    assert bridge.test_slack.api_call.call_args.kwargs["json"]["status"] == "suspended"
    a["status"] = "running"
    b.update(amendable=first, conversation_amendable=True)
    for identifier, record in ((first, a), (second, b)):
        await bridge.repository.set_connector_state("claude-event:" + identifier, record)
    await agent_session.sync(bridge, second)
    assert bridge.test_slack.api_call.call_args.kwargs["json"]["status"] == "processing"


@pytest.mark.parametrize("outcome", ["complete", "failed"])
async def test_terminal_sync_survives_crash_before_enqueue_and_clears_status(bridge, outcome):
    identifier, record = await receipt(bridge)
    await agent_session.sync(bridge, identifier)
    record.update(status=outcome)
    await bridge.repository.set_connector_state("claude-event:" + identifier, record)
    # Simulate process death after the terminal receipt, before its final dispatch.
    job_lifecycle.enqueue.reset_mock()
    assert await agent_session.maintenance(bridge) == 1
    await agent_session.sync(bridge, identifier)
    assert bridge.test_slack.api_call.call_args.kwargs["json"] == {
        "channel_id": "D1", "thread_ts": "1.0", "status": "active",
    }
    assert agent_session.DUE not in await bridge.repository.get_connector_state(
        "claude-event:" + identifier
    )


async def test_overlapping_completion_does_not_clear_newer_work(bridge):
    first, a = await receipt(bridge, status="running")
    await agent_session.sync(bridge, first)
    second, _ = await receipt(bridge, "2.0")
    a["status"] = "complete"
    await bridge.repository.set_connector_state("claude-event:" + first, a)
    await agent_session.sync(bridge, first)
    assert bridge.test_slack.api_call.await_count == 1  # Still processing.
    assert agent_session.DUE in await bridge.repository.get_connector_state(
        "claude-event:" + second
    )


async def test_completed_delayed_stop_does_not_hide_surviving_newer_work(bridge):
    await receipt(bridge, status="failed")
    await receipt(bridge, "4.0", status="running")
    stop_id, _ = await receipt(bridge, "3.0", status="complete", cancel_control=True)
    await agent_session.sync(bridge, stop_id)
    assert bridge.test_slack.api_call.call_args.kwargs["json"]["status"] == "processing"


async def test_busy_session_lock_preserves_outbox(bridge):
    identifier, record = await receipt(bridge)
    key = "claude-intake:" + record["thread_key"]
    claim = await bridge.repository.begin_event(key)
    with pytest.raises(ValueError):
        await agent_session.sync(bridge, identifier)
    bridge.test_slack.api_call.assert_not_awaited()
    assert agent_session.DUE in await bridge.repository.get_connector_state(
        "claude-event:" + identifier
    )
    await bridge.repository.release_event(key, claim.token)
    await agent_session.sync(bridge, identifier)


async def test_dispatch_failure_during_real_process_does_not_fail_analysis(bridge, monkeypatch):
    identifier, _ = await receipt(bridge)
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock(side_effect=OSError("offline")))
    assert await bridge.process(identifier, 0) == "running"
    record = await bridge.repository.get_connector_state("claude-event:" + identifier)
    assert record["job"] and agent_session.DUE in record
    bridge.test_slack.api_call.assert_not_awaited()  # Only the independent endpoint contacts Slack.


async def test_status_cleanup_preserves_a_concurrent_resumption(bridge):
    identifier, record = await receipt(bridge, status="failed")

    async def resume(*args, **kwargs):
        record["status"] = "pending"
        await bridge.repository.set_connector_state("claude-event:" + identifier, record)

    bridge.test_slack.api_call.side_effect = resume
    await agent_session.sync(bridge, identifier)
    saved = await bridge.repository.get_connector_state("claude-event:" + identifier)
    assert saved["status"] == "pending" and agent_session.DUE in saved
    bridge.test_slack.api_call.side_effect = None
    await agent_session.sync(bridge, identifier)
    assert bridge.test_slack.api_call.call_args.kwargs["json"]["status"] == "processing"


async def test_refresh_and_dispatch_ids_survive_multiple_hours(bridge, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent_session.time, "time", lambda: now[0])
    identifier, _ = await receipt(bridge, status="running")
    await agent_session.sync(bridge, identifier)
    for _ in range(3):
        now[0] += 1801
        assert await agent_session.maintenance(bridge) == 1
        await agent_session.sync(bridge, identifier)
    assert bridge.test_slack.api_call.await_count == 4
    ids = [call.args[1] for call in job_lifecycle.enqueue.call_args_list]
    assert len(ids) == len(set(ids))
    assert all("title" not in c.kwargs["json"]
               for c in bridge.test_slack.api_call.call_args_list[1:])


async def test_failures_leave_durable_due_without_changing_work(bridge, monkeypatch):
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock(side_effect=OSError("queue offline")))
    identifier, record = await receipt(bridge, status="running")
    bridge.test_slack.api_call.side_effect = OSError("Slack offline")
    with pytest.raises(OSError):
        await agent_session.sync(bridge, identifier)
    assert await bridge.repository.get_connector_state("claude-event:" + identifier) == record
    assert await bridge.repository.get_connector_state(
        "slack-session:" + record["thread_key"]
    ) is None
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    bridge.test_slack.api_call.side_effect = None
    assert await agent_session.maintenance(bridge) == 1
    await agent_session.sync(bridge, identifier)


async def test_transfer_handoff_uses_its_own_card(bridge):
    identifier, _ = await receipt(
        bridge, status="complete", transfer_status_owned=True, reply_handoff="transfer"
    )
    await agent_session.sync(bridge, identifier)
    assert bridge.test_slack.api_call.call_args.kwargs["json"]["status"] == "active"


async def test_flag_off_queued_task_and_email_channel_are_noops(bridge):
    identifier, record = await receipt(bridge)
    bridge.settings.slack_sessions = False
    await agent_session.sync(bridge, identifier)
    assert await agent_session.maintenance(bridge) == 0
    bridge.settings.slack_sessions = True
    for surface, channel, channel_type in (("email", "D1", "email"), ("slack", "C1", "channel")):
        changed = copy.deepcopy(record)
        changed["surface"] = surface
        changed["payload"]["event"].update(channel=channel, channel_type=channel_type)
        await bridge.repository.set_connector_state("claude-event:" + identifier, changed)
        await agent_session.sync(bridge, identifier)
    bridge.test_slack.api_call.assert_not_awaited()


async def test_stop_cancels_running_and_queued_requests_without_checkin(bridge):
    first, _ = await receipt(
        bridge, status="running", job="test/job", principal="alice@example.com"
    )
    second, _ = await receipt(bridge, "2.0")
    native = agent_session.stop_as_message(stop())
    cancel_id = await bridge.accept(native)
    assert await bridge.accept(native) == cancel_id
    control = await bridge.repository.get_connector_state("claude-event:" + cancel_id)
    await bridge.cancel(cancel_id, control)
    a = await bridge.repository.get_connector_state("claude-event:" + first)
    b = await bridge.repository.get_connector_state("claude-event:" + second)
    assert a["cancel_requested"] and b["status"] == "failed"
    bridge.test_client.delete_job.assert_called_once_with(name="test/job")
    assert not list(bridge.store.root.glob("write-access/request-controls/*"))


@pytest.mark.parametrize("new_status", ["pending", "complete"])
async def test_delayed_stop_skips_newer_request_before_terminal_boundary(bridge, new_status):
    first, _ = await receipt(bridge, status="running", job="older/job")
    newer, b = await receipt(bridge, "4.0", status=new_status)
    cancel_id = await bridge.accept(agent_session.stop_as_message(stop("3.0")))
    control = await bridge.repository.get_connector_state("claude-event:" + cancel_id)
    await bridge.cancel(cancel_id, control)
    assert await bridge.repository.get_connector_state("claude-event:" + newer) == b
    a = await bridge.repository.get_connector_state("claude-event:" + first)
    assert a["cancel_requested"]
    bridge.test_client.delete_job.assert_called_once_with(name="older/job")


async def test_signed_stop_is_buffered_and_replayed_as_cancel(runtime, monkeypatch):
    monkeypatch.setenv("AGENT_SLACK_SESSIONS", "true")
    for body in (event(timestamp="1.0"), stop(), stop()):
        response = await runtime.client.post(
            "/integrations/slack/events", **signed(runtime.settings, body)
        )
        assert response.status_code == 200
    rows = sorted(buffered(runtime).items(), key=lambda item: item[1]["created"])
    assert len(rows) == 2 and rows[1][1]["payload"]["event"]["text"] == "cancel"
    await set_mode(runtime, "draining")
    await runtime.repository.set_connector_state("migration-replay", {"service": "old-service"})
    dispatch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(slack, "dispatch_slack_event", dispatch)
    for key, _ in rows:
        response = await runtime.client.post(
            "/api/v1/internal/maintenance/replay-slack/" + key.removeprefix("migration-slack:"),
            headers=AUTH,
        )
        assert response.status_code == 200
    assert [call.args[1]["event"]["text"] for call in dispatch.call_args_list] == [
        "Analyze the sample", "cancel",
    ]


async def test_stop_persistence_failure_is_not_acknowledged(runtime, monkeypatch):
    monkeypatch.setenv("AGENT_SLACK_SESSIONS", "true")
    monkeypatch.setattr(runtime.repository, "set_connector_state", AsyncMock(side_effect=OSError))
    response = await runtime.client.post(
        "/integrations/slack/events", **signed(runtime.settings, stop())
    )
    assert response.status_code >= 500


async def test_flag_off_or_unsigned_stop_cannot_dispatch(runtime, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(slack, "dispatch_slack_event", dispatch)
    unsigned = await runtime.client.post("/integrations/slack/events", json=stop())
    assert unsigned.status_code == 401
    response = await runtime.client.post(
        "/integrations/slack/events", **signed(runtime.settings, stop())
    )
    assert response.status_code == 200 and not buffered(runtime)
    dispatch.assert_not_awaited()


async def test_session_endpoint_authentication_and_freeze(runtime, monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr(agent_session, "sync", sync)
    path = "/api/v1/internal/slack-analysis/agent-session/" + "a" * 32
    assert (await runtime.client.post(path)).status_code == 401
    assert (await runtime.client.post(path, headers=AUTH)).status_code == 503
    sync.assert_not_awaited()
    await set_mode(runtime, "draining")
    assert (await runtime.client.post(path, headers=AUTH)).status_code == 200
    sync.assert_awaited_once()


async def test_unmapped_requester_is_not_enqueued(bridge, monkeypatch):
    monkeypatch.setattr(slack_analysis, "BatchBridge", lambda *_: bridge)
    service = type("Services", (), {"settings": bridge.app, "repository": bridge.repository})()
    await slack_analysis.receive(service, agent_session.stop_as_message(stop(user="UUNKNOWN")))
    assert not [k for k in bridge.repository.connector_state if k.startswith("claude-event:")]
