"""Resolve Slack identities independently of the shared task API account services."""

from __future__ import annotations

import hashlib
import re
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Any
from uuid import NAMESPACE_URL, uuid5

NOT_ENABLED = "Your account is not enabled for Mica. Ask the administrator to enable it."


class IdentityUnavailable(Exception):
    """Slack could not verify identity; this is not a negative access decision."""


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
    return event.get("channel_type") == "email" or (
        str(event.get("channel", "")).startswith("D")
        and event.get("channel_type")
        in {
            None,
            "im",
        }
    )


def user_key(email: str) -> str:
    return hashlib.sha256(normalize(email).encode()).hexdigest()[:32]


def recipient_email(token: str) -> str:
    """An explicit email identifies a project member, not a Slack account."""
    mail = re.fullmatch(r"<mailto:([^|>]+)(?:\|[^>]+)?>", token)
    value = normalize(mail[1] if mail else token)
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as exc:
        raise ValueError("Supply one complete email address or a Slack user mention.") from exc
    if (
        not value.isascii()
        or any(c.isspace() for c in value)
        or not address.username
        or address.addr_spec != value
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", address.domain)
    ):
        raise ValueError("Supply one complete email address or a Slack user mention.")
    return value


def target_email(app: Any, team: str, token: str) -> str:
    mention = re.fullmatch(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", token)
    if not mention:
        return recipient_email(token)
    result = email_for(app, team, mention[1])
    if not result:
        raise ValueError("That Slack account is not enabled. Supply their email address instead.")
    return result


def member_email(
    app: Any, settings: Any, storage: Any, team: str, token: str, *, removing: bool = False
) -> str:
    """Resolve only the recipient; actor authentication remains independently fail-closed."""
    try:
        if settings.access_mode in {"channel", "workspace"}:
            from .channel_access import target_email as channel_target

            return channel_target(app, settings, storage, team, token, removing=removing)
        return target_email(app, team, token)
    except IdentityUnavailable as exc:
        # Recipient lookup failures must not trap an authenticated owner's request in
        # the actor-admission retry loop. No registry mutation has happened yet.
        raise ValueError(
            "I couldn't verify that Slack recipient right now. No access was changed. "
            "Please retry or supply their complete email address."
        ) from exc


async def post(token: str, channel: str, thread_ts: str, text: str, *, event_id: str) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    await AsyncWebClient(token=token, timeout=5).chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=text,
        unfurl_links=False,
        unfurl_media=False,
        client_msg_id=str(uuid5(NAMESPACE_URL, f"pistachio-message:{event_id}")),
    )


def resolve_email(app: Any, settings: Any, storage: Any, team: str, user: str) -> str | None:
    if settings.access_mode == "workspace":
        from .channel_access import profile

        return profile(app, settings, team, user)
    if settings.access_mode == "channel":
        from .channel_access import email_for as channel_email

        return channel_email(app, settings, storage, team, user)
    if settings.allowed_user_ids and user not in settings.csv("allowed_user_ids"):
        return None
    return email_for(app, team, user)
