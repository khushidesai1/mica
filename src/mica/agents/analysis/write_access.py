"""Human-issued, project-scoped grants for the trusted GCS output publisher."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, Field

from .config import Settings
from .data_links import data_uri, message_text
from .registry import Registry, RegistryDoc, project_key
from .storage import Conflict, Storage, update_model

if TYPE_CHECKING:
    from .access import Grant
    from .commands import Command

USAGE = (
    "In your DM: write grant <project> gs://bucket/folder/ [duration], "
    "write status <project>, or write revoke <project>. "
    "Use 30m, 1h, or 7d; omit duration for no expiry (until revoked)."
)


def now() -> datetime:
    return datetime.now(UTC)


def is_command(text: str) -> bool:
    return message_text(text).lower().split()[:2] in [
        ["write", "grant"],
        ["write", "status"],
        ["write", "revoke"],
    ]


class WriteGrant(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    uri: str
    actor: str
    admin_id: str
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None


class WriteState(BaseModel):
    grant: WriteGrant | None = None
    last_timestamp: str = "0"
    # Permanent receipts prevent replaying a grant after a revoke, or vice versa.
    events: dict[str, dict[str, Any]] = Field(default_factory=dict)


def key(project_id: str) -> str:
    project_key(project_id)
    return f"write-access/projects/{project_id}.json"


def load(storage: Storage, project_id: str) -> WriteState:
    try:
        return WriteState.model_validate_json(storage.get(key(project_id))[0])
    except FileNotFoundError:
        return WriteState()


def scope(raw: str) -> str:
    uri = data_uri(raw)
    if any(ord(c) < 32 or ord(c) == 127 or c in "*?[]<>|`" for c in uri):
        raise ValueError("Use a literal folder path without wildcards or control characters.")
    if "//" in urlsplit(uri).path:
        raise ValueError("Use a folder path without empty segments.")
    return uri.rstrip("/") + "/"


def duration(raw: str) -> timedelta:
    match = re.fullmatch(r"([1-9][0-9]{0,5})([mhd])", raw.lower())
    if not match:
        raise ValueError(USAGE)
    seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > 365 * 86400:
        raise ValueError("Use a duration up to 365d, or omit it for access until revoked.")
    return timedelta(seconds=seconds)


def validate_scope(uri: str, settings: Settings) -> None:
    if not settings.write_service_account or not settings.write_allowed_prefixes:
        raise ValueError("GCS write access is not configured. Ask an administrator to enable it.")
    if not any(uri.startswith(scope(root)) for root in settings.csv("write_allowed_prefixes")):
        raise ValueError("That location is outside the administrator-approved write folders.")
    # Never let an output upload manufacture runtime metadata or authorization records.
    parts = urlsplit(uri)
    if parts.netloc == settings.bucket and not parts.path.startswith("/exports/"):
        raise ValueError("In Mica's artifact bucket, write grants must be under exports/.")


def validate_project(uri: str, doc: RegistryDoc, project_id: str, actor: str) -> None:
    if not doc.can_use(project_id, actor):
        raise ValueError("That project is unavailable.")
    if urlsplit(uri).netloc != urlsplit(doc.projects[project_id].data_uri).netloc:
        raise ValueError("Choose a destination in this project's linked data bucket.")


def active(storage: Storage, project_id: str, settings: Settings) -> WriteGrant | None:
    grant = load(storage, project_id).grant
    if grant is None or (grant.expires_at is not None and now() >= grant.expires_at):
        return None
    if grant.admin_id not in settings.csv("write_admin_ids"):
        return None
    try:
        if scope(grant.uri) != grant.uri:
            return None
        validate_scope(grant.uri, settings)
        validate_project(grant.uri, Registry(storage).load(), project_id, grant.actor)
    except ValueError:
        return None
    return grant


def require_active(
    storage: Storage, project_id: str, grant_id: str, settings: Settings
) -> WriteGrant:
    grant = active(storage, project_id, settings)
    if grant is None or grant.id != grant_id:
        raise PermissionError("Write access expired, was revoked, or changed. Request a new run.")
    return grant


def description(grant: WriteGrant) -> str:
    expiry = (
        f"expires {grant.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        if grant.expires_at
        else "no expiry — until revoked"
    )
    return f"Write files in `{escape(grant.uri, quote=False)}`; {expiry}."


def status(storage: Storage, project_id: str, settings: Settings) -> str:
    grant = active(storage, project_id, settings)
    if grant:
        return (
            description(grant)
            + " Existing files can be overwritten after confirmation for each run."
        )
    previous = load(storage, project_id).grant
    if previous and previous.expires_at and now() >= previous.expires_at:
        return "Write access expired. No new GCS writes are allowed."
    return "No active write access."


def command(
    cmd: Command,
    access: Grant,
    storage: Storage,
    record: dict[str, Any],
    event_id: str,
    settings: Settings,
) -> str:
    if not access.private:
        raise ValueError("Manage write access in your DM with Mica.")
    doc = Registry(storage).load()
    if cmd.name == "write status":
        return status(storage, doc.find(cmd.args[0], access.email), settings)
    event = record["payload"]["event"]
    admin = f"{record['payload']['team_id']}:{event['user']}"
    if admin not in settings.csv("write_admin_ids"):
        raise ValueError("Only designated write-access administrators can grant or revoke writes.")
    timestamp = event["ts"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+", timestamp):
        raise ValueError("Invalid command timestamp.")
    uri = scope(cmd.args[1]) if cmd.name == "write grant" else ""
    if uri:
        validate_scope(uri, settings)
    # Anchor expiry to Slack's signed event time: retries and delayed delivery cannot extend it.
    created = datetime.fromtimestamp(float(timestamp), UTC)
    expires = created + duration(cmd.args[2]) if len(cmd.args) == 3 else None
    fingerprint = hashlib.sha256(
        json.dumps([cmd.name, cmd.args, access.email, admin, timestamp]).encode()
    ).hexdigest()
    # Pin the selector before the mutation. A retry after rename/name reuse must not
    # apply this old command to a different project with the same human-readable name.
    receipt_key = f"write-access/commands/{hashlib.sha256(event_id.encode()).hexdigest()}.json"
    try:
        receipt = json.loads(storage.get(receipt_key)[0])
    except FileNotFoundError:
        receipt = {"project_id": doc.find(cmd.args[0], access.email), "fingerprint": fingerprint}
        try:
            storage.put_if(receipt_key, json.dumps(receipt).encode(), None)
        except Conflict:
            receipt = json.loads(storage.get(receipt_key)[0])
    if receipt["fingerprint"] != fingerprint:
        raise ValueError("This write-access command ID was already used.")
    pid = receipt["project_id"]
    if not doc.can_use(pid, access.email):
        raise ValueError("That project is unavailable.")
    if uri:
        validate_project(uri, doc, pid, access.email)

    def change(state: WriteState) -> None:
        if event_id in state.events:
            if state.events[event_id]["fingerprint"] != fingerprint:
                raise ValueError("This write-access command ID was already used.")
            return
        if Decimal(timestamp) <= Decimal(state.last_timestamp):
            raise ValueError("A newer write-access command already applied. Send a new command.")
        state.grant = (
            WriteGrant(
                id=hashlib.sha256(event_id.encode()).hexdigest()[:32],
                uri=uri,
                actor=access.email,
                admin_id=admin,
                created_at=created,
                expires_at=expires,
            )
            if uri
            else None
        )
        state.last_timestamp = timestamp
        state.events[event_id] = {
            "fingerprint": fingerprint,
            "action": cmd.name,
            "actor": access.email,
            "admin_id": admin,
            "at": now().isoformat(),
            "uri": uri,
            "expires_at": expires.isoformat() if expires else None,
        }

    update_model(storage, key(pid), WriteState, change)
    current = status(storage, pid, settings)
    return current + (
        " Revocation blocks new uploads; an upload already completing may finish."
        if cmd.name == "write revoke" and load(storage, pid).grant is None
        else " Applies to future runs; this command does not upload any files."
    )


def instructions(grant: WriteGrant | None, overwrite_paths: list[str] | None = None) -> str:
    if grant is None:
        return "No GCS write grant for this run. Do not stage files in outputs/gcs/."
    return (
        description(grant)
        + " Only when the current request asks to save files to GCS, write or copy the complete "
        "result folder to outputs/gcs/<path relative to grant>/, preserving points/, shapes/, "
        "and tables/*.zarr layouts. Close writers and check the whole store, then call "
        "publish_gcs with that outputs/gcs/ folder immediately, before starting later steps. "
        "Unpublished new/changed files are also published after a successful turn. "
        "Report GCS saves as a short summary (folders and what each holds). Don't list "
        "individual files; the application appends a receipt. Full publication manifests "
        "are in logs/gcs-publish-*.json when you need to check specific files. "
        "Existing objects may be replaced only in this run's application-confirmed targets. "
        "Other paths are create-only; a collision needs a new request naming its destination. "
        "The publisher rejects versions changed after confirmation and never deletes extra files. "
        "For a result store, verify the complete key layout; use a new folder if old keys would "
        "be left behind. Once publishing starts, staged paths are immutable, even on retry. "
        "Do not copy unchanged inputs such as large image stores unless asked. "
        "Report only URIs returned by the publisher; do not invent or predict successful uploads. "
        "Do not infer write access from the read-only mount or earlier turns. "
        "Do not use cloud credentials, gsutil, or modify the data mount. "
        "A changed/revoked/expired grant blocks publishing, even in a resumed session. "
        + overwrite_instructions(overwrite_paths)
    )


def overwrite_instructions(paths: list[str] | None) -> str:
    return (
        "The requester confirmed these overwrite targets with the Slack approval button "
        "before this run started: " + ", ".join(paths) + ". Do not ask again."
        if paths
        else "No existing output versions were approved for replacement in this run."
    )


def turn_instructions(grant: WriteGrant | None, overwrite_paths: list[str] | None = None) -> str:
    return (
        "GCS write access for this turn (application-set, supersedes anything said earlier "
        "in this thread): "
        + (f"write files under {grant.uri}" if grant else "none")
        + ". The publisher rechecks this grant before uploading. "
        + overwrite_instructions(overwrite_paths)
    )


def log_turn(event_id: str, grant: WriteGrant | None) -> None:
    logging.info(
        "write_access event=%s grant=%s uri=%s",
        event_id,
        grant.id[:8] if grant else "none",
        grant.uri if grant else "none",
    )
