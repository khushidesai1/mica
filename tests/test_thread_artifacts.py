from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from analysis_helpers import seed_project

from mica.agents.analysis import memory_artifacts as artifacts
from mica.agents.analysis import memory_tasks, thread_import, thread_memory
from mica.agents.analysis.config import Settings
from mica.agents.analysis.project_memory import GitHubMemory
from mica.agents.analysis.storage import LocalStorage
from tests.fake_github import FakeGitHub


@pytest.fixture
def stores(tmp_path):
    storage = LocalStorage(tmp_path / "gcs")
    github = FakeGitHub()
    backend = GitHubMemory(
        Settings(memory_repo="owner/memory", _env_file=None), client=github.client
    )
    return storage, backend, github


async def test_binary_copy_age_cap_and_generation_fenced_expiry(stores):
    storage, backend, _ = stores
    source = "projects/p_test/runs/test/turn-1/outputs/plot.png"
    target = "threads/t_one/outputs/turn-1/plot.png"
    raw = b"\x89PNG\x00\xffa test plot"
    storage.put(source, raw)
    created = artifacts.stat(storage, source).updated
    young = await artifacts.move_output(
        storage, backend, "p_test", "t_one", source, target, now=created, min_age=10 * 86400
    )
    assert young["status"] == "young"
    receipt = await artifacts.move_output(
        storage,
        backend,
        "p_test",
        "t_one",
        source,
        target,
        now=created + 11 * 86400,
        min_age=10 * 86400,
    )
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    snapshot = await backend.read("p_test")
    assert target in snapshot.assets and target not in snapshot.files
    assert (await backend.read_bytes("p_test", target))[0] == raw
    assert not await artifacts.expire(
        storage, backend, "p_test", receipt, now=receipt["verified_at"] + 29 * 86400, age=30 * 86400
    )
    storage.put(source, b"a replacement generation")
    assert not await artifacts.expire(
        storage, backend, "p_test", receipt, now=receipt["verified_at"] + 31 * 86400, age=30 * 86400
    )
    storage.put(source, raw)
    assert await artifacts.expire(
        storage, backend, "p_test", receipt, now=receipt["verified_at"] + 31 * 86400, age=30 * 86400
    )
    with pytest.raises(FileNotFoundError):
        storage.get(source)


async def test_size_capacity_and_gcs_staging_never_moved(stores):
    storage, backend, github = stores
    backend.settings.thread_output_kb = 1
    source = "projects/p_test/runs/test/turn-1/outputs/large.txt"
    storage.put(source, b"x" * 1025)
    receipt = await artifacts.move_output(
        storage,
        backend,
        "p_test",
        "t_one",
        source,
        "threads/t_one/outputs/turn-1/large.txt",
        now=10**10,
        min_age=0,
    )
    assert receipt["status"] == "retained" and not github.commits
    with pytest.raises(ValueError):
        await artifacts.move_output(
            storage,
            backend,
            "p_test",
            "t_one",
            "projects/p_test/runs/test/turn-1/outputs/gcs/user.txt",
            "threads/t_one/outputs/turn-1/user.txt",
            now=10**10,
            min_age=0,
        )
    backend.settings.thread_outputs_kb = 1
    storage.put(source, b"x" * 800)
    await artifacts.move_output(
        storage,
        backend,
        "p_test",
        "t_one",
        source,
        "threads/t_one/outputs/turn-1/first.txt",
        now=10**10,
        min_age=0,
    )
    receipt = await artifacts.move_output(
        storage,
        backend,
        "p_test",
        "t_one",
        source,
        "threads/t_one/outputs/turn-1/second.txt",
        now=10**10,
        min_age=0,
    )
    assert receipt["status"] == "retained" and receipt["reason"] == "thread-capacity"


async def test_import_preview_idempotency_coverage_and_changed_source(stores, monkeypatch):
    storage, backend, github = stores
    pid = seed_project(storage)
    checkpoint = "projects/_threads/t_one/checkpoint.json"
    storage.put(checkpoint, json.dumps({"context": {"project_slug": pid}, "messages": []}).encode())
    source = f"projects/_threads/t_one/history/{pid}/old_event.json"
    storage.put(
        source,
        json.dumps(
            {
                "id": "old_event",
                "user": "A private verbatim question",
                "assistant": "A private verbatim answer",
                "success": True,
            }
        ).encode(),
    )
    editor = AsyncMock(
        return_value={
            "_thread": thread_memory.Prose(
                paraphrase="Discussed an earlier analysis.", summary="Reviewed the study."
            ).model_dump_json()
        }
    )
    monkeypatch.setattr(memory_tasks, "edit", editor)
    preview = await thread_import.import_threads(storage, backend, apply=False)
    assert preview[0]["events"] == 1 and not github.commits
    assert not (await artifacts.coverage(storage, backend))["complete"]
    imported = await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    assert imported[0]["status"] == "imported"
    assert (await artifacts.coverage(storage, backend))["complete"]
    commits = len(github.commits)
    await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    assert len(github.commits) == commits and editor.await_count == 1
    assert all("verbatim" not in str(v) for v in github.trees[github.head].values())
    original = storage.get(source)[0]
    storage.put(source, original + b" ")
    assert not (await artifacts.coverage(storage, backend))["complete"]
    with pytest.raises(ValueError, match="source changed"):
        await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    storage.put(source, original)
    storage.put(f"projects/_threads/t_one/history/{pid}/new_event.json", b'{"id":"new_event"}')
    with pytest.raises(ValueError, match="requires"):
        await artifacts.require_coverage(storage, backend)


async def test_orphaned_scoped_history_is_discovered_and_imported(stores, monkeypatch):
    storage, backend, _ = stores
    pid = seed_project(storage)
    storage.put(f"projects/_threads/t_old/history/{pid}/old.json", b'{"id":"old","user":"Hi"}')
    monkeypatch.setattr(
        memory_tasks,
        "edit",
        AsyncMock(
            return_value={
                "_thread": thread_memory.Prose(
                    paraphrase="Introduced the project."
                ).model_dump_json()
            }
        ),
    )
    assert not (await artifacts.coverage(storage, backend))["complete"]
    await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    assert (await artifacts.coverage(storage, backend))["complete"]


async def test_terminal_tail_does_not_hide_running_or_recovering_predecessor():
    from types import SimpleNamespace

    repo = SimpleNamespace(get_connector_state=AsyncMock(return_value={"status": "running"}))
    bridge = SimpleNamespace(repository=repo)
    assert not await artifacts.inactive(bridge, {"status": "complete", "previous": "old"})
    assert not await artifacts.inactive(bridge, {"status": "failed", "job": "batch-job"})
    assert await artifacts.inactive(
        bridge, {"status": "failed", "job": "batch-job", "worker_stopped": True}
    )


async def test_archive_requires_every_script_and_output_copy(stores):
    import io
    import tarfile

    from test_thread_memory import update

    storage, backend, _ = stores
    source = "projects/_threads/t_one/archives/event.tar.gz"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, raw in {
            "work/thread/code/run.py": b"print(7)",
            "work/thread/outputs/plot.png": b"\x89PNG\xff",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    storage.put(source, stream.getvalue())
    receipt = await artifacts.verify_archive(storage, backend, "p_test", "t_one", source, now=1)
    assert receipt["status"] == "retained" and len(receipt["rejected"]) == 2
    base = await backend.read("p_test")
    await backend.commit(
        "p_test",
        base,
        {},
        source="worker",
        event_id="event_1",
        thread_update=update(scripts={"run.py": "print(7)"}),
        blobs={"threads/t_one/outputs/turn-1/plot.png": b"\x89PNG\xff"},
    )
    receipt = await artifacts.verify_archive(storage, backend, "p_test", "t_one", source, now=2)
    assert receipt["status"] == "verified" and not receipt["rejected"]
