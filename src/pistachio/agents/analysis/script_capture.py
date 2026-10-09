"""Bounded execution snapshots owned by hooks, never by a workspace manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .user_memory import SECRET

MAX_BYTES = 1_000_000
MAX_SCRIPTS = 10
SUFFIXES = {".py", ".R", ".r", ".sh", ".jl"}
EXCLUDED = {"inputs", ".previews", "outputs", "scratch", ".pistachio"}
CACHE = Path(".pistachio/scripts")


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


def script_token(tokens: list[str]) -> str | None:
    """Recognize invocations, not script-shaped arguments to cat, echo or another script."""
    words = list(tokens)
    if words and words[0] == "env":
        words.pop(0)
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^$`]*", words[0]):
        words.pop(0)
    if not words:
        return None
    executable = Path(words.pop(0))
    if executable.suffix in SUFFIXES:
        return str(executable)
    name = executable.name
    if not (
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name)
        or name in {"Rscript", "R", "bash", "sh", "zsh", "julia"}
    ):
        return None
    while words:
        word = words.pop(0)
        if word == "--":
            return words[0] if words else None
        if not word.startswith("-"):
            return word if name != "R" else None
        if name == "R" and word in {"-f", "--file"}:
            return words[0] if words else None
        if name == "R" and word.startswith("--file="):
            return word.split("=", 1)[1]
        if name.startswith("python"):
            if word in {"-W", "-X"}:
                if not words:
                    return None
                words.pop(0)
            elif not re.fullmatch(r"-[uBbEIOqsS]+|-W.+|-X.+", word):
                return None
        elif name in {"bash", "sh", "zsh"}:
            if not re.fullmatch(r"-[euxv]+|--noprofile|--norc", word):
                return None
        elif name in {"R", "Rscript"}:
            if word not in {
                "--vanilla",
                "--quiet",
                "--slave",
                "--no-save",
                "--no-restore",
            } and not word.startswith("--default-packages="):
                return None
        elif name == "julia" and not re.fullmatch(
            r"--(?:project|threads|startup-file|history-file|color)=.+|-q", word
        ):
            return None
    return None


def candidates(work: Path, command: str) -> tuple[list[str], bool]:
    """Only simple foreground invocations and successful && chains are provable.

    Fallbacks/pipelines/semicolon lists can mask failures or skip branches, so never
    infer execution from their final exit code. Quoted separator characters are data.
    """
    if len(command) > 32_000 or any(c in command for c in ("$", "`")):
        return [], False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()< >\n".replace(" ", ""))
    lexer.whitespace = " \t\r"
    tokens = list(lexer)  # ValueError for malformed quoting: caller safely skips it.
    groups: list[list[str]] = [[]]
    uncertain = False
    for token in tokens:
        if token and all(c in ";&|()<>\n" for c in token):
            uncertain |= token != "&&"
            groups.append([])
        else:
            groups[-1].append(token)
    directory = work
    paths = []
    for words in groups:
        if not words:
            continue
        if words[0] == "cd":
            if len(words) != 2 or words[1].startswith("-"):
                return [], False
            directory = Path(os.path.abspath(directory / words[1]))
            continue
        if any(Path(t).name == "measure.py" for t in words):
            continue
        candidate = script_token(words)
        if not candidate or Path(candidate).suffix not in SUFFIXES:
            continue
        path = Path(os.path.abspath(directory / candidate))
        try:
            relative = relative_path(path.relative_to(work).as_posix())
        except ValueError:
            continue
        if not any(p.is_symlink() for p in [path, *path.parents] if p != work):
            paths.append(relative.as_posix())
    return list(dict.fromkeys(paths)), uncertain


def clear(work: Path) -> None:
    parent = work / CACHE.parent
    if parent.is_symlink() or parent.is_file():
        parent.unlink()
        return
    root = work / CACHE
    if root.is_symlink() or root.is_file():
        root.unlink()
    elif root.is_dir():
        shutil.rmtree(root)


@dataclass(frozen=True)
class Script:
    path: str
    content: bytes
    last_run: float

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
        self.pending: dict[str, tuple[dict[str, bytes], dict[str, str], bool]] = {}
        # Only the caller's checkpoint metadata is authoritative. Ignore the workspace manifest.
        if trusted_manifest:
            rows = trusted_manifest.get("scripts", [])
            if not isinstance(rows, list) or len(rows) > MAX_SCRIPTS:
                raise ValueError("Invalid script checkpoint")
            for row in rows:
                path = relative_path(row["path"])
                try:
                    content = read_regular(self.work, CACHE / path)
                    checked_content(content)
                    item = Script(str(path), content, float(row["last_run"]))
                    if item.metadata() != row:
                        raise ValueError("checkpoint integrity check failed")
                    self.scripts[str(path)] = item
                except (ValueError, OSError):
                    self.skip(str(path), "checkpoint copy could not be verified")
            for path, reason in list(trusted_manifest.get("skipped", {}).items())[:100]:
                self.skip(str(relative_path(path)), str(reason)[:100])
        clear(self.work)

    def skip(self, path: str, reason: str) -> None:
        self.scripts.pop(
            path, None
        )  # Never deliver an older successful version after an unsafe rerun.
        if len(self.skipped) < 100 or path in self.skipped:
            self.skipped[path] = reason

    def before(self, tool_id: str, args: dict[str, Any]) -> None:
        with self._lock:
            self._before(tool_id, args)

    def _before(self, tool_id: str, args: dict[str, Any]) -> None:
        data: dict[str, bytes] = {}
        skipped: dict[str, str] = {}
        try:
            paths, uncertain = candidates(self.work, str(args.get("command", "")))
        except ValueError:
            return
        for path in paths[:100]:
            if uncertain or args.get("run_in_background"):
                skipped[path] = "compound or background execution could not be verified"
                continue
            try:
                content = read_regular(self.work, Path(path))
                checked_content(content)
                if SECRET.search(path):
                    raise ValueError("possible credential")
                pending_bytes = sum(
                    len(value) for rows, _, _ in self.pending.values() for value in rows.values()
                )
                if (
                    pending_bytes + sum(map(len, data.values())) + len(content)
                    > MAX_BYTES * MAX_SCRIPTS
                ):
                    skipped[path] = "capture memory limit"
                elif len(data) < MAX_SCRIPTS:
                    data[path] = content
                else:
                    skipped[path] = "10-script limit"
            except (ValueError, OSError) as exc:
                skipped[path] = (
                    str(exc) if isinstance(exc, ValueError) else "file could not be read"
                )
        self.pending[tool_id] = data, skipped, "&&" in str(args.get("command", ""))

    def after(self, tool_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._after(tool_id, event)

    def _after(self, tool_id: str, event: dict[str, Any]) -> None:
        data, skipped, compound = self.pending.pop(tool_id, ({}, {}, False))
        response = event.get("tool_response", {})
        if event.get("hook_event_name") != "PostToolUse" or not isinstance(response, dict):
            return
        if (
            response.get("is_error")
            or response.get("isError")
            or response.get("interrupted")
            or response.get("backgroundTaskId")
            or response.get("exitCode", 0) != 0
            or response.get("exit_code", 0) != 0
        ):
            return
        for path, content in data.items():
            if compound:
                try:
                    unchanged = read_regular(self.work, Path(path)) == content
                except (ValueError, OSError):
                    unchanged = False
                if not unchanged:
                    self.skip(path, "script changed within a compound command")
                    continue
            if path not in self.scripts and len(self.scripts) >= MAX_SCRIPTS:
                self.skip(path, "10-script limit")
                continue
            # Reinsert to record last successful run order, including identical reruns.
            self.scripts.pop(path, None)
            self.scripts[path] = Script(path, content, time.time())
            self.skipped.pop(path, None)
        for path, reason in skipped.items():
            self.skip(path, reason)

    def checkpoint(self) -> dict[str, Any]:
        with self._lock:
            return self._checkpoint()

    def _checkpoint(self) -> dict[str, Any]:
        """Called by trusted code at checkpoint time; model changes cannot forge metadata."""
        clear(self.work)
        root = self.work / CACHE
        root.mkdir(parents=True, exist_ok=True)
        for script in self.scripts.values():
            path = root / script.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(script.content)
        manifest = {
            "version": 1,
            "scripts": [s.metadata() for s in self.scripts.values()],
            "skipped": dict(self.skipped),
        }
        (root / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def uploads(self, turn: int) -> list[dict[str, str]]:
        scripts = list(self.scripts.values())
        names = Counter(Path(s.path).name for s in scripts)
        chosen: set[str] = set()
        uploads = []
        root = self.work / "outputs/returned-scripts" / f"turn-{turn}"
        for ancestor in [root, *root.parents]:
            if ancestor == self.work:
                break
            if ancestor.is_symlink():
                raise ValueError("Script delivery path contains a symlink")
        for script in scripts:
            path = Path(script.path)
            name = path.name if names[path.name] == 1 else path.parent.name + "-" + path.name
            if name in chosen:
                name = (
                    path.stem
                    + "-"
                    + hashlib.sha256(script.path.encode()).hexdigest()[:12]
                    + path.suffix
                )
            base = name
            sequence = 2
            while name in chosen:
                name = Path(base).stem + f"-{sequence}" + Path(base).suffix
                sequence += 1
            chosen.add(name)
            target = root / name
            root.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.write_bytes(script.content)
            uploads.append({"file": str(target), "filename": name})
        return uploads

    def notes(self) -> list[str]:
        return [
            f"I didn't attach `{'script' if SECRET.search(path) else Path(path).name}` ({reason})."
            for path, reason in self.skipped.items()
        ]
