"""Complete immediate-child metadata listings within a shared time budget."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .data_links import data_uri

BUDGET_SECONDS = 20
_LOG = logging.getLogger("pistachio.inventory")
_LOG.setLevel(logging.INFO)


def inventory_uri(project_uri: str, relative_path: str, settings: Settings) -> str:
    """Constrain a model-selected folder to the authorized project's data prefix."""
    root = data_uri(project_uri).rstrip("/") + "/"
    if relative_path.startswith("/") or ":" in relative_path:
        raise ValueError("Choose a folder relative to this project's data location.")
    uri = data_uri(root + relative_path)
    parts = urlsplit(uri).path.strip("/").split("/")
    if any(
        p.startswith(".") or p == "outputs" or p.lower().endswith((".zarr", ".n5")) for p in parts
    ):
        raise ValueError("Choose a visible data folder outside Zarr/N5 stores.")
    if settings.storage == "local":
        parsed_root, parsed_uri = urlsplit(root), urlsplit(uri)
        base = settings.data_mount_root / parsed_root.netloc / parsed_root.path.lstrip("/")
        path = settings.data_mount_root / parsed_uri.netloc / parsed_uri.path.lstrip("/")
        if not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("Choose a folder inside this project's data location.")
    return uri


def _add(folder: dict[str, Any], entry: dict[str, Any]) -> None:
    parts = Path(entry["path"]).parts
    if any(p.startswith(".") or p == "outputs" for p in parts):
        return
    if any(p.lower().endswith((".zarr", ".n5")) for p in parts):
        entry["kind"] = "array store (contents not listed)"
    folder["entries"].append(entry)


def inventory(
    uris: str | Sequence[str], settings: Settings, *, event_id: str = ""
) -> dict[str, Any]:
    """List each folder once, without recursion; an incomplete listing proves no absence."""
    started = time.monotonic()
    deadline = started + BUDGET_SECONDS
    folders: list[dict[str, Any]] = []
    client = None

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("time budget")
        return seconds

    for uri in dict.fromkeys([uris] if isinstance(uris, str) else uris):
        parsed = urlsplit(uri)
        prefix = parsed.path.lstrip("/").rstrip("/")
        prefix = prefix + "/" if prefix else ""
        folder: dict[str, Any] = {"data_uri": uri, "entries": [], "complete": False}
        folders.append(folder)

        try:
            remaining()
            if settings.storage == "gcs":
                if client is None:
                    from google.cloud import storage

                    client = storage.Client()
                token = None
                while True:
                    # Fetch one page at a time so every RPC uses the remaining shared budget.
                    iterator = client.list_blobs(
                        parsed.netloc,
                        prefix=prefix,
                        delimiter="/",
                        page_size=1000,
                        page_token=token,
                        timeout=remaining(),
                        retry=None,
                    )
                    page = next(iterator.pages, None)
                    if page is not None:
                        for blob in page:
                            remaining()
                            if blob.name != prefix:
                                _add(
                                    folder,
                                    {"path": blob.name, "kind": "file", "size_bytes": blob.size},
                                )
                        for path in page.prefixes:
                            remaining()
                            _add(folder, {"path": path, "kind": "folder"})
                    token = iterator.next_page_token
                    if not token:
                        break
            else:
                path = settings.data_mount_root / parsed.netloc / prefix
                with os.scandir(path) as scan:
                    for item in scan:
                        remaining()
                        if item.is_symlink():
                            continue
                        is_dir = item.is_dir(follow_symlinks=False)
                        _add(
                            folder,
                            {
                                "path": prefix + item.name + ("/" if is_dir else ""),
                                "kind": "folder" if is_dir else "file",
                                **(
                                    {}
                                    if is_dir
                                    else {"size_bytes": item.stat(follow_symlinks=False).st_size}
                                ),
                            },
                        )
            remaining()
            folder["complete"] = True
        except TimeoutError:
            folder["stopped"] = "time budget or listing timeout"
        except Exception as exc:
            folder["stopped"] = f"metadata listing unavailable ({type(exc).__name__})"
        folder["entries"].sort(key=lambda entry: entry["path"])
    complete = all(folder["complete"] for folder in folders)
    elapsed = time.monotonic() - started
    count = sum(len(folder["entries"]) for folder in folders)
    logging.basicConfig()
    _LOG.info(
        "inventory event=%s paths=%d entries=%d complete=%s ms=%d",
        event_id,
        len(folders),
        count,
        str(complete).lower(),
        round(elapsed * 1000),
    )
    return {
        "folders": folders,
        "complete": complete,
        "entry_count": count,
        "coverage": "immediate visible children only; hidden paths and outputs excluded",
        "limits": {"seconds": BUDGET_SECONDS},
        "content_inspected": False,
        "note": "File contents, schemas, sample counts and cell counts were not inspected.",
        "elapsed_seconds": round(elapsed, 2),
    }
