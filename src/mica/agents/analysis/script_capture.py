"""Bounded code snapshots owned by hooks, never by a workspace manifest."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project_memory import SECRET

MAX_BYTES = 1_000_000
MAX_SCRIPTS = 10
SUFFIXES = {".py", ".R", ".r", ".sh", ".jl"}
EXCLUDED = {"inputs", ".previews", "outputs", "scratch", ".mica", ".pistachio"}
CACHE = Path(".mica/scripts")
LEGACY_CACHE = Path(".pistachio/scripts")


def relative_path(value: str) -> Path:
    path = Path(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] in EXCLUDED
        or path.suffix not in SUFFIXES
        or len(value) > 240
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid script path")
    return path


def read_regular(root: Path, path: Path) -> bytes:
    """Open every component without following links; do not block on FIFOs."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("not an ordinary file")
            if info.st_size > MAX_BYTES:
                raise ValueError("larger than 1 MB")
            with os.fdopen(os.dup(source), "rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("larger than 1 MB")
            return data
        finally:
            os.close(source)
    finally:
        os.close(fd)


def checked_content(data: bytes) -> None:
    if len(data) > MAX_BYTES:
        raise ValueError("larger than 1 MB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not UTF-8 text") from None
    if SECRET.search(text):
        raise ValueError("possible credential")


def clear(work: Path) -> None:
    for cache in (CACHE, LEGACY_CACHE):
        parent = work / cache.parent
        if parent.is_symlink() or parent.is_file():
            parent.unlink()
            continue
        root = work / cache
        if root.is_symlink() or root.is_file():
            root.unlink()
        elif root.is_dir():
            shutil.rmtree(root)


@dataclass(frozen=True)
class Script:
    path: str
    content: bytes
    last_run: float  # Persisted legacy name; now the snapshot time, not proof of execution.

    def metadata(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": hashlib.sha256(self.content).hexdigest(),
            "size": len(self.content),
            "last_run": self.last_run,
        }


class Capture:
    def __init__(self, work: Path, trusted_manifest: dict[str, Any] | None = None):
        self._lock = threading.RLock()
        self.work = work.resolve()
        self.scripts: dict[str, Script] = {}
        self.skipped: dict[str, str] = {}
        self.observed = {
            path: self.fingerprint(data, error) for path, data, error in self.inventory()
        }
        if trusted_manifest:
            rows = trusted_manifest.get("scripts", [])
            if not isinstance(rows, list) or len(rows) > MAX_SCRIPTS:
                raise ValueError("Invalid script checkpoint")
            for row in rows:
                path = relative_path(row["path"])
                try:
                    try:
                        content = read_regular(self.work, CACHE / path)
                    except FileNotFoundError:
                        content = read_regular(self.work, LEGACY_CACHE / path)
                    checked_content(content)
                    item = Script(str(path), content, float(row["last_run"]))
                    if item.metadata() != row:
                        raise ValueError("checkpoint integrity check failed")
                    self.scripts[str(path)] = item
                    self.observed[str(path)] = hashlib.sha256(content).hexdigest()
                except (ValueError, OSError):
                    self.skip(str(path), "checkpoint copy could not be verified")
            observed = trusted_manifest.get("observed", {})
            if not isinstance(observed, dict) or len(observed) > 101:
                raise ValueError("Invalid code snapshot index")
            for path, value in observed.items():
                self.valid_observed(path)
                if not isinstance(value, str) or len(value) > 120:
                    raise ValueError("Invalid code snapshot fingerprint")
                self.observed[path] = value
            for path, reason in list(trusted_manifest.get("skipped", {}).items())[:100]:
                self.valid_observed(path)
                self.skip(path, str(reason)[:100])
        clear(self.work)

    @staticmethod
    def valid_observed(value: str) -> None:
        path = Path(value)
        if not path.parts or path.is_absolute() or ".." in path.parts or len(value) > 240:
            raise ValueError("Invalid code snapshot path")

    @staticmethod
    def fingerprint(data: bytes | None, error: str) -> str:
        return "error:" + error if data is None else hashlib.sha256(data).hexdigest()

    def inventory(self) -> Iterator[tuple[str, bytes | None, str]]:
        root = self.work / "code"
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            yield "code", None, "code/ must be an ordinary directory"
            return
        count = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(dirs + files):
                count += 1
                if count > 100:
                    yield "code", None, "code snapshot scan limit (100 entries)"
                    return
                path = Path(directory) / name
                if path.suffix not in SUFFIXES:
                    continue
                relative = path.relative_to(self.work).as_posix()
                try:
                    relative_path(relative)
                    content = read_regular(self.work, Path(relative))
                    checked_content(content)
                    if SECRET.search(relative):
                        raise ValueError("possible credential")
                    yield relative, content, ""
                except (ValueError, OSError) as exc:
                    yield (
                        relative,
                        None,
                        str(exc) if isinstance(exc, ValueError) else "file could not be read",
                    )

    def skip(self, path: str, reason: str) -> None:
        self.scripts.pop(path, None)
        if len(self.skipped) < 100 or path in self.skipped:
            self.skipped[path] = reason

    def snapshot(self) -> None:
        """Capture changed code after a tool; shell syntax and exit status prove no execution."""
        with self._lock:
            observed = {}
            for path, content, error in self.inventory():
                fingerprint = self.fingerprint(content, error)
                observed[path] = fingerprint
                if fingerprint == self.observed.get(path):
                    continue
                if content is None:
                    self.skip(path, error)
                elif path not in self.scripts and len(self.scripts) >= MAX_SCRIPTS:
                    self.skip(path, "10-script limit")
                else:
                    self.scripts[path] = Script(path, content, time.time())
                    self.skipped.pop(path, None)
            self.observed = observed

    def checkpoint(self) -> dict[str, Any]:
        with self._lock:
            clear(self.work)
            root = self.work / CACHE
            root.mkdir(parents=True, exist_ok=True)
            for script in self.scripts.values():
                path = root / script.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(script.content)
            manifest = {
                "version": 2,
                "scripts": [s.metadata() for s in self.scripts.values()],
                "skipped": dict(self.skipped),
                "observed": dict(self.observed),
            }
            (root / "manifest.json").write_text(json.dumps(manifest))
            return manifest

    def uploads(self, turn: int, provided: set[str] | None = None) -> list[dict[str, str]]:
        scripts = [
            s
            for s in self.scripts.values()
            if hashlib.sha256(s.content).hexdigest() not in (provided or set())
        ]
        if not scripts:
            return []
        relative = Path("outputs/returned-scripts") / f"turn-{turn}"
        fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        name = f"scripts-{uuid.uuid4().hex[:12]}.zip"
        try:
            for part in relative.parts:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            out = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            try:
                with (
                    os.fdopen(out, "wb") as stream,
                    zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive,
                ):
                    for script in sorted(scripts, key=lambda s: s.path):
                        archive.writestr(script.path, script.content)
            except BaseException:
                os.unlink(name, dir_fd=fd)
                raise
        finally:
            os.close(fd)
        return [{"file": str(self.work / relative / name), "filename": name}]

    def notes(self, provided: set[str] | None = None) -> list[str]:
        return [
            f"I didn't attach `{'script' if SECRET.search(path) else Path(path).name}` ({reason})."
            for path, reason in self.skipped.items()
            if path not in (provided or set())
        ]
