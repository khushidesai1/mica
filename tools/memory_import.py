"""One-time GCS PROJECT.md import. Defaults to dry-run; existing GitHub guides are skipped."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging

from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.project_memory import GitHubMemory, validate
from pistachio.agents.analysis.registry import Registry
from pistachio.agents.analysis.storage import GCSStorage, Storage


async def import_guides(
    storage: Storage, backend: GitHubMemory, *, apply: bool
) -> list[dict[str, str]]:
    results = []
    for pid in sorted(Registry(storage).load().projects):
        try:
            data, _ = await asyncio.to_thread(storage.get, f"projects/{pid}/PROJECT.md")
        except FileNotFoundError:
            continue
        text = validate("PROJECT.md", data, backend.settings)
        base = await backend.read(pid, event_id="import")
        if base is None:
            raise RuntimeError("Cannot read GitHub; import stopped")
        if "PROJECT.md" in base.files or "PROJECT.md" in base.invalid:
            results.append({"project": pid, "status": "existing-guide-skipped"})
            continue
        result = {"project": pid, "status": "would-import", "bytes": str(len(data))}
        if apply:
            saved = await backend.commit(
                pid,
                base,
                {"PROJECT.md": text},
                source="init",
                event_id="import-" + hashlib.sha256(data).hexdigest()[:16],
                author_line="One-time import from the existing GCS project guide",
            )
            if saved.rejected or saved.unmerged:
                raise RuntimeError("Import conflicted or was rejected; inspect project history")
            result.update(status="imported", commit=saved.commit_oid)
        results.append(result)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Existing Pistachio artifact bucket")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    for result in asyncio.run(
        import_guides(GCSStorage(args.bucket), GitHubMemory(Settings()), apply=args.apply)
    ):
        print(" ".join(f"{key}={value}" for key, value in result.items()))
