"""Bounded maintenance never loses originals or repeats invalid model spending."""

# ruff: noqa: F811
import asyncio
import json
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import job_lifecycle, memory_archive, project_memory
from mica.agents.analysis import memory_compaction as compact
from mica.agents.analysis.config import CHECKPOINT_PREFIX

NOTE = "type: fact\nsource: Alice, 2026-10-10\nstatus: current\n\nUse viridis.\n"


async def seed(chat):
    chat.settings.memory_auto_edit = True
    chat.github.seed(
        {
            f"projects/{chat.pid}/PROJECT.md": "x" * 85000,
            f"projects/{chat.pid}/data/fact.md": NOTE,
            f"projects/{chat.pid}/_unmerged/abc/data/fact.md": NOTE + "A different fact.\n",
        }
    )
    return await chat.memory_backend.read(chat.pid)


def edits():
    return {"data/fact.md": NOTE + "A different fact.\n", "_unmerged/abc/data/fact.md": None}


async def test_coalesced_task_merges_one_set_and_preserves_originals(chat, monkeypatch):
    base = await seed(chat)
    dispatch = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", dispatch)
    await compact.enqueue(chat, chat.pid, project_memory.Saved())
    await compact.enqueue(chat, chat.pid, project_memory.Saved())
    dispatch.assert_awaited_once()
    model = AsyncMock(return_value=edits())
    monkeypatch.setattr(compact, "compact", model)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    model.assert_awaited_once()
    latest = await chat.memory_backend.read(chat.pid)
    assert latest.files["PROJECT.md"].text == base.files["PROJECT.md"].text
    assert latest.files["data/fact.md"].text == edits()["data/fact.md"]
    for path in ("data/fact.md", "_unmerged/abc/data/fact.md"):
        archived = memory_archive.archive_path(path, base.files[path].text)
        assert latest.files[archived].text == base.files[path].text
    assert "_unmerged/abc/data/fact.md" not in latest.files
    assert "PROJECT.md" not in model.call_args.args[2]["files"]  # Oversized group not truncated.
    chat.test_slack.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize(
    "invalid",
    [
        {"data/fact.md": "missing headers"},
        {"INDEX.md": "invented index"},
        {"PROJECT.md": "overwrite outside selected set"},
        {"recipes/unseen.py": "destroy"},
    ],
)
async def test_invalid_output_cached_and_terminal_without_partial_commit(
    chat, monkeypatch, invalid
):
    base = await seed(chat)
    model = AsyncMock(return_value=invalid)
    monkeypatch.setattr(compact, "compact", model)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    model.assert_awaited_once()
    assert chat.github.head == base.head_oid
    key = f"{CHECKPOINT_PREFIX}/memory-compaction-v2/{chat.pid}/{compact.fingerprint(base)}.json"
    cache = json.loads(chat.store.get(key)[0])
    assert cache["done"] and cache["changes"] == invalid
    assert cache["outcome"].startswith("exhausted:")


@pytest.mark.parametrize("failure", [ValueError("incomplete_model_output"), TimeoutError()])
async def test_model_parse_and_transport_failures_are_bounded(chat, monkeypatch, failure):
    base = await seed(chat)
    model = AsyncMock(side_effect=failure)
    monkeypatch.setattr(compact, "compact", model)
    for _ in range(4):
        with suppress(TimeoutError):
            await compact.process(chat, chat.pid, base.head_oid, chat.store)
    assert model.await_count == (1 if isinstance(failure, ValueError) else 2)
    state = await chat.repository.get_connector_state(compact.state_key(chat.pid))
    assert state["outcome"] == "already_complete"
    assert chat.github.head == base.head_oid


async def test_stale_head_is_checked_before_model_and_newer_turn_survives(chat, monkeypatch):
    base = await seed(chat)
    chat.github.seed({f"projects/{chat.pid}/PROJECT.md": "New fact"})
    model = AsyncMock(return_value=edits())
    monkeypatch.setattr(compact, "compact", model)
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    model.assert_not_awaited()
    assert (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"].text == "New fact"


async def test_other_project_commit_does_not_repeat_cached_model(chat, monkeypatch):
    base = await seed(chat)
    model = AsyncMock(side_effect=[TimeoutError(), edits()])
    monkeypatch.setattr(compact, "compact", model)
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    with pytest.raises(TimeoutError):
        await compact.process(chat, chat.pid, base.head_oid, chat.store)
    chat.github.seed({"projects/another/PROJECT.md": "Unrelated change"})
    latest = await chat.memory_backend.read(chat.pid)
    await compact.process(chat, chat.pid, latest.head_oid, chat.store)
    assert model.call_args_list[0] == model.call_args_list[1]
    assert (await chat.memory_backend.read(chat.pid)).files["data/fact.md"].text == edits()[
        "data/fact.md"
    ]


async def test_parallel_heads_share_project_lease(chat, monkeypatch):
    base = await seed(chat)
    started, release = asyncio.Event(), asyncio.Event()

    async def model(*args):
        started.set()
        await release.wait()
        return edits()

    mocked = AsyncMock(side_effect=model)
    monkeypatch.setattr(compact, "compact", mocked)
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    task = asyncio.create_task(compact.process(chat, chat.pid, base.head_oid, chat.store))
    await started.wait()
    await compact.process(chat, chat.pid, "b" * 40, chat.store)
    assert mocked.await_count == 1
    release.set()
    await task


async def test_different_heads_cannot_reset_project_call_budget(chat, monkeypatch):
    await seed(chat)
    now = [10000.0]
    monkeypatch.setattr(compact.time, "time", lambda: now[0])
    dispatch = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", dispatch)
    model = AsyncMock(return_value={"unseen.md": "invalid"})
    monkeypatch.setattr(compact, "compact", model)
    for n in range(8):
        chat.github.seed({f"projects/{chat.pid}/data/fact.md": NOTE + str(n)})
        latest = await chat.memory_backend.read(chat.pid)
        await compact.enqueue(chat, chat.pid, project_memory.Saved())
        # Calls arrive at/after the coalesced cooldown, still in the same hourly window.
        now[0] += 61
        await compact.process(chat, chat.pid, latest.head_oid, chat.store)
    assert model.await_count <= chat.settings.memory_compaction_calls_per_hour
    state = await chat.repository.get_connector_state(compact.state_key(chat.pid))
    assert state["calls"] <= chat.settings.memory_compaction_calls_per_hour
    assert dispatch.await_count <= 4


def test_working_set_pages_conflicts_and_budgets_serialized_unicode():
    from mica.agents.analysis.config import Settings

    settings = Settings(_env_file=None, memory_compaction_chars=2500)
    base = project_memory.Snapshot(
        head_oid="a" * 40,
        files={
            f"_unmerged/e{i}/data/fact.md": project_memory.File(text=NOTE + "λ" * 150, oid=str(i))
            for i in range(40)
        },
    )
    first, cursor = compact.working_set(base, settings)
    second, _ = compact.working_set(base, settings, cursor)
    assert first["files"] and second["files"] and first != second
    assert len(json.dumps(first, ensure_ascii=True)) <= settings.memory_compaction_chars
    assert len(json.dumps(second, ensure_ascii=True)) <= settings.memory_compaction_chars


async def test_three_call_hourly_budget_survives_new_fingerprints(chat, monkeypatch):
    await seed(chat)
    now = [10000.0]
    monkeypatch.setattr(compact.time, "time", lambda: now[0])
    dispatch = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", dispatch)
    model = AsyncMock(return_value={"unseen.md": "invalid"})
    monkeypatch.setattr(compact, "compact", model)
    for n in range(4):
        chat.github.seed({f"projects/{chat.pid}/data/fact.md": NOTE + str(n)})
        latest = await chat.memory_backend.read(chat.pid)
        await compact.process(chat, chat.pid, latest.head_oid, chat.store)
        now[0] += 601
    assert model.await_count == 3
    state = await chat.repository.get_connector_state(compact.state_key(chat.pid))
    assert state["outcome"] == "deferred:project_call_budget"
    assert state["pending"]["delay"] == 1797
    now[0] = 13601.0
    await compact.process(chat, chat.pid, latest.head_oid, chat.store)
    assert model.await_count == 4
    state = await chat.repository.get_connector_state(compact.state_key(chat.pid))
    assert state["calls"] == 1


async def test_crashed_model_attempts_do_not_restart_spending(chat, monkeypatch):
    base = await seed(chat)
    context, _ = compact.working_set(base, chat.settings)
    key = f"{CHECKPOINT_PREFIX}/memory-compaction-v2/{chat.pid}/{compact.fingerprint(base)}.json"
    chat.store.put(
        key, json.dumps({"base": base.model_dump(), "context": context, "attempts": 2}).encode()
    )
    model = AsyncMock()
    monkeypatch.setattr(compact, "compact", model)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    model.assert_not_awaited()
    assert json.loads(chat.store.get(key)[0])["done"]


async def test_invalid_repository_entry_is_quarantined_without_model_call(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    chat.github.seed({f"projects/{chat.pid}/data/invalid.md": "missing headers"})
    base = await chat.memory_backend.read(chat.pid)
    model = AsyncMock()
    monkeypatch.setattr(compact, "compact", model)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    await compact.process(chat, chat.pid, base.head_oid, chat.store)
    model.assert_not_awaited()
    assert (await chat.memory_backend.read(chat.pid)).invalid == base.invalid


async def test_unavailable_repository_input_stops_after_two_attempts(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(chat.memory_backend, "read", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="Memory input unavailable"):
        await compact.process(chat, chat.pid, "a" * 40, chat.store)
    await compact.process(chat, chat.pid, "a" * 40, chat.store)
    state = await chat.repository.get_connector_state(compact.state_key(chat.pid))
    assert state["outcome"] == "exhausted:unavailable_input"
