"""The memory model runs in a retryable task, outside turn/worker leases."""

# ruff: noqa: F811
import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import job_lifecycle, memory_tasks


async def test_delivery_queues_but_does_not_run_editor(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    enqueue = AsyncMock()
    editor = AsyncMock(return_value={})
    monkeypatch.setattr(job_lifecycle, "enqueue", enqueue)
    monkeypatch.setattr(memory_tasks, "edit", editor)
    event = await chat.accept(payload(text="Explain this result"))
    await chat.process(event, 0)
    editor.assert_not_awaited()
    assert enqueue.call_args.args[2].endswith("/memory/" + event)
    await memory_tasks.process(chat, event, chat.store)
    await memory_tasks.process(chat, event, chat.store)
    editor.assert_awaited_once()


async def test_failure_logs_type_only_and_retries(chat, monkeypatch, caplog):
    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    editor = AsyncMock(side_effect=httpx.ReadTimeout("PRIVATE CONTENT"))
    monkeypatch.setattr(memory_tasks, "edit", editor)
    event = await chat.accept(payload(text="Explain this result"))
    await chat.process(event, 0)
    with caplog.at_level(logging.WARNING), pytest.raises(httpx.ReadTimeout):
        await memory_tasks.process(chat, event, chat.store)
    assert "ReadTimeout" in caplog.text and "PRIVATE CONTENT" not in caplog.text
    editor.side_effect = None
    editor.return_value = {}
    await memory_tasks.process(chat, event, chat.store)
    assert editor.await_count == 2


async def test_editor_has_time_for_45_second_response(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    timeout = asyncio.timeout
    budgets = []

    def capture(seconds):
        budgets.append(seconds)
        return timeout(seconds)

    async def slow_model(*args):
        # Simulate elapsed model time without adding 45 s to the offline suite.
        assert budgets[-1] > 45
        await asyncio.sleep(0)
        return {}

    monkeypatch.setattr(memory_tasks.asyncio, "timeout", capture)
    monkeypatch.setattr(memory_tasks, "edit", slow_model)
    event = await chat.accept(payload(text="Explain this result"))
    await chat.process(event, 0)
    await memory_tasks.process(chat, event, chat.store)
    assert 150 in budgets


async def test_worker_slot_released_before_memory_enqueue(bridge, monkeypatch):
    import json

    from mica.agents.analysis import access

    bridge.settings.memory_auto_edit = True
    editor = AsyncMock()
    monkeypatch.setattr(memory_tasks, "edit", editor)
    event = await bridge.accept(payload(text="Plot data"))
    await bridge.process(event, 0)
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    state_key = access.checkpoint_key(record["thread_key"])
    state = json.loads(bridge.store.get(state_key)[0])
    state["messages"] = [{"id": event, "assistant": "Done", "success": True}]
    bridge.store.put(state_key, json.dumps(state).encode())
    bridge.test_states[next(iter(bridge.test_states))] = "SUCCEEDED"
    bridge.store.put(
        f"tasks/slack-analysis/events/{event}/attempt-a0.json",
        json.dumps({"event_id": event, "attempt": 0, "outcome": "complete"}).encode(),
    )

    async def enqueue(*args):
        assert not any(k.startswith("claude-slot:") for k in bridge.repository.event_claims)
        editor.assert_not_awaited()

    queued = AsyncMock(side_effect=enqueue)
    monkeypatch.setattr(job_lifecycle, "enqueue", queued)
    await bridge.process(event, 1)
    queued.assert_awaited_once()


async def test_http_timeout_is_90_seconds(monkeypatch):
    import json

    budgets = []
    factory = httpx.AsyncClient

    def client(**kwargs):
        budgets.append(kwargs["timeout"])

        def respond(request):
            assert json.loads(request.content)["max_tokens"] == 8192
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "edit_project_memory",
                            "input": {"changes": {}},
                        }
                    ]
                },
            )

        return factory(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(memory_tasks.httpx, "AsyncClient", client)
    await memory_tasks.edit("test", "model", {})
    assert budgets == [90]
