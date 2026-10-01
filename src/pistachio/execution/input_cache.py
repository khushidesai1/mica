"""Task-local immutable input reuse; never trust cache files without an integrity identity."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from pistachio.execution.workspace import digest
from pistachio.models import ExecutionBundle


class InputCache:
    def __init__(self, root: Path):
        self.root = root
        self.entries: dict[str, tuple[dict[str, str], dict[str, tuple[int, int, int, int]]]] = {}

    @staticmethod
    def stamp(path: Path) -> tuple[int, int, int, int]:
        s = path.stat()
        return s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    def stage(
        self, bundle: ExecutionBundle, destination: Path
    ) -> tuple[dict[str, str], dict[str, Any]]:
        from pistachio.execution.backends import LocalExecutor, sha256_file

        key = digest(
            [
                bundle.task_id,
                bundle.runtime_image,
                [a.model_dump(mode="json") for a in bundle.input_attachments],
                [d.model_dump(mode="json") for d in bundle.dataset_inputs],
            ]
        )
        directory = self.root / key
        hit = key in self.entries
        started = time.monotonic()
        if hit:
            checksums, stamps = self.entries[key]
            for name, checksum in checksums.items():
                path = directory / name
                if not path.is_file() or path.is_symlink():
                    raise ValueError("cached input is missing or replaced")
                if self.stamp(path) != stamps[name] and sha256_file(path) != checksum:
                    raise ValueError("cached input checksum mismatch")
        else:
            # A supervisor restart cannot inherit a trusted in-memory integrity record.
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True)
            LocalExecutor._stage_inputs(bundle, directory)
            downloaded = time.monotonic()
            checksums = {p.name: sha256_file(p) for p in directory.iterdir()}
            for path in directory.iterdir():
                path.chmod(0o444)
            stamps = {p.name: self.stamp(p) for p in directory.iterdir()}
            self.entries[key] = checksums, stamps
        # Bind the cache directory read-only in the fresh sandbox. Avoid hard links: unlinking
        # an old operation changes ctime and would force another full-file hash on every reuse.
        destination.rmdir()
        destination.symlink_to(directory.resolve(), target_is_directory=True)
        elapsed = time.monotonic() - started
        return dict(checksums), {
            "input_cache_hit": float(hit),
            "staging_seconds": elapsed,
            "download_seconds": 0 if hit else downloaded - started,
            "hashing_seconds": elapsed if hit else time.monotonic() - downloaded,
        }


_CACHES: dict[Path, InputCache] = {}


def input_cache(root: Path) -> InputCache:
    return _CACHES.setdefault(root, InputCache(root))
