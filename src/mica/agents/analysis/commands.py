"""Trusted account operations and their execution/approval policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import observability, surface
from .access import Grant
from .config import Settings
from .help_text import CAPABILITIES
from .identity import member_email
from .registry import ProjectEntry, Registry, RegistryDoc


@dataclass(frozen=True)
class Spec:
    """Policy shared by structured proposals, execution and confirmation cards."""

    approval: bool = False
    dm_only: bool = False
    exact_dm_only: bool = False
    email_read_only: bool = False
    sender_reply: bool = False
    recipient: bool = False
    channel: bool = False
    natural: bool = False
    registry: bool = False
    connection: bool = False
    transfer: bool = False
    exact_approval: bool = False
    arguments: str = "custom"
    handoff: bool = False
    mutating: bool = False


COMMANDS: dict[str, Spec] = {
    "bind channel": Spec(channel=True, approval=True, dm_only=True, exact_dm_only=True,
        email_read_only=True, natural=True, registry=True),
    "add-owner": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        recipient=True,
        natural=True,
        registry=True,
        exact_approval=True,
        dm_only=True,
    ),
    "atomx login status": Spec(
        sender_reply=True,
        natural=True,
        connection=True,
        transfer=True,
        arguments="none",
        dm_only=True,
    ),
    "brief": Spec(natural=True, arguments="none"),
    "cancel": Spec(natural=True, arguments="none", handoff=True),
    "cancel transfer": Spec(transfer=True, natural=True, handoff=True),
    "connect atomx": Spec(
        sender_reply=True, natural=True, connection=True, arguments="none", dm_only=True,
        handoff=True
    ),
    "data-root": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        natural=True,
        registry=True,
        exact_approval=True,
        dm_only=True,
    ),
    "forget atomx login": Spec(sender_reply=True, connection=True, transfer=True, natural=True,
        dm_only=True, handoff=True, arguments="none"),
    "help": Spec(sender_reply=True, natural=True, arguments="none"),
    "init": Spec(natural=True, dm_only=True, handoff=True),
    "import session": Spec(natural=True, dm_only=True, handoff=True, arguments="none"),
    "memory": Spec(natural=True, arguments="none"),
    "owners": Spec(
        exact_dm_only=True,
        sender_reply=True,
        natural=True,
        registry=True,
        arguments="project",
        dm_only=True,
    ),
    "projects": Spec(
        exact_dm_only=True,
        sender_reply=True,
        natural=True,
        registry=True,
        arguments="none",
        dm_only=True,
    ),
    "publish": Spec(
        channel=True,
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        natural=True,
        registry=True,
        dm_only=True,
    ),
    "remove-owner": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        recipient=True,
        natural=True,
        registry=True,
        exact_approval=True,
        dm_only=True,
    ),
    "rename": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        natural=True,
        registry=True,
        dm_only=True,
    ),
    "replace atomx login": Spec(sender_reply=True, connection=True, transfer=True, natural=True,
        dm_only=True, handoff=True, arguments="none"),
    "repos": Spec(exact_dm_only=True, natural=True, registry=True),
    "repos add": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        natural=True,
        registry=True,
        exact_approval=True,
        dm_only=True,
    ),
    "repos remove": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        natural=True,
        registry=True,
        exact_approval=True,
        dm_only=True,
    ),
    "scripts": Spec(sender_reply=True, natural=True, mutating=True),
    "share": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        recipient=True,
        natural=True,
        registry=True,
        dm_only=True,
    ),
    "transfer": Spec(transfer=True),
    "transfers": Spec(natural=True, transfer=True, arguments="none"),
    "unpublish": Spec(
        channel=True,
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        natural=True,
        registry=True,
        dm_only=True,
    ),
    "unshare": Spec(
        approval=True,
        exact_dm_only=True,
        email_read_only=True,
        recipient=True,
        natural=True,
        registry=True,
        dm_only=True,
    ),
    "use": Spec(sender_reply=True, natural=True, registry=True, arguments="project",
        dm_only=True, mutating=True),
    "write grant": Spec(approval=True, email_read_only=True, natural=True, dm_only=True),
    "write revoke": Spec(
        approval=True, email_read_only=True, natural=True, arguments="project", dm_only=True
    ),
    "write status": Spec(email_read_only=True, natural=True, arguments="project", dm_only=True),
}


def spec(name: str) -> Spec:
    return COMMANDS.get(name, Spec())


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple[str, ...] = ()
    force: bool = False


def cancel_message(raw: str) -> bool:
    """The sole text shortcut, also used by Slack's native Stop control."""
    text = re.sub(r"^(?:\s*<@[A-Z0-9]+>\s*)+", "", raw).strip()
    return re.fullmatch(r"[^\w]*(?:cancel|stop)[^\w]*", text, re.I) is not None


def registry_operation(cmd: Command, record: dict[str, Any]) -> str:
    event = record["payload"]["event"]
    return f"{cmd.name}:{cmd.args}:{cmd.force}:{record['payload']['team_id']}:{event['channel']}"


def legacy_reply(result: dict[str, Any] | str) -> str:
    """Previously persisted terminal commands still finish without another model turn."""
    if isinstance(result, str):
        return result
    if "message" in result:
        return str(result["message"])
    if "guide" in result:
        return str(result["guide"])
    if "projects" in result:
        rows = result["projects"]
        text = "Your projects:\n" + ("\n".join(
            row["name"] + (" (new threads)" if row["default"] else "")
            + " (" + row["id"] + ") · shared by " + row["owner"] for row in rows
        ) or "No matching projects.")
        if result["this_thread"] and result["this_thread"] != result["new_threads"]:
            thread = next((row["name"] for row in rows if row["id"] == result["this_thread"]), "")
            if thread:
                text += f"\nThis thread uses {thread}."
        return text
    if "new_threads" in result:
        return (f"New threads will use {result['new_threads']['name']}. "
                "Send a new message outside this thread to work there; "
                "existing threads keep their project.")
    if "return_scripts" in result:
        return ("I'll attach one ZIP of scripts changed during each request."
                if result["return_scripts"] else "I'll stop attaching scripts.")
    if "shared_references" in result:
        selected = ", ".join(result["references"]) or "none selected"
        shared = ", ".join(result["shared_references"]) or "none"
        return (f"Reference repositories for {result['project']['name']}: {selected}. "
                f"Repositories available across projects: {shared}.")
    if "co_owners" in result:
        co_owners = ", ".join(result["co_owners"]) or "none"
        return f"Project owner: {result['owner']}. Co-owners: {co_owners}."
    import json

    return json.dumps(result, ensure_ascii=False)


def run(
    cmd: Command,
    grant: Grant,
    registry: Registry,
    app: Any,
    record: dict[str, Any],
    event_id: str,
    settings: Settings,
) -> dict[str, Any] | str | None:
    event = record["payload"]["event"]
    team, channel = record["payload"]["team_id"], event["channel"]
    email, doc = grant.email, grant.doc
    if surface.is_email(record) and (spec(cmd.name).email_read_only):
        return surface.EMAIL_READ_ONLY
    if cmd.name == "help":
        return {"guide": CAPABILITIES}
    if cmd.name == "scripts":
        from . import user_settings

        return user_settings.command(
            registry.storage, email, cmd.args, settings.return_scripts_default, event_id
        )
    if cmd.name.startswith("write "):
        from .write_access import command

        return command(cmd, grant, registry.storage, record, event_id, settings)
    if not spec(cmd.name).registry:
        return None
    if spec(cmd.name).exact_dm_only and not grant.private:
        raise ValueError("Please ask me in your private conversation.")
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
        for pid, project in matches[:50]:
            rows.append({"id": pid, "name": project.name, "owner": project.owner,
                         "co_owners": sorted(project.co_owners), "default": pid == active})
        return {"projects": rows, "total": len(matches), "new_threads": active,
                "this_thread": grant.project_id if grant.allowed and grant.has_context else None}
    operation = registry_operation(cmd, record)

    def checked(current: RegistryDoc) -> RegistryDoc:
        expected = record.get("command_expected_project")
        if expected and current.projects.get(cmd.args[0]) != ProjectEntry.model_validate(expected):
            raise ValueError("Project settings changed. Request a fresh confirmation.")
        return current

    if cmd.name.startswith("repos"):
        selector = cmd.args[0] if cmd.args and cmd.args[0] else grant.project_id
        pid = doc.find(selector, email)
        observability.capture_project(record, grant, target=pid)
        if cmd.name == "repos":
            names = sorted(doc.projects[pid].reference_repos)
            shared = record.get("shared_reference_repos", [])
            return {"project": {"id": pid, "name": doc.projects[pid].name},
                    "references": names, "shared_references": shared}
        if not record.get("command_expected_project"):
            raise ValueError("Reference repository changes require a private Slack confirmation.")
        registry.update(
            event_id,
            email,
            operation,
            lambda d: checked(d).set_reference_repo(
                pid,
                email,
                cmd.args[1],
                remove=cmd.name == "repos remove",
                org=settings.reference_github_org,
            ),
        )
        return (
            f"{'Removed' if cmd.name == 'repos remove' else 'Added'} {cmd.args[1]} "
            f"{'from' if cmd.name == 'repos remove' else 'to'} {doc.projects[pid].name}'s "
            "reference repositories. Future compute requests use the updated selection."
        )
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
        return {"project": {"id": pid, "name": entry.name}, "owner": entry.owner,
                "co_owners": sorted(entry.co_owners)}
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
        return {"new_threads": {"id": pid, "name": doc.projects[pid].name},
                "this_thread": ({"id": grant.project_id, "name": grant.project.name}
                                if grant.allowed and grant.has_context else None),
                "instruction": "Existing threads keep their project. Start a new message "
                               "outside this thread to work in the selected project."}
    if cmd.name == "bind channel":
        target_channel = cmd.args[1]

        def rebind(current: RegistryDoc) -> str:
            checked(current)
            if "command_expected_binding" not in record:
                raise ValueError("Changing a channel's project needs confirmation first.")
            binding = current.bindings.get(f"{team}:{target_channel}")
            value = binding.model_dump(mode="json") if binding else None
            if value != record["command_expected_binding"]:
                raise ValueError(
                    "The channel’s project changed. Please request confirmation again."
                )
            current.bind(team, target_channel, email, pid, private=False, force=True)
            return pid

        registry.update(event_id, email, operation, rebind)
        return f"New threads in <#{target_channel}> will use {doc.projects[pid].name}."
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
        raise ValueError("Which Slack channel should I use? Please mention it.")
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
        "Ask me here to make it the channel’s default project."
    )
