"""Content-addressed task files and immutable operation evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from mica.config import Settings
from mica.models import SourceFile

MAX_SOURCE_BYTES = 240_000


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or str(path) != value
        or len(value) > 200
    ):
        raise ValueError("path must be a normalized relative POSIX path")
    return value


def validate_sources(files: list[SourceFile]) -> dict[str, str]:
    contents: dict[str, str] = {}
    for item in files:
        path = safe_relative(item.path)
        if path in contents:
            raise ValueError(f"duplicate source file: {path}")
        if not path.endswith((".py", ".md")) or path.startswith(("mica/", "pistachio/")):
            raise ValueError("workspace supports Python source/tests and Markdown design files")
        root_module = path.split("/")[0].removesuffix(".py")
        if root_module in sys.stdlib_module_names | {
            "numpy",
            "pandas",
            "scipy",
            "sklearn",
            "scanpy",
            "anndata",
            "task_checkpoints",
            "task_runtime",
        }:
            raise ValueError("source cannot shadow a runtime or trusted module")
        if path.split("/")[0] in {"config", "inputs", "outputs", "diagnostics"}:
            raise ValueError("protected workspace path")
        contents[path] = item.content
    if len(contents) > 32 or sum(len(x.encode()) for x in contents.values()) > MAX_SOURCE_BYTES:
        raise ValueError("source revision exceeds workspace budget")
    # A file cannot also be a parent directory.
    if any(str(parent) in contents for p in contents for parent in PurePosixPath(p).parents):
        raise ValueError("source file/directory collision")
    return contents


class WorkspaceStore:
    """Refs are task-local keys, never arbitrary filesystem paths or URLs."""

    def __init__(self, settings: Settings, task_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("invalid task id")
        self.settings = settings
        self.prefix = f"tasks/{task_id}/development/"

    async def put(self, value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = self.prefix + hashlib.sha256(payload.encode()).hexdigest() + ".json"
        await asyncio.to_thread(self._write, key, payload)
        return key

    def _write(self, key: str, payload: str) -> None:
        if self.settings.gcs_bucket:
            from google.api_core.exceptions import PreconditionFailed
            from google.cloud.storage import Client

            blob = (
                Client(project=self.settings.gcp_project or None)
                .bucket(self.settings.gcs_bucket)
                .blob(key)
            )
            try:
                blob.upload_from_string(
                    payload, content_type="application/json", if_generation_match=0
                )
            except PreconditionFailed:
                if blob.download_as_text() != payload:
                    raise ValueError("immutable workspace object differs") from None
        else:
            target = self.settings.artifact_root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=target.parent, delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if target.read_text() != payload:
                        raise ValueError("immutable workspace object differs") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    async def get(self, key: str) -> Any:
        if not re.fullmatch(re.escape(self.prefix) + r"[a-f0-9]{64}\.json", key):
            raise ValueError("workspace reference is outside this task")
        payload = await asyncio.to_thread(self._read, key)
        if hashlib.sha256(payload.encode()).hexdigest() != Path(key).stem:
            raise ValueError("workspace checksum mismatch")
        return json.loads(payload)

    def _read(self, key: str) -> str:
        if self.settings.gcs_bucket:
            from google.cloud.storage import Client

            return str(
                Client(project=self.settings.gcp_project or None)
                .bucket(self.settings.gcs_bucket)
                .blob(key)
                .download_as_text()
            )
        return (self.settings.artifact_root / key).read_text()
