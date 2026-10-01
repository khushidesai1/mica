"""Revision-frozen development operations in Docker or a credential-free Linux namespace."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from pistachio.config import Settings
from pistachio.execution.backends import (
    LocalExecutor,
    ProgressCallback,
    parse_progress_line,
    sha256_file,
)
from pistachio.execution.checkpoints import CHECKPOINT_HELPER, checkpoint_key
from pistachio.execution.reproduction import create_reproduction
from pistachio.execution.workspace import digest, safe_relative, validate_sources
from pistachio.models import ExecutionBundle, FailureReport, TaskResult, VerificationReport, utcnow


class OperationFailed(RuntimeError):
    def __init__(self, phase: str, category: str, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.phase = phase
        self.category = category
        self.exit_code = exit_code


def effective_seed(bundle: ExecutionBundle) -> int:
    seeds = [
        p.proposed_value
        for p in bundle.parameter_manifest
        if p.id.rsplit(".", 1)[-1].lower() in {"seed", "random_seed", "random_state"}
    ]
    if not seeds:
        return 0
    if any(not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in seeds):
        raise ValueError("seed must be an explicitly resolved nonnegative integer")
    # Per-procedure distinct seeds are preserved in config; bootstrap uses the first.
    first = seeds[0]
    assert isinstance(first, int)
    if first >= 2**32:
        raise ValueError("bootstrap seed exceeds the supported NumPy seed range")
    return first


class DevelopmentExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.deadlines: dict[str, float] = {}
        self.callbacks: dict[str, ProgressCallback] = {}
        self.timings: dict[str, dict[str, float]] = {}
        self.timestamps: dict[str, dict[str, datetime]] = {}
        self.active: dict[str, tuple[asyncio.subprocess.Process, str]] = {}

    async def cancel(self, task_id: str) -> bool:
        tracked = self.active.get(task_id)
        if tracked is None:
            return False
        await self._stop(*tracked)
        return True

    async def _stop(self, process: asyncio.subprocess.Process, container: str) -> None:
        if container:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                container,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(cleanup.wait(), timeout=10)
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    async def execute(
        self, bundle: ExecutionBundle, progress_callback: ProgressCallback | None = None
    ) -> TaskResult:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", bundle.task_id) or bundle.attempt < 1:
            raise ValueError("invalid operation workspace identity")
        if progress_callback:
            self.callbacks[bundle.operation_id] = progress_callback
        root = (
            self.settings.artifact_root / bundle.task_id / f"attempt-{bundle.attempt}"
        ).resolve()
        # An operation cannot depend on files left by a prior process. Durable committed results
        # may be returned, but partial attempts are quarantined before another local execution.
        commit = root / "publication.json"
        if commit.exists():
            saved = json.loads(commit.read_text())
            if saved["bundle_hash"] != digest(bundle.model_dump(mode="json")):
                raise ValueError("attempt already committed with a different bundle")
            for relative, checksum in saved["checksums"].items():
                if sha256_file(root / safe_relative(relative)) != checksum:
                    raise ValueError("committed artifact integrity failure")
            return TaskResult.model_validate_json((root / "operation-result.json").read_text())
        if root.exists():
            quarantine = root.with_name(root.name + f"-interrupted-{time.time_ns()}")
            root.rename(quarantine)
        for subdir in (
            "source",
            "inputs",
            "config",
            "outputs",
            "scratch",
            "evidence",
            "trusted",
            "checkpoint_inputs",
            "checkpoints",
        ):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        self.deadlines[bundle.operation_id] = time.monotonic() + bundle.command_timeout_seconds
        self.timings[bundle.operation_id] = {}
        self.timestamps[bundle.operation_id] = {}
        checks: list[str] = []
        result = TaskResult(
            summary="Operation did not complete.",
            operation_id=bundle.operation_id,
            source_hash=bundle.source_hash,
            exit_code=1,
            started_at=utcnow(),
        )
        try:
            files = validate_sources(bundle.source_files)
            if digest(files) != bundle.source_hash:
                raise OperationFailed("staging", "validation", "source revision checksum mismatch")
            for relative, content in files.items():
                target = root / "source" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                if relative.endswith(".py"):
                    compile(content, relative, "exec")
            checks.append("source integrity and compilation")
            await self.milestone(bundle, "staging", "Staging verified input data")
            staged_at = time.monotonic()
            if bundle.workflow_version == 2 and self.settings.worker_input_cache:
                from pistachio.execution.input_cache import input_cache

                input_checksums, timing = await asyncio.to_thread(
                    input_cache(self.settings.worker_input_cache).stage, bundle, root / "inputs"
                )
                self.timings[bundle.operation_id].update(timing)
            else:
                await asyncio.to_thread(LocalExecutor._stage_inputs, bundle, root / "inputs")
                self.timings[bundle.operation_id]["download_seconds"] = time.monotonic() - staged_at
                hashed_at = time.monotonic()
                input_checksums = await asyncio.to_thread(
                    lambda: {p.name: sha256_file(p) for p in (root / "inputs").iterdir()}
                )
                self.timings[bundle.operation_id]["hashing_seconds"] = time.monotonic() - hashed_at
            parameters = {p.id: p.proposed_value for p in bundle.parameter_manifest}
            checkpoint_keys = {
                spec.id: checkpoint_key(
                    spec, files, parameters, digest(input_checksums), bundle.runtime_image
                )
                for spec in bundle.checkpoint_specs
            }
            if bundle.checkpoint_inputs:
                from pistachio.services.artifacts import _materialize

                for artifact in bundle.checkpoint_inputs:
                    for checkpoint_id, key in checkpoint_keys.items():
                        if artifact.name == f"checkpoints/{key}/{checkpoint_id}.json":
                            destination = root / "checkpoint_inputs" / f"{checkpoint_id}.json"
                            await asyncio.to_thread(
                                _materialize, artifact, destination, self.settings
                            )
            config: dict[str, object] = {
                "parameters": parameters,
                "runtime_entrypoints": [x.entrypoint for x in bundle.tool_plan.invocations],
                "checkpoint_keys": checkpoint_keys,
                "checkpoint_specs": [x.model_dump(mode="json") for x in bundle.checkpoint_specs],
                "figures": [f.model_dump(mode="json") for f in bundle.figure_specs],
                "output_specs": [x.model_dump(mode="json") for x in bundle.output_specs],
            }
            if bundle.workflow_version == 2:
                config["resources"] = {
                    "memory_gib": bundle.resource_plan.memory_gib,
                    "cpu": bundle.resource_plan.cpu,
                    "host_telemetry_optional": True,
                }
                # Required standard APIs must be probed even when tool invocations are empty.
                config["runtime_entrypoints"] = sorted(
                    {x.entrypoint for x in bundle.tool_plan.invocations}
                    | {
                        "scanpy.pp.neighbors",
                        "scanpy.tl.louvain",
                        "sklearn.metrics.silhouette_score",
                        "scipy.spatial.cKDTree.query",
                    }
                )
            config_path = root / "config" / "effective_parameters.json"
            config_path.write_text(json.dumps(config, sort_keys=True, indent=2))
            shutil.copyfile(Path(__file__).with_name("checks.py"), root / "trusted" / "checks.py")
            (root / "trusted" / "task_checkpoints.py").write_text(CHECKPOINT_HELPER)
            shutil.copyfile(
                Path(__file__).with_name("runtime_support.py"), root / "trusted" / "task_runtime.py"
            )
            seed = effective_seed(bundle)
            # Unit tests and analysis use a common RNG bootstrap in a protected file.
            (root / "trusted" / "bootstrap.py").write_text(
                "import os, random, runpy, sys\n"
                "seed = int(os.environ['PISTACHIO_SEED'])\nrandom.seed(seed)\n"
                "try:\n import numpy as np\n np.random.seed(seed)\nexcept ImportError:\n pass\n"
                "target = sys.argv.pop(1)\nsys.path.insert(0, os.getcwd())\n"
                "sys.argv[0] = target\nrunpy.run_path(target, run_name='__main__')\n"
            )
            runtime_cache = None
            if bundle.workflow_version == 2 and self.settings.worker_input_cache:
                key = digest(
                    [
                        bundle.runtime_image,
                        input_checksums,
                        config["runtime_entrypoints"],
                        validation_suite_hash(),
                    ]
                )
                runtime_cache = self.settings.worker_input_cache / "runtime" / (key + ".json")
            if runtime_cache and runtime_cache.is_file():
                (root / "evidence/runtime.stdout.log").write_text(runtime_cache.read_text())
                self.timings[bundle.operation_id]["runtime_cache_hit"] = 1
            else:
                await self._call(bundle, root, "runtime", ["/trusted/checks.py", "probe"])
                if runtime_cache:
                    runtime_cache.parent.mkdir(parents=True, exist_ok=True)
                    runtime_cache.write_text((root / "evidence/runtime.stdout.log").read_text())
            runtime = json.loads((root / "evidence/runtime.stdout.log").read_text())
            if bundle.workflow_version == 2:
                result.runtime_evidence = {
                    k: runtime[k]
                    for k in ("inputs", "api_capabilities", "sandbox", "python", "architecture")
                    if k in runtime
                }
            checks.append("runtime and real input schema probe")
            if bundle.operation_phase != "probe":
                if "analysis.py" not in files or not any(x.startswith("tests/test") for x in files):
                    raise OperationFailed(
                        "tests", "validation", "analysis.py and regression tests are required"
                    )
                # unittest discovery is invoked by a trusted harness that rejects zero tests.
                (root / "trusted" / "tests.py").write_text(
                    "import sys, unittest\nsys.path.insert(0, '/source')\n"
                    "suite = unittest.defaultTestLoader.discover('tests')\n"
                    "if suite.countTestCases() == 0:\n"
                    " raise SystemExit('No regression tests discovered')\n"
                    "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
                    "raise SystemExit(not result.wasSuccessful())\n"
                )
                if bundle.workflow_version != 2 or bundle.operation_phase != "run":
                    await self._call(bundle, root, "tests", ["/trusted/tests.py"], bootstrap=True)
                    checks.append("production function regression tests")
                if bundle.operation_phase in {"test", "verify"}:
                    await self._call(
                        bundle,
                        root,
                        "smoke",
                        ["/source/analysis.py", "--mode", "smoke"],
                        bootstrap=True,
                    )
                    checks.append("bounded real-input end-to-end smoke execution")
                elif bundle.operation_phase == "run":
                    expected = bundle.verification
                    if (
                        not expected
                        or not expected.passed
                        or expected.source_hash != bundle.source_hash
                        or (
                            bundle.workflow_version == 2
                            and expected.validation_suite_hash != validation_suite_hash()
                        )
                    ):
                        raise OperationFailed(
                            "verification",
                            "validation",
                            "full run requires matching clean verification",
                        )
                    if (
                        expected.input_hash != digest(input_checksums)
                        or expected.config_hash != digest(config)
                        or expected.runtime_image != bundle.runtime_image
                    ):
                        raise OperationFailed(
                            "verification",
                            "validation",
                            "verification inputs/config/runtime changed",
                        )
                    await self._call(
                        bundle,
                        root,
                        "analysis",
                        ["/source/analysis.py", "--mode", "run"],
                        bootstrap=True,
                    )
                    checks.append("full analysis execution")
                await self._call(
                    bundle,
                    root,
                    "output-validation",
                    ["/trusted/checks.py", "outputs"],
                    outputs_readonly=True,
                )
                report = json.loads((root / "evidence/output-validation.stdout.log").read_text())
                checks.extend(report["checks"])
            result.exit_code = 0
            result.summary = f"{bundle.operation_phase} completed and required checks passed."
            result.verification = VerificationReport(
                operation_id=bundle.operation_id,
                source_hash=bundle.source_hash,
                input_hash=digest(input_checksums),
                config_hash=digest(config),
                runtime_image=bundle.runtime_image,
                validation_suite_hash=validation_suite_hash()
                if bundle.workflow_version == 2
                else "",
                checks=checks,
                passed=True,
                replay_coverage=(
                    "clean smoke replay verified"
                    if bundle.operation_phase in {"verify", "run"}
                    else "development checks only"
                ),
                limitations=[
                    "Scientific correctness requires task-specific tests and review.",
                    "Automated artifact checks; no visual review or full independent replay.",
                    "Configuration is read-only; arbitrary code use is not proven.",
                ],
            )
            if bundle.operation_phase == "run":
                for path in sorted((root / "outputs").rglob("*")):
                    if path.is_file():
                        artifact = LocalExecutor._artifact(path)
                        artifact.name = path.relative_to(root / "outputs").as_posix()
                        result.artifacts.append(artifact)
                manifest: dict[str, object] = {
                    "developer_model": bundle.developer_model,
                    "prompt_hash": bundle.prompt_hash,
                    "trusted_checksums": {
                        p.name: sha256_file(p) for p in (root / "trusted").glob("*.py")
                    },
                    "runtime_image": bundle.runtime_image,
                    "seed": seed,
                    "cpu": bundle.resource_plan.cpu,
                    "memory_gib": bundle.resource_plan.memory_gib,
                    "timeout_seconds": bundle.command_timeout_seconds,
                    "input_checksums": input_checksums,
                    "source_checksums": {
                        k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()
                    },
                    "config_checksum": sha256_file(config_path),
                    "runtime": runtime,
                }
                archive = create_reproduction(bundle, result, root, manifest)
                result.artifacts.append(LocalExecutor._artifact(archive))
                result.summary = (
                    "Analysis completed; outputs validated and reproduction bundle prepared."
                )
        except Exception as exc:
            category = exc.category if isinstance(exc, OperationFailed) else "environment"
            phase = exc.phase if isinstance(exc, OperationFailed) else "staging"
            result.exit_code = exc.exit_code if isinstance(exc, OperationFailed) else 1
            result.failure = FailureReport(
                phase=phase,
                category=category,
                message=str(exc),
                diagnostic_ref="evidence/",
                fingerprint=digest([phase, category, str(exc)]),
            )
            result.summary = f"{phase} failed: {exc}"
        # Failed downstream runs may still contain complete, schema-valid producer checkpoints.
        checkpoint_outputs = root / "scratch" / "checkpoints"
        if bundle.operation_phase == "run" and checkpoint_outputs.exists():
            try:
                declared = {spec.id: spec for spec in bundle.checkpoint_specs}
                for path in checkpoint_outputs.glob("*.json"):
                    if path.is_symlink() or path.stem not in declared:
                        raise ValueError("undeclared checkpoint output")
                    spec = declared[path.stem]
                    # JSON is data-only; bound size before parsing. Reject nonfinite values.
                    if path.stat().st_size > self.settings.development_artifact_bytes:
                        raise ValueError("checkpoint exceeds disk budget")

                    def reject_constant(value: str) -> None:
                        raise ValueError("nonfinite checkpoint value: " + value)

                    value = json.loads(path.read_text(), parse_constant=reject_constant)
                    if not isinstance(value, dict) or not set(spec.required_fields) <= value.keys():
                        raise ValueError("checkpoint schema mismatch")
                    key = checkpoint_keys[spec.id]
                    target = root / "checkpoints" / key / path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            except Exception as exc:
                (root / "evidence/checkpoints.stderr.log").write_text(str(exc))
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        for path in sorted((root / "evidence").glob("*.log")):
            text = path.read_text(errors="replace")
            (stdout_parts if ".stdout." in path.name else stderr_parts).append(
                f"[{path.name}]\n{text[-12000:]}"
            )
        result.stdout = "\n".join(stdout_parts)[-24000:]
        result.stderr = "\n".join(stderr_parts)[-24000:]
        result.output_text = result.stdout[-8000:]
        if bundle.queued_at and result.started_at:
            self.timings[bundle.operation_id]["queue_and_startup_seconds"] = max(
                0, (result.started_at - bundle.queued_at).total_seconds()
            )
            self.timestamps[bundle.operation_id]["queued"] = bundle.queued_at
            self.timestamps[bundle.operation_id]["worker_started"] = result.started_at
        result.timings = self.timings.pop(bundle.operation_id, {})
        result.phase_timestamps = self.timestamps.pop(bundle.operation_id, {})
        result.completed_at = utcnow()
        result.effective_parameters = bundle.parameter_manifest
        result.figure_specs = bundle.figure_specs
        result.tool_usage = bundle.tool_plan.invocations
        evidence = root / "evidence" / "verification.json"
        evidence.write_text(result.model_dump_json(indent=2))
        result.audit_artifacts = [
            LocalExecutor._artifact(p) for p in (root / "evidence").iterdir() if p.is_file()
        ]
        for item in result.audit_artifacts:
            item.name = "evidence/" + item.name
        if result.exit_code == 0 and bundle.operation_phase in {"test", "verify"}:
            for path in sorted((root / "outputs").rglob("*")):
                if path.is_file() and not path.is_symlink():
                    artifact = LocalExecutor._artifact(path)
                    artifact.name = "preview/" + path.relative_to(root / "outputs").as_posix()
                    result.audit_artifacts.append(artifact)
        for path in sorted((root / "checkpoints").rglob("*.json")):
            artifact = LocalExecutor._artifact(path)
            artifact.name = path.relative_to(root).as_posix()
            result.audit_artifacts.append(artifact)
        # The local commit is written last. The cloud collector creates its own durable commit.
        result.publication_committed = True
        (root / "operation-result.json").write_text(result.model_dump_json(indent=2))
        checksums = {
            p.relative_to(root).as_posix(): sha256_file(p)
            for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and "scratch" not in p.relative_to(root).parts
        }
        commit.write_text(
            json.dumps(
                {"bundle_hash": digest(bundle.model_dump(mode="json")), "checksums": checksums},
                sort_keys=True,
            )
        )
        self.callbacks.pop(bundle.operation_id, None)
        self.deadlines.pop(bundle.operation_id, None)
        return result

    async def milestone(self, bundle: ExecutionBundle, phase: str, message: str) -> None:
        self.timestamps[bundle.operation_id][phase] = utcnow()
        callback = self.callbacks.get(bundle.operation_id)
        if callback and bundle.workflow_version == 2:
            from pistachio.models import TaskProgress

            with suppress(Exception):
                await callback(
                    TaskProgress(
                        attempt=bundle.attempt,
                        status="started",
                        message=message,
                        item=phase,
                        operation_id=bundle.operation_id,
                        worker_session_id=bundle.worker_session_id,
                        trusted=True,
                    )
                )

    async def _call(
        self,
        bundle: ExecutionBundle,
        root: Path,
        label: str,
        args: list[str],
        *,
        bootstrap: bool = False,
        outputs_readonly: bool = False,
    ) -> None:
        await self.milestone(
            bundle,
            label,
            {
                "runtime": "Checking runtime and input structure",
                "tests": "Validating candidate tests",
                "smoke": "Validating the bounded production pipeline",
                "analysis": "Running full analysis",
                "output-validation": "Validating output artifacts",
            }.get(label, label),
        )
        phase_started = time.monotonic()
        common = [
            "--input-dir",
            "/inputs",
            "--output-dir",
            "/outputs",
            "--config",
            "/config/effective_parameters.json",
        ]
        if label not in {"tests"}:
            args = [*args, *common]
        if label in {"analysis", "smoke"}:
            args.extend(["--seed", str(effective_seed(bundle))])
        if bootstrap:
            args = ["/trusted/bootstrap.py", *args]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            # Python's slim image records /usr/local/lib in /etc/ld.so.cache. The namespace
            # deliberately omits /etc, so expose the approved image's library path explicitly.
            "LD_LIBRARY_PATH": "/usr/local/lib",
            "HOME": "/tmp",
            "MPLCONFIGDIR": "/tmp/mpl",
            "MPLBACKEND": "Agg",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONHASHSEED": str(effective_seed(bundle)),
            "PISTACHIO_SEED": str(effective_seed(bundle)),
            "PISTACHIO_CONFIG": "/config/effective_parameters.json",
            "PYTHONPATH": "/trusted",
            "PISTACHIO_CHECKPOINT_INPUTS": "/checkpoint_inputs",
            "PISTACHIO_CHECKPOINT_OUTPUTS": "/scratch/checkpoints",
            "OMP_NUM_THREADS": str(bundle.resource_plan.cpu),
            "OPENBLAS_NUM_THREADS": str(bundle.resource_plan.cpu),
        }
        mode = self.settings.worker_sandbox
        container = ""
        if mode == "test":
            if self.settings.env != "test":
                raise OperationFailed(label, "environment", "unisolated runner is test-only")
            remap = {
                "/" + x: str(root / x)
                for x in (
                    "trusted",
                    "source",
                    "inputs",
                    "outputs",
                    "config",
                    "scratch",
                    "checkpoint_inputs",
                )
            }
            args = [
                next(
                    (
                        arg.replace(k, v, 1)
                        for k, v in remap.items()
                        if arg == k or arg.startswith(k + "/")
                    ),
                    arg,
                )
                for arg in args
            ]
            for name in (
                "PISTACHIO_CONFIG",
                "PISTACHIO_CHECKPOINT_INPUTS",
                "PISTACHIO_CHECKPOINT_OUTPUTS",
            ):
                env[name] = next(
                    (
                        env[name].replace(k, v, 1)
                        for k, v in remap.items()
                        if env[name] == k or env[name].startswith(k + "/")
                    ),
                    env[name],
                )
            command = [sys.executable, *args]
            env["PATH"] = os.environ.get("PATH", "")
            env["HOME"] = str(root / "scratch")
            env["PYTHONPATH"] = str(root / "trusted")
        elif mode == "docker":
            image = bundle.runtime_image
            pinned = re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image)
            local_digest = self.settings.env in {"local", "test"} and re.fullmatch(
                r"sha256:[a-f0-9]{64}", image
            )
            if not (pinned or local_digest):
                raise OperationFailed(
                    label, "environment", "sandbox requires a digest-pinned runtime image"
                )
            container = (
                "pistachio-"
                + hashlib.sha256((bundle.operation_id + label).encode()).hexdigest()[:24]
            )
            command = [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--user={os.getuid()}:{os.getgid()}",
                "--pids-limit=256",
                f"--memory={bundle.resource_plan.memory_gib}g",
                f"--cpus={bundle.resource_plan.cpu}",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
                "-w",
                "/source",
            ]
            mount_root = root
            if self.settings.docker_artifact_root:
                mount_root = (
                    self.settings.docker_artifact_root
                    / bundle.task_id
                    / f"attempt-{bundle.attempt}"
                )
            for name in (
                "source",
                "trusted",
                "inputs",
                "config",
                "outputs",
                "scratch",
                "checkpoint_inputs",
            ):
                writable = name == "scratch" or (name == "outputs" and not outputs_readonly)
                command.extend(["-v", f"{mount_root / name}:/{name}:{'rw' if writable else 'ro'}"])
            for key, val in env.items():
                command.extend(["-e", f"{key}={val}"])
            command.extend(["--entrypoint=python", image, *args])
        elif mode == "bubblewrap":
            # Fail closed if user namespaces are disabled by the host. Never fall back to local.
            command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
            for base in ("/usr", "/bin", "/lib", "/lib64", "/opt"):
                if Path(base).exists():
                    command.extend(["--ro-bind", base, base])
            # Nested containers can prohibit mounting proc even in a new PID namespace.
            # Leave it absent; binding the host's proc would expose worker processes/secrets.
            command.extend(["--dev", "/dev", "--tmpfs", "/tmp"])
            for name in (
                "source",
                "trusted",
                "inputs",
                "config",
                "outputs",
                "scratch",
                "checkpoint_inputs",
            ):
                writable = name == "scratch" or (name == "outputs" and not outputs_readonly)
                command.extend(
                    ["--bind" if writable else "--ro-bind", str(root / name), "/" + name]
                )
            for key, val in env.items():
                command.extend(["--setenv", key, val])
            command.extend(["--chdir", "/source", "python", *args])
            command = [
                "prlimit",
                f"--as={bundle.resource_plan.memory_gib * 1024**3}",
                "--nproc=256",
                f"--fsize={self.settings.development_artifact_bytes}",
                "--",
                *command,
            ]
        else:
            raise OperationFailed(label, "environment", "unknown sandbox backend")
        started = time.monotonic()
        limit = max(0.01, self.deadlines[bundle.operation_id] - started)
        stdout = (root / "evidence" / f"{label}.stdout.log").open("wb")
        stderr = (root / "evidence" / f"{label}.stderr.log").open("wb")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=root / "source",
            env=env if mode == "test" else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.active[bundle.task_id] = (process, container)

        async def drain(stream: asyncio.StreamReader | None, handle: object) -> None:
            total = 0
            pending = bytearray()
            if stream:
                while chunk := await stream.read(65536):
                    total += len(chunk)
                    if total > 10_000_000:
                        await self._stop(process, container)
                        raise OperationFailed(label, "resource", "log budget exceeded")
                    handle.write(chunk)  # type: ignore[attr-defined]
                    callback = self.callbacks.get(bundle.operation_id)
                    if callback and handle is stdout:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            raw, _, remainder = pending.partition(b"\n")
                            pending = bytearray(remainder)
                            progress = parse_progress_line(
                                raw.decode(errors="replace"), bundle.attempt
                            )
                            if progress:
                                with suppress(Exception):
                                    await callback(progress)
                        if len(pending) > 65536:
                            pending.clear()

        async def monitor() -> None:
            while process.returncode is None:
                size = sum(
                    p.lstat().st_size
                    for folder in ("outputs", "scratch")
                    for p in (root / folder).rglob("*")
                    if not p.is_symlink()
                )
                if size > self.settings.development_artifact_bytes:
                    raise OperationFailed(label, "resource", "workspace disk budget exceeded")
                if time.monotonic() - started > limit:
                    raise OperationFailed(label, "resource", "command timeout", 124)
                await asyncio.sleep(0.1)

        jobs = [
            asyncio.create_task(drain(process.stdout, stdout)),
            asyncio.create_task(drain(process.stderr, stderr)),
            asyncio.create_task(monitor()),
        ]
        try:
            await asyncio.gather(process.wait(), *jobs)
            if process.returncode:
                category = "resource" if process.returncode in {-9, 137} else "code"
                if label == "output-validation":
                    category = "validation"
                raise OperationFailed(
                    label, category, f"{label} exited with {process.returncode}", process.returncode
                )
        finally:
            await self._stop(process, container)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self.active.pop(bundle.task_id, None)
            stdout.close()
            stderr.close()
            self.timings[bundle.operation_id][label + "_seconds"] = time.monotonic() - phase_started


def validation_suite_hash() -> str:
    return digest(
        {
            name: Path(__file__).with_name(name).read_text()
            for name in ("checks.py", "development.py", "runtime_support.py")
        }
    )
