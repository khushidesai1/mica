"""Compute-only Slack delivery; commands and authorization live in the control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from html import escape
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from .config import Settings
from .data_links import message_text
from .outputs import files_under, publish
from .progress import ProgressUpdate, TaskProgress
from .runner import Context, Runner
from .sessions import Sessions
from .slack_format import activity_blocks, chunks, done_blocks, status_blocks, to_mrkdwn
from .storage import Storage

log = logging.getLogger(__name__)
EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".csv", ".tsv", ".md", ".txt"}
HEARTBEAT_SECONDS = 10


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

    async def say(
        self, key: tuple[str, str], text: str, blocks: list[dict[str, Any]] | None = None
    ) -> AsyncSlackResponse:
        return await self.client.chat_postMessage(
            channel=key[0],
            thread_ts=key[1],
            text=text,
            unfurl_links=False,
            unfurl_media=False,
            blocks=blocks,
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
            await self.say(key, "Sorry, something broke on my end. Mind trying again?")
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
        emoji = self.settings.loading_emoji
        if notice_ts:
            status: Any = {"ts": notice_ts}
            try:
                await self.client.chat_update(
                    channel=key[0],
                    ts=notice_ts,
                    text="Working on it…",
                    blocks=status_blocks(emoji, "Working on it…"),
                )
            except Exception:
                log.warning("Initial status update failed", exc_info=True)
        else:
            status = await self.say(key, "Working on it…", status_blocks(emoji, "Working on it…"))
        last = time.monotonic()
        steps: list[str] = []
        tasks: dict[str, TaskProgress] = {}
        version = 0
        shown = 0
        native_progress = True
        status_lock = asyncio.Lock()

        async def render(
            text: str,
            pending: list[str],
            task_snapshot: list[TaskProgress],
            *,
            finished: bool = False,
            error: str | None = None,
        ) -> None:
            nonlocal native_progress
            fallback = (
                done_blocks(pending, error)
                if finished
                else status_blocks(emoji, "Working on it…", pending)
            )
            blocks = (
                activity_blocks(task_snapshot, finished=finished, error=error)
                if native_progress and task_snapshot
                else fallback
            )
            try:
                await self.client.chat_update(
                    channel=key[0],
                    ts=status["ts"],
                    text=text,
                    blocks=blocks,
                )
            except SlackApiError as exc:
                if not (
                    native_progress
                    and task_snapshot
                    and exc.response.get("error") == "invalid_blocks"
                ):
                    raise
                # Older Slack surfaces may reject plan blocks. Keep the answer usable.
                native_progress = False
                log.warning("Slack rejected native progress; using compact status")
                await self.client.chat_update(
                    channel=key[0],
                    ts=status["ts"],
                    text=text,
                    blocks=fallback,
                )

        async def flush() -> None:
            nonlocal last, shown
            async with status_lock:
                if version == shown or time.monotonic() - last < 5:
                    return
                pending = steps.copy()
                task_snapshot, snapshot_version = list(tasks.values()), version
                last = time.monotonic()
                try:
                    await render(
                        escape(pending[-1], quote=False),
                        pending,
                        task_snapshot,
                    )
                except Exception:
                    log.warning("Status update failed", exc_info=True)
                else:
                    shown = snapshot_version

        async def update(update: ProgressUpdate) -> None:
            nonlocal version
            if self.authorized and not await asyncio.to_thread(self.authorized):
                raise PermissionError("Project access changed during computation")
            if isinstance(update, TaskProgress):
                if tasks.get(update.task_id) == update:
                    return
                tasks[update.task_id] = update
                version += 1
                if update.status != "in_progress":
                    await flush()
                    return
                text = update.title
            else:
                text = update
            if not steps or steps[-1] != text:
                steps.append(text)
                version += 1
            await flush()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                if self.authorized and not await asyncio.to_thread(self.authorized):
                    return
                await flush()

        pulse = asyncio.create_task(heartbeat())
        try:
            result = await self.runner.run_turn(ctx, prompt, update)
        except Exception:
            if not self.authorized or await asyncio.to_thread(self.authorized):
                try:
                    async with status_lock:
                        await render(
                            "Stopped",
                            steps.copy(),
                            list(tasks.values()),
                            finished=True,
                            error="The run was interrupted",
                        )
                except Exception:
                    log.warning("Interrupted status update failed", exc_info=True)
            raise
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
                        "Updated this thread's draft brief. Send more tweaks anytime, "
                        "or say `save brief` to share it.",
                    )
            else:
                await self.say(key, "The draft is too large to apply; briefs must be under 100 KB.")
        try:
            await render(
                "Stopped" if result.error else "Finished",
                steps,
                list(tasks.values()),
                finished=True,
                error=result.error,
            )
        except Exception:
            log.warning("Final status update failed: %s", key, exc_info=True)
        if self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed before delivery")
        for chunk in chunks(to_mrkdwn(result.text)):
            await self.say(key, chunk)
        if result.error:
            await self.say(key, f"That run stopped early (`{result.error}`). Want me to try again?")
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
