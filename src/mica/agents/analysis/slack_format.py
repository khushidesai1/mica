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
    emoji: str = "",
) -> list[dict[str, Any]]:
    """One initially collapsed control; only public activity summaries go inside."""
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
    header = _rich_text(title + (" " if emoji and not finished else ""))
    if emoji and not finished:
        # The configured loading icon is trusted; model labels remain plain text.
        name = re.fullmatch(r":([a-zA-Z0-9_+\-]+):", emoji)
        header["elements"][0]["elements"].append(
            {"type": "emoji", "name": name[1]} if name else {"type": "text", "text": emoji}
        )
    return [
        {
            "type": "container",
            "title": {"type": "plain_text", "text": title, "emoji": False},
            "rich_text_title": header,
            "width": "narrow",
            "is_collapsible": True,
            "default_collapsed": True,
            "child_blocks": [_rich_text("\n".join(lines))],
        }
    ]


def activity_fallback(blocks: Sequence[dict[str, Any]], *, plain: bool) -> list[dict[str, Any]]:
    """Older Slack clients retain a native plan before falling back to plain status."""
    result = []
    for block in blocks:
        if block.get("type") != "container":
            result.append(block)
            continue
        title = block["title"]["text"]
        details = block["child_blocks"][0]
        if plain:
            result.extend([_rich_text(title), details])
        else:
            result.append(
                {
                    "type": "plan",
                    "title": title,
                    "tasks": [
                        {
                            "task_id": "mica-activity",
                            "title": "Activity summary",
                            "status": (
                                "complete"
                                if title == "Mica finished"
                                else "error"
                                if title == "Mica stopped"
                                else "in_progress"
                            ),
                            "details": details,
                        }
                    ],
                }
            )
    return result


def status_blocks(emoji: str, headline: str, steps: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Queue, startup and model activity share the same expandable control."""
    current = steps[-1] if steps else headline
    if current in {"Thinking…", "Working on it…", "Mica is thinking", "Mica is working"}:
        current = ""
    return activity_blocks([], headline=headline, summary=current, emoji=emoji)


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
