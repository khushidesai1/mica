"""Resolve Slack identities without importing the browser's account services."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

NOT_ENABLED = "Your account is not enabled for Pistachio. Ask the administrator to enable it."


def normalize(email: str) -> str:
    return email.strip().lower()


def email_for(app: Any, team: str, user: str) -> str | None:
    mapping = app.slack_user_map
    email = normalize(str(mapping.get(f"{team}:{user}") or mapping.get(user) or ""))
    allowed = {normalize(str(value)) for value in getattr(app, "allowed_emails", ())}
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", email) or (allowed and email not in allowed):
        return None
    return email


def is_private(event: dict[str, Any]) -> bool:
    return str(event.get("channel", "")).startswith("D") and event.get("channel_type") in {
        None, "im",
    }


def user_key(email: str) -> str:
    return hashlib.sha256(normalize(email).encode()).hexdigest()[:32]


def target_email(app: Any, team: str, token: str) -> str:
    mention = re.fullmatch(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", token)
    if mention:
        result = email_for(app, team, mention[1])
    else:
        mail = re.fullmatch(r"<mailto:([^|>]+)(?:\|[^>]+)?>", token)
        target = normalize(mail[1] if mail else token)
        result = next((email_for(app, team, key.split(":", 1)[-1])
                       for key, value in app.slack_user_map.items()
                       if (":" not in key or key.startswith(team + ":"))
                       and normalize(value) == target), None)
    if not result:
        raise ValueError("That account is not enabled for Pistachio.")
    return result


async def post(token: str, channel: str, thread_ts: str, text: str, *, event_id: str) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    await AsyncWebClient(token=token, timeout=5).chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False, unfurl_media=False,
        client_msg_id=str(uuid5(NAMESPACE_URL, f"pistachio-message:{event_id}")),
    )
