"""Validated command proposals and private project-choice context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import access, commands, project_memory, reference_repos, surface, write_access
from .registry import Registry
from .storage import Storage

if TYPE_CHECKING:
    from .batch import BatchBridge

Operation = Literal[
    "import session",
    "init",
    "projects",
    "help",
    "use",
    "brief",
    "memory",
    "scripts",
    "cancel",
    "transfers",
    "atomx login status",
    "connect atomx",
    "cancel transfer",
    "forget atomx login",
    "replace atomx login",
    "bind channel",
    "share",
    "unshare",
    "add-owner",
    "remove-owner",
    "owners",
    "publish",
    "unpublish",
    "rename",
    "data-root",
    "repos",
    "repos add",
    "repos remove",
    "write status",
    "write grant",
    "write revoke",
]


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Operation
    selector: str = Field(default="", max_length=200,
                         description="Project name/ID or 'this project'; init uses the new name.")
    target: str = Field(default="", max_length=320,
                       description="Recipient, channel, repository or new name for rename.")
    scope: str = Field(default="", max_length=2048,
                      description="Exact gs:// location for init, data-root or write grant.")
    duration: str = Field(default="", max_length=40,
                          description="Write duration such as 1h, 7d, or explicitly until revoked.")
    path: str = Field(default="", max_length=200)
    preference: Literal["", "on", "off"] = ""
    request: str = Field(default="", max_length=3000,
                        description="For import session, what to do after importing.")
    use_attachment: bool = Field(
        default=False, description="Import the explicitly selected single .jsonl attachment.",
    )


def visibility(grant: access.Grant) -> str:
    """Bind private account replies to the registry view that produced them."""
    rows = [
        (pid, p.name, p.owner, sorted(p.co_owners), p.access_revision)
        for pid, p in sorted(grant.doc.visible(grant.email).items())
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def private_reply(record: dict[str, Any], grant: access.Grant) -> None:
    record.update(
        reply_audience="sender_only",
        reply_scope="account",
        account_visibility=visibility(grant),
    )


async def remember(
    bridge: BatchBridge,
    record: dict[str, Any],
    grant: access.Grant,
    context: dict[str, Any],
) -> None:
    if grant.private:
        await bridge.repository.set_connector_state(
            f"command-context:{record['thread_key']}",
            {
                "principal": grant.email,
                "expires": time.time() + 600,
                "visibility": visibility(grant),
                "context": context,
            },
        )


async def clarification(
    bridge: BatchBridge,
    record: dict[str, Any],
    grant: access.Grant,
    question: str,
    proposal: Proposal | None = None,
) -> str:
    """Store bounded referents; never turn a model follow-up into an implicit mutation."""
    context: dict[str, Any] = {
        "request": record["payload"]["event"].get("text", "")[:2000],
        "question": question,
    }
    if proposal:
        context["proposal"] = proposal.model_dump()
        if proposal.selector and grant.private:
            choices = [
                {"id": pid, "name": p.name}
                for pid, p in sorted(grant.doc.visible(grant.email).items())
                if p.name == proposal.selector
            ][:50]
            if len(choices) > 1:
                context["choices"] = choices
                question = "Which project did you mean?\n" + "\n".join(
                    f"{i}. {choice['name']} ({choice['id']})" for i, choice in enumerate(choices, 1)
                )
    await remember(bridge, record, grant, context)
    return question


async def remember_projects(
    bridge: BatchBridge,
    record: dict[str, Any],
    grant: access.Grant,
    cmd: commands.Command,
) -> None:
    query = cmd.args[0].casefold() if cmd.args else ""
    choices = [
        {"id": pid, "name": p.name}
        for pid, p in sorted(
            grant.doc.visible(grant.email).items(), key=lambda v: (v[1].name, v[0])
        )
        if not query or query in p.name.casefold() or query in pid.casefold()
    ][:50]
    await remember(bridge, record, grant, {"choices": choices, "operation": "use"})


def command(
    proposal: Proposal, grant: access.Grant, *, source_text: str = "", input_text: str = ""
) -> commands.Command:
    op = proposal.operation
    supplied_text = source_text + "\n" + input_text
    if (op in {"brief", "memory"} and (proposal.target or proposal.scope)) or (
        op == "brief" and proposal.path
    ):
        raise ValueError(
            f"{op} only displays saved content; it cannot save those edits. "
            "Use start_worker to update PROJECT.md, preserve its other contents, "
            "and verify the saved change before reporting success."
        )
    if op in {"share", "unshare"} and (
        proposal.scope.strip().lower() not in {"", "member", "collaborator"}
    ):
        raise ValueError(
            "Sharing changes collaborator access only. To change ownership, ask me to "
            "add or remove a co-owner with their email address or Slack mention."
        )
    if op == "init":
        if not proposal.selector or not proposal.scope:
            raise ValueError(
                "What should the project be called, and what gs:// data location should it use?"
            )
        from .data_links import message_text

        supplied = re.findall(r"gs://[^\s<>`\"']+", message_text(supplied_text))
        exact_uris = {value for token in supplied for value in (token, token.rstrip(".,;)]"))}
        if not proposal.scope.startswith("gs://") or proposal.scope not in exact_uris:
            raise ValueError("Please supply the exact gs:// data location for the new project.")
        from .data_links import data_uri

        if not re.fullmatch(r"[a-z0-9-]{3,40}", proposal.selector):
            raise ValueError("Choose a name of 3–40 lowercase letters, numbers or hyphens.")
        return commands.Command(op, (proposal.selector, data_uri(proposal.scope)))
    if op == "import session":
        return commands.Command(op, (proposal.request, "attachment" if proposal.use_attachment
                                     else "form"))
    if op == "cancel transfer":
        from .transfer_config import TransferRequest

        sample = TransferRequest(sample=proposal.target or proposal.selector).sample
        return commands.Command(op, (sample,))
    if op == "projects":
        return commands.Command(op, (proposal.selector,) if proposal.selector else ())
    if op == "memory" and proposal.path:
        from .project_memory import valid_path

        valid_path(proposal.path)
        return commands.Command(op, (proposal.path,))
    if commands.spec(op).arguments == "none":
        return commands.Command(op)
    if op == "scripts":
        return commands.Command(op, (proposal.preference,) if proposal.preference else ())
    selector = proposal.selector.strip()
    if selector.lower() in {"this project", "current project"}:
        selector = grant.project_id if grant.allowed else ""
    if not selector:
        raise ValueError("Which project should I use? You can ask me to list your projects.")
    # Resolve to an ID before any operation or approval; later name reuse cannot retarget it.
    pid = grant.doc.find(selector, grant.email)
    if op == "repos":
        return commands.Command(op, (pid,))
    if op in {"repos add", "repos remove"}:
        from .reference_repos import valid_repo

        repo = valid_repo(proposal.target.strip())
        if not re.search(
            r"(?<![A-Za-z0-9_.-])" + re.escape(repo) + r"(?:\.git)?(?=$|[\s<>`\"'.,;)])",
            supplied_text,
            re.I,
        ):
            raise ValueError("Please supply the exact azizilab/repository to add or remove.")
        return commands.Command(op, (pid, repo))
    if op == "data-root":
        from .data_links import data_uri, message_text

        supplied = re.findall(r"gs://[^\s<>`\"']+", message_text(supplied_text))
        exact_uris = {value for token in supplied for value in (token, token.rstrip(".,;)]"))}
        if not proposal.scope.startswith("gs://") or proposal.scope not in exact_uris:
            raise ValueError("What exact gs:// data location should this project use by default?")
        return commands.Command(op, (pid, data_uri(proposal.scope)))
    if commands.spec(op).arguments == "project":
        return commands.Command(op, (pid,))
    if op == "write grant":
        from .write_access import duration, scope

        if not proposal.scope or not proposal.duration:
            raise ValueError("What exact GCS scope and duration should the write grant cover?")
        uri = scope(proposal.scope)
        if proposal.duration.strip().lower() == "until revoked":
            return commands.Command(op, (pid, uri))
        duration(proposal.duration)
        return commands.Command(op, (pid, uri, proposal.duration))
    target = proposal.target.strip()
    if not target:
        raise ValueError("Please include the exact recipient, channel, or new project name.")
    if commands.spec(op).recipient and not re.fullmatch(
        r"<@[A-Z0-9]+(?:\|[^>]+)?>|<mailto:[^>]+>|[^\s@]+@[^\s@]+", target
    ):
        raise ValueError("Please give the person's email address or Slack mention.")
    if commands.spec(op).channel:
        match = re.fullmatch(r"<#([CG][A-Z0-9]+)(?:\|[^>]+)?>|([CG][A-Z0-9]+)", target)
        if not match:
            raise ValueError("Which Slack channel should I use? Please mention it.")
        target = match[1] or match[2]
    if op == "rename" and not re.fullmatch(r"[a-z0-9-]{3,40}", target):
        raise ValueError("Choose a name of 3–40 lowercase letters, numbers or hyphens.")
    return commands.Command(op, (pid, target))


def terminal(cmd: commands.Command, grant: access.Grant, record: dict[str, Any]) -> bool:
    policy = commands.spec(cmd.name)
    return (policy.approval or policy.exact_approval or policy.handoff
            or (cmd.name == "use" and not record.get("project_reentry")
                and access.unused_context(grant.state, record["event_id"])))


def operation_key(event_id: str, cmd: commands.Command) -> str:
    canonical = json.dumps([cmd.name, cmd.args], separators=(",", ":"), ensure_ascii=False)
    return event_id + ":" + hashlib.sha256(canonical.encode()).hexdigest()


async def execute(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage,
    cmd: commands.Command, grant: access.Grant,
) -> dict[str, Any]:
    """Run a validated in-turn operation, retaining stable receipts across model replays."""
    policy = commands.spec(cmd.name)
    if policy.approval or policy.exact_approval or policy.handoff:
        raise ValueError("This operation needs its confirmation or private handoff.")
    if policy.sender_reply:
        private_reply(record, grant)
    mutation = policy.mutating and (cmd.name != "scripts" or bool(cmd.args))
    key = operation_key(event_id, cmd)
    operations = record.setdefault("operations", {})
    if mutation:
        receipt = operations.get(key)
        if receipt and receipt["status"] == "done":
            return dict(receipt["result"])
        operations[key] = {"operation": cmd.name, "args": list(cmd.args), "status": "started"}
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    result: dict[str, Any]
    if cmd.name in {"brief", "memory"}:
        if not grant.allowed:
            raise ValueError(access.deny_text(grant))
        snapshot = await project_memory.GitHubMemory(bridge.settings).read(
            grant.project_id, event_id=event_id
        )
        if snapshot is None:
            raise ValueError("Project memory is unavailable.")
        paths = ["PROJECT.md"] if cmd.name == "brief" else list(cmd.args) or ["INDEX.md", "open.md"]
        for path in paths:
            project_memory.valid_path(path)
        result = {"project": grant.project_id, "entries": [
            {"path": path, "text": snapshot.files[path].text}
            for path in paths if path in snapshot.files
        ], "paths": sorted(snapshot.files)}
    elif cmd.name == "transfers":
        from .transfers import Transfers

        if not grant.allowed:
            raise ValueError(access.deny_text(grant))
        result = await Transfers(bridge, storage).context(grant.project_id, grant.email)
    elif cmd.name == "atomx login status":
        from .transfers import Transfers

        value = await Transfers(bridge, storage).connections.status(grant.email)
        # Do not pass connection secrets or private management links through the model.
        result = {"status": value.get("status") or "not connected"}
    elif cmd.name == "write status":
        pid = grant.doc.find(cmd.args[0], grant.email)
        active = await asyncio.to_thread(write_access.active, storage, pid, bridge.settings)
        result = {"project": pid, "active": active is not None,
                  "destination": active.uri if active else None,
                  "expires_at": active.expires_at.isoformat()
                  if active and active.expires_at else None}
    else:
        if cmd.name == "repos":
            record["shared_reference_repos"] = sorted(
                await reference_repos.shared_repos(bridge.repository, bridge.settings)
            )
        output = await asyncio.to_thread(
            commands.run, cmd, grant, Registry(storage), bridge.app, record,
            key if mutation else event_id, bridge.settings,
        )
        if not isinstance(output, dict):
            raise ValueError("This operation is not available inside the conversation.")
        result = output
    if cmd.name == "projects":
        await remember_projects(bridge, record, grant, cmd)
    if mutation:
        operations[key].update(status="done", result=result)
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if cmd.name == "use" and grant.private and not surface.is_email(record):
        from . import project_picker

        event = record["payload"]["event"]
        await project_picker.used(bridge, record["payload"]["team_id"], event["user"], cmd.args[0])
        await project_picker.refresh(
            bridge, record["payload"]["team_id"], event["user"], event["channel"]
        )
    return result
