import io
import json
import sqlite3
import tarfile

import pytest

from mica.agents.analysis.batch import BatchBridge
from mica.agents.analysis.checkpoints import (
    cleanup_temporary,
    migrate_thread,
    restore_workspace,
    workspace_snapshot,
)
from mica.agents.analysis.config import Settings
from mica.agents.analysis.direct import record_exchange
from mica.agents.analysis.outputs import publish, select_artifacts
from mica.agents.analysis.recovery_tools import restored_cost
from mica.agents.analysis.runner import Context, TurnResult, build_options
from mica.agents.analysis.script_capture import Capture, Script
from mica.agents.analysis.storage import Conflict, LocalStorage
from mica.agents.analysis.thread_history import History, context


def file(root, name, data=b"content"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_both_archival_paths_reject_datasets(tmp_path):
    home = tmp_path / "home"
    work = home / "work/thread"
    script = file(work, "code/analysis.py", b"print('reusable')")
    figure = file(work, "outputs/figure.png")
    datasets = [
        file(work, p)
        for p in (
            "outputs/gcs/store/a.txt",
            "outputs/raw.parquet",
            "outputs/store.ome.zarr/0/0",
            "code/large.h5ad",
            "checkpoints/raw/data.txt",
            "scratch/cache.csv",
        )
    ]
    with datasets[1].open("wb") as stream:
        stream.truncate(3 * 1024**3)
    archive = tmp_path / "small.tar.gz"
    report = workspace_snapshot(archive, home, partial=False, recovery=False)
    assert report["excluded_bytes"] >= 3 * 1024**3 and archive.stat().st_size < 10000
    store = LocalStorage(tmp_path / "store")
    result = TurnResult(files=[figure, *datasets])
    uploaded = publish(store, Context(work, tmp_path / "data"), result, 1, "u", "p", "m")
    assert set(uploaded) == {script, figure}
    restored = tmp_path / "restored"
    with archive.open("rb") as stream:
        restore_workspace(stream, restored)
    assert (restored / "work/thread/code/analysis.py").read_bytes() == script.read_bytes()
    assert not (restored / "work/thread/outputs/raw.parquet").exists()


def test_optional_budget_never_drops_code_notes_or_recovery_scripts(tmp_path):
    paths = [
        file(tmp_path, p, b"x" * 100)
        for p in (
            "code/a.py",
            "notes/methods.md",
            ".mica/scripts/code/a.py",
            "outputs/figure.png",
            "logs/large.log",
        )
    ]
    kept, report = select_artifacts(paths, tmp_path, budget=1, recovery=True)
    assert set(kept) == set(paths[:3])
    assert report["excluded_paths"] == 2


def test_same_event_recovery_preserves_executed_script_and_cost(tmp_path):
    home = tmp_path / "home"
    work = home / "work/thread"
    script = file(work, "code/a.py", b"print('executed')")
    capture = Capture(work)
    capture.scripts["code/a.py"] = Script("code/a.py", script.read_bytes(), 1.0)
    manifest = capture.checkpoint()
    script.write_text("print('edited but not run')")
    sid = "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12
    session = {"type": "cost-state", "sessionId": sid, "totalCostUSD": 1.25}
    file(home, f".claude/projects/thread/{sid}.jsonl", json.dumps(session).encode() + b"\n{partial")
    file(home, ".claude/projects/thread/old.jsonl", b"old\n")
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE test (id INTEGER)")
        db.execute("INSERT INTO test VALUES (42)")
    archive = tmp_path / "partial.tar.gz"
    workspace_snapshot(archive, home, partial=True, session_id=sid, max_bytes=1)
    recovered = tmp_path / "recovered"
    with archive.open("rb") as stream:
        restore_workspace(stream, recovered)
    restored = Capture(recovered / "work/thread", manifest)
    assert restored.scripts["code/a.py"].content == b"print('executed')"
    assert restored_cost(sid, recovered) == 1.25
    assert not (recovered / ".claude/projects/thread/old.jsonl").exists()
    with sqlite3.connect(recovered / "state.db") as db:
        assert db.execute("SELECT id FROM test").fetchone() == (42,)


def test_durable_history_survives_prompt_window_and_failed_turn(tmp_path):
    store = LocalStorage(tmp_path)
    history = History(store, "thread")
    state = {}
    record_exchange(
        state,
        "first",
        "Use radius 17; reuse code/ingest.py",
        "Threshold 5 failed; use 9.",
        methods="Do not repeat threshold 5.",
        history=history,
    )
    for i in range(20):
        record_exchange(state, f"next-{i}", "request " * 1000, "reply " * 1000, history=history)
    record_exchange(
        state,
        "failed",
        "Continue the original method",
        "Stopped by the time allowance",
        success=False,
        history=history,
    )
    assert len(state["messages"]) == 4 and len(context(state["messages"])) <= 20000
    found = history.recall("radius")
    assert "radius 17" in json.dumps(found) and "threshold 5" in json.dumps(found)
    assert "code/ingest.py" in history.recall(event_id="first")["text"]
    assert "failed or interrupted" in state["thread_summary"]["text"]
    assert len(state["thread_summary"]["text"]) <= 8000
    assert (
        len(json.loads(store.get("projects/_threads/thread/history/next-0.json")[0])["user"]) > 6000
    )
    with pytest.raises(ValueError):
        history.recall(event_id="../../other")


def legacy(store, tmp_path):
    home = tmp_path / "old"
    file(home, "work/thread/code/analysis.py", b"print('old decision')")
    file(home, "work/thread/notes/methods.md", b"Avoid failed method")
    file(home, "work/thread/outputs/raw.parquet", b"dataset")
    archive = tmp_path / "legacy.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(home / "work", arcname="work")
    store.upload("tasks/legacy.tar.gz", archive)
    state = {
        "archive": "tasks/legacy.tar.gz",
        "context": {"workdir": "/srv/pistachio/work/thread", "session_id": "old"},
        "messages": [],
    }
    store.put("projects/_threads/thread/checkpoint.json", json.dumps(state).encode())


def test_migration_verifies_keeps_source_and_changes_pointer(tmp_path):
    store = LocalStorage(tmp_path / "storage")
    legacy(store, tmp_path)
    result = migrate_thread(store, "thread")
    assert store.get(result["source_retained"])[0]
    state = json.loads(store.get("projects/_threads/thread/checkpoint.json")[0])
    assert state["archive"].startswith("projects/_threads/thread/archives/")
    assert state["context"]["session_id"] is None
    with tarfile.open(fileobj=io.BytesIO(store.get(state["archive"])[0])) as archive:
        assert "work/thread/code/analysis.py" in archive.getnames()
        assert "work/thread/notes/methods.md" in archive.getnames()
        assert "work/thread/outputs/raw.parquet" not in archive.getnames()


def test_migration_racing_next_turn_preserves_new_pointer(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path / "storage")
    legacy(store, tmp_path)
    original = store.upload_new

    def upload(key, path):
        original(key, path)
        store.put("projects/_threads/thread/checkpoint.json", b'{"new_turn": true}')

    monkeypatch.setattr(store, "upload_new", upload)
    with pytest.raises(Conflict):
        migrate_thread(store, "thread")
    assert json.loads(store.get("projects/_threads/thread/checkpoint.json")[0]) == {
        "new_turn": True
    }
    assert store.get("tasks/legacy.tar.gz")[0]


@pytest.mark.parametrize(
    "status,stopped,version,clean",
    [
        ("pending", True, 1, False),
        ("running", False, 1, False),
        ("failed", False, 1, False),
        ("complete", True, 0, False),
        ("complete", True, 1, True),
        ("failed", True, 1, True),
    ],
)
async def test_cleanup_requires_logical_terminal_worker_exit_and_new_version(
    tmp_path, status, stopped, version, clean
):
    from types import SimpleNamespace

    store = LocalStorage(tmp_path)
    event = "a" * 32
    prefix = f"tasks/slack-analysis/events/{event}"
    for key in [
        f"{prefix}/stages/a.tar.gz",
        f"{prefix}/write-exports/a",
        f"{prefix}/checkpoint.tar.gz",
        f"{prefix}/write-manifests/a",
        "projects/_threads/thread/history/a.json",
    ]:
        store.put(key, b"keep")
    bridge = SimpleNamespace(storage=store)
    record = {"status": status, "worker_stopped": stopped, "retention_version": version}
    await BatchBridge.cleanup_terminal(bridge, event, record)
    assert bool(record.get("temporary_cleaned")) == clean
    assert bool(store.keys(f"{prefix}/stages/")) != clean
    assert store.get(f"{prefix}/checkpoint.tar.gz")[0]
    assert store.get(f"{prefix}/write-manifests/a")[0]
    assert store.get("projects/_threads/thread/history/a.json")[0]
    if clean:
        assert cleanup_temporary(store, event) == 0


async def test_cleanup_retry_does_not_mark_partial_deletion_done(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mica.agents.analysis import checkpoints

    store = LocalStorage(tmp_path)
    record = {"status": "complete", "worker_stopped": True, "retention_version": 1}
    monkeypatch.setattr(
        checkpoints, "cleanup_temporary", lambda *args: (_ for _ in ()).throw(OSError("retry"))
    )
    with pytest.raises(OSError):
        await BatchBridge.cleanup_terminal(SimpleNamespace(storage=store), "a" * 32, record)
    assert "temporary_cleaned" not in record


def test_worker_options_keep_cache_and_output_controls_out_of_other_calls(tmp_path):
    settings = Settings(_env_file=None)
    ctx = Context(tmp_path / "work", tmp_path / "data")
    plain = build_options(ctx, settings)
    worker = build_options(
        ctx, settings, history=History(LocalStorage(tmp_path / "store"), "thread")
    )
    assert "CLAUDE_CODE_PROMPT_CACHE_TTL" not in plain.env
    assert worker.env["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"
    assert json.loads(worker.settings)["bashOutputMaxChars"] == 12000
    assert "mcp__pistachio__recall_thread" in worker.allowed_tools


def test_history_does_not_cross_project_binding(tmp_path):
    store = LocalStorage(tmp_path)
    previous = History(store, "thread", "old-project")
    previous.record({"id": "event", "user": "private decision", "assistant": "saved"})
    current = History(store, "thread", "new-project")
    assert current.recall("private")["matches"] == []
    with pytest.raises(FileNotFoundError):
        current.recall(event_id="event")


def test_confirmed_publication_locations_enter_memory_proposal():
    from mica.agents.analysis import memory_workspace, project_memory

    settings = Settings(_env_file=None)
    base = project_memory.Snapshot(head_oid="a" * 40)
    raw = memory_workspace.Proposal(base_head=base.head_oid).model_dump_json().encode()
    receipt = "Saved to GCS: `gs://data/approved/result/` — 2 files, 20 bytes."
    updated = memory_workspace.publication_proposal(raw, base, "b" * 32, receipt, settings)
    proposal = memory_workspace.decode_pending(updated, settings)
    result = proposal.changes["results/published-" + "b" * 32 + ".md"]
    assert receipt in result and "not scientific validation" in result
    assert "INDEX.md" not in proposal.changes  # The store generates the inventory atomically.
    assert memory_workspace.publication_proposal(raw, base, "b" * 32, "", settings) == raw
