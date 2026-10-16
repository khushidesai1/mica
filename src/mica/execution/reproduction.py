"""Build a portable archive from the exact executed revision and trusted records."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from mica.models import ExecutionBundle, TaskResult

# A standalone standard-library launcher. No application installation or model is needed.
REPLAY = r'''"""Replay the retained analysis in its immutable Docker runtime."""
import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input-dir", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
manifest = json.loads((root / "run_manifest.json").read_text())
for name, expected in manifest["source_checksums"].items():
    path = root / "source" / name
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("Source checksum mismatch: " + name)
for name, expected in manifest["trusted_checksums"].items():
    if hashlib.sha256((root / "trusted" / name).read_bytes()).hexdigest() != expected:
        raise SystemExit("Trusted launcher checksum mismatch: " + name)
for name, expected in manifest["input_checksums"].items():
    path = args.input_dir / name
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            checksum.update(chunk)
    if checksum.hexdigest() != expected:
        raise SystemExit("Input checksum mismatch: " + name)
config = root / "config" / "effective_parameters.json"
if hashlib.sha256(config.read_bytes()).hexdigest() != manifest["config_checksum"]:
    raise SystemExit("Configuration checksum mismatch")
image = manifest["runtime_image"]
if "@sha256:" not in image and not image.startswith("sha256:"):
    raise SystemExit("This development package has no pinned image and is not portable.")
args.output_dir.mkdir(parents=True, exist_ok=True)
if any(args.output_dir.iterdir()):
    raise SystemExit("Replay requires an empty output directory")
container_name = "mica-replay-" + uuid.uuid4().hex
command = ["docker", "run", "--rm", "--name", container_name, "--network=none",
    "--read-only", "--cap-drop=ALL",
    "--security-opt=no-new-privileges", "--user=65532:65532", "--pids-limit=256",
    "--memory=" + str(manifest["memory_gib"]) + "g", "--cpus=" + str(manifest["cpu"]),
    "--tmpfs=/tmp:rw,noexec,nosuid,size=256m", "--tmpfs=/scratch:rw,noexec,nosuid,size=512m",
    "-e", "HOME=/tmp", "-e", "MPLCONFIGDIR=/tmp/mpl", "-e", "MPLBACKEND=Agg",
    "-e", "PYTHONHASHSEED=" + str(manifest["seed"]), "-e", "PYTHONDONTWRITEBYTECODE=1",
    "-e", "MICA_SEED=" + str(manifest["seed"]),
    "-e", "MICA_CONFIG=/config/effective_parameters.json",
    "-e", "MICA_CHECKPOINT_INPUTS=/checkpoint_inputs",
    "-e", "MICA_CHECKPOINT_OUTPUTS=/scratch/checkpoints",
    "-e", "PISTACHIO_SEED=" + str(manifest["seed"]),
    "-e", "PISTACHIO_CONFIG=/config/effective_parameters.json",
    "-e", "PISTACHIO_CHECKPOINT_INPUTS=/checkpoint_inputs",
    "-e", "PISTACHIO_CHECKPOINT_OUTPUTS=/scratch/checkpoints",
    "-e", "OMP_NUM_THREADS=" + str(manifest["cpu"]),
    "-e", "OPENBLAS_NUM_THREADS=" + str(manifest["cpu"]),
    "--tmpfs=/checkpoint_inputs:ro,noexec,nosuid,size=1m",
    "-v", str(root / "trusted") + ":/trusted:ro",
    "-v", str(root / "source") + ":/source:ro",
    "-v", str(root / "config") + ":/config:ro",
    "-v", str(args.input_dir.resolve()) + ":/inputs:ro",
    "-v", str(args.output_dir.resolve()) + ":/outputs:rw", "-w", "/source",
    "--entrypoint=python", image, "/trusted/bootstrap.py", "/source/analysis.py",
    "--mode", "run",
    "--input-dir", "/inputs", "--output-dir", "/outputs",
    "--config", "/config/effective_parameters.json", "--seed", str(manifest["seed"])]
try:
    subprocess.run(command, check=True, timeout=manifest["timeout_seconds"])
finally:
    try:
        subprocess.run(["docker", "rm", "-f", container_name], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print("Container cleanup timed out. Check Docker for " + container_name)
print("Replay execution completed. Compare outputs using verification_report.json. "
      "Scientific equivalence is not automatically established.")
'''


def create_reproduction(
    bundle: ExecutionBundle, result: TaskResult, task_dir: Path, manifest: dict[str, object]
) -> Path:
    destination = task_dir / "delivery" / "reproduction.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((task_dir / "trusted").glob("*.py")):
            archive.write(path, "reproduce/trusted/" + path.name)
        for source in bundle.source_files:
            archive.writestr(f"reproduce/source/{source.path}", source.content)
        archive.write(
            task_dir / "config" / "effective_parameters.json",
            "reproduce/config/effective_parameters.json",
        )
        archive.writestr("reproduce/run_manifest.json", json.dumps(manifest, indent=2))
        archive.writestr(
            "reproduce/verification_report.json",
            result.verification.model_dump_json(indent=2) if result.verification else "{}",
        )
        archive.writestr(
            "reproduce/result_manifest.json",
            json.dumps(
                [{"path": x.name, "sha256": x.checksum_sha256} for x in result.artifacts], indent=2
            ),
        )
        archive.writestr("reproduce/replay.py", REPLAY)
        probe = task_dir / "evidence" / "runtime.stdout.log"
        if probe.exists():
            archive.writestr("reproduce/environment_manifest.json", probe.read_text())
            try:
                packages = json.loads(probe.read_text())["packages"]
                archive.writestr("reproduce/dependencies.lock", "\n".join(packages) + "\n")
            except (ValueError, KeyError):
                pass
        archive.writestr(
            "reproduce/README.md",
            """# Reproduce this analysis

Extract the archive. Install Docker and obtain read access to the pinned image named in
run_manifest.json (authenticate to its registry if necessary). Obtain the original inputs
under their logical filenames. The launcher verifies SHA-256 checksums before execution.
Run `python reproduce/replay.py --input-dir /path/to/inputs --output-dir /path/to/new-results`.
The output directory must be empty and writable by container UID 65532 (on Linux, grant
that UID access). Use the architecture recorded in environment_manifest.json. GPU tasks
require the matching GPU runtime; this launcher currently supports CPU tasks only.

The exact retained image is the environment authority. dependencies.lock records resolved
Python versions; it does not lock OS packages or wheel hashes and is not a substitute for
that image. The image must remain accessible for replay. For a local sha256 image ID,
export it with docker save and restore it with docker load on the replay machine.
No private inputs or credentials
are included. See verification_report.json for actual replay coverage and limitations.
Clean smoke verification is not an independent full scientific replay. Numeric/hardware
nondeterminism can require method-specific equivalence checks. Source tests are included.
""",
        )
    return destination
