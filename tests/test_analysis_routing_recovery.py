"""Bounded routing repair and durable retries without model or cloud access."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import access, batch, conversation, direct, tiers
from mica.agents.analysis.registry import Registry


@pytest.mark.parametrize("reason", ["", "ok", "    ", " a "])
def test_short_reason_is_display_metadata(reason):
    assert tiers.WorkerChoice(category="analysis", reason=reason).reason == "unspecified"


@pytest.mark.parametrize(
    "worker",
    [
        {"category": "analysis"},
        {"category": "analysis", "reason": None},
        {"category": "analysis", "reason": 100},
        {"category": "analysis", "reason": "Normal", "workload": []},
        {"expected_hours": float("nan"), "reason": "Normal"},
        {"category": "analysis", "tier": "arbitrary", "reason": "Normal"},
    ],
)
def test_missing_wrong_type_and_semantic_fields_stay_strict(worker):
    with pytest.raises(ValidationError):
        tiers.WorkerChoice.model_validate(worker)


async def receipt(chat, event):
    return await chat.repository.get_connector_state("claude-event:" + event)


async def test_retry_backoff_survives_restart_and_exhausts_once(chat, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = direct.RoutingUnavailable("private error")
    event = await chat.accept(payload(text="Please plot this"))
    assert await chat.process(event, 0) == "pending"
    record = await receipt(chat, event)
    assert record["routing_failures"] == 1 and record["routing_retry_at"] == 130
    assert record["project_id"] == chat.pid
    assert record["principal"] == "alice@example.com"
    assert "retry automatically" in record["notice_text"]
    restarted = batch.BatchBridge(chat.app, chat.repository)
    now[0] = 129
    assert await restarted.process(event, 0) == "pending"  # duplicate delivery
    assert await restarted.process(event, 1) == "pending"  # early current tick
    assert chat.decision.await_count == 1
    now[0] = 130
    assert await restarted.process(event, 2) == "pending"
    record = await receipt(chat, event)
    assert record["routing_failures"] == 2 and record["routing_retry_at"] == 190
    now[0] = 189
    assert await restarted.process(event, 3) == "pending"
    assert chat.decision.await_count == 2
    now[0] = 190
    assert await restarted.process(event, 4) == "failed"
    assert await restarted.process(event, 4) == "failed"
    record = await receipt(chat, event)
    assert record["routing_failures"] == 3 and chat.decision.await_count == 3
    assert (
        "private" not in record["notice_text"] and "validation error" not in record["notice_text"]
    )
    state = json.loads(chat.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert len(state["messages"]) == 1 and not state["messages"][0]["success"]
    chat.test_client.create_job.assert_not_called()


async def test_recovery_releases_queued_turn_after_success(chat, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = [
        direct.RoutingUnavailable(),
        conversation.Decision(route="reply", reply="Recovered"),
        conversation.Decision(route="reply", reply="Followup"),
    ]
    event = await chat.accept(payload())
    assert await chat.process(event, 0) == "pending"
    queued = await chat.accept(payload("2.0", "1.0", "Next question"))
    assert await chat.process(queued, 0) == "pending"
    assert chat.decision.await_count == 1
    now[0] = 130
    assert await chat.process(event, 1) == "complete"
    assert await chat.process(event, 1) == "complete"
    assert await chat.process(queued, 1) == "complete"
    assert chat.decision.await_count == 3
    chat.test_client.create_job.assert_not_called()


async def test_cancellation_during_backoff_never_retries(chat, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = direct.RoutingUnavailable()
    event = await chat.accept(payload())
    assert await chat.process(event, 0) == "pending"
    cancel = await chat.accept(payload("2.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "complete"
    now[0] = 200
    assert await chat.process(event, 1) == "failed"
    assert chat.decision.await_count == 1
    chat.test_client.create_job.assert_not_called()


async def test_access_revocation_prevents_retry_model_and_project_reads(chat, monkeypatch):
    registry = Registry(chat.store)
    registry.update(
        "share",
        "alice@example.com",
        "share",
        lambda d: d.share(chat.pid, "alice@example.com", "bob@example.com"),
    )
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = direct.RoutingUnavailable()
    registry.update(
        "bob-default", "bob@example.com", "use",
        lambda d: d.bind("T1", "D2", "bob@example.com", chat.pid, private=True) or chat.pid,
    )
    event = await chat.accept(payload(user="U2", channel="D2"))
    assert await chat.process(event, 0) == "pending"
    registry.update(
        "unshare",
        "alice@example.com",
        "unshare",
        lambda d: d.share(chat.pid, "alice@example.com", "bob@example.com", remove=True),
    )
    memory = AsyncMock(wraps=chat.memory_backend.read)
    monkeypatch.setattr(chat.memory_backend, "read", memory)
    now[0] = 130
    chat.decision.side_effect = [conversation.Decision(route="reply", reply="Access is required.")]
    assert await chat.process(event, 1) == "complete"
    assert chat.decision.await_count == 2
    assert not chat.decision.call_args.args[4].allowed
    assert set(chat.decision.call_args.args[5]) == {"request"}
    memory.assert_not_awaited()
    chat.test_client.create_job.assert_not_called()


async def test_escaped_validation_error_is_generic_but_user_errors_remain_useful(chat):
    with pytest.raises(ValidationError) as invalid:
        conversation.Decision.model_validate({"route": "private", "reply": "Hi"})
    chat.decision.side_effect = invalid.value
    event = await chat.accept(payload())
    assert await chat.process(event, 0) == "failed"
    text = (await receipt(chat, event))["notice_text"]
    assert "internal issue" in text and "private" not in text and "validation error" not in text
    chat.decision.side_effect = ValueError("Choose a destination first.")
    other = await chat.accept(payload("2.0"))
    assert await chat.process(other, 0) == "failed"
    assert "Choose a destination first." in (await receipt(chat, other))["notice_text"]
    chat.test_client.create_job.assert_not_called()
