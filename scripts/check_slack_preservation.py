"""Verify that a layered Slack rollout preserves all other deployed application files."""

import argparse
import hashlib
import json
from importlib.util import find_spec
from pathlib import Path

PATCHED = {"config.py", "api/app.py", "connectors/slack.py"}


def hashes():
    spec = find_spec("pistachio")
    assert spec and spec.origin
    root = Path(spec.origin).parent
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and str(p.relative_to(root)) not in PATCHED
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "check"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.mode == "export":
        print(json.dumps(hashes(), sort_keys=True))
    else:
        expected, actual = json.loads(Path(args.path).read_text()), hashes()
        changed = [path for path, digest in expected.items() if actual.get(path) != digest]
        assert not changed, changed
        print(f"PRESERVED_DEPLOYED_FILES={len(expected)}")
