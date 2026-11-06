"""Validate a completed CosMx transfer using listings and TIFF headers, never pixels."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import tifffile


def local(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Sample paths must be inside bucket_root")
    return path


def marker(path):
    lines = path.read_text().splitlines()
    if not lines or lines[0] != "# AtoMx transfer marker v1":
        raise ValueError(f"{path.name}: expected marker v1")
    fields = {}
    for line in lines[1:]:
        if not line.strip():
            break
        key, value = line.split(":", 1)
        if key in fields:
            raise ValueError("Duplicate marker field")
        fields[key] = value.strip()
    if fields.get("status") != "done" or fields.get("verified") != "crc32c":
        raise ValueError("A verified transfer is required; listing-only backfills are incomplete")
    return fields


def fov(path):
    found = re.search(r"(?:_F|/FOV)(\d+)(?:\D|$)", str(path), re.I)
    return int(found[1]) if found else None


def check(request):
    root = Path(request["bucket_root"]).resolve()
    sample = request["sample"]
    folders = {
        "raw": local(root, request["raw"]),
        "additional_files": local(root, request["additional"]),
    }
    errors, counts, markers, files = [], {}, {}, {}
    for scope, folder in folders.items():
        try:
            markers[scope] = marker(folder / "done.txt")
            if markers[scope].get("sample") != sample or markers[scope].get("scope") != scope:
                raise ValueError("Marker sample/scope mismatch")
            paths = [p for p in folder.rglob("*") if p.is_file() and p.name != "done.txt"]
            if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in paths):
                raise ValueError("Linked files are not supported")
            files[scope] = paths
            counts[scope] = {
                "n_files": len(paths),
                "total_bytes": sum(p.stat().st_size for p in paths),
            }
            for key, marker_key in [("n_files", "scope_files"), ("total_bytes", "scope_bytes")]:
                if counts[scope][key] != int(markers[scope][marker_key]):
                    raise ValueError(f"{scope}: listing does not match marker {marker_key}")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(str(exc))
    if len(markers) == 2 and len(files) == 2:
        try:
            record = markers["raw"]["record"]
            if markers["additional_files"]["record"] != record:
                raise ValueError("Markers refer to different authoritative records")
            if record.startswith("gs://"):
                parsed = urlsplit(record)
                if parsed.netloc != request.get("bucket", root.name):
                    raise ValueError("Authoritative record is in a different bucket")
                record = parsed.path.lstrip("/")
            body = json.loads(local(root, record).read_text())
            if (
                body.get("version") != 1
                or body.get("sample") != sample
                or body.get("provenance", "transfer") != "transfer"
                or not body.get("finished")
                or not body.get("manifest")
            ):
                raise ValueError("Invalid authoritative completion record")
            actual = {
                str(p.relative_to(root)): p.stat().st_size
                for paths in files.values()
                for p in paths
            }
            manifest = {entry["name"]: entry["size"] for entry in body["manifest"]}
            if len(manifest) != len(body["manifest"]) or actual != manifest:
                raise ValueError("Folder listing differs from the authoritative manifest")
            for field, expected in [
                ("n_files", len(actual)),
                ("total_bytes", sum(actual.values())),
            ]:
                if body[field] != expected:
                    raise ValueError(f"Authoritative {field} differs from listing")
            for fields in markers.values():
                if (
                    int(fields["sample_files"]) != len(actual)
                    or int(fields["sample_bytes"]) != sum(actual.values())
                    or fields.get("run_id") != body.get("run_id")
                    or fields.get("finished") != body.get("finished")
                ):
                    raise ValueError(
                        "Marker sample totals or identity differ from authoritative record"
                    )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    raw = folders["raw"]
    required = [
        "*_tx_file.csv.gz",
        "*_fov_positions_file.csv.gz",
        "*-polygons.csv.gz",
        "Morphology_ChannelID_Dictionary.txt",
    ]
    for pattern in required:
        if len(list(raw.glob(pattern))) != 1:
            errors.append(f"Expected exactly one raw file matching {pattern}")
    expected = set()
    try:
        positions = list(raw.glob("*_fov_positions_file.csv.gz"))
        if len(positions) != 1:
            raise ValueError("Cannot identify FOV positions")
        with gzip.open(positions[0], "rt") as stream:
            reader = csv.DictReader(stream)
            column = next((c for c in reader.fieldnames or [] if c.lower() == "fov"), None)
            if not column:
                raise ValueError("FOV positions require an FOV column")
            rows = [int(row[column]) for row in reader]
        expected = set(rows)
        if not rows or len(rows) != len(expected):
            raise ValueError("FOV positions contain no rows or duplicate FOVs")
        images = [
            p for p in (raw / "Morphology2D").glob("*") if p.suffix.lower() in {".tif", ".tiff"}
        ]
        if len(images) != len(rows) or {fov(p) for p in images} != expected:
            errors.append("Morphology2D TIFs do not match the FOV-position rows")
        extra = files.get("additional_files", [])
        for label, selected in {
            "CellLabels": [p for p in extra if p.name.startswith("CellLabels_F")],
            "CellComposite": [p for p in extra if p.name.startswith("CellComposite_F")],
            "transcript coordinates": [
                p for p in extra if p.name.endswith("complete_code_cell_target_call_coord.csv")
            ],
        }.items():
            if selected and {fov(p) for p in selected} != expected:
                errors.append(f"Additional {label} FOVs do not match positions")
    except (OSError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    tiffs = [
        p
        for folder in folders.values()
        for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in {".tif", ".tiff"}
    ]
    for path in tiffs:
        try:
            with tifffile.TiffFile(path) as tif:
                if not len(tif.pages):
                    raise ValueError("Empty TIFF")
                size = path.stat().st_size
                for page in tif.pages:
                    if not page.shape or any(
                        o + n > size
                        for o, n in zip(page.dataoffsets, page.databytecounts, strict=True)
                    ):
                        raise ValueError("TIFF references missing pixel bytes")
        except Exception as exc:
            errors.append(f"Unreadable TIFF header {path.relative_to(root)}: {exc}")
    return {
        "schema_version": 1,
        "sample": sample,
        "complete": not errors,
        "errors": errors,
        "scope_counts": counts,
        "fovs": len(expected),
        "tiff_headers_checked": len(tiffs),
        "verification": (
            "Marker provenance, file listings and TIFF headers; pixels/checksums not reread"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request = json.loads(args.request)
    try:
        result = check(request)
    except (ValueError, KeyError, OSError) as exc:
        result = {"schema_version": 1, "complete": False, "errors": [str(exc)]}
    encoded = json.dumps(result, indent=2)
    if request.get("output"):
        path = Path(request["output"]).resolve()
        if not path.is_relative_to(Path("outputs").resolve()):
            raise ValueError("Write the check report under outputs/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded + "\n")
    print(encoded)
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
