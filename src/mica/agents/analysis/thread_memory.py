"""Application-owned thread ordering, compact event logs and explicit summary coverage.

Models supply paraphrases. Only the controller binds identities, sequences and coverage.
This module has no storage credentials and is also used by the offline worker.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings

IDENTIFIER = r"[A-Za-z0-9_-]{1,128}"
PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
ASSET = rf"{PART}(?:/{PART}){{0,8}}"
THREAD_ENTRY = (
    rf"threads/{IDENTIFIER}/(?:summary\.md|log(?:-[0-9]{{4}})?\.md|"
    rf"scripts/(?:MANIFEST\.json|{ASSET})|outputs/(?:MANIFEST\.json|turn-[0-9]+/{ASSET}))"
)
PATH = re.compile(THREAD_ENTRY)
LOG = re.compile(rf"threads/{IDENTIFIER}/log(?:-[0-9]{{4}})?\.md")
HEADER = "type: thread\nsource: Mica application\nstatus: active\n"


class Prose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paraphrase: str = Field(min_length=1, max_length=1500)
    methods: str = Field(default="", max_length=2500)
    summary: str = Field(default="", max_length=10000)
    progress: str = Field(default="", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=12)
    related: list[str] = Field(default_factory=list, max_length=12)


class Entry(BaseModel):
    event_id: str = Field(pattern=rf"^{IDENTIFIER}$")
    seq: int = Field(ge=0)
    date: str = Field(max_length=64)
    speaker: str = Field(max_length=250)
    success: bool
    paraphrase: str = Field(min_length=1, max_length=1500)
    methods: str = Field(default="", max_length=2500)
    outputs: list[str] = Field(default_factory=list, max_length=100)
    scripts: list[str] = Field(default_factory=list, max_length=200)
    rejected: dict[str, str] = Field(default_factory=dict, max_length=200)

    def line(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True) + "\n"

    def digest(self) -> str:
        return hashlib.sha256(self.line().encode()).hexdigest()


class Update(BaseModel):
    thread: str = Field(pattern=rf"^{IDENTIFIER}$")
    surface: Literal["dm", "channel", "email"]
    entry: Entry
    prose: Prose
    # Derived from the immutable input snapshot, never accepted from model output.
    covered: list[str] = Field(default_factory=list)
    summary_only: bool = False
    scripts: dict[str, str] | None = None


class Summary(BaseModel):
    thread: str
    surface: str
    turn: int
    date: str
    people: list[str]
    covered: list[str]
    progress: str
    tags: list[str]
    related: list[str]
    text: str

    def encode(self) -> str:
        fields = self.model_dump(exclude={"text"})
        return (
            HEADER
            + "".join(
                f"{key}: {json.dumps(value, ensure_ascii=False)}\n" for key, value in fields.items()
            )
            + "\n"
            + self.text
            + "\n"
        )

    @classmethod
    def decode(cls, text: str) -> Summary:
        header, _, body = text.partition("\n\n")
        values = {}
        for line in header.splitlines()[3:]:
            key, _, value = line.partition(": ")
            values[key] = json.loads(value)
        return cls.model_validate({**values, "text": body.strip()})


def enabled(settings: Settings) -> bool:
    return bool(
        (settings.memory_repo or settings.thread_memory_enabled) and settings.memory_auto_edit
    )


def github_reads(settings: Settings) -> bool:
    return enabled(settings) and settings.thread_memory in {"github", "github_only"}


def legacy(settings: Settings) -> bool:
    return settings.thread_memory != "github_only"


def prefix(thread: str) -> str:
    if not re.fullmatch(IDENTIFIER, thread):
        raise ValueError("Invalid thread identifier")
    return f"threads/{thread}/"


def legacy_event_id(message: Mapping[str, object]) -> str:
    value = str(message.get("id") or "")
    if re.fullmatch(IDENTIFIER, value):
        return value
    raw = json.dumps(message.get("id") or dict(message), sort_keys=True).encode()
    return "legacy-" + hashlib.sha256(raw).hexdigest()


def asset(path: str) -> bool:
    return (
        path.startswith("threads/")
        and ("/scripts/" in path or "/outputs/" in path)
        and not path.endswith("/MANIFEST.json")
    )


def log_entries(files: Mapping[str, str], thread: str) -> list[Entry]:
    rows: dict[str, Entry] = {}
    for path, text in files.items():
        if not path.startswith(prefix(thread)) or not LOG.fullmatch(path):
            continue
        if not text.startswith(HEADER + "\n"):
            raise ValueError("Invalid thread log header")
        for line in text.partition("\n\n")[2].splitlines():
            if line.strip():
                row = Entry.model_validate_json(line)
                if row.event_id in rows and rows[row.event_id] != row:
                    raise ValueError("Conflicting thread event")
                rows[row.event_id] = row
    return sorted(rows.values(), key=lambda e: (e.seq, e.event_id))


def coverage(files: Mapping[str, str], thread: str) -> list[str]:
    """The writer receives these complete entries plus the prior summary."""
    result = {row.event_id for row in log_entries(files, thread)}
    text = files.get(prefix(thread) + "summary.md")
    if text:
        result.update(Summary.decode(text).covered)
    return sorted(result)


def context(files: Mapping[str, str], thread: str, limit: int = 16000) -> str:
    root = prefix(thread)
    text = files.get(root + "summary.md", "")
    summary = Summary.decode(text) if text else None
    covered = set(summary.covered) if summary else set()
    result = (
        f"File: {root}summary.md\n{summary.text}\n" if summary else "No saved thread summary.\n"
    )
    omitted = False
    for row in log_entries(files, thread):
        if row.event_id in covered:
            continue
        line = row.line()
        if len(result) + len(line) > limit - 150:
            omitted = True
            continue
        result += line
    if omitted:
        result += f"\nMore uncovered entries: read {root}log*.md; this view is incomplete.\n"
    return result[:limit]


def recall(
    files: Mapping[str, str], thread: str, query: str = "", event_id: str = "", offset: int = 0
) -> dict[str, Any]:
    """Search the active GitHub history without restoring retired raw transcripts."""
    rows = log_entries(files, thread)
    if event_id:
        row = next((row for row in rows if row.event_id == event_id), None)
        if row is None:
            raise FileNotFoundError("That event is unavailable in this thread.")
        text = row.line()
        return {
            "event_id": event_id, "text": text[offset : offset + 20000],
            "next_offset": offset + 20000 if len(text) > offset + 20000 else None,
        }
    words = set(re.findall(r"\w+", query.casefold()))
    ranked = sorted(
        ((sum(word in row.line().casefold() for word in words), row) for row in rows),
        key=lambda item: (item[0], item[1].seq), reverse=True,
    )
    matches = [row for score, row in ranked if score or not words]
    page = matches[offset : offset + 4]
    return {
        "source": "project_thread_memory",
        "matches": [row.model_dump() for row in page],
        "next_offset": offset + len(page) if offset + len(page) < len(matches) else None,
    }


def pages(rows: list[Entry], thread: str, settings: Settings) -> dict[str, str]:
    chunks: list[str] = []
    current = HEADER + "\n"
    for row in sorted(rows, key=lambda e: (e.seq, e.event_id)):
        line = row.line()
        if len((HEADER + "\n" + line).encode()) > settings.thread_log_kb * 1024:
            raise ValueError("Thread event exceeds the log page limit")
        if len((current + line).encode()) > settings.thread_log_kb * 1024:
            chunks.append(current)
            current = HEADER + "\n"
        current += line
    chunks.append(current)
    if len(chunks) > 9999:
        raise ValueError("Thread log directory is full")
    return {
        prefix(thread) + ("log.md" if i == len(chunks) - 1 else f"log-{i + 1:04d}.md"): text
        for i, text in enumerate(chunks)
    }


def apply(files: Mapping[str, str], update: Update, settings: Settings) -> dict[str, str | None]:
    """Rebase on current head; immutable event ids make retries independent of base head."""
    root = prefix(update.thread)
    rows = log_entries(files, update.thread)
    existing = next((r for r in rows if r.event_id == update.entry.event_id), None)
    if existing:
        if existing.digest() != update.entry.digest():
            raise ValueError("Thread event id already contains different content")
        if not update.summary_only:
            return {}
    elif update.summary_only:
        raise ValueError("Compaction event disappeared")
    changes: dict[str, str | None] = (
        {} if update.summary_only else dict(pages([*rows, update.entry], update.thread, settings))
    )
    for path in files:
        if (
            not update.summary_only
            and path.startswith(root)
            and LOG.fullmatch(path)
            and path not in changes
        ):
            changes[path] = None
    previous = Summary.decode(files[root + "summary.md"]) if root + "summary.md" in files else None
    covered = (set(update.covered) if update.prose.summary else set()) | {update.entry.event_id}
    if (update.prose.summary or previous is None) and (
        previous is None
        or (
            (update.entry.seq > previous.turn or update.summary_only)
            and set(previous.covered) <= covered
        )
    ):
        summary = Summary(
            thread=update.thread,
            surface=update.surface,
            turn=update.entry.seq,
            date=update.entry.date,
            people=sorted(set(previous.people if previous else []) | {update.entry.speaker}),
            covered=sorted(covered),
            progress=update.prose.progress or update.entry.paraphrase,
            tags=update.prose.tags,
            related=update.prose.related,
            text=update.prose.summary or update.entry.paraphrase,
        )
        changes[root + "summary.md"] = summary.encode()
    manifest_path = root + "scripts/MANIFEST.json"
    manifest = json.loads(files.get(manifest_path, '{"turn": -1, "files": {}}'))
    if update.scripts is not None and update.entry.seq > manifest["turn"]:
        for path, text in update.scripts.items():
            target = root + "scripts/" + path
            if not PATH.fullmatch(target) or path == "MANIFEST.json":
                raise ValueError("Invalid thread script path")
            changes[target] = text
        # Rejected scripts keep the last accepted copy. Deletion is intentionally explicit
        # only through a later reviewed retention operation, never inferred from a partial scan.
        hashes = dict(manifest["files"])
        hashes.update(
            {p: hashlib.sha256(t.encode()).hexdigest() for p, t in update.scripts.items()}
        )
        changes[manifest_path] = json.dumps(
            {"turn": update.entry.seq, "files": hashes}, sort_keys=True
        )
    return {p: t for p, t in changes.items() if files.get(p) != t}


def overview(files: Mapping[str, str], limit: int) -> str:
    items = []
    for path, text in files.items():
        if path.startswith("threads/") and path.endswith("/summary.md"):
            summary = Summary.decode(text)
            rows = log_entries(files, summary.thread)
            latest = rows[-1] if rows else None
            date = latest.date if latest else summary.date
            progress = (
                latest.paraphrase
                if latest and latest.event_id not in summary.covered
                else summary.progress
            )
            items.append((date, path, " ".join(progress.split())[:180]))
    result = "# Project threads\n\n"
    for date, path, progress in sorted(items, reverse=True):
        line = f"- {date[:10]} [{path.split('/')[1]}]({path}): {progress}\n"
        if len(result + line) > limit - 80:
            result += "\nMore threads are available under threads/.\n"
            break
        result += line
    return result


def writer_view(files: Mapping[str, str], thread: str, limit: int = 24000) -> dict[str, object]:
    text = files.get(prefix(thread) + "summary.md", "")
    summary = Summary.decode(text) if text else None
    view: dict[str, object] = {"summary": summary.text if summary else "", "entries": []}
    covered = set(summary.covered) if summary else set()
    entries: list[dict[str, object]] = []
    for row in reversed(log_entries(files, thread)):
        if row.event_id in covered:
            continue
        candidate = [row.model_dump(), *entries]
        if len(json.dumps({**view, "entries": candidate})) > limit:
            continue
        entries = candidate
        covered.add(row.event_id)
    return {**view, "entries": entries, "covered": sorted(covered)}


def make_update(
    thread: str,
    event_id: str,
    seq: int,
    date: str,
    speaker: str,
    surface: str,
    success: bool,
    prose: Prose,
    covered: list[str],
    *,
    scripts: dict[str, str] | None = None,
    rejected: dict[str, str] | None = None,
    outputs: list[str] | None = None,
) -> Update:
    return Update.model_validate(
        {
            "thread": thread,
            "surface": surface,
            "entry": Entry(
                event_id=event_id,
                seq=seq,
                date=date,
                speaker=speaker,
                success=success,
                paraphrase=prose.paraphrase,
                methods=prose.methods if success else "",
                scripts=sorted(scripts or {}),
                outputs=outputs or [],
                rejected=rejected or {},
            ),
            "prose": prose,
            "covered": covered,
            "scripts": scripts if success else None,
        }
    )
