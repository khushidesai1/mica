"""Compaction is atomic, deduplicated, and retains immutable retry inputs."""
# ruff: noqa: F811

from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import job_lifecycle, memory_compaction, project_memory

NOTE = "type: fact\nsource: Alice, 2026-10-10\nstatus: current\n\nUse viridis.\n"


async def seed(chat):
    chat.settings.memory_auto_edit = True
    chat.github.seed(
        {
            f"projects/{chat.pid}/PROJECT.md": "x" * 85_000,
            f"projects/{chat.pid}/data/fact.md": NOTE,
            f"projects/{chat.pid}/_unmerged/abc/data/fact.md": NOTE,
        }
    )
    return await chat.memory_backend.read(chat.pid)


def edits():
    return {
        "PROJECT.md": "Concise guide",
        "_unmerged/abc/data/fact.md": None,
        "INDEX.md": "- [Guide](PROJECT.md)\n- [Fact](data/fact.md)\n",
    }


async def test_threshold_queues_once_then_merges_without_loss(chat, monkeypatch):
    base = await seed(chat)
    enqueue = AsyncMock()
    monkeypatch.setattr(job_lifecycle, "enqueue", enqueue)
    await memory_compaction.enqueue(chat, chat.pid, project_memory.Saved())
    await memory_compaction.enqueue(chat, chat.pid, project_memory.Saved())
    enqueue.assert_awaited_once()
    assert enqueue.call_args.args[1] == f"memory-compact-{chat.pid}-{base.head_oid[:7]}"
    model = AsyncMock(return_value=edits())
    monkeypatch.setattr(memory_compaction, "compact", model)
    await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    model.assert_awaited_once()
    latest = await chat.memory_backend.read(chat.pid)
    assert latest.files["data/fact.md"].text == NOTE
    assert memory_compaction.utilization(latest, chat.settings) <= 0.7
    assert not any(p.startswith("_unmerged/") for p in latest.files)
    assert chat.github.commits[0]["messageHeadline"].startswith(f"memory({chat.pid}): compaction")
    chat.test_slack.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize(
    "invalid",
    [
        {"PROJECT.md": "Good", "data/new.md": "Missing headers"},
        {"PROJECT.md": "Good", "INDEX.md": "- [Lost](missing.md)"},
        {"PROJECT.md": "x" * 75_000},
    ],
)
async def test_invalid_output_never_partially_commits(chat, monkeypatch, invalid):
    base = await seed(chat)
    monkeypatch.setattr(memory_compaction, "compact", AsyncMock(return_value=invalid))
    with pytest.raises(ValueError):
        await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    assert chat.github.head == base.head_oid


async def test_retry_has_identical_input_and_does_not_overwrite_new_turn(chat, monkeypatch):
    base = await seed(chat)
    model = AsyncMock(side_effect=[TimeoutError(), edits()])
    monkeypatch.setattr(memory_compaction, "compact", model)
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    with pytest.raises(TimeoutError):
        await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    chat.github.seed({f"projects/{chat.pid}/PROJECT.md": "New fact"})
    head = chat.github.head
    await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    assert model.call_args_list[0] == model.call_args_list[1]
    assert chat.github.head == head
    assert (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"].text == "New fact"


async def test_quota_reductions_can_commit_when_folder_starts_over_limit(chat):
    chat.settings.memory_max_files = 2
    chat.github.seed({f"projects/{chat.pid}/data/{n}.md": NOTE for n in range(4)})
    base = await chat.memory_backend.read(chat.pid)
    saved = await chat.memory_backend.commit(
        chat.pid, base, {p: None for p in base.files}, source="compaction", event_id="cleanup"
    )
    assert not saved.rejected and saved.commit_oid
    assert not (await chat.memory_backend.read(chat.pid)).files
