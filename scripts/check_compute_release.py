"""Linux image gate: process-tree probe, telemetry, checkpoint and worker sizing contracts."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mica.agents.analysis import recovery, tiers
from mica.agents.analysis.checkpoints import Stages
from mica.agents.analysis.config import Settings
from mica.agents.analysis.resources import Sampler
from mica.agents.analysis.storage import LocalStorage
from mica.agents.analysis.tiers import WorkerChoice

with tempfile.TemporaryDirectory(prefix="compute-image-check-") as temp:
    work = Path(temp)
    sample = Sampler(work)
    observed = sample.sample()
    assert sample.memory_gib and sample.cpus > 0 and observed.memory is not None
    output = work / "probe.json"
    child = (
        "import subprocess,sys;subprocess.run([sys.executable,'-c',"
        "'import time;x=bytearray(32*1024**2);time.sleep(1)'],check=True)"
    )
    subprocess.run(
        [
            sys.executable,
            "/opt/pistachio/tools/measure.py",
            "--label",
            "analysis",
            "--output",
            str(output),
            "--",
            sys.executable,
            "-c",
            child,
        ],
        check=True,
    )
    measured = json.loads(output.read_text())
    assert measured["sampled_tree_peak_gib"] > 0.03
    assert measured["exit_code"] == 0 and measured["wall_s"] >= 1
    assert child not in output.read_text()
    print("LINUX_PROCESS_TREE_AND_CGROUP_CHECK_PASSED", flush=True)
    (work / "checkpoints/ingest").mkdir(parents=True)
    (work / "checkpoints/ingest/result.txt").write_text("synthetic")
    store = LocalStorage(work / "cloud")
    stages = Stages(store, work, "a" * 32, 0)
    stages.capture({"path": "checkpoints/ingest", "fingerprint": "synthetic-v1"})
    restored = work / "restored"
    restored.mkdir()
    Stages(store, restored, "a" * 32, 1).restore(stages.manifest())
    assert (restored / "checkpoints/ingest/result.txt").read_text() == "synthetic"
    now = time.time()
    settings = Settings(_env_file=None)
    default = tiers.launch_choice(None, settings)
    assert default["tier"] == "standard" and default["provisioning"] == "SPOT"
    heavy = tiers.launch_choice(
        WorkerChoice(category="full_ingest", reason="Full data ingest"), settings
    )
    assert heavy["tier"] == "large" and heavy["provisioning"] == "STANDARD"
    record = {"launch": default}
    recovery.initialize(record, settings, now=now)
    seconds, allowance = recovery.limits(record, settings, now=now)
    assert seconds == settings.max_run_seconds and 0 < allowance < settings.max_budget_usd_per_turn
    request = recovery.RecoverCall(
        attempt=0,
        cause="capacity",
        memory_gib=100,
        evidence="Synthetic measured arrays",
        handoff="Continue the verified checkpoint",
    )
    assert recovery.destination(record, request, settings) == "large"
    record["resize_count"] = 1
    try:
        recovery.destination(record, request, settings)
    except ValueError:
        pass
    else:
        raise AssertionError("A second automatic resize must be denied")
    record["model_spent"] = record["model_limit"]
    try:
        recovery.limits(record, settings, now=now)
    except ValueError:
        pass
    else:
        raise AssertionError("An exhausted model allowance must be denied")
    print("CHECKPOINT_AND_COMPUTE_POLICY_CHECK_PASSED", flush=True)
