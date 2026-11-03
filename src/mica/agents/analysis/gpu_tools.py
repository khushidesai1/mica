"""General Python GPU execution inside the offline, unprivileged worker sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import BaseModel, ConfigDict, Field

from .checkpoints import Stages
from .config import Settings
from .runtime_sandbox import sandbox_command
from .toolkit_catalog import Environment, environments

if TYPE_CHECKING:
    from .runtime_install import RuntimeRecord

TAIL_BYTES = 12_000
MAX_SCRIPT_BYTES = 8 * 1024**2


class RunGPU(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    environment: str = Field(min_length=1, max_length=100)
    script: str = Field(min_length=1, max_length=2048)
    arguments: list[Annotated[str, Field(max_length=4096)]] = Field(
        default_factory=list, max_length=100
    )
    output_dir: str = Field(min_length=1, max_length=1024)
    input_fingerprint: str = Field(default="", max_length=700)


def select_environment(
    name: str, settings: Settings, records: Iterable[RuntimeRecord] = ()
) -> Environment:
    if not settings.gpu_worker:
        raise ValueError("GPU execution requires an approved GPU worker.")
    selected = next((row for row in environments(settings, records) if row.id == name), None)
    if selected is None:
        raise ValueError("Choose an installed environment id from the toolkit, not a path.")
    if not selected.cuda:
        raise ValueError("This environment has no CUDA runtime. Choose a CUDA-enabled environment.")
    if not Path(selected.python).is_file():
        raise ValueError("This environment is not installed here. Restore it with ensure_packages.")
    return selected


def paths(request: RunGPU, work: Path) -> tuple[Path, Path]:
    work = work.resolve(strict=True)
    source = (work / request.script).resolve(strict=True)
    if not source.is_relative_to(work) or source.is_relative_to(work / "inputs"):
        raise ValueError("Choose a Python script in this workspace, outside inputs/.")
    if not source.is_file() or source.stat().st_size > MAX_SCRIPT_BYTES:
        raise ValueError("Choose a regular Python script of at most 8 MiB.")
    relative = Path(request.output_dir)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:1] != ("outputs",)
        or len(relative.parts) < 2
        or not Stages.valid_path(str(relative), outputs=True)
    ):
        raise ValueError("Use a new output folder beneath outputs/.")
    output = work / relative
    if output.resolve() != output.absolute():
        raise ValueError("Output folders cannot traverse symbolic links.")
    if output.exists():
        raise ValueError("Choose a new output folder; previous results are preserved.")
    return source, output


def open_directory(root: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
    """Walk a trusted workspace without following mutable directory symlinks."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def command(
    request: RunGPU,
    source: Path,
    work: Path,
    data: Path,
    environment: Environment,
    *,
    script_fd: int | None = None,
) -> list[str]:
    inputs = work / "inputs"
    if inputs.is_symlink() or (inputs.exists() and not inputs.is_dir()):
        raise ValueError("The inputs mount must be a regular workspace directory.")
    args = sandbox_command(
        Path(environment.python).parent.parent,
        [environment.python, str(source), *request.arguments],
        read_only=(data, *(Path(path) for path in environment.assets)),
        writable=(work,),
        protected=(work / "inputs", data),
        cwd=work,
        gpu=True,
    )
    if script_fd is not None:
        index = args.index("--")
        args[index:index] = ["--ro-bind", f"/proc/self/fd/{script_fd}", str(source)]
    return args


async def observe_devices(used: set[str]) -> None:
    """Observe actual CUDA contexts on this exclusive worker; never infer use from allocation."""
    binary = next(
        (
            p
            for p in (Path("/usr/bin/nvidia-smi"), Path("/usr/local/nvidia/bin/nvidia-smi"))
            if p.is_file()
        ),
        None,
    )
    if binary is None:
        return
    while True:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(binary),
                "--query-compute-apps=gpu_uuid",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
            output, _ = await asyncio.wait_for(process.communicate(), 5)
            if process.returncode == 0:
                used.update(
                    line.strip()
                    for line in output.decode().splitlines()
                    if line.strip().startswith("GPU-")
                )
        except (OSError, TimeoutError, UnicodeError):
            return  # The result explicitly distinguishes unobserved usage.
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        await asyncio.sleep(1)


def tail(fd: int) -> str:
    os.lseek(fd, max(0, os.fstat(fd).st_size - TAIL_BYTES), os.SEEK_SET)
    return os.read(fd, TAIL_BYTES).decode(errors="replace")


async def execute(
    request: RunGPU,
    work: Path,
    data: Path,
    settings: Settings,
    records: Iterable[RuntimeRecord] = (),
) -> dict[str, Any]:
    environment = select_environment(request.environment, settings, records)
    work = work.resolve(strict=True)
    source, output = paths(request, work)
    with ExitStack() as stack:
        parent = open_directory(work, source.relative_to(work).parent.parts)
        stack.callback(os.close, parent)
        source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, source_fd)
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCRIPT_BYTES:
            raise ValueError("The script changed during selection; choose a bounded regular file.")
        # Execute the bytes whose fingerprint we return, even if another tool edits the script.
        snapshot = stack.enter_context(tempfile.TemporaryFile())
        with os.fdopen(os.dup(source_fd), "rb") as stream:
            code = stream.read(MAX_SCRIPT_BYTES + 1)
        if len(code) > MAX_SCRIPT_BYTES:
            raise ValueError("The script exceeds the size limit.")
        snapshot.write(code)
        snapshot.flush()
        snapshot.seek(0)
        parent_out = open_directory(work, output.relative_to(work).parent.parts, create=True)
        stack.callback(os.close, parent_out)
        os.mkdir(output.name, dir_fd=parent_out)
        output_fd = os.open(
            output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_out
        )
        stack.callback(os.close, output_fd)
        streams = []
        for name in ("stdout.log", "stderr.log"):
            fd = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=output_fd
            )
            streams.append(fd)
            stack.callback(os.close, fd)
        args = command(request, source, work, data, environment, script_fd=snapshot.fileno())
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=streams[0],
            stderr=streams[1],
            start_new_session=True,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            pass_fds=(snapshot.fileno(),),
        )
        used: set[str] = set()
        observer = asyncio.create_task(observe_devices(used))
        timed_out = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=settings.max_run_seconds)
            except TimeoutError:
                timed_out = True
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            observer.cancel()
            with suppress(asyncio.CancelledError):
                await observer
        script_hash = hashlib.sha256(code).hexdigest()
        fingerprint = json.dumps(
            {
                "environment": environment.id,
                "script_sha256": script_hash,
                "arguments_sha256": hashlib.sha256(
                    json.dumps(request.arguments).encode()
                ).hexdigest(),
                "inputs": request.input_fingerprint,
            },
            sort_keys=True,
        )
        return {
            "environment": environment.id,
            "script_sha256": script_hash,
            "exit_status": process.returncode,
            "timed_out": timed_out,
            "stdout": tail(streams[0]),
            "stderr": tail(streams[1]),
            "observed_gpu_devices": sorted(used),
            "device": "gpu" if used else "unverified",
            "device_note": "Observed CUDA contexts"
            if used
            else ("No GPU context was observed. Check script provenance before claiming GPU use."),
            "output_dir": str(output.relative_to(work)),
            "fingerprint": fingerprint,
        }


def tools(
    work: Path,
    data: Path,
    settings: Settings,
    save_result: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    records: Callable[[], Iterable[RuntimeRecord]] = lambda: (),
) -> list[SdkMcpTool[Any]]:
    if not settings.gpu_worker:
        return []
    lock = asyncio.Lock()

    @tool(
        "run_gpu",
        "Run a workspace Python script in a named CUDA environment on this approved GPU worker. "
        "No network or credentials; project data and inputs are read-only. Choose a new outputs/ "
        "folder. Returns exit status, bounded log tails and observed GPU devices. Successful "
        "output folders save durably. Bash remains CPU-only. Include input versions and method "
        "parameters in input_fingerprint and check method provenance before claiming GPU use.",
        RunGPU.model_json_schema(),
    )
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            async with lock:
                if save_result is None:
                    raise ValueError("Durable saves are unavailable on this worker; no script ran.")
                result = await execute(RunGPU.model_validate(args), work, data, settings, records())
                if result["exit_status"] == 0 and not result["timed_out"]:
                    try:
                        result["checkpoint"] = await save_result(result)
                    except Exception as exc:
                        result.update(
                            warning="Computation completed but its durable save failed. Keep this "
                            "output folder and retry checkpoint_stage before more computation.",
                            save_error=type(exc).__name__,
                        )
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": bool(
                    result["exit_status"] or result["timed_out"] or result.get("save_error")
                ),
            }
        except (ValueError, OSError, TimeoutError) as exc:
            return {"content": [{"type": "text", "text": str(exc)[:2000]}], "isError": True}

    return [run]
