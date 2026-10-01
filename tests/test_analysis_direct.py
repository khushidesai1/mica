import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401

from pistachio.agents.analysis import batch, direct
from pistachio.agents.analysis.projects import Projects


@pytest.fixture
def chat(bridge, monkeypatch):  # noqa: F811
    bridge.settings.direct_chat_enabled = True
    bridge.settings.storage = "local"
    store = batch.GCSStorage(bridge.app.gcs_bucket)
    projects = Projects(store, bridge.settings)
    projects.bind("D1", "liver", "gs://lab-data/liver/", "U1")
    projects.save_brief("liver", b"# Project\nSpecies: TODO")
    bridge.test_slack.conversations_replies.return_value = {"messages": []}
    monkeypatch.setattr(direct, "AsyncWebClient", lambda **kw: bridge.test_slack)
    bridge.decision = AsyncMock(
        return_value=direct.Decision(
            route="reply", reply="Updated.", draft="# Project\nSpecies: human (user provided)"
        )
    )
    monkeypatch.setattr(direct, "decide", bridge.decision)
    bridge.store, bridge.projects = store, projects
    return bridge


async def test_direct_edits_save_and_followups_never_create_workers(chat):
    first = await chat.accept(payload(text="These are human samples"))
    assert await chat.process(first, 0) == "complete"
    assert await chat.process(first, 0) == "complete"
    chat.decision.assert_awaited_once()
    record = await chat.repository.get_connector_state("claude-event:" + first)
    pointer = f"projects/_threads/{record['thread_key']}/checkpoint.json"
    state = json.loads(chat.store.get(pointer)[0])
    assert "human" in state["context"]["project_md"]
    assert "TODO" in chat.projects.brief("liver")[0]
    for i, command in enumerate(["brief", "save brief", "help", "cancel"], start=2):
        event = await chat.accept(payload(f"{i}.0", "1.0", command))
        assert await chat.process(event, 0) == "complete"
    assert "human" in chat.projects.brief("liver")[0]
    assert chat.decision.await_count == 1
    chat.test_client.create_job.assert_not_called()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


async def test_compute_uses_direct_brief_and_cached_routing(chat):
    first = await chat.accept(payload(text="These are human samples"))
    await chat.process(first, 0)
    chat.decision.return_value = direct.Decision(
        route="compute", reply="Plotting needs data access."
    )
    compute = await chat.accept(payload("2.0", "1.0", "Plot the spatial layout"))
    assert await chat.process(compute, 0) == "running"
    assert await chat.process(compute, 1) == "running"
    assert chat.test_client.create_job.call_count == 1
    assert chat.decision.await_count == 2
    supplied = chat.decision.call_args.args[2]
    assert "human" in supplied["brief"]
    assert supplied["recent_messages"][0]["user"] == "These are human samples"
    record = await chat.repository.get_connector_state("claude-event:" + compute)
    assert record["route"] == "compute" and record["mount_buckets"] == ["lab-data"]


async def test_direct_init_and_invalid_bucket(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *a: {"entries": [], "content_inspected": False})
    init = await chat.accept(payload(text="init liver gs://lab-data/liver/"))
    assert await chat.process(init, 0) == "complete"
    assert "init_inventory" in chat.decision.call_args.args[2]
    chat.test_client.create_job.assert_not_called()
    bad = await chat.accept(payload("2.0", text="init other gs://outside-project/"))
    assert await chat.process(bad, 0) == "failed"
    assert chat.decision.await_count == 1
    chat.test_client.create_job.assert_not_called()


async def test_direct_failure_does_not_fall_back_to_compute(chat):
    chat.decision.side_effect = httpx.ReadTimeout("timeout")
    event = await chat.accept(payload(text="Please revise my brief"))
    assert await chat.process(event, 0) == "failed"
    chat.test_client.create_job.assert_not_called()
    assert "TODO" in chat.projects.brief("liver")[0]


async def test_retry_after_state_write_reuses_decision(chat, monkeypatch):
    original = chat.store.put
    failed = False

    def put(key, data):
        nonlocal failed
        original(key, data)
        if key.endswith("checkpoint.json") and not failed:
            failed = True
            raise RuntimeError("response lost")

    monkeypatch.setattr(chat.store, "put", put)
    event = await chat.accept(payload(text="These are human samples"))
    with pytest.raises(RuntimeError, match="response lost"):
        await chat.process(event, 0)
    assert await chat.process(event, 0) == "complete"
    chat.decision.assert_awaited_once()
    chat.test_client.create_job.assert_not_called()


async def test_cancel_bypasses_queue_and_releases_slot_only_after_job_ends(chat):
    chat.decision.return_value = direct.Decision(route="compute", reply="Needs computation.")
    first = await chat.accept(payload(text="Plot data"))
    assert await chat.process(first, 0) == "running"
    queued = await chat.accept(payload("2.0", "1.0", "Change the plot colors"))
    cancel = await chat.accept(payload("3.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    chat.test_client.delete_job.assert_called_once()
    assert (await chat.repository.get_connector_state("claude-event:" + queued))[
        "status"
    ] == "failed"
    assert any(k.startswith("claude-slot") for k in chat.repository.event_claims)
    del chat.test_states[next(iter(chat.test_states))]
    assert await chat.process(first, 1) == "failed"
    assert await chat.process(cancel, 1) == "complete"
    chat.test_client.create_job.assert_called_once()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


async def test_optional_file_rejects_external_url_without_worker(chat):
    body = payload(text="brief")
    body["event"]["files"] = [
        {"name": "PROJECT.md", "url_private_download": "https://evil.example/x"}
    ]
    event = await chat.accept(body)
    assert await chat.process(event, 0) == "failed"
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_router_rejects_truncated_output(monkeypatch):
    def respond(request):
        return httpx.Response(200, json={"stop_reason": "max_tokens", "content": []})

    transport = httpx.MockTransport(respond)
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    with pytest.raises(ValueError, match="too long"):
        await direct.decide("fake-key", "test-model", {"request": "hi"})


async def test_reply_delivery_retry_does_not_repeat_model_or_create_worker(chat):
    original = chat.test_slack.chat_update
    failed = False

    async def update(**kwargs):
        nonlocal failed
        if kwargs["text"] == "Updated." and not failed:
            failed = True
            raise RuntimeError("Slack unavailable")
        return {"ts": kwargs["ts"]}

    original.side_effect = update
    event = await chat.accept(payload(text="These are human samples"))
    with pytest.raises(RuntimeError, match="delivery failed"):
        await chat.process(event, 0)
    assert await chat.process(event, 0) == "complete"
    chat.decision.assert_awaited_once()
    chat.test_client.create_job.assert_not_called()


async def test_cancel_during_interrupted_submission_never_submits_later(chat):
    chat.decision.return_value = direct.Decision(route="compute", reply="Needs computation.")
    chat.test_client.create_job.side_effect = RuntimeError("submission interrupted")
    first = await chat.accept(payload(text="Plot data"))
    with pytest.raises(RuntimeError, match="submission interrupted"):
        await chat.process(first, 0)
    cancel = await chat.accept(payload("2.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    assert await chat.process(first, 0) == "failed"
    assert await chat.process(cancel, 1) == "complete"
    chat.test_client.create_job.assert_called_once()
