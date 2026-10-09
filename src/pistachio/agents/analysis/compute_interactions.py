"""Spending approval from authenticated Slack buttons, never from agent text."""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import TYPE_CHECKING, Any

from . import access, conversation_memory, pricing
from .compute_batch import show_approval
from .compute_quotes import Quote, quote
from .identity import resolve_email
from .storage import Storage

if TYPE_CHECKING:
    from .batch import BatchBridge


def is_compute(payload: dict[str, Any]) -> bool:
    return any(
        str(a.get("action_id", "")).startswith("pistachio_compute_")
        for a in payload.get("actions", [])
        if isinstance(a, dict)
    )


async def handle(bridge: BatchBridge, store: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    from slack_sdk.web.async_client import AsyncWebClient

    team = str(payload.get("team", {}).get("id", ""))
    user = str(payload.get("user", {}).get("id", ""))
    channel = str(payload.get("channel", {}).get("id", ""))
    action = payload.get("actions", [{}])[0]
    value = str(action.get("value", ""))
    if not re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{32}", value):
        return {}
    event_id, quote_id = value.split(":")
    mutex = f"claude-poll:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=180)
    if claim.status != "claimed":
        return {}  # Slack redelivery can safely retry; no partial approval.
    try:
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if not record or record["status"] != "awaiting_approval":
            return {}
        event = record["payload"]["event"]
        email = await asyncio.to_thread(
            resolve_email, bridge.app, bridge.settings, store, team, user
        )
        grant = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, store, record)
        if not (
            team == record["payload"].get("team_id")
            and channel == event["channel"]
            and user == event["user"]
            and email == record["principal"]
            and grant.allowed
            and grant.signature == record["access_signature"]
            and await asyncio.to_thread(
                conversation_memory.is_current, store, grant, record["thread_key"], record
            )
            and time.time() < record.get("approval_expires", 0)
        ):
            raise ValueError("This approval is not authorized.")
        q = Quote.model_validate(record["compute_quote"])
        if quote_id != q.id:
            raise ValueError("This quote has been replaced.")
        name = action.get("action_id")
        if name == "pistachio_compute_cancel":
            record.update(status="failed", cancel_requested=True)
            await bridge.notify(event_id, record, "cancelled", "Cancelled the pending computation.")
        elif name == "pistachio_compute_approve":
            rates = await asyncio.to_thread(
                pricing.current, store, bridge.app.gcp_region, record.get("provisioning", "SPOT")
            )
            approved_limit = math.ceil(q.request_high_usd)
            fresh = quote(
                q.estimate,
                rates,
                spent_usd=record["compute_spent"],
                limit_usd=approved_limit,
                now=time.time(),
                identity=f"{event_id}:approval",
                reason=q.reason,
            )
            if fresh.requires_approval:
                record["compute_quote"] = fresh.model_dump(mode="json")
                await show_approval(bridge, event_id, record)
            else:
                record.update(
                    compute_limit=approved_limit,
                    status="pending",
                    approved_by=email,
                    approved_at=time.time(),
                    compute_quote=fresh.model_dump(mode="json"),
                )
                # Awaiting-approval polling already owns the durable dispatch chain.
                await bridge.notify(
                    event_id, record, "starting", "Approved. Resuming this computation…"
                )
        else:
            raise ValueError("Unknown compute action.")
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    except ValueError:
        await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_postEphemeral(
            channel=channel, user=user, text="This compute action is expired or not authorized."
        )
    finally:
        await bridge.repository.release_event(mutex, claim.token)
    return {}
