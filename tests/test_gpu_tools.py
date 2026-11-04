import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mica.agents.analysis import gpu_tools as tools
from mica.agents.analysis import runtime_sandbox
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runner import Context, build_options
from mica.agents.analysis.toolkit_catalog import Environment


def request(**changes):
    return tools.RunGPU.model_validate(
        {
            "environment": "cellpose-4.2.1.1",
            "script": "code/run.py",
            "output_dir": "outputs/result",
            **changes,
        }
    )


def test_gpu_tool_is_registered_only_on_gpu_workers_and_uses_run_allowance(tmp_path):
    settings = Settings(_env_file=None, runtimes_enabled=False, max_run_seconds=7200)
    ctx = Context(tmp_path, tmp_path / "data")
    cpu = build_options(ctx, settings)
    assert tools.tools(tmp_path, tmp_path, settings) == []
    assert "mcp__pistachio__run_gpu" not in cpu.allowed_tools
    gpu = build_options(ctx, settings.model_copy(update={"gpu_worker": True}))
    assert "mcp__pistachio__run_gpu" in gpu.allowed_tools
    assert gpu.env["MCP_TOOL_TIMEOUT"] == "7200000"
    assert not any("cellpose_" in name for name in gpu.allowed_tools)


@pytest.mark.parametrize("runtimes", [False, True])
def test_gpu_worker_requires_real_tool_preflight_before_large_work(tmp_path, runtimes):
    ctx = Context(tmp_path, tmp_path / "data", project_md="")
    settings = Settings(_env_file=None, gpu_worker=True, runtimes_enabled=runtimes)
    prompt = build_options(ctx, settings).system_prompt["append"]
    assert "run with /opt/sci/bin/python" not in prompt
    assert "Use run_gpu with a CUDA environment id" in prompt
    assert "Bash intentionally hides GPU" in prompt
    assert "Before loading\nlarge inputs or training" in prompt
    assert "complete a small CUDA operation" in prompt
    assert "full-data CPU probe/training without explicit user approval" in prompt
    if runtimes:
        assert "scvi/ResolVI" in prompt and "isolated=true" in prompt
        assert "nvidia-smi's CUDA label alone\ndo not prove compatibility" in prompt
    else:
        assert "cannot install packages" in prompt and "ensure_packages" not in prompt


def test_environment_requires_known_cuda_environment_and_installed_interpreter(monkeypatch):
    settings = Settings(_env_file=None, gpu_worker=True)
    with pytest.raises(ValueError, match="approved GPU"):
        tools.select_environment("cellpose-4.2.1.1", Settings(_env_file=None))
    for name in ("unknown", "/usr/bin/python", "../python"):
        with pytest.raises(ValueError, match="environment id"):
            tools.select_environment(name, settings)
    with pytest.raises(ValueError, match="no CUDA"):
        tools.select_environment("science", settings)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(ValueError, match="ensure_packages"):
        tools.select_environment("cellpose-4.2.1.1", settings)


@pytest.fixture
def work(tmp_path):
    (tmp_path / "code").mkdir()
    (tmp_path / "code/run.py").write_text("print('ok')")
    (tmp_path / "inputs").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "output", ["/tmp/out", "outputs/../code/out", "code/out", "outputs", "outputs/bad:folder"]
)
def test_output_must_be_new_workspace_result_folder(work, output):
    with pytest.raises(ValueError):
        tools.paths(request(output_dir=output), work)


def test_script_and_output_symlink_escapes_are_rejected(work, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "run.py").write_text("print('outside')")
    (work / "code/run.py").unlink()
    (work / "code/run.py").symlink_to(outside / "run.py")
    with pytest.raises(ValueError, match="workspace"):
        tools.paths(request(), work)
    (work / "code/run.py").unlink()
    (work / "code/run.py").write_text("print('ok')")
    (work / "outputs").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic"):
        tools.paths(request(), work)
    with pytest.raises(OSError):
        tools.open_directory(work, ("outputs", "result"), create=True)
    assert not (outside / "result").exists()


def test_inputs_and_existing_results_cannot_be_scripts_or_overwritten(work):
    (work / "inputs/run.py").write_text("print('untrusted')")
    with pytest.raises(ValueError, match="outside inputs"):
        tools.paths(request(script="inputs/run.py"), work)
    (work / "outputs/result").mkdir(parents=True)
    with pytest.raises(ValueError, match="preserved"):
        tools.paths(request(), work)


def test_gpu_command_keeps_workspace_writable_but_inputs_and_data_read_only(work, monkeypatch):
    devices = tuple(
        Path(p) for p in ("/dev/nvidia0", "/dev/nvidia1", "/dev/nvidiactl", "/dev/nvidia-uvm")
    )
    monkeypatch.setattr(runtime_sandbox, "gpu_devices", lambda: devices)
    environment = Environment("test", sys.executable, True, ("torch",))
    args = tools.command(
        request(), work / "code/run.py", work, work / "data", environment, script_fd=17
    )
    assert all(
        flag in args for flag in ("--unshare-all", "--clearenv", "--proc", "--die-with-parent")
    )
    bindings = [args[i + 1 : i + 3] for i, arg in enumerate(args) if arg == "--dev-bind"]
    assert bindings == [[str(path), str(path)] for path in devices]
    writable = args.index("--bind")
    assert args[writable + 1 : writable + 3] == [str(work), str(work)]
    protected = [args[i + 1] for i, arg in enumerate(args) if arg == "--ro-bind" and i > writable]
    assert str(work / "inputs") in protected and str(work / "data") in protected
    assert not any("KEY" in value or "TOKEN" in value for value in args)
    pinned = args.index("--ro-bind-data")
    assert args[pinned + 1:pinned + 3] == ["17", str(work / "code/run.py")]
    assert pinned < args.index("--") and not any("/proc/self/fd/" in arg for arg in args)
    (work / "inputs").rmdir()
    (work / "inputs").symlink_to(work.parent)
    with pytest.raises(ValueError, match="inputs mount"):
        tools.command(request(), work / "code/run.py", work, work / "data", environment)


def test_opened_directory_remains_bound_after_rename(work):
    (work / "outputs").mkdir()
    fd = tools.open_directory(work, ("outputs",))
    try:
        (work / "outputs").rename(work / "original")
        (work / "outputs").mkdir()
        os.mkdir("result", dir_fd=fd)
        assert (work / "original/result").is_dir()
        assert not (work / "outputs/result").exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "changes", [{"arguments": ["x"] * 101}, {"arguments": [42]}, {"device": "cpu"}]
)
def test_tool_input_is_bounded_and_closed(changes):
    with pytest.raises(ValidationError):
        request(**changes)


@pytest.mark.parametrize("observed", [False, True])
async def test_execution_logs_and_fingerprint_are_bounded_and_gpu_claim_uses_observation(
    work, monkeypatch, observed
):
    source = b"import os; print('x'*20000); print('secret' in os.environ); print('finished')"
    (work / "code/run.py").write_bytes(source)
    monkeypatch.setenv("secret", "must-not-leak")
    environment = Environment("test", sys.executable, True, ("torch",))
    monkeypatch.setattr(tools, "select_environment", lambda *a: environment)
    monkeypatch.setattr(
        tools, "command", lambda *a, script_fd: [sys.executable, f"/dev/fd/{script_fd}"]
    )

    async def observe(used):
        if observed:
            used.add("GPU-actual-context")
        await asyncio.sleep(30)

    monkeypatch.setattr(tools, "observe_devices", observe)
    result = await tools.execute(
        request(), work, work / "data", Settings(_env_file=None, gpu_worker=True)
    )
    assert result["exit_status"] == 0 and not result["timed_out"]
    assert len(result["stdout"].encode()) <= tools.TAIL_BYTES
    assert result["stdout"].endswith("False\nfinished\n")
    assert result["script_sha256"] == hashlib.sha256(source).hexdigest()
    assert result["device"] == ("gpu" if observed else "unverified")
    assert json.loads(result["fingerprint"])["environment"] == "test"
    assert (work / "outputs/result/stdout.log").stat().st_size > tools.TAIL_BYTES


async def test_timeout_kills_the_script_and_reports_partial_output(work, monkeypatch):
    (work / "code/run.py").write_text("import time; print('started', flush=True); time.sleep(30)")
    monkeypatch.setattr(
        tools, "select_environment", lambda *a: Environment("test", sys.executable, True, ())
    )
    monkeypatch.setattr(
        tools, "command", lambda *a, script_fd: [sys.executable, f"/dev/fd/{script_fd}"]
    )
    monkeypatch.setattr(tools, "observe_devices", AsyncMock())
    result = await tools.execute(
        request(),
        work,
        work / "data",
        Settings(_env_file=None, gpu_worker=True).model_copy(update={"max_run_seconds": 1}),
    )
    assert result["timed_out"] and result["exit_status"] != 0
    assert "started" in result["stdout"]


async def test_no_computation_starts_without_durable_saves(work, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(tools, "execute", execute)
    tool = tools.tools(work, work, Settings(_env_file=None, gpu_worker=True))[0]
    result = await tool.handler(request().model_dump())
    assert result["isError"] and "no script ran" in str(result)
    execute.assert_not_awaited()


async def test_failed_computation_is_not_published_as_a_durable_success(work, monkeypatch):
    monkeypatch.setattr(
        tools, "execute", AsyncMock(return_value={"exit_status": 2, "timed_out": False})
    )
    save = AsyncMock()
    tool = tools.tools(work, work, Settings(_env_file=None, gpu_worker=True), save)[0]
    assert (await tool.handler(request().model_dump()))["isError"]
    save.assert_not_awaited()
