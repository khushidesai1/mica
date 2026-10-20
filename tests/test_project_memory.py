from __future__ import annotations

import logging

import httpx
import pytest

from mica.agents.analysis import project_memory as memory
from mica.agents.analysis.config import Settings
from tests.fake_github import FakeGitHub, blob_oid

FACT = "type: fact\nsource: Khushi, 2026-10-10\nstatus: current\n\nUse viridis for heatmaps.\n"


@pytest.fixture
def store():
    github = FakeGitHub()
    settings = Settings(memory_repo="owner/memory", _env_file=None)
    return memory.GitHubMemory(settings, client=github.client), github


async def test_round_trip_and_historical_snapshot(store, caplog):
    backend, github = store
    caplog.set_level(logging.INFO)
    base = await backend.read("p_test", event_id="roundtrip")
    assert base is not None and base.files == {} and base.head_oid == github.head
    files = {"INDEX.md": "- conventions.md\n", "conventions.md": FACT, "PROJECT.md": "# Guide\n"}
    saved = await backend.commit("p_test", base, files, source="editor", event_id="roundtrip")
    result = await backend.read("p_test", event_id="readback")
    assert result.head_oid == saved.commit_oid
    assert {p: f.text for p, f in result.files.items()} == files
    assert all(f.oid == blob_oid(f.text) for f in result.files.values())
    assert (await backend.read("p_test", head_oid=base.head_oid)).files == {}
    assert "memory_read pid=p_test event=roundtrip" in caplog.text
    assert "source=github" in caplog.text and "outcome=committed" in caplog.text
    assert "viridis" not in caplog.text
    assert github.commits[0]["messageHeadline"] == "memory(p_test): editor roundtrip"


@pytest.mark.parametrize(
    "path,text,reason",
    [
        ("../escape.md", FACT, "path"),
        ("conventions.md", "No headers", "headers"),
        ("data/token.md", FACT + "password=secret", "Secret"),
        ("PROJECT.md", "x" * 100_001, "size"),
        ("lessons.md", FACT + "x" * 16_384, "size"),
        ("INDEX.md", "x" * 4001, "Index"),
    ],
)
async def test_rejected_files_have_reasons(store, path, text, reason):
    backend, github = store
    base = await backend.read("p_test")
    saved = await backend.commit("p_test", base, {path: text}, event_id="bad", source="editor")
    assert reason.lower() in saved.rejected[path].lower()
    assert not saved.commit_oid and not github.commits


async def test_exact_guide_limit_and_markdown_recipe_limit(store):
    backend, _ = store
    base = await backend.read("p_test")
    saved = await backend.commit(
        "p_test",
        base,
        {
            "PROJECT.md": "x" * 100_000,
            "recipes/plot.md": FACT + "x" * 32_000,
        },
        event_id="limits",
        source="worker",
    )
    assert saved.writes == 2 and not saved.rejected


async def test_concurrent_edits_merge_and_conflicts_are_preserved(store):
    backend, github = store
    base = await backend.read("p_test")
    github.seed({"projects/p_test/conventions.md": FACT})
    saved = await backend.commit(
        "p_test", base, {"lessons.md": FACT}, event_id="other", source="worker"
    )
    assert saved.writes == 1
    saved = await backend.commit(
        "p_test", base, {"conventions.md": FACT + "Changed"}, event_id="collision", source="worker"
    )
    result = await backend.read("p_test")
    assert result.files["conventions.md"].text == FACT
    assert result.files["lessons.md"].text == FACT
    assert len(saved.unmerged) == 1
    assert result.files[saved.unmerged[0]].text == FACT + "Changed"
    assert saved.unmerged[0] in result.files["INDEX.md"].text
    again = await backend.commit(
        "p_test", base, {"conventions.md": FACT + "Changed"}, event_id="collision", source="worker"
    )
    assert again.commit_oid == saved.commit_oid


async def test_truncated_blob_is_read_at_pinned_head_and_invalid_files_skipped(store, caplog):
    backend, github = store
    github.seed({"projects/p_test/conventions.md": FACT, "projects/p_test/unsafe.md": "bad"})
    github.truncated.add("projects/p_test/conventions.md")
    result = await backend.read("p_test")
    assert result.files["conventions.md"].text == FACT
    assert "unsafe.md" not in result.files
    assert github.requests[-1].url.params["ref"] == result.head_oid
    assert "memory_invalid" in caplog.text


@pytest.mark.parametrize("timeout", [False, True])
async def test_unavailable_reads_fail_open_and_writes_raise(store, timeout, caplog):
    backend, github = store
    base = await backend.read("p_test")
    if timeout:
        github.timeout = True
    else:
        github.fail = 502
    assert await backend.read("p_test") is None
    with pytest.raises(httpx.HTTPError):
        await backend.commit("p_test", base, {"INDEX.md": "test"}, source="editor", event_id="down")
    assert "source=unavailable" in caplog.text
    assert "outcome=failed:" in caplog.text


async def test_aggregate_limit_rejects_new_files_without_losing_existing(store):
    backend, github = store
    backend.settings.memory_max_files = 2
    base = await backend.read("p_test")
    saved = await backend.commit(
        "p_test",
        base,
        {
            "conventions.md": FACT,
            "lessons.md": FACT,
            "open.md": FACT,
        },
        source="editor",
        event_id="full",
    )
    assert saved.writes == 2 and len(saved.rejected) == 1
    assert len(github.trees[github.head]) == 2


async def test_success_metrics_survive_default_application_warning_level(store, caplog):
    backend, _ = store
    caplog.set_level(logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    try:
        base = await backend.read("logging_test", event_id="visible")
        await backend.commit(
            "logging_test", base, {"PROJECT.md": "A guide"}, source="editor", event_id="visible"
        )
        assert "memory_read pid=logging_test" in caplog.text
        assert "memory_commit pid=logging_test" in caplog.text
        assert "outcome=committed" in caplog.text
        assert root.level == logging.WARNING
        assert "A guide" not in caplog.text
    finally:
        root.setLevel(logging.INFO)


async def test_rename_moves_folder_keeps_stable_id_and_old_worker_proposals(store):
    backend, github = store
    github.seed(
        {
            "projects/pista-cosmx-6k-crc/PROJECT.md": "# Old title\n\nVerified data guide.\n",
            "projects/pista-cosmx-6k-crc/conventions.md": FACT,
        }
    )
    before = await backend.read("pista-cosmx-6k-crc")
    result = await backend.rename_project("pista-cosmx-6k-crc", "crc-cosmx", 1, event_id="rename")
    assert result.commit_oid
    files = github.trees[github.head]
    assert not any(p.startswith("projects/pista-cosmx-6k-crc/") for p in files)
    assert (
        files["projects/crc-cosmx/PROJECT.md"]
        == "# Project brief: crc-cosmx\n\nVerified data guide.\n"
    )
    assert files["projects/crc-cosmx/conventions.md"] == FACT
    after = await backend.read("pista-cosmx-6k-crc")
    assert after.files["conventions.md"].text == FACT
    assert "_names" not in after.model_dump() and "_root" not in after.model_dump()
    historical = await backend.read("pista-cosmx-6k-crc", head_oid=before.head_oid)
    assert historical.files == before.files
    # A worker started before the rename writes through the stable ID to the new folder.
    await backend.commit(
        "pista-cosmx-6k-crc", before, {"open.md": FACT}, source="worker", event_id="old-worker"
    )
    assert github.trees[github.head]["projects/crc-cosmx/open.md"] == FACT
    assert not any(p.startswith("projects/pista-cosmx-6k-crc/") for p in github.trees[github.head])


async def test_newer_rename_wins_and_replay_does_not_revert(store):
    backend, github = store
    await backend.rename_project("p_test", "first-name", 1, event_id="first")
    await backend.rename_project("p_test", "second-name", 2, event_id="second")
    head = github.head
    assert not (await backend.rename_project("p_test", "first-name", 1, event_id="first")).writes
    assert not (await backend.rename_project("p_test", "second-name", 2, event_id="second")).writes
    assert github.head == head
    assert (
        (await backend.read("p_test"))
        .files["PROJECT.md"]
        .text.startswith("# Project brief: second-name")
    )


async def test_duplicate_names_and_existing_folders_are_isolated(store):
    backend, github = store
    github.seed({"projects/crc-cosmx/PROJECT.md": "# Other owner's data\n"})
    await backend.rename_project("p_alice", "crc-cosmx", 1, event_id="alice")
    await backend.rename_project("p_bob", "crc-cosmx", 1, event_id="bob")
    alice = await backend.read("p_alice")
    bob = await backend.read("p_bob")
    assert alice._root != bob._root
    assert github.trees[github.head]["projects/crc-cosmx/PROJECT.md"] == "# Other owner's data\n"


async def test_rename_refuses_invalid_entries_without_deleting_them(store):
    backend, github = store
    github.seed({"projects/p_test/unsupported.bin": "keep me"})
    head = github.head
    with pytest.raises(ValueError, match="invalid memory"):
        await backend.rename_project("p_test", "new-name", 1, event_id="rename")
    assert github.head == head


async def test_rename_retries_concurrent_memory_edit_without_losing_it(store, monkeypatch):
    backend, github = store
    github.seed({"projects/p_test/PROJECT.md": "# Original\n"})
    original = backend.graphql
    moved = False

    async def race(client, query, variables):
        nonlocal moved
        if query == memory.COMMIT and not moved:
            moved = True
            github.seed({"projects/p_test/open.md": FACT})
        return await original(client, query, variables)

    monkeypatch.setattr(backend, "graphql", race)
    await backend.rename_project("p_test", "crc-cosmx", 1, event_id="raced")
    assert github.trees[github.head]["projects/crc-cosmx/open.md"] == FACT
    assert not any(p.startswith("projects/p_test/") for p in github.trees[github.head])
    # A folder name is not an alternate authorization identity.
    assert await backend.read("crc-cosmx") is None


async def test_rename_reserves_empty_project_ids_and_replays_suffixed_folder(store):
    backend, github = store
    await backend.rename_project(
        "p_test", "crc-cosmx", 1, event_id="rename", reserved_ids={"crc-cosmx", "p_test"}
    )
    snapshot = await backend.read("p_test")
    assert snapshot._root.startswith("projects/crc-cosmx-")
    head = github.head
    await backend.rename_project(
        "p_test", "crc-cosmx", 1, event_id="rename", reserved_ids={"crc-cosmx", "p_test"}
    )
    assert github.head == head
    assert (await backend.read("crc-cosmx")).files == {}
