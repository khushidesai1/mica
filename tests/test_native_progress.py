"""Slack owns native session chrome; real content and legacy delivery remain intact."""

# ruff: noqa: F811
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401

from mica.agents.analysis import amendments, request_controls, slack_app
from mica.agents.analysis.config import CHECKPOINT_PREFIX
from mica.agents.analysis.progress import ActivityState, ProgressNote, TaskProgress
from mica.agents.analysis.runner import TurnResult
from mica.agents.analysis.slack_format import activity_blocks, activity_fallback, status_blocks


@pytest.mark.parametrize("has_message", [False, True])
async def test_native_placeholders_persist_without_posting_or_erasing_content(bridge, has_message):
    bridge.settings.slack_sessions = True
    record = {"payload": payload()}
    if has_message:
        record.update(notice_ts="8.0", notice_text="Reading files", notice_phase="replying")
    for phase, text in [("received", "Mica is working"), ("replying", "Mica is thinking")]:
        await bridge.notify("native", record, phase, text)
        saved = await bridge.repository.get_connector_state("claude-event:native")
        assert saved["notice_pending_phase"] == phase
        assert saved.get("notice_text") == ("Reading files" if has_message else None)
    bridge.test_slack.chat_postMessage.assert_not_called()
    bridge.test_slack.chat_update.assert_not_called()


@pytest.mark.parametrize("phase,text,title", [
    ("replying", "Reading the source files", ""),
    ("replying", "I found the files. Checking their counts.", "Checking files"),
    ("waiting", "Waiting for an available worker", ""),
    ("waiting", amendments.WAITING, ""),
    ("complete", "Mica is thinking", ""),
    ("complete", "Answer\n" * 1000, ""),
])
async def test_first_real_native_content_keeps_delivery_identity(bridge, phase, text, title):
    bridge.settings.slack_sessions = True
    record = {"payload": payload()}
    event_id = "a" * 32
    await bridge.notify(event_id, record, "replying", "Mica is thinking")
    await bridge.notify(event_id, record, phase, text, activity_title=title)
    assert record["notice_ts"] == "9.0"
    assert "notice_pending_phase" not in record
    calls = bridge.test_slack.chat_postMessage.call_args_list
    assert calls[0].kwargs["thread_ts"] == "1.0"
    assert "in_progress" not in repr([c.kwargs.get("blocks") for c in calls])
    if text == amendments.WAITING:
        assert calls[0].kwargs["blocks"] == amendments.buttons(event_id)
    if phase == "complete":
        assert record["terminal_reply"] == text
        assert "".join(c.kwargs["text"].replace("\n", "") for c in calls) == text.replace("\n", "")
    await bridge.notify(event_id, record, "complete", "Final answer")
    assert bridge.test_slack.chat_update.call_args.kwargs == {
        "channel": "D1", "ts": "9.0", "text": "Final answer",
        "blocks": [{"type": "markdown", "text": "Final answer"}],
    }


@pytest.mark.parametrize("mode", ["flag-off", "channel", "ambient", "other-backend"])
async def test_ineligible_receipt_is_unchanged(bridge, mode):
    bridge.settings.slack_sessions = mode != "flag-off"
    record = {"payload": payload()}
    if mode == "channel":
        record["payload"] = payload(channel="C1", channel_type="channel", kind="app_mention")
    elif mode == "ambient":
        record["ambient"] = {"id": "ambient"}
    elif mode == "other-backend":
        bridge.app.slack_agent_backend = "task"
    await bridge.notify("legacy", record, "received", "Mica is working")
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["blocks"] == status_blocks(
        bridge.settings.loading_emoji, "Mica is working",
    )


def test_native_task_title_preserves_completed_steps_and_details_without_spinner():
    done = TaskProgress("done", "Read files", "Reading", "complete")
    active = TaskProgress("current", "Moving files: 3,221 objects done", "Moving")
    blocks = activity_blocks(
        [done, active], native_session=True, check_in="Progress is saved.", note="Found <!here>.",
    )
    assert blocks[0]["text"] == {"type": "plain_text", "text": "Found <!here>."}
    assert blocks[1]["title"] == active.title
    assert [t["task_id"] for t in blocks[1]["tasks"]] == ["done"]
    assert "Progress is saved." in repr(blocks)
    assert "in_progress" not in repr(blocks)
    assert "Moving files" in repr(activity_fallback(blocks))


def test_native_no_finished_tasks_does_not_invent_completion_or_drop_details():
    blocks = status_blocks("", "Reading files", detail="Checking 4 folders", native_session=True)
    assert blocks[0] == {"type": "plan", "title": "Reading files", "tasks": []}
    assert "Checking 4 folders" in repr(blocks)
    assert "Checking 4 folders" in repr(activity_fallback(blocks))
    assert "complete" not in repr(blocks) and "in_progress" not in repr(blocks)
    title = "Checking " + "sample " * 18
    detail = activity_blocks([TaskProgress("long", title, "Checking")], native_session=True)
    assert " ".join(title.split()) in repr(detail)


@pytest.mark.parametrize("error", [None, "Interrupted"])
@pytest.mark.parametrize("task_status", ["in_progress", "complete", "error"])
def test_native_endings_match_legacy(task_status, error):
    tasks = [TaskProgress("one", "Check files", "Checking", task_status)]
    options = dict(finished=True, error=error, summary="Read input", note="Commentary")
    expected = activity_blocks(tasks, **options)
    assert activity_blocks(tasks, native_session=True, **options) == expected
    assert activity_blocks([], native_session=True, **options) == activity_blocks([], **options)


async def test_checkin_updates_on_flag_change_and_keeps_report_and_continue(bridge, monkeypatch):
    monkeypatch.setattr(request_controls, "AsyncWebClient", lambda **kw: bridge.test_slack)
    event_id = await bridge.accept(payload())
    record = await bridge.repository.get_connector_state("claude-event:" + event_id)
    record.update(flexible_harness=True, routing_calls=6, routing_started=time.time() - 130)
    await request_controls.sync(bridge, event_id, record)
    posted = bridge.test_slack.chat_postMessage.call_args.kwargs["blocks"]
    assert "Cancel task" in repr(posted)
    for native in (True, False):
        bridge.settings.slack_sessions = native
        await request_controls.sync(bridge, event_id, record)
        blocks = bridge.test_slack.chat_update.call_args.kwargs["blocks"]
        assert ("Cancel task" in repr(blocks)) is not native
        assert "Keep working" in repr(blocks) and "Report a problem" in repr(blocks)


@pytest.mark.parametrize("native", [False, True])
async def test_worker_manifest_carries_controller_eligibility(bridge, native):
    bridge.settings.slack_sessions = native
    event_id = await bridge.accept(payload())
    assert await bridge.process(event_id, 0) == "running"
    raw, _ = bridge.store.get(f"{CHECKPOINT_PREFIX}/events/{event_id}/request.json")
    manifest = json.loads(raw)
    assert manifest.get("native_session", False) is native
    assert manifest["notice_ts"] == "9.0"


@pytest.mark.parametrize("notice_ts", [None, "9.0"])
async def test_native_worker_defers_placeholder_until_real_progress(bot, monkeypatch, notice_ts):
    bot.native_session = True
    monkeypatch.setattr(slack_app, "STATUS_UPDATE_SECONDS", 0)
    async def run(ctx, prompt, update):
        await update(ActivityState("thinking"))
        bot.client.chat_postMessage.assert_not_called()
        bot.client.chat_update.assert_not_called()
        task = TaskProgress("one", "Reading counts", "Reading")
        await update(task)
        await update(ProgressNote("Found the table."))
        await update(replace(task, status="complete"))
        return TurnResult("session", "Finished normally")
    bot.runner.run_turn = run
    await send(bot, event("run", _notice_ts=notice_ts))
    calls = bot.client.chat_postMessage.call_args_list + bot.client.chat_update.call_args_list
    blocks = [c.kwargs["blocks"] for c in calls if c.kwargs.get("blocks")]
    assert "in_progress" not in repr(blocks)
    assert "Reading counts" in repr(blocks) and "Found the table." in repr(blocks)
    assert "Mica finished" in repr(blocks)
    assert bot.completed and bot.last_reply == "Finished normally"


async def test_native_worker_continuation_has_no_spinner(bot):
    bot.native_session = True
    bot.runner.compute = SimpleNamespace(stop=SimpleNamespace(is_set=lambda: False), pending=True)
    bot.runner.run_turn = AsyncMock(return_value=TurnResult("session", ""))
    await send(bot, event("run", _notice_ts="9.0"))
    update = bot.client.chat_update.call_args.kwargs
    assert "Saving progress" in update["text"]
    assert "in_progress" not in repr(update["blocks"])
    assert bot.continuing
