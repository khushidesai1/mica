"""Bounded routing repair and durable retries without model or cloud access."""

# ruff: noqa: F811
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import access, batch, direct
from mica.agents.analysis.direct import decide as real_decide
from mica.agents.analysis.registry import Registry


def response(value):
    return {"content": [{"type": "tool_use", "name": "route_request", "input": value}]}


def provider(monkeypatch, responses):
    requests = []
    replies = iter(responses)

    def respond(request):
        requests.append(json.loads(request.content))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    return requests


@pytest.mark.parametrize("repaired", [True, False])
async def test_schema_correction_does_not_repeat_invalid_values(monkeypatch, caplog, repaired):
    invalid = response({"route": "private-patient-value", "reply": "private reply"})
    valid = response({"route": "reply", "reply": "Ready."})
    requests = provider(monkeypatch, [invalid, valid if repaired else invalid])
    if repaired:
        assert (await direct.decide("fake", "test", {})).reply == "Ready."
    else:
        with pytest.raises(direct.RoutingUnavailable):
            await direct.decide("fake", "test", {})
    assert len(requests) == 2
    feedback = requests[1]["messages"][0]["content"][-1]["text"]
    assert "route" in feedback and "literal_error" in feedback
    assert "private" not in feedback + caplog.text


@pytest.mark.parametrize("field", ["workload", "reason"])
async def test_long_display_labels_validate_without_correction(monkeypatch, field):
    worker = {"category": "analysis", "reason": "Normal analysis.", field: "x" * 250}
    requests = provider(
        monkeypatch, [response({"route": "compute", "reply": "Plot", "worker": worker})]
    )
    result = await direct.decide("fake", "test", {})
    assert getattr(result.worker, field) == "x" * 200
    assert len(requests) == 1
    schema = requests[0]["tools"][0]["input_schema"]["$defs"]["WorkerChoice"]
    assert schema["properties"][field]["maxLength"] == 200
    assert schema["properties"]["reason"]["minLength"] == 5


@pytest.mark.parametrize("reason", ["", "ok", "    ", " a "])
def test_short_reason_is_display_metadata(reason):
    assert direct.WorkerChoice(category="analysis", reason=reason).reason == "unspecified"


@pytest.mark.parametrize(
    "worker",
    [
        {"category": "analysis"},
        {"category": "analysis", "reason": None},
        {"category": "analysis", "reason": 100},
        {"category": "analysis", "reason": "Normal", "workload": []},
        {"category": "unknown", "reason": "Normal"},
        {"category": "analysis", "tier": "arbitrary", "reason": "Normal"},
    ],
)
def test_missing_wrong_type_and_semantic_fields_stay_strict(worker):
    with pytest.raises(ValidationError):
        direct.WorkerChoice.model_validate(worker)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"content": {}},
        {"content": [None]},
        {"content": ["private"]},
        {"content": [{"type": "tool_use", "name": "route_request"}]},
        {"content": [{"type": "tool_use", "name": "unknown"}]},
        {"content": response({"route": "reply", "reply": "Hi"})["content"] * 2},
        {
            "content": response({"route": "reply", "reply": "Hi"})["content"]
            + [{"type": "tool_use", "name": "unknown"}]
        },
        {"content": [{"type": {"private": "value"}}]},
    ],
)
async def test_malformed_responses_fail_with_bounded_recovery(monkeypatch, body):
    requests = provider(monkeypatch, [body, body])
    with pytest.raises(direct.RoutingUnavailable):
        await direct.decide("fake", "test", {"data_inventory": {"complete": True}})
    assert 1 <= len(requests) <= 2


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(408),
        httpx.Response(409),
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(503),
        httpx.ReadTimeout("private error"),
    ],
)
async def test_transient_provider_errors_are_retryable(monkeypatch, reply):
    requests = provider(monkeypatch, [reply])
    with pytest.raises(direct.RoutingUnavailable):
        await direct.decide("fake", "test", {})
    assert len(requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_permanent_provider_errors_do_not_consume_routing_retries(monkeypatch, status):
    requests = provider(monkeypatch, [httpx.Response(status)])
    with pytest.raises(httpx.HTTPStatusError):
        await direct.decide("fake", "test", {})
    assert len(requests) == 1


@pytest.mark.parametrize("media_first", [True, False])
async def test_media_fallback_and_correction_share_three_call_limit(monkeypatch, media_first):
    invalid = response({"route": "unknown", "reply": "Hi"})
    bad_media = httpx.Response(400)
    requests = provider(
        monkeypatch,
        [
            *([bad_media, invalid] if media_first else [invalid, bad_media]),
            response({"route": "reply", "reply": "Ready"}),
        ],
    )
    result = await direct.decide(
        "fake",
        "test",
        {"attachments": [{"head": "private", "shown": True}]},
        media=[{"type": "image"}],
    )
    assert result.reply == "Ready" and len(requests) == 3
    content = requests[-1]["messages"][0]["content"]
    assert all(block["type"] == "text" for block in content)
    assert "private" not in json.dumps(content)
    assert "invalid" in content[-1]["text"]


async def test_shared_deadline_is_not_reset_by_a_followup(monkeypatch):
    calls = []

    async def respond(request):
        calls.append(request)
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    # Advancing the timeout deadline to the next event-loop turn avoids a real wait.
    timeout_at = asyncio.timeout_at
    deadlines = []

    def expire(deadline):
        deadlines.append(deadline)
        return timeout_at(asyncio.get_running_loop().time())

    monkeypatch.setattr(direct.asyncio, "timeout_at", expire)
    deadline = asyncio.get_running_loop().time() + 120
    with pytest.raises(direct.RoutingUnavailable, match="budget"):
        await direct.decide("fake", "test", {}, deadline=deadline)
    assert deadlines == [deadline] and len(calls) == 1
    with pytest.raises(direct.RoutingUnavailable, match="budget"):
        await direct.decide("fake", "test", {}, deadline=0)
    assert len(calls) == 1


async def receipt(chat, event):
    return await chat.repository.get_connector_state("claude-event:" + event)


async def test_archive_never_turns_invalid_routing_into_compute(chat, monkeypatch):
    first = await chat.accept(payload())
    assert await chat.process(first, 0) == "complete"
    record = await receipt(chat, first)
    key = access.checkpoint_key(record["thread_key"])
    state = json.loads(chat.store.get(key)[0])
    state["archive"] = "existing/checkpoint.tar.gz"
    chat.store.put(key, json.dumps(state).encode())
    provider(monkeypatch, [{"content": []}, {"content": []}])
    monkeypatch.setattr(direct, "decide", real_decide)
    followup = await chat.accept(payload("2.0", "1.0", "Explain the earlier results"))
    assert await chat.process(followup, 0) == "pending"
    assert (await receipt(chat, followup))["routing_failures"] == 1
    chat.test_client.create_job.assert_not_called()


async def test_retry_backoff_survives_restart_and_exhausts_once(chat, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = direct.RoutingUnavailable("private error")
    event = await chat.accept(payload(text="Please plot this"))
    assert await chat.process(event, 0) == "pending"
    record = await receipt(chat, event)
    assert record["routing_failures"] == 1 and record["routing_retry_at"] == 130
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
        direct.Decision(route="reply", reply="Recovered"),
        direct.Decision(route="reply", reply="Followup"),
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
    assert await chat.process(event, 1) == "complete"
    assert chat.decision.await_count == 1
    memory.assert_not_awaited()
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("route", ["view", "inventory"])
async def test_second_stage_failure_retries_safely_with_shared_deadline(chat, monkeypatch, route):
    monkeypatch.setattr(direct, "inventory", Mock(return_value={"complete": True, "entries": []}))
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = [
        direct.Decision(route=route, reply="Checking"),
        direct.RoutingUnavailable(),
        direct.Decision(route="reply", reply="Recovered"),
    ]
    event = await chat.accept(payload())
    assert await chat.process(event, 0) == "pending"
    first, second = chat.decision.await_args_list
    assert first.kwargs["deadline"] == second.kwargs["deadline"]
    assert (await receipt(chat, event))["direct_decision_pending"]
    now[0] = 130
    assert await chat.process(event, 1) == "complete"
    assert not (await receipt(chat, event))["direct_decision_pending"]
    chat.test_client.create_job.assert_not_called()


async def test_escaped_validation_error_is_generic_but_user_errors_remain_useful(chat):
    with pytest.raises(ValidationError) as invalid:
        direct.Decision.model_validate({"route": "private", "reply": "Hi"})
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
