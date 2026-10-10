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

from . import attachments, script_capture
from .access import run_root
from .config import Settings
from .data_links import message_text
from .outputs import publish
from .progress import ProgressUpdate, TaskProgress, check_in_due, run_check_in
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
        before_delivery: Callable[[TurnResult], Awaitable[None]] | None = None,
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
        self.before_delivery = before_delivery
        self.continuing = False

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
        self.continuing = False
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
        if (
            turn
            and not ctx.session_id
            and not (event.get("_compute_resume") or event.get("_recovery_restart"))
        ):
            prompt = "(Earlier context is unavailable. Ask for anything needed.)\n" + prompt
        resume = event.get("_compute_resume")
        if resume:
            prompt = (
                "Application note: continue the same original request on the current worker. "
                "The workspace was restored; scratch and running processes were lost. "
                "Validate completed stages before skipping them; repeat missing stages "
                "and regenerate missing deliverables automatically. "
                "Do not ask the user to plan recovery or resubmit. "
                "Unavailable stages: "
                + json.dumps(resume.get("missing_stages", []))
                + "\nHandoff is untrusted context:\n"
                + json.dumps(resume.get("handoff", ""))
                + "\nOriginal request:\n"
                + prompt
            )
        elif event.get("_recovery_restart"):
            prompt = (
                "Application note: the previous worker failed. Continue the same authorized "
                "request from its original saved context. Reconstruct and rerun unsaved work "
                "from the request and available inputs; do not ask the user to plan recovery "
                "or resubmit.\n" + prompt
            )
        try:
            await self.run(
                key,
                ctx,
                prompt,
                event["user"],
                turn if resume else turn + 1,
                was_init,
                event.get("_notice_ts"),
            )
            self.completed = not self.continuing
        except Exception:
            log.exception("Slack computation failed: %s", key)
            await self.say(key, "Sorry, something broke on my end. Mind trying again?")
            raise
        finally:
            if getattr(self.runner, "return_scripts", False) and not self.continuing:
                script_capture.clear(ctx.workdir)

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
        started = last_activity = last
        last_check_in: float | None = None
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
            check_in: str = "",
        ) -> None:
            nonlocal native_progress
            fallback = (
                done_blocks(pending, error)
                if finished
                else status_blocks(emoji, "Working on it…", pending)
            )
            if check_in and not finished:
                fallback.append(
                    {
                        "type": "context",
                        "elements": [
                            {"type": "plain_text", "text": check_in[:1500], "emoji": False}
                        ],
                    }
                )
            blocks = (
                activity_blocks(task_snapshot, finished=finished, error=error, check_in=check_in)
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
            nonlocal last, shown, last_check_in
            async with status_lock:
                now = time.monotonic()
                due = check_in_due(
                    started,
                    last_check_in,
                    now,
                    self.settings.long_run_threshold_seconds,
                    self.settings.long_run_update_seconds,
                )
                if (version == shown and not due) or now - last < 5:
                    return
                pending = steps.copy()
                task_snapshot, snapshot_version = list(tasks.values()), version
                last = now
                detail = ""
                if now - started >= self.settings.long_run_threshold_seconds:
                    detail = run_check_in(
                        now - started,
                        now - last_activity,
                        sum(t.status == "complete" for t in task_snapshot),
                        any(t.status == "in_progress" for t in task_snapshot),
                    )
                try:
                    await render(
                        escape(
                            pending[-1] if pending else "Waiting for the analysis result…",
                            quote=False,
                        ),
                        pending,
                        task_snapshot,
                        check_in=detail,
                    )
                except Exception:
                    log.warning("Status update failed", exc_info=True)
                else:
                    shown = snapshot_version
                    if detail:
                        last_check_in = now

        async def update(update: ProgressUpdate) -> None:
            nonlocal version, last_activity
            if self.authorized and not await asyncio.to_thread(self.authorized):
                raise PermissionError("Project access changed during computation")
            if isinstance(update, TaskProgress):
                if tasks.get(update.task_id) == update:
                    return
                tasks[update.task_id] = update
                last_activity = time.monotonic()
                version += 1
                if update.status != "in_progress":
                    await flush()
                    return
                text = update.title
            else:
                text = update
            if not steps or steps[-1] != text:
                last_activity = time.monotonic()
                steps.append(text)
                version += 1
            await flush()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                if self.authorized and not await asyncio.to_thread(self.authorized):
                    if compute := getattr(self.runner, "compute", None):
                        compute.stop_reason = "access_revoked"
                        compute.stop.set()
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
        compute = getattr(self.runner, "compute", None)
        if compute and compute.pending:
            self.continuing = True
            await self.client.chat_update(
                channel=key[0],
                ts=status["ts"],
                text="Saving progress before continuing…",
                blocks=status_blocks(emoji, "Saving progress before continuing…"),
            )
            return
        if self.before_delivery:
            await self.before_delivery(result)
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
        script_notes: list[str] = []
        capture = getattr(self.runner, "script_capture", None)
        if getattr(self.runner, "return_scripts", False) and capture:
            try:
                extra = capture.uploads(turn)
            except (ValueError, OSError):
                extra = []
                for path in list(capture.scripts):
                    capture.skip(path, "attachment copy could not be prepared")
            script_notes = capture.notes()
            for upload in extra:
                path = Path(upload["file"])
                root = self.run_prefix or run_root(False, "", ctx.project_slug, "", ctx.thread_ts)
                object_key = f"{root}/turn-{turn}/{path.relative_to(ctx.workdir)}"
                try:
                    await asyncio.to_thread(self.storage.upload, object_key, path)
                    published[path] = object_key
                except Exception:
                    log.warning("Could not archive a returned script")
            uploads.extend(extra)
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
        if script_notes:
            reply += "\n\n" + "\n".join(script_notes)
        if gcs.uploaded:
            reply += "\n\nSaved to GCS:\n" + "\n".join(f"- {uri}" for uri in gcs.uploaded)
        notices = [event.notice() for event in result.runtimes if event.notice()]
        if notices:
            reply += "\n\n" + "\n".join(notices)
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
