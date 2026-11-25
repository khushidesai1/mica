"""Bounded, credential-free checkout and proposal formats shared with workers."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .project_memory import SECRET
from .storage import Storage

SHA = r"^[a-f0-9]{40}$"
ID = r"^[a-f0-9]{32}$"
MAX_ENTRIES = 20_000


class Limits(BaseModel):
    checkout_bytes: int = Field(default=32 * 1024**2, ge=1, le=128 * 1024**2)
    expanded_bytes: int = Field(default=128 * 1024**2, ge=1, le=512 * 1024**2)
    files: int = Field(default=50, ge=1, le=100)
    file_bytes: int = Field(default=256 * 1024, ge=1, le=1024**2)
    total_bytes: int = Field(default=1024**2, ge=1, le=4 * 1024**2)


class Pin(BaseModel):
    repo: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,38}/[a-z0-9][a-z0-9_.-]{0,99}$")
    commit: str = Field(pattern=SHA)
    branch: str = Field(min_length=1, max_length=255)
    checkout_key: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    index_key: str
    index_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    limits: Limits = Field(default_factory=Limits)


class Entry(BaseModel):
    sha: str = Field(pattern=SHA)
    mode: str = Field(pattern=r"^(100644|100755|120000|160000)$")
    included: bool = False


class Index(BaseModel):
    commit: str = Field(pattern=SHA)
    entries: dict[str, Entry]
    omitted: list[str] = Field(default_factory=list)


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    # UTF-8 content is frozen in the controller's approval document before review.
    content: str | None = None
    mode: str = Field(default="100644", pattern=r"^100(644|755)$")


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID)
    commit: str = Field(pattern=SHA)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=8000)
    checks: str = Field(default="Not reported", max_length=4000)
    changes: list[Change] = Field(min_length=1, max_length=100)


def prefix(event_id: str) -> str:
    if not re.fullmatch(ID, event_id):
        raise ValueError("Invalid pull request event")
    return f"tasks/slack-analysis/events/{event_id}/pull-request/"


def staged_key(event_id: str, proposal_id: str) -> str:
    if not re.fullmatch(ID, proposal_id):
        raise ValueError("Invalid proposal identity")
    return prefix(event_id) + "proposals/" + proposal_id + ".json"


def path_name(path: str) -> str:
    if (
        not path
        or len(path) > 500
        or path.startswith("/")
        or "\\" in path
        or any(p in {"", ".", "..", ".git"} for p in path.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise ValueError("Unsafe repository path")
    return path


def secret_name(path: str) -> bool:
    return bool(
        SECRET.search(path)
        or any(
            p.lower()
            in {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519"}
            or p.lower().endswith((".pem", ".key"))
            or p.lower().startswith(".env.")
            for p in path.split("/")
        )
    )


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def filtered_archive(raw: bytes, index: Index, limits: Limits) -> bytes:
    """Never extract an untrusted archive; compare regular files with Git's pinned tree."""
    if len(raw) > limits.checkout_bytes or len(index.entries) > MAX_ENTRIES:
        raise ValueError("Repository checkout exceeds its size limit")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        expanded = compressed.read(limits.expanded_bytes + MAX_ENTRIES * 1024 + 1)
    if len(expanded) > limits.expanded_bytes + MAX_ENTRIES * 1024:
        raise ValueError("Expanded repository archive exceeds its size limit")
    output = io.BytesIO()
    seen: set[str] = set()
    total = 0
    top = ""
    with (
        tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as source,
        tarfile.open(fileobj=output, mode="w:gz") as target,
    ):
        for count, member in enumerate(source):
            if count >= MAX_ENTRIES * 2:
                raise ValueError("Repository archive has too many entries")
            pieces = member.name.split("/", 1)
            if not top:
                top = path_name(pieces[0])
            if pieces[0] != top:
                raise ValueError("Repository archive has multiple roots")
            if len(pieces) == 1 or member.isdir():
                continue
            path = pieces[1]
            try:
                path_name(path)
            except ValueError:
                index.omitted.append(path)
                continue
            if path in seen:
                raise ValueError("Duplicate repository archive path")
            seen.add(path)
            entry = index.entries.get(path)
            if (
                not member.isfile()
                or not entry
                or entry.mode not in {"100644", "100755"}
                or path in {".pin", ".snapshot.json"}
                or secret_name(path)
            ):
                index.omitted.append(path)
                continue
            total += member.size
            if member.size < 0 or total > limits.expanded_bytes:
                raise ValueError("Expanded repository checkout exceeds its size limit")
            stream = source.extractfile(member)
            assert stream is not None
            data = stream.read(limits.expanded_bytes + 1)
            if len(data) != member.size or blob_sha(data) != entry.sha:
                raise ValueError("Repository archive does not match its pinned Git tree")
            if SECRET.search(data.decode("utf-8", errors="replace")):
                index.omitted.append(path)
                continue
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(data), int(entry.mode[-3:], 8)
            target.addfile(info, io.BytesIO(data))
            entry.included = True
    index.omitted = sorted(
        set(index.omitted) | {p for p, entry in index.entries.items() if not entry.included}
    )
    result = output.getvalue()
    if len(result) > limits.checkout_bytes:
        raise ValueError("Filtered repository checkout exceeds its size limit")
    return result


def read_bounded(storage: Storage, key: str, limit: int, sha: str = "") -> bytes:
    raw = storage.read_prefix(key, limit + 1)
    if len(raw) > limit or (sha and hashlib.sha256(raw).hexdigest() != sha):
        raise ValueError("Staged repository data is oversized or changed")
    return raw


def load_checkout(storage: Storage, pin: Pin) -> tuple[Index, dict[str, bytes]]:
    index = Index.model_validate_json(
        read_bounded(storage, pin.index_key, 16 * 1024**2, pin.index_sha256)
    )
    if index.commit != pin.commit or len(index.entries) > MAX_ENTRIES:
        raise ValueError("Checkout index does not match the pin")
    raw = read_bounded(storage, pin.checkout_key, pin.limits.checkout_bytes, pin.sha256)
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for count, member in enumerate(archive):
            path = path_name(member.name)
            entry = index.entries.get(path)
            total += member.size
            if (
                count >= MAX_ENTRIES
                or not member.isfile()
                or path in files
                or not entry
                or not entry.included
                or total > pin.limits.expanded_bytes
                or member.mode != int(entry.mode[-3:], 8)
            ):
                raise ValueError("Unsafe filtered checkout")
            stream = archive.extractfile(member)
            assert stream is not None
            data = stream.read(pin.limits.expanded_bytes + 1)
            if len(data) != member.size or blob_sha(data) != entry.sha:
                raise ValueError("Checkout file changed")
            files[path] = data
    if files.keys() != {p for p, e in index.entries.items() if e.included}:
        raise ValueError("Incomplete filtered checkout")
    return index, files


def clear_directory(root: Path) -> None:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    elif root.exists():
        for directory, _, _ in os.walk(root):
            Path(directory).chmod(0o755)
        shutil.rmtree(root)


def materialize(storage: Storage, work: Path, pin: Pin, *, resume: bool) -> None:
    index, files = load_checkout(storage, pin)
    repo = Path(pin.repo)
    base, editable = work / "inputs/pull-request" / repo, work / "contrib" / repo
    for root in (
        work / "inputs",
        work / "inputs/pull-request",
        base.parent,
        work / "contrib",
        editable.parent,
    ):
        if root.is_symlink():
            raise ValueError("Unsafe repository workspace")
        root.mkdir(parents=True, exist_ok=True)
    clear_directory(base)
    base.mkdir()
    keep = resume and editable.is_dir() and not editable.is_symlink()
    if not keep:
        clear_directory(editable)
        editable.mkdir()
    for path, data in files.items():
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o555 if index.entries[path].mode == "100755" else 0o444)
        if not keep:
            target = editable / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(int(index.entries[path].mode[-3:], 8))
    (base / ".pin").write_text(f"{pin.repo}@{pin.commit}\n")
    (base / ".snapshot.json").write_text(index.model_dump_json())
    for metadata in (base / ".pin", base / ".snapshot.json"):
        metadata.chmod(0o444)


def scan_changes(work: Path, pin: Pin, index: Index, base: dict[str, bytes]) -> list[Change]:
    root = work / "contrib" / pin.repo
    if any(p.is_symlink() for p in (root, root.parent, root.parent.parent)) or not root.is_dir():
        raise ValueError("Contribution checkout must be an ordinary directory")
    found: set[str] = set()
    changes: list[Change] = []
    count = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted([*dirs, *names]):
            count += 1
            if count > MAX_ENTRIES * 2:
                raise ValueError("Contribution checkout has too many entries")
            target = Path(directory) / name
            path = path_name(target.relative_to(root).as_posix())
            if target.is_symlink():
                raise ValueError("Symlinks are not allowed in a proposal")
            if name in dirs:
                continue
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Only regular files are allowed in a proposal")
                # Unchanged large/binary fixtures can remain in the full checkout.
                data = stream.read(max(len(base.get(path, b"")), pin.limits.file_bytes) + 1)
            mode = "100755" if info.st_mode & 0o111 else "100644"
            if path in index.entries and mode != index.entries[path].mode:
                raise ValueError("File mode changes are not supported")
            found.add(path)
            if path not in base or data != base[path]:
                if len(data) > pin.limits.file_bytes:
                    raise ValueError("Changed file exceeds proposal size limit")
                changes.append(Change(path=path, content=data.decode("utf-8"), mode=mode))
    changes.extend(Change(path=p, mode=index.entries[p].mode) for p in sorted(base.keys() - found))
    return changes


def validate(proposal: Proposal, pin: Pin, index: Index, base: dict[str, bytes]) -> None:
    if proposal.commit != pin.commit or len(proposal.changes) > pin.limits.files:
        raise ValueError("Proposal exceeds file limit or does not match the pinned base")
    if SECRET.search(proposal.title + "\n" + proposal.body + "\n" + proposal.checks):
        raise ValueError("Proposal text appears to contain a secret")
    touched: set[str] = set()
    total = 0
    for change in proposal.changes:
        path = path_name(change.path)
        if path in touched or path in {".pin", ".snapshot.json"}:
            raise ValueError("Duplicate or reserved proposal path")
        touched.add(path)
        if path == ".github/workflows" or path.startswith(".github/workflows/"):
            raise ValueError("Workflow edits are not allowed")
        if secret_name(path) or any(
            path == omitted or path.startswith(omitted + "/") or omitted.startswith(path + "/")
            for omitted in index.omitted
        ):
            raise ValueError("Omitted or secret-like paths cannot be changed")
        old = base.get(path)
        if old is None:
            if (
                change.content is None
                or change.mode != "100644"
                or any(path.startswith(p + "/") or p.startswith(path + "/") for p in index.entries)
            ):
                raise ValueError("Invalid new file or file/directory conflict")
        elif change.mode != index.entries[path].mode:
            raise ValueError("File mode changes are not supported")
        if change.content is not None and change.mode != "100644":
            raise ValueError("Executable file edits are not supported by the commit API")
        for data in (old, change.content.encode() if change.content is not None else None):
            if data is None:
                continue
            total += len(data)
            if len(data) > pin.limits.file_bytes or total > pin.limits.total_bytes:
                raise ValueError("Proposal exceeds byte limits")
            text = data.decode("utf-8")
            if "\0" in text or SECRET.search(text):
                raise ValueError("Binary or secret-like files cannot be changed")
        if old == (change.content.encode() if change.content is not None else None):
            raise ValueError("Proposal contains an unchanged file")


def encoded(proposal: Proposal) -> bytes:
    return proposal.model_dump_json().encode()


def tree_sha(index: Index, changes: list[Change]) -> str:
    """Calculate Git's exact target tree, including all omitted files and submodules."""
    entries = {p: (e.mode, e.sha) for p, e in index.entries.items()}
    for change in changes:
        if change.content is None:
            entries.pop(change.path)
        else:
            entries[change.path] = (change.mode, blob_sha(change.content.encode()))
    root: dict[str, Any] = {}
    for path, value in entries.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def digest(node: dict[str, Any]) -> str:
        data = bytearray()
        for name, value in sorted(
            node.items(),
            key=lambda item: (item[0] + ("/" if isinstance(item[1], dict) else "")).encode(),
        ):
            mode, sha = ("40000", digest(value)) if isinstance(value, dict) else value
            data.extend(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(sha))
        return hashlib.sha1(b"tree " + str(len(data)).encode() + b"\0" + data).hexdigest()

    return digest(root)
