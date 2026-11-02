"""Bounded CUDA/device-isolation gate for a new GPU family; no model or project data."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from mica.agents.analysis.cellpose_tools import SegmentRequest, command
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runtime_sandbox import PROBE

family = os.environ["VERIFY_GPU_TYPE"]
count = int(os.environ.get("VERIFY_GPU_COUNT", "1"))
assert family in {"t4", "l4", "a100", "a100-80gb", "h100"} and 1 <= count <= 16
assert os.geteuid() == 65532
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
assert int(status["CapEff"].strip(), 16) == int(status["CapBnd"].strip(), 16) == 0
assert status["NoNewPrivs"].strip() == "1"
assert os.environ["MICA_SANDBOX_PARENT_SENTINEL"] == "sentinel-not-a-credential"
probe = (
    PROBE
    + """
import torch
assert not Path('/dev/sda').exists() and not Path('/dev/nvme0n1').exists()
for environ in Path('/proc').glob('[0-9]*/environ'):
    try:
        assert b'sentinel-not-a-credential' not in environ.read_bytes()
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        pass
assert torch.cuda.is_available()
"""
    + f"\nassert torch.cuda.device_count() == {count}\n"
    + """
for index in range(torch.cuda.device_count()):
    device = torch.device('cuda', index)
    x = torch.ones((32, 32), device=device)
    y = x @ x
    assert float(y[0, 0]) == 32
    torch.cuda.synchronize(device)
    print(torch.cuda.get_device_name(index), flush=True)
"""
)
with tempfile.TemporaryDirectory(prefix="mica-cuda-check-") as temp:
    work = Path(temp)
    (work / "probe.tif").touch()
    output = work / "out"
    output.mkdir()
    for version, model in [("3.1.1.3", "cyto3"), ("4.2.1.1", "cpsam")]:
        args = command(
            SegmentRequest(
                input_path="probe.tif",
                output_dir="outputs/check",
                version=version,
                model=model,
                device="gpu",
            ),
            work / "probe.tif",
            output,
            Settings(_env_file=None, gpu_worker=True),
        )
        separator = args.index("--")
        result = subprocess.run(
            args[: separator + 2] + ["-I", "-c", probe],
            check=True,
            timeout=60,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        assert family.split("-")[0].upper() in result.stdout.upper(), result.stdout
        print(
            json.dumps(
                {
                    "version": version,
                    "hardware": result.stdout.strip(),
                    "cuda_and_isolation": "passed",
                }
            ),
            flush=True,
        )
print("MICA_GPU_HARDWARE_CHECK_PASSED", flush=True)
