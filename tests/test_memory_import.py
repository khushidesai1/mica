"""Import is explicit and never replaces an existing GitHub guide."""

import importlib.util
from pathlib import Path

from analysis_helpers import seed_project
from fake_github import FakeGitHub

from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.project_memory import GitHubMemory
from pistachio.agents.analysis.storage import LocalStorage

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
