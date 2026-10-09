"""Fetch and verify pinned build inputs. Used only when an image build is requested."""

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path


def verify_assets(manifest: dict, directory: Path) -> None:
    for asset in manifest["assets"]:
        path = directory / asset["name"]
        if path.stat().st_size != asset["size"]:
            raise ValueError(f"Size mismatch: {asset['name']}")
        algorithm = "sha256" if "sha256" in asset else "md5"
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, algorithm)
        actual = (
            digest.hexdigest()
            if algorithm == "sha256"
            else base64.b64encode(digest.digest()).decode()
        )
        if actual != asset[algorithm]:
            raise ValueError(f"Checksum mismatch: {asset['name']}")


def fetch_assets(manifest: dict, assets: Path) -> None:
    assets.mkdir(parents=True, exist_ok=True)
    for asset in manifest["assets"]:
        subprocess.run(
            ["gcloud", "storage", "cp", asset["uri"], str(assets / asset["name"])], check=True
        )
    verify_assets(manifest, assets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["fetch", "verify"])
    parser.add_argument(
        "--manifest", type=Path, default=Path("deploy/spatial-analysis-assets.json")
    )
    parser.add_argument("--assets", type=Path, default=Path("spatial-assets"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.action == "fetch":
        fetch_assets(manifest, args.assets)
    else:
        verify_assets(manifest, args.assets)
