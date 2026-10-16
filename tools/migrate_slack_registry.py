"""Review legacy projects and import explicit owners after all old writers are stopped."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from mica.agents.analysis.identity import normalize
from mica.agents.analysis.registry import (
    REGISTRY_KEY,
    Binding,
    ProjectEntry,
    Registry,
    RegistryDoc,
)
from mica.agents.analysis.storage import GCSStorage, LocalStorage, Storage, update_model


def report(storage: Storage, user_map: dict[str, str], team: str) -> list[dict[str, object]]:
    keys = storage.keys("projects/")
    counts: dict[str, int] = {}
    for key in keys:
        if key.startswith("projects/_threads/") and key.endswith("/checkpoint.json"):
            ctx = json.loads(storage.get(key)[0]).get("context", {})
            pid = ctx.get("project_slug", "")
            counts[pid] = counts.get(pid, 0) + 1
    current = Registry(storage).load()
    rows = []
    for key in keys:
        match = re.fullmatch(r"projects/([a-z0-9-]{3,40})/project.json", key)
        if not match:
            continue
        pid = match[1]
        old = json.loads(storage.get(key)[0])
        user = old.get("created_by", "")
        owner = normalize(user_map.get(f"{team}:{user}") or user_map.get(user) or "")
        try:
            size = len(storage.get(f"projects/{pid}/PROJECT.md")[0])
        except FileNotFoundError:
            size = 0
        rows.append(
            {
                "id": pid,
                "owner": owner,
                "channel": old.get("slack_channel_id", ""),
                "data_uri": old["data_uri"],
                "brief_bytes": size,
                "threads": counts.get(pid, 0),
                "registered": pid in current.projects,
            }
        )
    return rows


def apply(
    storage: Storage,
    rows: list[dict[str, object]],
    owners: dict[str, str],
    team: str,
    *,
    quiesced: bool,
) -> list[str]:
    if not quiesced:
        raise ValueError("Stop and drain all old API/worker writers before applying migration.")
    skipped = []
    additions = RegistryDoc()
    for row in rows:
        pid, channel = str(row["id"]), str(row["channel"])
        owner = normalize(owners.get(pid, ""))
        if not re.fullmatch(r"[^\s@]+@[^\s@]+", owner):
            skipped.append(pid)
            continue
        additions.projects[pid] = ProjectEntry(
            name=pid,
            owner=owner,
            data_uri=str(row["data_uri"]),
        )
        # Channel content needs new explicit publication; migrate the pointer, not permission.
        if channel:
            additions.bindings[f"{team}:{channel}"] = Binding(project_id=pid, bound_by=owner)
    before = Registry(storage).load()

    def change(doc: RegistryDoc) -> None:
        for pid, entry in additions.projects.items():
            existing = doc.projects.get(pid)
            if existing and (existing.owner != entry.owner or existing.data_uri != entry.data_uri):
                raise ValueError("Registry ownership/data differs; review migration drift.")
            if not existing:
                doc.projects[pid] = entry
        for key, binding in additions.bindings.items():
            existing = doc.bindings.get(key)
            if existing and existing.project_id != binding.project_id:
                raise ValueError("Channel binding changed; reconcile drift before cutover.")
            if not existing:
                doc.bindings[key] = binding

    proposed = before.model_copy(deep=True)
    change(proposed)
    if proposed == before:
        return skipped
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    storage.put_if(
        f"projects/_registry/backup-{stamp}.json", before.model_dump_json().encode(), None
    )
    update_model(storage, REGISTRY_KEY, RegistryDoc, change)
    return skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bucket")
    source.add_argument("--local-root", type=Path)
    parser.add_argument("--team", required=True)
    parser.add_argument("--user-map", type=Path)
    parser.add_argument("--owners", type=Path, default=Path("owners.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--quiesced",
        action="store_true",
        help="Attest old ingress, queued tasks, and workers have been drained",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z0-9]+", args.team):
        parser.error("Invalid Slack team ID")
    storage: Storage = GCSStorage(args.bucket) if args.bucket else LocalStorage(args.local_root)
    mapping = json.loads(args.user_map.read_text()) if args.user_map else {}
    rows = report(storage, mapping, args.team)
    print(json.dumps(rows, indent=2))
    if args.apply:
        owners = json.loads(args.owners.read_text())
        print(
            json.dumps(
                {
                    "skipped_unowned": apply(
                        storage,
                        rows,
                        owners,
                        args.team,
                        quiesced=args.quiesced,
                    )
                }
            )
        )
    elif not args.owners.exists():
        args.owners.write_text(json.dumps({str(r["id"]): r["owner"] for r in rows}, indent=2))


if __name__ == "__main__":
    main()
