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
    saved = Stages(store, work, "a" * 32, 0)
    result = saved.capture({"path": "checkpoints/ingest", "fingerprint": "input-v1/method-v1"})
    (stage / "data").write_text("changed after capture")
    restored = Stages(store, tmp_path / "restored", "a" * 32, 1)
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
        (path / "data.txt").write_text("value")
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
        assert "state.db" in names and "work/thread/code/data.txt" in names
        assert not any(
            "/scratch/" in n
            or "/checkpoints/" in n
            or "/inputs/" in n
            or "/outputs/data.parquet" in n
            for n in names
        )
        assert json.loads(tar.extractfile(".claude/projects/session.jsonl").read()) == {"ok": True}
    db.close()


def test_stages_reject_foreign_objects_without_size_caps(tmp_path):
    work = tmp_path / "work"
    stage = work / "checkpoints/big"
    stage.mkdir(parents=True)
    (stage / "data").write_bytes(b"x" * 20)
    stages = Stages(LocalStorage(tmp_path / "store"), work, "a" * 32, 0)
    assert stages.capture({"path": "checkpoints/big", "fingerprint": "big"})["bytes"] == 20
    with pytest.raises(ValueError):
        stages.restore({"stages": [{"archive": "foreign/file.tar.gz"}], "uploaded_bytes": 0})


def test_recovery_keeps_valid_stages_and_repeats_corrupt_ones(tmp_path):
    store = LocalStorage(tmp_path / "store")
    work = tmp_path / "work"
    saved = Stages(store, work, "a" * 32, 0)
    for name in ("good", "bad"):
        stage = work / "checkpoints" / name
        stage.mkdir(parents=True)
        (stage / "data").write_text(name)
        saved.capture({"path": f"checkpoints/{name}", "fingerprint": "input-v1/method-v1"})
    store.put(saved.stages[1]["archive"], b"corrupt")
    resumed = Stages(store, tmp_path / "restored", "a" * 32, 1)
    assert resumed.restore_available(saved.manifest()) == ["checkpoints/bad"]
    assert [s["path"] for s in resumed.stages] == ["checkpoints/good"]
    assert (resumed.work / "checkpoints/good/data").read_text() == "good"
    assert not (resumed.work / "checkpoints/bad").exists()


def test_full_snapshot_excludes_all_datasets_and_keeps_references(tmp_path):
    from pistachio.agents.analysis.batch_worker import restore

    home = tmp_path / "home"
    root = home / "work/thread"
    published = root / "outputs/gcs/results/large.parquet"
    pending = published.with_name("pending.parquet")
    published.parent.mkdir(parents=True)
    published.write_bytes(b"published")
    pending.write_bytes(b"not yet published")
    notes = root / "logs/publication.json"
    notes.parent.mkdir()
    notes.write_text('{"destination":"gs://data/results/large.parquet"}')
    archive = tmp_path / "full.tar.gz"
    workspace_snapshot(archive, home, partial=False, published={published})
    recovered = tmp_path / "recovered"
    recovered.mkdir()
    restore(archive, recovered)
    assert not (recovered / published.relative_to(home)).exists()
    assert not (recovered / pending.relative_to(home)).exists()
    assert (recovered / notes.relative_to(home)).read_bytes() == notes.read_bytes()
