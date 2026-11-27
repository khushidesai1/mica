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

from .ambient_config import AmbientPerson, AmbientTask
from .config import CHECKPOINT_PREFIX
from .slack_format import answer_blocks, chunks, neutralize
from .transport import SlackTransport

if TYPE_CHECKING:
    from .batch import BatchBridge


async def existing_dm(client: AsyncWebClient, user: str) -> str:
    """Resolve an uncertain user-ID send using im:read, without opening another DM."""
    cursor = ""
    for _ in range(5):
        response = await client.conversations_list(types="im", limit=200, cursor=cursor)
        channels: list[dict[str, Any]] = response.get("channels", [])
        for channel in channels:
            if channel.get("user") == user and str(channel.get("id", "")).startswith("D"):
                return str(channel["id"])
        metadata: dict[str, Any] = response.get("response_metadata", {})
        cursor = str(metadata.get("next_cursor") or "")
        if not cursor:
            break
    raise RuntimeError("Cannot resolve an uncertain ambient DM; operator reconciliation needed")


def message_key(operation: str) -> str:
    return "ambient-message:" + hashlib.sha256(operation.encode()).hexdigest()


async def resolve_blocked_notice(bridge: BatchBridge, state: dict[str, Any]) -> bool:
    """Edit a confirmed private block notice; recovery never sends a new message."""
    from .ambient import person_permitted

    key = message_key(state["key"] + ":blocked")
    claim = await bridge.repository.begin_event(key, lease_seconds=60)
    if claim.status != "claimed":
        return False
    try:
        saved = await bridge.repository.get_connector_state(key) or {}
        if saved.get("resolved") or not saved:
            return True
        if not saved.get("sent") or not str(saved.get("channel", "")).startswith("D"):
            return False  # An uncertain original send must not be replaced or duplicated.
        owner = AmbientPerson.model_validate(state["owner"])
        if not await asyncio.to_thread(
            person_permitted, bridge, owner, state["project"], state["team"], owner=True,
        ):
            return False
        await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_update(
            channel=saved["channel"], ts=saved["ts"],
            text=(f"Ambient task {state['task']} for {state['sample']} recovered and started. "
                  "See its run thread for current progress and results."),
        )
        saved["resolved"] = True
        await bridge.repository.set_connector_state(key, saved)
        return True
    finally:
        await bridge.repository.release_event(key, claim.token)


async def post_once(
    bridge: BatchBridge, operation: str, channel: str, text: str, thread_ts: str = "",
    *, blocks: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    key = message_key(operation)
    claim = await bridge.repository.begin_event(key, lease_seconds=60)
    if claim.status != "claimed":
        raise RuntimeError("Ambient notice delivery is busy")
    try:
        client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5)
        saved = await bridge.repository.get_connector_state(key) or {}
        if saved.get("sent"):
            return {"channel": saved["channel"], "ts": saved["ts"]}
        if not saved:
            if not channel.startswith(("D", "U", "W")):
                raise ValueError("Ambient delivery requires a private conversation")
            saved = {"channel": channel, "client_msg_id": str(uuid5(NAMESPACE_URL, operation))}
            await bridge.repository.set_connector_state(key, saved)
        if saved.get("sending"):
            # A timeout after Slack accepted a post is not permission to send it twice.
            # Search the exact conversation; if history is unavailable/ambiguous, fail closed.
            if not saved["channel"].startswith("D"):
                saved["channel"] = await existing_dm(client, saved["channel"])
                await bridge.repository.set_connector_state(key, saved)
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
                response = await SlackTransport(client).post(
                    channel=saved["channel"],
                    text=text,
                    client_msg_id=saved["client_msg_id"],
                    thread_ts=thread_ts or None,
                    unfurl_links=False,
                    unfurl_media=False,
                    **({"blocks": blocks} if blocks is not None else {}),
                )
            except SlackApiError:
                saved["sending"] = False  # Slack explicitly rejected this request.
                await bridge.repository.set_connector_state(key, saved)
                raise
            if not str(response["channel"]).startswith("D"):
                raise RuntimeError("Slack did not return a private ambient conversation")
            saved.update(sent=True, channel=str(response["channel"]), ts=str(response["ts"]))
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


async def launched(bridge: BatchBridge, record: dict[str, Any]) -> None:
    """Announce configured recipients only once a compute submission is authorized."""
    from . import ambient

    if not ambient.is_arrival(record):
        return
    key = record["ambient"]["key"]
    state = await bridge.repository.get_connector_state(key) or {}
    task = AmbientTask.model_validate(state["definition"])
    doc = await ambient.validate_start(bridge, task, state["sample"], state["team"])
    notices = state.setdefault("notices", {})
    for person in task.recipients:
        if person.email == task.authorized_by.email:
            continue
        notices[person.email] = await post_once(
            bridge, key + ":start:" + person.slack_user, person.slack_user,
            f"Ambient task set up by {task.authorized_by.email}: checking and processing "
            f"{state['sample']}. I'll post the final summary and report in this thread.",
        )
        await ambient.seed(
            bridge, doc, task.project, person, notices[person.email]["channel"],
            notices[person.email]["ts"], state["team"],
        )
        await bridge.repository.set_connector_state(key, state)
    state["decision"] = decision_summary(record)
    await bridge.repository.set_connector_state(key, state)


def decision_summary(record: dict[str, Any]) -> dict[str, str]:
    decision = record.get("direct_decision") or {}
    reply = str(decision.get("reply", "")).strip()
    return {
        "route": decision.get("route", ""),
        "reason": reply.splitlines()[0][:1000] if reply else "",
    }


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
            notice = state.get("notices", {}).get(person.email)
            if person.email == state["owner"]["email"] or not notice:
                continue
            if not await asyncio.to_thread(
                ambient.person_permitted, bridge, person, state["project"], state["team"]
            ):
                state.setdefault("withheld", []).append(person.email)
                continue
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
            for index, part in enumerate(chunks(summary)):
                await post_once(
                    bridge,
                    f"{event_id}:final:{person.slack_user}:{index}",
                    notice["channel"],
                    neutralize(part),
                    notice["ts"],
                    blocks=answer_blocks(part),
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
        state.update(status="done", outcome=record["status"], decision=decision_summary(record))
        if not state.get("recovery_notice_pending"):
            state.pop("ambient_due", None)
        await bridge.repository.set_connector_state(origin["key"], state)
    finally:
        await bridge.repository.release_event(mutex, claim.token)
