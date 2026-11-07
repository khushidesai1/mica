"""Signed Slack interaction branch. Raw modal payloads never enter BatchBridge.accept."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from . import atomx_delivery
from .identity import resolve_email
from .transfers import Transfers

ACTION_PREFIXES = ("mica_transfer_", "pistachio_transfer_")


def is_transfer(payload: dict[str, Any]) -> bool:
    if payload.get("type") == "view_submission":
        return payload.get("view", {}).get("callback_id") in {
            prefix + "login_submit" for prefix in ACTION_PREFIXES
        }
    return any(
        str(a.get("action_id", "")).startswith(ACTION_PREFIXES) for a in payload.get("actions", [])
    )


async def handle(service: Transfers, payload: dict[str, Any]) -> dict[str, Any]:
    modal = payload.get("type") == "view_submission"
    team, user = (
        str(payload.get("team", {}).get("id", "")),
        str(payload.get("user", {}).get("id", "")),
    )
    try:
        email = await asyncio.to_thread(
            resolve_email, service.app, service.settings, service.store, team, user
        )
        if not email:
            raise ValueError("Your Slack identity is not authorized.")
        if modal:
            metadata = json.loads(payload["view"]["private_metadata"])
            pid = str(metadata["pending_id"])
            action = "submit"
        else:
            if payload.get("type") != "block_actions" or len(payload.get("actions", [])) != 1:
                raise ValueError("Choose one transfer action.")
            action = payload["actions"][0]["action_id"].removeprefix("mica_transfer_")
            action = action.removeprefix("pistachio_transfer_")
            if action == "connection_link":
                return {}  # URL button only; credentials stay in the private form.
            value = str(payload["actions"][0].get("value", ""))
            if not re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{64}", value):
                raise ValueError("Use the current transfer card.")
            pid, expected = value.split(":")
        p = await service.pending(pid)
        if p["team"] != team:
            raise ValueError("This request belongs to a different workspace.")
        if not modal:
            message = await service.repo.get_connector_state(atomx_delivery.message_key(pid))
            if (
                not message or message.get("state") != "sent"
                or not message.get("ts")
                or payload.get("channel", {}).get("id") != message["channel"]
                or payload.get("message", {}).get("ts") != message["ts"]
                or p["slack_user"] != user or p["requested_by"] != email
            ):
                raise ValueError("Use your original private transfer card.")
        service.authorize(p["project"], email, p)
        if modal:
            # Retired forms never persist or use raw Slack password fields.
            return {"response_action": "clear"}
        if action == "login":
            if email != p["requested_by"]:
                raise ValueError("Only the requester can manage their personal connection.")
            record = await service.repo.get_connector_state(f"claude-event:{pid}")
            if not record:
                raise ValueError("Conversation unavailable")
            await service.connection_link(email, record, pid, pid)
        elif action == "confirm":
            if p["slack_user"] != user:
                raise ValueError("Only the requesting Slack user can confirm.")
            await service.confirm(pid, email, expected=expected)
        elif action == "cancel":
            await service.cancel(pid, email, expected=expected)
        else:
            raise ValueError("Unknown transfer action.")
    except Exception:
        # Do not serialize exceptions, user input, provider responses, or raw payloads.
        if modal:
            return {
                "response_action": "errors",
                "errors": {
                    "password": (
                        "Could not save. Check your project access and whether this form expired."
                    )
                },
            }
        await AsyncWebClient(token=service.app.slack_bot_token, timeout=5).chat_postEphemeral(
            channel=payload.get("channel", {}).get("id", ""),
            user=user,
            text="That transfer action is unavailable, expired, or not authorized.",
        )
    return {}
