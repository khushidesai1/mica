"""Resumable paraphrase-only migration of legacy thread exchanges and working scripts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import checkpoints as workspace
from . import memory_artifacts, memory_tasks, memory_workspace, project_memory, thread_memory
from .registry import Registry
from .storage import Storage


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
            result: dict[str, Any] = {
                "project": pid,
                "thread": tid,
                "events": len(info),
                "estimated_input_tokens": sum(i.size for i in info) // 4,
                "model_calls_upper_bound": len(info),
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
            # Include bounded legacy checkpoint messages not yet backfilled to history.
            records: list[tuple[dict[str, Any], str, str, str, float]] = []
            for item in info:
                if item.size > 512 * 1024:
                    raise ValueError("Oversized legacy exchange; retain source for review")
                body, source_generation = await asyncio.to_thread(storage.get, item.key)
                if source_generation != item.generation:
                    raise ValueError("History changed while importing")
                row = json.loads(body)
                event = str(row.get("id") or Path(item.key).stem)
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
                    event = str(
                        row.get("id")
                        or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                    )
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
            for ordinal, (row, source, source_generation, source_hash, updated) in enumerate(
                records, 1
            ):
                event = row["id"]
                old_source = receipt.get("sources", {}).get(source)
                if old_source and old_source.get("sha256") != source_hash and source != checkpoint:
                    raise ValueError("Imported source changed; preserve it for review")
                if event not in existing:
                    cache_key = f"projects/_threads/{tid}/migration-proposals/{pid}/{event}.json"
                    try:
                        cached = json.loads((await asyncio.to_thread(storage.get, cache_key))[0])
                        if cached["source_sha256"] != source_hash:
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
                                "success": row.get("success", True),
                                "speaker": "Unknown legacy speaker",
                                "date": datetime.fromtimestamp(updated, UTC).isoformat()
                                if updated
                                else "unknown",
                                "instruction": "Paraphrase only; no project-file changes.",
                            },
                        )
                        prose = thread_memory.Prose.model_validate_json(edits["_thread"])
                        cached = {
                            "source_sha256": source_hash,
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
                        bool(row.get("success", True)),
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
                    "verified_at": time_now(),
                }
                await asyncio.to_thread(storage.put, rkey, json.dumps(receipt).encode())
            # Import the latest working code only for this checkpoint's actual project.
            if state.get("context", {}).get("project_slug") == pid and state.get("archive"):
                archive_key = state["archive"]
                archive_info = await asyncio.to_thread(memory_artifacts.stat, storage, archive_key)
                old = receipt.get("archive", {})
                if old.get("generation") != archive_info.generation:
                    with tempfile.TemporaryDirectory(prefix="mica-thread-import-") as temporary:
                        home = Path(temporary)
                        parts = Path(state["context"]["workdir"]).parts
                        relative = Path(*parts[parts.index("work") :])
                        work = home / relative
                        with storage.open_read(archive_key) as stream:
                            workspace.restore_workspace(stream, home, work=work)
                        proposal = memory_workspace.decode_pending(
                            memory_workspace.pending(
                                work,
                                project_memory.Snapshot(head_oid=base.head_oid),
                                backend.settings,
                                thread=tid,
                            ),
                            backend.settings,
                        )
                        event = (
                            "scripts-"
                            + hashlib.sha256(
                                (archive_key + archive_info.generation).encode()
                            ).hexdigest()[:32]
                        )
                        prose = thread_memory.Prose(
                            paraphrase="Imported the retained working scripts."
                        )
                        update = thread_memory.make_update(
                            tid,
                            event,
                            0,
                            datetime.fromtimestamp(archive_info.updated, UTC).isoformat(),
                            "Mica migration",
                            "email" if state.get("private_owner") else "channel",
                            True,
                            prose,
                            [],
                            scripts=proposal.scripts,
                            rejected=proposal.rejected,
                        )
                        await backend.commit(
                            pid, base, {}, source="import", event_id=event, thread_update=update
                        )
                        verified = await memory_tasks.verify(backend, pid, update)
                        receipt["archive"] = {
                            "source": archive_key,
                            "generation": archive_info.generation,
                            "verified_at": time_now(),
                            "scripts": verified["scripts"],
                            "rejected": proposal.rejected,
                            "retain": True,
                        }
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
