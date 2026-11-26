"""Sticky requester-private retention for conversations containing personal repository data."""

from __future__ import annotations

import json
import re
from typing import Any

from .storage import Conflict, Storage


def key(thread: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread):
        raise ValueError("Invalid private conversation")
    return f"write-access/private-conversations/{thread}.json"


def owner(storage: Storage, thread: str) -> str:
    try:
        raw, _ = storage.get(key(thread))
    except FileNotFoundError:
        return ""
    return str(json.loads(raw)["owner"])


def inherit(storage: Storage, record: dict[str, Any]) -> None:
    if record.get("thread_key") and owner(storage, record["thread_key"]):
        record.update(private_contribution=True, backchannel_enabled=False, write_grant=None)


def protect(storage: Storage, record: dict[str, Any], principal: str) -> None:
    thread = record["thread_key"]
    current = owner(storage, thread)
    if current and current != principal:
        raise PermissionError("This private repository conversation belongs to another user")
    if not current:
        try:
            storage.put_if(key(thread), json.dumps({"owner": principal}).encode(), None)
        except Conflict:
            if owner(storage, thread) != principal:
                raise PermissionError("Private conversation ownership changed") from None
    inherit(storage, record)
