from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from pistachio.agents.analysis import runner
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.outputs import changed_files, snapshot_outputs
from pistachio.agents.analysis.runner import Context, Runner, build_options


def result(**kwargs):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-1",
        **kwargs,
    )


@pytest.fixture
def setup(tmp_path):
    ctx = Context(tmp_path / "work", tmp_path / "data")
    ctx.data_path.mkdir()
    return ctx, Settings(_env_file=None, work_root=tmp_path, state_db=tmp_path / "state.db")


async def test_runner_resume_and_files(setup, monkeypatch):
    ctx, settings = setup
    calls = []

    async def query(**kw):
        calls.append(kw["options"])
        (ctx.workdir / "outputs" / "figure.png").write_bytes(b"figure")
        yield AssistantMessage(
            content=[
                ToolUseBlock(id="1", name="Bash", input={"command": "python code/plot.py"}),
                TextBlock(text="Data: spatial.h5ad"),
            ],
            model="test",
        )
        yield result(total_cost_usd=0.1)

    monkeypatch.setattr(runner, "query", query)
    first = await Runner(settings).run_turn(ctx, "plot", AsyncMock())
    ctx.session_id = first.session_id
    await Runner(settings).run_turn(ctx, "bigger", AsyncMock())
    assert first.text.startswith("Data:") and first.files[0].name == "figure.png"
    assert calls[1].resume == "session-1" and calls[0].cwd == calls[1].cwd
    assert first.run_log[0]["timestamp"] and first.cost == 0.1


async def test_missing_session_retries_only_once(setup, monkeypatch):
    ctx, settings = setup
    ctx.session_id = "missing"
    calls = []

    async def query(**kw):
        calls.append(kw)
        if kw["options"].resume:
            raise RuntimeError("No conversation found")
        yield result(result="Data: recovered")

    monkeypatch.setattr(runner, "query", query)
    output = await Runner(settings).run_turn(ctx, "hello", AsyncMock())
    assert len(calls) == 2 and calls[1]["options"].resume is None
    assert output.session_id == "session-1" and output.error is None


async def test_security_options_and_native_tool_boundaries(setup):
    ctx, settings = setup
    opts = build_options(ctx, settings)
    assert opts.setting_sources == [] and opts.permission_mode == "dontAsk"
    assert opts.sandbox["failIfUnavailable"] and not opts.sandbox["allowUnsandboxedCommands"]
    assert opts.env["SLACK_BOT_TOKEN"] == opts.env["PISTACHIO_SLACK_BOT_TOKEN"] == ""
    assert opts.max_budget_usd == 5 and "Agent" in opts.disallowed_tools
    guard = opts.hooks["PreToolUse"][0].hooks[0]
    for name, args in [
        ("Write", {"file_path": str(ctx.data_path / "raw")}),
        ("Read", {"file_path": "/etc/pistachio/secrets.env"}),
        ("Bash", {"dangerouslyDisableSandbox": True}),
        ("Glob", {"pattern": "/etc/pistachio/*"}),
        ("Glob", {"pattern": "../other-thread/*"}),
        ("Grep", {"glob": "../other-thread/*", "pattern": "secret"}),
    ]:
        assert await guard(
            {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args}, None, None
        )
    assert not await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(ctx.workdir / "code/plot.py")},
        },
        None,
        None,
    )


def test_outputs_do_not_follow_links(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    original = tmp_path / "secret"
    original.write_text("private")
    before = snapshot_outputs(outputs)
    (outputs / "leak.txt").symlink_to(original)
    (outputs / "good.txt").write_text("ok")
    assert [p.name for p in changed_files(outputs, before)] == ["good.txt"]


def test_inspection_never_reads_matrix(tmp_path, monkeypatch):
    import h5py
    from make_fixture import make_fixture

    from tools.inspect_data import inspect

    path = make_fixture(tmp_path)
    original = h5py.Dataset.__getitem__

    def guarded(dataset, key):
        assert not dataset.name.startswith(("/X", "/layers", "/obsm", "/varm"))
        return original(dataset, key)

    monkeypatch.setattr(h5py.Dataset, "__getitem__", guarded)
    info = inspect(path)
    assert (info["n_obs"], info["n_vars"]) == (6000, 30)
    assert info["obs"]["sample_id"]["top"] == {"A": 2000, "B": 2000, "C": 2000}
    assert info["obsm"]["spatial"]["shape"] == [6000, 2]


async def test_budget_error_is_preserved_without_retry(setup, monkeypatch):
    ctx, settings = setup
    count = 0

    async def query(**kwargs):
        nonlocal count
        count += 1
        yield ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="stopped",
            total_cost_usd=5,
        )
        raise RuntimeError("query ended after an error result")

    monkeypatch.setattr(runner, "query", query)
    output = await Runner(settings).run_turn(ctx, "plot", AsyncMock())
    assert output.error == "error_max_budget_usd" and output.session_id == "stopped"
    assert output.cost == 5 and count == 1


def test_existing_pistachio_configuration_is_reused(monkeypatch):
    monkeypatch.setenv("PISTACHIO_GCS_BUCKET", "existing-bucket")
    monkeypatch.setenv("PISTACHIO_SLACK_BOT_TOKEN", "existing-test-token")
    settings = Settings(_env_file=None)
    assert settings.bucket == "existing-bucket"
    assert settings.slack_bot_token.get_secret_value() == "existing-test-token"


async def test_image_preview_denied_before_large_content_enters_stream(tmp_path):
    ctx = Context(tmp_path / "work", tmp_path / "data")
    outputs = ctx.workdir / "outputs"
    outputs.mkdir(parents=True)
    image = outputs / "spatial.PNG"
    image.write_bytes(b"x" * 6_500_000)
    opts = build_options(ctx, Settings(_env_file=None))
    guard = opts.hooks["PreToolUse"][0].hooks[0]
    for path in [image, outputs / "figure.pdf", outputs / "figure.webp"]:
        decision = await guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": str(path)},
            },
            None,
            None,
        )
        hook = decision["hookSpecificOutput"]
        assert hook["permissionDecision"] == "deny"
        assert "Image previews are disabled" in hook["permissionDecisionReason"]
    for name, args in [
        ("Read", {"file_path": str(outputs / "summary.csv")}),
        ("Read", {"file_path": str(ctx.workdir / "code/analysis.py")}),
        ("Write", {"file_path": str(outputs / "figure.svg")}),
        ("Bash", {"command": "python code/analysis.py && test -s outputs/spatial.PNG"}),
    ]:
        assert not await guard(
            {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args},
            None,
            None,
        )
    assert (
        image.stat().st_size == 6_500_000
    )  # Full-resolution artifact remains available to upload.
