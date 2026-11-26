"""Private application-owned connection links; never returned to the language model."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

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
    bridge: BatchBridge, owner: str, record: dict[str, Any], event_id: str,
    text: str, operation: str, *, private_link: bool = False,
    blocks: list[dict[str, Any]] | None = None, replace: bool = False,
) -> None:
    from .connections import deliver_private

    await deliver_private(
        bridge, owner, record, event_id, text, operation, namespace="atomx", label="AtoMx",
        client_factory=AsyncWebClient, replace_handler=replace_message,
        failure_cause="atomx_delivery_failed", blocks=blocks, replace=replace,
    )
