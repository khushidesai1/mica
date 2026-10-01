import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.projects import Projects
from pistachio.agents.analysis.runner import TurnResult
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
    projects = Projects(LocalStorage(tmp_path / "storage"), settings)
    sessions = Sessions(settings.state_db)
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1.2"}
    client.users_info.return_value = {"user": {"profile": {"display_name": "Alice"}}}
    client.conversations_replies.return_value = {"messages": []}
    agent = SlackAgent(settings, projects, sessions, FakeRunner(), client, "BOT")
    yield agent
    sessions.close()


def event(text, ts="1.0", thread=None, kind="app_mention", channel="C123", **extra):
    data = {"type": kind, "text": text, "ts": ts, "channel": channel, "user": "U123", **extra}
    if thread:
        data["thread_ts"] = thread
    return {"event_id": f"{channel}-{ts}-{kind}", "event": data}


async def send(bot, body):
    await bot.accept(body)
    if bot.tasks:
        await asyncio.gather(*bot.tasks)


async def test_init_save_and_frozen_brief(bot):
    await send(bot, event("init liver gs://lab-data/liver/"))
    assert bot.projects.channels["C123"].slug == "liver"
    assert bot.runner.calls[0][2].startswith("Survey the data")
    await send(bot, event("save brief", "2.0", "1.0", "message"))
    assert bot.projects.brief("liver")[0] == "# Draft"
    await send(bot, event("plot", "3.0"))
    bot.projects.save_brief("liver", b"# New")
    await send(bot, event("bigger", "4.0", "3.0", "message"))
    assert bot.runner.calls[-1][0] == "session" and bot.runner.calls[-1][1] == "# Draft"
    await send(bot, event("plot again", "5.0"))
    assert bot.runner.calls[-1][1] == "# New"
    assert bot.client.files_upload_v2.await_count == 4
    meta = json.loads(bot.projects.storage.get("projects/liver/runs/3.0/turn-2/meta.json")[0])
    assert meta["session_id"] == "session" and meta["user_id"] == "U123"


async def test_routing_deduplication_and_restart(bot):
    await send(bot, event("hello"))
    assert (
        not bot.runner.calls
        and "isn't linked" in bot.client.chat_postMessage.call_args.kwargs["text"]
    )
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    body = event("plot", "2.0")
    await send(bot, body)
    await send(bot, body)
    bot.sessions.close()
    bot.sessions = Sessions(bot.settings.state_db)
    await send(bot, body)
    await send(bot, event("hi", "3.0", kind="message"))
    await send(bot, event("<@BOT> plot", "4.0", "2.0", "message"))
    await send(bot, event("hi", "5.0", "2.0", "message", bot_id="other"))
    assert len(bot.runner.calls) == 1
    await send(bot, event("bigger", "6.0", "2.0", "message"))
    assert bot.runner.calls[-1][0] == "session"


async def test_serial_threads_parallel_channels(bot):
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    bot.projects.bind("C456", "brain", "gs://lab-data/brain/", "U123")
    await bot.accept(event("first"))
    await bot.accept(event("second", "2.0", "1.0", "message"))
    await bot.accept(event("other", channel="C456"))
    await asyncio.gather(*bot.tasks)
    assert bot.runner.maximum == 2
    same = [call for call in bot.runner.calls if "C123" in str(call[3])]
    assert len(same) == 2 and same[1][0] == "session"
    bot.client.reactions_add.assert_awaited_once()


async def test_allowlist_and_history(bot):
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    bot.settings.allowed_user_ids = "U456"
    await send(bot, event("hi"))
    assert not bot.runner.calls
    bot.settings.allowed_user_ids = ""
    bot.client.conversations_replies.return_value = {
        "messages": [{"user": "U456", "text": "sample B", "ts": "1.0"}]
    }
    await send(bot, event("plot", "2.0", "1.0"))
    assert "Earlier messages in this thread" in bot.runner.calls[0][2]
    assert "sample B" in bot.runner.calls[0][2]


def test_project_boundaries(bot):
    for slug, uri in [
        ("../evil", "gs://lab-data/"),
        ("liver", "gs://other/data/"),
        ("liver", "gs://lab-data/../escape"),
        ("liver", "gs://lab-data/%2e%2e/x"),
    ]:
        with pytest.raises(ValueError):
            bot.projects.bind("C123", slug, uri, "U123")
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    with pytest.raises(ValueError):
        bot.projects.bind("C456", "liver", "gs://lab-data/liver/", "U123", True)
    with pytest.raises(ValueError):
        bot.projects.save_brief("liver", b"x" * 100_000)
    with pytest.raises(UnicodeDecodeError):
        bot.projects.save_brief("liver", b"\xff")
    bot.projects.bind("C123", "brain", "gs://lab-data/brain/", "U123", True)
    bot.projects.reload()
    assert bot.projects.channels["C123"].slug == "brain"


def test_formatting():
    text = to_mrkdwn("# Title\n**bold** [source](https://example.org) `**literal**` <@all>")
    assert text == "Title\n*bold* <https://example.org|source> `**literal**` &lt;@all&gt;"
    assert all(len(part) <= 3500 for part in chunks("x" * 8000))


async def test_slow_reactions_cannot_reorder_requests(bot):
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")

    async def reaction(**kwargs):
        if kwargs["timestamp"] == "2.0":
            await asyncio.sleep(0.1)

    bot.client.reactions_add.side_effect = reaction
    await bot.accept(event("first"))
    await bot.accept(event("second", "2.0", "1.0", "message"))
    await bot.accept(event("third", "3.0", "1.0", "message"))
    await asyncio.gather(*bot.tasks)
    assert [c[2] for c in bot.runner.calls] == [
        "[Alice]: first",
        "[Alice]: second",
        "[Alice]: third",
    ]


async def test_brief_upload_and_untrusted_url(bot, monkeypatch):

    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    await send(bot, event("plot"))

    class Response:
        status = 200

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            yield b"# Uploaded brief"

        @property
        def content(self):
            return self

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            assert not kwargs["allow_redirects"] and "Authorization" in kwargs["headers"]
            return Response()

    monkeypatch.setattr("pistachio.agents.analysis.slack_app.aiohttp.ClientSession", Session)
    files = [{"name": "PROJECT.md", "url_private_download": "https://files.slack.com/example"}]
    await send(bot, event("brief", "2.0", files=files, subtype="file_share"))
    assert bot.projects.brief("liver")[0] == "# Uploaded brief"
    await send(bot, event("again", "3.0", "1.0", "message"))
    assert "No brief yet" in bot.runner.calls[-1][1]
    files[0]["url_private_download"] = "https://attacker.example/file"
    await send(bot, event("brief", "4.0", files=files))
    assert "private Slack file" in bot.client.chat_postMessage.call_args.kwargs["text"]


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


async def test_last_twenty_history_messages_and_status_failure(bot):
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    bot.client.conversations_replies.side_effect = [
        {
            "messages": [{"user": "U123", "text": "old context", "ts": "0.1"}],
            "response_metadata": {"next_cursor": "next"},
        },
        {"messages": [{"user": "U123", "text": f"recent {i}", "ts": f"1.{i}"} for i in range(20)]},
    ]
    bot.client.chat_update.side_effect = RuntimeError("Slack status update failed")
    await send(bot, event("plot", "2.0", "1.0"))
    assert "old context" not in bot.runner.calls[0][2]
    assert "recent 0" in bot.runner.calls[0][2] and "recent 19" in bot.runner.calls[0][2]
    bot.client.files_upload_v2.assert_awaited_once()
    assert not bot.pending and not bot.locks


async def test_receipt_status_reused_and_heartbeat_stops(bot, monkeypatch):
    from pistachio.agents.analysis import slack_app

    monkeypatch.setattr(slack_app, "HEARTBEAT_SECONDS", 0.005)
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
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


async def test_invalid_init_updates_existing_receipt(bot):
    await send(bot, event("init liver gs://unavailable-bucket/", _notice_ts="9.0"))
    assert "Couldn’t complete" in bot.client.chat_update.call_args.kwargs["text"]
    assert not bot.runner.calls


async def test_interrupted_init_binding_can_be_retried(bot):
    first = bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    bot.projects.save_brief("liver", b"Existing brief")
    await send(bot, event("init liver gs://lab-data/liver/"))
    assert bot.projects.channels["C123"].created_at == first.created_at
    assert bot.projects.brief("liver")[0] == "Existing brief"
    assert bot.runner.calls
    context, _, _ = bot.sessions.get("C123", "1.0")
    assert not context.survey_only
    assert (context.workdir / "init_inventory.json").exists()


@pytest.mark.parametrize("init_thread", [True, False])
async def test_conversational_brief_revision_survives_restart_and_can_be_saved(bot, init_thread):
    if init_thread:
        await send(bot, event("init liver gs://lab-data/liver/"))
    else:
        bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
        bot.projects.save_brief("liver", b"# Original\nSpecies: TODO")
        await send(bot, event("plot"))
    shared_before = bot.projects.brief("liver")[0]

    async def revise(ctx, prompt, status):
        assert "samples are human" in prompt
        draft = ctx.workdir / "outputs/PROJECT.md"
        draft.write_text("# Revised\nSpecies: human (provided by user)\nCell count: TODO")
        return TurnResult("session", "Updated the species; cell count is still unknown.", [draft])

    bot.runner.run_turn = revise
    await send(bot, event("brief update: the samples are human", "2.0", "1.0"))
    bot.sessions.close()
    bot.sessions = Sessions(bot.settings.state_db)
    ctx = bot.sessions.get("C123", "1.0")[0]
    assert "Species: human" in ctx.project_md
    assert ctx.brief_generation is None
    assert bot.projects.brief("liver")[0] == shared_before
    assert any(
        "No Markdown upload needed" in c.kwargs["text"]
        for c in bot.client.chat_postMessage.call_args_list
    )
    await send(bot, event("save brief", "3.0", "1.0", "message"))
    assert bot.projects.brief("liver")[0] == ctx.project_md
    assert bot.sessions.get("C123", "1.0")[0].brief_generation is not None


async def test_failed_turn_does_not_apply_partial_brief(bot):
    bot.projects.bind("C123", "liver", "gs://lab-data/liver/", "U123")
    bot.projects.save_brief("liver", b"# Original")

    async def fail(ctx, prompt, status):
        (ctx.workdir / "outputs").mkdir(parents=True)
        draft = ctx.workdir / "outputs/PROJECT.md"
        draft.write_text("# Partial")
        return TurnResult("session", "", [draft], error="interrupted")

    bot.runner.run_turn = fail
    await send(bot, event("update the brief"))
    assert bot.sessions.get("C123", "1.0")[0].project_md == "# Original"
    assert bot.projects.brief("liver")[0] == "# Original"
