"""One attachment policy and complete, bounded folder packages for both surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from claude_agent_sdk import SdkMcpTool, tool

from .outputs import ATTACHMENT_SUFFIXES, MAX_ATTACHMENT_BYTES
from .script_capture import SUFFIXES, checked_content

MAX_FOLDER_ENTRIES = 10_000
SCRIPT_SUFFIXES = {s.lower() for s in SUFFIXES} | {".sql"}


def open_regular(work: Path, relative: Path) -> BinaryIO:
    """Never follow links, including a parent replaced after inventory."""
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Choose a path inside the workspace.")
    fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Folder contains a link or special file; nothing was packaged.")
            return os.fdopen(source, "rb")
        except BaseException:
            os.close(source)
            raise
    finally:
        os.close(fd)


def attachment_issue(path: Path, work: Path) -> str:
    relative = path.relative_to(work)
    if relative.parts[:2] == ("outputs", "gcs") or ".previews" in relative.parts:
        return "bucket-staging-or-preview"
    if path.suffix.lower() not in ATTACHMENT_SUFFIXES:
        return "unsupported attachment type; package the complete folder or publish it to GCS"
    try:
        with open_regular(work, relative) as stream:
            if path.suffix.lower() in SCRIPT_SUFFIXES:
                checked_content(stream.read(1_000_001))
    except (OSError, ValueError) as exc:
        return str(exc) if isinstance(exc, ValueError) else "not a readable ordinary file"
    return ""


def inventory(target: Path, limit: int) -> tuple[list[tuple[Path, int, int]], int]:
    entries: list[tuple[Path, int, int]] = []
    total = 0
    for directory, dirs, files in os.walk(target, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("Folder contains a symlink or special file; nothing was packaged.")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError("Folder contains a hard link; nothing was packaged.")
                total += info.st_size
            entries.append((path, info.st_size if path.is_file() else -1, info.st_mtime_ns))
            if total > limit or len(entries) > MAX_FOLDER_ENTRIES:
                raise ValueError(
                    "The complete folder exceeds the attachment budget; publish the whole "
                    "folder to approved GCS or split it explicitly. Nothing packaged."
                )
    return entries, total


def package_folder(work: Path, folder: str, *, limit: int = MAX_ATTACHMENT_BYTES) -> dict[str, Any]:
    relative = Path(folder)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] not in {"code", "outputs"}
        or relative.parts == ("outputs",)
        or relative.parts[:2] == ("outputs", "gcs")
        or any(p.startswith(".") for p in relative.parts)
    ):
        raise ValueError("Choose code/ or a folder under outputs/ (not outputs/gcs/).")
    target = work / relative
    if not target.is_dir() or any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError("Choose an existing workspace folder without symlinks.")
    entries, total = inventory(target, limit)
    if not entries:
        raise ValueError("Folder is empty; nothing to attach.")
    if not any(p.suffix.lower() in SCRIPT_SUFFIXES for p, n, _ in entries if n >= 0):
        raise ValueError(
            "No scripts in this folder. Send reports and figures directly, without a ZIP."
        )
    output = work / "outputs"
    if output.is_symlink():
        raise ValueError("outputs/ must not be a symlink.")
    output.mkdir(exist_ok=True)
    archive = output / f"{target.name}-{uuid.uuid4().hex[:12]}.zip"
    names: list[str] = []
    hashes: dict[str, str] = {}
    try:
        with archive.open("xb") as stream, zipfile.ZipFile(stream, "w") as package:
            for path, size, modified in sorted(entries):
                name = target.name + "/" + path.relative_to(target).as_posix()
                if size < 0:
                    package.writestr(name + "/", b"")
                    continue
                with open_regular(work, path.relative_to(work)) as source:
                    before = os.fstat(source.fileno())
                    if before.st_size != size or before.st_mtime_ns != modified:
                        raise ValueError("Folder changed during packaging; retry when complete.")
                    if path.suffix.lower() in SCRIPT_SUFFIXES:
                        checked_content(source.read(1_000_001))
                        source.seek(0)
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    count = 0
                    digest = hashlib.sha256()
                    with package.open(info, "w") as dest:
                        while chunk := source.read(min(1024**2, size - count + 1)):
                            count += len(chunk)
                            if count > size:
                                raise ValueError("Folder changed during packaging; retry.")
                            dest.write(chunk)
                            digest.update(chunk)
                    after = os.fstat(source.fileno())
                    if count != size or after.st_mtime_ns != modified:
                        raise ValueError("Folder changed during packaging; retry.")
                names.append(name)
                hashes[name] = digest.hexdigest()
        if inventory(target, limit)[0] != entries:
            raise ValueError("Folder changed during packaging; retry when complete.")
        if archive.stat().st_size > limit:
            raise ValueError("Complete ZIP exceeds the attachment budget; use approved GCS.")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return {
        "status": "ready_for_attachment",
        "folder": folder,
        "attachment": archive.relative_to(work).as_posix(),
        "files": len(names),
        "source_bytes": total,
        "archive_bytes": archive.stat().st_size,
        "paths": names,
        "sha256": hashes,
        "structure": "All nested regular files and empty directories preserved in the ZIP.",
        "delivery": "Prepared locally; delivery is confirmed by the application after the turn.",
    }


def folder_tool(
    work: Path, prepared: dict[Path, dict[Path, str]], *, limit: int = MAX_ATTACHMENT_BYTES
) -> SdkMcpTool[Any]:
    @tool(
        "prepare_folder_attachment",
        "Prepare a complete ZIP of a script-containing code/ or outputs/ subfolder for delivery. "
        "Use only when sending scripts; ordinary reports/figures go directly. Includes all "
        "nested files, helper scripts, and empty folders with paths preserved; never filters "
        "by extension. Rejects links, special files and oversized trees instead of partial ZIPs. "
        f"Budget: {limit:,} bytes uncompressed and compressed, {MAX_FOLDER_ENTRIES:,} entries. "
        "For outputs/gcs/ use publish_gcs instead. Returns the exact package path and inventory; "
        "this prepares delivery but does not confirm sending. Keep desired figures in outputs/ "
        "for individual previews too.",
        {
            "type": "object",
            "properties": {"folder": {"type": "string"}},
            "required": ["folder"],
            "additionalProperties": False,
        },
    )
    async def prepare(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(args) != {"folder"} or not isinstance(args["folder"], str):
                raise ValueError("Provide one folder path.")
            result = await asyncio.to_thread(package_folder, work, args["folder"], limit=limit)
            prepared[work / result["attachment"]] = {
                work / Path(args["folder"]).parent / name: result["sha256"][name]
                for name in result["paths"]
            }
            del result["sha256"]
            result["paths_truncated"] = len(result["paths"]) > 100
            result["paths"] = result["paths"][:100]
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except (ValueError, OSError) as exc:
            message = (
                str(exc) if isinstance(exc, ValueError) else "Folder could not be read safely."
            )
            return {"content": [{"type": "text", "text": message}], "isError": True}

    return prepare
