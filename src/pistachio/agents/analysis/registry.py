"""Owner-scoped projects, explicit channel publication, and atomic registry operations."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .data_links import data_uri
from .identity import normalize
from .storage import Conflict, Storage, update_model

REGISTRY_KEY = "projects/_registry/registry.json"
NO_BRIEF = "No brief yet. Rely on listing and inspecting the data folder."


def now() -> str:
    return datetime.now(UTC).isoformat()


def project_key(project_id: str) -> str:
    if not re.fullmatch(r"(?:p_[a-f0-9]{16}|[a-z0-9-]{3,40})", project_id):
        raise ValueError("Invalid project identifier.")
    return f"projects/{project_id}"


class ProjectEntry(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-]{3,40}$")
    owner: str
    members: set[str] = Field(default_factory=set)
    data_uri: str
    created_at: str = Field(default_factory=now)
    access_revision: int = 0
    published_channels: set[str] = Field(default_factory=set)


class Binding(BaseModel):
    project_id: str
    bound_by: str
    bound_at: str = Field(default_factory=now)


class Operation(BaseModel):
    fingerprint: str
    result: str


class RegistryDoc(BaseModel):
    version: Literal[1] = 1
    projects: dict[str, ProjectEntry] = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    # Durable operation receipts: replaying an old share must never undo a later unshare.
    applied_events: dict[str, Operation] = Field(default_factory=dict)

    def can_use(self, project_id: str, email: str) -> bool:
        entry = self.projects.get(project_id)
        return bool(entry and (entry.owner == email or email in entry.members))

    def owns(self, project_id: str, email: str) -> bool:
        return project_id in self.projects and self.projects[project_id].owner == email

    def visible(self, email: str) -> dict[str, ProjectEntry]:
        return {key: value for key, value in self.projects.items() if self.can_use(key, email)}

    def find(self, name: str, email: str) -> str:
        if self.can_use(name, email):
            return name
        matches = [key for key, value in self.visible(email).items() if value.name == name]
        own = [key for key in matches if self.owns(key, email)]
        if own:
            return own[0]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ValueError(
                "Choose a project ID: "
                + ", ".join(f"{key} ({self.projects[key].owner})" for key in matches)
            )
        raise ValueError("That project is unavailable.")

    def bound(self, team: str, channel: str) -> str | None:
        binding = self.bindings.get(f"{team}:{channel}")
        return binding.project_id if binding else None

    def create(self, email: str, name: str, uri: str) -> str:
        email, uri = normalize(email), data_uri(uri)
        entry = ProjectEntry(name=name, owner=email, data_uri=uri)
        for pid, existing in self.projects.items():
            if existing.owner == email and existing.name == name:
                if existing.data_uri != uri:
                    raise ValueError("That project name already uses another data location.")
                return pid
        pid = "p_" + secrets.token_hex(8)
        while pid in self.projects:
            pid = "p_" + secrets.token_hex(8)
        self.projects[pid] = entry
        return pid

    def bind(
        self,
        team: str,
        channel: str,
        email: str,
        project_id: str,
        *,
        private: bool,
        force: bool = False,
    ) -> None:
        if not re.fullmatch(r"[A-Z0-9]+", team) or not re.fullmatch(r"[A-Z0-9]+", channel):
            raise ValueError("Invalid Slack conversation.")
        if not self.can_use(project_id, email):
            raise ValueError("That project is unavailable.")
        key = f"{team}:{channel}"
        old = self.bound(team, channel)
        if not private:
            if not self.owns(project_id, email):
                raise ValueError("Only the project owner can bind a channel.")
            if key not in self.projects[project_id].published_channels:
                raise ValueError("First authorize this channel's audience with publish in a DM.")
            if old and old in self.projects and not self.owns(old, email):
                raise ValueError("Only the current project's owner can change this binding.")
        if old == project_id:
            return
        if old and not force and not private:
            raise ValueError("This channel is already linked. Use --force to change it.")
        self.bindings[key] = Binding(project_id=project_id, bound_by=email)

    def create_and_bind(
        self,
        team: str,
        channel: str,
        email: str,
        name: str,
        uri: str,
        *,
        private: bool,
        force: bool = False,
    ) -> str:
        if not private:
            raise ValueError("Initialize in a DM, then publish the project to this channel.")
        old = self.bound(team, channel)
        pid = self.create(email, name, uri)
        if old and old != pid and not force:
            raise ValueError("This DM has an active project. Use --force or use <project>.")
        self.bind(team, channel, email, pid, private=private, force=force)
        return pid

    def share(self, project_id: str, owner: str, target: str, *, remove: bool = False) -> str:
        if not self.owns(project_id, owner):
            raise ValueError("Only the project owner can change sharing.")
        if target == owner:
            raise ValueError("The owner always has access.")
        members = self.projects[project_id].members
        if (target in members) == remove:
            self.projects[project_id].access_revision += 1
        if remove:
            members.discard(target)
        else:
            members.add(target)
        return project_id

    def publish(
        self, project_id: str, owner: str, team: str, channel: str, *, remove: bool = False
    ) -> str:
        if not self.owns(project_id, owner):
            raise ValueError("Only the project owner can publish to a channel.")
        if not re.fullmatch(r"[CG][A-Z0-9]+", channel):
            raise ValueError("Choose a Slack channel, not a DM.")
        channels = self.projects[project_id].published_channels
        key = f"{team}:{channel}"
        if (key in channels) == remove:
            self.projects[project_id].access_revision += 1
        if remove:
            channels.discard(key)
        else:
            channels.add(key)
        return project_id

    def access_signature(self, email: str) -> str:
        # A lost source-project grant invalidates private contexts using its memories.
        return hashlib.sha256(
            json.dumps(
                sorted((pid, p.access_revision) for pid, p in self.visible(email).items())
            ).encode()
        ).hexdigest()


class Registry:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def load(self) -> RegistryDoc:
        try:
            return RegistryDoc.model_validate_json(self.storage.get(REGISTRY_KEY)[0])
        except FileNotFoundError:
            return RegistryDoc()

    def update(
        self, event_id: str, email: str, operation: str, change: Callable[[RegistryDoc], str]
    ) -> str:
        fingerprint = hashlib.sha256((email + "\0" + operation).encode()).hexdigest()

        def apply(doc: RegistryDoc) -> None:
            prior = doc.applied_events.get(event_id)
            if prior:
                if prior.fingerprint != fingerprint:
                    raise ValueError("Event identity was reused for a different operation.")
                return
            result = change(doc)
            doc.applied_events[event_id] = Operation(fingerprint=fingerprint, result=result)

        updated = update_model(self.storage, REGISTRY_KEY, RegistryDoc, apply)
        return updated.applied_events[event_id].result


def read_brief(storage: Storage, project_id: str) -> tuple[str, str | None]:
    try:
        content, generation = storage.get(project_key(project_id) + "/PROJECT.md")
        return content.decode("utf-8"), generation
    except FileNotFoundError:
        return NO_BRIEF, None


def write_brief(storage: Storage, project_id: str, data: bytes, generation: str | None) -> str:
    if len(data) >= 100_000:
        raise ValueError("The brief must be under 100 KB.")
    data.decode("utf-8")
    key = project_key(project_id) + "/PROJECT.md"
    try:
        return storage.put_if(key, data, generation)
    except Conflict:
        # Covers a successful write whose response was lost, without overwriting newer content.
        current, current_generation = storage.get(key)
        if current == data:
            return current_generation
        raise Conflict(
            "The saved brief changed in another thread. Start a new thread to review "
            "the latest version before saving."
        ) from None
