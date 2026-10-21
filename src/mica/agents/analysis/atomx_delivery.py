"""Private application-owned connection links; never returned to the language model."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

from . import access, command_intent, email_delivery, surface

if TYPE_CHECKING:
    from .batch import BatchBridge


async def deliver(
    bridge: BatchBridge,
    owner: str,
    record: dict[str, Any],
    event_id: str,
    text: str,
    operation: str,
    *,
    private_link: bool = False,
) -> None:
    stage = "atomx-" + hashlib.sha256(operation.encode()).hexdigest()[:24]
    receipt = f"atomx-delivery:{event_id}:{stage}"
    claim = await bridge.repository.begin_event(receipt, lease_seconds=60)
    if claim.status == "completed":
        return
    if claim.status != "claimed":
        raise RuntimeError("Private delivery is in progress; retry")
    try:
        grant = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, bridge.storage, record
        )
        if grant.email != owner:
            raise PermissionError("The connection recipient is no longer verified.")
        if surface.is_email(record):
            selected = copy.deepcopy(record)
            command_intent.private_reply(selected, grant)
            await email_delivery.deliver(bridge, event_id, selected, text, kind=stage)
        else:
            client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=10)
            opened = await client.conversations_open(users=record["payload"]["event"]["user"])
            channel = opened["channel"]["id"]
            await client.chat_postMessage(
                channel=channel,
                text=text,
                client_msg_id=hashlib.md5(receipt.encode()).hexdigest(),
                unfurl_links=False,
                unfurl_media=False,
            )
        await bridge.repository.complete_event(receipt, claim.token)
    except Exception:
        await bridge.repository.release_event(receipt, claim.token)
        # No provider exceptions or link text in request error logs.
        raise RuntimeError("Private AtoMx delivery needs retry.") from None
