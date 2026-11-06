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


def archive_bytes(members):
    import io
    import tarfile

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, raw in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return data.getvalue()


def editor_prose():
    return {
        "_thread": thread_memory.Prose(
            paraphrase="Reviewed the study.", summary="Study reviewed."
        ).model_dump_json()
    }


async def test_large_archive_never_opens_and_does_not_block_conversation_import(
    stores, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import Mock

    storage, backend, _ = stores
    pid = seed_project(storage)
    source = "projects/_threads/t_one/archives/large.tar.gz"
    storage.put(source, b"placeholder")
    storage.put(
        "projects/_threads/t_one/checkpoint.json",
        json.dumps(
            {
                "context": {"project_slug": pid, "workdir": "/srv/pistachio/work/thread"},
                "archive": source,
                "messages": [{"id": "old", "user": "Earlier request"}],
            }
        ).encode(),
    )
    stat = artifacts.stat
    monkeypatch.setattr(
        artifacts,
        "stat",
        lambda s, k: replace(stat(s, k), size=109_000_000_000) if k == source else stat(s, k),
    )
    opened = Mock(side_effect=AssertionError("Large archive must not be downloaded"))
    monkeypatch.setattr(storage, "open_read", opened)
    monkeypatch.setattr(memory_tasks, "edit", AsyncMock(return_value=editor_prose()))
    preview = await thread_import.import_threads(storage, backend, apply=False)
    assert preview[0]["events"] == preview[0]["checkpoint_only_events"] == 1
    result = await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    assert result[0]["status"] == "imported"
    assert "bounded scan size" in result[0]["archive"]["rejected"]["archive"]
    assert (await artifacts.coverage(storage, backend, strict_checkpoint=True))["complete"]
    assert (await artifacts.verify_archive(storage, backend, pid, "t_one", source, now=1))[
        "status"
    ] == "retained"
    opened.assert_not_called()
    assert storage.get(source)[0] == b"placeholder"


async def test_script_only_archive_scan_stops_before_expansion_and_retains_source(stores):
    storage, backend, _ = stores
    backend.settings.thread_archive_expanded_mb = 1
    source = "projects/_threads/t_one/archives/bomb.tar.gz"
    storage.put(
        source,
        archive_bytes(
            {
                "work/thread/code/run.py": b"print(7)\n",
                "work/thread/outputs/large.txt": b"0" * (2 * 1024**2),
            }
        ),
    )
    info = artifacts.stat(storage, source)
    scripts, rejected = thread_import.read_scripts(
        storage,
        info,
        {"context": {"workdir": "/srv/pistachio/work/thread"}},
        await backend.read("p_test"),
        "t_one",
        backend.settings,
    )
    assert scripts == {"run.py": "print(7)\n"}
    assert "expansion" in rejected["archive"]
    assert storage.get(source)


async def test_archive_script_validation_and_retry_are_idempotent(stores):
    storage, backend, github = stores
    source = "projects/_threads/t_one/archives/old.tar.gz"
    storage.put(
        source,
        archive_bytes(
            {
                "work/thread/code/good.py": b"print(7)",
                "work/thread/code/bad.py": b"password = 'unsafe'",
                "work/thread/outputs/ignore.txt": b"not a script",
            }
        ),
    )
    state = {"archive": source, "context": {"workdir": "/srv/pistachio/work/thread"}}
    receipt = await thread_import.import_scripts(
        storage, backend, "p_test", "t_one", state, await backend.read("p_test")
    )
    assert receipt["accepted"] == ["good.py"] and receipt["rejected"]
    count = len(github.commits)
    await thread_import.import_scripts(
        storage, backend, "p_test", "t_one", state, await backend.read("p_test")
    )
    assert len(github.commits) == count
    assert (await backend.read_bytes("p_test", "threads/t_one/scripts/good.py"))[0] == b"print(7)"


async def test_native_github_only_coverage_requires_verified_receipts(stores):
    from test_thread_memory import update

    storage, backend, _ = stores
    item = update()
    await backend.commit(
        "p_test",
        await backend.read("p_test"),
        {},
        source="worker",
        event_id=item.entry.event_id,
        thread_update=item,
    )
    storage.put(
        "projects/_threads/t_one/checkpoint.json",
        json.dumps(
            {
                "context": {"project_slug": "p_test"},
                "thread_memory_origin": "github_only",
                "messages": [{"id": item.entry.event_id}],
            }
        ).encode(),
    )
    assert not (await artifacts.coverage(storage, backend))["complete"]
    receipt = await memory_tasks.verify(backend, "p_test", item)
    storage.put(
        f"projects/_threads/t_one/pending/{item.entry.event_id}/receipt.json",
        json.dumps(receipt).encode(),
    )
    assert (await artifacts.coverage(storage, backend))["complete"]
    storage.put("projects/_threads/t_one/history/p_test/missing.json", b'{"id":"missing"}')
    assert not (await artifacts.coverage(storage, backend))["complete"]


async def test_cutover_checks_fresh_checkpoint_and_active_retry_state(stores, monkeypatch):
    from types import SimpleNamespace

    from mica.storage.memory import InMemoryRepository

    storage, backend, _ = stores
    pid = seed_project(storage)
    key = "projects/_threads/t_one/checkpoint.json"
    storage.put(key, json.dumps({"context": {"project_slug": pid}, "messages": []}).encode())
    await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    storage.put(
        key, json.dumps({"context": {"project_slug": pid}, "messages": [], "turn": 1}).encode()
    )
    assert not (await artifacts.coverage(storage, backend, strict_checkpoint=True))["complete"]
    await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    repo = InMemoryRepository()
    bridge = SimpleNamespace(settings=backend.settings, storage=storage, repository=repo)
    monkeypatch.setattr(artifacts.project_memory, "GitHubMemory", lambda _: backend)
    report = await artifacts.cutover_report(bridge)
    assert report["complete"]
    with pytest.raises(ValueError, match="cutover receipt"):
        artifacts.validate_cutover(backend.settings, report)
    backend.settings.thread_memory_cutover_at = report["checked_at"]
    artifacts.validate_cutover(backend.settings, report)
    await repo.set_connector_state("claude-event:pending", {"memory_due": 1})
    assert not (await artifacts.cutover_report(bridge))["complete"]


async def test_unanswered_legacy_message_without_id_is_imported_as_pending(stores, monkeypatch):
    storage, backend, _ = stores
    pid = seed_project(storage)
    storage.put(
        "projects/_threads/t_one/checkpoint.json",
        json.dumps(
            {"context": {"project_slug": pid}, "messages": [{"user": "Please review the study"}]}
        ).encode(),
    )
    editor = AsyncMock(return_value=editor_prose())
    monkeypatch.setattr(memory_tasks, "edit", editor)
    await thread_import.import_threads(storage, backend, apply=True, model_key="test")
    assert editor.call_args.args[2]["success"] is False
    rows = thread_memory.log_entries(
        {p: f.text for p, f in (await backend.read(pid)).files.items()}, "t_one"
    )
    assert len(rows) == 1 and not rows[0].success
    assert (await artifacts.coverage(storage, backend, strict_checkpoint=True))["complete"]


@pytest.fixture
def sweep_context(stores, monkeypatch):
    from types import SimpleNamespace

    from mica.storage.memory import InMemoryRepository

    storage, backend, _ = stores
    repo = InMemoryRepository()
    bridge = SimpleNamespace(
        storage=storage, repository=repo,
        settings=Settings(memory_repo="owner/memory", thread_memory="github_only", _env_file=None),
    )
    for thread in ("t_one", "t_two", "t_three"):
        storage.put(
            f"projects/_threads/{thread}/checkpoint.json",
            json.dumps({"context": {"project_slug": "p_test"}}).encode(),
        )
        storage.put(
            artifacts.receipt_key(thread, "p_test"),
            json.dumps({"conversation_verified": True, "run_roots": [f"runs/{thread}"]}).encode(),
        )
    storage.put("runs/t_one/turn-1/outputs/plot.png", b"retained original")
    monkeypatch.setattr(artifacts.project_memory, "GitHubMemory", lambda settings: backend)
    monkeypatch.setattr(artifacts, "coverage", AsyncMock(return_value={
        "verified": [f"p_test/{t}" for t in ("t_one", "t_two", "t_three")],
    }))
    return bridge


async def test_sweep_times_out_slow_copy_without_losing_source_or_intake_lease(
    sweep_context, monkeypatch
):
    import asyncio

    bridge = sweep_context
    async def stalled_copy(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(artifacts, "move_output", stalled_copy)
    monkeypatch.setattr(artifacts, "THREAD_SWEEP_SECONDS", 0.02)
    monkeypatch.setattr(artifacts, "SWEEP_SECONDS", 2)
    result = await artifacts.sweep(bridge)
    assert result["memory_sweep_deferred"] == 1
    assert result["memory_sources_deleted"] == result["memory_outputs_moved"] == 0
    assert bridge.storage.get("runs/t_one/turn-1/outputs/plot.png")[0] == b"retained original"
    # Other threads still completed, and the timed-out thread no longer blocks intake.
    assert bridge.storage.get("projects/_threads/t_two/archive-receipts/p_test.json")
    lease = await bridge.repository.begin_event("claude-intake:t_one", lease_seconds=10)
    assert lease.status == "claimed"


async def test_sweep_total_budget_advances_only_visited_threads(sweep_context, monkeypatch):
    import asyncio

    bridge = sweep_context
    async def stalled_copy(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(artifacts, "move_output", stalled_copy)
    monkeypatch.setattr(artifacts, "SWEEP_SECONDS", 0.02)
    result = await artifacts.sweep(bridge)
    assert result["memory_sweep_deferred"] == 1
    assert await bridge.repository.get_connector_state("memory-artifact-cursor") == {"cursor": 1}
    # Next pass starts with the next thread, even though the first thread still needs work.
    visited = []
    begin = bridge.repository.begin_event
    async def record_visit(key, **kwargs):
        visited.append(key)
        return await begin(key, **kwargs)

    monkeypatch.setattr(bridge.repository, "begin_event", record_visit)
    monkeypatch.setattr(artifacts, "SWEEP_SECONDS", 2)
    monkeypatch.setattr(artifacts, "THREAD_SWEEP_SECONDS", 0.02)
    await artifacts.sweep(bridge)
    assert visited[0] == "claude-intake:t_three"


async def test_sweep_defers_all_mutation_when_coverage_times_out(sweep_context, monkeypatch):
    import asyncio

    bridge = sweep_context
    async def stalled_coverage(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(artifacts, "coverage", stalled_coverage)
    monkeypatch.setattr(artifacts, "SWEEP_SECONDS", 0.02)
    before = {key: bridge.storage.get(key)[0] for key in bridge.storage.keys("")}
    result = await artifacts.sweep(bridge)
    assert result["memory_sweep_deferred"] == 1
    assert result["memory_sources_deleted"] == 0
    assert {key: bridge.storage.get(key)[0] for key in bridge.storage.keys("")} == before
    assert await bridge.repository.get_connector_state("memory-artifact-cursor") is None
