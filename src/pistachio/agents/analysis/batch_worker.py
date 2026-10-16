"""Execute one attempt of a logical analysis, preserving progress between workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import tarfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from google.api_core.exceptions import NotFound
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from slack_sdk.web.async_client import AsyncWebClient

from . import attachments, memory_workspace, project_memory, write_access, write_worker
from .access import AccessMonitor, run_root, worker_permitted
from .checkpoints import Stages, digest, workspace_snapshot
from .config import CHECKPOINT_PREFIX as PREFIX
from .config import Settings
from .direct import record_exchange
from .outputs import files_under
from .preflight import check
from .recovery import event_prefix
from .recovery_tools import RecoveryManager
from .resources import Sampler
from .runner import Context, Runner, TurnResult
from .runtime_tools import RuntimeManager
from .sessions import Sessions
from .slack_app import SlackAgent
from .slack_format import status_blocks
from .storage import GCSStorage


def arm_stop_deadline() -> None:
    """Bound process exit even if an SDK or executor thread ignores cancellation."""
    timer = threading.Timer(45, os._exit, args=(0,))
    timer.daemon = True
    timer.start()


def restore(archive: Path, home: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        seen: set[str] = set()
        for member in members:
            target = home / member.name
            if (
                not target.resolve().is_relative_to(home.resolve())
                or member.name in seen
                or not (member.isfile() or member.isdir())
                or not (
                    member.name.startswith(("work/", ".claude/projects/"))
                    or member.name == "state.db"
                )
            ):
                raise ValueError("Unsafe session checkpoint")
            seen.add(member.name)
        tar.extractall(home, filter="data")


def snapshot(archive: Path, home: Path, *, published: set[Path] | None = None) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        paths = [*files_under(home / "work"), *files_under(home / ".claude/projects")]
        if (home / "state.db").is_file():
            paths.append(home / "state.db")
        for path in paths:
            if path in (published or set()) or "knowledge" in path.relative_to(home).parts:
                continue
            # Sources expire independently; rehydrate inputs from their original objects.
            if any(part in {".previews", "inputs"} for part in path.relative_to(home).parts):
                continue
            tar.add(path, arcname=str(path.relative_to(home)), recursive=False)


async def main(event_id: str, *, hard_stop: bool = False) -> None:
    settings = Settings()
    storage = GCSStorage(settings.bucket)
    request = json.loads(storage.get(f"{PREFIX}/events/{event_id}/request.json")[0])
    thread_key = request["thread_key"]
    home = settings.work_root.parent
    archive = home / "checkpoint.tar.gz"
    # Retain the turn record when workspace archives expire; refresh shared knowledge below.
    pointer = f"projects/_threads/{thread_key}/checkpoint.json"
    try:
        saved: dict[str, Any] = json.loads(storage.get(pointer)[0])
    except FileNotFoundError:
        saved = {}
    worker = request.get("recovery", {})
    attempt = int(worker.get("attempt", 0))
    tier = str(worker.get("tier", "standard"))
    if attempt and request.get("turn_start"):
        saved = dict(request["turn_start"])
    resume = request.get("resume")
    if resume:
        if resume.get("event_id") != event_id or resume.get("attempt") != attempt - 1:
            raise ValueError("Continuation belongs to a different request or attempt")
        saved.update(
            resume["state"],
            archive=resume["archive"],
            runtimes=resume.get("runtimes", []),
            sdk_cost_total=resume.get("sdk_cost_total"),
        )
    url = os.environ["PISTACHIO_CONTROL_PLANE_URL"].rstrip("/")
    token = await asyncio.to_thread(id_token.fetch_id_token, GoogleRequest(), url)  # type: ignore[no-untyped-call]
    async with httpx.AsyncClient(timeout=30) as client:
        deadline = time.monotonic() + settings.access_unavailable_grace_seconds
        delay = 1
        while True:
            response = await client.get(
                f"{url}/api/v1/internal/slack-analysis/credentials/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 503 or "identity verification" not in response.text:
                response.raise_for_status()
                keys = response.json()
                break
            if time.monotonic() >= deadline:
                storage.put(
                    f"{PREFIX}/events/{event_id}/attempt-a{attempt}.json",
                    json.dumps(
                        {
                            "event_id": event_id,
                            "attempt": attempt,
                            "outcome": "stopped",
                            "stop_reason": "access_unverifiable",
                        }
                    ).encode(),
                )
                return
            await asyncio.sleep(min(delay, deadline - time.monotonic()))
            delay = min(delay * 2, 30)
    settings = Settings(**keys)
    settings.require_credentials()
    if not settings.slack_bot_token.get_secret_value():
        raise ValueError("Slack credential has not been configured")
    check(settings)
    slack = AsyncWebClient(token=settings.slack_bot_token.get_secret_value())
    monitor = AccessMonitor(
        lambda: worker_permitted(storage, request, saved, settings=settings),
        settings.access_unavailable_grace_seconds,
        event_id,
    )
    if not await monitor.allowed(delivery=True):
        storage.put(
            f"{PREFIX}/events/{event_id}/attempt-a{attempt}.json",
            json.dumps(
                {
                    "event_id": event_id,
                    "attempt": attempt,
                    "outcome": "stopped",
                    "stop_reason": monitor.stop_reason,
                }
            ).encode(),
        )
        if request.get("notice_ts"):
            await slack.chat_update(
                channel=request["payload"]["event"]["channel"],
                ts=request["notice_ts"],
                text="Project access is unavailable.",
            )
        return
    restored = False
    pending_memory: bytes | None = None
    if saved.get("archive"):
        try:
            storage.bucket.blob(saved["archive"]).download_to_filename(str(archive))
            if resume and digest(archive) != resume["sha256"]:
                raise ValueError("Continuation checksum mismatch")
            if resume and resume.get("pending_memory"):
                ref = resume["pending_memory"]
                expected = f"{event_prefix(event_id)}/memory-a{attempt - 1}-"
                if not ref["key"].startswith(expected) or "/" in ref["key"][len(expected) :]:
                    raise ValueError("Knowledge checkpoint belongs to another attempt")
                pending_memory = storage.read_prefix(
                    ref["key"], memory_workspace.pending_limit(settings) + 1
                )
                if hashlib.sha256(pending_memory).hexdigest() != ref["sha256"]:
                    raise ValueError("Knowledge checkpoint checksum mismatch")
                memory_workspace.decode_pending(pending_memory, settings)
            restore(archive, home)
            restored = True
        except (NotFound, FileNotFoundError, ValueError, tarfile.TarError):
            if resume:
                saved = dict(request["turn_start"])
                resume = None
                pending_memory = None
                logging.warning("Recovery checkpoint unavailable; repeating work from turn start")
            logging.warning(
                "Thread checkpoint expired; continuing with the saved brief and bounded transcript"
            )
    if request.get("notice_ts"):
        try:
            await slack.chat_update(
                channel=request["payload"]["event"]["channel"],
                ts=request["notice_ts"],
                text="Getting set up…",
                blocks=status_blocks(settings.loading_emoji, "Getting set up…"),
            )
        except Exception:
            logging.warning("Could not update worker startup status", exc_info=True)
    sessions = Sessions(settings.state_db)
    payload = request["payload"]
    files: list[attachments.ThreadFile] = []
    if saved.get("context"):
        data = saved["context"]
        data["workdir"], data["data_path"] = Path(data["workdir"]), Path(data["data_path"])
        if not restored:
            data["session_id"] = None
        sessions.save(payload["event"]["channel"], Context(**data), saved["turn"], saved["is_init"])
        files = await asyncio.to_thread(
            attachments.materialize, storage, data["workdir"], saved.get("thread_files", [])
        )
    memory_base = project_memory.Snapshot.model_validate(
        request.get("memory", {"head_oid": "", "files": {}})
    )
    await asyncio.to_thread(
        memory_workspace.materialize, Path(saved["context"]["workdir"]), memory_base, settings
    )
    if pending_memory:
        memory_base = memory_workspace.restore_pending(
            pending_memory, Path(saved["context"]["workdir"]), memory_base, settings
        )
    guide_path = Path(saved["context"]["workdir"]) / "knowledge/PROJECT.md"
    saved["context"]["project_md"] = (
        guide_path.read_text() if guide_path.is_file() else "No project guide yet."
    )
    current = sessions.get(
        payload["event"]["channel"], payload["event"].get("thread_ts") or payload["event"]["ts"]
    )
    if current:
        current[0].project_md = saved["context"]["project_md"]
        sessions.save(payload["event"]["channel"], *current)
    memory_record: dict[str, Any] = {}
    compute: RecoveryManager | None = None
    stages: Stages | None = None
    sample_task: asyncio.Task[None] | None = None
    partial_task: asyncio.Task[None] | None = None
    partial_stop = asyncio.Event()
    telemetry_task: asyncio.Task[None] | None = None
    prefix = event_prefix(event_id)
    if worker:
        work = Path(saved["context"]["workdir"])
        sampler = Sampler(work)
        stages = Stages(
            storage,
            work,
            event_id,
            attempt,
        )
        if resume:
            try:
                missing = await asyncio.to_thread(
                    stages.restore_available, resume.get("stages", {})
                )
            except ValueError:
                missing = ["all completed stages"]
            resume["stages"] = stages.manifest()
            resume["missing_stages"] = missing

        async def operate(body: dict[str, Any]) -> dict[str, Any]:
            await persist_resources()
            fresh_token = await asyncio.to_thread(
                id_token.fetch_id_token,
                GoogleRequest(),  # type: ignore[no-untyped-call]
                url,
            )
            async with httpx.AsyncClient(timeout=120) as http:
                for retry in range(8):
                    result = await http.post(
                        f"{url}/api/v1/internal/slack-analysis/recovery/{event_id}",
                        json=body,
                        headers={"Authorization": f"Bearer {fresh_token}"},
                    )
                    if result.status_code == 409 and retry < 7:
                        await asyncio.sleep(2)
                        continue
                    if result.status_code == 422:
                        raise ValueError(
                            str(result.json().get("detail", "Invalid compute request"))
                        )
                    result.raise_for_status()
                    value: dict[str, Any] = result.json()
                    return value
            raise ValueError("Compute controller remained busy")

        async def save_stage(args: dict[str, Any]) -> dict[str, Any]:
            assert stages is not None
            value = await asyncio.to_thread(stages.capture, args)
            await save_partial()
            return value

        async def compute_status(text: str) -> None:
            await slack.chat_update(
                channel=payload["event"]["channel"],
                ts=request["notice_ts"],
                text=text,
                blocks=status_blocks(settings.loading_emoji, text),
            )

        compute = RecoveryManager(
            settings,
            worker,
            sampler,
            operate,
            save_stage=save_stage,
            status=compute_status,
        )
        if resume:
            compute.manifest["pending_outputs"] = resume.get("pending_outputs", [])
        compute.manifest["sdk_cost_total"] = saved.get("sdk_cost_total")
        sample_task = asyncio.create_task(sampler.run())

    publisher = write_worker.Publisher(storage, request, event_id, url)
    if compute:
        compute.stop_tools = publisher.cancel
        publisher.on_stop = compute.request_stop
    current_write_grant = (
        write_access.WriteGrant.model_validate(request["write_grant"])
        if request.get("write_grant")
        else None
    )
    write_access.log_turn(event_id, current_write_grant)

    async def before_delivery(result: TurnResult) -> None:
        await asyncio.to_thread(
            storage.put_if,
            f"{prefix}/finalizing-a{attempt}.json",
            json.dumps(
                {"event_id": event_id, "attempt": attempt, "model_cost": result.cost}
            ).encode(),
            None,
        )

    runtimes = (
        RuntimeManager(settings, storage=storage, principal=request["principal"])
        if settings.runtimes_enabled
        else None
    )
    if runtimes:
        await asyncio.to_thread(runtimes.restore_thread, saved.get("runtimes", []))
    agent = SlackAgent(
        settings,
        storage,
        sessions,
        Runner(
            settings,
            runtime_manager=runtimes,
            return_scripts=request.get("return_scripts") is True,
            script_manifest=resume.get("script_manifest") if resume else None,
            compute_manager=compute,
            gcs_publisher=publisher if current_write_grant else None,
            project_name=request.get("project_name", ""),
            memory_context=project_memory.context(
                memory_base, payload["event"].get("text", ""), settings
            ),
            write_access=write_access.instructions(
                current_write_grant, request.get("overwrite_paths")
            ),
        ),
        slack,
        run_prefix=run_root(
            request["private"],
            request["principal"],
            request["project_id"],
            thread_key,
            payload["event"].get("thread_ts") or payload["event"]["ts"],
        ),
        authorized=lambda: worker_permitted(storage, request, saved, settings=settings),
        publish_gcs=publisher.publish,
        write_grant=current_write_grant,
        overwrite_paths=request.get("overwrite_paths", []),
        before_delivery=before_delivery if compute else None,
    )

    def authorized() -> bool:
        if compute and memory_workspace.cancelled(storage, event_id):
            compute.stop_reason = "cancelled"
            compute.stop.set()
            return False
        return worker_permitted(storage, request, saved, settings=settings)

    monitor.check = authorized
    agent.access_monitor = monitor
    payload["event"]["_notice_ts"] = request.get("notice_ts")
    payload["event"]["_conversation_context"] = saved.get("messages", [])
    current_ids = set(request.get("attachments", []))
    payload["event"]["_event_id"] = event_id
    if resume:
        payload["event"]["_compute_resume"] = resume
    elif attempt:
        payload["event"]["_recovery_restart"] = True
    payload["event"]["_attachments"] = attachments.worker_listing(files, current_ids)
    present_ids = {f.id for f in files}
    unavailable = [f for f in saved.get("thread_files", []) if f["id"] not in present_ids]
    if unavailable:
        payload["event"]["_attachments"] += (
            "\nUnavailable files (expired, replaced, or unrestorable; ask for a replacement):\n"
            + "\n".join(f"- {json.dumps(f['name'])}" for f in unavailable)
        )
    payload["event"]["_thread_files"] = [f.model_dump() for f in files]

    async def persist_resources(*, finished: bool = False) -> None:
        if compute:
            await asyncio.to_thread(
                storage.put,
                f"{prefix}/resources-a{attempt}.json",
                json.dumps(
                    {
                        "event_id": event_id,
                        "attempt": attempt,
                        "tier": tier,
                        **compute.sampler.summary(),
                        **({"finished_at": time.time()} if finished else {}),
                    }
                ).encode(),
            )

    async def save_partial() -> None:
        if not compute or not stages:
            return
        try:
            if not await asyncio.to_thread(
                worker_permitted, storage, request, saved, settings=settings
            ):
                return
            current_state = sessions.get(
                payload["event"]["channel"],
                payload["event"].get("thread_ts") or payload["event"]["ts"],
            )
            if not current_state:
                return
            checkpoint_id = uuid4().hex
            pending = await asyncio.to_thread(memory_workspace.pending, work, memory_base, settings)
            pending_key = f"{prefix}/memory-a{attempt}-{checkpoint_id}.json"
            await asyncio.to_thread(storage.put_if, pending_key, pending, None)
            local = home / f"partial-{checkpoint_id}.tar.gz"
            await asyncio.to_thread(
                workspace_snapshot,
                local,
                home,
                partial=True,
                max_bytes=256 * 1024**2,
            )
            key = f"{prefix}/partial-a{attempt}-{checkpoint_id}.tar.gz"
            sha = await asyncio.to_thread(digest, local)
            await asyncio.to_thread(storage.upload_new, key, local)
            local.unlink(missing_ok=True)
            context = asdict(current_state[0])
            context["session_id"] = compute.session_id
            partial = {
                "event_id": event_id,
                "attempt": attempt,
                "archive": key,
                "sha256": sha,
                "saved_at": time.time(),
                "pending_memory": {
                    "key": pending_key,
                    "sha256": hashlib.sha256(pending).hexdigest(),
                },
                "model_cost": compute.model_cost,
                "sdk_cost_total": compute.sdk_cost_total,
                "stages": stages.manifest(),
                "state": {
                    "context": context,
                    "turn": int(request.get("turn_start", {}).get("turn", current_state[1])) + 1,
                    "is_init": current_state[2],
                },
                "runtimes": sorted(runtimes.thread_ids) if runtimes else saved.get("runtimes", []),
                **(
                    {"script_manifest": agent.runner.script_capture.checkpoint()}
                    if agent.runner.script_capture
                    else {}
                ),
            }
            await asyncio.to_thread(
                storage.put,
                f"{prefix}/partial-a{attempt}.json",
                json.dumps(partial, default=str).encode(),
            )
        except Exception:
            logging.warning("Partial checkpoint failed; retaining prior snapshot", exc_info=True)

    async def periodic(interval: int, telemetry: bool = False) -> None:
        while not partial_stop.is_set():
            try:
                await asyncio.wait_for(partial_stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            if telemetry:
                try:
                    await persist_resources()
                except Exception:
                    logging.warning("Resource summary upload failed; retaining prior measurements")
            else:
                await save_partial()

    if compute:
        partial_task = asyncio.create_task(periodic(settings.partial_checkpoint_seconds))
        telemetry_task = asyncio.create_task(periodic(30, telemetry=True))
        compute.persist_resources = persist_resources
    previous_methods = (
        methods_stamp(Path(saved["context"]["workdir"])) if saved.get("context") else None
    )
    state = None
    handle_task: asyncio.Task[None] | None = None

    async def stop_attempt() -> None:
        assert compute is not None
        # This stays active during final publication, after the model heartbeat ends.
        while not compute.stop.is_set():
            if await asyncio.to_thread(memory_workspace.cancelled, storage, event_id):
                compute.request_stop("cancelled")
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(compute.stop.wait(), 5)
        if compute.stop_reason == "worker_continuation":
            return
        logging.info("worker_stopping event=%s reason=%s", event_id, compute.stop_reason)
        if hard_stop:
            arm_stop_deadline()
        stopped = {
            "event_id": event_id,
            "attempt": attempt,
            "outcome": "stopped",
            "stop_reason": compute.stop_reason,
            "model_cost": compute.model_cost,
        }
        # Persist the outcome before waiting for tools or the SDK to unwind.
        await asyncio.to_thread(
            storage.put, f"{prefix}/attempt-a{attempt}.json", json.dumps(stopped).encode()
        )
        with suppress(Exception):
            await asyncio.wait_for(publisher.cancel(), 5)
        await asyncio.sleep(35)
        if handle_task and not handle_task.done():
            handle_task.cancel()

    stop_task = asyncio.create_task(stop_attempt()) if compute else None
    try:
        event = payload["event"]
        handle_task = asyncio.create_task(
            agent.handle(event, (event["channel"], event.get("thread_ts") or event["ts"]))
        )
        try:
            await handle_task
        except asyncio.CancelledError:
            if not compute or not compute.stop.is_set():
                raise
        state = sessions.get(event["channel"], event.get("thread_ts") or event["ts"])
    finally:
        # Cancelling to_thread does not stop its thread. Let an active snapshot finish
        # before closing SQLite or taking the final handoff snapshot.
        partial_stop.set()
        stopping = bool(compute and compute.stop.is_set() and not compute.pending)
        for background in (partial_task, telemetry_task):
            if background:
                if stopping:
                    background.cancel()
                with suppress(asyncio.CancelledError):
                    await background
        for background in (sample_task,):
            if background:
                background.cancel()
                with suppress(asyncio.CancelledError):
                    await background
        sessions.close()  # Flush SQLite WAL before archiving.
        if compute:
            await persist_resources(finished=True)
            await asyncio.to_thread(
                storage.put,
                f"{prefix}/attempt-a{attempt}.json",
                json.dumps(
                    {
                        "event_id": event_id,
                        "attempt": attempt,
                        "model_cost": compute.model_cost,
                        "outcome": "stopped" if stopping else "prepared",
                        **({"stop_reason": compute.stop_reason} if stopping else {}),
                    }
                ).encode(),
            )
        if stop_task:
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task
    if compute and compute.stop.is_set() and not compute.pending:
        return
    if (
        not memory_workspace.cancelled(storage, event_id)
        and state
        and (agent.completed or (compute and compute.pending))
        and await asyncio.to_thread(worker_permitted, storage, request, saved, settings=settings)
    ):
        proposal = await asyncio.to_thread(
            memory_workspace.pending, state[0].workdir, memory_base, settings
        )
        proposal_key = (
            f"{prefix}/memory-proposal.json"
            if agent.completed
            else f"{prefix}/memory-a{attempt}-{uuid4().hex}.json"
        )
        await asyncio.to_thread(storage.put, proposal_key, proposal)
        memory_record = {
            "pending_memory": {"key": proposal_key, "sha256": hashlib.sha256(proposal).hexdigest()}
        }
    if compute and compute.pending and state:
        script_record = (
            {"script_manifest": agent.runner.script_capture.checkpoint()}
            if agent.runner.script_capture
            else {}
        )
        await asyncio.to_thread(
            workspace_snapshot,
            archive,
            home,
            partial=False,
            published=publisher.published_paths(state[0].workdir) if state else set(),
        )
        key = f"{prefix}/handoff-a{attempt}-{uuid4().hex}.tar.gz"
        await asyncio.to_thread(storage.upload_new, key, archive)
        handoff = {
            "event_id": event_id,
            "attempt": attempt,
            "outcome": "handoff",
            **script_record,
            **memory_record,
            "archive": key,
            "sha256": digest(archive),
            "model_cost": compute.model_cost,
            "state": {"context": asdict(state[0]), "turn": state[1], "is_init": state[2]},
            "sdk_cost_total": compute.sdk_cost_total,
            "stages": stages.manifest() if stages else {},
            "runtimes": sorted(runtimes.thread_ids) if runtimes else saved.get("runtimes", []),
            "pending_outputs": compute.manifest.get("pending_outputs", []),
        }
        await asyncio.to_thread(
            storage.put,
            f"{prefix}/attempt-a{attempt}.json",
            json.dumps(handoff, default=str).encode(),
        )
        return
    if (
        not agent.completed
        or not state
        or not await asyncio.to_thread(worker_permitted, storage, request, saved, settings=settings)
    ):
        return
    if compute:
        workspace_snapshot(
            archive,
            home,
            partial=False,
            published=publisher.published_paths(state[0].workdir) if state else set(),
        )
    else:
        snapshot(archive, home, published=publisher.published_paths(state[0].workdir))
    archive_key = f"{PREFIX}/events/{event_id}/checkpoint.tar.gz"
    storage.upload(archive_key, archive)
    checkpoint: dict[str, Any] = {"archive": archive_key}
    if compute:
        checkpoint["sdk_cost_total"] = compute.sdk_cost_total
    if state:
        checkpoint.update(context=asdict(state[0]), turn=state[1], is_init=state[2])
    if runtimes:
        checkpoint["runtimes"] = sorted(runtimes.thread_ids)
    elif saved.get("runtimes"):
        checkpoint["runtimes"] = saved["runtimes"]
    checkpoint["messages"] = saved.get("messages", [])
    checkpoint["thread_files"] = attachments.merge(saved.get("thread_files", []), agent.delivered)
    record_exchange(
        checkpoint,
        event_id,
        event.get("text", ""),
        agent.last_reply,
        files=[f["name"] for f in saved.get("thread_files", []) if f["id"] in current_ids],
        outputs=[f["name"] for f in agent.delivered],
        success=agent.last_error is None,
        methods=read_methods(state[0].workdir, previous_methods),
    )
    storage.put(pointer, json.dumps(checkpoint, default=str).encode())
    storage.put(
        f"{prefix}/attempt-a{attempt}.json",
        json.dumps(
            {
                "event_id": event_id,
                "attempt": attempt,
                "outcome": "complete",
                "model_cost": compute.model_cost if compute else None,
                **memory_record,
            }
        ).encode(),
    )


def methods_stamp(workdir: Path) -> tuple[int, int, int] | None:
    path = workdir / "notes/methods.md"
    try:
        if not path.resolve().is_relative_to(workdir.resolve()) or not path.is_file():
            return None
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except OSError:
        return None


def read_methods(workdir: Path, previous: tuple[int, int, int] | None) -> str:
    stamp = methods_stamp(workdir)
    if stamp is None or stamp == previous:
        return ""
    try:
        with (workdir / "notes/methods.md").open(encoding="utf-8", errors="replace") as stream:
            return stream.read(2000)
    except OSError:
        return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(sys.argv[1], hard_stop=True))
