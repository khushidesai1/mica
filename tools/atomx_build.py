"""Print or validate the reviewed implementation digest for a layout-independent image."""

import hashlib
import os
from pathlib import Path


def implementation_digest() -> str:
    files = sorted(Path("tools/atomx_transfer").glob("*.py")) + [
        Path("tools/atomx_transfer/pyproject.toml"),
        Path("docker/Dockerfile.transfer"),
        Path("tools/atomx_build.py"),
    ]
    files += sorted(Path("tools/atomx_transfer/schema").glob("*.json"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    digest = implementation_digest()
    if "ATOMX_IMPLEMENTATION_SHA" not in os.environ:
        print(digest)
    else:
        if os.environ["ATOMX_IMPLEMENTATION_SHA"] != digest:
            raise SystemExit("Implementation digest does not match reviewed code")
