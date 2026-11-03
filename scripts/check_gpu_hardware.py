"""Bounded generic GPU execution/isolation gate; no model or project data."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from mica.agents.analysis.checkpoints import Stages, cleanup_temporary, digest
from mica.agents.analysis.config import Settings
from mica.agents.analysis.gpu_tools import tools
from mica.agents.analysis.storage import LocalStorage


def assert_controller():
    assert os.geteuid() == 65532, "GPU worker must remain unprivileged"
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    assert int(status["CapEff"].strip(), 16) == int(status["CapBnd"].strip(), 16) == 0
    assert status["NoNewPrivs"].strip() == "1"
    assert os.environ["MICA_SANDBOX_PARENT_SENTINEL"] == "sentinel-not-a-credential"


def probe(work, data, private, count):
    return f"""
import json, os, socket
from pathlib import Path
import torch
for address, port in [('1.1.1.1', 443), ('169.254.169.254', 80)]:
    try:
        with socket.create_connection((address, port), timeout=1):
            raise RuntimeError('Sandbox network is reachable')
    except OSError:
        pass
assert not any(k.startswith(('ANTHROPIC_', 'SLACK_', 'GOOGLE_')) for k in os.environ)
assert not Path({str(private)!r}).exists()
assert not Path('/etc/pistachio').exists()
assert not Path('/dev/sda').exists() and not Path('/dev/nvme0n1').exists()
for environ in Path('/proc').glob('[0-9]*/environ'):
    try:
        assert b'sentinel-not-a-credential' not in environ.read_bytes()
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        pass
for folder in [{str(data)!r}, {str(work / "inputs")!r}]:
    assert (Path(folder) / 'readable.txt').read_text() == 'synthetic'
    try:
        (Path(folder) / 'forbidden.txt').write_text('must not write')
    except OSError:
        pass
    else:
        raise AssertionError('Read-only input became writable')
assert torch.cuda.is_available()
assert torch.cuda.device_count() == {count}
names = []
for index in range(torch.cuda.device_count()):
    device = torch.device('cuda', index)
    x = torch.ones((32, 32), device=device)
    assert float((x @ x)[0, 0]) == 32
    torch.cuda.synchronize(device)
    names.append(torch.cuda.get_device_name(index))
result = {{'hardware': names, 'device': 'gpu', 'cuda_and_isolation': 'passed'}}
Path('outputs/hardware/device.json').write_text(json.dumps(result))
print(json.dumps(result), flush=True)
"""


async def main():
    assert_controller()
    family = os.environ["VERIFY_GPU_TYPE"]
    count = int(os.environ.get("VERIFY_GPU_COUNT", "1"))
    assert family in {"t4", "l4", "a100", "a100-80gb", "h100"} and 1 <= count <= 16
    environment = os.environ.get("VERIFY_GPU_ENVIRONMENT", "cellpose-4.2.1.1")
    with tempfile.TemporaryDirectory(prefix="mica-cuda-check-") as temp:
        root = Path(temp)
        work, data, private = root / "work", root / "data", root / "private"
        for folder in (work / "code", work / "inputs", data, private):
            folder.mkdir(parents=True)
        for folder in (work / "inputs", data):
            (folder / "readable.txt").write_text("synthetic")
        (work / "code/check.py").write_text(probe(work, data, private, count))
        storage = LocalStorage(root / "storage")
        stages = Stages(storage, work, "a" * 32, 0, thread_key="smoke", project_id="smoke")

        async def save(result):
            return stages.capture_result(result["output_dir"], result["fingerprint"])

        tool = tools(work, data, Settings(_env_file=None, gpu_worker=True), save)[0]
        reply = await tool.handler(
            {
                "environment": environment,
                "script": "code/check.py",
                "output_dir": "outputs/hardware",
                "input_fingerprint": "32x32-synthetic-matrices",
            }
        )
        assert not reply.get("isError"), reply
        result = json.loads(reply["content"][0]["text"])
        device = json.loads((work / "outputs/hardware/device.json").read_text())
        assert all(family.split("-")[0].upper() in name.upper() for name in device["hardware"])
        cleanup_temporary(storage, "a" * 32)
        restored = Stages(
            storage, root / "restored", "b" * 32, 0, thread_key="smoke", project_id="smoke"
        )
        restored.restore_saved(result["checkpoint"]["checkpoint_id"])
        assert digest(work / "outputs/hardware/device.json") == digest(
            restored.work / "outputs/hardware/device.json"
        )
        print(
            json.dumps({**device, "environment": environment, "durable_save": "passed"}), flush=True
        )
    print("MICA_GPU_HARDWARE_CHECK_PASSED", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
