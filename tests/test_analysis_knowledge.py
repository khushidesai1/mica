"""Project knowledge is bounded, conflict-safe, and independent of thread archives."""

import json
import tarfile

import pytest

from pistachio.agents.analysis import knowledge
from pistachio.agents.analysis.batch_worker import snapshot
from pistachio.agents.analysis.checkpoints import workspace_snapshot
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import Conflict, LocalStorage

NOTE = (
    b"type: fact\nsource: inspect_data.py, 2026-10-09, thread A\n"
    b"status: current\nTMA26 failed Proseg.\n"
)


@pytest.fixture
def setup(tmp_path):
    return (
        LocalStorage(tmp_path / "store"),
        tmp_path / "work",
        Settings(_env_file=None),
    )


def write(work, path, data=NOTE):
    target = work / "knowledge" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_conditional_delete(setup):
    store, _, _ = setup
    generation = store.put_if("a", b"old", None)
    with pytest.raises(Conflict):
        store.delete_if("a", "wrong")
    store.delete_if("a", generation)
    with pytest.raises(FileNotFoundError):
        store.get("a")


def test_save_restore_history_and_idempotent_retry(setup):
    store, work, settings = setup
    base = knowledge.download(store, "liver", work, settings)
    write(work, "data/samples.md")
    write(
        work,
        "INDEX.md",
        "- [Samples](data/samples.md) — fact · 2026-10-09 · Proseg status\n".encode(),
    )
    saved = knowledge.save(store, "liver", work, base, settings, event_id="event1", user_key="user")
    assert set(saved.added) == {"INDEX.md", "data/samples.md"}
    assert (
        knowledge.save(store, "liver", work, base, settings, event_id="event1", user_key="user")
        == saved
    )
    write(work, "data/stale.md")
    base = knowledge.download(store, "liver", work, settings)
    assert not (work / "knowledge/data/stale.md").exists()
    write(work, "data/samples.md", NOTE.replace(b"TMA26", b"TMA19"))
    (work / "knowledge/INDEX.md").unlink()
    saved = knowledge.save(store, "liver", work, base, settings, event_id="event2", user_key="user")
    assert saved.changed == ["data/samples.md"] and saved.removed == ["INDEX.md"]
    changes = json.loads(store.get(saved.changes_key)[0])
    assert changes["user_key"] == "user" and changes["attempt"] == 0
    history = saved.changes_key.removesuffix("CHANGES.json")
    assert store.get(history + "before/data/samples.md")[0] == NOTE
    assert store.get(history + "after/data/samples.md")[0] == NOTE.replace(b"TMA26", b"TMA19")


def test_concurrent_update_preserves_both_versions(setup):
    store, work, settings = setup
    store.put("projects/liver/knowledge/data/samples.md", NOTE)
    base = knowledge.download(store, "liver", work, settings)
    store.put("projects/liver/knowledge/data/samples.md", NOTE + b"Another thread.\n")
    write(work, "data/samples.md", NOTE + b"This thread.\n")
    saved = knowledge.save(store, "liver", work, base, settings, event_id="conflict", user_key="u")
    assert saved.conflicts == ["data/samples.md"]
    assert store.get("projects/liver/knowledge/data/samples.md")[0].endswith(b"Another thread.\n")
    keys = store.keys("projects/liver/knowledge/_unmerged/")
    assert any(store.get(key)[0].endswith(b"This thread.\n") for key in keys)
    assert b"Unmerged" in store.get("projects/liver/knowledge/INDEX.md")[0]


@pytest.mark.parametrize(
    "path,data",
    [
        ("../escape.md", NOTE),
        ("data/a.txt", NOTE),
        ("data/nested/a.md", NOTE),
        ("data/a.md", b"no provenance"),
        ("data/a.md", b"\xff"),
        ("data/a.md", NOTE + b"password=private"),
        ("data/a.md", NOTE + b"x" * 17000),
        ("recipes/a.py", b"x" * 65537),
        ("INDEX.md", b"x" * 4001),
    ],
)
def test_invalid_content(path, data, setup):
    _, _, settings = setup
    with pytest.raises(ValueError):
        knowledge.validate(path, data, settings)


def test_symlinks_and_limits_rejected_without_deleting_previous(setup, tmp_path):
    store, work, settings = setup
    store.put("projects/liver/knowledge/data/samples.md", NOTE)
    base = knowledge.download(store, "liver", work, settings)
    path = work / "knowledge/data/samples.md"
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(NOTE)
    path.symlink_to(outside)
    saved = knowledge.save(store, "liver", work, base, settings, event_id="links", user_key="u")
    assert "data/samples.md" in saved.rejected
    assert store.get("projects/liver/knowledge/data/samples.md")[0] == NOTE
    path.unlink()
    write(work, "data/samples.md")
    write(work, "data/extra.md")
    settings.knowledge_max_files = 1
    saved = knowledge.save(store, "liver", work, base, settings, event_id="limit", user_key="u")
    assert "data/extra.md" in saved.rejected


@pytest.mark.parametrize("partial", [None, False, True])
def test_knowledge_always_excluded_from_archives(tmp_path, partial):
    home = tmp_path / "home"
    work = home / "work/project/thread"
    write(work, "data/samples.md")
    archive = tmp_path / "a.tar.gz"

    def capture():
        if partial is None:
            snapshot(archive, home)
        else:
            workspace_snapshot(archive, home, partial=partial, max_bytes=10000)
        with tarfile.open(archive) as tar:
            return tar.getnames()

    assert not capture()


def test_prunes_only_old_history(setup):
    store, work, settings = setup
    for number in range(102):
        base = knowledge.download(store, "liver", work, settings)
        write(work, "data/samples.md", NOTE + str(number).encode())
        knowledge.save(store, "liver", work, base, settings, event_id=f"e{number}", user_key="u")
    assert (
        len(
            [
                k
                for k in store.keys("projects/liver/knowledge-history/")
                if k.endswith("CHANGES.json")
            ]
        )
        == 100
    )


def test_interrupted_save_is_recovered_before_next_attempt(setup, monkeypatch):
    store, work, settings = setup
    write(work, "data/samples.md")
    original = store.put
    failed = False

    def lose_ack(key, data):
        nonlocal failed
        if key.endswith("CHANGES.json") and json.loads(data)["changes"][0]["done"] and not failed:
            failed = True
            raise OSError("lost journal acknowledgement")
        return original(key, data)

    monkeypatch.setattr(store, "put", lose_ack)
    with pytest.raises(OSError):
        knowledge.save(store, "liver", work, {}, settings, event_id="interrupted", user_key="u")
    assert store.get("projects/liver/knowledge/data/samples.md")[0] == NOTE
    # A fresh attempt does not need the old workspace to finish the trusted save.
    base = knowledge.download(store, "liver", work, settings)
    assert base["data/samples.md"].data == NOTE
    journals = [json.loads(store.get(k)[0]) for k in knowledge.history_keys(store, "liver")]
    assert len(journals) == 1 and journals[0]["status"] == "complete"
    assert journals[0]["summary"]["added"] == ["data/samples.md"]


def test_total_budget_and_symlink_directory_cannot_delete_base(setup, tmp_path):
    store, work, settings = setup
    store.put("projects/liver/knowledge/data/samples.md", NOTE)
    base = knowledge.download(store, "liver", work, settings)
    (work / "knowledge/data/samples.md").unlink()
    (work / "knowledge/data").rmdir()
    (work / "knowledge/data").symlink_to(tmp_path, target_is_directory=True)
    saved = knowledge.save(store, "liver", work, base, settings, event_id="dir", user_key="u")
    assert "data" in saved.rejected and not saved.removed
    (work / "knowledge/data").unlink()
    write(work, "recipes/a.py", b"x" * 1100)
    settings.knowledge_total_kb = 1
    saved = knowledge.save(store, "liver", work, base, settings, event_id="total", user_key="u")
    assert "recipes/a.py" in saved.rejected


def test_cancel_marker_and_pending_history_retention(setup):
    store, _, _ = setup
    assert not knowledge.cancelled(store, "event")
    store.put("tasks/slack-analysis/events/event/knowledge-cancelled.json", b"true")
    assert knowledge.cancelled(store, "event")


def test_conflict_at_actual_write_is_saved_unmerged(setup, monkeypatch):
    store, work, settings = setup
    target = knowledge.prefix("liver") + "data/samples.md"
    store.put(target, NOTE)
    base = knowledge.download(store, "liver", work, settings)
    write(work, "data/samples.md", NOTE + b" our update")
    put_if = store.put_if
    raced = False

    def concurrent_write(key, data, generation):
        nonlocal raced
        if key == target and not raced:
            raced = True
            store.put(key, NOTE + b" other update")
        return put_if(key, data, generation)

    monkeypatch.setattr(store, "put_if", concurrent_write)
    saved = knowledge.save(store, "liver", work, base, settings, event_id="race", user_key="u")
    assert saved.conflicts == ["data/samples.md"]
    assert store.get(target)[0] == NOTE + b" other update"
    unmerged = store.keys(knowledge.prefix("liver") + "_unmerged/")
    assert len(unmerged) == 1 and store.get(unmerged[0])[0] == NOTE + b" our update"
    assert b"Unmerged" in store.get(knowledge.prefix("liver") + "INDEX.md")[0]


def test_conflict_index_cannot_exceed_project_file_limit(setup):
    store, work, settings = setup
    target = knowledge.prefix("liver") + "data/samples.md"
    store.put(target, NOTE)
    base = knowledge.download(store, "liver", work, settings)
    store.put(target, NOTE + b" other update")
    write(work, "data/samples.md", NOTE + b" our update")
    settings.knowledge_max_files = 2
    saved = knowledge.save(store, "liver", work, base, settings, event_id="race", user_key="u")
    assert "INDEX.md" in saved.rejected
    assert len(knowledge.load(store, "liver", settings)) == 2


def test_simultaneous_saves_preserve_both_versions(setup):
    from concurrent.futures import ThreadPoolExecutor

    store, work, settings = setup
    target = knowledge.prefix("liver") + "data/samples.md"
    store.put(target, NOTE)
    other = work.parent / "other-work"
    first = knowledge.download(store, "liver", work, settings)
    second = knowledge.download(store, "liver", other, settings)
    write(work, "data/samples.md", NOTE + b" first")
    write(other, "data/samples.md", NOTE + b" second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(
            knowledge.save, store, "liver", work, first, settings, event_id="a", user_key="u"
        )
        b = pool.submit(
            knowledge.save, store, "liver", other, second, settings, event_id="b", user_key="u"
        )
        results = [a.result(), b.result()]
    assert sum(bool(result.conflicts) for result in results) == 1
    contents = [
        store.get(key)[0]
        for key in store.keys(knowledge.prefix("liver"))
        if key.endswith("samples.md")
    ]
    assert set(contents) == {NOTE + b" first", NOTE + b" second"}


def test_full_project_can_replace_an_entry(setup):
    store, work, settings = setup
    store.put(knowledge.prefix("liver") + "data/z.md", NOTE)
    base = knowledge.download(store, "liver", work, settings)
    (work / "knowledge/data/z.md").unlink()
    write(work, "data/a.md")
    settings.knowledge_max_files = 1
    result = knowledge.save(store, "liver", work, base, settings, event_id="replace", user_key="u")
    assert result.added == ["data/a.md"] and result.removed == ["data/z.md"]
    assert not result.rejected
