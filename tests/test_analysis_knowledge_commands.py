# ruff: noqa: F811
from unittest.mock import AsyncMock

from analysis_helpers import seed_project
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_isolation import message

from pistachio.agents.analysis import commands, knowledge
from pistachio.agents.analysis.registry import Registry

NOTE = b"type: fact\nsource: inspect, 2026-10-09, A\nstatus: current\nTMA26 failed Proseg."


def seed(chat):
    chat.settings.knowledge_enabled = True
    root = knowledge.prefix(chat.pid)
    chat.store.put(root + "data/samples.md", NOTE)
    chat.store.put(
        root + "INDEX.md", b"- [Samples](data/samples.md) - fact - 2026-10-09 - Proseg\n"
    )


async def test_review_forget_undo_and_help(chat):
    seed(chat)
    result = await message(chat, "knowledge", "1.0")
    assert "Samples" in result["notice_text"] and "2 files" in result["notice_text"]
    result = await message(chat, "knowledge show data/samples.md", "2.0")
    assert "TMA26" in result["notice_text"]
    result = await message(chat, "forget knowledge data/samples.md", "3.0")
    assert "Removed" in result["notice_text"]
    assert b"Samples" not in chat.store.get(knowledge.prefix(chat.pid) + "INDEX.md")[0]
    result = await message(chat, "undo knowledge", "4.0")
    assert "Restored" in result["notice_text"]
    assert chat.store.get(knowledge.prefix(chat.pid) + "data/samples.md")[0] == NOTE
    result = await message(chat, "help", "5.0")
    assert "knowledge show" in result["notice_text"] and "undo knowledge" in result["notice_text"]
    chat.decision.assert_not_awaited()


async def test_denied_and_path_traversal(chat):
    seed(chat)
    # Bind to a known but inaccessible project: rejected before loading its knowledge.
    seed_project(chat.store, name="private", owner="bob@example.com", channel="D2")
    result = await message(chat, "knowledge show data/samples.md", "1.0", user="U2")
    assert "TMA26" not in result["notice_text"]
    for i, command in enumerate(["knowledge show ../PROJECT.md", "forget knowledge /INDEX.md"], 2):
        result = await message(chat, command, f"{i}.0")
        assert result["status"] == "failed"
    assert chat.store.get(knowledge.prefix(chat.pid) + "data/samples.md")[0] == NOTE


async def test_shared_member_and_published_channel_can_review(chat):
    seed(chat)
    registry = Registry(chat.store)
    registry.update(
        "share",
        "alice@example.com",
        "share",
        lambda d: d.share(chat.pid, "alice@example.com", "bob@example.com"),
    )
    result = await message(chat, "knowledge", "1.0", user="U2")
    assert "Samples" in result["notice_text"]
    pid = seed_project(chat.store, name="channel", channel="C1", publish=True)
    chat.store.put(knowledge.prefix(pid) + "data/samples.md", NOTE)
    result = await message(chat, "knowledge show data/samples.md", "2.0", channel="C1")
    assert "TMA26" in result["notice_text"]


async def test_github_opt_in_owner_only_and_off_overrides_sharing(chat, monkeypatch):
    seed(chat)
    monkeypatch.setattr("pistachio.agents.analysis.job_lifecycle.enqueue", AsyncMock())
    result = await message(chat, "knowledge github on", "1.0")
    assert "enabled" in result["notice_text"]
    assert Registry(chat.store).load().projects[chat.pid].knowledge_github is True
    await message(chat, "share liver <@U2>", "2.0")
    result = await message(chat, "knowledge github off", "3.0", user="U2")
    assert result["status"] == "failed"
    await message(chat, "knowledge github off", "4.0")
    assert Registry(chat.store).load().projects[chat.pid].knowledge_github is False


def test_command_grammar():
    assert commands.parse("knowledge show recipes/a.R").args == ("recipes/a.R",)
    assert commands.parse("forget knowledge data/a.md").name == "forget knowledge"
    assert commands.parse("undo knowledge").name == "undo knowledge"
