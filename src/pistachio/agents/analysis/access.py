"""Authorize routing metadata before loading project content or restoring a workspace."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import user_memory
from .config import Settings
from .help_text import PROJECT_HELP
from .identity import NOT_ENABLED, is_private, resolve_email, user_key
from .registry import ProjectEntry, Registry, RegistryDoc, read_brief
from .storage import Storage


def checkpoint_key(thread_key: str) -> str:
    return f"projects/_threads/{thread_key}/checkpoint.json"


@dataclass
class Grant:
    email: str
    private: bool
    state: dict[str, Any]
    doc: RegistryDoc
    project_id: str
    allowed: bool
    channel_key: str
    has_context: bool = False
    identity_policy: str = ""

    @property
    def project(self) -> ProjectEntry:
        return self.doc.projects[self.project_id]

    @property
    def signature(self) -> str:
        raw = [
            self.email,
            self.project_id,
            self.channel_key,
            self.private,
            self.doc.access_signature(self.email),
            self.project.data_uri if self.allowed else "",
        ]
        if self.identity_policy:
            raw.append(self.identity_policy)
        return hashlib.sha256(json.dumps(raw).encode()).hexdigest()


def resolve(app: Any, settings: Settings, storage: Storage, record: dict[str, Any]) -> Grant:
    payload = record["payload"]
    event, team = payload["event"], payload.get("team_id", "")
    email = resolve_email(app, settings, storage, team, event.get("user", "")) or ""
    if record.get("intake_principal") and record["intake_principal"] != email:
        email = ""
    private = is_private(event)
    channel_key = f"{team}:{event['channel']}"
    if not email:
        return Grant("", private, {}, RegistryDoc(), "", False, channel_key)
    doc = Registry(storage).load()
    try:
        # Only the trusted resolver sees routing metadata until membership is established.
        state = json.loads(storage.get(checkpoint_key(record["thread_key"]))[0])
    except FileNotFoundError:
        state = {}
    project_id = state.get("context", {}).get("project_slug") or doc.bound(team, event["channel"])
    project_id = str(project_id or "")
    allowed = doc.can_use(project_id, email)
    if allowed and not private:
        allowed = channel_key in doc.projects[project_id].published_channels
    # Do not hand unauthorized checkpoint contents to any caller, command, or model.
    return Grant(
        email,
        private,
        state if allowed else {},
        doc,
        project_id,
        allowed,
        channel_key,
        bool(state.get("context")),
        f"{settings.access_mode}:{team}" if settings.access_mode != "mapped" else "",
    )


def deny_text(grant: Grant) -> str:
    if not grant.email:
        return NOT_ENABLED
    if not grant.project_id:
        return "This chat isn't linked to a project. In your DM with Pistachio:\n" + PROJECT_HELP
    if grant.project_id not in grant.doc.projects:
        return "This thread's project is unavailable."
    return (
        "This conversation uses a private project or an unpublished channel. "
        "Ask its owner for access in a DM."
    )


def read_state(storage: Storage, grant: Grant) -> dict[str, Any]:
    if not grant.allowed:
        raise ValueError(deny_text(grant))
    state = grant.state
    if state.get("context") and not state.get("direct_version"):
        ctx = state["context"]
        for turn in range(int(state.get("turn", 0)), max(0, int(state.get("turn", 0)) - 10), -1):
            try:
                data = storage.get(
                    f"projects/{grant.project_id}/runs/{ctx['thread_ts']}/turn-{turn}/outputs/PROJECT.md"
                )[0]
                if len(data) < 100_000:
                    ctx["project_md"] = data.decode()
                    break
            except FileNotFoundError:
                continue
    return state


def worker_permitted(
    storage: Storage,
    request: dict[str, Any],
    state: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> bool:
    if request.get("protocol") != 2:
        return False
    email, pid = request.get("principal", ""), request.get("project_id", "")
    event = request.get("payload", {}).get("event", {})
    if not email or not pid or state.get("context", {}).get("project_slug") != pid:
        return False
    doc = Registry(storage).load()
    if not doc.can_use(pid, email) or request.get("private") != is_private(event):
        return False
    channel_key = f"{request['payload'].get('team_id', '')}:{event.get('channel', '')}"
    if not is_private(event) and channel_key not in doc.projects[pid].published_channels:
        return False
    policy = ""
    if settings and settings.access_mode != "mapped":
        team = request["payload"].get("team_id", "")
        if resolve_email(settings, settings, storage, team, event.get("user", "")) != email:
            return False
        policy = f"{settings.access_mode}:{team}"
    grant = Grant(
        email, is_private(event), state, doc, pid, True, channel_key, identity_policy=policy
    )
    if settings is not None:
        uri = urlsplit(grant.project.data_uri)
        context = state["context"]
        expected_data = settings.data_mount_root / uri.netloc / uri.path.lstrip("/")
        if str(context.get("data_path")) != str(expected_data):
            return False
        if grant.private and str(context.get("workdir")) != private_workdir(
            settings, grant, request["thread_key"], memory_epoch(storage, grant)
        ):
            return False
    if request.get("access_signature") != grant.signature:
        return False
    return not (grant.private and request.get("memory_epoch", 0) != memory_epoch(storage, grant))


def memory_epoch(storage: Storage, grant: Grant) -> int:
    return user_memory.load(storage, grant.email).epoch if grant.private else 0


def private_workdir(settings: Settings, grant: Grant, thread_key: str, epoch: int) -> str:
    stamp = hashlib.sha256(f"{grant.signature}:{epoch}".encode()).hexdigest()[:24]
    return str(settings.work_root / user_key(grant.email) / grant.project_id / thread_key / stamp)


def fresh_private_state(
    storage: Storage, grant: Grant, settings: Settings, thread_key: str, epoch: int
) -> dict[str, Any]:
    state = read_state(storage, grant)
    if (
        grant.private
        and state.get("context")
        and state["context"]["workdir"] != private_workdir(settings, grant, thread_key, epoch)
    ):
        # A new workspace cannot restore an old SDK session, script, draft, or transcript.
        # Persist the reset before continuing, including when the model call subsequently fails.
        retired = json.dumps(state, sort_keys=True).encode()
        revision = hashlib.sha256(retired).hexdigest()
        storage.put(f"projects/_threads/{thread_key}/retired/{revision}.json", retired)
        brief, generation = read_brief(storage, grant.project_id)
        context = dict(state["context"])
        uri = urlsplit(grant.project.data_uri)
        context.update(
            workdir=private_workdir(settings, grant, thread_key, epoch),
            project_md=brief,
            brief_generation=generation,
            session_id=None,
            data_path=str(settings.data_mount_root / uri.netloc / uri.path.lstrip("/")),
        )
        state = dict(
            context=context, turn=state.get("turn", 0), is_init=False, messages=[], direct_version=1
        )
        storage.put(checkpoint_key(thread_key), json.dumps(state).encode())
    return state
