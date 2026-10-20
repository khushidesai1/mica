"""Deterministic, loss-preserving project memory layout shared by every writer."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping

from .config import Settings

CORE = ("PROJECT.md", "conventions.md", "open.md", "lessons.md")
BASE_ENTRY = (
    r"(?:PROJECT\.md|INDEX\.md|conventions\.md|lessons\.md|open\.md|"
    r"(?:data|results)/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md|"
    r"recipes/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:md|py|R|sh))"
)
ORIGINAL = rf"(?:{BASE_ENTRY}|_unmerged/[a-zA-Z0-9_-]{{1,100}}/{BASE_ENTRY})"
ARCHIVE_ENTRY = rf"archive/[a-f0-9]{{64}}/{ORIGINAL}"
ARCHIVE_INDEX = r"archive/(?:INDEX|index-[0-9]{4})\.md"


def ready(settings: Settings) -> bool:
    return time.time() >= settings.memory_archive_after


def is_index(path: str) -> bool:
    return path == "INDEX.md" or re.fullmatch(ARCHIVE_INDEX, path) is not None


def original_path(path: str) -> str:
    return path.split("/", 2)[2] if re.fullmatch(ARCHIVE_ENTRY, path) else path


def archive_path(path: str, text: str) -> str:
    digest = hashlib.sha256((path + "\0" + text).encode()).hexdigest()
    return f"archive/{digest}/{path}"


def line(path: str, *, relative: str = "") -> str:
    label = original_path(path)
    return f"- [{label}]({path.removeprefix(relative)})\n"


def prepare(
    before: Mapping[str, str], changes: Mapping[str, str | None], settings: Settings
) -> dict[str, str | None]:
    """Preserve original bytes and generate bounded indexes as one atomic proposal.

    Archive content is immutable. Model-supplied index changes are ignored: inventories
    are generated from the actual files, including conflict copies and publication receipts.
    """
    if not before and not changes:
        return {}
    files = dict(before)

    def preserve(path: str, text: str) -> None:
        target = archive_path(path, text)
        if target in files and files[target] != text:
            raise ValueError("archive_collision")
        files[target] = text

    for path, value in changes.items():
        if is_index(path):
            continue
        if path.startswith("archive/"):
            if value != before.get(path):
                raise ValueError("archive_is_immutable")
            continue
        if path in CORE and value is None:
            raise ValueError("core_entry_cannot_be_deleted")
        old = before.get(path)
        if old is not None and old != value:
            preserve(path, old)
        if value is None:
            files.pop(path, None)
        else:
            files[path] = value

    # Reserve directory navigation even before the first archive. New/changed entries
    # get priority, then the prior index order, then stable path order for older stores.
    navigation = "- [Archived originals and entries](archive/INDEX.md)\n"
    index = "".join(line(p) for p in CORE if p in files)
    if len(index + navigation) > settings.memory_index_chars:
        raise ValueError("index_budget_too_small_for_core")
    prior = re.findall(r"\[[^\]]+\]\(([^)]+)\)", before.get("INDEX.md", ""))
    active = sorted(
        p for p in files if p not in CORE and not is_index(p) and not p.startswith("archive/")
    )
    order = list(dict.fromkeys([p for p in changes if p in active] + prior + active))
    for path in order:
        if path not in active:
            continue
        entry = line(path)
        if len(index + entry + navigation) <= settings.memory_index_chars:
            index += entry
        else:
            preserve(path, files.pop(path))
    archived = sorted(p for p in files if re.fullmatch(ARCHIVE_ENTRY, p))
    # Remove only generated indexes, never entries; pages are regenerated deterministically.
    for path in list(files):
        if is_index(path):
            del files[path]
    if archived:
        pages: list[str] = []
        page = ""
        for path in archived:
            entry = line(path, relative="archive/")
            if len(entry) > settings.memory_index_chars:
                raise ValueError("archive_link_exceeds_index_budget")
            if len(page + entry) > settings.memory_index_chars:
                pages.append(page)
                page = ""
            page += entry
        if page:
            pages.append(page)
        root = ""
        for number, contents in enumerate(pages, 1):
            name = f"index-{number:04d}.md"
            root += f"- [Archive page {number}]({name})\n"
            files["archive/" + name] = contents
        if len(root) > settings.memory_index_chars:
            raise ValueError("archive_directory_full")
        files["archive/INDEX.md"] = root
        index += navigation
    files["INDEX.md"] = index
    return {p: files.get(p) for p in files.keys() | before.keys() if files.get(p) != before.get(p)}
