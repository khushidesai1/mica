"""Operator live gate: synthetic inference inside the actual fixed-tool sandbox."""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from mica.agents.analysis.cellpose_tools import SegmentRequest, command, segment
from mica.agents.analysis.config import Settings
from mica.agents.analysis.runtime_sandbox import PROBE


async def main():
    gpu = os.environ.get("VERIFY_GPU") == "1"
    gpu_type = os.environ.get("VERIFY_GPU_TYPE", "")
    if gpu:
        assert gpu_type in {"t4", "l4"}, "Specify the GPU hardware being verified"
    assert os.geteuid() == 65532, "Worker must remain unprivileged"
    capabilities = next(
        line.split()[1]
        for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith("CapEff:")
    )
    assert int(capabilities, 16) == 0, "Worker unexpectedly has effective Linux capabilities"
    if gpu:
        status = dict(
            line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
        )
        assert int(status["CapBnd"].strip(), 16) == 0, (
            "GPU worker must drop all bounding capabilities"
        )
        assert status["NoNewPrivs"].strip() == "1", "GPU worker must not gain privileges"
    with tempfile.TemporaryDirectory(prefix="cellpose-tool-gate-") as temp:
        work = Path(temp)
        # The trusted controller doesn't import scientific packages from the base interpreter.
        subprocess.run(
            [
                "/opt/sap/bin/python",
                "-I",
                "-c",
                "import numpy as np,tifffile,sys; y,x=np.mgrid[:64,:64]; "
                "tifffile.imwrite(sys.argv[1],np.exp(-((x-32)**2+(y-32)**2)/32).astype('float32'))",
                str(work / "crop.tif"),
            ],
            check=True,
        )
        settings = Settings(_env_file=None, gpu_worker=gpu, max_run_seconds=600)
        for version, model in [("3.1.1.3", "cyto3"), ("4.2.1.1", "cpsam"), ("4.2.1.1", "cpsam_v2")]:
            request = SegmentRequest(
                input_path="crop.tif",
                output_dir="outputs/" + model,
                version=version,
                model=model,
                diameter=20,
            )
            # Same runtime, namespace and device bindings as the real tool; assert network,
            # metadata and parent credentials are absent before inference.
            output = work / "probe"
            output.mkdir(exist_ok=True)
            args = command(request, work / "crop.tif", output, settings)
            start = args.index("--")
            python = args[start + 1]
            probe = (
                PROBE
                + "\nassert not Path('/dev/sda').exists() and not Path('/dev/nvme0n1').exists()"
                + "\nimport torch\nassert torch.cuda.is_available() == "
                + repr(gpu)
            )
            if gpu:
                probe += "\nfor environ in Path('/proc').glob('[0-9]*/environ'):\n    try:\n        assert b'sentinel-not-a-credential' not in environ.read_bytes(), 'Parent environment exposed through proc'\n    except (PermissionError, FileNotFoundError, ProcessLookupError):\n        pass"
            subprocess.run(
                args[: start + 1] + [python, "-I", "-c", probe],
                check=True,
                timeout=60,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "ANTHROPIC_API_KEY": "sentinel-not-a-credential",
                },
            )
            try:
                result = await segment(request, work, work / "data", settings)
            except Exception:
                log = work / request.output_dir / "execution.log"
                if log.exists():
                    print(log.read_text(errors="replace")[-8000:], flush=True)
                raise
            assert result["version"] == version and result["model"] == model
            assert result["device"] == ("gpu" if gpu else "cpu")
            assert result["model_device"] == ("cuda" if gpu else "cpu")
            if gpu:
                assert gpu_type.upper() in result["gpu_name"]
            expected_precision = (
                "bfloat16" if gpu and gpu_type == "l4" and version.startswith("4.") else "float32"
            )
            assert result["model_precision"] == expected_precision
            assert result["input_shape"] == [64, 64]
            print(
                json.dumps(
                    {
                        "version": version,
                        "model": model,
                        "device": result["device"],
                        "model_device": result["model_device"],
                        "gpu_name": result["gpu_name"],
                        "model_precision": result["model_precision"],
                        "sandbox_segmentation": "passed",
                        "isolation": "passed",
                        "cell_count": result["cell_count"],
                        "synthetic": True,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
