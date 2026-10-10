"""The guide is shared, versioned project knowledge, saved without a command."""
# ruff: noqa: F811

from concurrent.futures import ThreadPoolExecutor

import pytest
from analysis_helpers import memory_message as message
from analysis_helpers import seed_project
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import direct, knowledge
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import LocalStorage


def test_legacy_guide_migrates_once_concurrently_and_allows_100kb(tmp_path):
    storage = LocalStorage(tmp_path)
    pid = seed_project(storage, brief=b"# Existing guide\nData lives in raw/")
    settings = Settings(_env_file=None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: knowledge.load(storage, pid, settings), range(2)))
    assert all(r["PROJECT.md"].data.startswith(b"# Existing guide") for r in results)
    assert len(knowledge.history_keys(storage, pid)) == 1
    assert storage.get(f"projects/{pid}/PROJECT.md")[0].startswith(b"# Existing guide")
    knowledge.validate("PROJECT.md", b"x" * 100_000, settings)
    with pytest.raises(ValueError):
        knowledge.validate("PROJECT.md", b"x" * 100_001, settings)
    with pytest.raises(ValueError):
        knowledge.validate("data/note.md", b"Headerless ordinary note", settings)


async def test_chat_correction_is_immediately_shared_and_undoable(chat):
    chat.decision.return_value = direct.Decision(
        route="reply", reply="Updated the sample path.", guide="# Guide\nSample B: raw/B2/"
    )
    await message(chat, "Sample B is in raw/B2/", "1.0")
    saved = knowledge.load(chat.store, chat.pid, chat.settings)
    assert b"raw/B2/" in saved["PROJECT.md"].data
    chat.decision.return_value = direct.Decision(route="reply", reply="It is in raw/B2/.")
    await message(chat, "Where is sample B?", "2.0")
    assert "raw/B2/" in chat.decision.call_args.args[2]["brief"]
    assert any(
        "project guide" in call.kwargs.get("text", "").lower()
        for call in chat.test_slack.chat_postMessage.call_args_list
    )
    await message(chat, "undo knowledge", "3.0")
    assert b"raw/B2/" not in knowledge.load(chat.store, chat.pid, chat.settings)["PROJECT.md"].data


async def test_init_saves_guide_and_legacy_commands_explain_autosave(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *args: {"entries": []})
    chat.decision.return_value = direct.Decision(route="reply", reply="Ready.", guide="# New guide")
    await message(chat, "init new gs://lab-data/new/ --force", "1.0")
    from pistachio.agents.analysis.registry import Registry

    pid = Registry(chat.store).load().bound("T1", "D1")
    assert knowledge.load(chat.store, pid, chat.settings)["PROJECT.md"].data == b"# New guide"
    count = chat.decision.await_count
    for i, cmd in enumerate(("save brief", "clean brief"), 2):
        result = await message(chat, cmd, f"{i}.0", thread="1.0")
        assert "saves automatically" in result["notice_text"]
    assert chat.decision.await_count == count


def test_pending_worker_guide_keeps_original_generation_on_recovery(tmp_path):
    store = LocalStorage(tmp_path / "store")
    work = tmp_path / "work"
    settings = Settings(_env_file=None)
    knowledge.commit(
        store, "liver", {"PROJECT.md": b"Original"}, {}, settings, event_id="start", user_key="u"
    )
    base = knowledge.download(store, "liver", work, settings)
    (work / "knowledge/PROJECT.md").write_text("Pending worker correction")
    pending = knowledge.pending(work, base, settings)
    knowledge.commit(
        store,
        "liver",
        {"PROJECT.md": b"New chat correction"},
        base,
        settings,
        event_id="chat",
        user_key="u",
    )
    latest = knowledge.download(store, "liver", work, settings)
    restored_base = knowledge.restore_pending(pending, work, latest, settings)
    result = knowledge.save(
        store, "liver", work, restored_base, settings, event_id="worker", user_key="u"
    )
    assert result.conflicts == ["PROJECT.md"]
    current = knowledge.load(store, "liver", settings)
    assert current["PROJECT.md"].data == b"New chat correction"
    assert any(
        v.data == b"Pending worker correction"
        for k, v in current.items()
        if k.startswith("_unmerged/")
    )
    with pytest.raises(ValueError):
        knowledge.restore_pending(b'{"../escape": {}}', work, latest, settings)
