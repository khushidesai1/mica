"""Generation-checked thread history merges shared by the API and worker bootstrap."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from google.api_core.exceptions import PreconditionFailed

from . import attachments
from .storage import Conflict, Storage


def merge(
    storage: Storage, key: str, state: dict[str, Any], *, messages_only: bool = False,
) -> dict[str, Any]:
    """Append concurrent exchanges; only the active turn owns session/context fields."""
    for _ in range(8):
        try:
            raw, generation = storage.get(key)
            current = json.loads(raw)
        except FileNotFoundError:
            current, generation = {}, None
        except PreconditionFailed:
            continue
        result = dict(current) if messages_only else {**current, **state}
        def identity(message: dict[str, Any]) -> str:
            return str(message.get("id") or hashlib.sha256(
                json.dumps(message, sort_keys=True).encode()).hexdigest())

        messages = {identity(m): m for m in current.get("messages", [])}
        messages.update({identity(m): m for m in state.get("messages", [])})
        # Legacy exchanges have no sequence and retain their insertion order.
        result["messages"] = sorted(messages.values(), key=lambda m: m.get("seq", 0))[-4:]
        if not messages_only:
            result["thread_files"] = attachments.merge(
                current.get("thread_files", []), state.get("thread_files", []),
            )
        try:
            storage.put_if(key, json.dumps(result, default=str).encode(), generation)
            return result
        except Conflict:
            continue
    raise Conflict("Thread checkpoint changed concurrently; retry this turn.")
