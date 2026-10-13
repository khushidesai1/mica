"""Shared guide initialization and recovery of unsaved worker corrections."""
# ruff: noqa: F811

import pytest
from analysis_helpers import memory_message as message
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import direct, memory_workspace, project_memory
from pistachio.agents.analysis.config import Settings


async def test_init_saves_guide_and_legacy_commands_explain_autosave(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *args, **kwargs: {"entries": []})
    chat.decision.return_value = direct.Decision(route="reply", reply="Ready.", guide="# New guide")
    await message(chat, "init new gs://lab-data/new/ --force", "1.0")
    from pistachio.agents.analysis.registry import Registry

    pid = Registry(chat.store).load().bound("T1", "D1")
    assert (await chat.memory_backend.read(pid)).files["PROJECT.md"].text == "# New guide"
    count = chat.decision.await_count
    for i, cmd in enumerate(("save brief", "clean brief", "remember hi", "knowledge"), 2):
        result = await message(chat, cmd, f"{i}.0", thread="1.0")
        assert "remembers and tidies project memory itself" in result["notice_text"]
    assert chat.decision.await_count == count


def test_pending_worker_guide_keeps_original_head_on_recovery(tmp_path):
    work = tmp_path / "work"
    settings = Settings(_env_file=None)
    base = project_memory.Snapshot(
        head_oid="a" * 40, files={"PROJECT.md": project_memory.File(text="Original", oid="b" * 40)}
    )
    memory_workspace.materialize(work, base, settings)
    (work / "knowledge/PROJECT.md").write_text("Pending worker correction")
    pending = memory_workspace.pending(work, base, settings)
    memory_workspace.materialize(work, base, settings)
    restored = memory_workspace.restore_pending(pending, work, base, settings)
    assert restored == base
    assert (work / "knowledge/PROJECT.md").read_text() == "Pending worker correction"
    latest = base.model_copy(update={"head_oid": "c" * 40})
    with pytest.raises(ValueError, match="original snapshot"):
        memory_workspace.restore_pending(pending, work, latest, settings)
    with pytest.raises(ValueError):
        memory_workspace.restore_pending(
            b'{"base_head": "a", "changes": {"../escape": ""}}', work, base, settings
        )
