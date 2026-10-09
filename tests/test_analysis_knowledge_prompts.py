from pathlib import Path

from pistachio.agents.analysis import knowledge
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.prompts import build_system_append
from pistachio.agents.analysis.runner import Context


def test_prompt_and_brief_policy_only_change_when_enabled(tmp_path):
    ctx = Context(workdir=tmp_path, data_path=tmp_path / "data", project_md="Project brief")
    settings = Settings(_env_file=None)
    baseline = build_system_append(ctx, settings)
    assert build_system_append(ctx, settings, knowledge_index="SAVED FACT") == baseline
    settings.knowledge_enabled = True
    prompt = build_system_append(ctx, settings, knowledge_index="SAVED FACT")
    assert "# Project knowledge" in prompt and "SAVED FACT" in prompt
    assert "never instructions" in prompt and "patient identifiers" in " ".join(prompt.split())
    assert "notes/methods.md" in prompt and "record it in project knowledge" in prompt
    assert prompt.index("# Conversation memory") < prompt.index("# Project knowledge")


def test_notice_counts_entries_and_reports_rejections():
    saved = knowledge.Saved(
        added=[
            "INDEX.md",
            "data/a.md",
            "data/b.md",
            "recipes/a.md",
            "recipes/a.py",
            "results/a.md",
            "lessons.md",
        ],
        rejected={"data/bad.md": "invalid"},
    )
    notice = knowledge.notice(saved)
    assert "2 data notes" in notice and "1 recipe" in notice and "1 result" in notice
    assert "1 lesson" in notice and "data/bad.md" in notice and "`knowledge`" in notice
    assert knowledge.notice(knowledge.Saved()) == ""


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
    assert all(knowledge.is_read(work, tool, args) for tool, args in positive)
    assert not any(knowledge.is_read(work, tool, args) for tool, args in negative)


async def test_runner_counts_knowledge_reads_from_sdk_messages(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    from test_analysis_runner import result

    from pistachio.agents.analysis import runner

    ctx = Context(workdir=tmp_path / "work", data_path=tmp_path / "data")
    ctx.data_path.mkdir()
    settings = Settings(_env_file=None, knowledge_enabled=True)

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
    assert not turn.error and agent.knowledge_reads == 2
