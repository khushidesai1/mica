"""Linux image gate: process-tree probe, telemetry, checkpoint and quote contracts."""

import json
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from pistachio.agents.analysis.checkpoints import Stages
from pistachio.agents.analysis.compute_quotes import Estimate, Rates, quote
from pistachio.agents.analysis.resources import Sampler
from pistachio.agents.analysis.storage import LocalStorage

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
    stages = Stages(store, work, "a" * 32, 0, max_bytes=1024**2)
    stages.capture({"path": "checkpoints/ingest", "fingerprint": "synthetic-v1"})
    restored = work / "restored"
    restored.mkdir()
    Stages(store, restored, "a" * 32, 1, max_bytes=1024**2).restore(stages.manifest())
    assert (restored / "checkpoints/ingest/result.txt").read_text() == "synthetic"
    now = time.time()
    rates = Rates(
        region="us-central1",
        fetched_at=now,
        expires_at=now + 3600,
        cpu_hour=Decimal(".01801"),
        memory_gib_hour=Decimal(".002415"),
        balanced_gb_hour=Decimal(".1") / 744,
        ssd_gb_hour=Decimal(".17") / 744,
        sku_ids=["fixture"] * 4,
    )
    estimate = Estimate(
        tier="large",
        hours_low=1,
        hours_high=2,
        peak_gib=100,
        task_peak_gib=50,
        disk_gb=100,
        basis="Synthetic image-gate fixture; not a live quote.",
    )
    q = quote(estimate, rates, spent_usd=1, limit_usd=50, now=now)
    assert not q.requires_approval
    assert quote(estimate, rates, spent_usd=49, limit_usd=50, now=now).requires_approval
    print("CHECKPOINT_AND_COMPUTE_POLICY_CHECK_PASSED", flush=True)
