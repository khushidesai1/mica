"""Fail-closed, networkless execution for package imports and build backends."""

from __future__ import annotations

from pathlib import Path

PROBE = """
import os, socket
from pathlib import Path
for address, port in [('1.1.1.1', 443), ('169.254.169.254', 80)]:
    try:
        with socket.create_connection((address, port), timeout=1):
            raise RuntimeError('Sandbox network is reachable')
    except OSError:
        pass
for path in ['/mnt/gcs', '/srv/pistachio/work', '/etc/pistachio']:
    assert not Path(path).exists(), 'Sandbox exposes ' + path
assert not any(k.startswith(('ANTHROPIC_', 'SLACK_', 'GOOGLE_')) for k in os.environ)
"""


def sandbox_command(
    runtime: Path,
    command: list[str],
    *,
    read_only: tuple[Path, ...] = (),
    writable: tuple[Path, ...] = (),
) -> list[str]:
    args = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    for root in (
        Path("/usr"),
        Path("/bin"),
        Path("/opt/sci"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc/ssl"),
        runtime,
        *read_only,
    ):
        if root.exists():
            args += ["--ro-bind", str(root), str(root)]
    args += ["--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]
    for root in writable:
        args += ["--bind", str(root), str(root)]
    args += [
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "MPLBACKEND",
        "Agg",
        "--setenv",
        "MPLCONFIGDIR",
        "/tmp/mpl",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--chdir",
        "/tmp",
        "--",
    ]
    return args + command


def probe() -> None:
    """Worker-image/Batch check; fails instead of skipping when nesting is unavailable."""
    import subprocess

    from .config import Settings

    settings = Settings()
    command = sandbox_command(
        settings.sci_python.parent.parent,
        [str(settings.sci_python), "-I", "-c", PROBE + "\nprint('Runtime isolation probe passed')"],
        read_only=(settings.sci_python.resolve().parent.parent,),
    )
    subprocess.run(
        command,
        check=True,
        timeout=30,
        cwd="/tmp",
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "ANTHROPIC_API_KEY": "probe-sentinel"},
    )


if __name__ == "__main__":
    probe()
