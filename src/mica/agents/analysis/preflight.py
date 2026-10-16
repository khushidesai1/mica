"""Fail before connecting to Slack if production isolation prerequisites are absent."""

import os
import platform
import shutil

from .config import Settings


def check(settings: Settings) -> None:
    if not settings.sci_python.is_file() or not (settings.tools_dir / "inspect_data.py").is_file():
        raise RuntimeError("Install the science environment and inspect_data.py before starting")
    if settings.storage == "gcs":
        if platform.system() != "Linux" or not all(shutil.which(x) for x in ("bwrap", "socat")):
            raise RuntimeError(
                "Production requires Linux, bubblewrap and socat; sandbox stays enabled"
            )
        for bucket in settings.csv("data_buckets"):
            path = settings.data_mount_root / bucket
            if not path.is_mount() or not os.statvfs(path).f_flag & os.ST_RDONLY:
                raise RuntimeError(
                    f"Data bucket must be mounted read-only before startup: {bucket}"
                )
