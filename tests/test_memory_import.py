"""Import is explicit and never replaces an existing GitHub guide."""

import importlib.util
from pathlib import Path

from analysis_helpers import seed_project
from fake_github import FakeGitHub

from mica.agents.analysis.config import Settings
from mica.agents.analysis.project_memory import GitHubMemory
from mica.agents.analysis.storage import LocalStorage

spec = importlib.util.spec_from_file_location(
    "memory_import", Path(__file__).parents[1] / "tools/memory_import.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


async def test_import_dry_run_apply_and_repeat(tmp_path):
    store = LocalStorage(tmp_path)
    pid = seed_project(store, brief=b"Existing guide")
    fake = FakeGitHub()
    backend = GitHubMemory(Settings(_env_file=None, memory_repo="owner/repo"), client=fake.client)
    dry = await module.import_guides(store, backend, apply=False)
    assert dry[0]["status"] == "would-import" and not fake.commits
    saved = await module.import_guides(store, backend, apply=True)
    assert saved[0]["commit"] == fake.head
    assert (await backend.read(pid)).files["PROJECT.md"].text == "Existing guide"
    store.put(f"projects/{pid}/PROJECT.md", b"Changed legacy guide")
    again = await module.import_guides(store, backend, apply=True)
    assert again[0]["status"] == "existing-guide-skipped" and len(fake.commits) == 1


async def test_layout_import_uses_verified_excerpts_and_normal_commit(tmp_path):
    store = LocalStorage(tmp_path)
    pid = seed_project(store)
    fake = FakeGitHub()
    text = (
        "type: open\nsource: analysis, 2026-10-11\nstatus: current\n\n"
        "- AtoMx export: `raw_outputs/raw/<TMA>/*_metadata_file.csv.gz`\n"
        "- CellPose prior fed to Proseg: `resegmentation/<TMA>/shapes/cellpose_polygons.parquet`"
        " (TMA1: 60,772 cells)\nHYPOTHESIS: unverified result\n"
    )
    fake.seed({f"projects/{pid}/open.md": text})
    backend = GitHubMemory(Settings(_env_file=None, memory_repo="owner/repo"), client=fake.client)
    facts = [
        "open.md::AtoMx export: `raw_outputs/raw/<TMA>/*_metadata_file.csv.gz`",
        "open.md::CellPose prior fed to Proseg: "
        "`resegmentation/<TMA>/shapes/cellpose_polygons.parquet`",
    ]
    dry = await module.layout_from_memory(store, backend, pid, facts, apply=False)
    assert dry["status"] == "would-import" and len(fake.commits) == 1
    assert "## Data" in dry["guide"] and "gs://lab-data/liver/" in dry["guide"]
    assert "HYPOTHESIS" not in dry["guide"] and "60,772" not in dry["guide"]
    saved = await module.layout_from_memory(store, backend, pid, facts, apply=True)
    assert saved["commit"] == fake.head
    assert ": import layout-import-" in fake.commits[0]["messageHeadline"]
    snapshot = await backend.read(pid)
    assert snapshot.files["PROJECT.md"].text == dry["guide"]
    assert snapshot.files["open.md"].text == text
    again = await module.layout_from_memory(store, backend, pid, facts, apply=True)
    assert again["status"] == "existing-guide-skipped" and len(fake.commits) == 2


async def test_layout_import_rejects_unverified_excerpts(tmp_path):
    import pytest

    store = LocalStorage(tmp_path)
    pid = seed_project(store)
    fake = FakeGitHub()
    backend = GitHubMemory(Settings(_env_file=None, memory_repo="owner/repo"), client=fake.client)
    for facts in [[], ["open.md::Invented data layout"], ["malformed"]]:
        with pytest.raises(ValueError):
            await module.layout_from_memory(store, backend, pid, facts, apply=True)
    assert not fake.commits
