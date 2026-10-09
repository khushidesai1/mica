"""Measure a bounded probe without collecting its command or output in telemetry."""

from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


def tree_rss(root: int) -> int | None:
    """Linux sampled aggregate RSS (shared pages can be counted more than once)."""
    if not Path("/proc").is_dir():
        return None
    rows = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            rows[int(entry.name)] = int(fields[1]), int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    pids = {root}
    while more := {pid for pid, (parent, _) in rows.items() if parent in pids} - pids:
        pids.update(more)
    return sum(rows[p][1] for p in pids if p in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", choices=["analysis", "ingest", "segmentation", "plot"], required=True
    )
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.seconds <= 1800:
        parser.error("Supply a command and a probe duration between 1 and 1800 seconds.")
    start, peak, timed_out = time.monotonic(), None, False
    child = subprocess.Popen(command, start_new_session=True)

    def stop(sig: int, _frame: object = None) -> None:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, sig)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while child.poll() is None:
        rss = tree_rss(child.pid)
        if rss is not None:
            peak = max(peak or 0, rss)
        if time.monotonic() - start >= args.seconds:
            timed_out = True
            stop(signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop(signal.SIGKILL)
            break
        time.sleep(0.2)
    code = child.wait()
    stop(signal.SIGTERM)  # A probe must not leave detached children running.
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    high_water = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    elapsed = time.monotonic() - start
    data = {
        "label": args.label,
        "wall_s": elapsed,
        "sampled_tree_peak_gib": peak / 1024**3 if peak is not None else None,
        "largest_child_high_water_gib": high_water / 1024**3,
        "avg_cores": (usage.ru_utime + usage.ru_stime) / elapsed,
        "exit_code": code,
        "timed_out": timed_out,
        "notes": (
            "Sampled RSS may miss brief peaks and double-count shared pages. "
            "A probe is not a full-workload guarantee."
        ),
    }
    encoded = json.dumps(data, allow_nan=False)
    if args.output:
        args.output.write_text(encoded + "\n")
    print("PROBE_RESOURCES " + encoded, file=sys.stderr)
    return 124 if timed_out else code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
