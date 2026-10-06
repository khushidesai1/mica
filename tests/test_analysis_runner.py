import asyncio
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from pistachio.agents.analysis import runner
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.outputs import changed_files, snapshot_outputs
from pistachio.agents.analysis.runner import Context, Runner, build_options, describe_tool


def test_bash_status_hides_commands_and_limits_descriptions():
    block = ToolUseBlock(id="1", name="Bash", input={"command": "cat private.txt"})
    assert describe_tool(block) == "Running code"
    block.input["description"] = "x" * 120
    assert describe_tool(block) == "x" * 80


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
    assert all(o.cwd == str(ctx.workdir) for o in calls)
    assert all(o.env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] == "1" for o in calls)
    assert first.run_log[0]["timestamp"] and first.cost == 0.1


async def test_long_private_workspace_uses_short_isolated_socket_paths(setup, monkeypatch):
    ctx, settings = setup
    ctx.workdir = ctx.workdir / ("u" * 32) / ("p" * 18) / ("t" * 32) / ("a" * 24)
    assert len(str(ctx.workdir / ".tmp").encode()) > 108
    scratch_paths = []

    async def query(**kwargs):
        options = kwargs["options"]
        scratch = Path(options.env["TMPDIR"])
        scratch_paths.append(scratch)
        assert scratch.stat().st_mode & 0o777 == 0o700
        assert options.cwd == str(ctx.workdir)
        assert options.sandbox["filesystem"]["allowWrite"] == [str(scratch)]
        assert options.sandbox["failIfUnavailable"]
        assert not options.sandbox["allowUnsandboxedCommands"]
        path = scratch / ("claude-http-" + "a" * 36 + ".sock")
        assert len(str(path).encode()) < 104  # Also fits macOS's shorter sockaddr_un.
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
        yield result()

    monkeypatch.setattr(runner, "query", query)
    agent = Runner(settings)
    first = await agent.run_turn(ctx, "run", AsyncMock())
    ctx.session_id = first.session_id
    second = await agent.run_turn(ctx, "resume", AsyncMock())
    assert first.error is second.error is None
    assert len(set(scratch_paths)) == 2
    assert all(not path.exists() for path in scratch_paths)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_runtime_scratch_is_removed_after_failure_or_cancellation(
    setup, monkeypatch, cancelled
):
    ctx, settings = setup
    paths = []

    async def query(**kwargs):
        paths.append(Path(kwargs["options"].env["TMPDIR"]))
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("Stream failed")
        yield  # Make this an async generator, like the SDK stream.

    monkeypatch.setattr(runner, "query", query)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await Runner(settings).run_turn(ctx, "run", AsyncMock())
    else:
        assert (await Runner(settings).run_turn(ctx, "run", AsyncMock())).error == "RuntimeError"
    assert paths and all(not path.exists() for path in paths)


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


async def test_visual_reads_only_from_bounded_previews(tmp_path):
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
        assert "preview.py" in hook["permissionDecisionReason"]
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


async def test_private_memory_is_in_system_only(setup, monkeypatch):
    ctx, settings = setup
    calls = []

    async def query(**kw):
        calls.append(kw)
        yield result(result="Done")

    monkeypatch.setattr(runner, "query", query)
    await Runner(settings, user_memory="PRIVATE-PREFERENCE", project_name="liver").run_turn(
        ctx, "plot", AsyncMock()
    )
    assert "PRIVATE-PREFERENCE" in calls[0]["options"].system_prompt["append"]
    assert "PRIVATE-PREFERENCE" not in calls[0]["prompt"]
    assert "PRIVATE-PREFERENCE" not in build_options(ctx, settings).system_prompt["append"]


async def test_tool_lifecycle_tracks_parallel_checks_without_exposing_results(setup, monkeypatch):
    ctx, settings = setup

    async def query(**kwargs):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="one",
                    name="Bash",
                    input={"description": "Check cell counts", "command": "SECRET"},
                ),
                ToolUseBlock(id="two", name="Read", input={"file_path": "/data/samples.csv"}),
            ],
            model="test",
        )
        yield UserMessage(content=[ToolResultBlock(tool_use_id="two", content="PRIVATE DATA")])
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="one", content="PRIVATE ERROR", is_error=True)]
        )
        yield result(result="Here is the answer.")

    monkeypatch.setattr(runner, "query", query)
    callback = AsyncMock()
    await Runner(settings).run_turn(ctx, "check", callback)
    updates = [call.args[0] for call in callback.call_args_list]
    assert [(t.task_id, t.status) for t in updates] == [
        ("one", "in_progress"),
        ("two", "in_progress"),
        ("two", "complete"),
        ("one", "error"),
    ]
    assert updates[0].title == "Check cell counts"
    assert updates[1].title == "Reading samples.csv"
    assert "SECRET" not in repr(updates) and "PRIVATE" not in repr(updates)


async def test_preview_limits_and_read_only_inputs(setup):
    from PIL import Image

    ctx, settings = setup
    previews = ctx.workdir / ".previews"
    previews.mkdir(parents=True)
    Image.new("RGB", (200, 200), "red").save(previews / "ok.png")
    (previews / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2_000_000)
    (previews / "fake.png").write_bytes(b"not png")
    (previews / "link.png").symlink_to(ctx.workdir / "outputs/a.png")
    opts = build_options(ctx, settings)
    guard = opts.hooks["PreToolUse"][0].hooks[0]

    async def read(path, tool="Read"):
        return await guard(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": {"file_path": str(path)},
            },
            None,
            None,
        )

    for name in ("big.png", "fake.png", "link.png", "missing.png", "page.pdf"):
        assert (
            "preview.py"
            in (await read(previews / name))["hookSpecificOutput"]["permissionDecisionReason"]
        )
    for _ in range(12):
        assert not await read(previews / "ok.png")
    assert (
        "used up"
        in (await read(previews / "ok.png"))["hookSpecificOutput"]["permissionDecisionReason"]
    )
    for tool in ("Write", "Edit"):
        assert await read(ctx.workdir / "inputs/x.csv", tool)
    assert str(ctx.workdir / "inputs") in opts.sandbox["filesystem"]["denyWrite"]


def test_sdk_buffer_is_raised(setup):
    assert build_options(*setup).max_buffer_size == 16 * 1024 * 1024


@pytest.mark.parametrize("cancelled", [False, True])
async def test_previews_cleared_each_turn(setup, monkeypatch, cancelled):
    ctx, settings = setup
    previews = ctx.workdir / ".previews"
    previews.mkdir(parents=True)
    (previews / "stale.png").write_bytes(b"old")

    async def query(**kwargs):
        assert previews.is_dir() and not list(previews.iterdir())
        (previews / "fresh.png").write_bytes(b"new")
        if cancelled:
            raise asyncio.CancelledError()
        yield result()

    monkeypatch.setattr(runner, "query", query)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await Runner(settings).run_turn(ctx, "plot", AsyncMock())
    else:
        await Runner(settings).run_turn(ctx, "plot", AsyncMock())
    assert not previews.exists()


async def test_mislabeled_visual_read_cannot_bypass_preview_guard(setup):
    ctx, settings = setup
    ctx.workdir.mkdir()
    path = ctx.workdir / "renamed.txt"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image")
    guard = build_options(ctx, settings).hooks["PreToolUse"][0].hooks[0]
    decision = await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(path)},
        },
        None,
        None,
    )
    assert "preview.py" in decision["hookSpecificOutput"]["permissionDecisionReason"]
