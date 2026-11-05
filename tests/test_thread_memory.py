from __future__ import annotations

import json
from pathlib import Path

import pytest

from mica.agents.analysis import memory_workspace, project_memory
from mica.agents.analysis import thread_memory as tm
from mica.agents.analysis.config import Settings
from tests.fake_github import FakeGitHub


def update(seq=1, thread="t_one", covered=(), scripts=None):
    prose = tm.Prose(paraphrase=f"Completed analysis {seq}.", summary=f"Analysis through {seq}.")
    return tm.make_update(
        thread,
        f"event_{seq}",
        seq,
        f"2026-11-{seq:02d}",
        "Scientist",
        "dm",
        True,
        prose,
        list(covered),
        scripts=scripts,
    )


def texts(snapshot):
    return {p: f.text for p, f in snapshot.files.items()}


@pytest.fixture
def store():
    github = FakeGitHub()
    backend = project_memory.GitHubMemory(
        Settings(memory_repo="owner/memory", _env_file=None), client=github.client
    )
    return backend, github


async def test_out_of_order_entries_duplicate_retry_and_summary_coverage(store):
    backend, github = store
    base = await backend.read("p_test")
    newer = update(6, covered=["event_3", "event_4", "event_5"])
    await backend.commit(
        "p_test", base, {}, source="worker", event_id="event_6", thread_update=newer
    )
    for seq in [2, 1, 2]:
        await backend.commit(
            "p_test", base, {}, source="worker", event_id=f"event_{seq}", thread_update=update(seq)
        )
    snapshot = await backend.read("p_test")
    files = texts(snapshot)
    assert [e.seq for e in tm.log_entries(files, "t_one")] == [1, 2, 6]
    summary = tm.Summary.decode(files["threads/t_one/summary.md"])
    assert summary.turn == 6
    assert '"seq": 1' in tm.context(files, "t_one")
    assert '"seq": 2' in tm.context(files, "t_one")
    assert not any("_unmerged/" in p for p in files)
    assert "t_one" in files["THREADS.md"]
    assert len(github.commits) == 3


async def test_stale_summary_cannot_drop_previously_covered_events(store):
    backend, _ = store
    base = await backend.read("p_test")
    await backend.commit(
        "p_test", base, {}, source="worker", event_id="event_1", thread_update=update(1)
    )
    await backend.commit(
        "p_test", base, {}, source="worker", event_id="event_2", thread_update=update(2)
    )
    files = texts(await backend.read("p_test"))
    assert tm.Summary.decode(files["threads/t_one/summary.md"]).turn == 1
    assert '"seq": 2' in tm.context(files, "t_one")


async def test_two_threads_survive_and_scripts_are_explicitly_loaded(store):
    backend, github = store
    base = await backend.read("p_test")
    for thread in ["t_one", "t_two"]:
        await backend.commit(
            "p_test",
            base,
            {},
            source="worker",
            event_id=thread,
            thread_update=update(thread=thread, scripts={"analysis.py": "print(42)\n"}),
        )
    snapshot = await backend.read("p_test")
    assert "threads/t_one/scripts/analysis.py" not in snapshot.files
    assert "threads/t_two/scripts/analysis.py" in snapshot.assets
    loaded = await backend.with_scripts("p_test", snapshot, "t_one")
    assert loaded.files["threads/t_one/scripts/analysis.py"].text == "print(42)\n"
    assert "threads/t_two/scripts/analysis.py" not in loaded.files
    assert "oid text byteSize" not in project_memory.TREE
    assert all("projects/p_test" in p for p in github.trees[github.head])


async def test_secret_script_and_foreign_thread_replacement_are_rejected(store):
    backend, _ = store
    base = await backend.read("p_test")
    with pytest.raises(ValueError, match="Secret"):
        await backend.commit(
            "p_test",
            base,
            {},
            source="worker",
            event_id="bad",
            thread_update=update(scripts={"run.py": "password = 'unsafe'"}),
        )
    saved = await backend.commit(
        "p_test",
        base,
        {"threads/other/log.md": "unauthorized"},
        source="worker",
        event_id="foreign",
    )
    assert saved.rejected and not saved.commit_oid


def test_uncovered_old_events_survive_paging_and_coverage_is_bounded():
    settings = Settings(thread_log_kb=4, _env_file=None)
    files = {}
    for seq in range(1, 15):
        item = update(seq)
        item.entry.methods = "A measured method. " * 30
        files.update({p: t for p, t in tm.apply(files, item, settings).items() if t is not None})
    assert any("log-" in p for p in files)
    assert len(tm.log_entries(files, "t_one")) == 14
    view = tm.writer_view(files, "t_one", limit=1500)
    included = {e["event_id"] for e in view["entries"]}
    assert set(view["covered"]) == included | {"event_1"}
    assert "incomplete" in tm.context(files, "t_one", limit=2000)


def test_worker_proposal_binds_thread_and_preserves_script_rejections(tmp_path: Path):
    settings = Settings(memory_repo="owner/memory", _env_file=None)
    (tmp_path / "knowledge/threads/t_one").mkdir(parents=True)
    (tmp_path / "knowledge/threads/t_one/turn.json").write_text(update().prose.model_dump_json())
    (tmp_path / "code").mkdir()
    (tmp_path / "code/good.py").write_text("print(42)")
    (tmp_path / "code/bad.py").write_text("password = 'unsafe'")
    pending = memory_workspace.decode_pending(
        memory_workspace.pending(
            tmp_path, project_memory.Snapshot(head_oid="base"), settings, thread="t_one"
        ),
        settings,
    )
    assert pending.thread is not None
    assert pending.scripts == {"good.py": "print(42)"}
    assert "code/bad.py" in pending.rejected
    assert not any(p.startswith("threads/") for p in pending.changes)
    assert json.loads(pending.model_dump_json())["thread"]["paraphrase"]


async def test_compaction_adds_coverage_without_creating_or_changing_events(store):
    backend, _ = store
    base = await backend.read("p_test")
    for seq in [6, 1, 2]:
        await backend.commit(
            "p_test", base, {}, source="worker", event_id=f"event_{seq}", thread_update=update(seq)
        )
    current = await backend.read("p_test")
    files = texts(current)
    before = {r.event_id: r.digest() for r in tm.log_entries(files, "t_one")}
    compact = update(6, covered=before)
    compact.summary_only = True
    compact.prose.summary = "Both early analyses and the final analysis are accounted for."
    await backend.commit(
        "p_test", current, {}, source="compaction", event_id="compact", thread_update=compact
    )
    after = texts(await backend.read("p_test"))
    assert before == {r.event_id: r.digest() for r in tm.log_entries(after, "t_one")}
    assert set(tm.Summary.decode(after["threads/t_one/summary.md"]).covered) == set(before)
    assert '"seq":' not in tm.context(after, "t_one")


async def test_asset_rename_preserves_bytes_and_old_project_identity(store):
    backend, github = store
    base = await backend.read("p_test")
    await backend.commit(
        "p_test",
        base,
        {},
        source="worker",
        event_id="event_1",
        thread_update=update(scripts={"run.py": "print(7)"}),
    )
    base = await backend.read("p_test")
    await backend.commit(
        "p_test",
        base,
        {},
        source="import",
        event_id="plot",
        blobs={"threads/t_one/outputs/turn-1/plot.png": b"\x89PNG\xff"},
    )
    await backend.rename_project("p_test", "new-name", 1, event_id="rename")
    current = await backend.read("p_test")
    assert current._root == "projects/new-name"
    assert (await backend.read_bytes("p_test", "threads/t_one/scripts/run.py"))[0] == b"print(7)"
    assert (await backend.read_bytes("p_test", "threads/t_one/outputs/turn-1/plot.png"))[
        0
    ] == b"\x89PNG\xff"
    assert not any(p.startswith("projects/p_test/") for p in github.trees[github.head])


async def test_deep_asset_metadata_uses_bounded_queries_without_fetching_bytes(store):
    backend, github = store
    path = "threads/t_one/scripts/a/b/c/d/e/f/g/run.py"
    github.seed({"projects/p_test/" + path: "print(7)"})
    snapshot = await backend.read("p_test")
    assert path in snapshot.assets and path not in snapshot.files
    requests = [json.loads(r.content) for r in github.requests if r.method == "POST"]
    assert any("MemoryTree" in r["query"] for r in requests)
    assert not any("MemoryBlobs" in r["query"] for r in requests)
    # GraphQL fragments do not add schema depth; each directory adds two fields.
    assert project_memory.TREE.count("entries {") <= 7
