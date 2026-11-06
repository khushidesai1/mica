"""Inspect or replace Mica's live lab list using a single compare-and-swap write."""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from mica.agents.analysis.lab import KEY, Lab
from mica.agents.analysis.storage import Conflict, GCSStorage, LocalStorage, Storage


def read(storage: Storage) -> tuple[Lab, str | None]:
    try:
        raw, generation = storage.get(KEY)
        return Lab.model_validate_json(raw), generation
    except FileNotFoundError:
        return Lab(), None


def replace(storage: Storage, proposed: Lab, *, expected: str | None = None) -> str | None:
    current, generation = read(storage)
    if expected is not None and expected != (generation or "0"):
        raise Conflict("The lab list changed since the expected generation; review it again.")
    before, after = (
        current.model_dump_json(indent=2) + "\n",
        proposed.model_dump_json(indent=2) + "\n",
    )
    print(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"{KEY}@{generation or 'missing'}",
                tofile="proposed",
            )
        ),
        end="",
    )
    if generation is not None and before == after:
        print("No changes.")
        return generation
    # Deliberately do not retry: a concurrent operator edit needs a new review.
    return storage.put_if(KEY, after.encode(), generation)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--bucket", help="Mica artifacts bucket")
    target.add_argument("--root", type=Path, help="Local artifacts root (offline use)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    setting = commands.add_parser("set")
    setting.add_argument("file", type=Path)
    setting.add_argument("--generation", help="Expected generation; 0 requires a missing document")
    args = parser.parse_args(argv)
    # Validate operator input before opening the remote store.
    proposed = Lab.model_validate_json(args.file.read_bytes()) if args.command == "set" else None
    storage: Storage = GCSStorage(args.bucket) if args.bucket else LocalStorage(args.root)
    if proposed is None:
        current, generation = read(storage)
        print(current.model_dump_json(indent=2))
    else:
        generation = replace(storage, proposed, expected=args.generation)
    print(f"Generation: {generation or '0 (missing)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
