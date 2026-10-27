"""Fetch immutable public model weights and verify their reviewed hashes."""

import hashlib
import json
import urllib.request
from pathlib import Path


def fetch(manifest: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, asset in json.loads(manifest.read_text())["models"].items():
        target = destination / name
        if not target.is_file():
            with (
                urllib.request.urlopen(asset["url"], timeout=120) as source,
                target.open("wb") as out,
            ):
                while block := source.read(1024 * 1024):
                    out.write(block)
        with target.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if target.stat().st_size != asset["size"] or actual != asset["sha256"]:
            raise ValueError(f"Model integrity check failed: {name}")
        print(f"Verified {name}: {actual}", flush=True)


if __name__ == "__main__":
    fetch(Path("deploy/cellpose-models.json"), Path("cellpose-assets"))
