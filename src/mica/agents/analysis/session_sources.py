"""Project-scoped durable session sources and bounded worker materialization."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import attachments, session_render
from .config import Settings
from .storage import Conflict, Storage


class Source(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,99}$")
    uploader: str = Field(max_length=320)
    filename: str = Field(max_length=200)
    date: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0, le=100_000_000)
    highlights: dict[str, str] = Field(default_factory=dict, max_length=10)
    format: str = session_render.FORMAT

    @property
    def prefix(self) -> str:
        return f"projects/{self.project}/sessions/{self.id}/"

    def file(self) -> attachments.ThreadFile:
        return attachments.ThreadFile(
            id=self.id,
            source="user",
            name=self.filename,
            path=f"inputs/sessions/{self.id}/session.jsonl",
            kind="text",
            media_type="application/json",
            size=self.size,
            sha256=self.sha256,
            key=self.prefix + "session.jsonl",
            event_id=self.id,
            user=self.uploader,
        )


def catalog(storage: Storage, project: str) -> list[Source]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,99}", project):
        raise ValueError("Invalid session project")
    prefix = f"projects/{project}/sessions/"
    sources = []
    for key in storage.keys(prefix):
        if not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}/meta\.json", key):
            continue
        source = Source.model_validate_json(storage.read_prefix(key, 16001))
        if source.project != project or key != source.prefix + "meta.json":
            raise ValueError("Session metadata escaped its project")
        sources.append(source)
    return sorted(sources, key=lambda s: (s.date, s.id), reverse=True)


def select(sources: list[Source], settings: Settings, required: str = "") -> list[Source]:
    remaining = settings.session_project_mb * 1_000_000
    selected = []
    for source in sorted(sources, key=lambda s: s.id != required):
        if source.size <= remaining:
            selected.append(source)
            remaining -= source.size
    if required and required not in {s.id for s in selected}:
        raise ValueError("The imported session is unavailable or exceeds the worker session cap")
    return selected


def persist(
    storage: Storage,
    file: attachments.ThreadFile,
    identifier: str,
    project: str,
    uploader: str,
    highlights: dict[str, str],
    settings: Settings,
) -> Source:
    if session_render.FORMAT not in settings.session_import_formats.split(","):
        raise ValueError("This session format is not enabled")
    with tempfile.TemporaryDirectory(prefix="mica-intake-") as folder:
        path = Path(folder) / "session.jsonl"
        if not file.key:
            raise ValueError("Session attachment is unavailable")
        storage.download(file.key, path)
        if path.stat().st_size != file.size or attachments.file_hash(path) != file.sha256:
            raise ValueError("Session attachment changed")
        stats: dict[str, int] = {}
        try:
            with path.open(encoding="utf-8") as stream:
                for _ in session_render.records(stream, stats):
                    pass
        except UnicodeError as exc:
            raise ValueError("Use a UTF-8 Claude Code JSONL session") from exc
        if not stats.get("session_records"):
            raise ValueError("The file does not contain Claude Code session records")
        source = Source(
            id=identifier,
            project=project,
            uploader=uploader,
            filename=file.name,
            date=datetime.now(UTC).isoformat(),
            sha256=file.sha256,
            size=file.size,
            highlights=highlights,
        )
        with suppress(Conflict):
            storage.upload_new(source.prefix + "session.jsonl", path)
        digest, size = hashlib.sha256(), 0
        with storage.open_read(source.prefix + "session.jsonl") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if size != source.size or digest.hexdigest() != source.sha256:
            raise ValueError("Durable session integrity check failed")
        # Metadata is the publication point: no worker can discover a partial upload.
        try:
            storage.put_if(source.prefix + "meta.json", source.model_dump_json().encode(), None)
        except Conflict:
            previous = Source.model_validate_json(storage.get(source.prefix + "meta.json")[0])
            if previous.sha256 != source.sha256 or previous.project != project:
                raise ValueError("Conflicting session metadata") from None
            return previous
        return source


def materialize(storage: Storage, work: Path, project: str, raw: list[dict[str, Any]]) -> None:
    root = work / "inputs/sessions"
    if (work / "inputs").is_symlink():
        raise ValueError("Session input directory is a symlink")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)
    for item in raw:
        source = Source.model_validate(item)
        if source.project != project:
            raise ValueError("Cross-project session source")
        if not attachments.materialize(storage, work, [source.file()]):
            raise ValueError("Session source is unavailable")
        (root / source.id / "meta.json").write_text(source.model_dump_json())
