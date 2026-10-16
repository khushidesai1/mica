"""Opt-in real Docker boundary tests; never substitute the unisolated test runner."""

from __future__ import annotations

import os

import pytest
from test_development import ANALYSIS, configuration, make_bundle

from mica.execution.backends import DockerExecutor


@pytest.mark.asyncio
async def test_real_docker_boundary(settings, tmp_path):
    image = os.getenv("MICA_CONFORMANCE_IMAGE")
    if not image:
        pytest.skip("set MICA_CONFORMANCE_IMAGE to an immutable Docker image")
    script = ANALYSIS.replace(
        "    config = json.loads(args.config.read_text())",
        """    import os
    import socket
    source_dir = __import__('pathlib').Path(__file__).parent
    for directory in (args.input_dir, args.config.parent, source_dir):
        try:
            (directory / 'forbidden-write').write_text('x')
        except OSError:
            pass
        else:
            raise RuntimeError('read-only boundary failed')
    assert 'MICA_TEST_SECRET' not in os.environ
    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect(('169.254.169.254', 80))
    except OSError:
        pass
    else:
        raise RuntimeError('metadata network boundary failed')
    finally:
        sock.close()
    config = json.loads(args.config.read_text())""",
    )
    settings = configuration(settings).model_copy(
        update={
            "worker_sandbox": "docker",
            "batch_image": image,
            "development_command_seconds": 60,
        }
    )
    bundle = make_bundle(tmp_path, "verify", script=script).model_copy(
        update={
            "runtime_image": image,
            "command_timeout_seconds": 60,
        }
    )
    result = await DockerExecutor(settings).execute(bundle)
    assert result.exit_code == 0, result.model_dump()
    assert result.verification.passed


@pytest.mark.asyncio
async def test_fresh_docker_replay_preserves_rng_and_source(settings, tmp_path):
    import json
    import subprocess
    import sys
    import zipfile

    image = os.getenv("MICA_CONFORMANCE_IMAGE")
    if not image:
        pytest.skip("set MICA_CONFORMANCE_IMAGE to an immutable Docker image")
    script = ANALYSIS.replace(
        "    config = json.loads(args.config.read_text())",
        "    import random\n    config = json.loads(args.config.read_text())",
    )
    script = script.replace("'seed': args.seed", "'seed': args.seed, 'draw': random.random()")
    settings = configuration(settings).model_copy(
        update={"worker_sandbox": "docker", "batch_image": image}
    )
    executor = DockerExecutor(settings)
    bundle = make_bundle(tmp_path, "verify", script=script).model_copy(
        update={
            "runtime_image": image,
            "command_timeout_seconds": 60,
        }
    )
    verified = await executor.execute(bundle)
    assert verified.exit_code == 0, verified.model_dump()
    full = bundle.model_copy(
        update={
            "attempt": 2,
            "operation_id": "replay-full",
            "operation_phase": "run",
            "verification": verified.verification,
        }
    )
    result = await executor.execute(full)
    assert result.exit_code == 0, result.model_dump()
    root = settings.artifact_root / bundle.task_id / "attempt-2"
    with zipfile.ZipFile(root / "delivery/reproduction.zip") as archive:
        archive.extractall(tmp_path / "extracted")
    replay_root = tmp_path / "extracted/reproduce"
    new_outputs = tmp_path / "replayed"
    new_outputs.mkdir(mode=0o777)
    new_outputs.chmod(0o777)
    completed = subprocess.run(
        [
            sys.executable,
            str(replay_root / "replay.py"),
            "--input-dir",
            str(tmp_path),
            "--output-dir",
            str(new_outputs),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads((new_outputs / "summary.json").read_text()) == json.loads(
        (root / "outputs/summary.json").read_text()
    )
    assert (replay_root / "source/analysis.py").read_text() == script
