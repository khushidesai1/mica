import json
import os
from unittest.mock import patch

import pytest

from mica.agents.analysis import memory_checks, memory_workspace, project_memory, thread_memory
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runner import Context, build_options


async def test_tool_validates_replaces_and_passes_stop_check(tmp_path):
    tool = memory_checks.turn_tool(tmp_path, "t_one")
    assert tool.input_schema == thread_memory.Prose.model_json_schema()
    path = tmp_path / "knowledge/threads/t_one/turn.json"
    assert (await tool.handler({"summary": "missing paraphrase"}))["isError"]
    assert not path.exists() and not (tmp_path / "knowledge").exists()
    assert "isError" not in await tool.handler({"paraphrase": "Initial attempt."})
    assert "isError" not in await tool.handler({"paraphrase": "Corrected attempt."})
    assert json.loads(path.read_text())["paraphrase"] == "Corrected attempt."
    before = path.read_bytes()
    assert (await tool.handler({"paraphrase": "Invalid", "covered": ["forged"]}))["isError"]
    assert path.read_bytes() == before
    assert (await tool.handler({"paraphrase": "Too large", "tags": ["x" * 1_048_576]}))["isError"]
    assert path.read_bytes() == before
    checks = memory_checks.Checks(tmp_path, Settings(_env_file=None, thread_memory_enabled=True),
                                  "t_one", None, None)
    assert not checks.check()
    proposal = memory_workspace.decode_pending(memory_workspace.pending(
        tmp_path, project_memory.Snapshot(head_oid=""), checks.settings, thread="t_one"
    ), checks.settings)
    assert proposal.thread.paraphrase == "Corrected attempt."


@pytest.mark.parametrize("relative", ["knowledge", "knowledge/threads", "knowledge/threads/t_one",
                                     "knowledge/threads/t_one/turn.json"])
async def test_tool_refuses_every_symlink_component(tmp_path, relative):
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    target = work / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    result = await memory_checks.turn_tool(work, "t_one").handler({"paraphrase": "Attempt."})
    assert result["isError"] and not list(outside.iterdir()) and target.is_symlink()


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory"])
async def test_tool_refuses_special_and_shared_files(tmp_path, kind):
    target = tmp_path / "knowledge/threads/t_one/turn.json"
    target.parent.mkdir(parents=True)
    original = tmp_path / "original"
    original.write_text("unchanged")
    if kind == "hardlink":
        os.link(original, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    assert (await memory_checks.turn_tool(tmp_path, "t_one").handler(
        {"paraphrase": "Attempt."}
    ))["isError"]
    assert original.read_text() == "unchanged"


def test_failed_atomic_replace_preserves_previous_turn(tmp_path, monkeypatch):
    memory_workspace.record_turn(tmp_path, "t_one", {"paraphrase": "Original."})
    path = tmp_path / "knowledge/threads/t_one/turn.json"
    before = path.read_bytes()
    monkeypatch.setattr(memory_workspace.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(
        OSError("storage unavailable")
    ))
    with pytest.raises(OSError):
        memory_workspace.record_turn(tmp_path, "t_one", {"paraphrase": "New."})
    assert path.read_bytes() == before and list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("survey", [False, True])
async def test_tool_registered_with_thread_hook_including_surveys(tmp_path, survey):
    captured = []
    settings = Settings(_env_file=None, thread_memory_enabled=True, runtimes_enabled=False)
    ctx = Context(tmp_path, tmp_path / "data", survey_only=survey)
    with patch("mica.agents.analysis.runner.create_server", side_effect=lambda manager, tools:
               captured.extend(tools) or {}):
        options = build_options(ctx, settings, thread_key="t_one")
    assert any(tool.name == "record_turn" for tool in captured)
    assert "mcp__pistachio__record_turn" in options.allowed_tools
    hook = options.hooks["UserPromptSubmit"][0].hooks[0]
    text = (await hook({"hook_event_name": "UserPromptSubmit"}, None, {}))[
        "hookSpecificOutput"]["additionalContext"]
    assert "Before finishing, call record_turn." in text
    assert "JSON schema" not in text and '"properties"' not in text


def test_definitions_are_advisory_for_ordinary_turns_and_exact_for_imports(tmp_path):
    root = tmp_path / "knowledge/results"
    root.mkdir(parents=True)
    path = root / "result.md"
    path.write_text("## Group definitions\nA small edit.")
    settings = Settings(_env_file=None)
    base = project_memory.Snapshot(head_oid="")
    assert not memory_checks.Checks(tmp_path, settings, "", base, None).reasons()
    check = memory_checks.Checks(tmp_path, settings, "", base, [])
    assert "results" in check.reasons()
    path.write_text("## Definitions\nGroup: unknown.")
    assert "results" not in check.reasons()


def test_thread_stop_reminders_stay_bounded(tmp_path):
    checks = memory_checks.Checks(tmp_path, Settings(_env_file=None, thread_memory_enabled=True),
                                  "t_one", None, None)
    assert checks.check() == checks.check() == "Call record_turn before finishing."
    assert checks.check() == ""
