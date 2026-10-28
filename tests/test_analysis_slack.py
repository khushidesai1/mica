import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from mica.agents.analysis import slack_app
from mica.agents.analysis.config import Settings
from mica.agents.analysis.progress import ActivityState, TaskProgress
from mica.agents.analysis.runner import Context, TurnResult
from mica.agents.analysis.sessions import Sessions
from mica.agents.analysis.slack_app import SlackAgent
from mica.agents.analysis.slack_format import (
    activity_blocks,
    chunks,
    done_blocks,
    status_blocks,
    to_mrkdwn,
)
from mica.agents.analysis.storage import LocalStorage


async def test_heartbeat_tolerates_three_unavailable_checks(bot, monkeypatch, caplog):
    from mica.agents.analysis.identity import IdentityUnavailable

    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.001)
    calls = 0

    def authorized():
        nonlocal calls
        calls += 1
        if 2 <= calls <= 4:
            raise IdentityUnavailable("timeout")
        return True

    async def run(ctx, prompt, status):
        while calls < 4:
            await asyncio.sleep(0.001)
        return TurnResult("session", "Finished normally")

    bot.authorized = authorized
    bot.runner.run_turn = run
    await asyncio.wait_for(send(bot, event("run", _notice_ts="9.0")), 1)
    assert bot.completed and bot.last_reply == "Finished normally"
    assert caplog.text.count("access_check_unavailable") == 3


async def test_heartbeat_revocation_interrupts_and_suppresses_delivery(bot, monkeypatch):
    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.001)
    permitted = [True]
    compute = SimpleNamespace(stop=asyncio.Event(), stop_reason="", pending=False)
    bot.runner.compute = compute
    bot.authorized = lambda: permitted[0]

    async def run(ctx, prompt, status):
        permitted[0] = False
        await asyncio.wait_for(compute.stop.wait(), 1)
        return TurnResult("session", "Must not deliver this result")

    bot.runner.run_turn = run
    await send(bot, event("run", _notice_ts="9.0"))
    assert compute.stop_reason == "access_revoked"
    assert not bot.completed
    bot.client.chat_postMessage.assert_not_called()
    bot.client.files_upload_v2.assert_not_called()


class FakeRunner:
    knowledge_reads = 0

    def __init__(self):
        self.calls = []
        self.active = self.maximum = 0

    async def run_turn(self, ctx, prompt, status):
        self.calls.append((ctx.session_id, ctx.project_md, prompt, ctx.workdir))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await asyncio.sleep(0.02)
        (ctx.workdir / "outputs").mkdir(parents=True, exist_ok=True)
        (ctx.workdir / "code").mkdir(exist_ok=True)
        path = (
            ctx.workdir / "outputs" / ("PROJECT.md" if "Survey the data" in prompt else "plot.png")
        )
        path.write_text("# Draft" if path.suffix == ".md" else "figure")
        self.active -= 1
        return TurnResult("session", "Data: fixture.h5ad", [path])


@pytest.fixture
def bot(tmp_path):
    settings = Settings(
        _env_file=None,
        storage="local",
        data_buckets="lab-data",
        work_root=tmp_path / "work",
        state_db=tmp_path / "state.db",
        data_mount_root=tmp_path / "mounts",
        tools_dir=Path(__file__).resolve().parents[1] / "tools",
    )
    storage = LocalStorage(tmp_path / "storage")
    sessions = Sessions(settings.state_db)
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1.2"}
    client.users_info.return_value = {"user": {"profile": {"display_name": "Alice"}}}
    client.conversations_replies.return_value = {"messages": []}
    agent = SlackAgent(settings, storage, sessions, FakeRunner(), client)
    ctx = Context(
        settings.work_root / "liver/C123-1.0",
        settings.data_mount_root / "lab-data/liver",
        project_slug="liver",
        project_md="# Original",
        thread_ts="1.0",
    )
    sessions.save("C123", ctx)
    yield agent
    sessions.close()


def event(text, ts="1.0", thread=None, kind="app_mention", channel="C123", **extra):
    data = {"type": kind, "text": text, "ts": ts, "channel": channel, "user": "U123", **extra}
    if thread:
        data["thread_ts"] = thread
    return {"event_id": f"{channel}-{ts}-{kind}", "event": data}


async def send(bot, body):
    e = body["event"]
    await bot.handle(e, (e["channel"], e.get("thread_ts") or e["ts"]))


def test_formatting():
    text = to_mrkdwn("# Title\n**bold** [source](https://example.org) `**literal**` <@all>")
    assert text == "Title\n*bold* <https://example.org|source> `**literal**` &lt;@all&gt;"
    assert all(len(part) <= 3500 for part in chunks("x" * 8000))


def test_status_labels_are_plain_text_and_bounded():
    blocks = status_blocks(":x:", "h", ["a", "<!channel> b"])
    elements = [element for block in blocks for element in block["elements"]]
    assert elements[0] == {"type": "plain_text", "text": "Mica is working", "emoji": False}
    assert elements[1] == {"type": "mrkdwn", "text": ":x:"}
    assert elements[2] == {"type": "plain_text", "text": "<!channel> b", "emoji": False}
    assert status_blocks(":x:", "Thinking…")[0]["elements"][0]["text"] == "Mica is thinking"
    assert done_blocks([])[0]["elements"][0]["text"] == "✓ Finished"
    assert done_blocks([], "error_max_turns")[0]["elements"][0]["text"].startswith("⚠️ Stopped")
    assert done_blocks(["old", "a", "b", "c"])[0]["elements"][0]["text"] == "✓ a · b · c"
    assert len(status_blocks(":x:", "x" * 500)[1]["elements"][0]["text"]) == 150
    assert len(done_blocks([], "x" * 500)[0]["elements"][0]["text"]) == 300


def test_cleanup_preserves_fresh_work_and_resets_old_session(bot):
    from mica.agents.analysis.runner import Context
    from tools.cleanup_workspaces import cleanup

    for thread in ["1.0", "2.0"]:
        work = bot.settings.work_root / "liver" / f"C123-{thread}"
        work.mkdir(parents=True)
        ctx = Context(work, Path("/data"), session_id="session", thread_ts=thread)
        bot.sessions.save("C123", ctx, turn=3)
    with bot.sessions.db:
        bot.sessions.db.execute("UPDATE sessions SET updated=0 WHERE thread='1.0'")
    assert cleanup(bot.settings) == 1
    assert bot.sessions.get("C123", "1.0")[0].session_id is None
    assert bot.sessions.get("C123", "1.0")[1] == 3
    assert bot.sessions.get("C123", "2.0")[0].workdir.exists()


async def test_receipt_status_reused_and_heartbeat_stops(bot, monkeypatch):
    from mica.agents.analysis import slack_app

    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.005)
    await send(bot, event("plot", _notice_ts="9.0"))
    updates = bot.client.chat_update.call_args_list
    assert all(c.kwargs["ts"] == "9.0" for c in updates)
    assert updates[-1].kwargs["blocks"][0]["elements"][0]["text"].startswith("✓")
    assert not re.search(
        r"\d+m \d+s|Done in|— working", json.dumps([c.kwargs for c in updates], ensure_ascii=False)
    )
    assert len(updates) == 2  # No unchanged heartbeat messages.
    count = len(updates)
    await asyncio.sleep(0.02)
    assert bot.client.chat_update.call_count == count
    assert not any(
        c.kwargs["text"] == "Working on it…" for c in bot.client.chat_postMessage.call_args_list
    )


@pytest.mark.parametrize("fail_once", [False, True])
async def test_heartbeat_flushes_throttled_step_and_retries_failure(bot, monkeypatch, fail_once):
    from mica.agents.analysis import slack_app

    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(slack_app, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.005)
    delivered = asyncio.Event()
    attempts = 0

    async def deliver(**kwargs):
        nonlocal attempts
        if kwargs["text"] == "Mica is working: &lt;!channel&gt; b &amp; c":
            attempts += 1
            if fail_once and attempts == 1:
                clock.now += 6
                raise RuntimeError("temporary Slack failure")
            delivered.set()

    async def slow(ctx, prompt, status):
        clock.now = 6
        await status("Reading PROJECT.md")
        await status("<!channel> b & c")
        await status("<!channel> b & c")  # Consecutive duplicates don't add history.
        await asyncio.sleep(0.02)
        assert not delivered.is_set()  # Heartbeat also respects the five-second throttle.
        clock.now = 12
        await asyncio.wait_for(delivered.wait(), timeout=1)
        count = bot.client.chat_update.call_count
        clock.now = 30
        await asyncio.sleep(0.02)
        assert bot.client.chat_update.call_count == count
        return TurnResult("session", "Finished plotting.", [])

    bot.client.chat_update.side_effect = deliver
    bot.runner.run_turn = slow
    await send(bot, event("plot", _notice_ts="9.0"))
    assert attempts == (2 if fail_once else 1)
    final = bot.client.chat_update.call_args.kwargs["blocks"][0]["elements"][0]
    assert final == {
        "type": "plain_text",
        "text": "✓ Reading PROJECT.md · <!channel> b & c",
        "emoji": True,
    }


async def test_failed_turn_does_not_apply_partial_brief(bot):

    async def fail(ctx, prompt, status):
        (ctx.workdir / "outputs").mkdir(parents=True)
        draft = ctx.workdir / "outputs/PROJECT.md"
        draft.write_text("# Partial")
        return TurnResult("session", "", [draft], error="interrupted")

    bot.runner.run_turn = fail
    await send(bot, event("update the brief"))
    assert bot.sessions.get("C123", "1.0")[0].project_md == "# Original"
    assert bot.last_error == "interrupted"
    assert bot.client.chat_update.call_args.kwargs["blocks"] == done_blocks([], "interrupted")


async def test_help_me_plot_is_computed(bot):
    await send(bot, event("help me plot these samples"))
    assert "help me plot" in bot.runner.calls[0][2]
    first = bot.client.chat_postMessage.call_args_list[0].kwargs
    assert first["blocks"] == status_blocks(bot.settings.loading_emoji, "Working on it…")


async def test_pdf_and_png_reports_are_attached_to_thread_before_answer(bot, monkeypatch):
    from claude_agent_sdk import ResultMessage

    from mica.agents.analysis import runner

    ctx = bot.sessions.get("C123", "1.0")[0]
    paths = []

    async def query(**kwargs):
        for name in [
            "gap_report/cellpose_gap_report.pdf",
            "figures/overview.png",
            "figures/detail.png",
        ]:
            path = ctx.workdir / "outputs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"report or figure")
            paths.append(path)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="Here's the report and both figures.",
        )

    monkeypatch.setattr(runner, "query", query)
    bot.runner = runner.Runner(bot.settings)
    await send(bot, event("Send the PDF report and PNGs", ts="2.0", thread="1.0"))
    bot.client.files_upload_v2.assert_awaited_once_with(
        channel="C123",
        thread_ts="1.0",
        file_uploads=[{"file": str(p), "filename": p.name} for p in sorted(paths)],
    )
    calls = bot.client.mock_calls
    upload_index = next(i for i, call in enumerate(calls) if call[0] == "files_upload_v2")
    answer_index = next(
        i
        for i, call in enumerate(calls)
        if call[0] == "chat_postMessage" and call.kwargs["text"].startswith("Here's the report")
    )
    assert upload_index < answer_index
    assert bot.completed


async def test_upload_failure_is_visible_and_preserves_reports_for_resending(bot):
    bot.client.files_upload_v2.side_effect = SlackApiError("Upload failed", {"error": "error"})
    await send(bot, event("Send the plot"))
    reply = bot.client.chat_postMessage.call_args.kwargs["text"]
    assert "couldn't confirm their delivery to Slack" in reply
    assert bot.last_reply == reply
    assert reply.startswith("Data: fixture.h5ad")
    assert bot.client.chat_update.call_args.kwargs["text"] == "Stopped"
    assert bot.completed  # The Batch worker can now checkpoint the workspace after the failure.
    ctx, turn, _ = bot.sessions.get("C123", "1.0")
    assert turn == 1
    assert (ctx.workdir / "outputs/plot.png").read_text() == "figure"


async def test_oversized_report_is_reported_as_undelivered(bot, monkeypatch):
    monkeypatch.setattr(slack_app, "MAX_UPLOAD_BYTES", 5)
    await send(bot, event("Send the plot"))
    bot.client.files_upload_v2.assert_not_awaited()
    reply = bot.client.chat_postMessage.call_args.kwargs["text"]
    assert "couldn't attach `plot.png` here" in reply
    assert "compressed or split" in reply
    assert "bucket" not in reply and "outputs/" not in reply
    assert bot.client.chat_update.call_args.kwargs["text"] == "Stopped"
    assert bot.completed


async def test_existing_reports_can_be_resent_without_regenerating(bot, monkeypatch):
    from claude_agent_sdk import ResultMessage

    from mica.agents.analysis import runner

    ctx = bot.sessions.get("C123", "1.0")[0]
    paths = [ctx.workdir / "outputs/gap_report" / name for name in ["report.pdf", "overview.png"]]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing report")
        os.utime(path, ns=(1, 1))

    async def query(**kwargs):
        for path in paths:
            path.touch()
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="Sending the existing report and figure.",
        )

    monkeypatch.setattr(runner, "query", query)
    bot.runner = runner.Runner(bot.settings)
    await send(bot, event("Please resend the report and PNG"))
    uploads = bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert {upload["filename"] for upload in uploads} == {"report.pdf", "overview.png"}
    assert all(path.read_bytes() == b"existing report" for path in paths)


async def test_missing_context_never_runs(bot):
    await send(bot, event("plot", ts="99.0"))
    assert not bot.runner.calls and not bot.completed
    assert "no saved context" in bot.client.chat_postMessage.call_args.kwargs["text"]


async def test_output_named_project_does_not_change_shared_guide(bot):
    ctx = bot.sessions.get("C123", "1.0")[0]
    ctx.brief_generation = "original-generation"
    bot.sessions.save("C123", ctx)

    async def revise(ctx, prompt, status):
        draft = ctx.workdir / "outputs/PROJECT.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("Revised draft")
        return TurnResult("session", "Revised.", [draft])

    bot.runner.run_turn = revise
    await send(bot, event("revise the brief"))
    saved = bot.sessions.get("C123", "1.0")[0]
    assert saved.project_md == "# Original"
    assert saved.brief_generation == "original-generation"


def test_plan_is_bounded_and_does_not_invent_success():
    tasks = [TaskProgress(str(i), "<!channel> check " + str(i), "Checking data…") for i in range(4)]
    tasks[2] = replace(tasks[2], status="complete")
    plan = activity_blocks(tasks)[0]
    assert plan["type"] == "plan" and plan["title"] == "Checking data…"
    assert [t["task_id"] for t in plan["tasks"]] == ["2", "3"]
    assert plan["tasks"][0]["title"] == "<!channel> check 2"  # Slack defines title as plain text.
    final = activity_blocks(tasks, finished=True, error="interrupted")[0]
    assert final["title"] == "Stopped: interrupted"
    assert [t["status"] for t in final["tasks"]] == ["complete", "error"]
    assert "not confirmed" in repr(final)


@pytest.mark.parametrize("reject_plan", [False, True])
async def test_plan_updates_completion_and_falls_back_only_on_invalid_blocks(
    bot, monkeypatch, reject_plan
):
    now = [0]
    monkeypatch.setattr(slack_app, "time", type("Clock", (), {"monotonic": lambda: now[0]}))
    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.005)
    seen = []
    delivered = asyncio.Event()

    async def deliver(**kwargs):
        blocks = kwargs["blocks"]
        seen.append(blocks)
        plan = next((b for b in blocks if b["type"] == "plan"), None)
        if plan:
            if reject_plan:
                raise SlackApiError("Unsupported", {"error": "invalid_blocks"})
            if plan["tasks"][0]["status"] == "complete":
                delivered.set()
        elif reject_plan and len(seen) >= 4:
            delivered.set()

    async def compute(ctx, prompt, status):
        task = TaskProgress("one", "Check counts", "Checking data…")
        now[0] = 6
        await status(task)
        await status(replace(task, status="complete"))
        now[0] = 12
        await asyncio.wait_for(delivered.wait(), 1)
        return TurnResult("session", "Done.")

    bot.runner.run_turn = compute
    bot.client.chat_update.side_effect = deliver
    await send(bot, event("check", _notice_ts="9.0"))
    plans = [block for blocks in seen for block in blocks if block["type"] == "plan"]
    if reject_plan:
        assert len(plans) == 1
        assert seen[-1][0]["type"] == "context"
    else:
        assert plans[-1]["title"] == "Finished"
        assert plans[-1]["tasks"][0]["status"] == "complete"
    assert bot.client.chat_postMessage.call_args.kwargs["text"] == "Done."


async def test_activity_switches_without_repeating_status_or_posting_messages(bot, monkeypatch):
    now = [0]
    monkeypatch.setattr(slack_app, "time", type("Clock", (), {"monotonic": lambda: now[0]}))

    async def compute(ctx, prompt, status):
        now[0] = 6
        await status(ActivityState("thinking"))
        now[0] = 12
        await status(ActivityState("thinking"))
        task = TaskProgress("tile", "Segmenting Cellpose-SAM: tile_02.npy", "Segmenting cells…")
        await status(task)
        now[0] = 18
        await status(replace(task, status="complete"))
        now[0] = 24
        await status(ActivityState("thinking"))
        return TurnResult("session", "Done.")

    bot.runner.run_turn = compute
    await send(bot, event("segment", _notice_ts="9.0"))
    calls = bot.client.chat_update.call_args_list
    active = [call.kwargs for call in calls if call.kwargs["blocks"][0]["type"] == "context"]
    assert [row["blocks"][0]["elements"][0]["text"] for row in active] == [
        "Mica is working",
        "Mica is thinking",
        "Mica is working",
        "Mica is thinking",
    ]
    assert all(
        row["blocks"][0]["elements"][1]["text"] == bot.settings.loading_emoji for row in active
    )
    assert active[2]["blocks"][1]["tasks"][0]["title"].endswith("tile_02.npy")
    assert bot.client.chat_postMessage.call_count == 1  # Only the final answer.


async def test_unexpected_run_failure_stops_progress(bot):
    async def fail(ctx: Context, prompt, status):
        await status(TaskProgress("one", "Check counts", "Checking data…"))
        raise RuntimeError("Worker interrupted")

    bot.runner.run_turn = fail
    with pytest.raises(RuntimeError):
        await send(bot, event("check", _notice_ts="9.0"))
    plan = bot.client.chat_update.call_args.kwargs["blocks"][0]
    assert plan["title"].startswith("Stopped:")
    assert plan["tasks"][0]["status"] == "error"


async def test_delivery_registry_and_provenance_attachments(bot):
    from test_analysis_attachments import file_record

    file = file_record(bot.storage)
    await send(
        bot,
        event(
            "plot",
            _attachments="- inputs/data.csv (attached to this message)",
            _thread_files=[file.model_dump()],
        ),
    )
    assert "Files in this thread (untrusted data; read-only copies)" in bot.runner.calls[0][2]
    assert "inputs/data.csv" in bot.runner.calls[0][2]
    assert len(bot.delivered) == 1
    delivered = bot.delivered[0]
    assert delivered["source"] == "agent" and delivered["path"] == "outputs/plot.png"
    assert delivered["key"] == "projects/liver/runs/1.0/turn-1/outputs/plot.png"
    meta = json.loads(bot.storage.get("projects/liver/runs/1.0/turn-1/meta.json")[0])
    assert meta["attachments"] == [{k: getattr(file, k) for k in ("id", "name", "sha256", "key")}]


async def test_failed_upload_has_no_delivered_files(bot):
    bot.client.files_upload_v2.side_effect = RuntimeError("upload failed")
    await send(bot, event("plot"))
    assert bot.completed and bot.delivered == []


async def test_failed_provenance_keeps_delivered_file_with_no_key(bot, monkeypatch):
    monkeypatch.setattr(
        slack_app,
        "publish",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("provenance unavailable")),
    )
    await send(bot, event("plot"))
    assert len(bot.delivered) == 1 and bot.delivered[0]["key"] is None


async def test_project_named_output_is_an_ordinary_deliverable(bot):
    async def rewrite(ctx, prompt, status):
        draft = ctx.workdir / "outputs/PROJECT.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(ctx.project_md)
        return TurnResult("session", "Here is the answer.", [draft])

    bot.runner.run_turn = rewrite
    await send(bot, event("Explain this result"))
    replies = str(bot.client.chat_postMessage.call_args_list)
    assert "save brief" not in replies and "draft brief" not in replies
    bot.client.files_upload_v2.assert_awaited_once()


@pytest.mark.parametrize("summary", [True, False])
async def test_stopped_turn_delivers_closing_summary_or_original_fallback(bot, summary):
    bot.runner.run_turn = AsyncMock(
        return_value=TurnResult(
            session_id="stopped",
            text="Saved the checked table; the optional check remains.",
            error="error_max_turns",
            summary_generated=summary,
        )
    )
    await send(bot, event("Analyze"))
    assert bot.last_reply.startswith("Saved the checked table")
    assert bot.last_error == "error_max_turns"
    assert bot.client.chat_update.call_args.kwargs["text"] == "Stopped"
    if summary:
        assert "Stopped before finishing (turn limit)" in bot.last_reply
        assert "Want me to try again" not in bot.last_reply
    else:
        assert "That run stopped early (`error_max_turns`). Want me to try again?" in bot.last_reply
