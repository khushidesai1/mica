"""Logical tasks survive delivery windows; check-ins never authorize or cancel implicitly."""

# ruff: noqa: F811
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_runner import result, setup  # noqa: F401
from test_conversation_tools import Client, reply, run, tool, turn  # noqa: F401
from test_gpu_compute import gpu  # noqa: F401

from mica.agents.analysis import (
    approvals,
    compute_approval,
    conversation,
    conversation_state,
    direct,
    observability,
    recovery,
    request_controls,
    runner,
)
from mica.agents.analysis.config import CHECKPOINT_PREFIX


@pytest.fixture
def flexible_turn(turn, monkeypatch):
    bridge, _, record, _ = turn
    record["flexible_harness"] = True
    monkeypatch.setattr(conversation, "inventory", Mock(return_value={"folders": ["evidence"]}))
    bridge.resolve_data = Mock(return_value=(bridge.settings, None))
    return turn


async def test_six_calls_yield_and_restart_with_all_evidence(flexible_turn):
    bridge, event_id, record, _ = flexible_turn
    first = Client(*(tool("list_folders", paths=[f"folder{i}/"]) for i in range(6)))
    with pytest.raises(conversation_state.YieldConversation):
        await run(flexible_turn, first)
    assert record["routing_calls"] == 6 and "routing_budget" not in record
    saved = await conversation_state.load(bridge.store, event_id)
    assert len(saved["messages"]) == 13
    next_client = Client(tool("list_folders", paths=["seventh/"]), reply("Finished."))
    assert (await run(flexible_turn, next_client)).reply == "Finished."
    assert next_client.calls[0]["messages"] == saved["messages"]
    assert next_client.calls[0]["tools"] and record["routing_calls"] == 8
    bridge.test_client.create_job.assert_not_called()


async def test_timeout_does_not_discard_evidence_or_exhaust_retry(flexible_turn):
    bridge, event_id, record, _ = flexible_turn
    first = Client(tool("list_folders", paths=["raw/"]), TimeoutError())
    with pytest.raises(direct.RoutingUnavailable):
        await run(flexible_turn, first)
    saved = await conversation_state.load(bridge.store, event_id)
    assert len(saved["messages"]) == 3 and record["routing_error_kind"] == "timeout"
    second = Client(reply())
    await run(flexible_turn, second)
    assert second.calls[0]["messages"] == saved["messages"]
    assert second.calls[0]["timeout"] > 80


async def test_output_truncation_repairs_without_executing_partial_tool(flexible_turn):
    broken = tool("list_folders", paths=["raw/"])
    with pytest.raises(conversation_state.YieldConversation):
        await run(flexible_turn, Client(("max_tokens", broken[1])))
    conversation.inventory.assert_not_called()
    assert (await run(flexible_turn, Client(reply("x" * 16001)))).reply == "x" * 16001


async def test_compaction_archives_evidence_and_keeps_request(flexible_turn):
    bridge, event_id, record, grant = flexible_turn
    original = {"role": "user", "content": "Original authorized request"}
    saved = {
        "version": 1,
        "access": grant.signature,
        "calls": 8,
        "compactions": 0,
        "messages": [original, {"role": "assistant", "content": "x" * 400000}],
    }
    await conversation_state.save(bridge.store, event_id, saved)
    sdk = Client(reply("Verified findings, source references and unresolved next step."), reply())
    await run(flexible_turn, sdk)
    assert sdk.calls[1]["messages"][0] == original
    assert record["routing_compactions"] == 1
    archived, _ = bridge.store.get(
        conversation_state.key(event_id).replace("state.json", "context-0.json")
    )
    assert json.loads(archived) == saved


async def test_cancel_during_model_call_prevents_terminal_dispatch(flexible_turn):
    bridge, event_id, record, _ = flexible_turn
    sdk = Client(reply("Should not be delivered"))
    create = sdk.create

    async def cancel_then_respond(**kwargs):
        bridge.store.put(f"{CHECKPOINT_PREFIX}/events/{event_id}/memory-cancelled.json", b"true")
        return await create(**kwargs)

    sdk.create = cancel_then_respond
    with pytest.raises(conversation_state.CancelConversation):
        await run(flexible_turn, sdk)
    assert "direct_decision" not in record
    bridge.test_client.create_job.assert_not_called()


async def test_refusal_finishes_instead_of_retrying_forever(flexible_turn):
    assert await run(flexible_turn, Client(reply("Cannot help with that.", stop="refusal")))


@pytest.fixture
async def checkin(bridge, monkeypatch):
    monkeypatch.setattr(request_controls, "AsyncWebClient", lambda **kw: bridge.test_slack)
    bridge.settings.flexible_harness = True
    event_id = await bridge.accept(payload())
    record = await bridge.repository.get_connector_state("claude-event:" + event_id)
    record.update(routing_calls=6, routing_started=time.time() - 130)
    await bridge.repository.set_connector_state("claude-event:" + event_id, record)
    await request_controls.sync(bridge, event_id, record)
    return bridge, event_id, record


def action(event_id, operation, **changes):
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "D1"},
        "message": {"ts": "9.0"},
        "actions": [{"action_id": request_controls.PREFIX + operation, "value": event_id}],
        **changes,
    }


async def test_checkin_silence_continue_and_day_are_nonblocking(checkin):
    bridge, event_id, record = checkin
    posts = bridge.test_slack.chat_postMessage.await_count
    await request_controls.sync(bridge, event_id, record)
    assert bridge.test_slack.chat_postMessage.await_count == posts
    assert not await request_controls.cancelled(bridge.store, event_id)
    await approvals.handle(bridge, bridge.store, action(event_id, "continue"), BackgroundTasks())
    state, _ = approvals.load_json(bridge.store, request_controls.key(event_id))
    assert set(state["acknowledged"]) == {"routing_steps", "routing_slow"}
    record["routing_started"] = time.time() - 86401
    await request_controls.sync(bridge, event_id, record)
    assert "24 hours" in bridge.test_slack.chat_update.call_args.kwargs["text"]
    assert record["status"] == "pending"
    record["route"] = "compute"
    assert "routing_day" not in request_controls.reasons(record)


async def test_report_is_durable_idempotent_and_does_not_cancel(checkin):
    bridge, event_id, record = checkin
    for _ in range(2):
        await approvals.handle(bridge, bridge.store, action(event_id, "report"), BackgroundTasks())
    incident = await bridge.repository.request_failure(
        observability.request_scope(bridge.app), event_id
    )
    assert incident.observation.cause == "request_reported" and incident.count == 1
    assert not await request_controls.cancelled(bridge.store, event_id)
    assert (await bridge.repository.get_connector_state("claude-event:" + event_id)) == record


async def test_report_persistence_failure_is_not_falsely_acknowledged(checkin, monkeypatch):
    bridge, event_id, record = checkin
    persist = bridge.repository.record_failure
    monkeypatch.setattr(
        bridge.repository, "record_failure", AsyncMock(side_effect=RuntimeError("offline"))
    )
    with pytest.raises(RuntimeError):
        await approvals.handle(bridge, bridge.store, action(event_id, "report"), BackgroundTasks())
    state, _ = approvals.load_json(bridge.store, request_controls.key(event_id))
    assert state["report_pending"] and not state.get("reported")
    monkeypatch.setattr(bridge.repository, "record_failure", persist)
    await request_controls.sync(bridge, event_id, record)
    state, _ = approvals.load_json(bridge.store, request_controls.key(event_id))
    assert state["reported"] and not state["report_pending"]


@pytest.mark.parametrize(
    "changes",
    [
        {"user": {"id": "U2"}},
        {"channel": {"id": "D2"}},
        {"team": {"id": "T2"}},
        {"message": {"ts": "8.0"}},
    ],
)
async def test_other_people_or_cards_cannot_cancel(checkin, changes):
    bridge, event_id, _ = checkin
    with pytest.raises(ValueError):
        await approvals.handle(
            bridge, bridge.store, action(event_id, "cancel", **changes), BackgroundTasks()
        )
    assert not await request_controls.cancelled(bridge.store, event_id)


async def test_cancel_marker_prevents_launch_and_terminal_card_has_no_actions(checkin):
    bridge, event_id, record = checkin
    await approvals.handle(bridge, bridge.store, action(event_id, "cancel"), BackgroundTasks())
    assert await bridge.process(event_id, 0) == "failed"
    bridge.test_client.create_job.assert_not_called()
    record = await bridge.repository.get_connector_state("claude-event:" + event_id)
    assert record["stop_reason"] == "cancelled"
    await request_controls.sync(bridge, event_id, record)
    assert all(
        b["type"] != "actions" for b in bridge.test_slack.chat_update.call_args.kwargs["blocks"]
    )


async def test_more_than_three_routing_failures_still_retry(chat, monkeypatch):
    chat.settings.flexible_harness = True
    monkeypatch.setattr(request_controls, "sync", AsyncMock())
    chat.decision.side_effect = direct.RoutingUnavailable("temporary")
    event_id = await chat.accept(payload())
    for tick in range(5):
        record = await chat.repository.get_connector_state("claude-event:" + event_id)
        record["routing_retry_at"] = 0
        await chat.repository.set_connector_state("claude-event:" + event_id, record)
        assert await chat.process(event_id, tick) == "pending"
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    assert record["routing_failures"] == 5
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize(
    "error,reason", [("error_max_turns", "worker_effort"), ("error_max_budget_usd", "worker_spend")]
)
async def test_worker_limits_continue_saved_session_with_full_tools(
    setup, monkeypatch, error, reason
):
    ctx, settings = setup
    settings.flexible_harness = True
    calls = []

    async def query(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            from claude_agent_sdk import ResultMessage

            yield ResultMessage(
                subtype=error,
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=500,
                session_id="session-1",
                total_cost_usd=5.1,
            )
        else:
            assert kwargs["options"].resume == "session-1"
            assert kwargs["options"].tools == calls[0]["options"].tools
            assert kwargs["options"].max_budget_usd == pytest.approx(10.1)
            yield result(result="Completed", total_cost_usd=5.3)

    monkeypatch.setattr(runner, "query", query)
    attention = AsyncMock()
    output = await runner.Runner(settings, on_attention=attention).run_turn(
        ctx, "Analyze", AsyncMock()
    )
    assert output.error is None and output.text == "Completed" and output.cost == pytest.approx(5.3)
    attention.assert_awaited_once_with(reason)
    assert not output.summary_generated


def test_flexible_recovery_preserves_compute_deadline_but_not_model_cap():
    settings = runner.Settings(_env_file=None)
    record = {
        "flexible_harness": True,
        "request_deadline": 200,
        "model_limit": 5,
        "model_spent": 10,
    }
    assert recovery.limits(record, settings, now=100) == (100, 5)
    with pytest.raises(ValueError, match="approved compute"):
        recovery.limits(record, settings, now=201)


async def test_gpu_approval_discloses_and_binds_notification_policy(gpu):
    gpu.settings.flexible_harness = True
    event_id = await gpu.accept(payload())
    assert await gpu.process(event_id, 0) == "awaiting_approval"
    record = await gpu.repository.get_connector_state("claude-event:" + event_id)
    approval, _ = compute_approval.load(gpu.store, event_id)
    assert approval.flexible_harness
    assert "model usage check-in" in gpu.test_slack.chat_update.call_args.kwargs["text"]
    record["flexible_harness"] = False
    with pytest.raises(ValueError, match="fresh confirmation"):
        compute_approval.validate(gpu, gpu.store, record, approval)


async def test_pending_report_delivery_works_after_task_finishes(checkin, monkeypatch):
    bridge, event_id, record = checkin
    state, generation = approvals.load_json(bridge.store, request_controls.key(event_id))
    state["report_pending"] = True
    approvals.save(bridge.store, request_controls.key(event_id), state, generation)
    record["status"] = "complete"
    await bridge.repository.set_connector_state("claude-event:" + event_id, record)
    monkeypatch.setattr(observability, "flush", AsyncMock())
    await request_controls.expire(bridge, bridge.store, event_id)
    incident = await bridge.repository.request_failure(
        observability.request_scope(bridge.app), event_id
    )
    assert incident.observation.cause == "request_reported"
    assert not await request_controls.cancelled(bridge.store, event_id)


async def test_rejected_post_can_retry_without_duplicate_accepted_post(bridge, monkeypatch):
    from slack_sdk.errors import SlackApiError

    bridge.settings.flexible_harness = True
    monkeypatch.setattr(request_controls, "AsyncWebClient", lambda **kw: bridge.test_slack)
    event_id = await bridge.accept(payload())
    record = await bridge.repository.get_connector_state("claude-event:" + event_id)
    record["routing_calls"] = 6
    bridge.test_slack.chat_postMessage.side_effect = SlackApiError(
        "Rate limited", {"error": "ratelimited"}
    )
    with pytest.raises(SlackApiError):
        await request_controls.sync(bridge, event_id, record)
    bridge.test_slack.chat_postMessage.side_effect = None
    await request_controls.sync(bridge, event_id, record)
    state, _ = approvals.load_json(bridge.store, request_controls.key(event_id))
    assert state["ts"] == "9.0"
    bridge.test_slack.conversations_replies.assert_not_awaited()
