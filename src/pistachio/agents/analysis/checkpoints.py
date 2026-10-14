"""Immutable completed stages and consistent small workspace snapshots."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from google.api_core.exceptions import NotFound

from .outputs import files_under
from .recovery import event_prefix
from .storage import Storage


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024**2):
            h.update(block)
    return h.hexdigest()


def workspace_snapshot(
    archive: Path,
    home: Path,
    *,
    partial: bool,
    max_bytes: int | None = None,
    published: set[Path] | None = None,
) -> None:
    total = 0
    with tempfile.TemporaryDirectory(dir=archive.parent, prefix="snapshot-") as temporary:
        backup = Path(temporary) / "state.db"
        if (home / "state.db").is_file():
            with sqlite3.connect(home / "state.db") as source, sqlite3.connect(backup) as target:
                source.backup(target)
        paths = [*files_under(home / "work"), *files_under(home / ".claude/projects")]
        if backup.exists():
            paths.append(backup)
        with tarfile.open(archive, "w:gz") as tar:
            for path in paths:
                relative = Path("state.db") if path == backup else path.relative_to(home)
                if path in (published or set()) or "knowledge" in relative.parts:
                    continue
                if any(
                    p in {"inputs", ".previews", "scratch", "checkpoints", ".tmp", ".mpl"}
                    for p in relative.parts
                ):
                    continue
                if partial and "outputs" in relative.parts:
                    continue
                before = path.stat()
                total += before.st_size
                if max_bytes is not None and total > max_bytes:
                    raise ValueError("Workspace checkpoint exceeds its byte allowance.")
                if relative.parts[0] == ".claude" and path.suffix == ".jsonl":
                    data = path.read_bytes()
                    data = data[: data.rfind(b"\n") + 1]
                    info = tar.gettarinfo(str(path), arcname=str(relative))
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    tar.add(path, arcname=str(relative), recursive=False)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError(
                            "Workspace changed during checkpoint; retaining prior snapshot."
                        )


class Stages:
    def __init__(self, storage: Storage, work: Path, event_id: str, attempt: int) -> None:
        self.storage, self.work = storage, work.resolve()
        self.prefix = event_prefix(event_id) + "/stages/"
        self.attempt = attempt
        self.uploaded_bytes = 0
        self.stages: list[dict[str, Any]] = []

    def manifest(self) -> dict[str, Any]:
        return {"stages": self.stages, "uploaded_bytes": self.uploaded_bytes}

    def capture(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"path", "fingerprint"}:
            raise ValueError("A checkpoint path and input/method fingerprint are required.")
        path, fingerprint = args["path"], args["fingerprint"]
        if not isinstance(path, str) or not re.fullmatch(r"checkpoints/[a-zA-Z0-9_-]{1,80}", path):
            raise ValueError("Use a completed stage directory directly under checkpoints/.")
        if not isinstance(fingerprint, str) or not 1 <= len(fingerprint) <= 1000:
            raise ValueError(
                "Record input versions, method, channels and parameters in the fingerprint."
            )
        root = self.work / path
        if root.is_symlink() or not root.resolve().is_relative_to(self.work / "checkpoints"):
            raise ValueError("Checkpoint must remain inside the workspace.")
        files = files_under(root)
        if not files:
            raise ValueError("Checkpoint is empty.")
        sizes = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in files}
        total = sum(size for size, _ in sizes.values())
        with tempfile.TemporaryDirectory(prefix="pista-stage-") as temporary:
            archive = Path(temporary) / "stage.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for file in files:
                    tar.add(file, arcname=str(file.relative_to(self.work)), recursive=False)
            if any((p.stat().st_size, p.stat().st_mtime_ns) != stat for p, stat in sizes.items()):
                raise ValueError("Stage is still changing. Close all writers before saving it.")
            key = self.prefix + f"a{self.attempt}-{uuid4().hex}.tar.gz"
            sha = digest(archive)
            self.storage.upload_new(key, archive)
        stage = {
            "path": path,
            "fingerprint": fingerprint,
            "bytes": total,
            "archive": key,
            "sha256": sha,
        }
        self.stages = [s for s in self.stages if s["path"] != path] + [stage]
        self.uploaded_bytes += total
        return {"status": "saved", "path": path, "bytes": total, "fingerprint": fingerprint}

    def restore(self, manifest: dict[str, Any]) -> None:
        self._restore(manifest, skip_invalid=False)

    def restore_available(self, manifest: dict[str, Any]) -> list[str]:
        """Keep verified stages and name unavailable stages for automatic recomputation."""
        return self._restore(manifest, skip_invalid=True)

    def _restore(self, manifest: dict[str, Any], *, skip_invalid: bool) -> list[str]:
        stages = manifest.get("stages", [])
        uploaded = manifest.get("uploaded_bytes", 0)
        if not isinstance(stages, list) or not isinstance(uploaded, int) or uploaded < 0:
            raise ValueError("Invalid stage checkpoint manifest.")
        total = 0
        verified, missing = [], []
        for stage in stages:
            if not isinstance(stage, dict):
                raise ValueError("Invalid stage checkpoint entry.")
            try:
                total += self._restore_stage(stage)
                verified.append(stage)
            except (ValueError, OSError, tarfile.TarError, NotFound):
                if not skip_invalid:
                    raise
                path = str(stage.get("path", "unknown stage"))
                missing.append(path)
                if re.fullmatch(r"checkpoints/[a-zA-Z0-9_-]{1,80}", path):
                    root = self.work / path
                    if root.is_symlink():
                        root.unlink()
                    elif root.exists():
                        shutil.rmtree(root)
        self.stages, self.uploaded_bytes = verified, uploaded
        return missing

    def _restore_stage(self, stage: dict[str, Any]) -> int:
        total = 0
        key = str(stage.get("archive", ""))
        if not re.fullmatch(re.escape(self.prefix) + r"a\d+-[a-f0-9]{32}\.tar\.gz", key):
            raise ValueError("Stage belongs to a different request.")
        path = str(stage.get("path", ""))
        if not re.fullmatch(r"checkpoints/[a-zA-Z0-9_-]{1,80}", path):
            raise ValueError("Invalid stage restore path.")
        with tempfile.TemporaryDirectory(prefix="pista-restore-") as temporary:
            archive = Path(temporary) / "stage.tar.gz"
            self.storage.download(key, archive)
            if digest(archive) != stage.get("sha256"):
                raise ValueError("Stage checkpoint checksum mismatch.")
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                seen: set[str] = set()
                for member in members:
                    target = self.work / member.name
                    total += member.size
                    if (
                        not member.isfile()
                        or member.name in seen
                        or not member.name.startswith(path + "/")
                        or not target.resolve().is_relative_to(self.work / path)
                    ):
                        raise ValueError("Unsafe stage checkpoint.")
                    seen.add(member.name)
                tar.extractall(self.work, filter="data")
        return total
