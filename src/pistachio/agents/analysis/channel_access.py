"""Approved Slack channels define admission; project authorization stays separate."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, Field
from slack_sdk import WebClient

from .config import Settings
from .identity import normalize
from .storage import Storage, update_model

KEY = "projects/_access/channels.json"
_LOCK = threading.RLock()
_CACHE: dict[str, tuple[float, Any]] = {}


class Channel(BaseModel):
    enabled: bool = True
    enabled_by: str


class Channels(BaseModel):
    version: Literal[1] = 1
    channels: dict[str, Channel] = Field(default_factory=dict)
    operations: dict[str, str] = Field(default_factory=dict)


def load(storage: Storage) -> Channels:
    try:
        return Channels.model_validate_json(storage.get(KEY)[0])
    except FileNotFoundError:
        return Channels()


def token_for(app: Any) -> str:
    token = app.slack_bot_token
    return str(token.get_secret_value() if hasattr(token, "get_secret_value") else token)


def cached(token: str, name: str, ttl: int, fetch: Callable[[], Any]) -> Any:
    key = hashlib.sha256((token + "\0" + name).encode()).hexdigest()
    with _LOCK:
        now = monotonic()
        prior = _CACHE.get(key)
        if prior and now < prior[0]:
            return prior[1]
        # Expired positives are never used after an API failure or a rate limit.
        _CACHE.pop(key, None)
        result = fetch()
        if len(_CACHE) > 2048:
            _CACHE.clear()
        _CACHE[key] = (monotonic() + ttl, result)
        return result


def client_for(app: Any) -> WebClient:
    return WebClient(token=token_for(app), timeout=5, retry_handlers=[])


def members(client: Any, channel: str) -> set[str]:
    result: set[str] = set()
    cursor = ""
    cursors = set()
    deadline = monotonic() + 15
    for _ in range(100):
        response = client.conversations_members(channel=channel, cursor=cursor, limit=200)
        if not response.get("ok") or not isinstance(response.get("members"), list):
            raise ValueError("Slack membership verification failed.")
        result.update(response["members"])
        cursor = response.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return result
        if cursor in cursors or monotonic() >= deadline:
            break
        cursors.add(cursor)
    raise ValueError("Slack membership verification was incomplete.")


def profile(app: Any, settings: Settings, team: str, user: str) -> str | None:
    """Identity lookup is scoped to this bot's workspace and never guesses an email."""
    token, client = token_for(app), client_for(app)
    auth = cached(token, "workspace", settings.access_cache_seconds, lambda: client.auth_test())
    if not auth.get("ok") or auth.get("team_id") != team:
        return None
    response = cached(
        token, "user:" + user, settings.access_cache_seconds, lambda: client.users_info(user=user)
    )
    value = response.get("user", {})
    if (
        not response.get("ok")
        or value.get("id") != user
        or value.get("deleted")
        or value.get("is_bot")
        or value.get("is_app_user")
    ):
        return None
    email = normalize(value.get("profile", {}).get("email") or "")

    return email if re.fullmatch(r"[^\s@]+@[^\s@]+", email) else None


def email_for(app: Any, settings: Settings, storage: Storage, team: str, user: str) -> str | None:
    # Registry membership is read on every check. Disabling a channel takes effect even
    # while its Slack member list is cached. Only the member lists/profile have a TTL.
    enabled = [
        key.split(":", 1)[1]
        for key, entry in load(storage).channels.items()
        if key.startswith(team + ":") and entry.enabled
    ]
    if not enabled:
        return None
    client, token = client_for(app), token_for(app)
    for channel in enabled:
        try:
            people = cached(
                token,
                "members:" + channel,
                settings.access_cache_seconds,
                partial(members, client, channel),
            )
            if user in people:
                return profile(app, settings, team, user)
        except Exception:
            # Another enabled channel can independently establish membership. Never use
            # an expired positive after an API failure or log Slack response bodies.
            continue
    return None


def target_email(
    app: Any, settings: Settings, storage: Storage, team: str, value: str, *, removing: bool = False
) -> str:
    mention = re.fullmatch(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", value)
    if mention:
        user = mention[1]
    else:
        mail = re.fullmatch(r"<mailto:([^|>]+)(?:\|[^>]+)?>", value)
        email = normalize(mail[1] if mail else value)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
            raise ValueError("Mention an enabled user or supply their Slack email.")
        if removing:
            return email  # Owners can revoke former members, including deactivated accounts.
        try:
            response = client_for(app).users_lookupByEmail(email=email)
            user = response["user"]["id"] if response.get("ok") else ""
        except Exception:
            user = ""
    try:
        result = (
            (
                profile(app, settings, team, user)
                if removing
                else email_for(app, settings, storage, team, user)
            )
            if user
            else None
        )
    except Exception:
        result = None
    if not result:
        raise ValueError("That account is not an enabled Slack channel member.")
    return result


def admin_command(
    app: Any, settings: Settings, storage: Storage | None, payload: dict[str, Any], event_id: str
) -> str | None:
    """Onboard via a signed mention, independently of ordinary member admission."""
    from .data_links import message_text

    event = payload.get("event", {})
    command = message_text(event.get("text", "")).lower()
    if command not in {"enable this channel", "disable this channel", "agent channels"}:
        return None
    if settings.access_mode != "channel":
        return "Channel onboarding requires AGENT_ACCESS_MODE=channel."
    assert storage is not None
    team, user, channel = (
        payload.get("team_id", ""),
        event.get("user", ""),
        event.get("channel", ""),
    )
    admin = f"{team}:{user}"
    if admin not in settings.csv("access_admin_ids"):
        return "A designated Pistachio admin must manage enabled channels."
    if command == "agent channels":
        if not channel.startswith("D"):
            return "Use agent channels in your DM with Pistachio."
        entries = [
            f"<#{key.split(':', 1)[1]}>"
            for key, c in load(storage).channels.items()
            if key.startswith(team + ":") and c.enabled
        ]
        return "Enabled access channels: " + (", ".join(sorted(entries)) or "none")
    if not channel.startswith(("C", "G")) or event.get("type") != "app_mention":
        return "In the channel, mention Pistachio with enable this channel or disable this channel."
    enabled = command == "enable this channel"
    if enabled:
        try:
            client = client_for(app)
            auth = client.auth_test()
            response = client.conversations_info(channel=channel)
            info: Any = response.get("channel", {})
            if (
                not auth.get("ok")
                or auth.get("team_id") != team
                or not response.get("ok")
                or not info.get("is_member")
                or info.get("is_archived")
                or info.get("is_im")
                or info.get("is_mpim")
            ):
                raise ValueError("unavailable")
            if not profile(app, settings, team, user):
                raise ValueError("Slack email lookup unavailable")
            # Verify scope and complete pagination now, not on the first user's request.
            members(client, channel)
        except Exception:
            return (
                "Could not verify this channel. Invite Pistachio and grant membership and email "
                "permissions, then retry."
            )
    fingerprint = hashlib.sha256(f"{admin}:{channel}:{command}".encode()).hexdigest()

    def change(doc: Channels) -> None:
        if event_id in doc.operations:
            if doc.operations[event_id] != fingerprint:
                raise ValueError("Channel operation identity was reused.")
            return
        doc.channels[f"{team}:{channel}"] = Channel(enabled=enabled, enabled_by=admin)
        doc.operations[event_id] = fingerprint

    doc = update_model(storage, KEY, Channels, change)
    # Reflect current state on replay; an old enable never reverses a later disable.
    active = doc.channels[f"{team}:{channel}"].enabled
    if active:
        return (
            "This channel is enabled for Pistachio access. Members can use the agent here "
            "or in DMs. Projects still require explicit sharing and channel publication; "
            "personal memory remains private."
        )
    return (
        "This channel is disabled for Pistachio access. People retain access only if they "
        "belong to another enabled channel. Existing messages remain."
    )
