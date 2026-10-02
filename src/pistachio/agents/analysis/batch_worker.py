"""One paid agent turn per Spot job; checkpoint sessions to the existing artifact bucket."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tarfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from google.api_core.exceptions import NotFound
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from slack_sdk.web.async_client import AsyncWebClient

from .access import worker_permitted
from .config import CHECKPOINT_PREFIX as PREFIX
from .config import Settings
from .direct import record_exchange
from .identity import user_key
from .outputs import files_under
from .preflight import check
from .runner import Context, Runner
from .sessions import Sessions
from .slack_app import SlackAgent
from .storage import GCSStorage


def restore(archive: Path, home: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = home / member.name
            if (
                not target.resolve().is_relative_to(home.resolve())
                or not (member.isfile() or member.isdir())
                or not (
                    member.name.startswith(("work/", ".claude/projects/"))
                    or member.name == "state.db"
                )
            ):
                raise ValueError("Unsafe session checkpoint")
        tar.extractall(home, filter="data")


def snapshot(archive: Path, home: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        paths = [*files_under(home / "work"), *files_under(home / ".claude/projects")]
        if (home / "state.db").is_file():
            paths.append(home / "state.db")
        for path in paths:
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
            logging.warning(
                "Thread checkpoint expired; continuing with the saved brief and bounded transcript"
            )
    if request.get("notice_ts"):
        try:
            await slack.chat_update(
                channel=request["payload"]["event"]["channel"],
                ts=request["notice_ts"],
                text="Worker started. Checking your request and restoring context…",
            )
        except Exception:
            logging.warning("Could not update worker startup status", exc_info=True)
    sessions = Sessions(settings.state_db)
    payload = request["payload"]
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
    agent = SlackAgent(
        settings,
        storage,
        sessions,
        Runner(
            settings,
            user_memory=request.get("user_memory", "") if request.get("private") else "",
            project_name=request.get("project_name", ""),
        ),
        slack,
        run_prefix=(
            f"projects/_users/{user_key(request['principal'])}/runs/{request['project_id']}/{thread_key}"
            if request["private"]
            else ""
        ),
        authorized=lambda: worker_permitted(storage, request, saved, settings=settings),
    )
    payload["event"]["_notice_ts"] = request.get("notice_ts")
    payload["event"]["_conversation_context"] = saved.get("messages", [])
    try:
        event = payload["event"]
        await agent.handle(event, (event["channel"], event.get("thread_ts") or event["ts"]))
        state = sessions.get(event["channel"], event.get("thread_ts") or event["ts"])
    finally:
        sessions.close()  # Flush SQLite WAL before archiving.
    if (
        not agent.completed
        or not state
        or not await asyncio.to_thread(worker_permitted, storage, request, saved, settings=settings)
    ):
        return
    snapshot(archive, home)
    archive_key = f"{PREFIX}/events/{event_id}/checkpoint.tar.gz"
    storage.upload(archive_key, archive)
    checkpoint: dict[str, Any] = {"archive": archive_key}
    if state:
        checkpoint.update(context=asdict(state[0]), turn=state[1], is_init=state[2])
    checkpoint["messages"] = saved.get("messages", [])
    record_exchange(checkpoint, event_id, event.get("text", ""), agent.last_reply)
    storage.put(pointer, json.dumps(checkpoint, default=str).encode())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(sys.argv[1]))
