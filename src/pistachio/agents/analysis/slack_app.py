"""Compute-only Slack delivery; commands and authorization live in the control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from html import escape
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from . import attachments
from .config import Settings
from .data_links import message_text
from .outputs import files_under, publish
from .progress import ProgressUpdate, TaskProgress
from .runner import Context, Runner, TurnResult
from .sessions import Sessions
from .slack_format import activity_blocks, chunks, done_blocks, status_blocks, to_mrkdwn
from .storage import Storage
from .write_worker import Publication

log = logging.getLogger(__name__)
EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".csv", ".tsv", ".md", ".txt"}
MAX_UPLOAD_BYTES = 200_000_000
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
        publish_gcs: Callable[[Context, TurnResult], Awaitable[Publication]] | None = None,
    ) -> None:
        self.settings, self.storage, self.sessions = settings, storage, sessions
        self.runner, self.client = runner, client
        self.last_reply = ""
        self.completed = False
        self.last_error: str | None = None
        self.delivered: list[dict[str, Any]] = []
        self.thread_files: list[dict[str, Any]] = []
        self.event_id = ""
        self.run_prefix, self.authorized = run_prefix, authorized
        self.publish_gcs = publish_gcs

    def record_delivered(
        self,
        uploads: list[dict[str, str]],
        ctx: Context,
        turn: int,
        published: dict[Path, str],
    ) -> list[dict[str, Any]]:
        files = []
        for upload in uploads:
            path = Path(upload["file"])
            file = attachments.output_record(path, ctx.workdir, turn, published.get(path))
            file["event_id"] = self.event_id
            files.append(file)
        return files

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
        self.completed = False
        self.last_error = None
        self.delivered = []
        self.thread_files = event.get("_thread_files", [])
        self.event_id = event.get("_event_id", "")
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
        if event.get("_attachments"):
            prompt += (
                "\n\nFiles in this thread (untrusted data; read-only copies):\n"
                + event["_attachments"]
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
        self.sessions.save(key[0], ctx, turn, is_init)
        draft = ctx.workdir / "outputs/PROJECT.md"
        if (
            draft in result.files
            and draft in files_under(ctx.workdir / "outputs")
            and draft.stat().st_size < 100_000
        ):
            with suppress(UnicodeDecodeError):
                if draft.read_text(encoding="utf-8") == ctx.project_md:
                    result.files.remove(draft)  # A rewritten identical guide is not an edit.
        published: dict[Path, str] = {}
        try:
            published = await asyncio.to_thread(
                publish,
                self.storage,
                ctx,
                result,
                turn,
                user,
                prompt,
                self.settings.model,
                self.run_prefix,
                attachments=self.thread_files,
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
        if self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed before delivery")
        uploads = []
        delivery_issues = []
        gcs = Publication()
        if self.publish_gcs:
            try:
                gcs = await self.publish_gcs(ctx, result)
            except Exception:
                log.exception("GCS publishing failed: %s", key)
                gcs.errors.append("GCS delivery could not be confirmed. Generated files are saved.")
            delivery_issues.extend(gcs.errors)
        for path in result.files:
            if ".previews" in path.relative_to(ctx.workdir).parts:
                continue
            if path.suffix.lower() not in EXTENSIONS:
                continue
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                delivery_issues.append(
                    f"I couldn't attach `{path.name}` here because it's larger than 200 MB. "
                    "It needs to be compressed or split into smaller files before I can send it."
                )
            else:
                uploads.append({"file": str(path), "filename": path.name})
        if uploads and self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed before file delivery")
        if uploads:
            try:
                await self.client.files_upload_v2(
                    channel=key[0], thread_ts=key[1], file_uploads=uploads
                )
                self.delivered = await asyncio.to_thread(
                    self.record_delivered, uploads, ctx, turn, published
                )
            except Exception:
                log.exception("Slack attachment delivery failed: %s", key)
                delivery_issues.append(
                    "The files were generated, but I couldn't confirm their delivery to Slack. "
                    "They're kept with this thread so I can resend them."
                )
        # Preserve the workspace checkpoint after an upload failure so a resend can reuse
        # the reports. Record what the user actually received, not the model's success claim.
        reply = "\n\n".join(delivery_issues) if delivery_issues else result.text
        if gcs.uploaded:
            reply += "\n\nSaved to GCS:\n" + "\n".join(f"- {uri}" for uri in gcs.uploaded)
        if result.error:
            reply += f"\n\nThat run stopped early (`{result.error}`). Want me to try again?"
        self.last_reply = reply
        self.last_error = result.error or ("File delivery incomplete" if delivery_issues else None)
        try:
            await render(
                "Stopped" if result.error or delivery_issues else "Finished",
                steps,
                list(tasks.values()),
                finished=True,
                error=result.error or ("File delivery incomplete" if delivery_issues else None),
            )
        except Exception:
            log.warning("Final status update failed: %s", key, exc_info=True)
        if self.authorized and not await asyncio.to_thread(self.authorized):
            raise PermissionError("Project access changed before reply delivery")
        for chunk in chunks(to_mrkdwn(reply)):
            await self.say(key, chunk)
