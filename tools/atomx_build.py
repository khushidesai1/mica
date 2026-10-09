"""Print the reviewed implementation digest, or validate/configure an image build."""

import base64
import hashlib
import json
import os
import re
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
        host_key = os.environ["ATOMX_HOST_KEY"]
        parts = host_key.split()
        if len(parts) != 2 or parts[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"}:
            raise SystemExit("Provide an independently verified SSH host key")
        base64.b64decode(parts[1], validate=True)
        bucket = os.environ["ATOMX_BUCKET"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+[a-z0-9]", bucket):
            raise SystemExit("Invalid destination bucket")
        # JSON is valid YAML; no shell interpretation of the public host key or bucket.
        Path("deploy/atomx/alchemy.yaml").write_text(
            json.dumps(
                {
                    "bucket": bucket,
                    "sftp": {
                        "host": "na.export.atomx.nanostring.com",
                        "port": 22,
                        "host_key": host_key,
                    },
                }
            )
        )
