"""Trusted worker transport; generated analysis code stays offline."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from . import backchannel

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool


class Client:
    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]], thread: str):
        self.send, self.thread = send, thread
        self.cursor: int | None = None
        self.last_poll = -float("inf")
        self.lock = asyncio.Lock()

    async def poll(self, *, force: bool = False) -> str:
        async with self.lock:
            if not force and time.monotonic() - self.last_poll < 10:
                return ""
            self.last_poll = time.monotonic()
            initial = self.cursor is None
            try:
                value = await self.send(backchannel.Call(after=self.cursor).model_dump())
            except Exception:
                return ("Project backchannel unavailable. "
                        "Required shared method decisions remain unverified.")
            self.cursor = int(value["cursor"])
            value["messages"] = [m for m in value["messages"] if m["thread"] != self.thread]
            if (not value["messages"] and not value.get("retention_gap")
                    and not value.get("has_more")
                    and not (initial and (value.get("recipes") or value.get("older_omitted")))):
                return ""
            return backchannel.GUIDANCE + "\n" + json.dumps(value)

    def tool(self) -> SdkMcpTool[Any]:
        from claude_agent_sdk import tool

        @tool(backchannel.TOOL, backchannel.DESCRIPTION, backchannel.Call.model_json_schema())
        async def coordinate(args: dict[str, Any]) -> dict[str, Any]:
            try:
                call = backchannel.Call.model_validate(args)
                value = await self.send(call.model_dump())
                return {"content": [{"type": "text", "text": json.dumps(value)}]}
            except ValueError as exc:
                return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            except Exception:
                return {"isError": True, "content": [{"type": "text", "text":
                    "Project backchannel unavailable; publication or receipt is unconfirmed. Retry."
                }]}

        return coordinate
