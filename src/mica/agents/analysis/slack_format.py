from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from html import escape
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


def neutralize(text: str) -> str:
    """Model angle syntax stays literal, including in notification fallbacks."""
    return escape(text, quote=False)


def markdown_text(text: str) -> str:
    """Escape prose while keeping literal code intact in Slack's Markdown parser."""
    result: list[str] = []
    cursor = 0
    token = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)\n|\\.|`+", re.M)
    while match := token.search(text, cursor):
        result.append(neutralize(text[cursor:match.start()]))
        start, end = match.span()
        if match[0].startswith("\\"):
            result.append(neutralize(match[0]))
        elif match[1] and not (match[1][0] == "`" and "`" in match[2]):
            delimiter = match[1]
            closing = re.compile(
                r"^ {0,3}" + re.escape(delimiter[0]) + "{" + str(len(delimiter))
                + r",}[ \t]*(?:\n|$)", re.M,
            ).search(text, end)
            end = closing.end() if closing else len(text)
            result.append(text[start:end])
        elif not match[1]:
            # Only a same-line, matching backtick run is unambiguously an inline span.
            closing = re.compile(r"(?<!`)" + re.escape(match[0]) + r"(?!`)").search(text, end)
            if closing and "\n" not in text[end:closing.start()]:
                end = closing.end()
                result.append(text[start:end])
            else:
                result.append(neutralize(match[0]))
        else:
            result.append(neutralize(match[0]))
        cursor = end
    result.append(neutralize(text[cursor:]))
    return "".join(result)


def answer_blocks(text: str) -> list[dict[str, Any]]:
    value = markdown_text(text)
    if len(value) > 12000:
        raise ValueError("Markdown answer exceeds Slack's message limit")
    return [{"type": "markdown", "text": value or " "}]


def _plain_chunks(text: str, limit: int) -> Iterator[str]:
    while len(text) > limit:
        end = text.rfind("\n", 0, limit + 1)
        end = end if end > limit // 2 else limit
        yield text[:end]
        text = text[end:].lstrip("\n")
    if text:
        yield text


def _table_separator(line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", c) for c in cells)


def chunks(text: str, limit: int = 3500) -> Iterator[str]:
    """Keep Markdown units together; oversized units continue with valid syntax.

    `limit` is the target for prose. A whole code block/table may use Slack's 12k budget.
    Escaping is included in that budget; callers retain the original text for persistence.
    """
    if limit < 32:
        raise ValueError("Chunk limit must allow Markdown delimiters")
    limit = min(limit, 12000)
    lines = text.splitlines(keepends=True)
    units: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})[^\n]*", line)
        if fence:
            start = index
            delimiter = fence[1]
            closing = None
            index += 1
            while index < len(lines):
                closing = re.fullmatch(
                    r" {0,3}" + re.escape(delimiter[0]) + "{" + str(len(delimiter)) + r",}\s*",
                    lines[index],
                )
                index += 1
                if closing:
                    break
            unit = "".join(lines[start:index])
            if len(neutralize(unit)) <= 12000:
                units.append(unit)
            else:
                # Each continuation is a complete fenced block, with the same language.
                opening = lines[start].rstrip("\n") + "\n"
                body = "".join(lines[start + 1:index - 1 if closing else index])
                budget = min(limit, 12000) - len(neutralize(opening + "\n" + delimiter))
                if budget < 32:
                    units.extend(_bounded_plain(unit, limit))
                else:
                    for part in _bounded_plain(body, budget):
                        units.append(opening + part.rstrip("\n") + "\n" + delimiter + "\n")
        elif index + 1 < len(lines) and "|" in line and _table_separator(lines[index + 1]):
            header = "".join(lines[index:index + 2])
            index += 2
            if len(neutralize(header)) > 12000:
                units.extend(_bounded_plain(header, limit))
                continue
            table = header
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = lines[index]
                if len(neutralize(table + row)) > 12000:
                    units.append(table)
                    table = header
                if len(neutralize(header + row)) > 12000:
                    # An individual row larger than Slack's ceiling is delivered literally.
                    units.extend(_bounded_plain(row, limit))
                else:
                    table += row
                index += 1
            units.append(table)
        else:
            units.extend(_bounded_plain(line, limit))
            index += 1
    pending = ""
    for unit in units:
        if pending and len(neutralize(pending + unit)) > limit:
            if pending.rstrip("\n"):
                yield pending.rstrip("\n")
            pending = ""
        pending += unit
    if pending.rstrip("\n"):
        yield pending.rstrip("\n")


def _bounded_plain(text: str, limit: int) -> Iterator[str]:
    for piece in _plain_chunks(text, limit):
        while len(neutralize(piece)) > limit:
            end = limit
            while len(neutralize(piece[:end])) > limit:
                end -= 1
            yield piece[:end]
            piece = piece[end:]
        if piece:
            yield piece
