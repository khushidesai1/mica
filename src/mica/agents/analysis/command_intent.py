"""Validated command proposals and private project-choice context."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import access, commands

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
    selector: str = Field(default="", max_length=200)
    target: str = Field(default="", max_length=320)
    scope: str = Field(default="", max_length=2048)
    duration: str = Field(default="", max_length=40)
    path: str = Field(default="", max_length=200)
    preference: Literal["", "on", "off"] = ""


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


def command(proposal: Proposal, grant: access.Grant, *, source_text: str = "") -> commands.Command:
    op = proposal.operation
    if op in {"share", "unshare"} and (
        proposal.scope.strip().lower() not in {"", "member", "collaborator"}
        or re.search(r"\b(?:co[- ]?owners?|owners?|ownership)\b", source_text, re.I)
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

        supplied = re.findall(r"gs://[^\s<>`\"']+", message_text(source_text))
        exact_uris = {value for token in supplied for value in (token, token.rstrip(".,;)]"))}
        if not proposal.scope.startswith("gs://") or proposal.scope not in exact_uris:
            raise ValueError("Please supply the exact gs:// data location for the new project.")
        parsed = commands.parse(f"init {proposal.selector} {proposal.scope}")
        if parsed is None or parsed.force:
            raise ValueError("Please supply a project name and an explicit gs:// data location.")
        return parsed
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
            source_text,
            re.I,
        ):
            raise ValueError("Please supply the exact azizilab/repository to add or remove.")
        return commands.Command(op, (pid, repo))
    if op == "data-root":
        from .data_links import data_uri, message_text

        supplied = re.findall(r"gs://[^\s<>`\"']+", message_text(source_text))
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
        duration(proposal.duration)
        return commands.Command(op, (pid, uri, proposal.duration))
    target = proposal.target.strip()
    if not target:
        raise ValueError("Please include the exact recipient, channel, or new project name.")
    parsed = commands.parse(f"{op} {pid} {target}")
    if parsed is None or parsed.name != op:
        raise ValueError("Please specify an email address, Slack ID, or valid project name.")
    return parsed
