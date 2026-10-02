"""Exact command grammar and owner-controlled account operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .access import Grant
from .config import Settings
from .data_links import init_args
from .identity import target_email
from .registry import Registry


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()
    force: bool = False


def parse(raw: str) -> Command | None:
    text = re.sub(r"^(?:\s*<@[A-Z0-9]+>\s*)+", "", raw).strip()
    lower = text.lower()
    if lower in {"help", "projects", "brief", "save brief", "cancel", "memory"}:
        return Command(lower)
    if lower.split(maxsplit=1)[:1] == ["init"]:
        args = init_args(text)
        assert args is not None
        return Command("init", (args[0], args[1]), args[2])
    words = text.split()
    if not words:
        return None
    action = words[0].lower()
    if action == "use" and len(words) in {2, 3} and (len(words) == 2 or words[2] == "--force"):
        return Command(action, (words[1],), len(words) == 3)
    if action in {"share", "unshare", "publish", "unpublish"} and len(words) == 3:
        return Command(action, tuple(words[1:]))
    if action == "remember" and len(words) > 1:
        content = text.split(maxsplit=1)[1]
        if len(content) > 600:
            raise ValueError("Remember accepts at most 600 characters.")
        return Command(action, (content,))
    if (
        action == "forget"
        and len(words) == 2
        and (words[1].isdigit() or words[1].lower() in {"profile", "preferences", "all"})
    ):
        return Command(action, (words[1].lower(),))
    return None


def run(
    cmd: Command,
    grant: Grant,
    registry: Registry,
    app: Any,
    record: dict[str, Any],
    event_id: str,
    settings: Settings,
) -> str | None:
    event = record["payload"]["event"]
    team, channel = record["payload"]["team_id"], event["channel"]
    email, doc = grant.email, grant.doc
    if cmd.name == "help":
        detail = ""
        if grant.allowed:
            role = "owner" if doc.owns(grant.project_id, email) else "member"
            detail = f" Project: {grant.project.name}; owner: {grant.project.owner}; role: {role}."
        return (
            "Commands: init <name> gs://bucket/prefix/, use <project>, brief, save brief, cancel. "
            "In DMs: projects, share/unshare <project> @user, "
            "publish/unpublish <project> #channel, "
            "memory, remember <text>, forget <number|profile|preferences|all>." + detail
        )
    if cmd.name not in {"projects", "use", "share", "unshare", "publish", "unpublish"}:
        return None
    if cmd.name != "use" and not grant.private:
        raise ValueError("Use that command in your DM with Pistachio.")
    if cmd.name == "projects":
        rows = [
            f"{'* ' if pid == grant.project_id else ''}{p.name} ({pid}; owner {p.owner})"
            for pid, p in doc.visible(email).items()
        ]
        return "Your projects:\n" + ("\n".join(rows) or "No projects yet. Use init to create one.")
    try:
        pid = doc.find(cmd.args[0], email)
    except ValueError:
        if not grant.private:
            raise ValueError("Choose an authorized project in your DM first.") from None
        raise
    operation = f"{cmd.name}:{cmd.args}:{cmd.force}:{team}:{channel}"
    if cmd.name == "use":

        def bind(current: Any) -> str:
            current.bind(team, channel, email, pid, private=grant.private, force=cmd.force)
            return pid

        registry.update(event_id, email, operation, bind)
        return (
            f"New threads will use {doc.projects[pid].name}. Existing threads keep their project."
        )
    if cmd.name in {"share", "unshare"}:
        if not doc.owns(pid, email):
            raise ValueError("Only the project owner can change sharing.")
        if settings.access_mode == "channel":
            from .channel_access import target_email as channel_target

            target = channel_target(
                app, settings, registry.storage, team, cmd.args[1], removing=cmd.name == "unshare"
            )
        else:
            target = target_email(app, team, cmd.args[1])
        registry.update(
            event_id,
            email,
            operation,
            lambda d: d.share(pid, email, target, remove=cmd.name == "unshare"),
        )
        return (
            f"{'Removed access for' if cmd.name == 'unshare' else 'Shared with'} {target}. "
            "Existing Slack channel messages remain visible to their channel audience."
        )
    match = re.fullmatch(r"<#([CG][A-Z0-9]+)(?:\|[^>]+)?>|([CG][A-Z0-9]+)", cmd.args[1])
    if not match:
        raise ValueError("Mention a Slack channel, for example publish liver #research.")
    target_channel = match[1] or match[2]
    registry.update(
        event_id,
        email,
        operation,
        lambda d: d.publish(pid, email, team, target_channel, remove=cmd.name == "unpublish"),
    )
    if cmd.name == "unpublish":
        return (
            "Future project replies in that channel are disabled. Existing messages remain visible."
        )
    return (
        "Project publication authorized for everyone who can view that channel, including future "
        "channel members. Private DM histories, outputs, and user memory remain private. "
        f"In the channel, use {pid} to select the project."
    )
