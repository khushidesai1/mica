from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from .progress import TaskProgress


def activity_blocks(
    tasks: Sequence[TaskProgress],
    *,
    finished: bool = False,
    error: str | None = None,
    check_in: str = "",
) -> list[dict[str, Any]]:
    """Slack's expandable plan; keep two cards, always including the active task."""
    active = next((task for task in reversed(tasks) if task.status == "in_progress"), None)
    title = active.phase if active else "Reviewing the latest results…"
    if finished:
        title = f"Stopped: {error}" if error else "Finished"
    cards = []
    visible = list(tasks[-2:])
    if active is not None and active not in visible:
        visible = [visible[-1], active]
    for task in visible:
        card: dict[str, Any] = {
            "task_id": task.task_id,
            "title": task.title[:150] or "Working on the request",
            "status": task.status,
        }
        if check_in and not finished and task == (active or tasks[-1]):
            card["details"] = {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": check_in[:1500]}],
                    }
                ],
            }
        if finished and task.status == "in_progress":
            # Do not infer tool success from the overall turn finishing.
            card["status"] = "error"
            card["details"] = {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "Completion was not confirmed."}],
                    }
                ],
            }
        cards.append(card)
    return [{"type": "plan", "title": title[:150], "tasks": cards}]


def status_blocks(emoji: str, headline: str, steps: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Show the current action and recent steps without parsing model-derived labels."""
    thinking = headline in {"Thinking…", "Mica is thinking"}
    label = "Mica is thinking" if thinking else "Mica is working"
    elements = [
        {"type": "plain_text", "text": label, "emoji": False},
        {"type": "mrkdwn", "text": emoji},
    ]
    blocks: list[dict[str, Any]] = [{"type": "context", "elements": elements}]
    current = steps[-1] if steps else headline
    if current not in {"Thinking…", "Working on it…", "Mica is thinking", "Mica is working"}:
        elements = [{"type": "plain_text", "text": current[:150], "emoji": False}]
        blocks.append({"type": "context", "elements": elements})
    return blocks


def done_blocks(steps: Sequence[str], error: str | None = None) -> list[dict[str, Any]]:
    """Collapse progress to one context line without a duration."""
    text = (
        f"⚠️ Stopped: {error}"
        if error
        else "✓ " + (" · ".join(s[:80] for s in steps[-3:]) or "Finished")
    )
    return [
        {"type": "context", "elements": [{"type": "plain_text", "text": text[:300], "emoji": True}]}
    ]


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
