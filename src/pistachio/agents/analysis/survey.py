"""Small metadata-only inventory for init; no recursive filesystem traversal."""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings

MAX_ENTRIES = 200
MAX_LISTINGS = 4
MAX_DEPTH = 2
BUDGET_SECONDS = 15


def inventory(uri: str, settings: Settings) -> dict[str, Any]:
    parsed = urlsplit(uri)
    root = parsed.path.lstrip("/")
    if root and not root.endswith("/"):
        root += "/"
    result: dict[str, Any] = {
        "data_uri": uri,
        "coverage": "bounded metadata sample, not a complete inventory",
        "limits": {
            "entries": MAX_ENTRIES,
            "listings": MAX_LISTINGS,
            "depth": MAX_DEPTH,
            "seconds": BUDGET_SECONDS,
        },
        "entries": [],
        "listings": 0,
        "content_inspected": False,
        "note": "File contents, schemas, sample counts and cell counts were not inspected.",
    }
    started = time.monotonic()
    pending = deque([(root, 0)])
    client = None
    if settings.storage == "gcs":
        from google.cloud import storage

        client = storage.Client()
    while pending and result["listings"] < MAX_LISTINGS and len(result["entries"]) < MAX_ENTRIES:
        prefix, depth = pending.popleft()
        remaining = BUDGET_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            result["stopped"] = "time budget"
            break
        try:
            result["listings"] += 1
            if client is not None:
                iterator = client.list_blobs(
                    parsed.netloc,
                    prefix=prefix,
                    delimiter="/",
                    max_results=80,
                    page_size=80,
                    timeout=min(5, remaining),
                    retry=None,
                )
                page = next(iterator.pages, None)
                objects = list(page) if page is not None else []
                entries = [
                    {"path": blob.name, "kind": "file", "size_bytes": blob.size}
                    for blob in objects
                    if blob.name != prefix
                ]
                entries += [{"path": p, "kind": "folder"} for p in sorted(iterator.prefixes)]
            else:
                entries = []
                path = settings.data_mount_root / parsed.netloc / prefix
                with os.scandir(path) as scan:
                    for item in scan:
                        if len(entries) >= 80:
                            break
                        entries.append(
                            {
                                "path": prefix + item.name + ("/" if item.is_dir() else ""),
                                "kind": "folder" if item.is_dir() else "file",
                            }
                        )
            for entry in entries:
                parts = Path(entry["path"]).parts
                if any(x.startswith(".") or x == "outputs" for x in parts):
                    continue
                if any(x.lower().endswith((".zarr", ".n5")) for x in parts):
                    entry["kind"] = "array store (contents not listed)"
                result["entries"].append(entry)
                if len(result["entries"]) >= MAX_ENTRIES:
                    break
                if entry["kind"] == "folder" and depth < MAX_DEPTH:
                    pending.append((entry["path"], depth + 1))
        except Exception as exc:
            result["stopped"] = f"metadata listing unavailable ({type(exc).__name__})"
            break
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result
