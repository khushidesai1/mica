"""Local, Docker, and Google Cloud Batch execution backends."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import mimetypes
import os
import re
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pistachio.config import Settings
from pistachio.execution.validation import validate_script
from pistachio.models import ArtifactRef, ExecutionBundle, TaskProgress, TaskResult

MACHINE_TYPES = {
    "cpu-small": "e2-standard-2",
    "cpu-medium": "e2-standard-8",
    "memory-large": "n2-highmem-16",
    "gpu-l4": "g2-standard-8",
}

PROGRESS_PREFIX = "PISTACHIO_PROGRESS "
ProgressCallback = Callable[[TaskProgress], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BatchJobStatus:
    state: str
    detail: str = ""


@runtime_checkable
class BatchStatusBackend(Protocol):
    async def get_job_status(self, job_name: str) -> BatchJobStatus: ...

    async def load_job_result(self, task_id: str, attempt: int) -> TaskResult: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ExecutionBackend(ABC):
    @abstractmethod
    async def execute(
        self,
        bundle: ExecutionBundle,
        progress_callback: ProgressCallback | None = None,
    ) -> TaskResult:
        raise NotImplementedError

    async def cancel(self, task_id: str, job_name: str | None = None) -> bool:
        """Best-effort cancellation for an active execution."""
        del task_id, job_name
        return False


def parse_progress_line(line: str, attempt: int) -> TaskProgress | None:
    """Parse the deliberately narrow stdout protocol used by generated jobs."""
    if not line.startswith(PROGRESS_PREFIX):
        return None
    raw = line.removeprefix(PROGRESS_PREFIX).strip()
    if len(raw) > 4_000:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        progress = TaskProgress.model_validate(
            {
                **payload,
                "attempt": attempt,
                "trusted": False,
                "eta_low_seconds": None,
                "eta_high_seconds": None,
                "eta_basis": "",
                "operation_id": "",
                "worker_session_id": "",
            }
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        progress.current is not None
        and progress.total is not None
        and progress.current > progress.total
    ):
        return None
    return progress


async def _read_process_stream(
    stream: asyncio.StreamReader | None,
    *,
    attempt: int,
    progress_callback: ProgressCallback | None = None,
) -> bytes:
    if stream is None:
        return b""
    output = bytearray()
    pending = bytearray()
    while chunk := await stream.read(64 * 1024):
        output.extend(chunk)
        if progress_callback is None:
            continue
        pending.extend(chunk)
        while b"\n" in pending:
            raw_line, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            progress = parse_progress_line(
                raw_line.decode("utf-8", errors="replace").rstrip("\r"), attempt
            )
            if progress is not None:
                with suppress(Exception):
                    await progress_callback(progress)
    if pending and progress_callback is not None:
        progress = parse_progress_line(pending.decode("utf-8", errors="replace"), attempt)
        if progress is not None:
            with suppress(Exception):
                await progress_callback(progress)
    return bytes(output)


async def _communicate_with_progress(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
    attempt: int,
    progress_callback: ProgressCallback | None,
) -> tuple[bytes, bytes]:
    progress_queue: asyncio.Queue[TaskProgress | None] | None = None
    progress_consumer: asyncio.Task[None] | None = None
    stream_callback: ProgressCallback | None = None
    if progress_callback is not None:
        progress_queue = asyncio.Queue()

        async def enqueue(progress: TaskProgress) -> None:
            if progress_queue is not None:
                progress_queue.put_nowait(progress)

        async def consume() -> None:
            if progress_queue is None or progress_callback is None:
                return
            while (progress := await progress_queue.get()) is not None:
                with suppress(Exception):
                    await progress_callback(progress)

        stream_callback = enqueue
        progress_consumer = asyncio.create_task(consume())
    stdout_task = asyncio.create_task(
        _read_process_stream(
            process.stdout,
            attempt=attempt,
            progress_callback=stream_callback,
        )
    )
    stderr_task = asyncio.create_task(_read_process_stream(process.stderr, attempt=attempt))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        if progress_queue is not None:
            progress_queue.put_nowait(None)
        if progress_consumer is not None:
            await progress_consumer
        raise TimeoutError("generated task exceeded its resource timeout") from None
    stdout_raw, stderr_raw = await asyncio.gather(stdout_task, stderr_task)
    if progress_queue is not None:
        progress_queue.put_nowait(None)
    if progress_consumer is not None:
        await progress_consumer
    return stdout_raw, stderr_raw


class LocalExecutor(ExecutionBackend):
    """Development executor; production must use Docker or Batch isolation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        from pistachio.execution.development import DevelopmentExecutor

        self.development_executor = DevelopmentExecutor(settings)

    async def _communicate_tracked(
        self,
        task_id: str,
        process: asyncio.subprocess.Process,
        *,
        timeout: float,
        attempt: int,
        progress_callback: ProgressCallback | None,
    ) -> tuple[bytes, bytes]:
        self._active_processes[task_id] = process
        try:
            return await _communicate_with_progress(
                process,
                timeout=timeout,
                attempt=attempt,
                progress_callback=progress_callback,
            )
        finally:
            if self._active_processes.get(task_id) is process:
                self._active_processes.pop(task_id, None)

    async def cancel(self, task_id: str, job_name: str | None = None) -> bool:
        del job_name
        if await self.development_executor.cancel(task_id):
            return True
        process = self._active_processes.get(task_id)
        if process is None or process.returncode is not None:
            return False
        process.kill()
        return True

    async def execute(
        self,
        bundle: ExecutionBundle,
        progress_callback: ProgressCallback | None = None,
    ) -> TaskResult:
        if bundle.builder_protocol_version == 2:
            return await self.development_executor.execute(bundle, progress_callback)
        validate_script(bundle.script)
        task_dir = self.settings.artifact_root / bundle.task_id / f"attempt-{bundle.attempt}"
        output_dir = task_dir / "outputs"
        input_dir = task_dir / "inputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._stage_inputs, bundle, input_dir)
        script_path = task_dir / "task.py"
        contract_path = task_dir / "task_contract.json"
        manifest_path = task_dir / "context_manifest.json"
        script_path.write_text(bundle.script, encoding="utf-8")
        (task_dir / "execution_bundle.json").write_text(
            bundle.model_dump_json(indent=2), encoding="utf-8"
        )
        contract_path.write_text(bundle.contract.model_dump_json(indent=2), encoding="utf-8")
        (task_dir / "resource_plan.json").write_text(
            bundle.resource_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        (task_dir / "tool_plan.json").write_text(
            bundle.tool_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        installed = sorted(
            f"{distribution.metadata['Name']}=={distribution.version}"
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        )
        (task_dir / "environment.lock").write_text(
            "\n".join(
                [
                    f"python=={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    f"container_image={self.settings.batch_image or 'local-process'}",
                    *installed,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(
                [entry.model_dump(mode="json") for entry in bundle.context_manifest], indent=2
            ),
            encoding="utf-8",
        )
        runtime_dir = task_dir / "runtime"
        home_dir = runtime_dir / "home"
        config_dir = runtime_dir / "config"
        cache_dir = runtime_dir / "cache"
        matplotlib_dir = runtime_dir / "matplotlib"
        for directory in (home_dir, config_dir, cache_dir, matplotlib_dir):
            directory.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home_dir.resolve()),
            "XDG_CONFIG_HOME": str(config_dir.resolve()),
            "XDG_CACHE_HOME": str(cache_dir.resolve()),
            "MPLCONFIGDIR": str(matplotlib_dir.resolve()),
            "MPLBACKEND": "Agg",
            "PYTHONHASHSEED": "0",
            "PISTACHIO_OUTPUT_DIR": str(output_dir.resolve()),
            "PISTACHIO_INPUT_DIR": str((task_dir / "inputs").resolve()),
        }
        started = datetime.now(UTC)
        command = [
            sys.executable,
            str(script_path.resolve()),
            "--input-dir",
            str(input_dir.resolve()),
            "--output-dir",
            str(output_dir.resolve()),
            "--seed",
            "0",
        ]
        if "--preflight" in bundle.script:
            preflight = await asyncio.create_subprocess_exec(
                *command,
                "--preflight",
                cwd=task_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                preflight_stdout_raw, preflight_stderr_raw = await self._communicate_tracked(
                    bundle.task_id,
                    preflight,
                    timeout=min(bundle.resource_plan.timeout_hours * 3600, 300),
                    attempt=bundle.attempt,
                    progress_callback=None,
                )
                preflight_returncode = preflight.returncode or 0
            except TimeoutError as exc:
                preflight_stdout_raw = b""
                preflight_stderr_raw = str(exc).encode()
                preflight_returncode = 124
            preflight_stdout = preflight_stdout_raw.decode("utf-8", errors="replace")
            preflight_stderr = preflight_stderr_raw.decode("utf-8", errors="replace")
            (task_dir / "preflight.stdout.log").write_text(preflight_stdout, encoding="utf-8")
            (task_dir / "preflight.stderr.log").write_text(preflight_stderr, encoding="utf-8")
            emitted = [path.name for path in output_dir.rglob("*") if path.is_file()]
            if preflight_returncode != 0 or emitted:
                artifact_error = (
                    "Preflight must not write user artifacts: " + ", ".join(emitted)
                    if emitted
                    else ""
                )
                diagnostic = "\n".join(
                    part
                    for part in (
                        "Real-data preflight failed before full analysis.",
                        preflight_stderr,
                        artifact_error,
                    )
                    if part
                )
                result = TaskResult(
                    summary="Real-data preflight failed before full analysis.",
                    output_text=preflight_stdout[-8_000:],
                    stdout=preflight_stdout,
                    stderr=diagnostic,
                    tool_usage=bundle.tool_plan.invocations,
                    effective_parameters=bundle.parameter_manifest,
                    figure_specs=bundle.figure_specs,
                    exit_code=preflight_returncode or 1,
                    started_at=started,
                )
                self._write_execution_metadata(bundle, task_dir / "metadata")
                (task_dir / "result.json").write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )
                return result
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=task_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_raw, stderr_raw = await self._communicate_tracked(
            bundle.task_id,
            process,
            timeout=bundle.resource_plan.timeout_hours * 3600,
            attempt=bundle.attempt,
            progress_callback=progress_callback,
        )
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        (task_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (task_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        self._write_execution_metadata(bundle, task_dir / "metadata")
        if process.returncode == 0:
            shutil.copyfile(script_path, output_dir / "generated_analysis.py")
        artifacts = [self._artifact(path) for path in output_dir.rglob("*") if path.is_file()]
        summary = (
            f"Execution completed with {len(artifacts)} output artifact(s)."
            if process.returncode == 0
            else f"Execution failed with exit code {process.returncode}."
        )
        result = TaskResult(
            summary=summary,
            output_text=stdout[-8_000:],
            artifacts=artifacts,
            stdout=stdout,
            stderr=stderr,
            tool_usage=bundle.tool_plan.invocations,
            effective_parameters=bundle.parameter_manifest,
            figure_specs=bundle.figure_specs,
            exit_code=process.returncode,
            started_at=started,
        )
        (task_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    @staticmethod
    def _artifact(path: Path) -> ArtifactRef:
        return ArtifactRef(
            name=path.name,
            uri=path.resolve().as_uri(),
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            size_bytes=path.stat().st_size,
            checksum_sha256=sha256_file(path),
        )

    @staticmethod
    def _write_execution_metadata(bundle: ExecutionBundle, metadata_dir: Path) -> None:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        if bundle.tool_plan.invocations:
            (metadata_dir / "effective_hyperparameters.json").write_text(
                json.dumps(
                    [item.model_dump(mode="json") for item in bundle.tool_plan.invocations],
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if bundle.parameter_manifest:
            (metadata_dir / "parameter_manifest.json").write_text(
                json.dumps(
                    [item.model_dump(mode="json") for item in bundle.parameter_manifest],
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if bundle.figure_specs:
            (metadata_dir / "figure_specs.json").write_text(
                json.dumps(
                    [item.model_dump(mode="json") for item in bundle.figure_specs],
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _stage_inputs(bundle: ExecutionBundle, input_dir: Path) -> None:
        staged_names: set[str] = set()
        attachment_uris = {item.uri for item in bundle.input_attachments}
        for attachment in bundle.input_attachments:
            target = input_dir / Path(attachment.filename).name
            if target.name in staged_names:
                raise ValueError(f"duplicate input filename: {target.name}")
            staged_names.add(target.name)
            if attachment.uri.startswith("file://"):
                source = Path(attachment.uri.removeprefix("file://")).resolve()
                if source != target.resolve():
                    shutil.copyfile(source, target)
            elif attachment.uri.startswith("gs://"):
                from google.cloud.storage import Client

                bucket_name, _, blob_name = attachment.uri.removeprefix("gs://").partition("/")
                Client().bucket(bucket_name).blob(blob_name).download_to_filename(
                    target,
                    if_generation_match=(
                        int(attachment.generation) if attachment.generation else None
                    ),
                )
            else:
                raise ValueError(f"unsupported input URI: {attachment.uri}")
            digest = sha256_file(target)
            if digest != attachment.checksum_sha256:
                raise ValueError(f"input checksum mismatch: {attachment.filename}")
        for dataset in bundle.dataset_inputs:
            if dataset.uri in attachment_uris:
                continue
            if dataset.inspection_error:
                raise ValueError(
                    f"dataset could not be staged: {dataset.uri}: {dataset.inspection_error}"
                )
            if not dataset.uri.startswith("gs://") or not dataset.generation:
                raise ValueError(f"dataset input is not generation-pinned: {dataset.uri}")
            bucket_name, separator, blob_name = dataset.uri.removeprefix("gs://").partition("/")
            filename = Path(blob_name).name
            if not separator or not bucket_name or not filename.lower().endswith(".h5ad"):
                raise ValueError(f"unsupported dataset input URI: {dataset.uri}")
            if filename in staged_names:
                raise ValueError(f"duplicate input filename: {filename}")
            staged_names.add(filename)

            from google.cloud.storage import Client

            generation = int(dataset.generation)
            blob = Client().bucket(bucket_name).blob(blob_name, generation=generation)
            blob.reload(if_generation_match=generation)
            if int(blob.generation or 0) != generation:
                raise ValueError(f"dataset generation changed: {dataset.uri}")
            remote_checksum = blob.md5_hash or blob.crc32c or ""
            if dataset.checksum and remote_checksum != dataset.checksum:
                raise ValueError(f"dataset checksum changed: {dataset.uri}")
            if dataset.size_bytes and int(blob.size or 0) != dataset.size_bytes:
                raise ValueError(f"dataset size changed: {dataset.uri}")
            target = input_dir / filename
            blob.download_to_filename(target, if_generation_match=generation)
            if dataset.size_bytes and target.stat().st_size != dataset.size_bytes:
                raise ValueError(f"downloaded dataset size mismatch: {dataset.uri}")


class DockerExecutor(LocalExecutor):
    """Docker executor with read-only root, no network, and dropped capabilities."""

    async def execute(
        self,
        bundle: ExecutionBundle,
        progress_callback: ProgressCallback | None = None,
    ) -> TaskResult:
        if bundle.builder_protocol_version == 2:
            return await self.development_executor.execute(bundle, progress_callback)
        validate_script(bundle.script)
        task_dir = (
            self.settings.artifact_root / bundle.task_id / f"attempt-{bundle.attempt}"
        ).resolve()
        output_dir = task_dir / "outputs"
        input_dir = task_dir / "inputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._stage_inputs, bundle, input_dir)
        (task_dir / "task.py").write_text(bundle.script, encoding="utf-8")
        (task_dir / "task_contract.json").write_text(
            bundle.contract.model_dump_json(indent=2), encoding="utf-8"
        )
        (task_dir / "execution_bundle.json").write_text(
            bundle.model_dump_json(indent=2), encoding="utf-8"
        )
        image = self.settings.batch_image or "python:3.12-slim"
        (task_dir / "resource_plan.json").write_text(
            bundle.resource_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        (task_dir / "tool_plan.json").write_text(
            bundle.tool_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        (task_dir / "context_manifest.json").write_text(
            json.dumps(
                [entry.model_dump(mode="json") for entry in bundle.context_manifest], indent=2
            ),
            encoding="utf-8",
        )
        (task_dir / "environment.lock").write_text(f"container_image={image}\n", encoding="utf-8")
        mount_source = task_dir
        if self.settings.docker_artifact_root:
            mount_source = (
                self.settings.docker_artifact_root / bundle.task_id / f"attempt-{bundle.attempt}"
            ).resolve()
        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={os.getuid()}:{os.getgid()}",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            "-e",
            "PYTHONHASHSEED=0",
            "-e",
            "HOME=/tmp/pistachio-home",
            "-e",
            "XDG_CONFIG_HOME=/tmp/pistachio-config",
            "-e",
            "XDG_CACHE_HOME=/tmp/pistachio-cache",
            "-e",
            "MPLCONFIGDIR=/tmp/pistachio-matplotlib",
            "-e",
            "MPLBACKEND=Agg",
            "-e",
            "PISTACHIO_OUTPUT_DIR=/work/outputs",
            "-v",
            f"{mount_source}:/work:rw",
            "--entrypoint=python",
            image,
            "/work/task.py",
            "--input-dir",
            "/work/inputs",
            "--output-dir",
            "/work/outputs",
            "--seed",
            "0",
        ]
        started = datetime.now(UTC)
        if "--preflight" in bundle.script:
            preflight = await asyncio.create_subprocess_exec(
                *command,
                "--preflight",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                preflight_stdout_raw, preflight_stderr_raw = await self._communicate_tracked(
                    bundle.task_id,
                    preflight,
                    timeout=min(bundle.resource_plan.timeout_hours * 3600, 300),
                    attempt=bundle.attempt,
                    progress_callback=None,
                )
                preflight_returncode = preflight.returncode or 0
            except TimeoutError as exc:
                preflight_stdout_raw = b""
                preflight_stderr_raw = str(exc).encode()
                preflight_returncode = 124
            preflight_stdout = preflight_stdout_raw.decode(errors="replace")
            preflight_stderr = preflight_stderr_raw.decode(errors="replace")
            (task_dir / "preflight.stdout.log").write_text(preflight_stdout, encoding="utf-8")
            (task_dir / "preflight.stderr.log").write_text(preflight_stderr, encoding="utf-8")
            emitted = [path.name for path in output_dir.rglob("*") if path.is_file()]
            if preflight_returncode != 0 or emitted:
                artifact_error = (
                    "Preflight must not write user artifacts: " + ", ".join(emitted)
                    if emitted
                    else ""
                )
                diagnostic = "\n".join(
                    part
                    for part in (
                        "Real-data preflight failed before full analysis.",
                        preflight_stderr,
                        artifact_error,
                    )
                    if part
                )
                result = TaskResult(
                    summary="Real-data preflight failed before full analysis.",
                    output_text=preflight_stdout[-8_000:],
                    stdout=preflight_stdout,
                    stderr=diagnostic,
                    tool_usage=bundle.tool_plan.invocations,
                    effective_parameters=bundle.parameter_manifest,
                    figure_specs=bundle.figure_specs,
                    exit_code=preflight_returncode or 1,
                    started_at=started,
                )
                self._write_execution_metadata(bundle, task_dir / "metadata")
                (task_dir / "result.json").write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )
                return result
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_raw, stderr_raw = await self._communicate_tracked(
            bundle.task_id,
            process,
            timeout=bundle.resource_plan.timeout_hours * 3600,
            attempt=bundle.attempt,
            progress_callback=progress_callback,
        )
        stdout = stdout_raw.decode(errors="replace")
        stderr = stderr_raw.decode(errors="replace")
        self._write_execution_metadata(bundle, task_dir / "metadata")
        if process.returncode == 0:
            shutil.copyfile(task_dir / "task.py", output_dir / "generated_analysis.py")
        artifacts = [self._artifact(path) for path in output_dir.rglob("*") if path.is_file()]
        result = TaskResult(
            summary=(
                f"Docker execution completed with {len(artifacts)} artifact(s)."
                if process.returncode == 0
                else f"Docker execution failed with exit code {process.returncode}."
            ),
            output_text=stdout[-8_000:],
            artifacts=artifacts,
            stdout=stdout,
            stderr=stderr,
            tool_usage=bundle.tool_plan.invocations,
            effective_parameters=bundle.parameter_manifest,
            figure_specs=bundle.figure_specs,
            exit_code=process.returncode,
            started_at=started,
        )
        (task_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (task_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        (task_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result


class GcpBatchExecutor(ExecutionBackend):
    """Submit immutable execution bundles to Google Cloud Batch."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not all(
            (
                settings.gcp_project,
                settings.gcs_bucket,
                settings.batch_service_account,
                settings.batch_image,
            )
        ):
            raise ValueError("GCP Batch settings are incomplete")

    async def execute(
        self,
        bundle: ExecutionBundle,
        progress_callback: ProgressCallback | None = None,
        *,
        retry_index: int = 0,
        worker_session: dict[str, Any] | None = None,
    ) -> TaskResult:
        del progress_callback
        if bundle.builder_protocol_version == 1:
            validate_script(bundle.script)
        elif bundle.runtime_image not in (
            self.settings.batch_image,
            *self.settings.batch_compatible_images,
        ):
            raise ValueError("operation runtime differs from configured pinned worker image")
        runtime_image = (
            bundle.runtime_image
            if bundle.builder_protocol_version == 2
            else self.settings.batch_image
        )
        nested_sandbox = (
            bundle.builder_protocol_version == 2 and self.settings.batch_nested_sandbox_opt_in
        )
        if nested_sandbox and self.settings.env not in {"staging", "test"}:
            raise ValueError("experimental nested sandbox launch is restricted to staging")
        from google.cloud import batch_v1
        from google.cloud.storage import Client as StorageClient

        # Older pinned workers must receive their original wire contract, including nested reports.
        excluded: dict[str, Any] = {}
        if bundle.workflow_version != 2:
            excluded = {
                k: True
                for k in (
                    "workflow_version",
                    "worker_session_id",
                    "worker_session_deadline",
                    "queued_at",
                )
            }
            excluded["verification"] = {"validation_suite_hash"}
        payload = bundle.model_dump_json(indent=2, exclude=excluded)
        bundle_digest = hashlib.sha256(payload.encode()).hexdigest()

        def submit() -> str:
            from google.api_core.exceptions import AlreadyExists, PreconditionFailed

            storage_client = StorageClient(project=self.settings.gcp_project)
            blob = storage_client.bucket(self.settings.gcs_bucket).blob(
                f"tasks/{bundle.task_id}/attempt-{bundle.attempt}/execution_bundle.json"
            )
            try:
                blob.upload_from_string(
                    payload, content_type="application/json", if_generation_match=0
                )
            except PreconditionFailed:
                existing_payload = blob.download_as_text()
                if hashlib.sha256(existing_payload.encode()).hexdigest() != bundle_digest:
                    raise RuntimeError(
                        "an execution bundle already exists for this attempt with different content"
                    ) from None
            bundle_uri = f"gs://{self.settings.gcs_bucket}/{blob.name}"

            if worker_session:
                from pistachio.execution.session import write_manifest

                write_manifest(
                    storage_client.bucket(self.settings.gcs_bucket),
                    bundle.task_id,
                    {
                        **worker_session,
                        "sequence": bundle.attempt,
                        "bundle_uri": bundle_uri,
                        "operation_id": bundle.operation_id,
                        "source_hash": bundle.source_hash,
                        "stop": False,
                    },
                )
            runnable = batch_v1.Runnable()
            runnable.container = batch_v1.Runnable.Container(
                image_uri=runtime_image,
                commands=[
                    "python",
                    "-m",
                    "pistachio.worker.main",
                    "--bundle",
                    bundle_uri,
                ]
                if not worker_session
                else [
                    "python",
                    "-m",
                    "pistachio.worker.main",
                    "--session-task",
                    bundle.task_id,
                    "--session-id",
                    worker_session["session_id"],
                ],
                volumes=[],
                # The trusted protocol-2 worker creates its own user/mount/network namespaces.
                # An explicit staging-only opt-in permits namespace setup blocked by Docker's
                # outer profiles. The image stays unprivileged and inner checks fail closed.
                options="--security-opt seccomp=unconfined --security-opt apparmor=unconfined"
                if nested_sandbox
                else "",
            )
            runnable.environment = batch_v1.Environment(
                variables={
                    "HOME": "/tmp/pistachio-home",
                    "XDG_CONFIG_HOME": "/tmp/pistachio-config",
                    "XDG_CACHE_HOME": "/tmp/pistachio-cache",
                    "MPLCONFIGDIR": "/tmp/pistachio-matplotlib",
                    "MPLBACKEND": "Agg",
                    "PISTACHIO_GCS_BUCKET": self.settings.gcs_bucket,
                    "PISTACHIO_GCP_PROJECT": self.settings.gcp_project,
                    "PISTACHIO_COMPLETION_TOPIC": self.settings.completion_topic,
                    "PISTACHIO_BATCH_IMAGE": runtime_image,
                    "PISTACHIO_WORKER_SESSION_ID": worker_session["session_id"]
                    if worker_session
                    else "",
                    "PISTACHIO_SESSION_IDLE_SECONDS": str(
                        self.settings.worker_session_idle_seconds
                    ),
                }
            )
            task_spec = batch_v1.TaskSpec(
                runnables=[runnable],
                compute_resource=batch_v1.ComputeResource(
                    cpu_milli=bundle.resource_plan.cpu * 1000,
                    memory_mib=bundle.resource_plan.memory_gib * 1024,
                ),
                max_run_duration=(
                    f"{
                        max(
                            1,
                            int(
                                (
                                    datetime.fromisoformat(worker_session['deadline'])
                                    - datetime.now(UTC)
                                ).total_seconds()
                            ),
                        )
                    }s"
                )
                if worker_session
                else f"{int(bundle.command_timeout_seconds + 300)}s"
                if bundle.builder_protocol_version == 2
                else f"{int(bundle.resource_plan.timeout_hours * 3600)}s",
                max_retry_count=(2 if bundle.resource_plan.checkpointable else 0)
                if bundle.builder_protocol_version == 1
                else 0,
            )
            group = batch_v1.TaskGroup(task_count=1, task_spec=task_spec)
            policy = batch_v1.AllocationPolicy.InstancePolicy(
                machine_type=MACHINE_TYPES[bundle.resource_plan.profile.value],
                provisioning_model=(
                    batch_v1.AllocationPolicy.ProvisioningModel.SPOT
                    if bundle.resource_plan.provisioning_model.value == "SPOT"
                    else batch_v1.AllocationPolicy.ProvisioningModel.STANDARD
                ),
                boot_disk=batch_v1.AllocationPolicy.Disk(
                    type_="pd-balanced",
                    size_gb=bundle.resource_plan.disk_gib,
                ),
            )
            instance = batch_v1.AllocationPolicy.InstancePolicyOrTemplate(policy=policy)
            network_name = self.settings.batch_network
            if not network_name.startswith("projects/"):
                network_name = (
                    f"projects/{self.settings.gcp_project}/global/networks/{network_name}"
                )
            subnetwork_name = self.settings.batch_subnetwork
            if not subnetwork_name.startswith("projects/"):
                subnetwork_name = (
                    f"projects/{self.settings.gcp_project}/regions/{self.settings.gcp_region}/"
                    f"subnetworks/{subnetwork_name}"
                )
            allocation = batch_v1.AllocationPolicy(
                instances=[instance],
                service_account=batch_v1.ServiceAccount(email=self.settings.batch_service_account),
                network=batch_v1.AllocationPolicy.NetworkPolicy(
                    network_interfaces=[
                        batch_v1.AllocationPolicy.NetworkInterface(
                            network=network_name,
                            subnetwork=subnetwork_name,
                            no_external_ip_address=True,
                        )
                    ]
                ),
            )
            job = batch_v1.Job(
                task_groups=[group],
                allocation_policy=allocation,
                labels={
                    "pistachio-attempt": str(bundle.attempt),
                    "pistachio-bundle": bundle_digest[:16],
                },
                logs_policy=batch_v1.LogsPolicy(
                    destination=batch_v1.LogsPolicy.Destination.CLOUD_LOGGING
                ),
            )
            client = batch_v1.BatchServiceClient()
            parent = f"projects/{self.settings.gcp_project}/locations/{self.settings.gcp_region}"
            suffix = f"-a{bundle.attempt}-{bundle_digest[:8]}-r{retry_index}"
            if bundle.builder_protocol_version == 1:
                suffix = f"-a{bundle.attempt}-{bundle_digest[:8]}"
            if worker_session:
                suffix = "-s" + worker_session["session_id"][:16]
                job.labels.clear()
                job.labels["pistachio-session"] = worker_session["session_id"]
            normalized = re.sub(r"[^a-z0-9-]", "-", bundle.task_id.lower()).strip("-")
            job_id = f"{normalized[: 63 - len(suffix)].rstrip('-')}{suffix}"
            try:
                response = client.create_job(parent=parent, job_id=job_id, job=job)
            except AlreadyExists:
                response = client.get_job(name=f"{parent}/jobs/{job_id}")
                labels = dict(response.labels)
                if (
                    worker_session
                    and labels.get("pistachio-session") != worker_session["session_id"]
                ):
                    raise RuntimeError("existing Batch session identity mismatch") from None
                if not worker_session and (
                    labels.get("pistachio-attempt") != str(bundle.attempt)
                    or labels.get("pistachio-bundle") != bundle_digest[:16]
                ):
                    raise RuntimeError(
                        "an existing Batch job does not match the reserved execution attempt"
                    ) from None
            return response.name

        job_name = await asyncio.to_thread(submit)
        return TaskResult(
            summary=f"Submitted Google Cloud Batch job {job_name}.",
            output_text=job_name,
            exit_code=None,
            started_at=datetime.now(UTC),
        )

    async def get_job_status(self, job_name: str) -> BatchJobStatus:
        from google.cloud import batch_v1

        def fetch() -> BatchJobStatus:
            job = batch_v1.BatchServiceClient().get_job(name=job_name)
            state = batch_v1.JobStatus.State(job.status.state).name
            details = [event.description for event in job.status.status_events if event.description]
            return BatchJobStatus(state=state, detail=max(details, key=len) if details else "")

        return await asyncio.to_thread(fetch)

    async def cancel(self, task_id: str, job_name: str | None = None) -> bool:
        await self.stop_session(task_id)
        if not job_name:
            return False
        from google.cloud import batch_v1

        await asyncio.to_thread(batch_v1.BatchServiceClient().delete_job, name=job_name)
        return True

    async def stop_session(self, task_id: str, session_id: str = "") -> None:
        from google.cloud.storage import Client

        from pistachio.execution.session import stop_manifest

        await asyncio.to_thread(
            stop_manifest,
            Client(project=self.settings.gcp_project).bucket(self.settings.gcs_bucket),
            task_id,
            session_id,
        )

    async def load_job_result(self, task_id: str, attempt: int) -> TaskResult:
        from google.cloud.storage import Client as StorageClient

        def fetch() -> TaskResult:
            payload = (
                StorageClient(project=self.settings.gcp_project)
                .bucket(self.settings.gcs_bucket)
                .blob(f"tasks/{task_id}/attempt-{attempt}/worker-result.json")
                .download_as_text()
            )
            result = TaskResult.model_validate_json(payload)
            if result.operation_id:
                from pistachio.execution.publication import verify_cloud_commit

                commit = verify_cloud_commit(
                    StorageClient(project=self.settings.gcp_project).bucket(
                        self.settings.gcs_bucket
                    ),
                    task_id,
                    attempt,
                    result,
                    payload,
                )
                if "publication_seconds" in commit:
                    result.timings["publication_seconds"] = commit["publication_seconds"]
            return result

        return await asyncio.to_thread(fetch)


def build_executor(settings: Settings) -> ExecutionBackend:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    if settings.execution_backend == "docker":
        if shutil.which("docker") is None:
            raise RuntimeError("docker executable is unavailable")
        return DockerExecutor(settings)
    if settings.execution_backend == "gcp_batch":
        return GcpBatchExecutor(settings)
    if settings.env in {"staging", "production"}:
        raise RuntimeError("local execution is forbidden outside local/test environments")
    return LocalExecutor(settings)
