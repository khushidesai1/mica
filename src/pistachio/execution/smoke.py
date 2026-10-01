"""Bounded protocol-2 deployment check using synthetic inputs and no model calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

from pistachio.config import Settings
from pistachio.execution.development import DevelopmentExecutor
from pistachio.execution.workspace import digest
from pistachio.models import (
    AttachmentRef,
    ExecutionBundle,
    OutputSpec,
    ResourcePlan,
    SourceFile,
    TaskContract,
    ToolInvocation,
    ToolPlan,
)

ANALYSIS = """import argparse
import json
import os
import socket
from pathlib import Path
import igraph
import louvain
import matplotlib.pyplot as plt
from task_runtime import require_memory, positional_rows
from sklearn.metrics import silhouette_score

def total(values):
    return sum(values)

def main():
    parser = argparse.ArgumentParser()
    for key in ('input-dir', 'output-dir', 'config'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--seed', type=int, required=True)
    args = parser.parse_args()
    require_memory(1)
    assert positional_rows([1, 2], 1) == 2
    assert 'PISTACHIO_SMOKE_SECRET' not in os.environ
    assert not Path('/tmp/pistachio-smoke-host-marker').exists()
    assert not Path('/proc/self/environ').exists()
    for directory in (args.input_dir, args.config.parent, Path(__file__).parent):
        try:
            (directory / 'forbidden-write').write_text('x')
        except OSError:
            pass
        else:
            raise AssertionError('read-only mount violated')
    with socket.socket() as connection:
        connection.settimeout(1)
        try:
            connection.connect(('169.254.169.254', 80))
        except OSError:
            pass
        else:
            raise AssertionError('metadata network was reachable')
    values = json.loads((args.input_dir / 'values.json').read_text())
    graph = igraph.Graph.Ring(8)
    partition = louvain.find_partition(
        graph, louvain.RBConfigurationVertexPartition, seed=args.seed
    )
    assert len(partition.membership) == 8
    score = silhouette_score([[0], [1], [9], [10]], [0, 0, 1, 1])
    assert score > 0
    (args.output_dir / 'summary.json').write_text(json.dumps({'answer': total(values)}))
    plt.plot(values)
    plt.savefig(args.output_dir / 'figure.png')
    plt.close()

if __name__ == '__main__':
    main()
"""

TESTS = """import unittest
from analysis import total
class Correctness(unittest.TestCase):
    def test_reference(self):
        self.assertEqual(total([1, 2, 3]), 6)
"""


async def run() -> None:
    os.environ["PISTACHIO_SMOKE_SECRET"] = "synthetic-must-not-reach-analysis"
    marker = Path("/tmp/pistachio-smoke-host-marker")
    marker.write_text("synthetic host marker")
    try:
        with tempfile.TemporaryDirectory(prefix="pistachio-conformance-") as directory:
            root = Path(directory)
            data = root / "values.json"
            data.write_text("[1, 2, 3]")
            import h5py

            coordinates = root / "probe.h5ad"
            with h5py.File(coordinates, "w") as handle:
                frame = handle.create_group("obsm/spatial")
                frame.attrs["encoding-type"] = "dataframe"
                frame.attrs["_index"] = "_index"
                frame.attrs["column-order"] = ["x", "y"]
                for name in ("_index", "x", "y"):
                    frame.create_dataset(name, data=[0, 1, 2])
            settings = Settings(
                _env_file=None,
                env="test",
                artifact_root=root,
                worker_sandbox="bubblewrap",
                worker_input_cache=root / "input-cache",
            )
            files = {"analysis.py": ANALYSIS, "tests/test_analysis.py": TESTS}
            bundle = ExecutionBundle(
                builder_protocol_version=2,
                workflow_version=2,
                task_id="deployment_conformance",
                operation_id="deployment-verify",
                operation_phase="verify",
                attempt=1,
                source_hash=digest(files),
                source_files=[SourceFile(path=k, content=v) for k, v in files.items()],
                script=ANALYSIS,
                runtime_image=os.environ["PISTACHIO_BATCH_IMAGE"],
                command_timeout_seconds=180,
                contract=TaskContract(
                    objective="Deployment conformance",
                    requested_outputs=["summary.json", "figure.png"],
                    requires_execution=True,
                ),
                resource_plan=ResourcePlan(cpu=2, memory_gib=8),
                tool_plan=ToolPlan(
                    invocations=[
                        ToolInvocation(
                            tool_id="scanpy",
                            tool_name="Scanpy",
                            version="1.11.1",
                            entrypoint=name,
                        )
                        for name in ("scanpy.pp.neighbors", "scanpy.tl.louvain")
                    ]
                ),
                output_specs=[
                    OutputSpec(
                        path="summary.json",
                        requested_output="summary.json",
                        format="json",
                        required_fields=["answer"],
                    ),
                    OutputSpec(path="figure.png", requested_output="figure.png", format="png"),
                ],
                input_attachments=[
                    AttachmentRef(
                        filename=path.name,
                        uri=path.as_uri(),
                        size_bytes=path.stat().st_size,
                        checksum_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                    for path in (data, coordinates)
                ],
            )
            executor = DevelopmentExecutor(settings)
            verified = await executor.execute(bundle)
            print(
                json.dumps(
                    {
                        "phase": "verify",
                        "exit_code": verified.exit_code,
                        "summary": verified.summary,
                        "stderr": verified.stderr,
                    }
                ),
                flush=True,
            )
            if verified.exit_code != 0:
                raise RuntimeError("Deployment verification failed")
            probe = json.loads(
                (
                    root / bundle.task_id / "attempt-1" / "evidence" / "runtime.stdout.log"
                ).read_text()
            )
            hdf = next(item for item in probe["inputs"] if item["name"] == "probe.h5ad")
            assert hdf["obsm_structure"]["spatial"]["representation"] == "pandas.DataFrame"
            assert len(probe["api_capabilities"]) == 4
            assert all(item["available"] for item in probe["api_capabilities"])
            print("INITIAL_QUALITY_RUNTIME_PROBE_PASSED", flush=True)
            full = bundle.model_copy(
                update={
                    "attempt": 2,
                    "operation_id": "deployment-run",
                    "operation_phase": "run",
                    "verification": verified.verification,
                }
            )
            result = await executor.execute(full)
            print(
                json.dumps(
                    {
                        "phase": "run",
                        "exit_code": result.exit_code,
                        "summary": result.summary,
                        "stderr": result.stderr,
                        "artifacts": [x.name for x in result.artifacts],
                    }
                ),
                flush=True,
            )
            if result.exit_code != 0 or not result.publication_committed:
                raise RuntimeError("Deployment full execution failed")
            assert {x.name for x in result.artifacts} == {
                "figure.png",
                "summary.json",
                "reproduction.zip",
            }
            assert result.timings["input_cache_hit"] == 1
            assert result.timings["runtime_cache_hit"] == 1
            assert "tests_seconds" not in result.timings
            print("LATENCY_WORKFLOW_CONFORMANCE_PASSED", flush=True)
            print("PROTOCOL2_WORKER_CONFORMANCE_PASSED", flush=True)
    finally:
        marker.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(run())
