import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from mica.agents.analysis import cellpose_tools as tools
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runner import Context, build_options


def request(**changes):
    return tools.SegmentRequest.model_validate(
        dict(
            input_path="crop.tif",
            output_dir="outputs/cells",
            version="4.2.1.1",
            model="cpsam",
            **changes,
        )
    )


def test_versions_select_separate_interpreters():
    assert tools.interpreter("3.1.1.3", gpu=False) == Path("/opt/sap/bin/python")
    assert tools.interpreter("3.1.1.3", gpu=True) == Path("/opt/cellpose/3.1.1.3/bin/python")
    assert tools.interpreter("4.2.1.1", gpu=False) == Path("/opt/cellpose/4.2.1.1/bin/python")
    with pytest.raises(ValueError):
        tools.interpreter("latest", gpu=False)


def test_tools_are_available_without_dynamic_package_installation(tmp_path):
    settings = Settings(_env_file=None, runtimes_enabled=False)
    options = build_options(Context(tmp_path, tmp_path / "data"), settings)
    assert "mcp__pistachio__cellpose_segment" in options.allowed_tools
    assert "mcp__pistachio__cellpose_versions" in options.allowed_tools
    assert options.env["MCP_TOOL_TIMEOUT"] == "1200000"


@pytest.mark.parametrize("output", ["/tmp/out", "outputs/../code/out", "code/out", "outputs"])
def test_output_must_be_new_workspace_result_folder(tmp_path, output):
    (tmp_path / "crop.tif").write_bytes(b"image")
    body = request().model_copy(update={"output_dir": output})
    with pytest.raises(ValueError):
        tools.paths(body, tmp_path, tmp_path / "data")


def test_input_and_output_symlink_escapes_are_rejected(tmp_path):
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (outside / "crop.tif").write_bytes(b"image")
    (work / "crop.tif").symlink_to(outside / "crop.tif")
    with pytest.raises(ValueError, match="workspace"):
        tools.paths(request(), work, work / "data")
    (work / "crop.tif").unlink()
    (work / "crop.tif").write_bytes(b"image")
    (work / "outputs").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic"):
        tools.paths(request(), work, work / "data")


def test_existing_results_are_never_replaced(tmp_path):
    (tmp_path / "crop.tif").write_bytes(b"image")
    (tmp_path / "outputs/cells").mkdir(parents=True)
    with pytest.raises(ValueError, match="preserved"):
        tools.paths(request(), tmp_path, tmp_path / "data")


def test_cpu_worker_rejects_gpu_and_wrong_model(tmp_path, monkeypatch):
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(tools, "interpreter", lambda *a, **kw: python)
    settings = Settings(_env_file=None)
    with pytest.raises(ValueError, match="approved GPU"):
        tools.command(request(device="gpu"), tmp_path / "in", tmp_path / "out", settings)
    with pytest.raises(ValueError, match="model"):
        tools.command(
            request().model_copy(update={"model": "cyto3"}),
            tmp_path / "in",
            tmp_path / "out",
            settings,
        )


def test_gpu_tool_preserves_isolation_and_only_binds_fixed_devices(tmp_path, monkeypatch):
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(tools, "interpreter", lambda *a, **kw: python)
    devices = (Path("/dev/nvidia0"), Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm"))
    monkeypatch.setattr(tools, "gpu_devices", lambda: devices)
    args = tools.command(
        request(), tmp_path / "in", tmp_path / "out", Settings(_env_file=None, gpu_worker=True)
    )
    assert "--unshare-all" in args and "--clearenv" in args and "--proc" in args
    bindings = [args[i + 1 : i + 3] for i, arg in enumerate(args) if arg == "--dev-bind"]
    assert bindings == [[str(d), str(d)] for d in devices]
    assert "/mnt/gcs" not in args and "/srv/pistachio/work" not in args
    assert args[-2] == "--request" and '"device": "gpu"' in args[-1]


def test_unknown_version_and_unbounded_options_are_rejected():
    for change in [
        {"version": "latest"},
        {"batch_size": 10000},
        {"flow_threshold": float("nan")},
        {"script": "arbitrary.py"},
    ]:
        with pytest.raises(ValidationError):
            tools.SegmentRequest.model_validate({**request().model_dump(), **change})


def test_descriptor_walk_rejects_retargeted_output_parent(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "outputs").symlink_to(outside)
    with pytest.raises(OSError):
        tools.open_directory(work, ("outputs", "cells"), create=True)
    assert list(outside.iterdir()) == []


def test_opened_directory_remains_bound_after_rename(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "outputs").mkdir()
    fd = tools.open_directory(root, ("outputs",))
    try:
        (root / "outputs").rename(root / "original")
        (root / "outputs").mkdir()
        os.mkdir("cells", dir_fd=fd)
        assert (root / "original/cells").is_dir()
        assert not (root / "outputs/cells").exists()
    finally:
        os.close(fd)
