"""Signed Slack interaction branch. Raw modal payloads never enter BatchBridge.accept."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from .identity import resolve_email
from .transfers import Transfers


def is_transfer(payload: dict[str, Any]) -> bool:
    if payload.get("type") == "view_submission":
        return bool(payload.get("view", {}).get("callback_id") == "pistachio_transfer_login_submit")
    return any(
        str(a.get("action_id", "")).startswith("pistachio_transfer_")
        for a in payload.get("actions", [])
    )


async def handle(service: Transfers, payload: dict[str, Any]) -> dict[str, Any]:
    modal = payload.get("type") == "view_submission"
    team, user = (
        str(payload.get("team", {}).get("id", "")),
        str(payload.get("user", {}).get("id", "")),
    )
    email = await asyncio.to_thread(
        resolve_email, service.app, service.settings, service.store, team, user
    )
    try:
        if not email:
            raise ValueError("Your Slack identity is not authorized.")
        if modal:
            metadata = json.loads(payload["view"]["private_metadata"])
            pid = str(metadata["pending_id"])
            action = "submit"
        else:
            action = payload["actions"][0]["action_id"].removeprefix("pistachio_transfer_")
            pid = str(payload["actions"][0]["value"])
        p = await service.pending(pid)
        if p["team"] != team:
            raise ValueError("This request belongs to a different workspace.")
        if not modal and payload.get("channel", {}).get("id") != p["channel"]:
            raise ValueError("This action belongs to a different channel.")
        service.authorize(p["project"], email, p)
        if modal:
            values = payload["view"]["state"]["values"]
            await service.save_login(
                pid,
                email,
                values["username"]["value"]["value"],
                values["password"]["value"]["value"],
                str(payload["view"]["id"]),
            )
            return {"response_action": "clear"}
        if action == "login":
            login = await service.login(p["project"])
            if login:
                service.can_manage(p["project"], email, login)
            elif email != p["requested_by"]:
                raise ValueError("Only the requester can enter the first login.")
            if p["state"] != "login":
                raise ValueError("This login request is no longer active.")
            view = {
                "type": "modal",
                "callback_id": "pistachio_transfer_login_submit",
                "title": {"type": "plain_text", "text": "Save AtoMx login"},
                "submit": {"type": "plain_text", "text": "Save"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "private_metadata": json.dumps({"project_id": p["project"], "pending_id": pid}),
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "plain_text",
                            "text": (
                                "Saved for future transfers by project members. "
                                "The login is not sent to the agent. Slack's input is not masked."
                            ),
                        },
                    },
                    *[
                        {
                            "type": "input",
                            "block_id": name,
                            "label": {"type": "plain_text", "text": label},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "value",
                                "max_length": maximum,
                            },
                        }
                        for name, label, maximum in (
                            ("username", "AtoMx username", 320),
                            ("password", "AtoMx password", 3000),
                        )
                    ],
                ],
            }
            await AsyncWebClient(token=service.app.slack_bot_token, timeout=5).views_open(
                trigger_id=payload["trigger_id"],
                view=view,
            )
        elif action == "confirm":
            if p["slack_user"] != user:
                raise ValueError("Only the requesting Slack user can confirm.")
            await service.confirm(pid, email)
        elif action == "cancel":
            await service.cancel(pid, email)
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
