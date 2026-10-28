from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from .progress import TaskProgress


def _rich_text(text: str) -> dict[str, Any]:
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}],
    }


def activity_blocks(
    tasks: Sequence[TaskProgress],
    *,
    finished: bool = False,
    error: str | None = None,
    check_in: str = "",
    headline: str = "Mica is working",
    summary: str = "",
) -> list[dict[str, Any]]:
    """One compact native disclosure; only public activity summaries go inside."""
    active = next((task for task in reversed(tasks) if task.status == "in_progress"), None)
    thinking = headline in {"Thinking…", "Mica is thinking"}
    title = "Mica is thinking" if thinking else "Mica is working"
    if finished:
        title = "Mica stopped" if error else "Mica finished"
    visible = list(tasks[-2:])
    if active is not None and active not in visible:
        visible = [visible[-1], active]
    lines = []
    if error:
        lines.append(f"Stopped: {error[:300]}")
    if thinking and not finished:
        lines.append(
            "Thinking: " + ("Reviewing the latest results." if tasks else "Reviewing your request.")
        )
    for task in visible:
        label = " ".join(task.title.split())[:150] or "Working on the request"
        if task.status == "complete":
            lines.append(f"✓ {label}")
        elif task.status == "error" or finished:
            lines.append(f"⚠ {label} — completion was not confirmed.")
        else:
            lines.append(f"Working: {label}")
    if summary and not thinking and not active:
        lines.append(
            ("Last activity: " if finished else "Working: ") + " ".join(summary.split())[:150]
        )
    if not lines:
        lines.append("Finished." if finished else "Preparing your request.")
    if check_in and not finished:
        lines.append(check_in[:1500])
    return [
        {
            "type": "plan",
            "title": title,
            "tasks": [
                {
                    "task_id": "mica-activity",
                    "title": "Activity summary",
                    "hide_title": True,
                    "status": "error" if error else "complete" if finished else "in_progress",
                    "details": _rich_text("\n".join(lines)),
                }
            ],
        }
    ]


def activity_fallback(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unsupported native disclosures retain a small, muted context status."""
    result = []
    for block in blocks:
        if block.get("type") != "plan":
            result.append(block)
            continue
        details = block["tasks"][0]["details"]["elements"][0]["elements"][0]["text"]
        result.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "plain_text",
                        "text": (block["title"] + "\n" + details)[:3000],
                        "emoji": False,
                    }
                ],
            }
        )
    return result


def status_blocks(_emoji: str, headline: str, steps: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Queue, startup and model activity share the same expandable control."""
    current = steps[-1] if steps else headline
    if current in {"Thinking…", "Working on it…", "Mica is thinking", "Mica is working"}:
        current = ""
    return activity_blocks([], headline=headline, summary=current)


def to_mrkdwn(text: str) -> str:
    # Leave code fences and inline code untouched.
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    for i in range(0, len(parts), 2):
        value = parts[i].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        value = re.sub(r"^#{1,6}\s+", "", value, flags=re.M)
        value = re.sub(r"\*\*(.+?)\*\*", r"*\1*", value)
        parts[i] = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", value)
    return "".join(parts)


def chunks(text: str, limit: int = 3500) -> Iterator[str]:
    while len(text) > limit:
        end = text.rfind("\n", 0, limit + 1)
        end = end if end > limit // 2 else limit
        yield text[:end]
        text = text[end:].lstrip("\n")
    if text:
        yield text
