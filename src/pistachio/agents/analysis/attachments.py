"""Thread file metadata and byte transport. Media decoding belongs only in the sandbox."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from .config import CHECKPOINT_PREFIX, Settings
from .limits import ROUTER_IMAGE_MAX_BYTES, ROUTER_PDF_MAX_BYTES, THREAD_FILES_MAX
from .storage import Storage

Kind = Literal["image", "pdf", "table", "text", "other"]

log = logging.getLogger(__name__)
HEAD_BYTES = 8192
IMAGE_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TABLE_MEDIA = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".parquet": "application/vnd.apache.parquet",
    ".json": "application/json",
    ".h5ad": "application/x-hdf5",
}
TEXT_MEDIA = {".txt", ".md", ".py", ".r", ".log", ".yaml", ".yml"}


class AttachmentError(ValueError):
    """A bounded, user-facing upload validation report."""


class ThreadFile(BaseModel):
    id: str
    source: Literal["user", "agent"]
    name: str = Field(max_length=200)
    path: str
    kind: Kind
    media_type: str
    size: int = Field(ge=0)
    sha256: str
    key: str | None
    event_id: str
    turn: int | None = None
    user: str = ""


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", unicodedata.normalize("NFKC", name)).lstrip(".")
    suffix = Path(name).suffix[:20]
    if len(name) > 100:
        name = name[: 100 - len(suffix)] + suffix
    return name or "file"


def classify(name: str, head: bytes) -> tuple[Kind, str]:
    for magic, media in (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"II*\0", "image/tiff"),
        (b"MM\0*", "image/tiff"),
        (b"BM", "image/bmp"),
    ):
        if head.startswith(magic):
            return "image", media
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    suffix = Path(name).suffix.lower()
    if suffix in TABLE_MEDIA:
        return "table", TABLE_MEDIA[suffix]
    if suffix in TEXT_MEDIA:
        return "text", "text/markdown" if suffix == ".md" else "text/plain"
    return "other", "application/octet-stream"


def validate_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "files.slack.com"
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The attachment must be a private Slack file.")
    return url


def slack_problems(files: Sequence[dict[str, Any]], settings: Settings) -> list[str]:
    problems = []
    if len(files) > settings.attachment_max_files:
        problems.append(f"Attach at most {settings.attachment_max_files} files per message.")
    total = 0
    for file in files:
        name = str(file.get("name") or "file")[:200]
        size = max(0, int(file.get("size") or 0))
        total += size
        if file.get("mode") in {"external", "tombstone"} or file.get("hidden_by_limit"):
            problems.append(f"{name}: this Slack file is external, deleted, or unavailable.")
        elif size > settings.attachment_max_mb * 1_000_000:
            problems.append(
                f"{name}: exceeds the {settings.attachment_max_mb} MB per-file cap. "
                "Put larger data in the project's mounted GCS bucket and send its gs:// path."
            )
    if total > settings.attachment_max_message_mb * 1_000_000:
        problems.append(
            f"Attachments exceed the {settings.attachment_max_message_mb} MB message cap. "
            "Use the project's mounted GCS bucket and send its gs:// path for larger data."
        )
    return problems


def resolve_url(file: dict[str, Any], client: httpx.Client) -> str:
    url = file.get("url_private_download")
    if not url and file.get("file_access") == "check_file_info" and file.get("id"):
        response = client.get("https://slack.com/api/files.info", params={"file": file["id"]})
        if response.status_code != 200 or not response.json().get("ok"):
            raise ValueError("Slack did not return the attached file information.")
        info = response.json().get("file", {})
        if info.get("mode") in {"external", "tombstone"} or info.get("hidden_by_limit"):
            raise ValueError("The attached Slack file is unavailable.")
        url = info.get("url_private_download")
    return validate_url(str(url or ""))


def upload_root(root: str) -> str:
    """Use the existing 30-day tasks lifecycle, scoped like this thread's provenance."""
    path = PurePosixPath(root)
    if path.is_absolute() or ".." in path.parts or not root.startswith("projects/"):
        raise ValueError("Invalid attachment storage root")
    return f"{CHECKPOINT_PREFIX}/uploads/{root.removeprefix('projects/')}"


def ingest(
    bot_token: str,
    storage: Storage,
    files: Sequence[dict[str, Any]],
    root: str,
    event_id: str,
    settings: Settings,
) -> list[ThreadFile]:
    problems = slack_problems(files, settings)
    if problems:
        raise AttachmentError("\n".join(problems))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", event_id):
        raise ValueError("Invalid attachment event")
    root = upload_root(root)
    total = 0
    ingested = []
    with httpx.Client(
        timeout=60,
        follow_redirects=False,
        headers={"Authorization": f"Bearer {bot_token}"},
    ) as client:
        for index, file in enumerate(files, 1):
            name = str(file.get("name") or "file")[:200]
            filename = f"{index:02d}-{safe_name(str(file.get('name') or 'file'))}"
            key = f"{root}/attachments/{event_id}/{filename}"
            size, head, digest = 0, bytearray(), hashlib.sha256()
            with client.stream("GET", resolve_url(file, client)) as response:
                if response.status_code != 200:
                    raise ValueError("Slack did not return the attached file.")
                with storage.open_write(key) as destination:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > settings.attachment_max_mb * 1_000_000:
                            raise AttachmentError(
                                f"{name}: exceeds the {settings.attachment_max_mb} MB file cap. "
                                "Use the project's mounted GCS bucket for larger data."
                            )
                        if total > settings.attachment_max_message_mb * 1_000_000:
                            raise AttachmentError(
                                f"Attachments exceed the {settings.attachment_max_message_mb} MB "
                                "message cap. Use the project's mounted GCS bucket for larger data."
                            )
                        head.extend(chunk[: max(0, HEAD_BYTES - len(head))])
                        digest.update(chunk)
                        destination.write(chunk)
            kind, media = classify(filename, bytes(head))
            ingested.append(
                ThreadFile(
                    id="f" + hashlib.sha256(f"{event_id}:{index}".encode()).hexdigest()[:8],
                    source="user",
                    name=name,
                    path=f"inputs/{event_id[:8]}/{filename}",
                    kind=kind,
                    media_type=media,
                    size=size,
                    sha256=digest.hexdigest(),
                    key=key,
                    event_id=event_id,
                    user=str(file.get("user") or ""),
                )
            )
    return ingested


def models(files: Iterable[ThreadFile | dict[str, Any]]) -> list[ThreadFile]:
    return [f if isinstance(f, ThreadFile) else ThreadFile.model_validate(f) for f in files]


def merge(
    existing: Iterable[ThreadFile | dict[str, Any]],
    new: Iterable[ThreadFile | dict[str, Any]],
    cap: int = THREAD_FILES_MAX,
) -> list[dict[str, Any]]:
    entries = {f.id: f for f in models([*existing, *new])}
    for source in ("agent", "user"):
        for id, file in list(entries.items()):
            if len(entries) <= cap:
                break
            if file.source == source:
                del entries[id]
    return [f.model_dump() for f in entries.values()]


def router_heads(
    storage: Storage,
    files: Iterable[ThreadFile | dict[str, Any]],
    current_ids: set[str],
) -> dict[str, bytes]:
    heads = {}
    for file in models(files):
        if file.id in current_ids and file.kind in {"text", "table"} and file.key:
            try:
                heads[file.id] = storage.read_prefix(file.key, HEAD_BYTES)
            except FileNotFoundError:
                log.info("Attachment is unavailable: %s", file.id)
    return heads


def shown_ids(media: Sequence[dict[str, Any]]) -> set[str]:
    return {
        block["text"].split(":", 1)[0].removeprefix("Attachment ")
        for block in media
        if block.get("type") == "text" and block.get("text", "").startswith("Attachment ")
    }


def router_listing(
    files: Iterable[ThreadFile | dict[str, Any]],
    current_ids: set[str],
    heads: dict[str, bytes],
    *,
    media: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    shown = shown_ids(media)
    listing = []
    for file in models(files):
        item: dict[str, Any] = {
            k: getattr(file, k) for k in ("id", "name", "kind", "size", "source", "turn")
        }
        item.update(current=file.id in current_ids, shown=file.id in shown)
        if file.id in current_ids and file.id in heads:
            item.update(
                head=heads[file.id][:HEAD_BYTES].decode("utf-8", errors="replace"), shown=True
            )
        if not item["shown"]:
            item["reason"] = (
                "Not requested this turn"
                if file.id not in current_ids
                else "Contents unavailable, unsupported, or above the router size/count budget; "
                "use compute to inspect available files"
            )
        listing.append(item)
    return listing


def router_media(
    storage: Storage,
    files: Iterable[ThreadFile | dict[str, Any]],
) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    images, pdfs = 0, 0
    for file in models(files):
        image = file.kind == "image" and file.media_type in IMAGE_MEDIA
        pdf = file.kind == "pdf" and file.media_type == "application/pdf"
        cap = ROUTER_IMAGE_MAX_BYTES if image else ROUTER_PDF_MAX_BYTES
        if not file.key or file.size > cap or not (image or pdf):
            continue
        if (image and images >= 4) or (pdf and pdfs >= 2):
            continue
        try:
            raw = storage.read_prefix(file.key, cap + 1)
        except FileNotFoundError:
            log.info("Attachment is unavailable: %s", file.id)
            continue
        if (
            len(raw) > cap
            or len(raw) != file.size
            or hashlib.sha256(raw).hexdigest() != file.sha256
            or classify(file.name, raw[:HEAD_BYTES]) != (file.kind, file.media_type)
        ):
            continue
        media.extend(
            [
                {"type": "text", "text": f"Attachment {file.id}: {file.name}"},
                {
                    "type": "image" if image else "document",
                    "source": {
                        "type": "base64",
                        "media_type": file.media_type,
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                },
            ]
        )
        images += int(image)
        pdfs += int(pdf)
    return media


def combine_media(*groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reuse already-fetched blocks within the per-call image/document budgets."""
    result: list[dict[str, Any]] = []
    counts = {"image": 0, "document": 0}
    for group in groups:
        for label, block in zip(group[::2], group[1::2], strict=True):
            kind = block["type"]
            if counts[kind] < (4 if kind == "image" else 2):
                result.extend([label, block])
                counts[kind] += 1
    return result


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def workspace_path(workdir: Path, file: ThreadFile) -> Path:
    relative = PurePosixPath(file.path)
    expected = "inputs" if file.source == "user" else "outputs"
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != expected
    ):
        raise ValueError("Invalid attachment path")
    path = workdir / file.path
    if not path.resolve().is_relative_to(workdir.resolve()) or any(
        part.is_symlink() for part in [path, *path.parents] if part != workdir
    ):
        raise ValueError("Attachment path contains a symlink")
    return path


def materialize(
    storage: Storage,
    workdir: Path,
    files: Iterable[ThreadFile | dict[str, Any]],
) -> list[ThreadFile]:
    present = []
    # A later delivery can replace the same workspace path. Restore its newest version.
    latest = {file.path: file for file in models(files)}
    for file in latest.values():
        temporary: Path | None = None
        try:
            path = workspace_path(workdir, file)
            if not path.is_file() or (file.source == "user" and file_hash(path) != file.sha256):
                if not file.key:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                storage.download(file.key, temporary)
                if temporary.stat().st_size != file.size or file_hash(temporary) != file.sha256:
                    raise ValueError("Attachment integrity check failed")
                temporary.replace(path)
            if file.source == "user":
                path.chmod(0o444)
            present.append(file)
        except (FileNotFoundError, ValueError, OSError):
            log.warning("Could not restore thread file %s", file.id)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return present


def worker_listing(files: Iterable[ThreadFile | dict[str, Any]], current_ids: set[str]) -> str:
    lines = []
    for file in models(files):
        if file.source == "agent":
            detail = f"{file.kind}, you delivered it in turn {file.turn}"
        else:
            when = "attached to this message" if file.id in current_ids else "attached earlier"
            detail = f"{file.kind}, {file.size / 1_000_000:.1f} MB, {when} by <@{file.user}>"
        lines.append(f"- `{file.path}` ({detail})")
    return "\n".join(lines)


def output_record(path: Path, workdir: Path, turn: int, key: str | None) -> dict[str, Any]:
    relative = path.relative_to(workdir).as_posix()
    with path.open("rb") as stream:
        kind, media = classify(path.name, stream.read(HEAD_BYTES))
    return ThreadFile(
        id="o" + hashlib.sha256(f"{turn}:{relative}".encode()).hexdigest()[:8],
        source="agent",
        name=path.name[:200],
        path=relative,
        kind=kind,
        media_type=media,
        size=path.stat().st_size,
        sha256=file_hash(path),
        key=key,
        event_id="",
        turn=turn,
    ).model_dump()
