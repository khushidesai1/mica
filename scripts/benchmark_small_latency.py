"""Compare legacy/reused validation on bounded aggregation and clustering fixtures.

Run inside the pinned scientific worker with the normal bubblewrap sandbox. This measures
worker operation overhead, not model quality or separate Batch startup times.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from pistachio.config import Settings
from pistachio.execution.development import DevelopmentExecutor
from pistachio.execution.smoke import ANALYSIS, TESTS
from pistachio.execution.workspace import digest
from pistachio.models import (
    AttachmentRef,
    ExecutionBundle,
    OutputSpec,
    ResourcePlan,
    SourceFile,
    TaskContract,
    ToolPlan,
)

# Workflow one did not inject resource metadata into config. Supply the actual fixed
# benchmark allocation explicitly so both versions exercise the same memory check.
BENCHMARK_ANALYSIS = ANALYSIS.replace(
    "require_memory(1)", "require_memory(1, {'resources': {'memory_gib': 8}})"
)


async def benchmark() -> None:
    results = []
    for version in (1, 2):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "values.json"
            data.write_text("[1, 2, 3]")
            files = {"analysis.py": BENCHMARK_ANALYSIS, "tests/test_analysis.py": TESTS}
            bundle = ExecutionBundle(
                builder_protocol_version=2,
                workflow_version=version,
                task_id=f"small_latency_v{version}",
                source_hash=digest(files),
                source_files=[SourceFile(path=k, content=v) for k, v in files.items()],
                script=BENCHMARK_ANALYSIS,
                runtime_image=os.environ["PISTACHIO_BATCH_IMAGE"],
                command_timeout_seconds=180,
                contract=TaskContract(
                    objective="Aggregation, Louvain and Silhouette bounded benchmark",
                    requested_outputs=["summary.json", "figure.png"],
                    requires_execution=True,
                ),
                resource_plan=ResourcePlan(cpu=2, memory_gib=8),
                tool_plan=ToolPlan(),
                output_specs=[
                    OutputSpec(
                        path="summary.json", requested_output="summary.json", format="json",
                        required_fields=["answer"],
                    ),
                    OutputSpec(path="figure.png", requested_output="figure.png", format="png"),
                ],
                input_attachments=[AttachmentRef(
                    filename=data.name, uri=data.as_uri(), size_bytes=data.stat().st_size,
                    checksum_sha256=hashlib.sha256(data.read_bytes()).hexdigest(),
                )],
            )
            settings = Settings(
                _env_file=None, env="test", artifact_root=root, worker_sandbox="bubblewrap",
                worker_input_cache=root / "cache" if version == 2 else None,
            )
            executor = DevelopmentExecutor(settings)
            phases = ("probe", "test", "verify", "run") if version == 1 else (
                "probe", "verify", "run"
            )
            verification = None
            operations = []
            started = time.monotonic()
            for attempt, phase in enumerate(phases, 1):
                operation = bundle.model_copy(update={
                    "attempt": attempt, "operation_id": f"{version}-{phase}",
                    "operation_phase": phase, "verification": verification,
                })
                result = await executor.execute(operation)
                if result.exit_code:
                    raise RuntimeError(f"{version}/{phase}: {result.summary}: {result.stderr}")
                operations.append({"phase": phase, "timings": result.timings})
                if phase == "verify":
                    verification = result.verification
            artifacts = {
                a.name: a.checksum_sha256
                for a in result.artifacts if a.name in {"summary.json", "figure.png"}
            }
            results.append({
                "workflow_version": version, "elapsed_seconds": time.monotonic() - started,
                "operations": operations, "artifact_hashes": artifacts,
            })
    assert results[0]["artifact_hashes"] == results[1]["artifact_hashes"]
    print("SMALL_LATENCY_BENCHMARK " + json.dumps({
        "results": results, "scientific_outputs_identical": True,
        "includes_model_calls": False, "includes_batch_startups": False,
    }), flush=True)


if __name__ == "__main__":
    asyncio.run(benchmark())
