"""Bounded advisory worker checks; durable memory validation remains authoritative."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import memory_workspace, project_memory, session_review, thread_memory
from .config import Settings
from .script_capture import read_regular

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool


def turn_tool(work: Path, thread: str) -> SdkMcpTool[Any]:
    from claude_agent_sdk import tool

    @tool(
        "record_turn",
        "Record this turn before finishing. Use concise paraphrases and methods, never "
        "transcripts. Incorporate prior context into the rolling summary; do not copy log or "
        "summary wrappers. Identity, sequence and coverage are supplied by the application. "
        "A later call replaces this turn's earlier record.",
        thread_memory.Prose.model_json_schema(),
    )
    async def record(args: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.to_thread(memory_workspace.record_turn, work, thread, args)
        except (OSError, ValueError):
            return {"isError": True, "content": [{"type": "text", "text":
                "Invalid or oversized turn fields, or unsafe thread folder/file. Use the schema "
                "and an ordinary knowledge thread folder."}]}
        return {"content": [{"type": "text", "text": "Turn recorded."}]}

    return record


class Checks:
    def __init__(
        self,
        work: Path,
        settings: Settings,
        thread: str,
        base: project_memory.Snapshot | None,
        highlights: list[str] | None,
    ) -> None:
        self.work, self.settings, self.thread = work, settings, thread
        self.base, self.highlights = base, highlights
        self.attempts: dict[str, int] = {}

    def reasons(self) -> dict[str, str]:
        reasons = {}
        if self.thread and thread_memory.enabled(self.settings):
            path = Path("knowledge") / thread_memory.prefix(self.thread) / "turn.json"
            try:
                thread_memory.Prose.model_validate_json(read_regular(self.work, path))
            except (OSError, ValueError):
                reasons["thread"] = "Call record_turn before finishing."
        if self.base is not None and self.highlights is not None:
            missing = []
            for path in sorted((self.work / "knowledge/results").glob("*.md")):
                try:
                    text = read_regular(self.work, path.relative_to(self.work)).decode()
                    before = self.base.files.get("results/" + path.name)
                    if (not before or before.text != text) and not re.search(
                        r"(?m)^## Definitions[ \t]*\r?$", text
                    ):
                        missing.append(path.name)
                except (OSError, ValueError):
                    continue  # The proposal validator rejects unsafe entries.
            if missing:
                reasons["results"] = (
                    "Add a Definitions section to changed knowledge/results entries: "
                    + ", ".join(missing[:20])
                    + ". Include group rules, n, units and models; "
                    "mark unavailable qualifiers unknown."
                )
        if self.highlights is not None:
            try:
                session_review.read(self.work, self.highlights)
            except (OSError, ValueError):
                reasons["import"] = (
                    "Write valid notes/import-review.json accounting for highlight IDs "
                    + json.dumps(self.highlights)
                    + ". Schema: "
                    + json.dumps(session_review.Review.model_json_schema())
                )
        return reasons

    def check(self) -> str:
        blocked = []
        for kind, reason in self.reasons().items():
            count = self.attempts.get(kind, 0)
            if count < 2:
                self.attempts[kind] = count + 1
                blocked.append(reason)
            else:
                logging.getLogger(__name__).warning("Worker memory check unresolved: %s", kind)
        return "\n".join(blocked)
