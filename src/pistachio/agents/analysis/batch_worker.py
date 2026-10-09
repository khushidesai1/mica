"""One paid agent turn per Spot job; checkpoint sessions to the existing artifact bucket."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tarfile
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

from . import attachments, write_access, write_worker
from .access import run_root, worker_permitted
from .checkpoints import Stages, workspace_snapshot
from .compute_batch import event_prefix
from .compute_tools import ComputeManager
from .config import CHECKPOINT_PREFIX as PREFIX
from .config import Settings
from .direct import record_exchange
from .outputs import files_under
from .preflight import check
from .resources import Sampler
from .runner import Context, Runner, TurnResult
from .runtime_tools import RuntimeManager
from .sessions import Sessions
from .slack_app import SlackAgent
from .slack_format import status_blocks
from .storage import GCSStorage


def restore(archive: Path, home: Path, *, max_bytes: int = 100 * 1024**3) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 200000 or sum(m.size for m in members) > max_bytes:
            raise ValueError("Oversized session checkpoint")
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


def snapshot(archive: Path, home: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        paths = [*files_under(home / "work"), *files_under(home / ".claude/projects")]
        if (home / "state.db").is_file():
            paths.append(home / "state.db")
        for path in paths:
            # Sources expire independently; rehydrate inputs from their original objects.
            if any(part in {".previews", "inputs"} for part in path.relative_to(home).parts):
                continue
            tar.add(path, arcname=str(path.relative_to(home)), recursive=False)


async def main(event_id: str) -> None:
    settings = Settings()
    storage = GCSStorage(settings.bucket)
    request = json.loads(storage.get(f"{PREFIX}/events/{event_id}/request.json")[0])
    thread_key = request["thread_key"]
    home = settings.work_root.parent
    archive = home / "checkpoint.tar.gz"
    # Keep the small frozen brief/turn record when 30-day workspace archives expire.
    pointer = f"projects/_threads/{thread_key}/checkpoint.json"
    try:
        saved: dict[str, Any] = json.loads(storage.get(pointer)[0])
    except FileNotFoundError:
        saved = {}
    resume = request.get("resume") if settings.tiers_enabled else None
    if resume:
        if resume.get("event_id") != event_id or resume.get("attempt") != settings.attempt - 1:
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
        response = await client.get(
            f"{url}/api/v1/internal/slack-analysis/credentials/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        keys = response.json()
    settings = Settings(**keys)
    settings.require_credentials()
    if not settings.slack_bot_token.get_secret_value():
        raise ValueError("Slack credential has not been configured")
    check(settings)
    slack = AsyncWebClient(token=settings.slack_bot_token.get_secret_value())
    if not await asyncio.to_thread(worker_permitted, storage, request, saved, settings=settings):
        if request.get("notice_ts"):
            await slack.chat_update(
                channel=request["payload"]["event"]["channel"],
                ts=request["notice_ts"],
                text="Project access is unavailable.",
            )
        return
    restored = False
    if saved.get("archive"):
        try:
            storage.bucket.blob(saved["archive"]).download_to_filename(str(archive))
            restore(archive, home)
            restored = True
        except NotFound:
            if resume:
                raise ValueError("The continuation checkpoint is unavailable") from None
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
        if saved.get("direct_version"):
            # The small record may contain brief edits made without a compute worker.
            draft = data["workdir"] / "outputs/PROJECT.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text(data["project_md"])
        files = await asyncio.to_thread(
            attachments.materialize, storage, data["workdir"], saved.get("thread_files", [])
        )
    compute: ComputeManager | None = None
    stages: Stages | None = None
    sample_task: asyncio.Task[None] | None = None
    partial_task: asyncio.Task[None] | None = None
    partial_stop = asyncio.Event()
    prefix = event_prefix(event_id) if settings.tiers_enabled else f"{PREFIX}/events/{event_id}"
    if settings.tiers_enabled and request.get("compute"):
        work = Path(saved["context"]["workdir"])
        sampler = Sampler(work)
        stages = Stages(
            storage,
            work,
            event_id,
            settings.attempt,
            max_bytes=settings.compute_checkpoint_max_gb * 1024**3,
        )
        if resume:
            await asyncio.to_thread(stages.restore, resume.get("stages", {}))

        async def operate(body: dict[str, Any]) -> dict[str, Any]:
            fresh_token = await asyncio.to_thread(
                id_token.fetch_id_token,
                GoogleRequest(),  # type: ignore[no-untyped-call]
                url,
            )
            async with httpx.AsyncClient(timeout=120) as http:
                for retry in range(8):
                    result = await http.post(
                        f"{url}/api/v1/internal/slack-analysis/compute/{event_id}",
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
            return await asyncio.to_thread(stages.capture, args)

        async def compute_status(text: str) -> None:
            await slack.chat_update(
                channel=payload["event"]["channel"],
                ts=request["notice_ts"],
                text=text,
                blocks=status_blocks(settings.loading_emoji, text),
            )

        compute = ComputeManager(
            settings,
            request["compute"],
            sampler,
            operate,
            save_stage=save_stage,
            status=compute_status,
        )
        if resume:
            compute.manifest["pending_outputs"] = resume.get("pending_outputs", [])
        compute.manifest["sdk_cost_total"] = saved.get("sdk_cost_total")
        sample_task = asyncio.create_task(sampler.run())

    async def publish_gcs(ctx: Context, result: TurnResult) -> write_worker.Publication:
        return await write_worker.publish(storage, request, event_id, url, ctx, result)

    async def before_delivery(result: TurnResult) -> None:
        await asyncio.to_thread(
            storage.put_if,
            f"{prefix}/finalizing-a{settings.attempt}.json",
            json.dumps(
                {"event_id": event_id, "attempt": settings.attempt, "model_cost": result.cost}
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
            compute_manager=compute,
            user_memory=request.get("user_memory", "") if request.get("private") else "",
            project_name=request.get("project_name", ""),
            conversation_summary=request.get("conversation_memory", ""),
            write_access=write_access.instructions(
                write_access.WriteGrant.model_validate(request["write_grant"])
                if request.get("write_grant")
                else None
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
        publish_gcs=publish_gcs,
        before_delivery=before_delivery if compute else None,
    )
    payload["event"]["_notice_ts"] = request.get("notice_ts")
    payload["event"]["_conversation_context"] = saved.get("messages", [])
    current_ids = set(request.get("attachments", []))
    payload["event"]["_event_id"] = event_id
    if resume:
        payload["event"]["_compute_resume"] = resume
    payload["event"]["_attachments"] = attachments.worker_listing(files, current_ids)
    present_ids = {f.id for f in files}
    unavailable = [f for f in saved.get("thread_files", []) if f["id"] not in present_ids]
    if unavailable:
        payload["event"]["_attachments"] += (
            "\nUnavailable files (expired, replaced, or unrestorable; ask for a replacement):\n"
            + "\n".join(f"- {json.dumps(f['name'])}" for f in unavailable)
        )
    payload["event"]["_thread_files"] = [f.model_dump() for f in files]
    if compute:

        async def partial_checkpoints() -> None:
            assert compute is not None and stages is not None
            while not partial_stop.is_set():
                try:
                    await asyncio.wait_for(
                        partial_stop.wait(), timeout=settings.partial_checkpoint_seconds
                    )
                    return
                except TimeoutError:
                    pass
                try:
                    state = sessions.get(
                        payload["event"]["channel"],
                        payload["event"].get("thread_ts") or payload["event"]["ts"],
                    )
                    if not state or not compute.session_id:
                        continue
                    stage_manifest = stages.manifest()
                    checkpoint_id = uuid4().hex
                    local = home / f"partial-{checkpoint_id}.tar.gz"
                    await asyncio.to_thread(
                        workspace_snapshot,
                        local,
                        home,
                        partial=True,
                        max_bytes=min(settings.compute_checkpoint_max_gb * 1024**3, 256 * 1024**2),
                    )
                    key = f"{prefix}/partial-a{settings.attempt}-{checkpoint_id}.tar.gz"
                    await asyncio.to_thread(storage.upload_new, key, local)
                    local.unlink(missing_ok=True)
                    context = asdict(state[0])
                    context["session_id"] = compute.session_id
                    partial = {
                        "event_id": event_id,
                        "attempt": settings.attempt,
                        "archive": key,
                        "saved_at": time.time(),
                        "model_cost": compute.model_cost,
                        "stages": stage_manifest,
                        "state": {"context": context, "turn": state[1], "is_init": state[2]},
                    }
                    await asyncio.to_thread(
                        storage.put,
                        f"{prefix}/partial-a{settings.attempt}.json",
                        json.dumps(partial, default=str).encode(),
                    )
                except Exception:
                    logging.warning(
                        "Partial checkpoint failed; retaining prior snapshot", exc_info=True
                    )

        partial_task = asyncio.create_task(partial_checkpoints())
    try:
        event = payload["event"]
        await agent.handle(event, (event["channel"], event.get("thread_ts") or event["ts"]))
        state = sessions.get(event["channel"], event.get("thread_ts") or event["ts"])
    finally:
        # Cancelling to_thread does not stop its thread. Let an active snapshot finish
        # before closing SQLite or taking the final handoff snapshot.
        partial_stop.set()
        if partial_task:
            await partial_task
        for background in (sample_task,):
            if background:
                background.cancel()
                with suppress(asyncio.CancelledError):
                    await background
        sessions.close()  # Flush SQLite WAL before archiving.
        if compute:
            await asyncio.to_thread(
                storage.put,
                f"{prefix}/resources-a{settings.attempt}.json",
                json.dumps(
                    {
                        "tier": settings.tier,
                        "attempt": settings.attempt,
                        **compute.sampler.summary(),
                    }
                ).encode(),
            )
            await asyncio.to_thread(
                storage.put,
                f"{prefix}/attempt-a{settings.attempt}.json",
                json.dumps(
                    {
                        "event_id": event_id,
                        "attempt": settings.attempt,
                        "model_cost": compute.model_cost,
                        "outcome": "prepared",
                    }
                ).encode(),
            )
    if compute and compute.pending and state:
        await asyncio.to_thread(
            workspace_snapshot,
            archive,
            home,
            partial=False,
            max_bytes=settings.compute_checkpoint_max_gb * 1024**3,
        )
        key = f"{prefix}/handoff-a{settings.attempt}-{uuid4().hex}.tar.gz"
        await asyncio.to_thread(storage.upload_new, key, archive)
        handoff = {
            "event_id": event_id,
            "attempt": settings.attempt,
            "outcome": "handoff",
            "archive": key,
            "model_cost": compute.model_cost,
            "state": {"context": asdict(state[0]), "turn": state[1], "is_init": state[2]},
            "sdk_cost_total": compute.sdk_cost_total,
            "stages": stages.manifest() if stages else {},
            "runtimes": sorted(runtimes.thread_ids) if runtimes else saved.get("runtimes", []),
            "pending_outputs": compute.manifest.get("pending_outputs", []),
        }
        await asyncio.to_thread(
            storage.put,
            f"{prefix}/attempt-a{settings.attempt}.json",
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
            archive, home, partial=False, max_bytes=settings.compute_checkpoint_max_gb * 1024**3
        )
    else:
        snapshot(archive, home)
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
    for field in ("conversation_epoch", "brief_policy_version", "brief_cleanup"):
        if field in saved:
            checkpoint[field] = saved[field]
    checkpoint["thread_files"] = attachments.merge(saved.get("thread_files", []), agent.delivered)
    record_exchange(
        checkpoint,
        event_id,
        event.get("text", ""),
        agent.last_reply,
        files=[f["name"] for f in saved.get("thread_files", []) if f["id"] in current_ids],
        outputs=[f["name"] for f in agent.delivered],
        success=agent.last_error is None,
    )
    storage.put(pointer, json.dumps(checkpoint, default=str).encode())
    if compute:
        storage.put(
            f"{prefix}/attempt-a{settings.attempt}.json",
            json.dumps(
                {
                    "event_id": event_id,
                    "attempt": settings.attempt,
                    "outcome": "complete",
                    "model_cost": compute.model_cost,
                }
            ).encode(),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(sys.argv[1]))
