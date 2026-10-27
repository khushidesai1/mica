"""Generate/verify a single-layer copy of a pinned Docker runtime, preserving metadata."""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

FIELDS = ("Env", "User", "WorkingDir", "Entrypoint", "Cmd", "Labels")


def dockerfile(image: dict) -> str:
    config = image["Config"]
    for field in ("Volumes", "ExposedPorts", "Healthcheck", "OnBuild", "Shell", "StopSignal"):
        if config.get(field):
            raise ValueError(f"Runtime has unsupported configuration: {field}")
    lines = [
        "ARG BASE_IMAGE",
        "FROM ${BASE_IMAGE} AS runtime",
        "FROM scratch",
        "COPY --from=runtime / /",
    ]
    for item in config.get("Env") or []:
        key, value = item.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value:
            raise ValueError("Unsupported runtime environment")
        lines.append(f"ENV {key}=" + json.dumps(value).replace("$", r"\$"))
    for key, value in (config.get("Labels") or {}).items():
        lines.append("LABEL " + json.dumps(key) + "=" + json.dumps(value).replace("$", r"\$"))
    for field, instruction in (("WorkingDir", "WORKDIR"), ("User", "USER")):
        if config.get(field):
            value = config[field]
            if not re.fullmatch(r"[A-Za-z0-9_/:.-]+", value):
                raise ValueError(f"Unsupported {field}")
            lines.append(instruction + " " + value)
    for field, instruction in (("Entrypoint", "ENTRYPOINT"), ("Cmd", "CMD")):
        if config.get(field) is not None:
            lines.append(instruction + " " + json.dumps(config[field]))
    return "\n".join(lines) + "\n"


def verify(before: dict, after: dict) -> None:
    for field in FIELDS:
        assert before["Config"].get(field) == after["Config"].get(field), field
    for field in ("Architecture", "Os"):
        assert before[field] == after[field], field
    assert len(after["RootFS"]["Layers"]) == 1
    print("RUNTIME_METADATA_PRESERVED layers=1")


def import_script(image: dict, archive: str, tag: str) -> str:
    """Import one exported filesystem layer without WORKDIR adding a build layer."""
    changes = dockerfile(image).splitlines()[4:]
    args = ["docker", "import"]
    for change in changes:
        args.extend(["--change", change])
    return shlex.join([*args, archive, tag]) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--import-archive")
    parser.add_argument("--import-tag")
    args = parser.parse_args()
    before = json.loads(args.before.read_text())[0]
    if args.verify:
        verify(before, json.loads(args.output.read_text())[0])
    elif args.import_archive and args.import_tag:
        args.output.write_text(import_script(before, args.import_archive, args.import_tag))
    else:
        args.output.write_text(dockerfile(before))
