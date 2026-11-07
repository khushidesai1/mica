"""Private application-owned connection links; never returned to the language model."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import access, command_intent, email_delivery, surface
from .transport import SlackTransport

if TYPE_CHECKING:
    from .batch import BatchBridge


def message_key(event_id: str) -> str:
    return "transfer-message:" + event_id


async def replace_message(
    bridge: BatchBridge, client: AsyncWebClient, record: dict[str, Any], event_id: str,
    channel: str, thread: str | None, text: str, blocks: list[dict[str, Any]],
    *, adopt: bool,
) -> None:
    """One durable message per transfer; ambiguous first posts are reconciled, never duplicated."""
    from . import approvals

    key = message_key(event_id)
    async with approvals.lease(bridge, key, busy_error=RuntimeError):
        saved = await bridge.repository.get_connector_state(key)
        content = hashlib.sha256(json.dumps([text, blocks], sort_keys=True).encode()).hexdigest()
        identifier = hashlib.md5(key.encode()).hexdigest()
        if saved is None:
            original = str(record.get("notice_ts", "")) if adopt else ""
            saved = {
                "channel": channel, "thread": thread,
                "ts": original if re.fullmatch(r"\d+\.\d+", original) else "",
                "state": "ready", "started": time.time(),
            }
            await bridge.repository.set_connector_state(key, saved)
        if saved["channel"] != channel or saved["thread"] != thread:
            raise PermissionError("The private transfer conversation changed.")
        if saved.get("content") == content:
            return
        if not saved["ts"] and saved["state"] == "sending":
            who = await client.auth_test()
            args: dict[str, Any] = dict(
                channel=channel, oldest=str(saved["started"] - 10),
                latest=str(saved["started"] + 300), inclusive=True, limit=100,
            )
            history = (
                await client.conversations_replies(ts=thread, **args)
                if thread else await client.conversations_history(**args)
            )
            messages: list[dict[str, Any]] = history.get("messages", [])
            matches = [m for m in messages
                       if m.get("client_msg_id") == identifier and m.get("user") == who["user_id"]]
            if len(matches) != 1:
                raise RuntimeError("Transfer message delivery is not yet confirmed.")
            found = str(matches[0].get("ts", ""))
            if not re.fullmatch(r"\d+\.\d+", found):
                raise RuntimeError("Transfer message receipt is unavailable.")
            saved["ts"] = found
            await bridge.repository.set_connector_state(key, saved)
        transport = SlackTransport(client)
        if saved["ts"]:
            await transport.update(channel=channel, ts=saved["ts"], text=text, blocks=blocks)
        else:
            saved.update(state="sending", started=time.time())
            await bridge.repository.set_connector_state(key, saved)
            try:
                result = await transport.post(
                    channel=channel, thread_ts=thread, text=text, blocks=blocks,
                    client_msg_id=identifier, unfurl_links=False, unfurl_media=False,
                )
            except SlackApiError:
                # An explicit API rejection confirms that no message was accepted.
                saved["state"] = "ready"
                await bridge.repository.set_connector_state(key, saved)
                raise
            posted = str(result.get("ts", ""))
            if not re.fullmatch(r"\d+\.\d+", posted):
                raise RuntimeError("Transfer message receipt is unavailable.")
            saved["ts"] = posted
        saved.update(state="sent", content=content)
        await bridge.repository.set_connector_state(key, saved)


async def deliver(
    bridge: BatchBridge,
    owner: str,
    record: dict[str, Any],
    event_id: str,
    text: str,
    operation: str,
    *,
    private_link: bool = False,
    blocks: list[dict[str, Any]] | None = None,
    replace: bool = False,
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
            in_request_dm = (
                grant.private
                and event.get("channel_type") == "im"
                and re.fullmatch(r"D[A-Z0-9]+", channel)
            )
            if not in_request_dm:
                # Shared channels must never receive credentials or approval links.
                opened = await client.conversations_open(users=event["user"])
                channel = str(opened["channel"]["id"])
                if not re.fullmatch(r"D[A-Z0-9]+", channel):
                    raise PermissionError("A one-to-one private conversation is required.")
            thread = (event.get("thread_ts") or event["ts"]) if in_request_dm else None
            if replace:
                await replace_message(
                    bridge, client, record, event_id, channel, thread, text, blocks or [],
                    adopt=bool(in_request_dm),
                )
            else:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread, text=text,
                    client_msg_id=hashlib.md5(receipt.encode()).hexdigest(),
                    unfurl_links=False, unfurl_media=False,
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
