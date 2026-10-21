"""Exact command grammar and owner-controlled account operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import observability, surface
from .access import Grant
from .config import Settings
from .data_links import data_uri, init_args, message_text
from .help_text import COMMAND_HELP, PROJECT_HELP, WRITE_DETAILS, WRITE_HELP
from .identity import member_email
from .registry import ProjectEntry, Registry, RegistryDoc


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
        "connect atomx",
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
    if action == "data-root":
        words = message_text(text).split()
        if len(words) != 3:
            raise ValueError("Use data-root <project name or ID> gs://bucket/folder/.")
        return Command("data-root", (words[1], data_uri(words[2])))
    if action == "memory" and len(words) == 2:
        return Command("memory", (words[1],))
    if action == "scripts":
        if len(words) == 1:
            return Command("scripts")
        if len(words) == 2 and words[1].lower() in {"on", "off"}:
            return Command("scripts", (words[1].lower(),))
        if len(words) == 2 and words[1].lower() in {"onn", "of", "ofv", "maybe"}:
            raise ValueError("Use scripts, scripts on or scripts off.")
        return None
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
            return None
        transfer_args = {"sample": words[1]}
        for i in range(2, len(words), 2):
            field = {"as": "as_sample", "from": "export_root"}.get(words[i].lower())
            if not field or field in transfer_args:
                return None
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
    if action == "owners" and len(words) == 2:
        return Command("owners", (words[1],))
    if action in {"share", "unshare", "add-owner", "remove-owner"} and len(words) == 3:
        if re.fullmatch(r"<@[A-Z0-9]+(?:\|[^>]+)?>|<mailto:[^>]+>|[^\s@]+@[^\s@]+", words[2]):
            return Command(action, tuple(words[1:]))
        return None
    if action in {"publish", "unpublish"} and len(words) == 3:
        if re.fullmatch(r"<#([CG][A-Z0-9]+)(?:\|[^>]+)?>|[CG][A-Z0-9]+", words[2]):
            return Command(action, tuple(words[1:]))
        return None
    if action == "rename":
        if len(words) <= 2:
            raise ValueError("In your DM, use rename <name or ID> <new-name>.")
        if len(words) != 3 or words[1].lower() in {"this", "the", "my"}:
            return None
        return Command(action, tuple(words[1:]))
    if action == "remember" and len(words) > 1:
        content = text.split(maxsplit=1)[1]
        return Command(action, (content,))
    if (
        action == "forget"
        and len(words) == 2
        and (words[1].isdigit() or words[1].lower() in {"profile", "preferences", "all"})
    ):
        return Command(action, (words[1].lower(),))
    return None


MEMORY_CHANGED = (
    "Mica now remembers and tidies project memory itself. Use `memory` to see it, "
    "or just tell me what's wrong."
)


def removed_memory(cmd: Command) -> bool:
    return cmd.name in {
        "remember",
        "forget",
        "memory context",
        "forget context",
        "knowledge",
        "knowledge show",
        "knowledge github",
        "forget knowledge",
        "undo knowledge",
        "save brief",
        "clean brief",
    }


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
    if surface.is_email(record) and (
        cmd.name.startswith("write ")
        or cmd.name
        in {
            "help write",
            "share",
            "unshare",
            "add-owner",
            "remove-owner",
            "publish",
            "unpublish",
            "rename",
            "data-root",
        }
    ):
        return surface.EMAIL_READ_ONLY
    if cmd.name in {"help", "help all"}:
        return COMMAND_HELP
    if cmd.name == "help write":
        return WRITE_HELP + "\n\n" + WRITE_DETAILS
    if cmd.name == "scripts":
        from . import user_settings

        return user_settings.command(
            registry.storage, email, cmd.args, settings.return_scripts_default, event_id
        )
    if cmd.name.startswith("write "):
        from .write_access import command

        return command(cmd, grant, registry.storage, record, event_id, settings)
    if cmd.name not in {
        "projects",
        "use",
        "rename",
        "data-root",
        "owners",
        "add-owner",
        "remove-owner",
        "share",
        "unshare",
        "publish",
        "unpublish",
    }:
        return None
    if cmd.name != "use" and not grant.private:
        raise ValueError("Use that command in your DM with Mica.")
    if cmd.name == "projects":
        active = doc.active_for(email, team, channel, private=grant.private)
        rows = []
        query = cmd.args[0].casefold() if cmd.args else ""
        matches = sorted(
            (
                (pid, project)
                for pid, project in doc.visible(email).items()
                if not query or query in project.name.casefold() or query in pid.casefold()
            ),
            key=lambda item: (item[1].name, item[0]),
        )
        for number, (pid, project) in enumerate(matches[:50], 1):
            selector = doc.selector(pid, email)
            label = " (new threads)" if pid == active else ""
            owner = (
                " · co-owner"
                if email in project.co_owners
                else f" · shared by {project.owner}"
                if project.owner != email
                else ""
            )
            rows.append(f"{number}. {project.name}{label}{owner} — `switch {selector}`")
        thread = (
            f"\nThis thread uses {grant.project.name}."
            if grant.allowed and grant.has_context and grant.project_id != active
            else ""
        )
        return (
            "Your projects:\n"
            + (
                "\n".join(rows) or "No matching projects."
                if query
                else "\n".join(rows) or "No projects yet."
            )
            + (
                f"\nShowing 50 of {len(matches)} projects. "
                "Ask me to find projects by name to narrow the list."
                if len(matches) > 50
                else ""
            )
            + thread
            + "\n\n"
            + PROJECT_HELP
        )
    operation = f"{cmd.name}:{cmd.args}:{cmd.force}:{team}:{channel}"

    def checked(current: RegistryDoc) -> RegistryDoc:
        expected = record.get("command_expected_project")
        if expected and current.projects.get(cmd.args[0]) != ProjectEntry.model_validate(expected):
            raise ValueError("Project settings changed. Request a fresh confirmation.")
        return current

    if cmd.name == "data-root":
        pid = registry.update(
            event_id,
            email,
            operation,
            lambda d: checked(d).set_data_uri(d.find(cmd.args[0], email), email, cmd.args[1]),
        )
        observability.capture_project(record, grant, target=pid)
        current = registry.load().projects[pid]
        return (
            f"{current.name}'s default data location is {current.data_uri}. "
            "Future requests, including follow-ups in existing conversations, use this location. "
            "No files were moved and no write permissions were granted."
        )
    if cmd.name == "rename":
        # Resolve inside the atomic update: retries must survive the old name disappearing,
        # and a replay must not rename another project that later reuses that name.
        record["renamed_project"] = registry.update(
            event_id,
            email,
            operation,
            lambda d: checked(d).rename(d.find(cmd.args[0], email), email, cmd.args[1]),
        )
        observability.capture_project(record, grant, target=record["renamed_project"])
        record["failure_project_name"] = cmd.args[1]
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
    observability.capture_project(record, grant, target=pid)
    if cmd.name == "owners":
        entry = doc.projects[pid]
        return f"Owners of {entry.name}:\n" + "\n".join(
            [f"• {entry.owner} (original owner)"]
            + [f"• {target} (co-owner)" for target in sorted(entry.co_owners)]
        )
    if cmd.name in {"add-owner", "remove-owner"}:
        if not record.get("command_expected_project"):
            raise ValueError("Ownership changes require a private Slack confirmation.")
        target = member_email(
            app,
            settings,
            registry.storage,
            team,
            cmd.args[1],
            removing=cmd.name == "remove-owner",
        )
        registry.update(
            event_id,
            email,
            operation,
            lambda d: checked(d).set_co_owner(
                pid, email, target, remove=cmd.name == "remove-owner"
            ),
        )
        return (
            f"Removed {target}'s co-owner role; they remain a collaborator."
            if cmd.name == "remove-owner"
            else f"{target} is now a co-owner of {doc.projects[pid].name}. "
            "Existing owners keep their roles."
        )
    if cmd.name == "use":

        def bind(current: Any) -> str:
            current.bind(team, channel, email, pid, private=grant.private, force=cmd.force)
            return pid

        registry.update(event_id, email, operation, bind)
        return f"New threads will use {doc.projects[pid].name}. " + (
            "Start a new email thread to use it. This email thread keeps its project."
            if surface.is_email(record)
            else "Send a new message outside this thread to start. "
            "Existing threads keep their project."
        )
    if cmd.name in {"share", "unshare"}:
        if not doc.owns(pid, email):
            raise ValueError("Only the project owner can change sharing.")
        target = member_email(
            app, settings, registry.storage, team, cmd.args[1], removing=cmd.name == "unshare"
        )
        registry.update(
            event_id,
            email,
            operation,
            lambda d: checked(d).share(pid, email, target, remove=cmd.name == "unshare"),
        )
        return (
            f"{'Removed access for' if cmd.name == 'unshare' else 'Shared with'} {target}. "
            "Existing Slack channel messages remain visible to their channel audience."
            + (
                " They can email Mica from that address and ask to list or select this project; "
                "Slack membership and channel publication are not required for email access. "
                "Members also see this project's knowledge."
                if cmd.name == "share"
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
        lambda d: checked(d).publish(
            pid, email, team, target_channel, remove=cmd.name == "unpublish"
        ),
    )
    if cmd.name == "unpublish":
        return (
            "Future project replies in that channel are disabled. Existing messages remain visible."
        )
    return (
        "Project publication authorized for everyone who can view that channel, including future "
        "channel members. DM histories and outputs remain private; project memory is shared. "
        f"In the channel, send `switch {pid}` to select the project."
    )
