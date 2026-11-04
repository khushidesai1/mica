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
from mica.agents.analysis.gpu_tools import script_binding, tools
from mica.agents.analysis.runtime_sandbox import PROBE, sandbox_command
from mica.agents.analysis.storage import LocalStorage
from mica.agents.analysis.toolkit_catalog import environments


async def offline():
    """Exercise the packaged sandbox without exposing hardware or loading extra models."""
    assert os.geteuid() == 65532
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    assert int(status["CapEff"].strip(), 16) == 0
    with tempfile.TemporaryDirectory(prefix="mica-offline-gate-") as temp:
        work = Path(temp)
        for folder in (work / "code", work / "inputs", work / "outputs", work / "data"):
            folder.mkdir()
        settings = Settings(_env_file=None, gpu_worker=False)
        assert not tools(work, work / "data", settings)
        registered = {row.id: row for row in environments(settings)}
        assert not any(row.cuda for row in registered.values())
        runtime = Path(registered["cellpose-4.2.1.1"].python).parent.parent
        interpreter = str(runtime / "bin/python")
        command = sandbox_command(
            runtime, [interpreter, "-I", "-c", PROBE +
                      "\nimport torch\nassert not torch.cuda.is_available()"],
            writable=(work,), protected=(work / "inputs", work / "data"), cwd=work,
            read_only=(Path("/opt/cellpose/models"), Path("/opt/cellpose/manifest.json")),
        )
        source = work / "code/pinned.py"
        source.write_text("raise AssertionError('Mutable source path was executed')")
        pinned_code = PROBE + """
import torch
assert not torch.cuda.is_available()
assert 'MICA_PINNED_SCRIPT_BINDING_PASSED' in Path(__file__).read_text()
try:
    Path(__file__).write_text('must not overwrite the pinned script')
except OSError:
    pass
else:
    raise AssertionError('Pinned script became writable')
print('MICA_PINNED_SCRIPT_BINDING_PASSED')
"""
        with tempfile.TemporaryFile() as script:
            script.write(pinned_code.encode())
            script.seek(0)
            boundary = command.index("--")
            pinned_command = command[:boundary] + script_binding(source, script.fileno()) + [
                "--", interpreter, "-I", str(source),
            ]
            subprocess.run(
                pinned_command, check=True, timeout=60, pass_fds=(script.fileno(),),
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
        assert len(tools(work, work / "data", Settings(_env_file=None, gpu_worker=True))) == 1
        if os.environ.get("VERIFY_CPU_CELLPOSE") == "1":
            subprocess.run([
                "/opt/sap/bin/python", "-I", "-c",
                "import numpy as np,tifffile,sys; y,x=np.mgrid[:64,:64]; "
                "tifffile.imwrite(sys.argv[1],np.exp(-((x-32)**2+(y-32)**2)/32).astype('float32'))",
                str(work / "inputs/crop.tif"),
            ], check=True, timeout=30)
            shutil.copyfile(
                "/opt/pistachio/plugin/skills/cellpose/scripts/segment.py", work / "code/segment.py"
            )
            request = json.dumps({
                "input_path": "inputs/crop.tif", "output_dir": "outputs/cpu", "version": "4.2.1.1",
                "model": "cpsam", "device": "cpu", "diameter": 20,
            })
            separator = command.index("--")
            subprocess.run(
                command[:separator + 1] +
                [interpreter, "-I", str(work / "code/segment.py"), "--request", request],
                check=True, timeout=300, env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
            result = json.loads((work / "outputs/cpu/provenance.json").read_text())
            assert result["device"] == result["model_device"] == "cpu"
            assert result["input_shape"] == [64, 64]
            assert (work / "outputs/cpu/masks.tif").stat().st_size > 0
            print("MICA_CPU_SKILL_CROP_PASSED", flush=True)
        print("MICA_OFFLINE_GPU_BOUNDARY_PASSED", flush=True)


async def main(storage_factory=LocalStorage, *, clean_temporary=True):
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
        storage = storage_factory(root / "storage")
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
        if clean_temporary:
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
                    "temporary_cleanup_exercised": clean_temporary,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main() if os.environ.get("VERIFY_GPU") == "1" else offline())
