"""Resumable paraphrase-only migration of legacy thread exchanges and working scripts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import memory_artifacts, memory_tasks, project_memory, thread_memory
from .config import Settings
from .registry import Registry
from .storage import Storage


def read_scripts(
    storage: Storage,
    info: memory_artifacts.ObjectInfo,
    state: dict[str, Any],
    base: project_memory.Snapshot,
    thread: str,
    settings: Settings,
) -> tuple[dict[str, str], dict[str, str]]:
    scripts: dict[str, str] = {}
    rejected: dict[str, str] = {}
    parts = Path(state.get("context", {}).get("workdir", "")).parts
    if "work" not in parts or ".." in parts:
        return {}, {"archive": "Unrecognized workspace path; source retained."}
    root = Path(*parts[parts.index("work") :]).as_posix() + "/code/"
    target_root = thread_memory.prefix(thread) + "scripts/"
    sizes = {p: b.size for p, b in base.assets.items() if p.startswith(target_root)}
    total = sum(sizes.values())
    try:
        for archive, member in memory_artifacts.archive_members(storage, info, settings):
            if not member.name.startswith(root) or member.isdir():
                continue
            relative = member.name.removeprefix(root)
            if len(scripts) + len(rejected) >= 190:
                raise memory_artifacts.ArchiveLimit("Script entry limit")
            try:
                path = target_root + relative
                project_memory.valid_path(path)
                if relative == "MANIFEST.json" or member.size > settings.thread_script_kb * 1024:
                    raise ValueError("Script size or reserved path")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Unreadable script")
                data = stream.read(settings.thread_script_kb * 1024 + 1)
                text = project_memory.validate(path, data, settings)
                proposed = total - sizes.get(path, 0) + len(data)
                if proposed > settings.thread_scripts_kb * 1024 * 9 // 10:
                    raise ValueError("Script capacity reserve")
                scripts[relative], total = text, proposed
            except (ValueError, UnicodeError):
                rejected[f"script-{len(rejected) + 1}"] = (
                    "Unsafe, secret-like or oversized script; archive retained."
                )
    except (ValueError, OSError, EOFError, tarfile.TarError) as exc:
        rejected["archive"] = (
            str(exc) if isinstance(exc, memory_artifacts.ArchiveLimit) else "Unreadable archive"
        ) + "; source retained."
    return scripts, rejected


async def import_scripts(
    storage: Storage,
    backend: project_memory.GitHubMemory,
    pid: str,
    thread: str,
    state: dict[str, Any],
    base: project_memory.Snapshot,
) -> dict[str, Any]:
    source = state["archive"]
    info = await asyncio.to_thread(memory_artifacts.stat, storage, source)
    event = "scripts-" + hashlib.sha256((source + info.generation).encode()).hexdigest()[:32]
    cache = f"projects/_threads/{thread}/migration-proposals/{pid}/{event}.json"
    try:
        update = thread_memory.Update.model_validate_json(
            (await asyncio.to_thread(storage.get, cache))[0]
        )
    except FileNotFoundError:
        scripts, rejected = await asyncio.to_thread(
            read_scripts, storage, info, state, base, thread, backend.settings
        )
        if (
            await asyncio.to_thread(memory_artifacts.stat, storage, source)
        ).generation != info.generation:
            raise ValueError("Archive changed while importing; retry") from None
        update = thread_memory.make_update(
            thread,
            event,
            0,
            datetime.fromtimestamp(info.updated, UTC).isoformat(),
            "Mica migration",
            "email" if state.get("private_owner") else "channel",
            True,
            thread_memory.Prose(
                paraphrase=f"Imported {len(scripts)} working scripts; "
                "retained the source workspace for verification."
            ),
            [],
            scripts=scripts,
            rejected=rejected,
            outputs=[source],
        )
        await asyncio.to_thread(storage.put, cache, update.model_dump_json().encode())
    existing = next(
        (
            row
            for row in thread_memory.log_entries({p: f.text for p, f in base.files.items()}, thread)
            if row.event_id == event
        ),
        None,
    )
    if existing is None:
        saved = await backend.commit(
            pid, base, {}, source="import", event_id=event, thread_update=update
        )
        if saved.rejected or saved.unmerged:
            raise ValueError("Script import was not saved; source retained")
    elif existing.digest() != update.entry.digest():
        raise ValueError("Script import event changed; source retained")
    verified = await memory_tasks.verify(backend, pid, update)
    return {
        "source": source,
        "generation": info.generation,
        "bytes": info.size,
        "verified_at": time_now(),
        "scripts": verified["scripts"],
        "accepted": sorted(update.scripts or {}),
        "rejected": update.entry.rejected,
        "retain": True,
    }


async def import_threads(
    storage: Storage,
    backend: project_memory.GitHubMemory,
    *,
    apply: bool,
    model_key: str = "",
    project: str = "",
    thread: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    registry = await asyncio.to_thread(Registry(storage).load)
    for checkpoint in await asyncio.to_thread(memory_artifacts.checkpoints, storage):
        tid = checkpoint.split("/")[2]
        if thread and tid != thread:
            continue
        raw, generation = await asyncio.to_thread(
            memory_artifacts.checkpoint_state, storage, checkpoint
        )
        state = json.loads(raw)
        history = await asyncio.to_thread(storage.keys, f"projects/_threads/{tid}/history/")
        pids = {state.get("context", {}).get("project_slug", "")}
        pids.update(k.split("/")[4] for k in history if len(k.split("/")) == 6)
        for pid in sorted(pids & registry.projects.keys()):
            if not pid or (project and pid != project):
                continue
            keys = [k for k in history if f"/history/{pid}/" in k]
            # Legacy unscoped history is eligible only when the checkpoint still identifies
            # its project. Never guess attribution or project membership for orphaned data.
            if state.get("context", {}).get("project_slug") == pid:
                keys += [k for k in history if len(k.split("/")) == 5]
            info = [await asyncio.to_thread(memory_artifacts.stat, storage, k) for k in keys]
            info.sort(key=lambda i: (i.updated, i.key))
            # Include bounded legacy checkpoint messages not yet backfilled to history.
            records: list[tuple[dict[str, Any], str, str, str, float]] = []
            for item in info:
                if item.size > 512 * 1024:
                    raise ValueError("Oversized legacy exchange; retain source for review")
                body, source_generation = await asyncio.to_thread(storage.get, item.key)
                if source_generation != item.generation:
                    raise ValueError("History changed while importing")
                row = json.loads(body)
                event = thread_memory.legacy_event_id(
                    {**row, "id": row.get("id") or Path(item.key).stem}
                )
                row["id"] = event
                records.append(
                    (
                        row,
                        item.key,
                        source_generation,
                        hashlib.sha256(body).hexdigest(),
                        item.updated,
                    )
                )
            if state.get("context", {}).get("project_slug") == pid:
                ids = {str(row[0]["id"]) for row in records}
                for row in state.get("messages", []):
                    event = thread_memory.legacy_event_id(row)
                    if event not in ids:
                        records.append(
                            (
                                {**row, "id": event},
                                checkpoint,
                                generation,
                                hashlib.sha256(raw).hexdigest(),
                                0,
                            )
                        )
            raw_bytes = sum(len(json.dumps(row).encode()) for row, *_ in records)
            result: dict[str, Any] = {
                "project": pid,
                "thread": tid,
                "events": len(records),
                "history_objects": len(info),
                "checkpoint_only_events": len(records) - len(info),
                "raw_input_bytes": raw_bytes,
                "estimated_raw_input_tokens": raw_bytes // 4,
                "estimated_input_tokens": raw_bytes // 4 + len(records) * 6500,
                "model_calls_upper_bound": len(records),
                "status": "would-import",
            }
            results.append(result)
            if not apply:
                continue
            if not model_key:
                raise ValueError("Thread import needs the configured memory-model credential")
            rkey = memory_artifacts.receipt_key(tid, pid)
            try:
                receipt = json.loads((await asyncio.to_thread(storage.get, rkey))[0])
            except FileNotFoundError:
                receipt = {"events": {}, "sources": {}, "rejected": {}, "run_roots": []}
            base = await backend.read(pid)
            if base is None:
                raise RuntimeError("Project memory unavailable during import")
            existing = {
                e.event_id: e
                for e in thread_memory.log_entries({p: f.text for p, f in base.files.items()}, tid)
            }
            for ordinal, (row, source, source_generation, source_hash, updated) in enumerate(
                records, 1
            ):
                event = row["id"]
                old_source = receipt.get("sources", {}).get(source)
                if old_source and old_source.get("sha256") != source_hash and source != checkpoint:
                    raise ValueError("Imported source changed; preserve it for review")
                if event not in existing:
                    input_hash = hashlib.sha256(
                        json.dumps(row, sort_keys=True).encode()
                    ).hexdigest()
                    cache_key = f"projects/_threads/{tid}/migration-proposals/{pid}/{event}.json"
                    try:
                        cached = json.loads((await asyncio.to_thread(storage.get, cache_key))[0])
                        if cached["input_sha256"] != input_hash:
                            raise ValueError("Changed migration input")
                        prose = thread_memory.Prose.model_validate(cached["prose"])
                    except FileNotFoundError:
                        files = {p: f.text for p, f in base.files.items()}
                        view = thread_memory.writer_view(files, tid)
                        edits = await memory_tasks.edit(
                            model_key,
                            backend.settings.memory_model,
                            {
                                "files": {},
                                "thread": view,
                                "user": str(row.get("user", "")),
                                "reply": str(row.get("assistant", "")),
                                "success": row.get("success", bool(row.get("assistant"))),
                                "speaker": "Unknown legacy speaker",
                                "date": datetime.fromtimestamp(updated, UTC).isoformat()
                                if updated
                                else "unknown",
                                "instruction": "Paraphrase only; no project-file changes.",
                            },
                        )
                        prose = thread_memory.Prose.model_validate_json(edits["_thread"])
                        cached = {
                            "input_sha256": input_hash,
                            "prose": prose.model_dump(),
                            "covered": view["covered"],
                        }
                        await asyncio.to_thread(storage.put, cache_key, json.dumps(cached).encode())
                    update = thread_memory.make_update(
                        tid,
                        event,
                        ordinal,
                        datetime.fromtimestamp(updated, UTC).isoformat() if updated else "unknown",
                        "Unknown legacy speaker",
                        "email" if state.get("private_owner") else "channel",
                        bool(row.get("success", bool(row.get("assistant")))),
                        prose,
                        cached.get("covered", []),
                    )
                    saved = await backend.commit(
                        pid,
                        base,
                        {},
                        source="import",
                        event_id="thread-" + event,
                        thread_update=update,
                    )
                    if saved.rejected or saved.unmerged:
                        raise ValueError("Rejected thread import; retain source")
                    await memory_tasks.verify(backend, pid, update)
                    base = await backend.read(pid)
                    if base is None:
                        raise RuntimeError("Cannot verify imported history")
                    existing[event] = update.entry
                entry = existing[event]
                receipt["events"][event] = entry.digest()
                receipt["sources"][source] = {
                    "generation": source_generation,
                    "sha256": source_hash,
                    "event_id": event,
                    "entry_sha256": entry.digest(),
                    "verified_at": (old_source or {}).get("verified_at", time_now())
                    if old_source and old_source.get("generation") == source_generation
                    else time_now(),
                }
                await asyncio.to_thread(storage.put, rkey, json.dumps(receipt).encode())
            # Script failures retain their source without blocking conversation coverage.
            if state.get("context", {}).get("project_slug") == pid and state.get("archive"):
                receipt["archive"] = await import_scripts(storage, backend, pid, tid, state, base)
                result["archive"] = receipt["archive"]
            if (await asyncio.to_thread(memory_artifacts.checkpoint_state, storage, checkpoint))[
                1
            ] != generation:
                raise ValueError("Thread resumed during import; rerun before cutover")
            context = state.get("context", {})
            if context.get("thread_ts") and context.get("project_slug") == pid:
                channel_root = f"projects/{pid}/runs/{context['thread_ts']}"
                roots = {channel_root}
                if owner := state.get("private_owner"):
                    from .access import run_root

                    roots.add(run_root(True, owner, pid, tid, context["thread_ts"]))
                # Existing private roots use a hash, so discover only exact project/thread tails.
                for source_key in await asyncio.to_thread(storage.keys, "projects/_users/"):
                    marker = f"/runs/{pid}/{tid}/"
                    if marker in source_key:
                        roots.add(source_key.split(marker)[0] + marker.rstrip("/"))
                receipt["run_roots"] = sorted(roots)
            receipt.update(conversation_verified=True, checkpoint_generation=generation)
            await asyncio.to_thread(storage.put, rkey, json.dumps(receipt).encode())
            result["status"] = "imported"
    if apply:
        report = await memory_artifacts.coverage(storage, backend)
        await asyncio.to_thread(
            storage.put,
            "projects/_registry/thread-memory-coverage.json",
            json.dumps(report).encode(),
        )
    return results


def time_now() -> float:
    import time

    return time.time()
