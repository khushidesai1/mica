"""Import an existing guide or selected memory layout facts; dry-run unless --apply."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging

from mica.agents.analysis.config import Settings
from mica.agents.analysis.project_memory import GitHubMemory, validate
from mica.agents.analysis.registry import Registry
from mica.agents.analysis.storage import GCSStorage, Storage


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


async def layout_from_memory(
    storage: Storage,
    backend: GitHubMemory,
    pid: str,
    facts: list[str],
    *,
    apply: bool,
) -> dict[str, str]:
    """Copy explicitly reviewed, verbatim layout excerpts through the normal commit path."""
    project = Registry(storage).load().projects[pid]
    base = await backend.read(pid, event_id="layout-import")
    if base is None:
        raise RuntimeError("Cannot read GitHub; import stopped")
    if "PROJECT.md" in base.files or "PROJECT.md" in base.invalid:
        return {"project": pid, "status": "existing-guide-skipped"}
    if not facts:
        raise ValueError("Select at least one verified layout fact with --layout-fact")
    excerpts = []
    for fact in facts:
        path, separator, excerpt = fact.partition("::")
        if (
            not separator
            or not excerpt.strip()
            or "\n" in excerpt
            or path not in base.files
            or path.startswith("_unmerged/")
            or excerpt not in base.files[path].text
        ):
            raise ValueError("Each layout fact must be source.md::verbatim single-line excerpt")
        excerpts.append(f"- {excerpt.removeprefix('- ')} (source: {path}, {base.head_oid[:12]})")
    text = validate(
        "PROJECT.md",
        (
            f"# {project.name}\n\n## Data\n\nData location: `{project.data_uri}`\n\n"
            + "\n".join(excerpts)
            + "\n"
        ).encode(),
        backend.settings,
    )
    result = {"project": pid, "status": "would-import", "guide": text}
    if apply:
        saved = await backend.commit(
            pid,
            base,
            {"PROJECT.md": text},
            source="import",
            event_id="layout-import-" + hashlib.sha256(text.encode()).hexdigest()[:16],
            author_line=f"Verified layout excerpts from project memory at {base.head_oid}",
        )
        if saved.rejected or saved.unmerged:
            raise RuntimeError("Import conflicted or was rejected; inspect project history")
        result.update(status="imported", commit=saved.commit_oid)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Existing Mica artifact bucket")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--threads", action="store_true")
    parser.add_argument("--verify-cutover", action="store_true")
    parser.add_argument("--project", default="", help="Limit thread import to one project")
    parser.add_argument("--thread", default="", help="Limit import to one thread")
    parser.add_argument("--layout-from-memory", metavar="PROJECT_ID")
    parser.add_argument(
        "--layout-fact",
        action="append",
        default=[],
        metavar="SOURCE.md::EXCERPT",
        help="Reviewed, verbatim layout fact from existing memory (repeat for each fact)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.layout_fact and not args.layout_from_memory:
        parser.error("--layout-fact requires --layout-from-memory")
    store, memory = GCSStorage(args.bucket), GitHubMemory(Settings())
    if args.verify_cutover:
        import json

        from mica.agents.analysis.batch import BatchBridge
        from mica.agents.analysis.memory_artifacts import cutover_report
        from mica.config import Settings as AppSettings
        from mica.storage.firestore import FirestoreRepository

        if args.threads or args.project or args.thread or args.layout_from_memory:
            parser.error("Cutover verification must cover the whole registry")
        bridge = BatchBridge(AppSettings(gcs_bucket=args.bucket), FirestoreRepository())
        bridge.settings = memory.settings
        report = asyncio.run(cutover_report(bridge))
        if args.apply:
            if not report["complete"]:
                raise ValueError("Cutover is not ready; no receipt written")
            store.put("projects/_registry/thread-memory-cutover.json", json.dumps(report).encode())
        print(json.dumps(report))
        raise SystemExit(0)
    if args.threads:
        import json

        from mica.agents.analysis.thread_import import import_threads

        for item in asyncio.run(
            import_threads(
                store,
                memory,
                apply=args.apply,
                project=args.project,
                thread=args.thread,
                model_key=memory.settings.anthropic_api_key.get_secret_value(),
            )
        ):
            print(json.dumps(item))
        raise SystemExit(0)
    if args.project or args.thread:
        parser.error("--project and --thread require --threads")
    results = (
        [
            asyncio.run(
                layout_from_memory(
                    store, memory, args.layout_from_memory, args.layout_fact, apply=args.apply
                )
            )
        ]
        if args.layout_from_memory
        else asyncio.run(import_guides(store, memory, apply=args.apply))
    )
    for result in results:
        print(" ".join(f"{key}={value}" for key, value in result.items()))
