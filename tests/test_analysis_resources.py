from pathlib import Path

import pytest

from mica.agents.analysis.resources import Sampler


def put(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(text))


def test_cgroup_limits_windows_oom_and_cpu(tmp_path):
    put(tmp_path, "proc/meminfo", "MemTotal: 67108864 kB\nMemAvailable: 33554432 kB\n")
    put(tmp_path, "sys/fs/cgroup/memory.max", 16 * 1024**3)
    put(tmp_path, "sys/fs/cgroup/cpu.max", "200000 100000")
    put(tmp_path, "sys/fs/cgroup/memory.current", 8 * 1024**3)
    put(tmp_path, "sys/fs/cgroup/memory.events", "oom_kill 0\n")
    put(tmp_path, "sys/fs/cgroup/cpu.stat", "usage_usec 0\n")
    sampler = Sampler(Path(tmp_path), root=tmp_path, cpus=8)
    sampler.sample(0)
    assert sampler.memory_gib == 16 and sampler.cpus == 2
    put(tmp_path, "sys/fs/cgroup/memory.current", 12 * 1024**3)
    put(tmp_path, "sys/fs/cgroup/memory.events", "oom_kill 2\n")
    put(tmp_path, "sys/fs/cgroup/cpu.stat", "usage_usec 15000000\n")
    sampler.sample(10)
    use = sampler.window(0, 10)
    assert use["peak_gib"] == 12 and use["avg_cores"] == 1.5 and use["oom_kills"] == 2


def test_missing_files_are_unknown_not_zero(tmp_path):
    sampler = Sampler(tmp_path, root=tmp_path, cpus=1)
    sampler.sample(0)
    sampler.sample(10)
    use = sampler.window(0, 10)
    assert use["peak_gib"] is None and use["oom_kills"] is None
    assert use["avg_cores"] is None


def test_tool_feedback_threshold_and_truthful_timeout(tmp_path):
    put(tmp_path, "proc/meminfo", "MemTotal: 67108864 kB\nMemAvailable: 62914560 kB\n")
    sampler = Sampler(tmp_path, root=tmp_path, cpus=8)
    sampler.begin("short", {}, now=0)
    assert sampler.end("short", {}, now=5) == ""
    sampler.begin("long", {"timeout": 600000}, now=10)
    text = sampler.end("long", {"error": "Command timed out after 600000 ms"}, now=610)
    assert "your chosen timeout" in text and "platform" not in text
    assert sampler.commands["long"]["wall_s"] == pytest.approx(600)
