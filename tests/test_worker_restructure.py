import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_analysis_runner import result

from mica.agents.analysis import memory_workspace, project_memory, prompts, runner
from mica.agents.analysis.config import Settings
from mica.agents.analysis.deliverables import select_files, send_tool
from mica.agents.analysis.runner import Context, build_options


@pytest.mark.parametrize(
    "path,kind",
    [
        ("results/test.md", "result"),
        ("recipes/test.md", "recipe"),
        ("data/test.md", "fact"),
        ("lessons.md", "lesson"),
        ("open.md", "open"),
        ("conventions.md", "user"),
    ],
)
def test_normalize_entry_metadata(path, kind):
    text = project_memory.normalize(
        path, "Verified observation.", "Alice; run 123; 2026-10-28; thread 4"
    )
    assert text.startswith(f"type: {kind}\nsource: Alice;")
    assert project_memory.validate(path, text.encode(), Settings(_env_file=None)) == text
    assert project_memory.normalize(path, text, "different") == text
    superseded = text.replace("status: current", "status: superseded by data/new.md")
    assert project_memory.normalize(path, superseded, "different") == superseded


def test_headerless_lesson_survives_worker_checkpoint(tmp_path):
    settings = Settings(_env_file=None)
    base = project_memory.Snapshot(head_oid="a" * 40)
    memory_workspace.materialize(tmp_path, base, settings)
    (tmp_path / "knowledge/lessons.md").write_text("Sample 7 replicates sample 3.")
    data = memory_workspace.pending(
        tmp_path, base, settings, "worker abc; Alice; thread 123; 2026-10-28"
    )
    saved = memory_workspace.decode_pending(data, settings)
    assert not saved.rejected
    assert saved.changes["lessons.md"].startswith("type: lesson\nsource: worker abc;")
    assert "Sample 7" in saved.changes["lessons.md"]


@pytest.mark.parametrize(
    "kind", ["outside", "traversal", "symlink", "parent-link", "suffix", "large", "empty", "gcs"]
)
def test_send_files_rejects_unsafe_inputs(tmp_path, kind):
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "report.txt").write_text("data")
    path = "outputs/report.txt"
    if kind == "outside":
        path = "/etc/passwd"
    elif kind == "traversal":
        path = "outputs/../report.txt"
    elif kind == "symlink":
        (output / "link.txt").symlink_to(output / "report.txt")
        path = "outputs/link.txt"
    elif kind == "parent-link":
        (output / "link").symlink_to(output, target_is_directory=True)
        path = "outputs/link/report.txt"
    elif kind == "suffix":
        (output / "data.unknown").write_text("data")
        path = "outputs/data.unknown"
    elif kind == "empty":
        (output / "report.txt").write_text("")
    elif kind == "gcs":
        (output / "gcs").mkdir()
        (output / "gcs/report.txt").write_text("data")
        path = "outputs/gcs/report.txt"
    selected = select_files(tmp_path, [path], limit=2 if kind == "large" else 100)
    assert not selected["accepted"] and path in selected["errors"]


async def test_resend_unchanged_file_union_and_pending_restart(tmp_path, monkeypatch):
    ctx = Context(tmp_path, tmp_path / "data")
    output = tmp_path / "outputs"
    output.mkdir()
    existing = output / "figure.png"
    existing.write_bytes(b"old figure")
    before = existing.stat().st_mtime_ns
    manifest = {"pending_outputs": []}
    tools = {}
    original = runner.create_server

    def server(manager, extra):
        tools.update({t.name: t for t in extra})
        return original(manager, extra)

    monkeypatch.setattr(runner, "create_server", server)

    async def query(**kwargs):
        selected = await tools["send_files"].handler(
            {"paths": ["outputs/figure.png", "outputs/figure.png"]}
        )
        assert json.loads(selected["content"][0]["text"])["accepted"] == ["outputs/figure.png"]
        (output / "new.txt").write_text("new")
        await tools["send_files"].handler({"paths": ["outputs/new.txt"]})
        yield result(result="Files ready.")

    monkeypatch.setattr(runner, "query", query)
    delivered = await runner.Runner(Settings(_env_file=None)).run_turn(ctx, "Resend", AsyncMock())
    assert delivered.files == sorted([existing, output / "new.txt"])
    assert delivered.attachments == delivered.files
    assert existing.stat().st_mtime_ns == before
    named = set()
    await send_tool(tmp_path, named, manifest=manifest).handler({"paths": ["outputs/figure.png"]})
    restored = json.loads(json.dumps(manifest))
    assert restored["pending_outputs"] == ["figure.png"]
    assert restored["pending_attachments"] == ["figure.png"]
    assert select_files(tmp_path, ["outputs/" + p for p in restored["pending_outputs"]])[
        "accepted"
    ] == ["outputs/figure.png"]


async def test_stable_system_per_turn_hook_and_trusted_skills(tmp_path):
    ctx = Context(tmp_path, tmp_path / "data", project_md="")
    settings = Settings(_env_file=None)
    first = build_options(ctx, settings, memory_context="FIRST FACT", write_access="FIRST GRANT")
    second = build_options(ctx, settings, memory_context="SECOND FACT", write_access="NO GRANT")
    assert first.system_prompt == second.system_prompt
    assert len(first.system_prompt["append"]) <= 4500
    hook = second.hooks["UserPromptSubmit"][0].hooks[0]
    context = await hook({"hook_event_name": "UserPromptSubmit"}, None, {})
    text = context["hookSpecificOutput"]["additionalContext"]
    assert "SECOND FACT" in text and "NO GRANT" in text
    assert "FIRST FACT" not in text
    assert second.setting_sources == []
    assert second.plugins == [{"type": "local", "path": "/opt/pistachio/plugin"}]
    assert second.skills == [
        "mica:spatial-analysis",
        "mica:cellpose",
        "mica:project-knowledge",
        "mica:segmentation-report",
        "mica:session-import",
    ]
    guard = second.hooks["PreToolUse"][0].hooks[0]
    assert not await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Skill",
            "tool_input": {"skill": "mica:cellpose"},
        },
        None,
        {},
    )
    denied = await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Skill",
            "tool_input": {"skill": "update-config"},
        },
        None,
        {},
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "cannot install packages" in prompts.build_system_append(ctx, settings)
    settings.runtimes_enabled = True
    assert "cannot install packages" not in prompts.build_system_append(ctx, settings)
    assert "ensure_packages" in prompts.build_system_append(ctx, settings)
    for name in second.skills:
        assert (Path("skills/skills") / name.split(":")[1] / "SKILL.md").is_file()


def test_worker_prompt_budget_includes_all_static_sections():
    # Includes shared commentary guidance; measured maximum 4,772, excluding the guide.
    ctx = Context(Path("/work"), Path("/data"), project_md="")
    for runtimes in (False, True):
        for scripts in (False, True):
            settings = Settings(_env_file=None, runtimes_enabled=runtimes)
            assert len(prompts.build_system_append(ctx, settings, return_scripts=scripts)) <= 5000


@pytest.mark.parametrize("legacy", [False, True])
async def test_continuation_restores_only_explicit_attachments(tmp_path, monkeypatch, legacy):
    from mica.agents.analysis.recovery_tools import RecoveryManager
    from mica.agents.analysis.resources import Sampler

    ctx = Context(tmp_path, tmp_path / "data")
    output = tmp_path / "outputs"
    output.mkdir()
    for name in ("review.png", "internal.png", "cell_status.parquet"):
        (output / name).write_bytes(b"saved result")
    manifest = {
        "attempt": 1, "tier": "standard", "provisioning": "SPOT",
        "request_deadline": 9999999999,
        "pending_outputs": ["review.png", "internal.png", "cell_status.parquet"],
    }
    if not legacy:
        manifest["pending_attachments"] = ["review.png"]
    settings = Settings(_env_file=None)
    compute = RecoveryManager(settings, manifest, Sampler(tmp_path), AsyncMock())

    async def resumed(*args):
        yield result(result="Checked result.")

    monkeypatch.setattr(runner, "recovery_query", resumed)
    restored = await runner.Runner(settings, compute_manager=compute).run_turn(
        ctx, "Continue", AsyncMock()
    )
    assert restored.error is None
    assert {p.name for p in restored.files} == set(manifest["pending_outputs"])
    assert [p.name for p in restored.attachments] == ([] if legacy else ["review.png"])
    assert manifest["pending_attachments"] == ([] if legacy else ["review.png"])
