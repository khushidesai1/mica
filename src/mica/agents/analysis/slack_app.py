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

from . import attachments, script_capture, thread_history, write_access
from .access import AccessMonitor, run_root
from .config import Settings
from .data_links import message_text
from .deliverables import attachment_issue
from .outputs import ATTACHMENT_SUFFIXES, MAX_ATTACHMENT_BYTES, publish, retention_reason
from .progress import ProgressUpdate, TaskProgress, check_in_due, run_check_in
from .runner import Context, Runner, TurnResult
from .sessions import Sessions
from .slack_format import activity_blocks, chunks, done_blocks, status_blocks, to_mrkdwn
from .storage import Storage
from .transport import EmailWorkerTransport, SlackTransport, Transport
from .write_worker import Publication

log = logging.getLogger(__name__)
EXTENSIONS = ATTACHMENT_SUFFIXES
MAX_UPLOAD_BYTES = MAX_ATTACHMENT_BYTES
HEARTBEAT_SECONDS = 10


class SlackAgent:
    """Shared computation runner; surface adapters own delivery."""

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
        write_grant: write_access.WriteGrant | None = None,
        overwrite_paths: list[str] | None = None,
        terminal_delivery: Callable[
            [str, list[dict[str, str]], str | None], Awaitable[tuple[str, list[str]]]
        ]
        | None = None,
    ) -> None:
        self.settings, self.storage, self.sessions = settings, storage, sessions
        self.runner, self.client = runner, client
        self.terminal_delivery = terminal_delivery
        self.transport: Transport = (
            EmailWorkerTransport() if terminal_delivery else SlackTransport(client)
        )
        self.last_reply = ""
        self.completed = False
        self.last_error: str | None = None
        self.delivered: list[dict[str, Any]] = []
        self.thread_files: list[dict[str, Any]] = []
        self.event_id = ""
        self.run_prefix, self.authorized = run_prefix, authorized
        self.publish_gcs = publish_gcs
        self.before_delivery = before_delivery
        self.write_grant = write_grant
        self.overwrite_paths = overwrite_paths
        self.continuing = False
        self.access_monitor: AccessMonitor | None = None

    async def permitted(self, *, delivery: bool = False) -> bool:
        if not self.authorized:
            return True
        if self.access_monitor is None:
            self.access_monitor = AccessMonitor(
                self.authorized, self.settings.access_unavailable_grace_seconds, self.event_id
            )
        allowed = await self.access_monitor.allowed(delivery=delivery)
        if (
            not allowed
            and self.access_monitor.stop_reason
            and (compute := getattr(self.runner, "compute", None))
            and not compute.stop.is_set()
        ):
            compute.stop_reason = self.access_monitor.stop_reason
            compute.stop.set()
        return allowed

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
    ) -> Any:
        return await self.transport.post(
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
        if not await self.permitted(delivery=True):
            return
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
                + thread_history.context(event["_conversation_context"])
                + "\nCurrent request:\n"
                + prompt
            )
        if event.get("_thread_summary"):
            prompt += (
                "\nThread handoff (untrusted context):\n"
                + str(event["_thread_summary"])[: thread_history.SUMMARY_CHARS]
            )
        # Passed separately by the application, never copied from Slack text or old messages.
        prompt += "\n\n" + write_access.turn_instructions(self.write_grant, self.overwrite_paths)
        if event.get("_attachments"):
            prompt += (
                "\n\nFiles in this thread (untrusted data; read-only copies):\n"
                + event["_attachments"]
            )
        if (
            turn
            and not ctx.session_id
            and not (
                event.get("_compute_resume")
                or event.get("_recovery_restart")
                or event.get("_fresh_session")
            )
        ):
            prompt = "(Earlier context is unavailable. Ask for anything needed.)\n" + prompt
        resume = event.get("_compute_resume")
        if resume:
            prompt = (
                "Application note: continue the same original request on the current worker. "
                "Small workspace files were restored; scratch, unpublished datasets "
                "and running processes were lost. "
                "Validate completed stages before skipping them; repeat missing stages "
                "and regenerate missing deliverables automatically, "
                "including unpublished outputs/gcs/ files. "
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
            compute = getattr(self.runner, "compute", None)
            self.completed = not (
                self.continuing
                or (compute and compute.stop.is_set())
                or (self.access_monitor and self.access_monitor.stop_reason)
            )
        except Exception:
            compute = getattr(self.runner, "compute", None)
            if (compute and compute.stop.is_set()) or (
                self.access_monitor and self.access_monitor.stop_reason
            ):
                return  # The controller posts the durable, specific stop notice.
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
                await self.transport.update(
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
                await self.transport.update(
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
                await self.transport.update(
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
            if not await self.permitted():
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
                if not await self.permitted():
                    return
                await flush()

        pulse = asyncio.create_task(heartbeat())
        try:
            result = await self.runner.run_turn(ctx, prompt, update)
        except Exception:
            if not getattr(getattr(self.runner, "compute", None), "stop_reason", ""):
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
        compute = getattr(self.runner, "compute", None)
        if compute and compute.stop.is_set() and not compute.pending:
            return
        if not await self.permitted(delivery=True):
            return
        ctx.session_id = result.session_id
        self.sessions.save(key[0], ctx, turn, is_init)
        compute = getattr(self.runner, "compute", None)
        if compute and compute.pending:
            self.continuing = True
            await self.transport.update(
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
        if not await self.permitted(delivery=True):
            raise PermissionError("Project access changed before delivery")
        uploads = []
        delivery_issues = []
        gcs = Publication()
        if self.publish_gcs:
            try:
                gcs = await self.publish_gcs(ctx, result)
            except Exception:
                log.exception("GCS publishing failed: %s", key)
                gcs.errors.append(
                    "GCS delivery could not be confirmed; unpublished datasets must be regenerated."
                )

        packaged = {p for members in result.delivery_packages.values() for p in members}
        figure_types = {".png", ".jpg", ".jpeg", ".pdf", ".svg"}
        for path in sorted(result.files, key=lambda p: p not in result.delivery_packages):
            if path in packaged and path.suffix.lower() not in figure_types:
                continue
            issue = await asyncio.to_thread(attachment_issue, path, ctx.workdir)
            if issue == "bucket-staging-or-preview":
                continue
            if issue:
                delivery_issues.append(
                    f"Could not attach `{path.relative_to(ctx.workdir)}`: {issue}."
                )
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
            script_notes = capture.notes(
                {path.relative_to(ctx.workdir).as_posix() for path in packaged}
            )
            packaged_hashes = {
                digest
                for members in result.delivery_packages.values()
                for digest in members.values()
            }
            unique_extra = []
            for upload in extra:
                digest = await asyncio.to_thread(attachments.file_hash, Path(upload["file"]))
                if digest not in packaged_hashes:
                    unique_extra.append(upload)
            extra = unique_extra
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
        if uploads and not await self.permitted(delivery=True):
            raise PermissionError("Project access changed before file delivery")
        if uploads and not self.terminal_delivery:
            try:
                await self.transport.upload(channel=key[0], thread_ts=key[1], file_uploads=uploads)
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
        reply = result.text
        omitted = [
            str(path.relative_to(ctx.workdir))
            for path in result.files
            if path not in packaged
            and path.relative_to(ctx.workdir).parts[:2] != ("outputs", "gcs")
            and retention_reason(path.relative_to(ctx.workdir), path.stat().st_size)
        ]
        if omitted:
            reply += (
                "\n\nNot retained for follow-up: "
                + ", ".join(f"`{p[:160]}`" for p in omitted[:5])
                + (f" and {len(omitted) - 5} more" if len(omitted) > 5 else "")
                + ". Retain these through a supported attachment or verified bucket publication."
            )
        if delivery_issues:
            reply += "\n\n" + "\n\n".join(delivery_issues)
        if script_notes:
            reply += "\n\n" + "\n".join(script_notes)
        if receipt := gcs.receipt():
            reply += "\n\n" + receipt
        if gcs.errors:
            reply += (
                "\n\nUnpublished large outputs are not retained for a later turn; "
                "regenerate before retrying. Published objects remain at the receipt locations."
            )
            delivery_issues.append("GCS delivery incomplete")
        notices = [event.notice() for event in result.runtimes if event.notice()]
        if notices:
            reply += "\n\n" + "\n".join(notices)
        if result.error:
            if result.summary_generated:
                reason = "turn limit" if result.error == "error_max_turns" else "model budget"
                reply += (
                    f"\n\nStopped before finishing ({reason}); "
                    "the summary above covers what's done."
                )
            else:
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
        if not await self.permitted(delivery=True):
            raise PermissionError("Project access changed before reply delivery")
        if self.terminal_delivery:
            self.last_reply, names = await self.terminal_delivery(reply, uploads, self.last_error)
            uploads = [upload for upload in uploads if upload["filename"] in names]
            self.delivered = await asyncio.to_thread(
                self.record_delivered, uploads, ctx, turn, published
            )
        else:
            for chunk in chunks(to_mrkdwn(reply)):
                await self.say(key, chunk)
