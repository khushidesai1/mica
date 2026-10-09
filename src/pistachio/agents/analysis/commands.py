"""Exact command grammar and owner-controlled account operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .access import Grant
from .config import Settings
from .data_links import init_args, message_text
from .help_text import COMMAND_HELP, PROJECT_HELP, WRITE_DETAILS, WRITE_HELP
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
    if lower in {
        "help",
        "help all",
        "help write",
        "projects",
        "brief",
        "save brief",
        "cancel",
        "memory",
        "knowledge",
        "undo knowledge",
        "memory context",
        "forget context",
        "clean brief",
        "transfers",
        "atomx login status",
        "replace atomx login",
        "forget atomx login",
    }:
        return Command(lower)
    if lower == "switch":
        return Command("projects")
    if lower.split(maxsplit=1)[:1] == ["init"]:
        args = init_args(text)
        assert args is not None
        return Command("init", (args[0], args[1]), args[2])
    words = text.split()
    if not words:
        return None
    action = words[0].lower()
    if action == "knowledge" and len(words) == 3 and words[1].lower() == "show":
        return Command("knowledge show", (words[2],))
    if action == "knowledge" and len(words) == 3 and words[1].lower() == "github":
        if words[2].lower() not in {"on", "off"}:
            raise ValueError("Use knowledge github on or knowledge github off.")
        return Command("knowledge github", (words[2].lower(),))
    if action == "forget" and len(words) == 3 and words[1].lower() == "knowledge":
        return Command("forget knowledge", (words[2],))
    if action == "cancel" and len(words) == 3 and words[1].lower() == "transfer":
        return Command("cancel transfer", (words[2],))
    if action == "transfer":
        import shlex

        from .transfer_config import TransferRequest

        words = shlex.split(message_text(text))
        if len(words) not in {2, 4, 6}:
            raise ValueError("Use transfer <sample> [as <name>] [from <export_root>].")
        transfer_args = {"sample": words[1]}
        for i in range(2, len(words), 2):
            field = {"as": "as_sample", "from": "export_root"}.get(words[i].lower())
            if not field or field in transfer_args:
                raise ValueError("Use transfer <sample> [as <name>] [from <export_root>].")
            transfer_args[field] = words[i + 1]
        request = TransferRequest(**transfer_args)
        return Command("transfer", (request.sample, request.export_root, request.as_sample))
    if action == "write":
        from .write_access import USAGE, duration, scope

        words = message_text(text).split()
        subcommand = words[1].lower() if len(words) > 1 else ""
        if subcommand == "help" and len(words) == 2:
            return Command("help write")
        if subcommand and subcommand not in {"grant", "status", "revoke", "help"}:
            return None  # Preserve ordinary requests such as "write a script".
        if subcommand in {"status", "revoke"} and len(words) == 3:
            return Command(f"write {subcommand}", (words[2],))
        if subcommand == "grant" and len(words) in {4, 5}:
            uri = scope(words[3])
            if len(words) == 5:
                duration(words[4])  # Invalid durations never become indefinite grants.
            return Command("write grant", (words[2], uri, *words[4:]))
        raise ValueError(USAGE)
    if (
        action in {"use", "switch"}
        and len(words) in {2, 3}
        and (len(words) == 2 or words[2] == "--force")
    ):
        return Command("use", (words[1],), len(words) == 3)
    if action in {"share", "unshare", "publish", "unpublish"} and len(words) == 3:
        return Command(action, tuple(words[1:]))
    if action == "rename":
        if len(words) != 3:
            raise ValueError("In your DM, use rename <name or ID> <new-name>.")
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
    if cmd.name in {"help", "help all"}:
        return COMMAND_HELP
    if cmd.name == "help write":
        return WRITE_HELP + "\n\n" + WRITE_DETAILS
    if cmd.name.startswith("write "):
        from .write_access import command

        return command(cmd, grant, registry.storage, record, event_id, settings)
    if cmd.name not in {"projects", "use", "rename", "share", "unshare", "publish", "unpublish"}:
        return None
    if cmd.name != "use" and not grant.private:
        raise ValueError("Use that command in your DM with Pistachio.")
    if cmd.name == "projects":
        active = doc.bound(team, channel)
        rows = []
        for pid, project in sorted(doc.visible(email).items(), key=lambda item: item[1].name):
            selector = doc.selector(pid, email)
            label = " (new threads)" if pid == active else ""
            owner = f" · shared by {project.owner}" if project.owner != email else ""
            rows.append(f"• {project.name}{label}{owner} — `switch {selector}`")
        thread = (
            f"\nThis thread uses {grant.project.name}."
            if grant.allowed and grant.has_context and grant.project_id != active
            else ""
        )
        return (
            "Your projects:\n"
            + ("\n".join(rows) or "No projects yet.")
            + thread
            + "\n\n"
            + PROJECT_HELP
        )
    operation = f"{cmd.name}:{cmd.args}:{cmd.force}:{team}:{channel}"
    if cmd.name == "rename":
        # Resolve inside the atomic update: retries must survive the old name disappearing,
        # and a replay must not rename another project that later reuses that name.
        registry.update(
            event_id,
            email,
            operation,
            lambda d: d.rename(d.find(cmd.args[0], email), email, cmd.args[1]),
        )
        return (
            f"Project renamed to {cmd.args[1]}. "
            "Its data, sharing, channel links, and existing threads are unchanged."
        )
    try:
        pid = doc.find(cmd.args[0], email)
    except ValueError:
        if not grant.private:
            raise ValueError("Choose an authorized project in your DM first.") from None
        raise
    if cmd.name == "use":

        def bind(current: Any) -> str:
            current.bind(team, channel, email, pid, private=grant.private, force=cmd.force)
            return pid

        registry.update(event_id, email, operation, bind)
        return (
            f"New threads will use {doc.projects[pid].name}. "
            "Send a new message outside this thread to start. Existing threads keep their project."
        )
    if cmd.name in {"share", "unshare"}:
        if not doc.owns(pid, email):
            raise ValueError("Only the project owner can change sharing.")
        if settings.access_mode in {"channel", "workspace"}:
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
            + (
                " Members also see this project's knowledge."
                if cmd.name == "share" and settings.knowledge_enabled
                else ""
            )
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
        f"In the channel, send `switch {pid}` to select the project."
    )
