import json
import sqlite3
import tarfile

import pytest

from pistachio.agents.analysis.checkpoints import Stages, workspace_snapshot
from pistachio.agents.analysis.storage import LocalStorage


def test_stage_is_immutable_and_restores_under_checkpoint_root(tmp_path):
    work = tmp_path / "work"
    stage = work / "checkpoints/ingest"
    stage.mkdir(parents=True)
    (stage / "data").write_text("completed")
    store = LocalStorage(tmp_path / "store")
    saved = Stages(store, work, "a" * 32, 0, max_bytes=1000)
    result = saved.capture({"path": "checkpoints/ingest", "fingerprint": "input-v1/method-v1"})
    (stage / "data").write_text("changed after capture")
    restored = Stages(store, tmp_path / "restored", "a" * 32, 1, max_bytes=1000)
    restored.restore(saved.manifest())
    assert (tmp_path / "restored/checkpoints/ingest/data").read_text() == "completed"
    assert result["status"] == "saved"
    with pytest.raises(ValueError):
        saved.capture({"path": "../store", "fingerprint": "escape"})


def test_partial_snapshot_has_consistent_db_and_complete_jsonl(tmp_path):
    home = tmp_path / "home"
    for folder in ("code", "scratch", "checkpoints", "inputs", "outputs"):
        path = home / "work/thread" / folder
        path.mkdir(parents=True)
        (path / "data").write_text("value")
    session = home / ".claude/projects/session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"ok":true}\n{"partial":')
    db = sqlite3.connect(home / "state.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE items (id INTEGER)")
    db.execute("INSERT INTO items VALUES (7)")
    db.commit()
    archive = tmp_path / "partial.tar.gz"
    workspace_snapshot(archive, home, partial=True, max_bytes=100000)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert "state.db" in names and "work/thread/code/data" in names
        assert not any(
            "/scratch/" in n or "/checkpoints/" in n or "/inputs/" in n or "/outputs/data" in n
            for n in names
        )
        assert json.loads(tar.extractfile(".claude/projects/session.jsonl").read()) == {"ok": True}
    db.close()


def test_stages_reject_foreign_objects_and_oversize(tmp_path):
    work = tmp_path / "work"
    stage = work / "checkpoints/big"
    stage.mkdir(parents=True)
    (stage / "data").write_bytes(b"x" * 20)
    stages = Stages(LocalStorage(tmp_path / "store"), work, "a" * 32, 0, max_bytes=10)
    with pytest.raises(ValueError):
        stages.capture({"path": "checkpoints/big", "fingerprint": "big"})
    with pytest.raises(ValueError):
        stages.restore({"stages": [{"archive": "foreign/file.tar.gz"}], "uploaded_bytes": 0})


def test_recovery_keeps_valid_stages_and_repeats_corrupt_ones(tmp_path):
    store = LocalStorage(tmp_path / "store")
    work = tmp_path / "work"
    saved = Stages(store, work, "a" * 32, 0, max_bytes=1000)
    for name in ("good", "bad"):
        stage = work / "checkpoints" / name
        stage.mkdir(parents=True)
        (stage / "data").write_text(name)
        saved.capture({"path": f"checkpoints/{name}", "fingerprint": "input-v1/method-v1"})
    store.put(saved.stages[1]["archive"], b"corrupt")
    resumed = Stages(store, tmp_path / "restored", "a" * 32, 1, max_bytes=1000)
    assert resumed.restore_available(saved.manifest()) == ["checkpoints/bad"]
    assert [s["path"] for s in resumed.stages] == ["checkpoints/good"]
    assert (resumed.work / "checkpoints/good/data").read_text() == "good"
    assert not (resumed.work / "checkpoints/bad").exists()
