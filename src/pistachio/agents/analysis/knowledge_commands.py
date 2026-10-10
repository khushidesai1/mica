"""Project-scoped human review and correction of shared knowledge."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import knowledge
from .access import Grant, deny_text
from .commands import Command
from .config import Settings
from .identity import user_key
from .registry import ProjectEntry, Registry, RegistryDoc, project_key
from .storage import Storage

NAMES = {"knowledge", "knowledge show", "forget knowledge", "undo knowledge", "knowledge github"}


def mirror_project(project: ProjectEntry) -> bool:
    if project.knowledge_github is not None:
        return project.knowledge_github
    return bool(project.members or project.published_channels)


def _prior(storage: Storage, pid: str, event_id: str, settings: Settings) -> knowledge.Saved | None:
    operation = hashlib.sha256(f"{event_id}:0".encode()).hexdigest()
    try:
        receipt = json.loads(
            storage.get(project_key(pid) + f"/knowledge-control/operations/{operation}.json")[0]
        )
    except FileNotFoundError:
        return None
    if "summary" in receipt:
        return knowledge.Saved.model_validate(receipt["summary"])
    # Resume a pending journal before selecting a new undo target.
    return knowledge.commit(storage, pid, {}, {}, settings, event_id=event_id, user_key="")


def run(
    cmd: Command, grant: Grant, storage: Storage, event_id: str, settings: Settings
) -> tuple[str, knowledge.Saved | None]:
    if not grant.allowed:
        return deny_text(grant), None
    pid = grant.project_id
    if cmd.name == "knowledge github":
        if not grant.doc.owns(pid, grant.email):
            raise ValueError("Only the project owner can change GitHub mirroring.")
        enabled = cmd.args[0] == "on"

        def set_opt_in(doc: RegistryDoc) -> str:
            if not doc.owns(pid, grant.email):
                raise ValueError("Only the project owner can change GitHub mirroring.")
            doc.projects[pid].knowledge_github = enabled
            return pid

        Registry(storage).update(
            event_id, grant.email, f"knowledge github {cmd.args[0]}", set_opt_in
        )
        return (
            "GitHub mirroring enabled for this project. It will run once the private repository "
            "and control-plane credential are configured. Repository readers can see this "
            "project's mirrored knowledge and its history."
            if enabled
            else (
                "Future GitHub mirroring disabled for this project. "
                "Existing GitHub history remains."
            )
        ), None
    base = knowledge.load(storage, pid, settings)
    if cmd.name == "knowledge":
        histories = knowledge.history_keys(storage, pid)
        latest = (
            json.loads(storage.get(histories[-1])[0])["timestamp"] if histories else "no saves yet"
        )
        index = base["INDEX.md"].data.decode() if "INDEX.md" in base else "No index yet."
        return f"{len(base)} files · Last update: {latest}\n{index}", None
    if cmd.name == "knowledge show":
        path = knowledge.valid_path(cmd.args[0])
        if path not in base:
            return "That knowledge entry does not exist.", None
        return f"{path}\n" + base[path].data.decode()[:3500], None
    result = _prior(storage, pid, event_id, settings)
    if result is None:
        updates: dict[str, bytes | None] = {}
        if cmd.name == "forget knowledge":
            path = knowledge.valid_path(cmd.args[0])
            if path not in base:
                return "That knowledge entry does not exist.", None
            updates[path] = None
            if path != "INDEX.md" and "INDEX.md" in base:
                index = base["INDEX.md"].data.decode()
                lines = [
                    line
                    for line in index.splitlines(keepends=True)
                    if not re.search(r"\]\(" + re.escape(path) + r"\)", line)
                ]
                if "".join(lines) != index:
                    updates["INDEX.md"] = "".join(lines).encode()
        else:
            histories = knowledge.history_keys(storage, pid)
            latest_doc: dict[str, Any] | None = None
            root = ""
            for key in reversed(histories):
                doc = json.loads(storage.get(key)[0])
                if doc.get("status") == "complete":
                    latest_doc, root = doc, key.removesuffix("CHANGES.json")
                    break
            if latest_doc is None:
                return "There is no knowledge save to undo.", None
            for change in latest_doc["changes"]:
                if change.get("skipped"):
                    continue
                path = knowledge.valid_path(change["path"])
                updates[path] = (
                    None
                    if change["operation"] == "added"
                    else storage.get(root + "before/" + path)[0]
                )
                # Undo only the saved generation, never a later edit.
                generation = change["after_generation"]
                if generation is None:
                    base.pop(path, None)
                else:
                    base[path] = knowledge.Version(
                        storage.get(root + "after/" + path)[0], generation
                    )
        result = knowledge.commit(
            storage, pid, updates, base, settings, event_id=event_id, user_key=user_key(grant.email)
        )
    if cmd.name == "forget knowledge":
        text = "Removed from project knowledge. Previous versions are kept in project history."
        was_mirrored = any(
            json.loads(storage.get(k)[0]).get("mirrored")
            for k in knowledge.history_keys(storage, pid)
        )
        if was_mirrored or (settings.knowledge_github_repo and mirror_project(grant.project)):
            text = (
                "Removed from project knowledge. Older versions remain in the GitHub "
                "history; ask an admin to purge them if needed."
            )
    else:
        text = "Restored the files changed by the most recent knowledge save."
    if result.conflicts or result.rejected:
        text = (
            "Some knowledge changes could not be applied; review `knowledge`. "
            + knowledge.notice(result)
        )
    return text, result
