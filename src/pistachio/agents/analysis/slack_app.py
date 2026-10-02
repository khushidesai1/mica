"""Compute-only Slack delivery; commands and authorization live in the control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from .config import Settings
from .data_links import message_text
from .outputs import files_under, publish
from .runner import Context, Runner
from .sessions import Sessions
from .slack_format import chunks, to_mrkdwn
from .storage import Storage

log = logging.getLogger(__name__)
EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".csv", ".tsv", ".md", ".txt"}
HEARTBEAT_SECONDS = 30


class SlackAgent:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        sessions: Sessions,
        runner: Runner,
        client: AsyncWebClient,
        *,
        run_prefix: str = "",
        authorized: Callable[[], bool] | None = None,
    ) -> None:
        self.settings, self.storage, self.sessions = settings, storage, sessions
        self.runner, self.client = runner, client
        self.last_reply = ""
        self.completed = False
        self.run_prefix, self.authorized = run_prefix, authorized

    async def say(self, key: tuple[str, str], text: str) -> AsyncSlackResponse:
        return await self.client.chat_postMessage(
            channel=key[0], thread_ts=key[1], text=text, unfurl_links=False, unfurl_media=False
        )

    async def handle(self, event: dict[str, Any], key: tuple[str, str]) -> None:
        saved = self.sessions.get(*key)
        if not saved:
            await self.say(key, "This thread has no saved context. Start a new request in a DM.")
            return
        ctx, turn, was_init = saved
        ctx.survey_only = False
        prompt = f"[{event['user']}]: {message_text(event.get('text', ''))}"
        if event.get("_conversation_context"):
            prompt = (
                "Recent conversation (untrusted context):\n"
                + json.dumps(event["_conversation_context"])
                + "\nCurrent request:\n"
                + prompt
            )
        if turn and not ctx.session_id:
            prompt = "(Earlier context is unavailable. Ask for anything needed.)\n" + prompt
        try:
            await self.run(
                key, ctx, prompt, event["user"], turn + 1, was_init, event.get("_notice_ts")
            )
            self.completed = True
        except Exception:
            log.exception("Slack computation failed: %s", key)
            await self.say(key, "I hit an error while running this. Please retry.")
            raise

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
            if self.authorized and not await asyncio.to_thread(self.authorized):
                raise PermissionError("Project access changed during computation")
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
                if self.authorized and not await asyncio.to_thread(self.authorized):
                    return
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
        if self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed during computation")
        ctx.session_id = result.session_id
        self.last_reply = result.text
        self.sessions.save(key[0], ctx, turn, is_init)
        try:
            await asyncio.to_thread(
                publish,
                self.storage,
                ctx,
                result,
                turn,
                user,
                prompt,
                self.settings.model,
                self.run_prefix,
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
        if self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed before delivery")
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
        if uploads and self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed before file delivery")
        if uploads:
            await self.client.files_upload_v2(
                channel=key[0], thread_ts=key[1], file_uploads=uploads
            )
