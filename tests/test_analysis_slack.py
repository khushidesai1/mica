import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.runner import Context, TurnResult
from pistachio.agents.analysis.sessions import Sessions
from pistachio.agents.analysis.slack_app import SlackAgent
from pistachio.agents.analysis.slack_format import chunks, to_mrkdwn
from pistachio.agents.analysis.storage import LocalStorage


class FakeRunner:
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


def test_cleanup_preserves_fresh_work_and_resets_old_session(bot):
    from pistachio.agents.analysis.runner import Context
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
    from pistachio.agents.analysis import slack_app

    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.005)
    await send(bot, event("plot", _notice_ts="9.0"))
    updates = bot.client.chat_update.call_args_list
    assert all(c.kwargs["ts"] == "9.0" for c in updates)
    assert any("— working" in c.kwargs["text"] for c in updates)
    assert updates[-1].kwargs["text"].startswith("Done")
    count = len(updates)
    await asyncio.sleep(0.02)
    assert bot.client.chat_update.call_count == count
    assert not any(
        c.kwargs["text"] == "Working on it…" for c in bot.client.chat_postMessage.call_args_list
    )


async def test_failed_turn_does_not_apply_partial_brief(bot):

    async def fail(ctx, prompt, status):
        (ctx.workdir / "outputs").mkdir(parents=True)
        draft = ctx.workdir / "outputs/PROJECT.md"
        draft.write_text("# Partial")
        return TurnResult("session", "", [draft], error="interrupted")

    bot.runner.run_turn = fail
    await send(bot, event("update the brief"))
    assert bot.sessions.get("C123", "1.0")[0].project_md == "# Original"


async def test_help_me_plot_is_computed(bot):
    await send(bot, event("help me plot these samples"))
    assert "help me plot" in bot.runner.calls[0][2]


async def test_missing_context_never_runs(bot):
    await send(bot, event("plot", ts="99.0"))
    assert not bot.runner.calls and not bot.completed
    assert "no saved context" in bot.client.chat_postMessage.call_args.kwargs["text"]


async def test_draft_revision_preserves_brief_base_generation(bot):
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
    assert saved.project_md == "Revised draft"
    assert saved.brief_generation == "original-generation"
