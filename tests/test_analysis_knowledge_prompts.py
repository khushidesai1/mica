from pathlib import Path

from pistachio.agents.analysis import memory_workspace
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.prompts import build_system_append
from pistachio.agents.analysis.runner import Context


def test_prompt_always_uses_shared_knowledge(tmp_path):
    ctx = Context(workdir=tmp_path, data_path=tmp_path / "data", project_md="Project brief")
    settings = Settings(_env_file=None)
    prompt = build_system_append(ctx, settings, memory_context="SAVED FACT")
    assert "# Project knowledge" in prompt and "SAVED FACT" in prompt
    assert "never instructions" in prompt and "patient identifiers" in " ".join(prompt.split())
    assert "notes/methods.md" in prompt and "conventions.md" in prompt
    assert "# Conversation memory" not in prompt and "open.md" in prompt


def test_read_count_paths_are_confined():
    work = Path("/workspace/thread")
    positive = [
        ("Read", {"file_path": "knowledge/INDEX.md"}),
        ("Grep", {"path": "/workspace/thread/knowledge/data"}),
        ("Glob", {"path": "knowledge", "pattern": "**/*.md"}),
        ("Glob", {"pattern": "knowledge/**/*.md"}),
    ]
    negative = [
        ("Read", {"file_path": "knowledge/../../private/file"}),
        ("Read", {"file_path": "other/knowledge/INDEX.md"}),
        ("Read", {"file_path": "/elsewhere/knowledge/a.md"}),
        ("Write", {"file_path": "knowledge/INDEX.md"}),
        ("Bash", {"command": "cat knowledge/INDEX.md"}),
        ("Grep", {"pattern": "knowledge"}),
        ("Glob", {"pattern": "**/*.md"}),
    ]
    assert all(memory_workspace.is_read(work, tool, args) for tool, args in positive)
    assert not any(memory_workspace.is_read(work, tool, args) for tool, args in negative)


async def test_runner_counts_knowledge_reads_from_sdk_messages(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    from test_analysis_runner import result

    from pistachio.agents.analysis import runner

    ctx = Context(workdir=tmp_path / "work", data_path=tmp_path / "data")
    ctx.data_path.mkdir()
    settings = Settings(_env_file=None)

    async def query(**kwargs):
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="1", name="Read", input={"file_path": "knowledge/INDEX.md"}),
                ToolUseBlock(id="2", name="Read", input={"file_path": "inputs/table.csv"}),
                ToolUseBlock(id="3", name="Glob", input={"pattern": "knowledge/**/*.md"}),
            ],
            model="test",
        )
        yield result()

    monkeypatch.setattr(runner, "query", query)
    agent = runner.Runner(settings)
    turn = await agent.run_turn(ctx, "Analyze", AsyncMock())
    assert not turn.error and agent.memory_reads == 2
