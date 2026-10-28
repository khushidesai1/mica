"""Versioned Cellpose tools; only the fixed adapter receives GPU device access."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .runtime_sandbox import sandbox_command

VERSIONS = {
    "3.1.1.3": {"family": "Cellpose 3", "models": ["cyto3"]},
    "4.2.1.1": {"family": "Cellpose-SAM", "models": ["cpsam", "cpsam_v2"]},
}


class SegmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    input_path: str = Field(min_length=1, max_length=2048)
    output_dir: str = Field(min_length=1, max_length=1024)
    version: Literal["3.1.1.3", "4.2.1.1"]
    model: Literal["cyto3", "cpsam", "cpsam_v2"]
    device: Literal["auto", "cpu", "gpu"] = "auto"
    channel_axis: Literal[0, 1, 2] | None = None
    channels: list[int] = Field(default_factory=list, max_length=3)
    diameter: float | None = Field(default=None, gt=0, le=10000)
    batch_size: int = Field(default=1, ge=1, le=16)
    flow_threshold: float = Field(default=0.4, ge=0, le=3)
    cellprob_threshold: float = Field(default=0, ge=-20, le=20)
    min_size: int = Field(default=15, ge=1, le=1000000)


def interpreter(version: str, *, gpu: bool) -> Path:
    if version not in VERSIONS:
        raise ValueError("Choose one of the installed Cellpose versions.")
    if version == "3.1.1.3" and not gpu:
        return Path("/opt/sap/bin/python")
    return Path("/opt/cellpose") / version / "bin/python"


def gpu_devices() -> tuple[Path, ...]:
    required = (Path("/dev/nvidia0"), Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm"))
    optional = Path("/dev/nvidia-uvm-tools")
    for path in required:
        if not path.exists() or not stat.S_ISCHR(path.lstat().st_mode):
            raise ValueError("The approved GPU worker has no usable NVIDIA device.")
    return required + (
        (optional,) if optional.exists() and stat.S_ISCHR(optional.lstat().st_mode) else ()
    )


def command(
    request: SegmentRequest,
    source: Path,
    output: Path,
    settings: Settings,
    *,
    source_fd: int | None = None,
    output_fd: int | None = None,
) -> list[str]:
    gpu = request.device == "gpu" or (request.device == "auto" and settings.gpu_worker)
    if gpu and not settings.gpu_worker:
        raise ValueError("GPU execution requires an approved GPU worker. Request a GPU run.")
    if request.model not in VERSIONS[request.version]["models"]:
        raise ValueError("The requested model does not belong to this Cellpose version.")
    if any(type(v) is not int or v < 0 or v > 255 for v in request.channels):
        raise ValueError("Channel indices must be integers between 0 and 255.")
    if len(set(request.channels)) != len(request.channels):
        raise ValueError("Choose each channel only once.")
    python = interpreter(request.version, gpu=gpu)
    if not python.is_file():
        raise ValueError(f"Cellpose {request.version} is absent from this worker image.")
    script = settings.tools_dir / "cellpose_segment.py"
    models = (
        Path("/opt/sap/models")
        if request.version.startswith("3.")
        else Path("/opt/cellpose/models")
    )
    bound_source = Path("/input/image" + source.suffix) if source_fd is not None else source
    bound_output = Path("/output") if output_fd is not None else output
    args = sandbox_command(
        python.parent.parent,
        [
            str(python),
            "-I",
            str(script),
            "--request",
            json.dumps(
                {
                    **request.model_dump(),
                    "input_path": str(bound_source),
                    "input_label": request.input_path,
                    "output_dir": str(bound_output),
                    "device": "gpu" if gpu else "cpu",
                    "model_dir": str(models),
                }
            ),
        ],
        read_only=(
            *(() if source_fd is not None else (source,)),
            script,
            models,
            Path("/opt/cellpose/manifest.json"),
        ),
        writable=() if output_fd is not None else (output,),
    )
    if source_fd is not None and output_fd is not None:
        index = args.index("--")
        args[index:index] = [
            "--ro-bind",
            f"/proc/self/fd/{source_fd}",
            str(bound_source),
            "--bind",
            f"/proc/self/fd/{output_fd}",
            str(bound_output),
        ]
    if gpu:
        # Preserve network/PID isolation and expose only the one allocated accelerator.
        extra = [part for path in gpu_devices() for part in ("--dev-bind", str(path), str(path))]
        extra += ["--setenv", "LD_LIBRARY_PATH", "/usr/local/nvidia/lib64:/usr/local/nvidia/lib"]
        index = args.index("--")
        args[index:index] = extra
    return args


def paths(request: SegmentRequest, work: Path, data: Path) -> tuple[Path, Path]:
    source = (work / request.input_path).resolve(strict=True)
    if not any(source.is_relative_to(root.resolve()) for root in (work, data)):
        raise ValueError("Choose an image inside this workspace or its project data.")
    if not source.is_file() or source.stat().st_size > 512 * 1024**2:
        raise ValueError("Use one image crop of at most 512 MiB; tile larger images first.")
    relative = Path(request.output_dir)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:1] != ("outputs",)
        or len(relative.parts) < 2
    ):
        raise ValueError("Use a new output folder beneath outputs/.")
    output = work / relative
    if output.resolve() != output.absolute() or not output.resolve().is_relative_to(
        (work / "outputs").resolve()
    ):
        raise ValueError("Output folders cannot traverse symbolic links.")
    if output.exists():
        raise ValueError("Choose a new output folder; previous segmentation results are preserved.")
    return source, output


def open_directory(root: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
    """Walk from a trusted root without following mutable workspace symlinks."""
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


async def segment(
    request: SegmentRequest, work: Path, data: Path, settings: Settings
) -> dict[str, Any]:
    source, output = paths(request, work, data)
    log = output / "execution.log"
    root = next(p.resolve() for p in (work, data) if source.is_relative_to(p.resolve()))
    with ExitStack() as stack:
        parent = open_directory(root, source.relative_to(root).parent.parts)
        stack.callback(os.close, parent)
        source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, source_fd)
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024**2:
            raise ValueError("Image changed during selection; choose a bounded regular image.")
        with os.fdopen(os.dup(source_fd), "rb") as source_stream:
            input_sha256 = hashlib.file_digest(source_stream, "sha256").hexdigest()
        os.lseek(source_fd, 0, os.SEEK_SET)
        parent_out = open_directory(work, output.relative_to(work).parent.parts, create=True)
        stack.callback(os.close, parent_out)
        os.mkdir(output.name, dir_fd=parent_out)
        output_fd = os.open(
            output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_out
        )
        stack.callback(os.close, output_fd)
        args = command(request, source, output, settings, source_fd=source_fd, output_fd=output_fd)
        log_fd = os.open(
            "execution.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=output_fd,
        )
        stream = stack.enter_context(os.fdopen(log_fd, "wb"))
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            pass_fds=(source_fd, output_fd),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=settings.max_run_seconds)
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        if process.returncode:
            raise ValueError(
                f"Cellpose failed; inspect {log.relative_to(work)}. No version fallback was used."
            )
        result_fd = os.open("provenance.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=output_fd)
        with os.fdopen(result_fd, "rb") as metadata:
            raw = metadata.read(65537)
            if len(raw) > 65536:
                raise ValueError("Invalid Cellpose result metadata.")
            result: dict[str, Any] = json.loads(raw)
    if result.get("version") != request.version or result.get("model") != request.model:
        raise ValueError("Cellpose result does not match the requested version and model.")
    return {**result, "input_sha256": input_sha256, "output_dir": str(output.relative_to(work))}


def tools(
    work: Path,
    data: Path,
    settings: Settings,
    save_result: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> list[SdkMcpTool[Any]]:
    @tool("cellpose_versions", "List installed Cellpose versions and models for this worker.", {})
    async def versions(_args: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {
                "version": version,
                **details,
                "installed": interpreter(version, gpu=settings.gpu_worker).is_file(),
            }
            for version, details in VERSIONS.items()
        ]
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "versions": rows,
                            "gpu_worker": settings.gpu_worker,
                            "selection": "Choose an exact version and model per call.",
                        }
                    ),
                }
            ]
        }

    @tool(
        "cellpose_segment",
        "Segment one 2D image crop with an explicit Cellpose version and model. "
        "Writes masks.tif and provenance.json in a new outputs/ folder. Tile larger images first. "
        "Channels are zero-based; for Cellpose 3 select cytoplasm then nucleus.",
        SegmentRequest.model_json_schema(),
    )
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await segment(SegmentRequest.model_validate(args), work, data, settings)
            if save_result:
                try:
                    result["checkpoint"] = await save_result(result)
                except Exception as exc:
                    return {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        **result,
                                    "warning": (
                                        "Segmentation completed but its durable save failed. "
                                        "Keep this output folder and retry checkpoint_stage "
                                        "before more computation."
                                    ),
                                        "save_error": type(exc).__name__,
                                    }
                                ),
                            }
                        ],
                    }
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except (ValueError, OSError, TimeoutError) as exc:
            return {"content": [{"type": "text", "text": str(exc)[:2000]}], "isError": True}

    return [versions, run]
