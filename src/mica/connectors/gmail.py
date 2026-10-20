"""Recoverable, labeled Gmail intake into the shared Claude agent."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from googleapiclient.errors import HttpError

from mica.agents.analysis.batch import BatchBridge
from mica.agents.analysis.config import CHECKPOINT_PREFIX
from mica.agents.analysis.surface import EMAIL_STATE_PREFIX, email_keys
from mica.api.auth import Principal, get_principal
from mica.api.dependencies import Services
from mica.connectors import gmail_client as client

router = APIRouter(prefix="/integrations/gmail", tags=["integrations"])


def state_key(account: str) -> str:
    return f"{EMAIL_STATE_PREFIX}/{hashlib.sha256(account.encode()).hexdigest()}/cursor.json"


async def ingest(bridge: BatchBridge, api: Any, message_id: str, label: str, since: int) -> None:
    item = await asyncio.to_thread(
        api.users().messages().get(userId="me", id=message_id, format="metadata").execute
    )
    if (
        label not in item.get("labelIds", [])
        or "SENT" in item.get("labelIds", [])
        or int(item.get("internalDate", 0)) < since
        or int(item.get("sizeEstimate", 0)) > client.MAX_MESSAGE_BYTES
    ):
        return
    item = await asyncio.to_thread(
        api.users().messages().get(userId="me", id=message_id, format="raw").execute
    )
    encoded = item.get("raw", "")
    if len(encoded) > (client.MAX_MESSAGE_BYTES + 2) // 3 * 4:
        return
    raw = base64.urlsafe_b64decode(encoded + "===")
    verified = await asyncio.to_thread(client.verified_message, raw)
    if verified is None:
        return
    sender, message = verified
    if client.is_own(sender, bridge.app):
        return
    body = message.get_body(preferencelist=("plain",))
    original_text = str(body.get_content()) if body else ""
    if len(original_text) > 50_000:
        original_text = original_text[:50_000] + "\n[Message truncated at 50,000 characters.]"
    text = client.request_text(original_text)
    parts = [p for p in message.walk() if p.get_filename() and not p.is_multipart()]
    payload: dict[str, Any] = {
        "type": "event_callback",
        "team_id": "EMAIL",
        "event": {
            "type": "message",
            "channel_type": "email",
            "user": sender,
            "channel": "E" + item["threadId"].upper(),
            "ts": f"{int(item['internalDate']) // 1000}.{int(item['internalDate']) % 1000:06d}",
            "text": text or "Please review the attached files.",
        },
        "_email": {
            "mailbox": client.mailbox(bridge.app),
            "id": message_id,
            "thread_id": item["threadId"],
            "message_id": str(message["Message-ID"]),
            "subject": str(message["Subject"] or "Mica"),
            "recipients": [
                a.addr_spec.lower()
                for h in ("To", "Cc")
                for a in getattr(message[h], "addresses", ())
            ],
        },
    }
    if text != original_text:
        payload["_email"]["original_text"] = original_text
    event_id, _ = email_keys(payload, sender)
    if parts:
        key = f"{CHECKPOINT_PREFIX}/gmail/inputs/{event_id}.eml"
        await asyncio.to_thread(bridge.storage.put, key, raw)
        payload["_email"]["raw_key"] = key
        payload["event"]["files"] = [{"name": p.get_filename()} for p in parts]
    await bridge.accept(payload, principal=sender, surface_name="email")


async def consume(bridge: BatchBridge, api: Any, notified: str) -> None:
    account = client.mailbox(bridge.app)
    key = state_key(account)
    mutex = "gmail-consume:" + hashlib.sha256(account.encode()).hexdigest()
    claim = await bridge.repository.begin_event(mutex, lease_seconds=300)
    if claim.status != "claimed":
        raise RuntimeError("Mailbox consumption busy; retry")
    deadline = time.monotonic() + 180
    store = bridge.storage
    try:
        try:
            raw, generation = await asyncio.to_thread(store.get, key)
        except FileNotFoundError:
            raise RuntimeError("Renew the Gmail watch to activate intake first") from None
        state = json.loads(raw)
        if not state.get("resync") and int(notified) <= int(state["history_id"]):
            return
        label = await asyncio.to_thread(client.label_id, api, bridge.app.gmail_label)
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("Mailbox page saved; retry remaining history")
            if state.get("resync"):
                response = await asyncio.to_thread(
                    api.users()
                    .messages()
                    .list(
                        userId="me",
                        labelIds=[label],
                        maxResults=50,
                        q=f"after:{max(0, state['since'] // 1000 - 1)}",
                        pageToken=state.get("page"),
                    )
                    .execute
                )
                ids = [m["id"] for m in response.get("messages", [])]
            else:
                try:
                    response = await asyncio.to_thread(
                        api.users()
                        .history()
                        .list(
                            userId="me",
                            startHistoryId=state["history_id"],
                            labelId=label,
                            historyTypes=["messageAdded", "labelAdded"],
                            maxResults=50,
                            pageToken=state.get("page"),
                        )
                        .execute
                    )
                except HttpError as exc:
                    if exc.resp.status != 404:
                        raise
                    profile = await asyncio.to_thread(api.users().getProfile(userId="me").execute)
                    state.update(resync=True, target=str(profile["historyId"]), page=None)
                    generation = await asyncio.to_thread(
                        store.put_if, key, json.dumps(state).encode(), generation
                    )
                    continue
                ids = list(
                    dict.fromkeys(
                        change["message"]["id"]
                        for entry in response.get("history", [])
                        for kind in ("messagesAdded", "labelsAdded")
                        for change in entry.get(kind, [])
                    )
                )
            for message_id in ids:
                if time.monotonic() > deadline:
                    raise RuntimeError("Mailbox consumption timed out; retry this page")
                try:
                    await ingest(bridge, api, message_id, label, state["since"])
                except HttpError as exc:
                    if exc.resp.status != 404:
                        raise
            # CAS prevents a stale consumer from advancing over another consumer's work.
            state["page"] = response.get("nextPageToken")
            if not state["page"]:
                state["history_id"] = (
                    state.pop("target")
                    if state.pop("resync", False)
                    else str(response.get("historyId", notified))
                )
            generation = await asyncio.to_thread(
                store.put_if, key, json.dumps(state).encode(), generation
            )
            if not state["page"]:
                return
    finally:
        await bridge.repository.release_event(mutex, claim.token)


@router.post("/push")
async def gmail_push(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> dict[str, bool]:
    services: Services = request.app.state.services
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(403, "Untrusted Pub/Sub principal")
    if not client.enabled(services.settings):
        return {"ok": True}
    try:
        envelope = await request.json()
        notice = json.loads(base64.b64decode(envelope["message"]["data"], validate=True))
        if notice["emailAddress"].lower() != services.settings.gmail_account.lower() and notice[
            "emailAddress"
        ].lower() != client.mailbox(services.settings):
            # Compare Gmail's canonical base address, including dotted account names.
            copy = services.settings.model_copy(update={"gmail_account": notice["emailAddress"]})
            if client.mailbox(copy) != client.mailbox(services.settings):
                raise ValueError("Foreign mailbox")
        history = str(int(notice["historyId"]))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid Gmail notification") from None
    bridge = BatchBridge(services.settings, services.repository)
    api = await asyncio.to_thread(client.service, services.settings)
    await consume(bridge, api, history)
    return {"ok": True}


async def renew_watch(services: Services) -> dict[str, Any]:
    if not client.enabled(services.settings):
        return {"enabled": False}
    api = await asyncio.to_thread(client.service, services.settings)
    profile = await asyncio.to_thread(api.users().getProfile(userId="me").execute)
    actual = services.settings.model_copy(update={"gmail_account": profile.get("emailAddress", "")})
    if client.mailbox(actual) != client.mailbox(services.settings):
        raise ValueError("OAuth credentials belong to a different Gmail mailbox")
    label = await asyncio.to_thread(client.label_id, api, services.settings.gmail_label)
    bridge = BatchBridge(services.settings, services.repository)
    store = bridge.storage
    key = state_key(client.mailbox(services.settings))
    try:
        await asyncio.to_thread(store.get, key)
    except FileNotFoundError:
        # Baseline before watch: renewal never overwrites the processed cursor.
        since = int(time.time() * 1000)
        from mica.agents.analysis.storage import Conflict

        with suppress(Conflict):
            await asyncio.to_thread(
                store.put_if,
                key,
                json.dumps({"history_id": str(profile["historyId"]), "since": since}).encode(),
                None,
            )
    result: dict[str, Any] = await asyncio.to_thread(
        api.users()
        .watch(
            userId="me",
            body={
                "topicName": services.settings.gmail_pubsub_topic,
                "labelIds": [label],
                "labelFilterBehavior": "INCLUDE",
            },
        )
        .execute
    )
    await services.repository.set_connector_state("gmail-watch", result)
    return result
