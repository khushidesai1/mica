"""A bounded, snapshot-only read loop for internal memory models."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

READ_RULES = """
Use the titled index to choose relevant entries. Read complete files with read_memory_files
before replacing them. At most two read rounds are available, with up to eight paths each.
Reads use the supplied immutable snapshot. A context_limit or unavailable result does not
expose a file and cannot authorize replacing it. Finish with the final tool; an empty change
set is valid when the available context does not support a safe edit.
"""


class ReadFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=8)


class ModelError(ValueError):
    """A stable error code without model output or memory contents."""


@dataclass
class Result:
    value: Any
    seen: set[str]


def chars(files: Mapping[str, str]) -> int:
    return len(json.dumps(files, ensure_ascii=True))


async def run(
    key: str, model: str, context: dict[str, Any], *, system: str,
    final_tool: dict[str, Any], files: Mapping[str, str], remaining_chars: int,
    timeout: float, allow_reads: bool = True,
) -> Result:
    """Share one deadline and one file budget across reads and the final proposal.

    The caller persists a single attempt before entering this loop. Only complete,
    successfully supplied files enter `seen`; inventory rows never authorize edits.
    """
    seen = set(context.get("files", {}))
    messages: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(context)}]
    read_tool = {
        "name": "read_memory_files",
        "description": "Read up to eight complete memory files from the cached snapshot",
        "input_schema": ReadFiles.model_json_schema(),
    }
    remaining = max(0, remaining_chars)
    async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout) as client:
        for round_number in range(3):
            can_read = allow_reads and round_number < 2
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={
                    "model": model,
                    "max_tokens": 16384,
                    "system": system + (READ_RULES if allow_reads else ""),
                    "messages": messages,
                    "tools": [final_tool, read_tool] if can_read else [final_tool],
                    "tool_choice": (
                        {"type": "any", "disable_parallel_tool_use": True} if can_read else
                        {"type": "tool", "name": final_tool["name"],
                         "disable_parallel_tool_use": True}
                    ),
                },
            )
            response.raise_for_status()
            body = response.json()
            if body.get("stop_reason") == "max_tokens":
                raise ModelError("output_limit")
            calls = [b for b in body.get("content", []) if b.get("type") == "tool_use"]
            if len(calls) != 1:
                raise ModelError("incomplete_output")
            call = calls[0]
            if call.get("name") == final_tool["name"]:
                return Result(value=call.get("input"), seen=seen)
            if not can_read or call.get("name") != "read_memory_files" or not call.get("id"):
                raise ModelError("incomplete_output")
            output: dict[str, Any] = {}
            try:
                paths = ReadFiles.model_validate(call.get("input")).paths
            except ValidationError:
                output = {"error": "invalid_paths", "max_paths": 8}
            else:
                for path in dict.fromkeys(paths):
                    if path not in files:
                        output[path] = {"error": "unavailable"}
                        continue
                    text = files[path]
                    cost = chars({path: text})
                    if cost > remaining:
                        output[path] = {"error": "context_limit"}
                        continue
                    output[path] = text
                    remaining -= cost
                    seen.add(path)
            messages.extend([
                {"role": "assistant", "content": body["content"]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call["id"],
                    "content": json.dumps(output),
                }]},
            ])
    raise ModelError("incomplete_output")
