"""Owner-scoped projects, explicit channel publication, and atomic registry operations."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .data_links import data_uri
from .identity import normalize
from .storage import Storage, update_model

REGISTRY_KEY = "projects/_registry/registry.json"


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
    name_revision: int = 0
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
    defaults: dict[str, str] = Field(default_factory=dict)
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

    def active_for(self, email: str, team: str, channel: str, *, private: bool) -> str | None:
        if not private:
            return self.bound(team, channel)
        email = normalize(email)
        if email in self.defaults:
            pid = self.defaults[email]
            return pid if self.can_use(pid, email) else None
        legacy = {
            binding.project_id
            for key, binding in self.bindings.items()
            if key.split(":", 1)[-1].startswith("D")
            and normalize(binding.bound_by) == email
            and self.can_use(binding.project_id, email)
        }
        local = self.bindings.get(f"{team}:{channel}")
        if channel.startswith("D") and local and normalize(local.bound_by) == email:
            return local.project_id if self.can_use(local.project_id, email) else None
        return next(iter(legacy)) if len(legacy) == 1 else None

    def selector(self, project_id: str, email: str) -> str:
        """Prefer a readable name only when it resolves to this exact project."""
        name = self.projects[project_id].name
        try:
            return name if self.find(name, email) == project_id else project_id
        except ValueError:
            return project_id

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

    def rename(self, project_id: str, owner: str, name: str) -> str:
        if not self.owns(project_id, owner):
            raise ValueError("Only the project owner can rename it.")
        if not re.fullmatch(r"[a-z0-9-]{3,40}", name):
            raise ValueError("Use 3–40 lowercase letters, numbers, or hyphens for the new name.")
        if any(
            pid != project_id and entry.owner == owner and entry.name == name
            for pid, entry in self.projects.items()
        ):
            raise ValueError("You already have a project with that name. Choose another name.")
        if self.projects[project_id].name != name:
            self.projects[project_id].name = name
            self.projects[project_id].name_revision += 1
        return project_id

    def set_data_uri(self, project_id: str, owner: str, uri: str) -> str:
        if not self.owns(project_id, owner):
            raise ValueError("Only the project owner can change its data location.")
        self.projects[project_id].data_uri = data_uri(uri)
        return project_id

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
        if private:
            self.defaults[normalize(email)] = project_id
            # Keep existing DM bindings readable across rolling deployments.
            if team == "EMAIL":
                return
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
        pid = self.create(email, name, uri)
        self.bind(team, channel, email, pid, private=private, force=force)
        return pid

    def share(self, project_id: str, owner: str, target: str, *, remove: bool = False) -> str:
        target = normalize(target)
        if not self.owns(project_id, owner):
            raise ValueError("Only the project owner can change sharing.")
        if target == owner:
            raise ValueError("The owner always has access.")
        members = self.projects[project_id].members
        new_member = target not in members
        if (target in members) == remove:
            self.projects[project_id].access_revision += 1
        if remove:
            members.discard(target)
        else:
            members.add(target)
            if new_member:
                self.initialize_default(project_id, target)
        return project_id

    def initialize_default(self, project_id: str, email: str) -> bool:
        """Select an initial private project without replacing any prior choice."""
        email = normalize(email)
        if not self.can_use(project_id, email):
            raise ValueError("That project is unavailable.")
        if email in self.defaults or any(
            key.split(":", 1)[-1].startswith("D") and normalize(binding.bound_by) == email
            for key, binding in self.bindings.items()
        ):
            # Preserve revoked and ambiguous legacy choices as well: an invitation
            # must never silently redirect an established user's private messages.
            return False
        self.defaults[email] = project_id
        return True

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
