"""Read-only inventories and copy verification for Mica's managed artifacts bucket.

No object bodies, secrets, cloud writes, or copies are accessed. JSONL evidence is
created exclusively with mode 0600. Repeat the source inventory after the final
write freeze and again after copying; both must agree before accepting a copy.
Snapshot patches are proposals only and require a later frozen, conditional apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT = "azizilab-aml"
SOURCE = "pistachio-staging-artifacts-5304c19c"
DESTINATION = re.compile(r"mica-staging-artifacts-[a-z0-9]+(?:-[a-z0-9]+)*")
PRESERVED = (
    "contentType", "cacheControl", "contentDisposition", "contentEncoding", "contentLanguage",
    "customTime", "storageClass", "kmsKeyName", "temporaryHold", "eventBasedHold",
)
FIELDS = (
    "nextPageToken,items(name,size,generation,metageneration,crc32c,md5Hash,"
    "contentType,cacheControl,contentDisposition,contentEncoding,contentLanguage,"
    "customTime,storageClass,kmsKeyName,temporaryHold,eventBasedHold,metadata,timeCreated)"
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def buckets(source: str, destination: str) -> None:
    if source != SOURCE or not DESTINATION.fullmatch(destination) or len(destination) > 63:
        raise ValueError("Use the existing managed source and an explicit Mica artifacts bucket")
    if source == destination:
        raise ValueError("Source and destination must differ")


def object_record(properties: dict[str, Any]) -> dict[str, Any]:
    """Preserve verifiable metadata fingerprints without disclosing metadata values."""
    metadata = {
        k: v for k, v in properties.get("metadata", {}).items()
        if k != "x-goog-reserved-source-generation"
    }
    preserved = {
        key: properties.get(key, False if key.endswith("Hold") else None)
        for key in PRESERVED
    }
    preserved["metadata"] = metadata
    record = {
        "kind": "object", "name": properties["name"], "size": int(properties["size"]),
        "generation": str(properties["generation"]),
        "metageneration": str(properties["metageneration"]),
        "crc32c": properties.get("crc32c"), "md5": properties.get("md5Hash"),
        "created": properties.get("timeCreated"),
        "metadata_hash": hashlib.sha256(canonical(preserved)).hexdigest(),
    }
    if not record["crc32c"] or record["size"] < 0:
        raise ValueError("Object metadata lacks a checksum or valid size")
    return record


def write_evidence(
    path: Path, header: dict[str, Any], rows: Iterable[dict[str, Any]], *, complete: bool = True
) -> dict[str, Any]:
    """An interrupted write has no valid footer and can never pass verification."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    count = 0
    size = 0
    digest = hashlib.sha256()
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical({"kind": "header", "version": 1, **header}))
        for row in rows:
            data = canonical(row)
            digest.update(data)
            handle.write(data)
            count += 1
            size += int(row.get("size", 0))
        footer = {
            "kind": "footer", "complete": complete, "count": count,
            "bytes": size, "sha256": digest.hexdigest(),
        }
        handle.write(canonical(footer))
    return footer


def read_evidence(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with path.open("rb") as handle:
        header = json.loads(next(handle))
        if header.get("kind") != "header" or header.get("version") != 1:
            raise ValueError("Unsupported evidence header")
        rows = []
        digest = hashlib.sha256()
        footer = None
        for line in handle:
            value = json.loads(line)
            if footer is not None:
                raise ValueError("Trailing evidence after footer")
            if value.get("kind") == "footer":
                footer = value
            else:
                rows.append(value)
                digest.update(canonical(value))
    if not footer or footer.get("complete") is not True:
        raise ValueError("Incomplete evidence cannot verify a copy")
    if (footer.get("count") != len(rows)
            or footer.get("bytes") != sum(int(row.get("size", 0)) for row in rows)
            or footer.get("sha256") != digest.hexdigest()):
        raise ValueError("Evidence integrity mismatch")
    return header, rows


def inventory(client: Any, bucket: str, project_number: str, path: Path, limit: int) -> dict:
    metadata = client.get_bucket(bucket)
    if str(metadata._properties.get("projectNumber")) != project_number:
        raise ValueError("Bucket is not owned by the approved project")
    if limit < 1:
        raise ValueError("Inventory object limit must be positive")

    def objects() -> Iterable[dict[str, Any]]:
        for index, blob in enumerate(client.list_blobs(bucket, fields=FIELDS, page_size=1000)):
            if index >= limit:
                raise ValueError("Inventory exceeded its bound; partial output is not verifiable")
            yield object_record(blob._properties)

    return write_evidence(path, {
        "type": "inventory", "project": PROJECT, "project_number": project_number,
        "bucket": bucket, "at": datetime.now(UTC).isoformat(),
    }, objects())


def inventory_records(
    header: dict[str, Any], rows: list[dict[str, Any]], bucket: str
) -> dict[str, dict[str, Any]]:
    if (header.get("type") != "inventory" or header.get("project") != PROJECT
            or header.get("bucket") != bucket or not header.get("project_number")):
        raise ValueError("Inventory scope does not match the approved bucket/project")
    result = {}
    for row in rows:
        if row.get("kind") != "object" or not row.get("name") or not row.get("crc32c"):
            raise ValueError("Invalid inventory record")
        if row["name"] in result:
            raise ValueError("Duplicate object name in inventory")
        result[row["name"]] = row
    return result


def load_inventory(path: Path, bucket: str) -> dict[str, dict[str, Any]]:
    header, rows = read_evidence(path)
    return inventory_records(header, rows, bucket)


def verify(
    source: str, destination: str, before_path: Path, after_path: Path,
    destination_path: Path, output: Path,
) -> dict[str, Any]:
    buckets(source, destination)
    inputs = [read_evidence(path) for path in [before_path, after_path, destination_path]]
    numbers = {header.get("project_number") for header, _ in inputs}
    if len(numbers) != 1 or not str(next(iter(numbers))).isdigit():
        raise ValueError("Inventories must belong to the same verified project number")
    before, after, copied = [
        inventory_records(header, rows, bucket)
        for (header, rows), bucket in zip(inputs, [source, source, destination], strict=True)
    ]
    problems: Counter[str] = Counter()
    records = []
    for name in sorted(set(before) | set(after) | set(copied)):
        a, b, c = before.get(name), after.get(name), copied.get(name)
        if a != b:
            problems["source_changed"] += 1
            continue
        if b is None:
            problems["destination_extra"] += 1
            continue
        if c is None:
            problems["destination_missing"] += 1
            continue
        if b["size"] != c["size"] or b["crc32c"] != c["crc32c"]:
            problems["content_mismatch"] += 1
            continue
        if b.get("md5") and c.get("md5") and b["md5"] != c["md5"]:
            problems["content_mismatch"] += 1
            continue
        if b["metadata_hash"] != c["metadata_hash"]:
            problems["metadata_mismatch"] += 1
            continue
        records.append({
            "kind": "mapping", "name": name, "size": b["size"], "crc32c": b["crc32c"],
            "source_generation": b["generation"], "destination_generation": c["generation"],
            "source_metageneration": b["metageneration"],
            "destination_metageneration": c["metageneration"],
        })
    result = {
        "verified": not problems, "matched": len(records), "issues": dict(problems),
        "source_objects": len(after), "destination_objects": len(copied),
    }
    write_evidence(output, {
        "type": "copy-verification", "project": PROJECT, "source": source,
        "destination": destination, "at": datetime.now(UTC).isoformat(), **result,
    }, records)
    return result


def snapshot_proposals(mapping_path: Path, snapshots_path: Path, output: Path) -> dict[str, Any]:
    """Propose only TaskRecord.paused_state_uri replacements; never edit historical payloads."""
    header, rows = read_evidence(mapping_path)
    source, destination = header.get("source", ""), header.get("destination", "")
    buckets(source, destination)
    if (header.get("type") != "copy-verification" or header.get("project") != PROJECT
            or header.get("verified") is not True):
        raise ValueError("Snapshot proposals require a verified copy")
    mapping = {row["name"]: row for row in rows if row.get("kind") == "mapping"}
    if len(mapping) != len(rows):
        raise ValueError("Invalid generation mapping")
    snapshots = json.loads(snapshots_path.read_text())
    if not isinstance(snapshots, list) or len(snapshots) > 100:
        raise ValueError("Use a bounded snapshot metadata export")
    proposals = []
    seen = set()
    for item in snapshots:
        if set(item) != {"id", "paused_state_uri", "update_time", "status"}:
            raise ValueError("Export only snapshot identifiers and concurrency metadata")
        task_id, uri = item["id"], item["paused_state_uri"]
        if not re.fullmatch(r"task_[a-f0-9]{32}", task_id) or task_id in seen:
            raise ValueError("Invalid or duplicate task identifier")
        seen.add(task_id)
        if not item["update_time"] or not item["status"]:
            raise ValueError("Snapshot updates require Firestore concurrency metadata")
        target = urlsplit(uri)
        name = target.path.lstrip("/")
        if (target.scheme != "gs" or target.netloc != source or target.query
                or not re.fullmatch(rf"paused/{task_id}/[0-9]+-[a-f0-9]{{12}}\.json", name)
                or not target.fragment.isdigit()):
            raise ValueError("Snapshot URI is outside the exact managed task scope")
        matched = mapping.get(name)
        if not matched or matched["source_generation"] != target.fragment:
            raise ValueError("Snapshot generation is not covered by the verified copy")
        proposals.append({
            "kind": "snapshot-proposal", "document": f"tasks/{task_id}",
            "precondition": {"update_time": item["update_time"], "status": item["status"],
                             "paused_state_uri": uri},
            "updates": {"paused_state_uri":
                        f"gs://{destination}/{name}#{matched['destination_generation']}"},
        })
    write_evidence(output, {
        "type": "snapshot-proposals", "dry_run": True, "requires_write_freeze": True,
        "project": PROJECT, "source": source, "destination": destination,
    }, proposals)
    return {"proposals": len(proposals), "dry_run": True, "requires_write_freeze": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory")
    inv.add_argument("--source", required=True)
    inv.add_argument("--destination", required=True)
    inv.add_argument("--side", choices=["source", "destination"], required=True)
    inv.add_argument("--max-objects", type=int, default=250000)
    inv.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--source", required=True)
    check.add_argument("--destination", required=True)
    check.add_argument("--source-before", type=Path, required=True)
    check.add_argument("--source-after", type=Path, required=True)
    check.add_argument("--destination-inventory", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    proposal = commands.add_parser("snapshot-proposals")
    proposal.add_argument("--mapping", type=Path, required=True)
    proposal.add_argument("--snapshots", type=Path, required=True)
    proposal.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            from google.cloud.storage import Client
            from google.oauth2.credentials import Credentials

            buckets(args.source, args.destination)
            # Use the operator's existing gcloud login; do not print/persist the access token.
            token = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            number = subprocess.run(
                ["gcloud", "projects", "describe", PROJECT, "--format=value(projectNumber)"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            client = Client(project=PROJECT, credentials=Credentials(token))
            result = inventory(
                client, getattr(args, args.side), number, args.output, args.max_objects
            )
        elif args.command == "verify":
            result = verify(
                args.source, args.destination, args.source_before, args.source_after,
                args.destination_inventory, args.output,
            )
        else:
            result = snapshot_proposals(args.mapping, args.snapshots, args.output)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("verified", True) else 1
    except Exception as exc:
        # API exception messages can contain object names; only expose the exception class.
        print(json.dumps({"error": type(exc).__name__, "success": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
