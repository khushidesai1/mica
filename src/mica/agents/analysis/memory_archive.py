"""Deterministic, loss-preserving project memory layout shared by every writer."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Mapping

from .config import Settings
from .thread_memory import THREAD_ENTRY

CORE = ("PROJECT.md", "conventions.md", "open.md", "lessons.md")
BASE_ENTRY = (
    rf"(?:{THREAD_ENTRY}|THREADS\.md|PROJECT\.md|INDEX\.md|conventions\.md|lessons\.md|open\.md|"
    r"(?:data|results)/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md|"
    r"recipes/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:md|py|R|sh))"
)
ORIGINAL = rf"(?:{BASE_ENTRY}|_unmerged/[a-zA-Z0-9_-]{{1,100}}/{BASE_ENTRY})"
ARCHIVE_ENTRY = rf"archive/[a-f0-9]{{64}}/{ORIGINAL}"
ARCHIVE_INDEX = r"archive/(?:INDEX|index-[0-9]{4})\.md"


def ready(settings: Settings) -> bool:
    return time.time() >= settings.memory_archive_after


def is_index(path: str) -> bool:
    return path in {"INDEX.md", "THREADS.md"} or re.fullmatch(ARCHIVE_INDEX, path) is not None


def original_path(path: str) -> str:
    return path.split("/", 2)[2] if re.fullmatch(ARCHIVE_ENTRY, path) else path


def archive_path(path: str, text: str) -> str:
    digest = hashlib.sha256((path + "\0" + text).encode()).hexdigest()
    return f"archive/{digest}/{path}"


def title(text: str) -> str:
    """Describe an entry without exposing mechanical metadata as its title."""
    body = re.sub(r"\A(?:(?:type|source|status):[^\n]*(?:\n|$))+", "", text).lstrip("\n")
    heading = re.search(
        r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$|^([^\n]+)\n {0,3}(?:=+|-+)[ \t]*$",
        body, re.M,
    )
    value = (
        (heading[1] or heading[2]) if heading else
        next((s for s in body.splitlines() if s.strip()), "")
    )
    return " ".join(value.replace("[", "").replace("]", "").split())[:80]


def line(path: str, text: str = "", *, relative: str = "") -> str:
    label = original_path(path)
    suffix = f": {title(text)}" if path.endswith(".md") and not relative else ""
    return f"- [{label}]({path.removeprefix(relative)}){suffix}\n"


def prepare(
    before: Mapping[str, str], changes: Mapping[str, str | None], settings: Settings,
    *, required_paths: frozenset[str] = frozenset(),
) -> dict[str, str | None]:
    """Preserve original bytes and generate bounded indexes as one atomic proposal.

    Archive content is immutable. Model-supplied index changes are ignored: inventories
    are generated from the actual files, including conflict copies and publication receipts.
    """
    if not before and not changes:
        return {}
    before = {p: t for p, t in before.items() if not p.startswith("threads/") and p != "THREADS.md"}
    changes = {
        p: t for p, t in changes.items() if not p.startswith("threads/") and p != "THREADS.md"
    }
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
        if path in required_paths and value is None:
            raise ValueError("active_ambient_recipe_cannot_be_deleted")
        old = before.get(path)
        if old is not None and old != value:
            preserve(path, old)
        if value is None:
            files.pop(path, None)
        else:
            files[path] = value

    # Reserve archive navigation only when needed. Above the hard limit, changed
    # entries get priority, then prior index order and stable path order for older stores.
    navigation = "- [Archived originals and entries](archive/INDEX.md)\n"
    index = "".join(line(p, files[p]) for p in CORE if p in files)
    required = sorted(p for p in required_paths if p in files and p not in CORE)
    index += "".join(line(p, files[p]) for p in required)
    has_archive = any(re.fullmatch(ARCHIVE_ENTRY, p) for p in files)
    full = index + "".join(
        line(p, files[p]) for p in files
        if p not in CORE and p not in required and not is_index(p)
        and not p.startswith("archive/")
    )
    overflow = len(full + (navigation if has_archive else "")) > settings.memory_index_chars
    reserve = navigation if has_archive or overflow else ""
    if len(index + reserve) > settings.memory_index_chars:
        raise ValueError("index_budget_too_small_for_core_and_active_recipes")
    prior = re.findall(r"\[[^\]]+\]\(([^)]+)\)", before.get("INDEX.md", ""))
    active = sorted(
        p for p in files if p not in CORE and p not in required
        and not is_index(p) and not p.startswith("archive/")
    )
    order = list(dict.fromkeys([p for p in changes if p in active] + prior + active))
    demoted = 0
    for path in order:
        if path not in active:
            continue
        entry = line(path, files[path])
        if len(index + entry + reserve) <= settings.memory_index_chars:
            index += entry
        else:
            preserve(path, files.pop(path))
            demoted += 1
    if demoted:
        logging.warning(
            "memory_index_backstop demoted=%d hard_limit=%d", demoted, settings.memory_index_chars
        )
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
