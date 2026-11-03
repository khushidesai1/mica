"""Bounded operator gate: one synthetic SAM crop through the skill and generic GPU tool."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from check_gpu_hardware import assert_controller, probe

from mica.agents.analysis.checkpoints import Stages, cleanup_temporary, digest
from mica.agents.analysis.config import Settings
from mica.agents.analysis.gpu_tools import tools
from mica.agents.analysis.storage import LocalStorage


async def main():
    assert os.environ.get("VERIFY_GPU") == "1"
    assert_controller()
    gpu_type = os.environ["VERIFY_GPU_TYPE"]
    assert gpu_type in {"t4", "l4", "a100", "a100-80gb", "h100"}
    with tempfile.TemporaryDirectory(prefix="mica-skill-gate-") as temp:
        root = Path(temp)
        work, data, private = root / "work", root / "data", root / "private"
        for folder in (work / "code", work / "inputs", data, private):
            folder.mkdir(parents=True)
        for folder in (work / "inputs", data):
            (folder / "readable.txt").write_text("synthetic")
        subprocess.run(
            [
                "/opt/sap/bin/python",
                "-I",
                "-c",
                "import numpy as np,tifffile,sys; y,x=np.mgrid[:256,:256]; "
                "tifffile.imwrite(sys.argv[1],np.exp(-((x-128)**2+(y-128)**2)/128).astype('float32'))",
                str(work / "inputs/crop.tif"),
            ],
            check=True,
            timeout=30,
        )
        shutil.copyfile(
            "/opt/pistachio/plugin/skills/cellpose/scripts/segment.py", work / "code/segment.py"
        )
        (work / "code/check.py").write_text(
            probe(work, data, private, int(os.environ.get("VERIFY_GPU_COUNT", "1")))
        )
        storage = LocalStorage(root / "storage")
        stages = Stages(storage, work, "a" * 32, 0, thread_key="smoke", project_id="smoke")

        async def save(result):
            return stages.capture_result(result["output_dir"], result["fingerprint"])

        settings = Settings(_env_file=None, gpu_worker=True, max_run_seconds=600)
        tool = tools(work, data, settings, save)[0]
        for script, folder, arguments in (
            ("code/check.py", "outputs/hardware", []),
            (
                "code/segment.py",
                "outputs/sam",
                [
                    "--request",
                    json.dumps(
                        {
                            "input_path": "inputs/crop.tif",
                            "output_dir": "outputs/sam",
                            "version": "4.2.1.1",
                            "model": "cpsam",
                            "device": "gpu",
                            "diameter": 20,
                        }
                    ),
                ],
            ),
        ):
            reply = await tool.handler(
                {
                    "environment": "cellpose-4.2.1.1",
                    "script": script,
                    "arguments": arguments,
                    "output_dir": folder,
                    "input_fingerprint": "256x256-synthetic-crop-cpsam-diameter20",
                }
            )
            assert not reply.get("isError"), reply
            result = json.loads(reply["content"][0]["text"])
        provenance = json.loads((work / "outputs/sam/provenance.json").read_text())
        assert provenance["device"] == "gpu" and provenance["model_device"] == "cuda"
        assert gpu_type.split("-")[0].upper() in provenance["gpu_name"].upper()
        assert provenance["input_shape"] == [256, 256]
        assert len(provenance["input_sha256"]) == 64
        cleanup_temporary(storage, "a" * 32)
        restored = Stages(
            storage, root / "restored", "b" * 32, 0, thread_key="smoke", project_id="smoke"
        )
        restored.restore_saved(result["checkpoint"]["checkpoint_id"])
        assert digest(work / "outputs/sam/masks.tif") == digest(
            restored.work / "outputs/sam/masks.tif"
        )
        print(
            json.dumps(
                {
                    "skill": "cellpose",
                    "model": provenance["model"],
                    "gpu": provenance["gpu_name"],
                    "device": provenance["device"],
                    "durable_save": "passed",
                    "isolation": "passed",
                    "synthetic": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
