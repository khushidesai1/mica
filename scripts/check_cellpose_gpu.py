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
            probe = PROBE + "\nimport torch\nassert torch.cuda.is_available() == " + repr(gpu)
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
                assert "L4" in result["gpu_name"]
            assert result["input_shape"] == [64, 64]
            print(
                json.dumps(
                    {
                        "version": version,
                        "model": model,
                        "device": result["device"],
                        "model_device": result["model_device"],
                        "gpu_name": result["gpu_name"],
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
