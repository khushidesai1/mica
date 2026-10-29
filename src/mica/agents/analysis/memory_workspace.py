"""Offline worker memory snapshots and proposals; never connects to GitHub."""

from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import memory_archive
from .config import CHECKPOINT_PREFIX, Settings
from .project_memory import Snapshot, file_limit, normalize, valid_path, validate
from .storage import Storage


class Proposal(BaseModel):
    base_head: str
    changes: dict[str, str | None] = Field(default_factory=dict)
    rejected: dict[str, str] = Field(default_factory=dict)


def materialize(work: Path, base: Snapshot, settings: Settings) -> None:
    root = work / "knowledge"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for path, version in base.files.items():
        validate(path, version.text.encode(), settings)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(version.text)


def scan(
    work: Path, settings: Settings, attribution: str = ""
) -> tuple[dict[str, bytes], dict[str, str]]:
    root = work / "knowledge"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("Knowledge root must be an ordinary directory.")
    found: dict[str, bytes] = {}
    rejected: dict[str, str] = {}
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted([*dirs, *files]):
            count += 1
            if count > 4 * settings.memory_max_files + 20:
                raise ValueError("Too many workspace knowledge entries.")
            target = Path(directory) / name
            path = target.relative_to(root).as_posix()
            if target.is_symlink():
                rejected[path] = "Symlinks are not allowed."
                continue
            if name in dirs:
                continue
            try:
                valid_path(path)
                # O_NOFOLLOW plus fstat prevents links/FIFOs/devices from being read.
                fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("Only ordinary files are allowed.")
                    data = stream.read(file_limit(path, settings) + 1)
                    after = os.fstat(stream.fileno())
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise ValueError("File changed during save.")
                data = normalize(
                    path,
                    data.decode("utf-8"),
                    attribution or f"worker thread {work.name}; {datetime.now(UTC).date()}",
                ).encode()
                validate(path, data, settings)
                found[path] = data
            except (ValueError, OSError) as exc:
                rejected[path] = type(exc).__name__ + ": invalid path, content, or size"
    return found, rejected


def is_read(work: Path, tool: str, args: dict[str, Any]) -> bool:
    if tool not in {"Read", "Grep", "Glob"}:
        return False
    raw = args.get("file_path") if tool == "Read" else args.get("path")
    if not raw and tool == "Glob":
        raw = args.get("pattern", "")
    if not isinstance(raw, str) or not raw:
        return False
    path = Path(raw)
    if not path.is_absolute():
        path = work / path
    return path.resolve().is_relative_to((work / "knowledge").resolve())


def proposal(work: Path, base: Snapshot, settings: Settings, attribution: str = "") -> Proposal:
    files, rejected = scan(work, settings, attribution)
    changes: dict[str, str | None] = {}
    for path in files.keys() | base.files.keys():
        if memory_archive.is_index(path):
            continue
        if any(path == bad or path.startswith(bad + "/") for bad in rejected):
            continue
        value = files[path].decode() if path in files else None
        before = base.files.get(path)
        if value != (before.text if before else None):
            if path.startswith("archive/"):
                rejected[path] = "Archive entries are immutable; edit an active entry."
            else:
                changes[path] = value
    return Proposal(base_head=base.head_oid, changes=changes, rejected=rejected)


def pending(work: Path, base: Snapshot, settings: Settings, attribution: str = "") -> bytes:
    return proposal(work, base, settings, attribution).model_dump_json().encode()


def publication_proposal(
    data: bytes, base: Snapshot, event_id: str, receipts: str, settings: Settings
) -> bytes:
    """Retain controller-confirmed destinations even when final auto-publish follows the model."""
    if not receipts:
        return data
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid publication event")
    result = decode_pending(data, settings)
    path = f"results/published-{event_id}.md"
    text = (
        f"type: result\nsource: verified publication receipts, event {event_id}\n"
        "status: current\n\n" + receipts + "\nOnly confirmed files are counted. "
        "This is publication evidence, not scientific validation.\n"
    )
    try:
        validate(path, text.encode(), settings)
    except ValueError:
        result.rejected[path] = "Publication receipt exceeds memory limits; retained in history."
    else:
        result.changes[path] = text
    return result.model_dump_json().encode()


def pending_limit(settings: Settings) -> int:
    return 6 * settings.memory_total_kb * 1024 + settings.memory_max_files * 1024


def decode_pending(data: bytes, settings: Settings) -> Proposal:
    if len(data) > pending_limit(settings):
        raise ValueError("Oversized memory proposal")
    result = Proposal.model_validate_json(data)
    if len(result.changes) > settings.memory_max_files:
        raise ValueError("Too many memory proposals")
    for path, text in result.changes.items():
        valid_path(path)
        if text is not None:
            validate(path, text.encode(), settings)
    return result


def restore_pending(data: bytes, work: Path, base: Snapshot, settings: Settings) -> Snapshot:
    edits = decode_pending(data, settings)
    if edits.base_head != base.head_oid:
        raise ValueError("Memory checkpoint does not match the original snapshot")
    for path, text in edits.changes.items():
        target = work / "knowledge" / path
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    return base


def cancelled(storage: Storage, event_id: str) -> bool:
    try:
        storage.get(f"{CHECKPOINT_PREFIX}/events/{event_id}/memory-cancelled.json")
        return True
    except FileNotFoundError:
        return False
