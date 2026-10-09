"""Bounded cgroup telemetry; samples are observations, not exact process peaks."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def numbers(path: Path) -> dict[str, int]:
    try:
        return {
            p[0].rstrip(":"): int(p[1])
            for line in path.read_text().splitlines()
            if len(p := line.split()) >= 2 and p[1].isdigit()
        }
    except (OSError, ValueError):
        return {}


def integer(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Sample:
    at: float
    memory: int | None
    cpu: float | None
    oom: int | None
    free: float | None


class Sampler:
    def __init__(self, work: Path, *, root: Path = Path("/"), cpus: int | None = None) -> None:
        self.work, self.root = work, root
        self.cg = root / "sys/fs/cgroup"
        self.cpus = float(
            cpus
            or (
                len(os.sched_getaffinity(0))
                if hasattr(os, "sched_getaffinity")
                else os.cpu_count() or 1
            )
        )
        try:
            quota, period = (self.cg / "cpu.max").read_text().split()
            if quota != "max":
                self.cpus = min(self.cpus, int(quota) / int(period))
        except (OSError, ValueError, ZeroDivisionError):
            pass
        mem = numbers(root / "proc/meminfo").get("MemTotal")
        limits = [
            n
            for n in [
                mem * 1024 if mem else None,
                integer(self.cg / "memory.max"),
                integer(self.cg / "memory/memory.limit_in_bytes"),
            ]
            if n is not None and 0 < n < 2**60
        ]
        self.memory_gib = min(limits) / 1024**3 if limits else None
        self.samples: deque[Sample] = deque(maxlen=21600)
        self.starts: dict[str, tuple[float, int | None]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.first: Sample | None = None
        self.peak: int | None = None
        self.min_free: float | None = None
        self.source = "unavailable"

    def sample(self, now: float | None = None) -> Sample:
        mem = integer(self.cg / "memory.current")
        cpu_us = numbers(self.cg / "cpu.stat").get("usage_usec")
        cpu = cpu_us / 1e6 if cpu_us is not None else None
        oom = numbers(self.cg / "memory.events").get("oom_kill")
        self.source = "cgroup-v2"
        if mem is None:
            mem = integer(self.cg / "memory/memory.usage_in_bytes")
            ns = integer(self.cg / "cpuacct/cpuacct.usage")
            cpu = ns / 1e9 if ns is not None else None
            self.source, oom = "cgroup-v1", None
        if mem is None:
            info = numbers(self.root / "proc/meminfo")
            mem = (
                (info["MemTotal"] - info["MemAvailable"]) * 1024
                if ("MemTotal" in info and "MemAvailable" in info)
                else None
            )
            self.source = "host estimate"
        try:
            fs = os.statvfs(self.work)
            free = fs.f_bavail * fs.f_frsize / 1e9
        except OSError:
            free = None
        sample = Sample(time.monotonic() if now is None else now, mem, cpu, oom, free)
        self.samples.append(sample)
        if self.first is None:
            self.first = sample
        if mem is not None:
            self.peak = max(self.peak or 0, mem)
        if free is not None:
            self.min_free = min(self.min_free if self.min_free is not None else free, free)
        return sample

    async def run(self) -> None:
        while True:
            self.sample()
            await asyncio.sleep(2)

    def window(self, start: float, end: float) -> dict[str, Any]:
        rows = [s for s in self.samples if start <= s.at <= end]
        memory = [s.memory for s in rows if s.memory is not None]
        free = [s.free for s in rows if s.free is not None]
        cpu, oom = None, None
        if len(rows) > 1:
            a, b = rows[0], rows[-1]
            if a.cpu is not None and b.cpu is not None and b.at > a.at:
                cpu = max(0, (b.cpu - a.cpu) / (b.at - a.at))
            if a.oom is not None and b.oom is not None:
                oom = max(0, b.oom - a.oom)
        return {
            "wall_s": max(0, end - start),
            "peak_gib": max(memory) / 1024**3 if memory else None,
            "avg_cores": cpu,
            "oom_kills": oom,
            "min_free_disk_gb": min(free) if free else None,
            "source": self.source,
            "sampled": True,
        }

    def begin(self, tool_id: str, args: dict[str, Any], *, now: float | None = None) -> None:
        sample = self.sample(now)
        raw = args.get("timeout")
        timeout = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None
        self.starts[tool_id] = sample.at, timeout

    def end(self, tool_id: str, result: dict[str, Any], *, now: float | None = None) -> str:
        sample = self.sample(now)
        if tool_id not in self.starts:
            return ""
        start, timeout = self.starts.pop(tool_id)
        use = self.window(start, sample.at)
        # No raw commands/descriptions in aggregate telemetry.
        self.commands[tool_id] = use
        peak = use["peak_gib"]
        if (
            use["wall_s"] < 60
            and not use["oom_kills"]
            and not (peak is not None and self.memory_gib and peak >= 0.7 * self.memory_gib)
        ):
            return ""
        text = f"Observed resource use: {use['wall_s']:.0f}s"
        if peak is not None:
            text += f", sampled memory peak {peak:.1f} GiB"
        if self.memory_gib:
            text += f" of {self.memory_gib:.1f} GiB"
        if use["avg_cores"] is not None:
            text += f", average {use['avg_cores']:.1f} of {self.cpus:g} CPUs"
        if use["oom_kills"]:
            text += f", {use['oom_kills']} kernel OOM kills"
        message = str(result.get("error", ""))
        if re.search(r"timed out|timeout exceeded", message, re.I):
            text += (
                f"; stopped by your chosen timeout ({timeout / 1000:g}s)"
                if timeout
                else "; tool timeout reported (you did not pass an explicit timeout)"
            )
        return text + f". Source: {self.source}; overlapping tools share this observation."

    def summary(self) -> dict[str, Any]:
        last = self.sample()
        first = self.first or last
        use = self.window(first.at, last.at)
        use["peak_gib"] = self.peak / 1024**3 if self.peak is not None else None
        use["min_free_disk_gb"] = self.min_free
        if first.cpu is not None and last.cpu is not None and last.at > first.at:
            use["avg_cores"] = max(0, (last.cpu - first.cpu) / (last.at - first.at))
        use["commands"] = sorted(self.commands.values(), key=lambda c: c["wall_s"], reverse=True)[
            :10
        ]
        return use
