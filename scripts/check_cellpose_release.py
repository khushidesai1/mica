"""Offline model-load and segmentation gate, run with each supported interpreter."""

import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

import numpy as np
import tifffile


def main():
    actual = version("cellpose")
    sam = actual == "4.2.1.1"
    assert sam or actual == "3.1.1.3"
    with tempfile.TemporaryDirectory(prefix="cellpose-canary-") as temp:
        work = Path(temp)
        y, x = np.mgrid[:64, :64]
        sample = np.exp(-((x - 32) ** 2 + (y - 32) ** 2) / 32).astype(np.float32)
        tifffile.imwrite(work / "crop.tif", sample)
        for model in ["cpsam", "cpsam_v2"] if sam else ["cyto3"]:
            out = work / model
            out.mkdir()
            request = dict(
                input_path=str(work / "crop.tif"),
                output_dir=str(out),
                version=actual,
                model=model,
                model_dir="/opt/cellpose/models" if sam else "/opt/sap/models",
                device="gpu" if os.environ.get("VERIFY_GPU") == "1" else "cpu",
                channel_axis=None,
                channels=[],
                diameter=20,
                batch_size=1,
                flow_threshold=0.4,
                cellprob_threshold=0,
                min_size=15,
            )
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "/opt/pistachio/plugin/skills/cellpose/scripts/segment.py",
                    "--request",
                    json.dumps(request),
                ],
                check=True,
                timeout=600,
            )
            result = json.loads((out / "provenance.json").read_text())
            assert result["version"] == actual and result["model"] == model
            masks = tifffile.imread(out / "masks.tif")
            assert masks.shape == (64, 64) and masks.dtype == np.uint32
            print(
                json.dumps(
                    {
                        "cellpose": actual,
                        "model": model,
                        "device": result["device"],
                        "offline_segmentation": "passed",
                        "synthetic": True,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
