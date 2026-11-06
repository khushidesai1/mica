"""Durable private notices and application-selected background result recipients."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .ambient_config import AmbientPerson
from .config import CHECKPOINT_PREFIX
from .slack_format import chunks, to_mrkdwn

if TYPE_CHECKING:
    from .batch import BatchBridge


async def post_once(
    bridge: BatchBridge, operation: str, channel: str, text: str, thread_ts: str = ""
) -> dict[str, str]:
    key = "ambient-message:" + hashlib.sha256(operation.encode()).hexdigest()
    claim = await bridge.repository.begin_event(key, lease_seconds=60)
    if claim.status != "claimed":
        raise RuntimeError("Ambient notice delivery is busy")
    try:
        client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5)
        saved = await bridge.repository.get_connector_state(key) or {}
        if saved.get("sent"):
            return {"channel": saved["channel"], "ts": saved["ts"]}
        if not saved:
            if not channel.startswith("D"):
                opened = await client.conversations_open(users=channel)
                channel = str(opened["channel"]["id"])
            if not channel.startswith("D"):
                raise ValueError("Ambient delivery requires a private conversation")
            saved = {"channel": channel, "client_msg_id": str(uuid5(NAMESPACE_URL, operation))}
            await bridge.repository.set_connector_state(key, saved)
        if saved.get("sending"):
            # A timeout after Slack accepted a post is not permission to send it twice.
            # Search the exact conversation; if history is unavailable/ambiguous, fail closed.
            response = await (
                client.conversations_replies(channel=saved["channel"], ts=thread_ts, limit=100)
                if thread_ts
                else client.conversations_history(channel=saved["channel"], limit=100)
            )
            messages: list[dict[str, Any]] = response.get("messages", [])
            matched = next(
                (
                    m
                    for m in messages
                    if m.get("client_msg_id") == saved["client_msg_id"]
                ),
                None,
            )
            if matched is None:
                raise RuntimeError(
                    "Ambient notice has an uncertain send; operator reconciliation needed"
                )
            saved.update(sent=True, ts=str(matched["ts"]))
        else:
            saved["sending"] = True
            await bridge.repository.set_connector_state(key, saved)
            try:
                response = await client.chat_postMessage(
                    channel=saved["channel"],
                    text=text,
                    client_msg_id=saved["client_msg_id"],
                    thread_ts=thread_ts or None,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            except SlackApiError:
                saved["sending"] = False  # Slack explicitly rejected this request.
                await bridge.repository.set_connector_state(key, saved)
                raise
            saved.update(sent=True, ts=str(response["ts"]))
        await bridge.repository.set_connector_state(key, saved)
        return {"channel": saved["channel"], "ts": saved["ts"]}
    finally:
        await bridge.repository.release_event(key, claim.token)


async def pdf_once(
    bridge: BatchBridge, operation: str, channel: str, ts: str, file: dict[str, Any]
) -> None:
    """Persist Slack's file ID before completion so a retry never allocates another file."""
    key = "ambient-file:" + hashlib.sha256(operation.encode()).hexdigest()
    saved = await bridge.repository.get_connector_state(key) or {}
    if saved.get("sent"):
        return
    client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=15)
    if not saved:
        allocated = await client.files_getUploadURLExternal(
            filename=file["name"], length=file["size"]
        )
        saved = {"id": str(allocated["file_id"]), "url": str(allocated["upload_url"])}
        await bridge.repository.set_connector_state(key, saved)
    if not saved.get("uploaded"):
        data = (await asyncio.to_thread(bridge.storage.get, file["key"]))[0]
        if len(data) != file["size"] or hashlib.sha256(data).hexdigest() != file["sha256"]:
            raise ValueError("The saved report changed before delivery")
        async with httpx.AsyncClient(timeout=30) as client_http:
            uploaded = await client_http.post(saved["url"], content=data)
            uploaded.raise_for_status()
        saved["uploaded"] = True
        await bridge.repository.set_connector_state(key, saved)
    if saved.get("completing"):
        info = await client.files_info(file=saved["id"])
        shares = info["file"].get("shares", {}).get("private", {}).get(channel, [])
        if not any(share.get("thread_ts") == ts for share in shares):
            raise RuntimeError(
                "Ambient report delivery is uncertain; operator reconciliation needed"
            )
    else:
        saved["completing"] = True
        await bridge.repository.set_connector_state(key, saved)
        try:
            await client.files_completeUploadExternal(
                files=[{"id": saved["id"], "title": file["name"]}], channel_id=channel, thread_ts=ts
            )
        except SlackApiError:
            saved["completing"] = False
            await bridge.repository.set_connector_state(key, saved)
            raise
    saved.pop("url", None)
    saved["sent"] = True
    await bridge.repository.set_connector_state(key, saved)


async def terminal(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    from . import ambient
    from .registry import Registry

    origin = record.get("ambient")
    if not origin or record.get("status") not in {"complete", "failed"}:
        return
    mutex = f"ambient-terminal:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=120)
    if claim.status != "claimed":
        raise RuntimeError("Ambient terminal delivery is busy")
    try:
        state = await bridge.repository.get_connector_state(origin["key"]) or {}
        if state.get("status") == "done":
            return
        try:
            payload = json.loads(
                (
                    await asyncio.to_thread(
                        bridge.storage.get,
                        f"{CHECKPOINT_PREFIX}/events/{event_id}/ambient-result.json",
                    )
                )[0]
            )
        except FileNotFoundError:
            payload = {
                "text": record.get("terminal_reply")
                or record.get("notice_text")
                or "The background run stopped before producing a report.",
                "files": [],
            }
        summary = str(payload["text"])
        doc = await asyncio.to_thread(Registry(bridge.storage).load)
        for raw in state.get("recipients", []):
            person = AmbientPerson.model_validate(raw)
            if person.email == state["owner"]["email"]:
                continue
            if not await asyncio.to_thread(
                ambient.person_permitted, bridge, person, state["project"], state["team"]
            ):
                state.setdefault("withheld", []).append(person.email)
                continue
            notice = state["notices"][person.email]
            # Bind the reply thread to the configured project, independent of DM defaults.
            await ambient.seed(
                bridge,
                doc,
                state["project"],
                person,
                notice["channel"],
                notice["ts"],
                state["team"],
                summary,
            )
            for index, part in enumerate(chunks(to_mrkdwn(summary))):
                await post_once(
                    bridge,
                    f"{event_id}:final:{person.slack_user}:{index}",
                    notice["channel"],
                    part,
                    notice["ts"],
                )
            for file in payload.get("files", []):
                if not await asyncio.to_thread(
                    ambient.person_permitted, bridge, person, state["project"], state["team"]
                ):
                    raise PermissionError("Recipient access changed before report delivery")
                if (
                    str(file.get("name", "")).lower().endswith(".pdf")
                    and file.get("event_id") == event_id
                    and file.get("key")
                    and 0 < file.get("size", 0) <= 200_000_000
                ):
                    await pdf_once(
                        bridge,
                        f"{event_id}:{person.slack_user}:{file['sha256']}",
                        notice["channel"],
                        notice["ts"],
                        file,
                    )
        state.update(status="done", outcome=record["status"])
        state.pop("ambient_due", None)
        await bridge.repository.set_connector_state(origin["key"], state)
    finally:
        await bridge.repository.release_event(mutex, claim.token)
