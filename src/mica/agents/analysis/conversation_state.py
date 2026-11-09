"""Private, resumable conversation state; delivery windows are not task deadlines."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .storage import Storage


class YieldConversation(Exception):
    """A healthy conversation checkpointed and should get another queue delivery."""


class CancelConversation(Exception):
    """Cancellation won before another model/tool/terminal action."""


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid conversation request")
    return f"write-access/conversations/{event_id}/state.json"


async def load(storage: Storage, event_id: str) -> dict[str, Any] | None:
    try:
        raw, _ = await asyncio.to_thread(storage.get, key(event_id))
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Unsupported conversation checkpoint")
    return value


async def save(storage: Storage, event_id: str, value: dict[str, Any]) -> None:
    await asyncio.to_thread(storage.put, key(event_id), json.dumps(value).encode())


async def archive(storage: Storage, event_id: str, value: dict[str, Any]) -> None:
    path = key(event_id).removesuffix("state.json") + f"context-{value['compactions']}.json"
    await asyncio.to_thread(storage.put, path, json.dumps(value).encode())


def evidence(value: Any) -> Any:
    """Summarize ordinary evidence as text, not provider tool/thinking blocks.

    The exact transcript remains archived. Binary media can be viewed again by its
    authorized file reference; base64 payloads must not consume a text context window.
    """
    if isinstance(value, list):
        return [evidence(item) for item in value if not (
            isinstance(item, dict) and item.get("type") in {"thinking", "redacted_thinking"}
        )]
    if isinstance(value, dict):
        if value.get("type") in {"base64", "image", "document"}:
            return {"media": "Retained in archived evidence; use its file reference to view again."}
        return {k: evidence(v) for k, v in value.items()}
    return value
