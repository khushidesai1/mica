"""One process, one Dask worker, bounded threads and RAM; no TCP listener."""

from __future__ import annotations

import math
import os
from pathlib import Path


def cluster(workspace: str | Path, *, task_peak_gib: float, threads: int | None = None):
    from dask.distributed import LocalCluster

    limits = []
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = int(Path(path).read_text())
            if 0 < value < 2**60:
                limits.append(value)
        except (OSError, ValueError):
            pass
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            limits.append(int(line.split()[1]) * 1024)
    memory_gib = min(limits) / 1024**3 if limits else None
    cpus = len(os.sched_getaffinity(0))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    if not memory_gib or not math.isfinite(task_peak_gib) or task_peak_gib <= 0:
        raise ValueError("Provide measured task memory; effective RAM must be observable.")
    usable = max(0, (memory_gib - 6) * 0.7)
    if task_peak_gib * 1.3 > usable:
        raise ValueError("A single task exceeds safe worker memory; reduce the task or resize.")
    count = min(max(1, cpus), max(1, int(usable / (task_peak_gib * 1.3))))
    if threads is not None:
        if not 1 <= threads <= count:
            raise ValueError(f"Use at most {count} concurrent task threads for this estimate.")
        count = threads
    scratch = Path(workspace) / "scratch/dask"
    scratch.mkdir(parents=True, exist_ok=True)
    # Prevent nested BLAS fan-out; set these before importing scientific libraries.
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[variable] = "1"
    return LocalCluster(
        processes=False,
        protocol="inproc://",
        dashboard_address=None,
        n_workers=1,
        threads_per_worker=count,
        memory_limit=int(usable * 1024**3),
        local_directory=str(scratch),
    )
