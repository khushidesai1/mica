"""Trusted surface policy and stable, private email conversation identities."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

EMAIL_STATE_PREFIX = "projects/_gmail"
EMAIL_POLICY = "email:dkim-v1"
EMAIL_READ_ONLY = (
    "Email supports reading project data and returning analysis artifacts. "
    "Use your Slack DM with Mica for bucket writes, transfers, and access controls."
)


def is_email(record: dict[str, Any]) -> bool:
    # This marker is set by trusted intake, never inferred from message text.
    return record.get("surface") == "email"


def email_keys(payload: dict[str, Any], principal: str) -> tuple[str, str]:
    meta = payload.get("_email", {})
    event = payload.get("event", {})
    mailbox = str(meta.get("mailbox", "")).strip().lower()
    if (
        payload.get("team_id") != "EMAIL"
        or event.get("channel_type") != "email"
        or event.get("user") != principal
        or not re.fullmatch(r"[^\s@]+@[^\s@]+", principal)
        or not re.fullmatch(r"[^\s@]+@[^\s@]+", mailbox)
        or not all(re.fullmatch(r"[a-f0-9]+", str(meta.get(k, ""))) for k in ("id", "thread_id"))
    ):
        raise ValueError("Invalid verified email identity")

    def key(*parts: str) -> str:
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]

    return (
        key("email-message", mailbox, meta["id"]),
        key("email-thread", mailbox, principal, meta["thread_id"]),
    )


def valid_email_record(record: dict[str, Any], principal: str) -> bool:
    try:
        _, thread = email_keys(record["payload"], principal)
    except (ValueError, KeyError, TypeError):
        return False
    return record.get("thread_key") == thread
