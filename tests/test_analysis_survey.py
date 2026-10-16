from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mica.agents.analysis import survey
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runner import Context, build_options


def test_inventory_paginates_all_children_without_recursing(monkeypatch):
    from fake_inventory import fake_gcs

    requests = fake_gcs(
        monkeypatch,
        {
            "": [f"sample-{i}.h5ad" for i in range(300)]
            + [
                "big.zarr/",
                "other.n5/",
                "data/",
                "outputs/",
                ".hidden/",
            ]
        },
    )
    result = survey.inventory("gs://lab-data/", Settings(_env_file=None))
    assert result["complete"] and result["entry_count"] == 303
    assert len(result["folders"][0]["entries"]) == 303
    assert len(requests) == 4
    assert all(r["query_params"]["prefix"] == "" for r in requests)
    assert all(r["query_params"]["delimiter"] == "/" for r in requests)
    assert all(r["retry"] is None and r["timeout"] <= 20 for r in requests)
    assert not result["content_inspected"]
    assert sum("array store" in e["kind"] for e in result["folders"][0]["entries"]) == 2


def test_inventory_stops_on_timeout_without_retry(monkeypatch):
    from google.cloud import storage

    listing = Mock(side_effect=TimeoutError("secret folder name"))
    monkeypatch.setattr(storage, "Client", lambda: SimpleNamespace(list_blobs=listing))
    result = survey.inventory("gs://lab-data/", Settings(_env_file=None))
    assert not result["complete"]
    assert "timeout" in result["folders"][0]["stopped"]
    assert "secret" not in str(result)
    assert listing.call_count == 1


def test_inventory_shares_deadline_across_pages_and_folders(monkeypatch, caplog):
    from fake_inventory import fake_gcs

    requests = fake_gcs(monkeypatch, {"raw/": [f"{i}/" for i in range(300)]})
    monkeypatch.setattr(survey.time, "monotonic", lambda: 21 if requests else 0)
    result = survey.inventory(
        ["gs://lab-data/raw/", "gs://lab-data/other/"],
        Settings(_env_file=None),
        event_id="timeout-event",
    )
    assert not result["complete"]
    assert all(not f["complete"] for f in result["folders"])
    assert len(requests) == 1
    assert "paths=2 entries=0 complete=false ms=21000" in caplog.text
    assert "raw/" not in caplog.text and "other/" not in caplog.text


def test_inventory_preserves_first_page_on_later_error(monkeypatch):
    from fake_inventory import fake_gcs

    fake_gcs(monkeypatch, {"raw/": [f"{i}/" for i in range(300)]}, fail_page=2)
    result = survey.inventory("gs://lab-data/raw/", Settings(_env_file=None))
    assert not result["complete"] and result["entry_count"] == 100
    assert "RuntimeError" in result["folders"][0]["stopped"]


@pytest.mark.parametrize("path", ["", "raw_outputs/raw/TMA1/", "raw data"])
def test_inventory_uri_is_scoped_to_project(path):
    uri = survey.inventory_uri("gs://lab-data/liver", path, Settings(_env_file=None))
    assert uri == "gs://lab-data/liver/" + path


@pytest.mark.parametrize(
    "path",
    [
        "../other/",
        "/other/",
        "gs://other-bucket/",
        "raw/../../other",
        "raw\\..\\other",
        "%2e%2e/other",
        "raw?query=1",
        "raw#fragment",
        "raw/huge.zarr/",
        "sample.n5/",
        ".hidden/",
        "outputs/",
    ],
)
def test_inventory_uri_rejects_unscoped_or_opaque_paths(path):
    with pytest.raises(ValueError):
        survey.inventory_uri("gs://lab-data/liver/", path, Settings(_env_file=None))


def test_local_inventory_does_not_follow_symlinks_or_read_file_contents(tmp_path, monkeypatch):
    root = tmp_path / "lab-data" / "liver"
    root.mkdir(parents=True)
    outside = tmp_path / "other-project"
    outside.mkdir()
    (outside / "private.csv").write_text("private")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "sample.csv").write_bytes(b"cells\n42\n")
    settings = Settings(_env_file=None, storage="local", data_mount_root=tmp_path)
    with pytest.raises(ValueError, match="inside this project"):
        survey.inventory_uri("gs://lab-data/liver/", "link", settings)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: pytest.fail("No data-file reads"))
    result = survey.inventory("gs://lab-data/liver/", settings)
    assert result["folders"][0]["entries"] == [
        {"path": "liver/sample.csv", "kind": "file", "size_bytes": 9}
    ]
    assert not result["content_inspected"]


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
