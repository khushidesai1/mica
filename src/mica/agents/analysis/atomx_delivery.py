"""Private application-owned connection links; never returned to the language model."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError
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
            event = record["payload"]["event"]
            channel = str(event.get("channel", ""))
            if not (
                grant.private
                and event.get("channel_type") == "im"
                and re.fullmatch(r"D[A-Z0-9]+", channel)
            ):
                # Shared channels must never receive credentials or approval links.
                opened = await client.conversations_open(users=event["user"])
                channel = str(opened["channel"]["id"])
                if not re.fullmatch(r"D[A-Z0-9]+", channel):
                    raise PermissionError("A one-to-one private conversation is required.")
            await client.chat_postMessage(
                channel=channel,
                text=text,
                client_msg_id=hashlib.md5(receipt.encode()).hexdigest(),
                unfurl_links=False,
                unfurl_media=False,
            )
        await bridge.repository.complete_event(receipt, claim.token)
    except Exception as exc:
        if not surface.is_email(record) and not isinstance(exc, PermissionError):
            from . import observability

            await observability.report(
                bridge, "atomx_delivery_failed", event_id, record, unit=stage
            )
        await bridge.repository.release_event(receipt, claim.token)
        # Permanent refusals must finish the request instead of retrying for an hour.
        if isinstance(exc, PermissionError):
            raise ValueError(
                "I couldn't verify a private recipient. Please message Mica directly "
                "to connect your AtoMx account."
            ) from None
        if isinstance(exc, SlackApiError) and exc.response.get("error") in {
            "missing_scope",
            "not_allowed_token_type",
            "channel_not_found",
            "not_in_channel",
            "user_not_found",
            "user_not_visible",
            "is_archived",
            "no_permission",
            "restricted_action",
            "cannot_dm_bot",
            "invalid_auth",
            "token_revoked",
            "account_inactive",
            "user_disabled",
        }:
            raise ValueError(
                "Slack wouldn't let me deliver your private AtoMx link. Please message Mica "
                "in a one-to-one DM and ask to connect AtoMx. If you're already in that DM, "
                "the operator needs to check Mica's Slack access."
            ) from None
        # No provider exceptions or link text in request error logs.
        raise RuntimeError("Private AtoMx delivery needs retry.") from None
