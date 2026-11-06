"""Verified, generation-fenced migration receipts and temporary artifact retention.

No lifecycle or IAM changes. Sources without complete receipts are retained indefinitely.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from . import access, project_memory, thread_memory
from .config import Settings
from .storage import Conflict, GCSStorage, LocalStorage, Storage

if TYPE_CHECKING:
    from .batch import BatchBridge

SWEEP_SECONDS = 90.0
THREAD_SWEEP_SECONDS = 30.0


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    generation: str
    size: int
    updated: float


class ArchiveLimit(ValueError):
    """Retain a source that cannot be inspected within the migration budget."""


class Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class LimitedReader(io.RawIOBase):
    def __init__(self, stream: Readable, limit: int, deadline: float):
        self.stream, self.remaining, self.deadline = stream, limit, deadline

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() >= self.deadline:
            raise ArchiveLimit("Archive scan time limit")
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ArchiveLimit("Archive scan byte limit")
        return data


def archive_members(
    storage: Storage, info: ObjectInfo, settings: Settings
) -> Iterator[tuple[tarfile.TarFile, tarfile.TarInfo]]:
    """Bound compressed bytes, expansion, members, and time; never extract onto disk."""
    if info.size > settings.thread_archive_scan_mb * 1024**2:
        raise ArchiveLimit("Archive exceeds bounded scan size")
    deadline = time.monotonic() + settings.thread_archive_scan_seconds
    expanded = settings.thread_archive_expanded_mb * 1024**2
    seen: set[str] = set()
    with storage.open_read(info.key) as stream:
        compressed = LimitedReader(stream, settings.thread_archive_scan_mb * 1024**2, deadline)
        with gzip.GzipFile(fileobj=compressed) as unpacked:
            bounded = LimitedReader(unpacked, expanded, deadline)
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    if (
                        len(seen) >= 10000
                        or member.offset_data + member.size > expanded
                        or time.monotonic() >= deadline
                    ):
                        raise ArchiveLimit("Archive member, expansion or time limit")
                    parts = Path(member.name).parts
                    if (
                        member.name in seen
                        or ".." in parts
                        or member.name.startswith("/")
                        or not (member.isfile() or member.isdir())
                    ):
                        raise ArchiveLimit("Unsafe or duplicate archive member")
                    seen.add(member.name)
                    yield archive, member


def stat(storage: Storage, key: str) -> ObjectInfo:
    if isinstance(storage, GCSStorage):
        blob = storage.bucket.blob(storage.physical_key(key))
        from google.api_core.exceptions import NotFound

        try:
            blob.reload()
        except NotFound:
            raise FileNotFoundError(key) from None
        return ObjectInfo(key, str(blob.generation), int(blob.size or 0), blob.updated.timestamp())
    if isinstance(storage, LocalStorage):
        info = storage.path(key).stat()
        return ObjectInfo(key, storage.get(key)[1], info.st_size, info.st_mtime)
    data, generation = storage.get(key)
    return ObjectInfo(key, generation, len(data), time.time())


def receipt_key(thread: str, project: str) -> str:
    thread_memory.prefix(thread)
    project_memory.valid_project(project)
    return f"projects/_threads/{thread}/migration/{project}.json"


def checkpoints(storage: Storage) -> list[str]:
    return sorted(
        {
            "/".join(k.split("/")[:3]) + "/checkpoint.json"
            for k in storage.keys("projects/_threads/")
            if k.endswith("/checkpoint.json") or "/history/" in k
        }
    )


def checkpoint_state(storage: Storage, key: str) -> tuple[bytes, str]:
    try:
        return storage.get(key)
    except FileNotFoundError:
        return b"{}", ""


async def coverage(
    storage: Storage, backend: project_memory.GitHubMemory, *, strict_checkpoint: bool = False
) -> dict[str, Any]:
    """Missing conversation import is never excused by an artifact retention marker."""
    missing: list[str] = []
    verified: list[str] = []
    snapshots: dict[str, project_memory.Snapshot] = {}
    for key in await asyncio.to_thread(checkpoints, storage):
        state_raw, state_generation = await asyncio.to_thread(checkpoint_state, storage, key)
        state = json.loads(state_raw)
        thread = key.split("/")[2]
        projects = {state.get("context", {}).get("project_slug", "")}
        histories = await asyncio.to_thread(storage.keys, f"projects/_threads/{thread}/history/")
        projects.update(p.split("/")[4] for p in histories if len(p.split("/")) == 6)
        if not state.get("context", {}).get("project_slug") and any(
            len(p.split("/")) == 5 for p in histories
        ):
            missing.append(f"unattributed/{thread}")
        for pid in sorted(projects - {""}):
            try:
                native = False
                try:
                    receipt = json.loads(
                        (await asyncio.to_thread(storage.get, receipt_key(thread, pid)))[0]
                    )
                except FileNotFoundError:
                    if state.get("thread_memory_origin") != "github_only":
                        raise
                    native = True
                    receipt = {"conversation_verified": True, "events": {}, "sources": {}}
                if not receipt.get("conversation_verified"):
                    raise ValueError("Unverified conversation")
                if (
                    strict_checkpoint
                    and not native
                    and state.get("context", {}).get("project_slug") == pid
                    and receipt.get("checkpoint_generation") != state_generation
                ):
                    raise ValueError("Checkpoint changed since import")
                snapshot = snapshots.get(pid) or await backend.read(pid)
                if snapshot is None:
                    raise ValueError("Unavailable memory")
                snapshots[pid] = snapshot
                rows = {
                    e.event_id: e.digest()
                    for e in thread_memory.log_entries(
                        {p: f.text for p, f in snapshot.files.items()}, thread
                    )
                }
                for event, digest in receipt["events"].items():
                    if rows.get(event) != digest:
                        raise ValueError("Changed imported event")
                if native:
                    for event, digest in rows.items():
                        saved = json.loads(
                            (
                                await asyncio.to_thread(
                                    storage.get,
                                    f"projects/_threads/{thread}/pending/{event}/receipt.json",
                                )
                            )[0]
                        )
                        if saved.get("project_id") != pid or saved.get("entry_sha256") != digest:
                            raise ValueError("Unverified native event")
                for source in histories:
                    if f"/history/{pid}/" not in source and not (
                        len(source.split("/")) == 5
                        and state.get("context", {}).get("project_slug") == pid
                    ):
                        continue
                    info = await asyncio.to_thread(stat, storage, source)
                    if (
                        receipt.get("sources", {}).get(source, {}).get("generation")
                        != info.generation
                    ):
                        raise ValueError("New or changed history requires import verification")
                if state.get("context", {}).get("project_slug") == pid:
                    for message in state.get("messages", []):
                        if native and message.get("id") in state.get("thread_memory_exempt", []):
                            continue
                        if thread_memory.legacy_event_id(message) not in rows:
                            raise ValueError("Checkpoint exchange is not yet saved")
                verified.append(f"{pid}/{thread}")
            except (FileNotFoundError, ValueError, KeyError):
                missing.append(f"{pid}/{thread}")
    return {
        "complete": not missing,
        "verified": verified,
        "missing": missing,
        "checked_at": time.time(),
    }


async def require_coverage(storage: Storage, backend: project_memory.GitHubMemory) -> None:
    report = await coverage(storage, backend)
    if not report["complete"]:
        raise ValueError("github_only requires verified conversation import for every thread")


async def cutover_report(bridge: BatchBridge) -> dict[str, Any]:
    """Operator gate: re-read coverage and active/retry state immediately before cutover."""
    report = await coverage(
        bridge.storage, project_memory.GitHubMemory(bridge.settings), strict_checkpoint=True
    )
    active = []
    for key in await asyncio.to_thread(checkpoints, bridge.storage):
        thread = key.split("/")[2]
        tail = await bridge.repository.get_connector_state(f"claude-thread:{thread}") or {}
        event = (
            await bridge.repository.get_connector_state(f"claude-event:{tail.get('tail', '')}")
            or {}
        )
        if not await inactive(bridge, event):
            active.append(thread)
    pending = await bridge.repository.due_connector_states(
        "memory_due", time.time() + 365 * 86400, limit=1
    )
    report.update(active=active, pending=bool(pending), checked_at=time.time())
    report["complete"] = report["complete"] and not active and not pending
    return report


def validate_cutover(settings: Settings, report: dict[str, Any]) -> None:
    if (
        not thread_memory.enabled(settings)
        or settings.thread_memory_cutover_at <= 0
        or not report.get("complete")
        or report.get("active")
        or report.get("pending")
        or report.get("checked_at") != settings.thread_memory_cutover_at
    ):
        raise ValueError("github_only requires a fresh verified operator cutover receipt")


async def move_output(
    storage: Storage,
    backend: project_memory.GitHubMemory,
    pid: str,
    thread: str,
    source: str,
    destination: str,
    *,
    now: float,
    min_age: float,
) -> dict[str, Any]:
    """Only controller-enumerated run outputs, never arbitrary bucket/user destinations."""
    project_memory.valid_path(destination)
    expected = thread_memory.prefix(thread) + "outputs/"
    if not destination.startswith(expected) or "/outputs/gcs/" in source:
        raise ValueError("Invalid memory artifact destination")
    info = await asyncio.to_thread(stat, storage, source)
    if now - info.updated < min_age:
        return {"source": source, "status": "young"}
    if info.size > backend.settings.thread_output_kb * 1024:
        return {"source": source, "status": "retained", "reason": "size"}
    raw, generation = await asyncio.to_thread(storage.get, source)
    if generation != info.generation:
        raise ValueError("Source changed while copying")
    base = await backend.read(pid)
    if base is None:
        raise RuntimeError("Cannot read memory for artifact move")
    root = thread_memory.prefix(thread) + "outputs/"
    used = sum(b.size for p, b in base.assets.items() if p.startswith(root) and p != destination)
    if used + len(raw) > backend.settings.thread_outputs_kb * 1024:
        return {"source": source, "status": "retained", "reason": "thread-capacity"}
    sha = hashlib.sha256(raw).hexdigest()
    existing = base.assets.get(destination)
    if existing:
        current, _ = await backend.read_bytes(pid, destination, head_oid=base.head_oid)
        if current != raw:
            raise ValueError("Artifact path already has different bytes")
    else:
        await backend.commit(
            pid,
            base,
            {},
            source="import",
            event_id="artifact-" + sha[:32],
            blobs={destination: raw},
        )
    copied, head = await backend.read_bytes(pid, destination)
    if copied != raw:
        raise ValueError("Artifact verification failed")
    return {
        "source": source,
        "generation": generation,
        "sha256": sha,
        "path": destination,
        "head": head,
        "verified_at": now,
        "status": "verified",
    }


async def expire(
    storage: Storage,
    backend: project_memory.GitHubMemory,
    pid: str,
    receipt: dict[str, Any],
    *,
    now: float,
    age: float,
) -> bool:
    if receipt.get("status") != "verified" or now - receipt["verified_at"] < age:
        return False
    raw, _ = await backend.read_bytes(pid, receipt["path"])
    if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
        return False
    try:
        info = await asyncio.to_thread(stat, storage, receipt["source"])
    except FileNotFoundError:
        return True
    if info.generation != receipt["generation"]:
        return False
    await asyncio.to_thread(storage.delete_if, receipt["source"], receipt["generation"])
    return True


async def sweep(
    bridge: BatchBridge,
    *,
    project: str = "",
    now: float | None = None,
    test_age: float | None = None,
) -> dict[str, int]:
    settings, storage = bridge.settings, bridge.storage
    result = {"memory_outputs_moved": 0, "memory_sources_deleted": 0, "memory_sweep_deferred": 0}
    if settings.thread_memory != "github_only" and test_age is None:
        return result
    if test_age is not None and not project.startswith("_smoke"):
        raise ValueError("Age overrides require an isolated smoke project")
    backend = project_memory.GitHubMemory(settings)
    deadline = asyncio.get_running_loop().time() + SWEEP_SECONDS
    try:
        async with asyncio.timeout(SWEEP_SECONDS):
            report = await coverage(storage, backend)
            keys = await asyncio.to_thread(checkpoints, storage)
            progress = await bridge.repository.get_connector_state("memory-artifact-cursor") or {}
    except TimeoutError:
        # Unknown coverage never authorizes retention work. Retry on the next daily pass.
        result["memory_sweep_deferred"] = 1
        return result
    covered = set(report["verified"])
    now = time.time() if now is None else now
    cursor = int(progress.get("cursor", 0)) % max(1, len(keys))
    selected = keys if project else (keys[cursor:] + keys[:cursor])[:5]
    visited = 0
    for key in selected:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            result["memory_sweep_deferred"] = 1
            break
        visited += 1
        thread = key.split("/")[2]
        # Admission uses the same lease. No new turn can enter between the active check
        # and a fenced deletion; existing queued events keep the tail nonterminal.
        lease = await bridge.repository.begin_event(f"claude-intake:{thread}", lease_seconds=170)
        if lease.status != "claimed":
            continue
        try:
            budget = min(THREAD_SWEEP_SECONDS, deadline - asyncio.get_running_loop().time())
            async with asyncio.timeout(max(0, budget)):
                tail = await bridge.repository.get_connector_state(f"claude-thread:{thread}") or {}
                event = (
                    await bridge.repository.get_connector_state(
                        f"claude-event:{tail.get('tail', '')}"
                    )
                    or {}
                )
                if not await inactive(bridge, event):
                    continue
                state = json.loads((await asyncio.to_thread(checkpoint_state, storage, key))[0])
                pid = state.get("context", {}).get("project_slug", "")
                if not pid or (project and pid != project):
                    continue
                if f"{pid}/{thread}" not in covered:
                    continue
                try:
                    imported = json.loads(
                        (await asyncio.to_thread(storage.get, receipt_key(thread, pid)))[0]
                    )
                except FileNotFoundError:
                    if state.get("thread_memory_origin") != "github_only":
                        continue
                    imported = {"conversation_verified": True}
                if not imported.get("conversation_verified"):
                    continue
                roots = set(imported.get("run_roots", []))
                saved_receipt = event.get("memory_receipt", {})
                if saved_receipt.get("project_id") == pid and saved_receipt.get("run_root"):
                    roots.add(saved_receipt["run_root"])
                if event.get("project_id") == pid:
                    ctx = state.get("context", {})
                    roots.add(
                        access.run_root(
                            bool(event.get("private")),
                            event.get("principal", ""),
                            pid,
                            thread,
                            ctx.get("thread_ts", ""),
                        )
                    )
                ledger_key = f"projects/_threads/{thread}/artifact-receipts/{pid}.json"
                try:
                    ledger = json.loads((await asyncio.to_thread(storage.get, ledger_key))[0])
                except FileNotFoundError:
                    ledger = {}
                for run_root in sorted(roots):
                    for source in await asyncio.to_thread(storage.keys, run_root + "/"):
                        relative = source.removeprefix(run_root + "/")
                        match = re.fullmatch(r"(turn-[0-9]+)/outputs/(.+)", relative)
                        if not match or match[2].startswith("gcs/"):
                            continue
                        destination = f"threads/{thread}/outputs/{match[1]}/{match[2]}"
                        receipt = ledger.get(source)
                        if receipt is None or receipt.get("status") == "young":
                            try:
                                receipt = await move_output(
                                    storage,
                                    backend,
                                    pid,
                                    thread,
                                    source,
                                    destination,
                                    now=now,
                                    min_age=test_age
                                    if test_age is not None
                                    else settings.thread_output_age_days * 86400,
                                )
                            except ValueError:
                                receipt = {
                                    "source": source,
                                    "status": "retained",
                                    "reason": "invalid",
                                }
                            ledger[source] = receipt
                            await asyncio.to_thread(
                                storage.put, ledger_key, json.dumps(ledger).encode()
                            )
                            result["memory_outputs_moved"] += receipt.get("status") == "verified"
                            snapshot = await backend.read(pid)
                            if snapshot is None:
                                raise RuntimeError("Cannot save artifact inventory")
                            manifest = json.dumps(ledger, sort_keys=True).encode()
                            await backend.commit(
                                pid,
                                snapshot,
                                {},
                                source="import",
                                event_id="inventory-" + hashlib.sha256(manifest).hexdigest()[:32],
                                blobs={
                                    thread_memory.prefix(thread) + "outputs/MANIFEST.json": manifest
                                },
                            )
                        if receipt and await expire(
                            storage,
                            backend,
                            pid,
                            receipt,
                            now=now,
                            age=test_age
                            if test_age is not None
                            else settings.thread_expiry_days * 86400,
                        ):
                            result["memory_sources_deleted"] += 1
                # Read-back covers every script/output before a workspace gets an expiry clock.
                archive_ledger_key = f"projects/_threads/{thread}/archive-receipts/{pid}.json"
                try:
                    archives = json.loads(
                        (await asyncio.to_thread(storage.get, archive_ledger_key))[0]
                    )
                except FileNotFoundError:
                    archives = {}
                archive_sources = await asyncio.to_thread(
                    storage.keys, f"projects/_threads/{thread}/archives/"
                )
                archive_sources.sort()
                archive_cursor = int(archives.get("_cursor", 0)) % max(1, len(archive_sources))
                selected_archives = (
                    archive_sources[archive_cursor:] + archive_sources[:archive_cursor]
                )[:4]
                for source in selected_archives:
                    # An archive belongs to the project that produced that exact event.
                    # Never infer ownership from the thread's current project binding.
                    producing_event = (
                        await bridge.repository.get_connector_state(
                            f"claude-event:{Path(source).name.removesuffix('.tar.gz')}"
                        )
                        or {}
                    )
                    imported_archive = imported.get("archive", {})
                    if (
                        producing_event.get("project_id") != pid
                        and imported_archive.get("source") != source
                    ):
                        continue
                    receipt = archives.get(source)
                    if not receipt or receipt.get("status") != "verified":
                        receipt = await verify_archive(
                            storage, backend, pid, thread, source, now=now
                        )
                        archives[source] = receipt
                        await asyncio.to_thread(
                            storage.put, archive_ledger_key, json.dumps(archives).encode()
                        )
                    if (
                        receipt["status"] != "verified"
                        or now - receipt["verified_at"] < settings.thread_expiry_days * 86400
                    ):
                        continue
                    # Verify the pinned durable copies again, then CAS-detach the temporary pointer.
                    for path, sha in receipt["copies"].items():
                        raw, _ = await backend.read_bytes(pid, path, head_oid=receipt["head"])
                        if hashlib.sha256(raw).hexdigest() != sha:
                            raise ValueError("Archive copy changed; retaining source")
                    current_raw, current_generation = await asyncio.to_thread(storage.get, key)
                    current = json.loads(current_raw)
                    if current.get("context", {}).get("project_slug") != pid:
                        continue
                    if current.get("archive") == source:
                        current.pop("archive")
                        current["archive_expired"] = {"head": receipt["head"], "source": source}
                        await asyncio.to_thread(
                            storage.put_if, key, json.dumps(current).encode(), current_generation
                        )
                    try:
                        await asyncio.to_thread(storage.delete_if, source, receipt["generation"])
                        result["memory_sources_deleted"] += 1
                    except (FileNotFoundError, Conflict):
                        pass
                archives["_cursor"] = (archive_cursor + len(selected_archives)) % max(
                    1, len(archive_sources)
                )
                await asyncio.to_thread(
                    storage.put, archive_ledger_key, json.dumps(archives).encode()
                )
                # History import receipts bind exact source generations and compact event digests.
                snapshot = await backend.read(pid)
                rows = (
                    {
                        e.event_id: e.digest()
                        for e in thread_memory.log_entries(
                            {p: f.text for p, f in snapshot.files.items()}, thread
                        )
                    }
                    if snapshot
                    else {}
                )
                for source, receipt in imported.get("sources", {}).items():
                    if (
                        "/history/" not in source
                        or now - receipt.get("verified_at", now)
                        < settings.thread_expiry_days * 86400
                    ):
                        continue
                    if rows.get(receipt.get("event_id")) != receipt.get("entry_sha256"):
                        continue
                    try:
                        await asyncio.to_thread(storage.delete_if, source, receipt["generation"])
                        result["memory_sources_deleted"] += 1
                    except (FileNotFoundError, Conflict):
                        pass
                pending_root = f"projects/_threads/{thread}/pending/"
                for source in await asyncio.to_thread(storage.keys, pending_root):
                    if not source.endswith("/receipt.json"):
                        continue
                    receipt = json.loads((await asyncio.to_thread(storage.get, source))[0])
                    if (
                        receipt.get("project_id") != pid
                        or rows.get(receipt.get("event_id")) != receipt.get("entry_sha256")
                        or now - receipt.get("verified_at", now)
                        < settings.thread_expiry_days * 86400
                    ):
                        continue
                    for name in ("memory-editor.json", "memory-proposal.json"):
                        transport = source.removesuffix("receipt.json") + name
                        try:
                            info = await asyncio.to_thread(stat, storage, transport)
                            await asyncio.to_thread(storage.delete_if, transport, info.generation)
                            result["memory_sources_deleted"] += 1
                        except (FileNotFoundError, Conflict):
                            pass
        except TimeoutError:
            # Durable copy receipts make partial work resumable. Rotate past a slow
            # thread so it cannot block the scheduler or starve other threads.
            result["memory_sweep_deferred"] = 1
        finally:
            await bridge.repository.release_event(f"claude-intake:{thread}", lease.token)
    if not project:
        await bridge.repository.set_connector_state(
            "memory-artifact-cursor", {"cursor": (cursor + visited) % max(1, len(keys))}
        )
    return result


async def inactive(bridge: BatchBridge, event: dict[str, Any]) -> bool:
    """A terminal control message can still have a live predecessor behind it."""
    seen: set[str] = set()
    for _ in range(100):
        if event.get("status") not in {None, "complete", "failed"} or event.get("memory_pending"):
            return False
        if event.get("job") and not event.get("worker_stopped"):
            return False
        previous = event.get("previous")
        if not previous:
            return True
        if previous in seen:
            return False
        seen.add(previous)
        event = await bridge.repository.get_connector_state(f"claude-event:{previous}") or {}
    return False  # A bounded scan must never infer absence of older active work.


async def verify_archive(
    storage: Storage,
    backend: project_memory.GitHubMemory,
    pid: str,
    thread: str,
    source: str,
    *,
    now: float,
) -> dict[str, Any]:
    """An archive expires only when every retained script/output has a verified GitHub copy."""
    expected = f"projects/_threads/{thread}/archives/"
    if not source.startswith(expected) or "/" in source.removeprefix(expected):
        raise ValueError("Only completed thread archives can expire")
    info = await asyncio.to_thread(stat, storage, source)
    if info.size > backend.settings.thread_archive_scan_mb * 1024**2:
        return {
            "source": source,
            "generation": info.generation,
            "head": "",
            "copies": {},
            "rejected": {"archive": "Archive exceeds bounded scan size; source retained."},
            "verified_at": now,
            "status": "retained",
        }
    snapshot = await backend.read(pid)
    if snapshot is None:
        raise RuntimeError("Cannot verify workspace archive")
    copies: dict[str, str] = {}
    output_hashes: set[str] = set()
    for path in snapshot.assets:
        if not path.startswith(thread_memory.prefix(thread)):
            continue
        raw, _ = await backend.read_bytes(pid, path, head_oid=snapshot.head_oid)
        sha = hashlib.sha256(raw).hexdigest()
        copies[path] = sha
        if "/outputs/" in path:
            output_hashes.add(sha)

    def inspect() -> dict[str, str]:
        retained = {}
        try:
            for tar, member in archive_members(storage, info, backend.settings):
                parts = Path(member.name).parts
                if member.isdir():
                    continue
                if not member.isfile() or ".." in parts or member.name.startswith("/"):
                    return {"archive": "unsafe archive member"}
                if parts and parts[0] == ".claude":
                    return {"archive": "legacy transcript needs separate import"}
                for folder in ("code", "outputs"):
                    if folder not in parts:
                        continue
                    relative = "/".join(parts[parts.index(folder) + 1 :])
                    if member.size > backend.settings.thread_output_kb * 1024:
                        retained[member.name] = "oversized member"
                        continue
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        retained[member.name] = "unreadable member"
                        continue
                    sha = hashlib.sha256(extracted.read(member.size + 1)).hexdigest()
                    if folder == "code":
                        if copies.get(thread_memory.prefix(thread) + "scripts/" + relative) != sha:
                            retained[member.name] = "script has no matching GitHub copy"
                    elif sha not in output_hashes:
                        retained[member.name] = "output has no matching GitHub copy"
        except (ValueError, OSError, EOFError, tarfile.TarError) as exc:
            retained["archive"] = (
                str(exc) if isinstance(exc, ArchiveLimit) else "Unreadable archive"
            ) + "; source retained."
        return retained

    rejected = await asyncio.to_thread(inspect)
    if (await asyncio.to_thread(stat, storage, source)).generation != info.generation:
        raise ValueError("Archive changed during verification")
    return {
        "source": source,
        "generation": info.generation,
        "head": snapshot.head_oid,
        "copies": copies,
        "rejected": rejected,
        "verified_at": now,
        "status": "retained" if rejected else "verified",
    }
