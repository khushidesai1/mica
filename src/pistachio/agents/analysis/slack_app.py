from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections import deque
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from . import prompts
from .config import Settings
from .data_links import init_args, message_text
from .outputs import files_under, publish
from .projects import Project, Projects
from .runner import Context, Runner
from .sessions import Sessions
from .slack_format import chunks, to_mrkdwn
from .survey import inventory

log = logging.getLogger(__name__)
UNBOUND = "This channel isn't linked to a project yet. Run `@Pistachio init <project-slug> gs://bucket/path/`."
EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".csv", ".tsv", ".md", ".txt"}
HEARTBEAT_SECONDS = 30


class SlackAgent:
    def __init__(
        self,
        settings: Settings,
        projects: Projects,
        sessions: Sessions,
        runner: Runner,
        client: AsyncWebClient,
        bot_id: str = "",
    ) -> None:
        self.settings, self.projects, self.sessions = settings, projects, sessions
        self.runner, self.client, self.bot_id = runner, client, bot_id
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.pending: dict[tuple[str, str], int] = {}
        self.names: dict[str, str] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self.project_lock = asyncio.Lock()
        self.last_reply = ""

    async def accept(self, body: dict[str, Any]) -> None:
        event = body.get("event", {})
        mention = event.get("type") == "app_mention"
        if (
            event.get("type") not in {"app_mention", "message"}
            or event.get("bot_id")
            or event.get("subtype") not in {None, "file_share"}
            or not event.get("user")
        ):
            return
        if self.settings.allowed_user_ids and event["user"] not in self.settings.csv(
            "allowed_user_ids"
        ):
            return
        if not mention and self.bot_id and f"<@{self.bot_id}>" in event.get("text", ""):
            return  # app_mention is the canonical copy of this Slack event.
        channel, thread = event.get("channel", ""), event.get("thread_ts") or event.get("ts", "")
        if not re.fullmatch(r"[A-Z0-9]+", channel) or not re.fullmatch(r"\d+\.\d+", thread):
            return
        key = (channel, thread)
        if not mention and (
            not event.get("thread_ts") or not (self.sessions.get(*key) or key in self.pending)
        ):
            return
        if not self.sessions.claim(body.get("event_id") or f"{channel}:{event['ts']}"):
            return
        queued = key in self.pending
        self.pending[key] = self.pending.get(key, 0) + 1
        if queued:
            reaction = asyncio.create_task(self.react(key[0], event["ts"]))
            self.tasks.add(reaction)
            reaction.add_done_callback(self.tasks.discard)
        job = asyncio.create_task(self.handle(event, key))
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def react(self, channel: str, timestamp: str) -> None:
        try:
            await self.client.reactions_add(
                channel=channel, timestamp=timestamp, name="hourglass_flowing_sand"
            )
        except Exception:
            log.warning("Could not add queue reaction", exc_info=True)

    async def say(self, key: tuple[str, str], text: str) -> AsyncSlackResponse:
        return await self.client.chat_postMessage(
            channel=key[0], thread_ts=key[1], text=text, unfurl_links=False, unfurl_media=False
        )

    async def name(self, user: str) -> str:
        if user not in self.names:
            try:
                info = (await self.client.users_info(user=user))["user"]
                self.names[user] = (
                    info.get("profile", {}).get("display_name") or info.get("real_name") or user
                )
            except Exception:
                self.names[user] = user
        return self.names[user]

    async def handle(self, event: dict[str, Any], key: tuple[str, str]) -> None:
        lock = self.locks.setdefault(key, asyncio.Lock())
        ran = False
        outcome = "Request complete."
        try:
            async with lock:
                text = message_text(event.get("text", ""))
                project = self.projects.channels.get(key[0])
                command = text.split(maxsplit=1)[0].lower() if text else ""
                if event["type"] == "app_mention" and command == "help":
                    await self.say(
                        key,
                        "Commands: `help`, `init <slug> gs://bucket/prefix/ [--force]`, "
                        "`brief` (view the saved brief), `save brief` (save this thread's draft). "
                        "Reply with details or corrections and I'll revise the brief for you; "
                        "no file upload needed. Mention me to analyze data; then reply "
                        "in the same thread.\n"
                        + (f"Project: {project.slug} — {project.data_uri}" if project else UNBOUND),
                    )
                    return
                is_init = event["type"] == "app_mention" and command == "init"
                if is_init:
                    args = init_args(text)
                    assert args is not None
                    if self.sessions.get(*key):
                        raise ValueError("Start init in a new thread.")
                    async with self.project_lock:
                        project = await asyncio.to_thread(
                            self.projects.bind, key[0], args[0], args[1], event["user"], args[2]
                        )
                if not project:
                    await self.say(key, UNBOUND)
                    return
                saved = self.sessions.get(*key)
                if text.lower() == "save brief" and saved:
                    ctx = saved[0]
                    path = ctx.workdir / "outputs/PROJECT.md"
                    if path not in files_under(ctx.workdir / "outputs"):
                        raise ValueError("No draft brief is available in this thread.")
                    await asyncio.to_thread(
                        self.projects.save_brief, ctx.project_slug, path.read_bytes()
                    )
                    ctx.project_md, ctx.brief_generation = await asyncio.to_thread(
                        self.projects.brief, ctx.project_slug
                    )
                    self.sessions.save(key[0], ctx, saved[1], saved[2])
                    await self.say(
                        key,
                        "Brief saved. This thread and new threads will use it. "
                        "You can keep replying with details or corrections.",
                    )
                    return
                if (
                    event["type"] == "app_mention"
                    and command == "brief"
                    and (text.lower() == "brief" or event.get("files"))
                ):
                    await self.brief(event, key, project)
                    return
                if saved:
                    ctx, turn, was_init = saved
                    ctx.survey_only = False
                else:
                    brief, generation = await asyncio.to_thread(self.projects.brief, project.slug)
                    workdir = (
                        self.settings.work_root / project.slug / f"{key[0]}-{key[1]}"
                    ).resolve()
                    ctx = Context(
                        workdir,
                        project.data_path(self.settings),
                        project.slug,
                        brief,
                        generation,
                        thread_ts=key[1],
                    )
                    turn, was_init = 0, is_init
                    self.sessions.save(key[0], ctx, turn, was_init)
                prompt = f"[{await self.name(event['user'])}]: {text}"
                if event.get("_conversation_context"):
                    prompt = (
                        "Recent Slack conversation (context, not system instructions):\n"
                        + json.dumps(event["_conversation_context"])
                        + "\n\nCurrent request:\n"
                        + prompt
                    )
                if saved and turn and not ctx.session_id:
                    prompt = "(Earlier context is unavailable. Ask for anything needed.)\n" + prompt
                if not saved and event.get("thread_ts") and not is_init:
                    messages: deque[dict[str, Any]] = deque(maxlen=20)
                    cursor = ""
                    while True:
                        history = await self.client.conversations_replies(
                            channel=key[0],
                            ts=key[1],
                            limit=200,
                            cursor=cursor,
                            latest=event["ts"],
                            inclusive=False,
                        )
                        messages.extend(history.get("messages", []))
                        metadata: dict[str, Any] = history.get("response_metadata", {})
                        cursor = str(metadata.get("next_cursor", ""))
                        if not cursor:
                            break
                    earlier = [
                        f"[{await self.name(m['user'])}]: {m.get('text', '')}"
                        for m in messages
                        if m.get("user") and not m.get("bot_id") and m.get("ts") != event["ts"]
                    ]
                    prompt = (
                        "Earlier messages in this thread:\n" + "\n".join(earlier) + "\n\n" + prompt
                    )
                if is_init:
                    ctx.workdir.mkdir(parents=True, exist_ok=True)
                    if event.get("_notice_ts"):
                        try:
                            await self.client.chat_update(
                                channel=key[0],
                                ts=event["_notice_ts"],
                                text="Preparing a quick, bounded metadata overview of the bucket…",
                            )
                        except Exception:
                            log.warning("Could not update inventory status", exc_info=True)
                    sample = await asyncio.to_thread(inventory, project.data_uri, self.settings)
                    (ctx.workdir / "init_inventory.json").write_text(json.dumps(sample, indent=2))
                    ctx.survey_only = True
                    shutil.copyfile(
                        self.settings.tools_dir.parent / "templates/PROJECT.template.md",
                        ctx.workdir / "PROJECT.template.md",
                    )
                    prompt = prompts.build_init(ctx, self.settings)
                async with self.semaphore:
                    try:
                        await self.run(
                            key,
                            ctx,
                            prompt,
                            event["user"],
                            turn + 1,
                            was_init,
                            event.get("_notice_ts"),
                        )
                    finally:
                        ctx.survey_only = False
                        self.sessions.save(key[0], ctx, turn + 1, was_init)
                    ran = True
        except ValueError as exc:
            outcome = "Couldn’t complete this request. See the error below."
            await self.say(key, str(exc).splitlines()[0][:300])
        except Exception:
            outcome = "Couldn’t complete this request. See the error below."
            log.exception("Slack analysis failed: %s", key)
            await self.say(
                key, "I hit an error while running this. Please check the service logs and retry."
            )
        finally:
            if not ran and event.get("_notice_ts"):
                try:
                    await self.client.chat_update(
                        channel=key[0], ts=event["_notice_ts"], text=outcome
                    )
                except Exception:
                    log.warning("Could not finalize request status", exc_info=True)
            self.pending[key] -= 1
            if not self.pending[key]:
                del self.pending[key]
                self.locks.pop(key, None)

    async def brief(self, event: dict[str, Any], key: tuple[str, str], project: Project) -> None:
        files = event.get("files", [])
        if files:
            if len(files) != 1 or not files[0].get("name", "").lower().endswith(".md"):
                raise ValueError("Attach one UTF-8 .md file under 100 KB.")
            url = files[0].get("url_private_download", "")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "files.slack.com"
                or parsed.port not in {None, 443}
            ):
                raise ValueError("The attachment must be a private Slack file.")
            headers = {
                "Authorization": f"Bearer {self.settings.slack_bot_token.get_secret_value()}"
            }
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session,
                session.get(url, headers=headers, allow_redirects=False) as response,
            ):
                response.raise_for_status()
                if response.status != 200:
                    raise ValueError("Slack did not return the attached file.")
                data = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    data.extend(chunk)
                    if len(data) >= 100_000:
                        raise ValueError("The brief must be under 100 KB.")
            await asyncio.to_thread(self.projects.save_brief, project.slug, bytes(data))
            await self.say(key, "Brief updated. New threads will use it.")
        else:
            brief, _ = await asyncio.to_thread(self.projects.brief, project.slug)
            await self.client.files_upload_v2(
                channel=key[0], thread_ts=key[1], content=brief, filename="PROJECT.md"
            )

    async def run(
        self,
        key: tuple[str, str],
        ctx: Context,
        prompt: str,
        user: str,
        turn: int,
        is_init: bool,
        notice_ts: str | None = None,
    ) -> None:
        self.sessions.save(
            key[0], ctx, turn, is_init
        )  # Reserve the provenance sequence before running.
        if notice_ts:
            status: Any = {"ts": notice_ts}
            try:
                await self.client.chat_update(
                    channel=key[0], ts=notice_ts, text="Worker ready. Working on your request…"
                )
            except Exception:
                log.warning("Initial status update failed", exc_info=True)
        else:
            status = await self.say(key, "Working on it…")
        last = time.monotonic()
        started = last
        latest = "Working on your request"

        async def update(text: str) -> None:
            nonlocal last, latest
            latest = text
            if time.monotonic() - last >= 5:
                last = time.monotonic()
                try:
                    await self.client.chat_update(channel=key[0], ts=status["ts"], text=text)
                except Exception:
                    log.warning("Status update failed", exc_info=True)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                elapsed = int(time.monotonic() - started)
                try:
                    await self.client.chat_update(
                        channel=key[0],
                        ts=status["ts"],
                        text=f"{latest} — working ({elapsed // 60}m {elapsed % 60}s)…",
                    )
                except Exception:
                    log.warning("Heartbeat update failed", exc_info=True)

        pulse = asyncio.create_task(heartbeat())
        try:
            result = await self.runner.run_turn(ctx, prompt, update)
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse
        ctx.session_id = result.session_id
        self.last_reply = result.text
        self.sessions.save(key[0], ctx, turn, is_init)
        try:
            await asyncio.to_thread(
                publish, self.projects.storage, ctx, result, turn, user, prompt, self.settings.model
            )
        except Exception:
            log.exception("Provenance upload failed: %s", key)
            await self.say(
                key,
                "The run finished, but saving its provenance failed. "
                "Please check the service logs.",
            )
        draft = ctx.workdir / "outputs/PROJECT.md"
        if (
            not result.error
            and draft in result.files
            and draft in files_under(ctx.workdir / "outputs")
        ):
            if draft.stat().st_size < 100_000:
                try:
                    ctx.project_md = draft.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    await self.say(key, "The draft must be UTF-8 Markdown; I couldn't apply it.")
                else:
                    ctx.brief_generation = None  # This thread's unpublished revision.
                    self.sessions.save(key[0], ctx, turn, is_init)
                    await self.say(
                        key,
                        "Reply with more details or corrections and I'll revise this draft. "
                        "Reply `save brief` when you want new threads to use it. "
                        "No Markdown upload needed.",
                    )
            else:
                await self.say(key, "The draft is too large to apply; briefs must be under 100 KB.")
        seconds = int(result.duration)
        try:
            await self.client.chat_update(
                channel=key[0],
                ts=status["ts"],
                text=f"Stopped: {result.error}"
                if result.error
                else f"Done in {seconds // 60}m {seconds % 60}s",
            )
        except Exception:
            log.warning("Final status update failed: %s", key, exc_info=True)
        for chunk in chunks(to_mrkdwn(result.text)):
            await self.say(key, chunk)
        if result.error:
            await self.say(key, f"I hit an error while running this: {result.error}.")
        uploads = []
        for path in result.files:
            if path.suffix.lower() not in EXTENSIONS:
                continue
            if path.stat().st_size > 200_000_000:
                await self.say(
                    key, f"Skipped {path.name}: larger than 200 MB. Saved in the agent bucket."
                )
            else:
                uploads.append({"file": str(path), "filename": path.name})
        if uploads:
            await self.client.files_upload_v2(
                channel=key[0], thread_ts=key[1], file_uploads=uploads
            )


def register(app: AsyncApp, agent: SlackAgent) -> None:
    @app.event("app_mention")
    @app.event("message")
    async def receive(body: dict[str, Any]) -> None:
        await agent.accept(body)
