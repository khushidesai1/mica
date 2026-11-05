"""Failed user intent survives in the thread and the existing project-memory task."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from analysis_helpers import memory_edits
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import (
    access,
    conversation,
    job_lifecycle,
    memory_tasks,
    write_approval,
)

REQUEST = "Re-run TMA17–20 with AtoMx priors, using the same Proseg method and layout as TMA15."
NOTICE = "Choose output paths inside this run's write folder."
OPEN = (
    "type: open\nsource: alice@example.com, 2026-10-13\nstatus: current\n\n"
    "2026-10-13 alice@example.com: Proseg TMA17–20 with AtoMx priors, TMA15 layout; "
    "blocked at output-folder approval.\n"
)


async def fail(chat, monkeypatch, *, ts="1.0", thread=None, text=REQUEST):
    chat.decision.return_value = conversation.Decision(route="compute", reply="Running Proseg.")
    monkeypatch.setattr(write_approval, "ready", AsyncMock(side_effect=ValueError(NOTICE)))
    event = await chat.accept(payload(ts, thread, text))
    assert await chat.process(event, 0) == "failed"
    return event


async def checkpoint(chat, event):
    record = await chat.repository.get_connector_state("claude-event:" + event)
    return record, json.loads(chat.store.get(access.checkpoint_key(record["thread_key"]))[0])


async def test_failed_request_reaches_followup_router_once_with_original_intent(chat, monkeypatch):
    event = await fail(chat, monkeypatch)
    await chat.process(event, 0)
    record, state = await checkpoint(chat, event)
    assert state["messages"] == [
        {
            "id": event,
            "user": REQUEST,
            "assistant": NOTICE,
            "success": False,
            "files": [],
            "outputs": [],
            "methods": "",
        }
    ]
    assert record["memory_reply"] == NOTICE and record["terminal_recorded"]

    async def followup(_bridge, _event_id, _record, _storage, _grant, context, _files, **kwargs):
        previous = context["recent_messages"][-1]
        assert previous["user"] == REQUEST and not previous["success"]
        assert context["request"] == "then do this just for tma17"
        return conversation.Decision(route="compute", reply="Same Proseg/AtoMx method, TMA17 only.")

    chat.decision.side_effect = followup
    monkeypatch.setattr(write_approval, "ready", AsyncMock(return_value=True))
    second = await chat.accept(payload("2.0", "1.0", "then do this just for tma17"))
    assert await chat.process(second, 0) == "running"
    second_record, state = await checkpoint(chat, second)
    assert "TMA17 only" in second_record["direct_decision"]["reply"]
    assert len(state["messages"]) == 1


async def test_before_routing_failure_keeps_history_but_never_queues_memory(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    queued = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", queued)
    chat.decision.side_effect = httpx.ReadTimeout("private diagnostic")
    event = await chat.accept(payload(text=REQUEST))
    assert await chat.process(event, 0) == "failed"
    record, state = await checkpoint(chat, event)
    assert state["messages"][0]["user"] == REQUEST
    assert state["messages"][0]["success"] is False
    assert "private diagnostic" not in state["messages"][0]["assistant"]
    assert "memory_reply" not in record
    queued.assert_not_awaited()


async def test_history_write_failure_retries_terminal_hook_without_duplicate(chat, monkeypatch):
    event = await chat.accept(payload(text=REQUEST))
    chat.decision.return_value = conversation.Decision(route="compute", reply="Running.")
    monkeypatch.setattr(write_approval, "ready", AsyncMock(side_effect=ValueError(NOTICE)))
    put = chat.store.put
    failed = False

    def fail_once(key, data):
        nonlocal failed
        if key.endswith("checkpoint.json") and json.loads(data).get("messages") and not failed:
            failed = True
            raise OSError("temporary checkpoint error")
        return put(key, data)

    monkeypatch.setattr(chat.store, "put", fail_once)
    with pytest.raises(OSError):
        await chat.process(event, 0)
    assert await chat.process(event, 0) == "failed"
    assert await chat.process(event, 0) == "failed"
    assert len((await checkpoint(chat, event))[1]["messages"]) == 1


async def test_failed_user_message_is_not_truncated_and_working_window_retains_four(
    chat, monkeypatch
):
    text = REQUEST + " Detail " * 1000
    first = await fail(chat, monkeypatch, text=text)
    assert (await checkpoint(chat, first))[1]["messages"][0]["user"] == text
    for n in range(2, 15):
        latest = await fail(chat, monkeypatch, ts=f"{n}.0", thread="1.0")
    messages = (await checkpoint(chat, latest))[1]["messages"]
    assert len(messages) == 4 and all(m["id"] != first for m in messages)


@pytest.mark.parametrize("text", ["help", "cancel", "write grant", "memory"])
async def test_commands_never_add_history_or_memory_task(chat, monkeypatch, text):
    chat.settings.memory_auto_edit = True
    queued = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", queued)
    initial = await fail(chat, monkeypatch)
    before = (await checkpoint(chat, initial))[1]["messages"]
    queued.reset_mock()
    event = await chat.accept(payload("2.0", "1.0", text))
    await chat.process(event, 0)
    assert (await checkpoint(chat, event))[1]["messages"] == before
    queued.assert_not_awaited()


async def test_cancelled_pending_compute_never_adds_history_or_memory(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    queued = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", queued)
    chat.decision.return_value = conversation.Decision(route="compute", reply="Running.")

    async def awaiting(bridge, event, record, store):
        record.update(status="awaiting_approval", overwrite_approval=True)
        return False

    monkeypatch.setattr(write_approval, "ready", awaiting)
    event = await chat.accept(payload(text=REQUEST))
    assert await chat.process(event, 0) == "awaiting_approval"
    cancel = await chat.accept(payload("2.0", "1.0", "cancel"))
    await chat.process(cancel, 0)
    await chat.process(event, 1)
    assert (await checkpoint(chat, event))[1]["messages"] == []
    queued.assert_not_awaited()


async def test_routed_failure_uses_editor_not_missing_worker_proposal(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    queued = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", queued)
    editor = AsyncMock(return_value=memory_edits({"open.md": OPEN, "lessons.md": OPEN}))
    monkeypatch.setattr(memory_tasks, "edit", editor)
    event = await fail(chat, monkeypatch)
    assert queued.call_args.args[2].endswith("/memory/" + event)
    await memory_tasks.process(chat, event, chat.store)
    await memory_tasks.process(chat, event, chat.store)
    editor.assert_awaited_once()
    context = editor.call_args.args[2]
    assert context["reply"] == NOTICE and context["user"] == REQUEST
    assert context["success"] is False
    snapshot = await chat.memory_backend.read(chat.pid)
    assert snapshot.files["open.md"].text == OPEN and "lessons.md" not in snapshot.files


async def test_pending_line_updates_on_retry_and_disappears_on_success(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    editor = AsyncMock(return_value=memory_edits({"open.md": OPEN}))
    monkeypatch.setattr(memory_tasks, "edit", editor)
    for n in [1, 2]:
        event = await fail(chat, monkeypatch, ts=f"{n}.0", thread="1.0" if n > 1 else None)
        await memory_tasks.process(chat, event, chat.store)
    assert editor.call_args.args[2]["files"]["open.md"] == OPEN
    assert (await chat.memory_backend.read(chat.pid)).files["open.md"].text.count("Proseg TMA") == 1
    chat.decision.return_value = conversation.Decision(route="reply", reply="The retry completed.")
    editor.return_value = memory_edits(
        {"open.md": OPEN.split("\n\n")[0] + "\n\nNo pending work.\n"}
    )
    event = await chat.accept(payload("3.0", "1.0", "The retry is complete."))
    assert await chat.process(event, 0) == "complete"
    await memory_tasks.process(chat, event, chat.store)
    assert editor.call_args.args[2]["success"] is True
    assert "Proseg TMA" not in (await chat.memory_backend.read(chat.pid)).files["open.md"].text


async def test_growth_below_file_pressure_commits_without_compaction(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    queued = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", queued)
    expanded = OPEN + "Additional attributed pending work.\n" * 50
    monkeypatch.setattr(
        memory_tasks, "edit", AsyncMock(return_value=memory_edits({"open.md": expanded}))
    )
    event = await fail(chat, monkeypatch)
    await memory_tasks.process(chat, event, chat.store)
    await memory_tasks.process(chat, event, chat.store)
    assert (await chat.memory_backend.read(chat.pid)).files["open.md"].text == expanded
    compact = [c for c in queued.call_args_list if "/memory-compact/" in c.args[2]]
    assert not compact
