from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any, Literal

from .progress import TaskProgress, note_headline, public_note

_GENERIC_ACTIVITY = {"Thinking…", "Working on it…", "Mica is thinking", "Mica is working"}
NoticeState = Literal["working", "waiting", "cancelled", "expired"]


def context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "plain_text", "text": text[:3000]}]}


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
    note: str = "",
    native_session: bool = False,
) -> list[dict[str, Any]]:
    """One compact disclosure with native task rows and public activity details."""
    active = next((task for task in reversed(tasks) if task.status == "in_progress"), None)
    thinking = headline in {"Thinking…", "Mica is thinking"}
    title = "Mica is thinking" if thinking else "Mica is working"
    note = "" if finished else public_note(note)
    if finished:
        title = "Mica stopped" if error else "Mica finished"
    else:
        specific = next(
            (text for text in (active.title if active else "", headline,
                               "" if thinking else summary)
             if text and text not in _GENERIC_ACTIVITY),
            "",
        )
        title = note_headline(specific) or title
    visible = list(tasks[-2:])
    if active is not None and active not in visible:
        visible = [visible[-1], active]
    if native_session and not finished:
        visible = [task for task in tasks if task.status != "in_progress"][-2:]
    lines = []
    if native_session and active and not finished:
        current = " ".join(active.title.split())[:150]
        if current != title:
            lines.append("Current step: " + current)
    if error:
        lines.append(f"Stopped: {error[:300]}")
    if summary and not note and not thinking and not active:
        lines.append(
            ("Last activity: " if finished else "Working: ") + " ".join(summary.split())[:150]
        )
    if check_in and not finished:
        lines.append(check_in[:1500])
    active_count = sum(task.status == "in_progress" for task in tasks)
    if active_count > 1 and not finished:
        lines.append(f"{active_count} steps in progress; showing the most recent activity.")
    cards: list[dict[str, Any]] = []
    for task in visible:
        card: dict[str, Any] = {
            "task_id": task.task_id,
            "title": " ".join(task.title.split())[:150] or "Working on the request",
            "status": "error" if finished and task.status == "in_progress" else task.status,
        }
        if card["status"] == "error":
            card["details"] = _rich_text("Completion was not confirmed.")
        cards.append(card)
    if error and cards and not any(card["status"] == "error" for card in cards):
        # A delivery/turn failure does not invalidate tasks already confirmed complete.
        cards = cards[-1:] + [
            {"task_id": "mica-terminal-error", "title": "Stopped", "status": "error"}
        ]
    if not cards and (not native_session or finished or error):
        cards.append({
            "task_id": "mica-activity",
            "title": "Stopped" if error else "Finished" if finished else "Preparing your request",
            "status": "error" if error else "complete" if finished else "in_progress",
        })
    if lines and cards:
        details = cards[-1].get("details")
        if details:
            lines.insert(0, details["elements"][0]["elements"][0]["text"])
        cards[-1]["details"] = _rich_text("\n".join(lines))
    blocks: list[dict[str, Any]] = [{"type": "plan", "title": title, "tasks": cards}]
    if lines and not cards:
        blocks.append(context_block("\n".join(lines)))
    if note:
        # Public commentary is separate from tool activity and the eventual final answer.
        # Literal text prevents model-generated mentions, links or formatting from executing.
        blocks.insert(0, {"type": "section", "text": {"type": "plain_text", "text": note}})
    return blocks


def activity_fallback(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unsupported native disclosures retain a small, muted context status."""
    result = []
    for block in blocks:
        if block.get("type") != "plan":
            result.append(block)
            continue
        failed = any(task["status"] == "error" for task in block["tasks"])
        lines = [("🚩 " if failed else "") + block["title"]]
        for task in block["tasks"]:
            label = {"complete": "Done", "error": "Stopped"}.get(task["status"], "Working")
            lines.append(f"{label}: {task['title']}")
            if details := task.get("details"):
                lines.append(details["elements"][0]["elements"][0]["text"])
        result.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "plain_text",
                        "text": "\n".join(lines)[:3000],
                        "emoji": False,
                    }
                ],
            }
        )
    return result


def status_blocks(
    _emoji: str, headline: str, steps: Sequence[str] = (), *,
    detail: str = "", state: NoticeState = "working", note: str = "",
    native_session: bool = False,
) -> list[dict[str, Any]]:
    """Queue, startup and model activity share the same expandable control."""
    if state != "working":
        return [context_block("\n".join(part for part in (headline, detail) if part))]
    current = steps[-1] if steps else headline
    if current in _GENERIC_ACTIVITY:
        current = ""
    if current and note_headline(current) != current:
        detail = "\n".join(part for part in (current, detail) if part)
    return activity_blocks(
        [], headline=current or headline, check_in=detail, note=note,
        native_session=native_session,
    )


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
