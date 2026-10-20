"""Lossless archive/index behavior across all project memory writers."""

import json
import time

import pytest
from test_project_memory import FACT, store  # noqa: F401

from mica.agents.analysis import memory_archive, memory_workspace, project_memory
from mica.agents.analysis.config import Settings


async def test_growth_beyond_old_megabyte_and_flat_index_is_lossless(store):  # noqa: F811
    backend, _ = store
    base = await backend.read("p_test")
    texts = {f"data/note-{n:03d}.md": FACT + str(n) + "x" * 6000 for n in range(250)}
    saved = await backend.commit("p_test", base, texts, source="worker", event_id="growth")
    assert saved.commit_oid and not saved.rejected
    result = await backend.read("p_test")
    assert sum(len(f.text.encode()) for f in result.files.values()) > 1024 * 1024
    assert len(result.files["INDEX.md"].text) <= backend.settings.memory_index_chars
    assert len([p for p in result.files if p.startswith("archive/index-")]) > 1
    for path, text in texts.items():
        version = result.files.get(path) or result.files[memory_archive.archive_path(path, text)]
        assert version.text == text
    context = project_memory.context(result, "x note", backend.settings)
    assert len(context) <= backend.settings.memory_router_chars
    assert "File: archive/" not in context
    for path, file in result.files.items():
        if memory_archive.is_index(path):
            assert len(file.text) <= backend.settings.memory_index_chars


@pytest.mark.parametrize("source", ["editor", "worker", "compaction"])
async def test_rewrites_archive_original_bytes_for_all_writers(store, source):  # noqa: F811
    backend, github = store
    before = {
        "data/same.md": FACT,
        "results/same.md": FACT + "Result",
        "recipes/plot.py": "# source: scientist\n" + "print(1)\n" * 3000,
    }
    github.seed({"projects/p_test/" + p: text for p, text in before.items()})
    base = await backend.read("p_test")
    saved = await backend.commit(
        "p_test", base, {p: None for p in before}, source=source, event_id="archive"
    )
    assert saved.commit_oid and not saved.rejected
    result = await backend.read("p_test")
    assert not result.invalid
    for path, text in before.items():
        assert path not in result.files
        assert result.files[memory_archive.archive_path(path, text)].text == text
    assert len({memory_archive.archive_path(p, t) for p, t in before.items()}) == 3


async def test_full_storage_rejects_deletion_and_preserves_original(store):  # noqa: F811
    backend, github = store
    github.seed({"projects/p_test/data/fact.md": FACT})
    backend.settings.memory_max_files = 1
    base = await backend.read("p_test")
    saved = await backend.commit(
        "p_test", base, {"data/fact.md": None}, source="worker", event_id="full"
    )
    assert not saved.commit_oid and saved.rejected
    assert (await backend.read("p_test")).files["data/fact.md"].text == FACT


async def test_archive_immutable_and_old_worker_conflict_survives(store):  # noqa: F811
    backend, github = store
    github.seed({"projects/p_test/data/fact.md": FACT})
    base = await backend.read("p_test")
    await backend.commit(
        "p_test", base, {"data/fact.md": None}, source="compaction", event_id="move"
    )
    archived = memory_archive.archive_path("data/fact.md", FACT)
    latest = await backend.read("p_test")
    reject = await backend.commit(
        "p_test", latest, {archived: None}, source="worker", event_id="bad"
    )
    assert archived in reject.rejected
    saved = await backend.commit(
        "p_test", base, {"data/fact.md": FACT + "new"}, source="worker", event_id="old-worker"
    )
    after = await backend.read("p_test")
    assert after.files[archived].text == FACT
    assert saved.unmerged and after.files[saved.unmerged[0]].text == FACT + "new"


def test_worker_materializes_archives_but_cannot_modify_them(tmp_path):
    settings = Settings(_env_file=None)
    path = memory_archive.archive_path("_unmerged/old/data/fact.md", FACT)
    base = project_memory.Snapshot(
        head_oid="a" * 40, files={path: project_memory.File(text=FACT, oid="b" * 40)}
    )
    memory_workspace.materialize(tmp_path, base, settings)
    (tmp_path / "knowledge" / path).unlink()
    proposal = memory_workspace.proposal(tmp_path, base, settings)
    assert proposal.changes == {} and path in proposal.rejected


def test_capacity_materialization_benchmark(tmp_path):
    settings = Settings(_env_file=None)
    texts = {f"data/note-{n:03d}.md": FACT + str(n) + "x" * 7000 for n in range(450)}
    layout = memory_archive.prepare({}, texts, settings)
    base = project_memory.Snapshot(
        head_oid="a" * 40,
        files={
            p: project_memory.File(text=t, oid="b" * 40) for p, t in layout.items() if t is not None
        },
    )
    assert len(base.files) < settings.memory_max_files
    started = time.monotonic()
    memory_workspace.materialize(tmp_path, base, settings)
    pending = memory_workspace.pending(tmp_path, base, settings)
    assert json.loads(pending)["changes"] == {}
    assert time.monotonic() - started < 15


async def test_rollout_deadline_keeps_old_worker_snapshots_compatible(store, monkeypatch):  # noqa: F811
    backend, github = store
    github.seed({"projects/p_test/data/fact.md": FACT})
    backend.settings.memory_archive_after = 2000
    monkeypatch.setattr(memory_archive.time, "time", lambda: 1000)
    base = await backend.read("p_test")
    await backend.commit(
        "p_test", base, {"data/fact.md": FACT + "before"}, source="worker", event_id="before"
    )
    before = await backend.read("p_test")
    assert not any(p.startswith("archive/") for p in before.files)
    monkeypatch.setattr(memory_archive.time, "time", lambda: 2001)
    await backend.commit(
        "p_test", before, {"data/fact.md": FACT + "after"}, source="worker", event_id="after"
    )
    after = await backend.read("p_test")
    assert (
        after.files[memory_archive.archive_path("data/fact.md", FACT + "before")].text
        == FACT + "before"
    )


async def test_rename_preserves_guide_and_archive_indexes(store):  # noqa: F811
    backend, github = store
    github.seed({"projects/p_test/PROJECT.md": "# Original\nImportant layout."})
    base = await backend.read("p_test")
    await backend.rename_project("p_test", "renamed", 1, event_id="rename")
    after = await backend.read("p_test")
    assert after._root == "projects/renamed"
    path = memory_archive.archive_path("PROJECT.md", base.files["PROJECT.md"].text)
    assert after.files[path].text == base.files["PROJECT.md"].text
    assert "archive/INDEX.md" in after.files["INDEX.md"].text
