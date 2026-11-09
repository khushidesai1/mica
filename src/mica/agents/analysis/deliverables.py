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
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool

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


def select_files(
    work: Path,
    paths: list[str],
    *,
    limit: int = MAX_ATTACHMENT_BYTES,
    prepared: dict[Path, dict[Path, str]] | None = None,
) -> dict[str, Any]:
    """Validate explicit attachments with the same no-link policy as folder delivery."""
    accepted: list[str] = []
    errors: dict[str, str] = {}
    packages = {}
    for raw in dict.fromkeys(paths):
        try:
            relative = Path(raw)
            if relative.is_absolute():
                relative = relative.relative_to(work)
            if ".." in relative.parts or not relative.parts:
                raise ValueError("Choose a workspace output file or code/output folder.")
            path = work / relative
            if path.is_dir():
                package = package_folder(work, relative.as_posix(), limit=limit)
                if prepared is not None:
                    prepared[work / package["attachment"]] = {
                        work / relative.parent / name: digest
                        for name, digest in package["sha256"].items()
                    }
                accepted.append(package["attachment"])
                packages[raw] = {
                    key: package[key] for key in ("attachment", "files", "archive_bytes")
                }
                continue
            if relative.parts[:1] != ("outputs",):
                raise ValueError("Choose a file under outputs/ or a folder under code/.")
            issue = attachment_issue(path, work)
            if issue:
                raise ValueError(issue)
            with open_regular(work, relative) as stream:
                size = os.fstat(stream.fileno()).st_size
            if not size:
                raise ValueError("File is empty.")
            if size > limit:
                raise ValueError(f"Attachment exceeds {limit:,} bytes; split it or publish to GCS.")
            accepted.append(relative.as_posix())
        except (OSError, ValueError) as exc:
            errors[raw] = (
                str(exc) if isinstance(exc, ValueError) else "File could not be read safely."
            )
    return {"accepted": accepted, "errors": errors, **({"packages": packages} if packages else {})}


def send_tool(
    work: Path,
    named: set[Path],
    *,
    manifest: dict[str, Any] | None = None,
    prepared: dict[Path, dict[Path, str]] | None = None,
    limit: int = MAX_ATTACHMENT_BYTES,
) -> SdkMcpTool[Any]:
    from claude_agent_sdk import tool

    @tool(
        "send_files",
        "Select only figures necessary for user review and files the user requested. "
        "Files are never attached just because they exist in outputs/. Keep intermediates, "
        "diagnostics and large datasets internal unless requested. "
        "Send completed outputs/ files or explicitly requested code/output folders as attachments "
        "at turn end, including unchanged files requested again. A folder becomes one ZIP "
        "with every nested file and empty directory preserved. Links, unsafe scripts and "
        f"trees over {limit:,} bytes or {MAX_FOLDER_ENTRIES:,} entries fail without a partial ZIP. "
        "Ordinary files must be nonempty supported figures, PDFs, tables, text/config, scripts "
        "or ZIPs. Returns accepted paths and per-path errors; the application confirms delivery "
        "later. Inspect figures first and send individual figures when previews are useful. "
        "Local paths are not user-accessible. For outputs/gcs/ use publish_gcs with approved "
        "destinations and report only confirmed locations and counts.",
        {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 100}
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    )
    async def send(args: dict[str, Any]) -> dict[str, Any]:
        paths = args.get("paths")
        if (
            set(args) != {"paths"}
            or not isinstance(paths, list)
            or len(paths) > 100
            or not all(isinstance(p, str) for p in paths)
        ):
            return {
                "content": [{"type": "text", "text": "Provide up to 100 file paths."}],
                "isError": True,
            }
        result = await asyncio.to_thread(select_files, work, paths, limit=limit, prepared=prepared)
        named.update(work / path for path in result["accepted"])
        if manifest is not None:
            pending = set(manifest.get("pending_outputs", []))
            pending.update(str(p.relative_to(work / "outputs")) for p in named)
            manifest["pending_outputs"] = sorted(pending)
            selected = set(manifest.get("pending_attachments", []))
            selected.update(str(p.relative_to(work / "outputs")) for p in named)
            manifest["pending_attachments"] = sorted(selected)
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": bool(result["errors"]),
        }

    return send
