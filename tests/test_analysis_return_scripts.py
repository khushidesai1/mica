# ruff: noqa: F811
"""Script opt-in, trusted execution snapshots, checkpointing and delivery (offline)."""

import json
import os
import zipfile
from pathlib import Path

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401

from mica.agents.analysis import commands, script_capture, user_settings
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runner import Context, TurnResult, build_options
from mica.agents.analysis.storage import Conflict, LocalStorage


def write(root, name, content=b'print("hello")\n'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def executed(capture, command, *, success=True, tool_id="tool", response=None):
    capture.snapshot()


def test_settings_default_override_cas_and_old_retry(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    assert not Settings(_env_file=None).return_scripts_default
    assert not user_settings.effective(store, "a@lab.org")
    assert user_settings.effective(store, "a@lab.org", True)
    original = store.put_if
    calls = []

    def concurrent(key, data, generation):
        calls.append(generation)
        if len(calls) == 1:
            raise Conflict("concurrent write")
        return original(key, data, generation)

    monkeypatch.setattr(store, "put_if", concurrent)
    user_settings.set_scripts(store, "a@lab.org", True, "on")
    assert len(calls) == 2 and user_settings.effective(store, "a@lab.org")
    user_settings.set_scripts(store, "a@lab.org", False, "off")
    user_settings.set_scripts(store, "a@lab.org", True, "on")
    assert not user_settings.effective(store, "a@lab.org", True)
    assert user_settings.key("a@lab.org") != "legacy-personal-memory"
    # Model-produced memory can never become a trusted preference.
    store.put("legacy-personal-memory", b'{"return_scripts": true}')
    assert not user_settings.effective(store, "b@lab.org")


@pytest.mark.parametrize(
    "value, expected", [("scripts", ()), ("SCRIPTS ON", ("on",)), ("<@U1> scripts off", ("off",))]
)
def test_command_parser(value, expected):
    assert commands.parse(value) == commands.Command("scripts", expected)
    with pytest.raises(ValueError):
        commands.parse("scripts maybe")


async def test_commands_apply_to_sender_without_starting_worker(bridge):
    for ts, text in [("2.0", "scripts on"), ("3.0", "scripts"), ("4.0", "scripts off")]:
        eid = await bridge.accept(payload(ts, text=text))
        assert await bridge.process(eid, 0) == "complete"
    assert bridge.test_client.create_job.call_count == 0
    assert not user_settings.effective(bridge.store, "alice@example.com")
    assert "your setting" in str(bridge.test_slack.chat_update.call_args_list)


async def test_request_freezes_on_across_retry(bridge):
    user_settings.set_scripts(bridge.store, "alice@example.com", True, "on")
    eid = await bridge.accept(payload())
    assert await bridge.process(eid, 0) == "running"
    key = f"tasks/slack-analysis/events/{eid}/request.json"
    first = bridge.store.get(key)[0]
    assert json.loads(first)["return_scripts"] is True
    user_settings.set_scripts(bridge.store, "alice@example.com", False, "off")
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    record["status"] = "submitting"
    await bridge.repository.set_connector_state("claude-event:" + eid, record)
    await bridge.process(eid, 1)
    assert bridge.store.get(key)[0] == first


def unzip(capture, provided=None):
    uploads = capture.uploads(1, provided)
    if not uploads:
        return {}
    assert len(uploads) == 1
    with zipfile.ZipFile(uploads[0]["file"]) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_snapshots_changed_code_only_and_delivers_last_snapshot(tmp_path):
    write(tmp_path, "code/old.py", b"unchanged")
    capture = script_capture.Capture(tmp_path)
    write(tmp_path, "code/nested/a.py", b"first")
    write(tmp_path, "inputs/private.py", b"outside code")
    capture.snapshot()
    write(tmp_path, "code/nested/a.py", b"second")
    capture.snapshot()
    write(tmp_path, "code/nested/a.py", b"edited after final tool")
    assert unzip(capture) == {"code/nested/a.py": b"second"}
    assert not capture.skipped


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "large", "secret", "binary"])
def test_unsafe_changed_script_is_excluded_without_stale_copy(tmp_path, kind):
    capture = script_capture.Capture(tmp_path)
    path = write(tmp_path, "code/a.py")
    capture.snapshot()
    path.unlink()
    if kind == "symlink":
        path.symlink_to(write(tmp_path, "inputs/a.py"))
    elif kind == "hardlink":
        os.link(write(tmp_path, "inputs/a.py"), path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(
            {
                "large": b"x" * (script_capture.MAX_BYTES + 1),
                "secret": b"password=private",
                "binary": b"\xff",
            }[kind]
        )
    capture.snapshot()
    assert not capture.scripts and "code/a.py" in capture.skipped


def test_limits_and_safe_zip_paths(tmp_path):
    capture = script_capture.Capture(tmp_path)
    for index in range(11):
        write(tmp_path, f"code/{index:02}.py")
    capture.snapshot()
    assert len(capture.scripts) == 10 and len(capture.skipped) == 1
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "outputs").symlink_to(outside)
    with pytest.raises(OSError):
        capture.uploads(1)
    assert not list(outside.iterdir())


def test_checkpoint_trust_legacy_and_last_observation_survive_restart(tmp_path):
    capture = script_capture.Capture(tmp_path)
    write(tmp_path, "code/a.py", b"saved")
    capture.snapshot()
    manifest = capture.checkpoint()
    write(tmp_path, "code/a.py", b"edited after snapshot")
    resumed = script_capture.Capture(tmp_path, manifest)
    assert unzip(resumed) == {"code/a.py": b"saved"}
    resumed.snapshot()
    assert unzip(resumed) == {"code/a.py": b"edited after snapshot"}
    manifest = resumed.checkpoint()
    (tmp_path / ".mica").rename(tmp_path / ".pistachio")
    legacy = script_capture.Capture(tmp_path, manifest)
    assert legacy.scripts == resumed.scripts
    legacy.checkpoint()
    write(tmp_path, ".mica/scripts/code/a.py", b"forged")
    assert not script_capture.Capture(tmp_path, manifest).scripts
    assert not script_capture.Capture(tmp_path).scripts


@pytest.mark.parametrize(
    "command",
    [
        "cd code && python a.py | tee log",
        "python code/a.py",
        "python code/a.py &",
        "python code/a.py " + "x" * 33000,
    ],
)
@pytest.mark.parametrize("failed", [False, True])
async def test_registered_hooks_capture_content_without_shell_parsing(tmp_path, command, failed):
    capture = script_capture.Capture(tmp_path)
    settings = Settings(_env_file=None)
    ctx = Context(tmp_path, tmp_path / "data")
    plain = build_options(ctx, settings)
    options = build_options(ctx, settings, return_scripts=True, capture=capture)
    assert options.sandbox == plain.sandbox and "PostToolUse" not in plain.hooks
    assert "snapshots do not prove successful execution" in options.system_prompt["append"]
    write(tmp_path, "code/a.py", b"new script")
    name = "PostToolUseFailure" if failed else "PostToolUse"
    import re

    assert re.fullmatch(options.hooks[name][0].matcher, "Bash")
    assert re.fullmatch(options.hooks[name][0].matcher, "mcp__pistachio__run_gpu")
    await options.hooks[name][0].hooks[-1](
        {
            "hook_event_name": name,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"exit_code": 1 if failed else 0},
        },
        "t",
        {},
    )
    assert unzip(capture) == {"code/a.py": b"new script"}
    assert not capture.skipped


async def test_runner_adds_one_zip_to_normal_outputs_and_preserves_snapshot(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from test_analysis_runner import result

    from mica.agents.analysis import runner

    async def query(**kwargs):
        write(tmp_path, "code/a.py", b"snapshot")
        opts = kwargs["options"]
        if "PostToolUse" in opts.hooks:
            await opts.hooks["PostToolUse"][0].hooks[-1](
                {"hook_event_name": "PostToolUse", "tool_name": "mcp__pistachio__run_gpu"}, "t", {}
            )
        write(tmp_path, "code/a.py", b"later edit")
        yield result()

    monkeypatch.setattr(runner, "query", query)
    agent = runner.Runner(Settings(_env_file=None), return_scripts=True)
    turn = await agent.run_turn(Context(tmp_path, tmp_path / "data"), "plot", AsyncMock())
    assert not turn.error and len(turn.files) == 1
    with zipfile.ZipFile(turn.files[0]) as archive:
        assert archive.read("code/a.py") == b"snapshot"

    agent.return_scripts = False
    followup = await agent.run_turn(Context(tmp_path, tmp_path / "data"), "continue", AsyncMock())
    assert not followup.error and agent.script_capture is None


async def script_runner(bot, *, continuing=False):
    bot.runner.return_scripts = True

    async def run(ctx, prompt, status):
        capture = bot.runner.script_capture = script_capture.Capture(ctx.workdir)
        write(ctx.workdir, "code/nested/a.py", b"script")
        write(ctx.workdir, "code/secret.py", b"password=private")
        capture.snapshot()
        figure = write(ctx.workdir, "outputs/plot.png", b"figure")
        files = [figure, *(Path(row["file"]) for row in capture.uploads(1))]
        if continuing:
            import asyncio

            bot.runner.compute = type("Compute", (), {"pending": True, "stop": asyncio.Event()})()
        return TurnResult("session", "The figure is ready.", files, attachments=files)

    bot.runner.run_turn = run


async def test_zip_delivery_archival_and_secret_warning(bot):
    from mica.agents.analysis.attachments import materialize

    await script_runner(bot)
    await send(bot, event("plot"))
    uploads = bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert len(uploads) == 2 and {Path(u["file"]).suffix for u in uploads} == {".png", ".zip"}
    assert "I didn't attach `secret.py` (possible credential)." in bot.last_reply
    assert len(bot.delivered) == 2 and all(row["key"] for row in bot.delivered)
    archive = next(Path(u["file"]) for u in uploads if u["file"].endswith(".zip"))
    archive.unlink()
    work = bot.sessions.get("C123", "1.0")[0].workdir
    materialize(bot.storage, work, bot.delivered)
    with zipfile.ZipFile(archive) as data:
        assert data.read("code/nested/a.py") == b"script"
    assert not (work / script_capture.CACHE).exists()


async def test_delivery_failure_keeps_zip_for_resend(bot):
    await script_runner(bot)
    bot.client.files_upload_v2.side_effect = RuntimeError("temporary outage")
    await send(bot, event("plot"))
    work = bot.sessions.get("C123", "1.0")[0].workdir
    assert bot.delivered == [] and bot.completed
    assert list((work / "outputs/returned-scripts").rglob("*.zip"))


async def test_cancelled_request_sends_no_scripts(bot):
    await script_runner(bot)
    bot.authorized = lambda: False
    await send(bot, event("plot"))
    assert not bot.completed
    bot.client.files_upload_v2.assert_not_called()


async def test_continuation_keeps_snapshots_without_delivery(bot):
    await script_runner(bot, continuing=True)
    await send(bot, event("plot"))
    assert bot.continuing and not bot.completed
    bot.client.files_upload_v2.assert_not_called()
    assert bot.runner.script_capture.scripts


def test_packaged_snapshot_is_not_sent_twice(tmp_path):
    import hashlib

    capture = script_capture.Capture(tmp_path)
    write(tmp_path, "code/a.py", b"already packaged")
    capture.snapshot()
    assert unzip(capture, {hashlib.sha256(b"already packaged").hexdigest()}) == {}


def test_secret_filename_is_not_echoed(tmp_path):
    capture = script_capture.Capture(tmp_path)
    write(tmp_path, "code/password=private.py")
    capture.snapshot()
    assert capture.notes() == ["I didn't attach `script` (possible credential)."]
