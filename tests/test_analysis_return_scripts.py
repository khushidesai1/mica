# ruff: noqa: F811
"""Script opt-in, trusted execution snapshots, checkpointing and delivery (offline)."""

import json
from pathlib import Path

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401

from pistachio.agents.analysis import commands, script_capture, user_settings
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.runner import Context, TurnResult, build_options
from pistachio.agents.analysis.storage import Conflict, LocalStorage


def write(root, name, content=b'print("hello")\n'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def executed(capture, command, *, success=True, tool_id="tool", response=None):
    capture.before(tool_id, {"command": command})
    capture.after(
        tool_id,
        {
            "hook_event_name": "PostToolUse" if success else "PostToolUseFailure",
            "tool_response": {} if response is None else response,
        },
    )


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


@pytest.mark.parametrize(
    "command",
    [
        "python code/a.py",
        "/opt/sap/bin/python code/a.py",
        "/runtime/venv/bin/python code/a.py",
        "cd code && python a.py",
        "env OMP_NUM_THREADS=1 python -u code/a.py",
        "./code/a.py",
        "Rscript code/a.R",
        "R --vanilla -f code/a.R",
        "julia --project=. code/a.jl",
        "bash code/a.sh",
        'python "code/a space.py"',
    ],
)
def test_recognized_invocations(tmp_path, command):
    for name in ("code/a.py", "code/a.R", "code/a.jl", "code/a.sh", "code/a space.py"):
        write(tmp_path, name)
    capture = script_capture.Capture(tmp_path)
    executed(capture, command)
    assert len(capture.scripts) == 1


@pytest.mark.parametrize(
    "command",
    [
        "cat code/a.py",
        'echo "python code/a.py"',
        'python -c "print(1)" code/a.py',
        "python -m module code/a.py",
        "python --help code/a.py",
        "python -V code/a.py",
        "python -cprint(1) code/a.py",
        "Rscript --version code/a.R",
        "bash -n code/a.sh",
        "/opt/sci/bin/python /opt/pistachio/tools/plot.py",
        "python /opt/pistachio/tools/measure.py -- python code/a.py",
        'bash -c "python code/a.py"',
        'python "unterminated',
        "python inputs/a.py",
        "python outputs/a.py",
        "python .previews/a.py",
        "python scratch/a.py",
        "python code/link.py",
        "python linked/a.py",
        "python code/fifo.py",
    ],
)
def test_nonexecuted_excluded_or_unsafe_files(tmp_path, command):
    import os

    for folder in ("code", "inputs", "outputs", ".previews", "scratch"):
        write(tmp_path, folder + "/a.py")
    (tmp_path / "code/link.py").symlink_to(tmp_path / "code/a.py")
    (tmp_path / "linked").symlink_to(tmp_path / "code", target_is_directory=True)
    os.mkfifo(tmp_path / "code/fifo.py")
    capture = script_capture.Capture(tmp_path)
    executed(capture, command)
    assert not capture.scripts


@pytest.mark.parametrize("separator", ["||", ";", "|"])
def test_ambiguous_shell_success_never_claims_all_branches_ran(tmp_path, separator):
    write(tmp_path, "code/a.py")
    write(tmp_path, "code/b.py")
    capture = script_capture.Capture(tmp_path)
    executed(capture, f"python code/a.py {separator} python code/b.py")
    assert not capture.scripts and len(capture.skipped) == 2


def test_exact_pre_execution_bytes_and_repeated_order(tmp_path):
    path = write(tmp_path, "code/a.py", b"original\n")
    write(tmp_path, "code/b.py")
    capture = script_capture.Capture(tmp_path)
    capture.before("a", {"command": "python code/a.py"})
    path.write_bytes(b"edited during execution\n")
    capture.after("a", {"hook_event_name": "PostToolUse", "tool_response": {}})
    assert capture.scripts["code/a.py"].content == b"original\n"
    executed(capture, "python code/b.py")
    executed(capture, "python code/a.py")
    assert list(capture.scripts) == ["code/b.py", "code/a.py"]
    assert capture.scripts["code/a.py"].content == path.read_bytes()


def test_arguments_are_not_executed_scripts_and_absolute_paths_work(tmp_path):
    write(tmp_path, "code/a.py")
    write(tmp_path, "code/b.py")
    capture = script_capture.Capture(tmp_path)
    executed(capture, f"python {tmp_path}/code/a.py code/b.py")
    assert list(capture.scripts) == ["code/a.py"]


@pytest.mark.parametrize(
    "response",
    [
        {"is_error": True},
        {"exitCode": 1},
        {"exit_code": 7},
        {"interrupted": True},
        {"backgroundTaskId": "id"},
    ],
)
def test_error_and_background_responses_are_not_captured(tmp_path, response):
    write(tmp_path, "code/a.py")
    capture = script_capture.Capture(tmp_path)
    executed(capture, "python code/a.py", response=response)
    executed(capture, "python code/a.py", success=False)
    assert not capture.scripts and not capture.pending


def test_limits_and_secret_rerun_do_not_send_old_version(tmp_path):
    capture = script_capture.Capture(tmp_path)
    for i in range(11):
        write(tmp_path, f"code/s{i}.py")
        executed(capture, f"python code/s{i}.py")
    assert len(capture.scripts) == 10 and capture.skipped["code/s10.py"] == "10-script limit"
    write(tmp_path, "code/s0.py", b"password=private\n")
    executed(capture, "python code/s0.py")
    assert "code/s0.py" not in capture.scripts
    assert capture.skipped["code/s0.py"] == "possible credential"
    write(tmp_path, "code/big.py", b"x" * (script_capture.MAX_BYTES + 1))
    executed(capture, "python code/big.py")
    assert capture.skipped["code/big.py"] == "larger than 1 MB"


def test_checkpoint_carries_captures_but_workspace_cannot_forge_manifest(tmp_path):
    write(tmp_path, "code/a.py")
    capture = script_capture.Capture(tmp_path)
    executed(capture, "python code/a.py")
    manifest = capture.checkpoint()
    write(tmp_path, ".pistachio/scripts/manifest.json", b'{"scripts": []}')
    resumed = script_capture.Capture(tmp_path, manifest)
    assert resumed.scripts == capture.scripts
    resumed.checkpoint()
    write(tmp_path, ".pistachio/scripts/code/a.py", b"forged\n")
    tampered = script_capture.Capture(tmp_path, manifest)
    assert not tampered.scripts and tampered.skipped
    capture.checkpoint()
    assert not script_capture.Capture(tmp_path).scripts  # New request ignores all prior captures.


async def test_hooks_and_prompt_are_opt_in_and_keep_failures_out(tmp_path):
    write(tmp_path, "code/a.py")
    ctx = Context(tmp_path, tmp_path / "data")
    settings = Settings(_env_file=None)
    before = build_options(ctx, settings)
    capture = script_capture.Capture(tmp_path)
    after = build_options(ctx, settings, return_scripts=True, capture=capture)
    assert before.sandbox == after.sandbox
    assert "PostToolUse" not in before.hooks
    assert "attached automatically" in after.system_prompt["append"]
    e = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "python code/a.py"},
        "tool_use_id": "t",
    }
    await after.hooks["PreToolUse"][0].hooks[0](e, "t", {})
    e["hook_event_name"] = "PostToolUseFailure"
    await after.hooks["PostToolUseFailure"][0].hooks[0](e, "t", {})
    assert not capture.scripts
    e["hook_event_name"] = "PreToolUse"
    await after.hooks["PreToolUse"][0].hooks[0](e, "t", {})
    e.update(
        hook_event_name="PostToolUse",
        tool_response={"stdout": "", "stderr": "", "interrupted": False},
    )
    await after.hooks["PostToolUse"][0].hooks[0](e, "t", {})
    assert len(capture.scripts) == 1


async def script_runner(bot, *, continue_run=False):
    bot.runner.return_scripts = True

    async def run(ctx, prompt, status):
        capture = bot.runner.script_capture = script_capture.Capture(ctx.workdir)
        for name in ("code/qc/plot.py", "code/figs/plot.py", "code/secret.py"):
            write(
                ctx.workdir,
                name,
                b"password=private" if name.endswith("secret.py") else name.encode(),
            )
            executed(capture, "python " + name)
        path = write(ctx.workdir, "outputs/plot.png", b"figure")
        if continue_run:
            bot.runner.compute = type("Compute", (), {"pending": True})()
        return TurnResult("session", "The figure is ready.", [path])

    bot.runner.run_turn = run


async def test_delivery_names_bytes_order_registry_and_cleanup(bot):
    from pistachio.agents.analysis.attachments import materialize

    await script_runner(bot)
    await send(bot, event("plot"))
    uploads = bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert [u["filename"] for u in uploads] == ["plot.png", "qc-plot.py", "figs-plot.py"]
    assert Path(uploads[1]["file"]).read_bytes() == b"code/qc/plot.py"
    assert bot.last_reply.startswith("The figure is ready.")
    assert "I didn't attach `secret.py` (possible credential)." in bot.last_reply
    assert len(bot.delivered) == 3 and all(f["key"] for f in bot.delivered)
    work = bot.sessions.get("C123", "1.0")[0].workdir
    assert not (work / script_capture.CACHE).exists()
    Path(uploads[1]["file"]).unlink()
    restored = materialize(bot.storage, work, bot.delivered)
    assert len(restored) == 3 and Path(uploads[1]["file"]).read_bytes() == b"code/qc/plot.py"


async def test_delivery_failure_still_cleans_captures(bot):
    await script_runner(bot)
    bot.client.files_upload_v2.side_effect = RuntimeError("temporary outage")
    await send(bot, event("plot"))
    work = bot.sessions.get("C123", "1.0")[0].workdir
    assert not (work / script_capture.CACHE).exists()
    assert bot.delivered == [] and bot.completed
    assert list((work / "outputs/returned-scripts").rglob("*.py"))


async def test_cancelled_request_sends_no_scripts(bot):
    await script_runner(bot)
    bot.authorized = lambda: False
    with pytest.raises(PermissionError):
        await send(bot, event("plot"))
    bot.client.files_upload_v2.assert_not_called()
    work = bot.sessions.get("C123", "1.0")[0].workdir
    assert not (work / script_capture.CACHE).exists()


async def test_continuation_does_not_deliver_or_clear(bot):
    await script_runner(bot, continue_run=True)
    await send(bot, event("plot"))
    assert bot.continuing and not bot.completed
    bot.client.files_upload_v2.assert_not_called()
    assert len(bot.runner.script_capture.scripts) == 2


def test_pinned_sdk_observation_confirms_nonzero_exit_is_failure(tmp_path):
    from importlib.metadata import version

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/return-scripts/sdk-hooks.json").read_text()
    )
    assert version("claude-agent-sdk") == fixture["sdk_version"] == "0.2.163"
    assert fixture["cli_version"] == "2.1.286"
    events = fixture["events"]
    assert [e["hook_event_name"] for e in events] == [
        "PreToolUse",
        "PostToolUse",
        "PreToolUse",
        "PostToolUseFailure",
    ]
    assert events[-1]["error"] == "Exit code 7"
    write(tmp_path, "code/a.py")
    capture = script_capture.Capture(tmp_path)
    for i in (0, 2):
        capture.before(str(i), {"command": "python code/a.py"})
        capture.after(str(i), events[i + 1])
        assert len(capture.scripts) == 1
    failed = script_capture.Capture(tmp_path)
    failed.before("x", {"command": "python code/a.py"})
    failed.after("x", events[-1])
    assert not failed.scripts


def test_modified_later_script_in_chain_is_not_misrepresented(tmp_path):
    write(tmp_path, "code/modify.py")
    write(tmp_path, "code/run.py", b"old")
    capture = script_capture.Capture(tmp_path)
    capture.before("t", {"command": "python code/modify.py && python code/run.py"})
    write(tmp_path, "code/run.py", b"changed by modify.py")
    capture.after("t", {"hook_event_name": "PostToolUse", "tool_response": {}})
    assert list(capture.scripts) == ["code/modify.py"]
    assert "code/run.py" in capture.skipped


def test_actual_handoff_archive_retains_trusted_execution_copies(tmp_path):
    from pistachio.agents.analysis.batch_worker import restore
    from pistachio.agents.analysis.checkpoints import workspace_snapshot

    home = tmp_path / "worker0"
    work = home / "work/project/thread"
    write(work, "code/first.py", b"first attempt")
    capture = script_capture.Capture(work)
    executed(capture, "python code/first.py")
    manifest = capture.checkpoint()
    archive = tmp_path / "handoff.tar.gz"
    workspace_snapshot(archive, home, partial=False, max_bytes=10_000_000)
    next_home = tmp_path / "worker1"
    next_home.mkdir()
    restore(archive, next_home)
    next_work = next_home / "work/project/thread"
    resumed = script_capture.Capture(next_work, manifest)
    write(next_work, "code/second.py", b"second attempt")
    executed(resumed, "python code/second.py")
    assert [Path(u["file"]).read_bytes() for u in resumed.uploads(1)] == [
        b"first attempt",
        b"second attempt",
    ]


async def test_channel_toggle_affects_only_sender(bridge):
    eid = await bridge.accept(
        payload("7.0", text="scripts on", channel="C1", kind="app_mention", channel_type="channel")
    )
    assert await bridge.process(eid, 0) == "complete"
    assert user_settings.effective(bridge.store, "alice@example.com")
    assert not user_settings.effective(bridge.store, "bob@example.com")


async def test_runner_captures_through_registered_sdk_hooks(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from test_analysis_runner import result

    from pistachio.agents.analysis import runner

    write(tmp_path, "code/a.py", b"original")

    async def query(**kwargs):
        opts = kwargs["options"]
        e = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "python code/a.py"},
            "tool_use_id": "t",
        }
        await opts.hooks["PreToolUse"][0].hooks[0](e, "t", {})
        write(tmp_path, "code/a.py", b"edited later")
        e.update(hook_event_name="PostToolUse", tool_response={})
        await opts.hooks["PostToolUse"][0].hooks[0](e, "t", {})
        yield result()

    monkeypatch.setattr(runner, "query", query)
    agent = runner.Runner(Settings(_env_file=None), return_scripts=True)
    turn = await agent.run_turn(Context(tmp_path, tmp_path / "data"), "plot", AsyncMock())
    assert not turn.error and agent.script_capture.scripts["code/a.py"].content == b"original"


async def test_unwritable_delivery_location_does_not_discard_regular_results(bot, monkeypatch):
    await script_runner(bot)

    def unavailable(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(script_capture.Capture, "uploads", unavailable)
    await send(bot, event("plot"))
    uploads = bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert [u["filename"] for u in uploads] == ["plot.png"]
    assert bot.last_reply.startswith("The figure is ready.")
    assert "attachment copy could not be prepared" in bot.last_reply


def test_secret_shaped_filename_is_not_echoed_in_skip_note(tmp_path):
    write(tmp_path, "code/password=private.py")
    capture = script_capture.Capture(tmp_path)
    executed(capture, "python code/password=private.py")
    assert not capture.scripts
    assert capture.notes() == ["I didn't attach `script` (possible credential)."]


def test_capture_merges_existing_compute_hooks(tmp_path):
    from types import SimpleNamespace

    compute = SimpleNamespace(tools=lambda: [], prompt=lambda: "\nCompute policy")
    ctx = Context(tmp_path, tmp_path / "data")
    settings = Settings(_env_file=None)
    old = build_options(ctx, settings, compute_manager=compute)
    new = build_options(ctx, settings, compute_manager=compute, return_scripts=True)
    for name in ("PostToolUse", "PostToolUseFailure"):
        assert [f.__name__ for f in old.hooks[name][0].hooks] == ["resource_feedback"]
        assert [f.__name__ for f in new.hooks[name][0].hooks] == [
            "resource_feedback",
            "captured_script",
        ]
    assert old.sandbox == new.sandbox


def test_concurrent_tool_hooks_and_checkpoint_keep_bounds(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    capture = script_capture.Capture(tmp_path)
    for i in range(20):
        write(tmp_path, f"code/run{i}.py")

    def run(i):
        executed(capture, f"python code/run{i}.py", tool_id=str(i))
        return capture.checkpoint()

    with ThreadPoolExecutor(max_workers=6) as pool:
        manifests = list(pool.map(run, range(20)))
    assert all(len(m["scripts"]) <= 10 for m in manifests)
    assert not capture.pending and len(capture.scripts) == 10
