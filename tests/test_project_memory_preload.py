"""Routing sees the ordered core and chooses topic reads from its index."""

import pytest
from test_memory_editor import snapshot

from mica.agents.analysis import project_memory
from mica.agents.analysis.config import Settings


def test_whole_core_preload_in_order_without_topic_bodies():
    paths = ["INDEX.md", "open.md", "conventions.md", "lessons.md", "THREADS.md"]
    files = {p: p + "\nsecond line\n" for p in paths}
    files.update({"results/x.md": "HIDDEN TOPIC", "PROJECT.md": "Separate guide"})
    result = project_memory.context(snapshot(files), Settings(_env_file=None))
    assert result == "\n\n".join(f"File: {p}\n{files[p]}" for p in paths)
    assert "HIDDEN TOPIC" not in result and "Separate guide" not in result


@pytest.mark.parametrize("budget", [1, 70, 100, 300, 12000])
def test_cut_is_at_line_boundary_names_path_and_unread_files(budget):
    files = {"INDEX.md": "index row\n" * 1300, "open.md": "pending", "lessons.md": "lesson"}
    settings = Settings(_env_file=None, memory_router_chars=budget)
    result = project_memory.context(snapshot(files), settings)
    assert len(result) <= budget
    if budget >= 70:
        assert result.endswith("[Cut: INDEX.md. Unread: open.md, lessons.md.]")
        assert result.removeprefix("File: INDEX.md\n").partition("\n[Cut:")[0] in (
            "index row\n" * i for i in range(1301)
        )


def test_cut_preserves_earlier_whole_files():
    files = {"INDEX.md": "one\ntwo\n", "open.md": "work\n" * 100, "conventions.md": "later"}
    settings = Settings(_env_file=None, memory_router_chars=120)
    result = project_memory.context(snapshot(files), settings)
    assert result.startswith("File: INDEX.md\none\ntwo\n\n\nFile: open.md\n")
    assert result.endswith("[Cut: open.md. Unread: conventions.md.]")
    assert len(result) <= 120
