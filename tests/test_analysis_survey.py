from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pistachio.agents.analysis import survey
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.runner import Context, build_options


def test_inventory_does_not_paginate_or_expand_stores(monkeypatch):
    from google.cloud import storage

    calls = []

    def listing(bucket, **kwargs):
        calls.append(kwargs)
        assert kwargs["delimiter"] == "/" and kwargs["retry"] is None
        assert kwargs["max_results"] == 80 and kwargs["timeout"] <= 5
        prefix = kwargs["prefix"]
        assert ".zarr" not in prefix and ".n5" not in prefix

        def pages():
            yield [SimpleNamespace(name=f"{prefix}sample-{i}.h5ad", size=10**11) for i in range(60)]
            raise AssertionError("Must never load another page")

        return SimpleNamespace(pages=pages(), prefixes={"big.zarr/", "other.n5/", "data/", "more/"})

    monkeypatch.setattr(storage, "Client", lambda: SimpleNamespace(list_blobs=listing))
    result = survey.inventory("gs://lab-data/", Settings(_env_file=None))
    assert len(result["entries"]) <= 200 and len(calls) <= 4
    assert not result["content_inspected"]
    assert any("array store" in e["kind"] for e in result["entries"])


def test_inventory_stops_on_timeout_without_retry(monkeypatch):
    from google.cloud import storage

    listing = Mock(side_effect=TimeoutError("slow API"))
    monkeypatch.setattr(storage, "Client", lambda: SimpleNamespace(list_blobs=listing))
    result = survey.inventory("gs://lab-data/", Settings(_env_file=None))
    assert result["listings"] == 1 and "TimeoutError" in result["stopped"]
    assert listing.call_count == 1


async def test_init_tool_policy_blocks_all_data_scans(tmp_path):
    ctx = Context(tmp_path / "work", tmp_path / "mount", survey_only=True)
    opts = build_options(ctx, Settings(_env_file=None))
    assert set(opts.tools) == {"Read", "Write", "Edit"}
    assert not opts.add_dirs
    guard = opts.hooks["PreToolUse"][0].hooks[0]
    for name, args in [
        ("Bash", {"command": "find /mnt/gcs -type f"}),
        ("Glob", {"pattern": "**/*"}),
        ("Read", {"file_path": str(ctx.data_path / "big.h5ad")}),
    ]:
        decision = await guard(
            {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args}, None, None
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(ctx.workdir / "init_inventory.json")},
        },
        None,
        None,
    )
    ctx.survey_only = False
    assert "Bash" in build_options(ctx, Settings(_env_file=None)).tools


def test_directory_inspector_never_recurses(tmp_path, monkeypatch):
    from pathlib import Path

    from tools.inspect_data import inspect

    for i in range(120):
        (tmp_path / f"file-{i}.txt").touch()
    (tmp_path / "huge.zarr").mkdir()
    monkeypatch.setattr(Path, "rglob", lambda *args: pytest.fail("Recursive scan forbidden"))
    result = inspect(tmp_path)
    assert len(result["entries"]) <= 100
    assert "not recursive" in result["coverage"]
