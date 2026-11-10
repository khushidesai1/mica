"""Offline, bounded native Proseg gate; synthetic data is not scientific validation."""

import csv
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from mica.agents.analysis.runtime_sandbox import PROBE, sandbox_command

EXECUTABLE = Path("/opt/proseg/3.1.1/bin/proseg")
TUNING = (
    "--prior-seg-reassignment-prob", "0.25",
    "--nuclear-reassignment-prob", "0.15",
    "--enforce-connectivity", "--cell-compactness", "0.03",
)


def write_fixture(path: Path) -> None:
    # Seed only the fixture generator: Proseg 3.1.1 has no CLI seed option.
    rng = random.Random(17)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("x", "y", "z", "gene", "cell", "compartment"))
        for cell in range(25):
            for transcript in range(160):
                dx, dy = rng.gauss(0, 2), rng.gauss(0, 2)
                writer.writerow((
                    10 + cell % 5 * 18 + dx, 10 + cell // 5 * 18 + dy,
                    rng.uniform(0, 2), f"gene{transcript % 16}", cell + 1,
                    "nucleus" if dx * dx + dy * dy < 4 else "cytoplasm",
                ))


def main() -> None:
    assert os.geteuid() == 65532, "Gate must run as the worker user"
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    assert int(status["CapEff"].strip(), 16) == 0
    manifest = json.loads((EXECUTABLE.parent.parent / "manifest.json").read_text())
    source_manifest = Path(__file__).resolve().parents[1] / "deploy/proseg-cli.json"
    expected = json.loads(source_manifest.read_text())
    assert manifest == expected
    with tempfile.TemporaryDirectory(prefix="mica-proseg-gate-") as temp:
        work = Path(temp)
        inputs, outputs = work / "inputs", work / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        transcripts = inputs / "transcripts.csv"
        write_fixture(transcripts)

        def run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
            command = sandbox_command(
                EXECUTABLE.parent.parent, arguments,
                writable=(work,), protected=(inputs,), cwd=work,
            )
            return subprocess.run(
                command, check=check, timeout=120, capture_output=True, text=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "ANTHROPIC_API_KEY": "probe-sentinel"},
            )

        run([sys.executable, "-I", "-c", PROBE])
        assert run([str(EXECUTABLE), "--version"]).stdout.strip() == "proseg 3.1.1"
        help_text = run([str(EXECUTABLE), "--help"]).stdout
        for flag in (argument for argument in TUNING if argument.startswith("--")):
            assert flag in help_text, flag
        # Do not silently invent a seed or map obsolete perimeter controls to compactness.
        for unsupported in ("--seed", "--perimeter-bound", "--perimeter-eta"):
            reply = run([str(EXECUTABLE), str(transcripts), unsupported, "1"], check=False)
            assert reply.returncode != 0 and "unexpected argument" in reply.stderr, reply.stderr
        arguments = [
            str(EXECUTABLE), str(transcripts),
            "--x-column", "x", "--y-column", "y", "--z-column", "z", "--gene-column", "gene",
            "--cell-id-column", "cell", "--cell-id-unassigned", "0",
            "--compartment-column", "compartment", "--compartment-nuclear", "nucleus",
            "--coordinate-scale", "1", "--nthreads", "1", "--no-factorization",
            "--ncomponents", "2", "--voxel-layers", "1", "--voxel-size", "1",
            "--burnin-samples", "2", "--samples", "3", "--hillclimb", "0",
            "--recorded-samples", "2", "--morphology-steps-per-iter", "10",
            *TUNING, "--output-path", str(outputs), "--output-cell-metadata", "cells.csv",
            "--output-counts", "counts.csv", "--output-transcript-metadata", "transcripts.csv",
        ]
        try:
            run(arguments)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Synthetic Proseg failed: {exc.stdout[-8000:]} {exc.stderr[-8000:]}"
            ) from exc
        with (outputs / "cells.csv").open() as stream:
            cells = list(csv.DictReader(stream))
        assert len(cells) == 25
        assert {row["original_cell_id"] for row in cells} == {str(cell) for cell in range(1, 26)}
        volumes = [float(row["volume"]) for row in cells]
        assert all(math.isfinite(volume) and volume > 0 for volume in volumes)
        with (outputs / "transcripts.csv").open() as stream:
            assert len(list(csv.DictReader(stream))) == 4000
        assert (outputs / "counts.csv").stat().st_size > 0
        assert (outputs / "proseg-output.zarr" / ".zgroup").is_file()
        print(json.dumps({
            "gate": "MICA_PROSEG_CLI_PASSED", "version": "3.1.1", "synthetic": True,
            "transcripts": 4000, "cells": len(cells), "offline_sandbox": "passed",
            "tuning_arguments": TUNING, "unsupported_flags_rejected": True,
        }), flush=True)


if __name__ == "__main__":
    main()
