"""Conversation and bounded read-only information on Cloud Run; code runs in workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from mica.tool_catalog import spatial_analysis_capability

from . import (
    access,
    attachments,
    commands,
    observability,
    project_memory,
    reference_repos,
    surface,
    thread_history,
    thread_memory,
    tiers,
    write_access,
)
from .cloud_access_config import CloudAccessProposal
from .data_links import message_text
from .help_text import PROJECT_HELP
from .identity import post
from .registry import Registry
from .slack_format import chunks, to_mrkdwn
from .storage import Storage
from .survey import inventory
from .transfer_config import TransferRequest

if TYPE_CHECKING:
    from .batch import BatchBridge


class DecisionFields(BaseModel):
    cloud_access_request: CloudAccessProposal | None = None
    transfer_request: TransferRequest | None = None
    view_ids: list[str] = Field(default_factory=list, max_length=4)
    reference_repos: list[Annotated[str, Field(max_length=150)]] = Field(
        default_factory=list,
        max_length=5,
        description="Available full repo names needed for read-only inspection or computation.",
    )
    reference_paths: dict[
        str, Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=12)]
    ] = Field(
        default_factory=dict,
        max_length=5,
        description="Optional exact paths per selected repo; empty reads its source snapshot.",
    )
    read_file_ids: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=4
    )
    memory_paths: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=8
    )
    history_query: str | None = Field(default=None, max_length=500)
    history_event_id: str = Field(default="", max_length=128)
    history_offset: int = Field(default=0, ge=0, le=1_000_000)
    reply: str = Field(min_length=1, max_length=16000)
    guide: str | None = Field(
        default=None,
        max_length=99_999,
        description=(
            "Default null. Complete initial project guide only for init. "
            "Exclude observations, hypotheses, results, and TODOs. "
            "Leave null for routine messages, results, and unchanged content."
        ),
    )
    inventory_paths: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "All folders needed in one listing step, relative to data_uri. Resolve names "
            "from project_md, project_memory or conversation; use an empty string for root "
            "only when context cannot identify a needed folder."
        ),
    )
    write_paths: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list,
        max_length=5,
        description="Requested GCS output file/folder URIs, for pre-run overwrite confirmation.",
    )
    worker: tiers.WorkerChoice | None = None


class Decision(DecisionFields):
    route: Literal["reply", "inventory", "view", "read", "compute", "transfer"]


class RoutingUnavailable(ValueError):
    """A model routing failure that can be retried without replaying user actions."""


ROUTING_SECONDS = 120


def record_exchange(
    state: dict[str, Any],
    event_id: str,
    text: str,
    reply: str,
    *,
    files: Sequence[str] = (),
    outputs: Sequence[str] = (),
    success: bool = True,
    methods: str = "",
    history: thread_history.History | None = None,
    summary: str = "",
    keep_summary: bool = True,
) -> None:
    full = {
        "id": event_id,
        "user": text,
        "assistant": reply,
        "files": list(files),
        "outputs": list(outputs),
        "success": success,
        "methods": methods,
    }
    if history:
        # Backfill the bounded legacy transcript before older rows leave the prompt window.
        for previous in state.get("messages", []):
            history.record(previous, only_missing=True)
        history.record(full)
    if keep_summary:
        thread_history.update_summary(state, full, summary)
    else:
        state.pop("thread_summary", None)
    messages = [m for m in state.get("messages", []) if m.get("id") != event_id]
    messages.append(
        {
            "id": event_id,
            "user": text if not success else text[:6000],
            "assistant": reply[:6000],
            "files": list(files),
            "outputs": list(outputs),
            "success": success,
            "methods": methods[:2000],
        }
    )
    state["messages"] = messages[-4:]
    state["direct_version"] = 1


async def finish(bridge: BatchBridge, event_id: str, record: dict[str, Any], reply: str) -> bool:
    await bridge.notify(event_id, record, "complete", reply)
    if record.get("notice_text") != reply:
        raise RuntimeError("Slack reply delivery failed; retry the saved response")
    record["status"] = "complete"
    return True


async def show_context(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage, text: str
) -> bool:
    """Show the whole requested guide/cache, with resumable bounded Slack messages."""
    if surface.is_email(record):
        grant = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, storage, record
        )
        return await finish(
            bridge, event_id, record, text if grant.allowed else access.deny_text(grant)
        )
    event = record["payload"]["event"]
    parts = list(chunks(to_mrkdwn(text)))
    sent = int(record.get("context_parts_sent", 0))
    for index, part in enumerate(parts):
        if index < sent:
            continue
        grant = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, storage, record
        )
        if not grant.allowed:
            return await finish(bridge, event_id, record, access.deny_text(grant))
        if index == 0:
            await bridge.notify(event_id, record, "complete", part)
            if record.get("notice_text") != part:
                raise RuntimeError("Context display failed; retry the saved response")
        else:
            await post(
                bridge.app.slack_bot_token,
                event["channel"],
                event.get("thread_ts") or event["ts"],
                part,
                event_id=f"{event_id}:context:{index}",
            )
        record["context_parts_sent"] = index + 1
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    record["status"] = "complete"
    return True


async def dispatch_command(
    bridge: BatchBridge,
    event_id: str,
    record: dict[str, Any],
    storage: Storage,
    cmd: commands.Command,
    grant: access.Grant,
) -> bool | None:
    """One execution path for exact commands and validated natural-language proposals."""
    from . import command_intent
    from .transfers import Transfers

    registry = Registry(storage)
    if commands.spec(cmd.name).sender_reply:
        command_intent.private_reply(record, grant)
    if commands.spec(cmd.name).connection:
        if not grant.email:
            return await finish(bridge, event_id, record, access.deny_text(grant))
        command_intent.private_reply(record, grant)
        connection_reply = await Transfers(bridge, storage).command(cmd, grant, record, event_id)
        return await finish(bridge, event_id, record, connection_reply)
    if cmd.name == "cancel":
        if not (grant.private or grant.allowed):
            return await finish(bridge, event_id, record, access.deny_text(grant))
        await bridge.cancel(event_id, record)
        return True
    if commands.removed_memory(cmd):
        return await finish(bridge, event_id, record, commands.MEMORY_CHANGED)
    if cmd.name in {"memory", "brief"}:
        if not grant.allowed:
            return await finish(bridge, event_id, record, access.deny_text(grant))
        snapshot = await project_memory.GitHubMemory(bridge.settings).read(
            grant.project_id, event_id=event_id
        )
        if snapshot is None:
            return await finish(bridge, event_id, record, "Project memory is unavailable.")
        paths = ["PROJECT.md"] if cmd.name == "brief" else list(cmd.args) or ["INDEX.md", "open.md"]
        for path in paths:
            project_memory.valid_path(path)
        contents = "\n\n".join(
            f"{path}:\n{snapshot.files[path].text}" for path in paths if path in snapshot.files
        )
        if cmd.name == "memory" and not cmd.args and "INDEX.md" not in snapshot.files:
            listing = "\n".join(f"• {path}" for path in sorted(snapshot.files))
            contents = "\n\n".join(part for part in (listing, contents) if part)

        return await show_context(
            bridge, event_id, record, storage, contents or "No project memory saved yet."
        )
    if cmd.name == "data-root":
        if surface.is_email(record):
            command_intent.private_reply(record, grant)
            return await finish(bridge, event_id, record, surface.EMAIL_READ_ONLY)
        if not grant.private:
            raise ValueError("Change the data location in your private Slack conversation.")
        pid = grant.doc.find(cmd.args[0], grant.email)
        if not grant.doc.owns(pid, grant.email):
            raise ValueError("Only the project owner can change its data location.")
        await asyncio.to_thread(bridge.resolve_data, record, uri=cmd.args[1])
    if cmd.name != "init":
        if cmd.name == "repos":
            record["shared_reference_repos"] = sorted(
                await reference_repos.shared_repos(bridge.repository, bridge.settings)
            )
        reply = await asyncio.to_thread(
            commands.run, cmd, grant, registry, bridge.app, record, event_id, bridge.settings
        )
        if reply is not None:
            if cmd.name == "projects":
                await command_intent.remember_projects(bridge, record, grant, cmd)
            if cmd.name == "rename" and bridge.settings.memory_repo:
                doc = await asyncio.to_thread(registry.load)
                renamed = record["renamed_project"]
                if not doc.owns(renamed, grant.email):
                    raise ValueError("Project ownership changed before memory synchronization.")
                project = doc.projects[renamed]
                # The registry operation is idempotent. A transient GitHub failure retries the
                # same command, with the latest name/revision, before acknowledging completion.
                try:
                    await project_memory.GitHubMemory(bridge.settings).rename_project(
                        renamed,
                        project.name,
                        project.name_revision,
                        event_id=event_id,
                        reserved_ids=set(doc.projects),
                    )
                except httpx.HTTPError as exc:
                    # Preserve the receipt for Cloud Tasks retry instead of marking a
                    # registry-only rename complete/failed in the generic HTTP handler.
                    raise RuntimeError("Project memory synchronization needs retry") from exc
            return await finish(bridge, event_id, record, reply)
    if commands.spec(cmd.name).transfer:
        if not grant.allowed:
            return await finish(bridge, event_id, record, access.deny_text(grant))
        if cmd.name == "atomx login status" and not grant.private:
            return await finish(
                bridge,
                event_id,
                record,
                "Ask about your connection in a private conversation with Mica.",
            )
        reply = await Transfers(bridge, storage).command(cmd, grant, record, event_id)
        return await finish(bridge, event_id, record, reply)
    if cmd.name != "init":
        raise ValueError("This operation is not available through this interface.")
    return None


async def read_information(
    bridge: BatchBridge,
    storage: Storage,
    grant: access.Grant,
    record: dict[str, Any],
    decision: Decision,
    files: list[attachments.ThreadFile],
    snapshots: dict[str, reference_repos.RepoSnapshot],
    *,
    max_bytes: int = 384 * 1024,
) -> dict[str, Any]:
    """Read only authorized source/attachments/history; never run user code."""
    selected = list(
        dict.fromkeys(
            reference_repos.valid_repo(name, bridge.settings.reference_github_org)
            for name in decision.reference_repos
        )
    )
    available = grant.project.reference_repos | await reference_repos.shared_repos(
        bridge.repository, bridge.settings
    )
    if not set(selected) <= available or not set(decision.reference_paths) <= set(selected):
        raise ValueError("The requested repository is not in Mica's available reference catalog.")
    for paths in decision.reference_paths.values():
        if any(not reference_repos.eligible(path) for path in paths):
            raise ValueError("Use exact source file paths from the repository snapshot.")
    result: dict[str, Any] = {"references": [], "files": []}
    remaining = max_bytes
    for repo in selected:
        try:
            if repo not in snapshots:
                snapshots[repo] = await reference_repos.fetch(bridge.settings, repo)
            value = reference_repos.prompt_view(
                snapshots[repo], decision.reference_paths.get(repo, []), remaining
            )
            remaining -= sum(len(text.encode()) for text in value["files"].values())
            result["references"].append(value)
        except Exception:
            result["references"].append({"repo": repo, "unavailable": True})
    if decision.read_file_ids:
        result["files"] = await asyncio.to_thread(
            attachments.router_text, storage, files, decision.read_file_ids, remaining
        )
        remaining -= sum(len(row.get("text", "").encode()) for row in result["files"])
    if decision.memory_paths:
        backend = project_memory.GitHubMemory(bridge.settings)
        result["memory"] = []
        for path in dict.fromkeys(decision.memory_paths):
            project_memory.valid_path(path)
            try:
                data, head = await backend.read_bytes(grant.project_id, path)
                kind, media = attachments.classify(Path(path).name, data[: attachments.HEAD_BYTES])
                if thread_memory.asset(path) and kind in {"image", "pdf", "other"}:
                    if len(data) > remaining:
                        result["memory"].append({"path": path, "context_limit": True})
                        continue
                    from .config import CHECKPOINT_PREFIX

                    sha = hashlib.sha256(data).hexdigest()
                    key = f"{CHECKPOINT_PREFIX}/events/{record['event_id']}/memory-artifacts/{sha}"
                    await asyncio.to_thread(storage.put, key, data)
                    name = attachments.safe_name(Path(path).name)
                    file = attachments.ThreadFile(
                        id="m" + sha[:12],
                        source="agent",
                        name=name,
                        path=f"outputs/memory/{sha[:12]}/{name}",
                        kind=kind,
                        media_type=media,
                        size=len(data),
                        sha256=sha,
                        key=key,
                        event_id=record["event_id"],
                    )
                    files.append(file)
                    grant.state["thread_files"] = attachments.merge(
                        grant.state.get("thread_files", []), [file.model_dump()]
                    )
                    await asyncio.to_thread(
                        storage.put,
                        access.checkpoint_key(record["thread_key"]),
                        json.dumps(grant.state).encode(),
                    )
                    result["memory"].append(
                        {
                            "path": path,
                            "file_id": file.id,
                            "workspace_path": file.path,
                            "commit": head,
                        }
                    )
                    remaining -= len(data)
                    continue
                if len(data) > remaining:
                    result["memory"].append({"path": path, "context_limit": True})
                    continue
                text = data.decode("utf-8")
                result["memory"].append({"path": path, "text": text, "commit": head})
                remaining -= len(data)
            except (ValueError, FileNotFoundError, UnicodeError):
                result["memory"].append({"path": path, "unavailable": True})
    if thread_memory.legacy(bridge.settings) and (
        decision.history_query is not None or decision.history_event_id
    ):
        history = thread_history.History(storage, record["thread_key"], grant.project_id)
        try:
            result["history"] = await asyncio.to_thread(
                history.recall,
                decision.history_query or "",
                decision.history_event_id,
                decision.history_offset,
            )
        except (ValueError, FileNotFoundError):
            result["history"] = {"unavailable": True}
        if len(json.dumps(result["history"], ensure_ascii=False).encode()) > remaining:
            result["history"] = {"context_limit": True, "note": "Request history separately."}
    return result


async def cloud_request(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage,
    saved: dict[str, Any],
) -> bool:
    from . import cloud_access

    cloud_access.scope_reply(record)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    await cloud_access.detach_thread(storage, record, event_id)
    reply = await cloud_access.propose(
        bridge, storage, record, event_id,
        CloudAccessProposal.model_validate(saved["cloud_access_request"]),
    )
    return await finish(bridge, event_id, record, reply)


async def handle(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage
) -> bool:
    """Return False only for an authorized computation with a durable thread context."""
    from . import conversation
    from .transfers import Transfers

    event = record["payload"]["event"]
    text = message_text(event.get("text", ""))
    if bridge.settings.cloud_access and surface.is_email(record):
        from . import cloud_access

        reply = await cloud_access.email_decision(bridge, storage, record)
        if reply is not None:
            cloud_access.scope_reply(record)
            return await finish(bridge, event_id, record, reply)
    conversation.start(record)
    saved = conversation.saved_decision(record)
    if saved.get("route") == "cloud_access":
        return await cloud_request(bridge, event_id, record, storage, saved)
    grant = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, storage, record)
    if observability.capture_project(record, grant):
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if not grant.email:
        return await finish(bridge, event_id, record, access.deny_text(grant))
    if record.get("ambient") and not grant.allowed:
        return await finish(bridge, event_id, record, access.deny_text(grant))
    cmd = None if record.get("ambient") else commands.parse(event.get("text", ""))
    if (
        not grant.allowed
        and cmd is None
        and not grant.private
        and not surface.is_email(record)
        and bridge.settings.access_request_owner_id
    ):
        from . import access_requests

        return await finish(
            bridge, event_id, record, await access_requests.propose(bridge, record, grant)
        )
    files = event.get("files") or []
    if files and cmd and cmd.name == "init":
        raise ValueError("Send files in their own message, not with init.")
    registry = Registry(storage)
    if cmd is None:
        if saved:
            if record.get("decision_access") != grant.signature:
                return await finish(
                    bridge, event_id, record, "Project access changed. Send a new request."
                )
        elif not grant.allowed:
            opening = await conversation.run(
                bridge, event_id, record, storage, grant, {"request": text}, []
            )
            saved = opening.model_dump()
        if saved.get("route") == "access":
            from . import access_requests

            return await finish(
                bridge, event_id, record, await access_requests.propose(bridge, record, grant)
            )
        if saved.get("route") == "cloud_access":
            return await cloud_request(bridge, event_id, record, storage, saved)
        if saved.get("route") == "command":
            from . import command_intent

            proposal = command_intent.Proposal.model_validate(saved["command_proposal"])
            if surface.is_email(record) and commands.spec(proposal.operation).approval:
                command_intent.private_reply(record, grant)
                return await finish(bridge, event_id, record, surface.EMAIL_READ_ONLY)
            if not grant.private and commands.spec(proposal.operation).dm_only:
                return await finish(
                    bridge,
                    event_id,
                    record,
                    "Please continue in a private conversation with Mica "
                    "for account and project selection requests.",
                )
            if commands.spec(proposal.operation).sender_reply:
                command_intent.private_reply(record, grant)
            referents = record.get("intent_referents")
            if referents and (
                referents["expires"] < time.time()
                or referents["visibility"] != command_intent.visibility(grant)
            ):
                return await finish(
                    bridge,
                    event_id,
                    record,
                    "Those project choices changed or expired. Ask me for the current list again.",
                )
            try:
                cmd = command_intent.command(
                    proposal,
                    grant,
                    source_text=event.get("text", "")
                    + "\n"
                    + record.get("intent_prior_request", ""),
                )
            except ValueError as exc:
                command_intent.private_reply(record, grant)
                question = await command_intent.clarification(
                    bridge, record, grant, str(exc), proposal
                )
                return await finish(bridge, event_id, record, question)
            if commands.spec(cmd.name).approval:
                from . import command_approval

                return await command_approval.propose(bridge, event_id, record, storage, cmd, grant)
        elif not grant.allowed:
            return await finish(
                bridge, event_id, record, saved.get("reply") or access.deny_text(grant)
            )
    if cmd and commands.spec(cmd.name).exact_approval:
        from . import command_approval, command_intent

        if surface.is_email(record):
            command_intent.private_reply(record, grant)
            return await finish(bridge, event_id, record, surface.EMAIL_READ_ONLY)
        if not grant.private:
            raise ValueError("Change project settings in your private Slack conversation.")
        pid = grant.doc.find(cmd.args[0] or grant.project_id, grant.email)
        cmd = commands.Command(cmd.name, (pid, cmd.args[1]))
        return await command_approval.propose(bridge, event_id, record, storage, cmd, grant)
    if cmd and cmd.name != "init":
        handled = await dispatch_command(bridge, event_id, record, storage, cmd, grant)
        if handled is not None:
            return handled
    is_init = bool(cmd and cmd.name == "init")
    if is_init:
        # Setup can mention the requester's previous default as a switch-back choice.
        # Keep project admission checks while limiting this mixed response to its sender.
        record["reply_audience"] = "sender_only"
    if is_init and files:
        raise ValueError("Send files in their own message, not with init.")
    if is_init:
        assert cmd
        own_empty_context = (
            grant.state.get("conversation_origin_event") == event_id
            and not grant.state.get("messages")
            and not grant.state.get("turn")
        )
        if grant.has_context and not record.get("direct_init") and not own_empty_context:
            raise ValueError("Start init in a new thread.")
        await asyncio.to_thread(bridge.resolve_data, record, uri=cmd.args[1])
        if "init_previous_project_id" not in record:
            record["init_previous_project_id"] = grant.doc.active_for(
                grant.email, record["payload"]["team_id"], event["channel"], private=grant.private
            )
            # Save the switch-back target before changing the binding, including on retries.
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        pid = await asyncio.to_thread(
            registry.update,
            event_id,
            grant.email,
            text,
            lambda d: d.create_and_bind(
                record["payload"]["team_id"],
                event["channel"],
                grant.email,
                cmd.args[0],
                cmd.args[1],
                private=grant.private,
                force=cmd.force,
            ),
        )
        if record.get("direct_decision", {}).get("route") == "command":
            record.pop("direct_decision", None)
        record["direct_init"] = True
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        doc = await asyncio.to_thread(registry.load)
        grant = access.Grant(
            grant.email,
            grant.private,
            {} if own_empty_context else grant.state,
            doc,
            pid,
            doc.can_use(pid, grant.email),
            grant.channel_key,
            False if own_empty_context else grant.has_context,
            grant.identity_policy,
        )
    if not grant.allowed:
        return await finish(bridge, event_id, record, access.deny_text(grant))
    # Record only the authorized project, before memory/model calls can fail. This is
    # diagnostic context; access.resolve still rechecks the registry on every retry.
    record.update(
        principal=grant.email, project_id=grant.project_id, project_name=grant.project.name
    )
    transfer_service = Transfers(bridge, storage)
    if event.get("atomx_password_removed"):
        return await finish(
            bridge,
            event_id,
            record,
            "Please use the private AtoMx connection link and consider changing that password. "
            "Your original message still exists in Slack or your mailbox.",
        )
    state = access.read_state(storage, grant)
    ctx = state.get("context")
    if not ctx:
        uri = urlsplit(grant.project.data_uri)
        thread = event.get("thread_ts") or event["ts"]
        ctx = dict(
            workdir=(
                access.private_workdir(
                    bridge.settings,
                    grant.email,
                    grant.project_id,
                    record["thread_key"],
                )
                if grant.private
                else str(
                    bridge.settings.work_root / grant.project_id / f"{event['channel']}-{thread}"
                )
            ),
            data_path=str(bridge.settings.data_mount_root / uri.netloc / uri.path.lstrip("/")),
            project_slug=grant.project_id,
            project_md=project_memory.NO_GUIDE,
            session_id=None,
            thread_ts=thread,
            survey_only=False,
        )
        state.update(context=ctx, turn=0, is_init=is_init, messages=[])
        state["conversation_origin_event"] = event_id
        state["thread_memory_origin"] = bridge.settings.thread_memory
        state["thread_memory_exempt"] = [event_id] if is_init else []
        if surface.is_email(record):
            state["private_owner"] = grant.email
        # Bind the thread before a router/memory failure can end its first turn.
        await asyncio.to_thread(
            storage.put, access.checkpoint_key(record["thread_key"]), json.dumps(state).encode()
        )
    # Rebind the next turn to the registry's current data root. Do not resume an SDK
    # session whose sandbox was initialized with the previous mount scope. Preserve
    # conversation history and work products; in-flight requests fail their existing
    # access-signature check rather than being silently retargeted.
    uri = urlsplit(grant.project.data_uri)
    expected_path = str(bridge.settings.data_mount_root / uri.netloc / uri.path.lstrip("/"))
    if ctx["data_path"] != expected_path:
        await asyncio.to_thread(bridge.resolve_data, record, uri=grant.project.data_uri)
        ctx.update(data_path=expected_path, session_id=None)
        await asyncio.to_thread(
            storage.put, access.checkpoint_key(record["thread_key"]), json.dumps(state).encode()
        )
    if surface.is_email(record):
        # Gmail message timestamps change on every reply. Worker sessions are keyed by
        # the original conversation context, not the latest message's timestamp.
        event["thread_ts"] = ctx["thread_ts"]
    backend = project_memory.GitHubMemory(bridge.settings)
    memory = await backend.read(grant.project_id, event_id=event_id)
    guide = memory.files.get("PROJECT.md") if memory else None
    ctx["project_md"] = guide.text if guide else project_memory.NO_GUIDE
    if is_init and "memory_init_base" not in record and memory is not None:
        # Only the guide base is needed for init CAS; do not put a 1 MiB project in Firestore.
        record["memory_init_base"] = project_memory.Snapshot(
            head_oid=memory.head_oid, files={"PROJECT.md": guide} if guide else {}
        ).model_dump()
    if files:
        if record.get("attachments_access", grant.signature) != grant.signature:
            return await finish(
                bridge, event_id, record, "Project access changed. Send a new request."
            )
        if "attachments" not in record:
            await bridge.notify(event_id, record, "replying", "Reading your files…")
            root = access.run_root(
                grant.private, grant.email, grant.project_id, record["thread_key"], ctx["thread_ts"]
            )
            if surface.is_email(record):
                ingested = await asyncio.to_thread(
                    attachments.ingest_email, storage, record, root, event_id, bridge.settings
                )
            else:
                ingested = await asyncio.to_thread(
                    attachments.ingest,
                    bridge.app.slack_bot_token,
                    storage,
                    [{**f, "user": f.get("user") or event.get("user", "")} for f in files],
                    root,
                    event_id,
                    bridge.settings,
                )
            record["attachments"] = [f.model_dump() for f in ingested]
            record["attachments_access"] = grant.signature
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        state["thread_files"] = attachments.merge(
            state.get("thread_files", []), record["attachments"]
        )
    current_ids = {f["id"] for f in record.get("attachments", [])}
    decision: conversation.Decision
    if record.get("ambient"):
        from .ambient_config import AmbientWorker

        preset = AmbientWorker.model_validate(record["ambient"]["preset"])
        decision = conversation.Decision(
            route="compute", reply=text,
            worker=tiers.WorkerChoice(
                tier=preset.tier, on_demand=preset.on_demand,
                expected_hours=preset.max_hours,
                gpu_hours=preset.max_hours if preset.tier == "gpu" else None,
                gpu_type=preset.gpu_type,
                gpu_sizing=preset.gpu_sizing,
                allow_resize=False, reason="Operator-configured background task",
            ),
        )
        record["direct_decision"] = decision.model_dump()
        record["direct_decision_pending"] = False
        record["decision_access"] = grant.signature
        record["memory_project_id"] = grant.project_id
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    elif saved := conversation.saved_decision(record):
        decision = conversation.Decision.model_validate(saved)
    else:
        await bridge.notify(event_id, record, "replying", "Mica is thinking")
        model_context = {
            "request": text,
            "brief": ctx["project_md"],
            "project_md": ctx["project_md"],
            "data_path": ctx["data_path"],
            "data_uri": grant.project.data_uri,
            "project_name": grant.project.name,
            "reference_repos": sorted(
                grant.project.reference_repos
                | await reference_repos.shared_repos(bridge.repository, bridge.settings)
            ),
            "transfer_tool": await transfer_service.context(grant.project_id, grant.email),
            "analysis_tools": [spatial_analysis_capability()],
            "gpu_available": bool(tiers.available_gpus(bridge.settings)),
            "gpu_types": tiers.available_gpus(bridge.settings),
            "compute_capabilities": tiers.capabilities(bridge.settings),
            "write_access": (
                surface.EMAIL_READ_ONLY
                if surface.is_email(record)
                else await asyncio.to_thread(
                    write_access.status, storage, grant.project_id, bridge.settings
                )
            ),
            "recent_messages": state.get("messages", []),
            "thread_handoff": state.get("thread_summary", {}),
        }
        messages = model_context.pop("recent_messages")
        handoff = model_context.pop("thread_handoff")
        model_context["project_memory"] = project_memory.context(memory, text, bridge.settings)
        model_context["thread_memory"] = (
            thread_memory.context(
                {p: f.text for p, f in memory.files.items()}, record["thread_key"]
            )
            if memory and thread_memory.github_reads(bridge.settings)
            else handoff
        )
        model_context["recent_messages"] = messages
        from .calibration import history

        worker_history = await asyncio.to_thread(history, storage, grant.project_id)
        if worker_history:
            model_context["worker_history"] = worker_history
        if is_init:
            assert cmd
            settings, _ = await asyncio.to_thread(bridge.resolve_data, record, uri=cmd.args[1])
            model_context["init_inventory"] = await asyncio.to_thread(
                inventory, cmd.args[1], settings, event_id=event_id
            )
            model_context["request"] += "\nDraft an initial brief directly, without computation."
        thread_files = attachments.models(state.get("thread_files", []))
        current_files = [f for f in thread_files if f.id in current_ids]
        media = await asyncio.to_thread(attachments.router_media, storage, current_files)
        heads = await asyncio.to_thread(
            attachments.router_heads, storage, thread_files, current_ids
        )
        model_context["attachments"] = attachments.router_listing(
            thread_files, current_ids, heads, media=media
        )

        decision = await conversation.run(
            bridge,
            event_id,
            record,
            storage,
            grant,
            model_context,
            thread_files,
            media=media,
            init=is_init,
        )
        if is_init and (decision.route != "reply" or decision.guide is None):
            raise ValueError("Could not draft the initial brief. Please retry init in this thread.")
        record["direct_decision"] = decision.model_dump()
        record["direct_decision_pending"] = False
        if decision.route == "cloud_access":
            from .cloud_access import scope_reply

            scope_reply(record)
        else:
            record["decision_access"] = grant.signature
            record["memory_project_id"] = grant.project_id
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if decision.route in {"command", "cloud_access"}:
        # Re-enter the exact/validated command path using the durable terminal proposal.
        # No further model call is made, and init retains its existing admission sequence.
        return await handle(bridge, event_id, record, storage)
    if surface.is_email(record) and decision.write_paths:
        return await finish(bridge, event_id, record, surface.EMAIL_READ_ONLY)
    current = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, storage, record)
    # Init's binding may have moved in another thread, but this turn is fixed to its created ID.
    if is_init and not current.has_context:
        current.project_id = grant.project_id
        current.allowed = current.doc.can_use(grant.project_id, grant.email)
    if (
        not current.allowed
        or current.signature != grant.signature
        or (record.get("decision_access") and record["decision_access"] != current.signature)
    ):
        return await finish(bridge, event_id, record, "Project access changed. Send a new request.")
    if record.get("information_repos"):
        available_repos = current.project.reference_repos | await reference_repos.shared_repos(
            bridge.repository, bridge.settings
        )
        if not set(record["information_repos"]) <= available_repos:
            return await finish(
                bridge, event_id, record, "Reference repository access changed. Send a new request."
            )
    if is_init and decision.guide is not None:
        if bridge.settings.memory_repo:
            if memory is None:
                raise RuntimeError("Project memory is unavailable; retry init")
            base = project_memory.Snapshot.model_validate(record["memory_init_base"])
            saved_guide = await backend.commit(
                grant.project_id,
                base,
                {"PROJECT.md": decision.guide},
                source="init",
                event_id=event_id,
                author_line=f"Speaker: {grant.email}",
            )
            if saved_guide.rejected:
                raise ValueError("Initial project guide was rejected")
            from . import memory_compaction

            await memory_compaction.enqueue(bridge, grant.project_id, saved_guide)
            record["memory_init_saved"] = saved_guide.model_dump()
        ctx["project_md"] = decision.guide
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if decision.route == "reply" and not cmd:
        record_exchange(
            state,
            event_id,
            text,
            decision.reply,
            files=[f["name"] for f in record.get("attachments", [])],
            history=thread_history.History(storage, record["thread_key"], grant.project_id)
            if thread_memory.legacy(bridge.settings)
            else None,
            keep_summary=not thread_memory.github_reads(bridge.settings),
        )
    else:
        state["direct_version"] = 1
    await asyncio.to_thread(
        storage.put, access.checkpoint_key(record["thread_key"]), json.dumps(state).encode()
    )
    if decision.route == "transfer":
        if decision.transfer_request is None:
            raise ValueError("Name the AtoMx sample to transfer.")
        reply = await transfer_service.request(current, record, event_id, decision.transfer_request)
        return await finish(bridge, event_id, record, reply)
    if decision.route == "compute":
        from .tiers import launch_choice

        selected = set() if record.get("ambient") else {
            reference_repos.valid_repo(name, bridge.settings.reference_github_org)
            for name in decision.reference_repos
        } or current.project.reference_repos
        available = current.project.reference_repos | await reference_repos.shared_repos(
            bridge.repository, bridge.settings
        )
        if not selected <= available:
            raise ValueError(
                "The requested repository is not in Mica's available reference catalog."
            )
        record["reference_repos"] = sorted(selected)
        try:
            record["launch"] = launch_choice(decision.worker, bridge.settings)
        except tiers.WorkerUnavailable as exc:
            return await finish(bridge, event_id, record, str(exc))
        record["route"] = "compute"
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        return False
    reply = decision.reply
    if is_init:
        if (
            current.doc.active_for(
                grant.email, record["payload"]["team_id"], event["channel"], private=grant.private
            )
            == grant.project_id
        ):
            selection = f"Active project: {grant.project.name}."
            previous = record.get("init_previous_project_id")
            if (
                previous
                and previous != grant.project_id
                and current.doc.can_use(previous, grant.email)
            ):
                selector = current.doc.selector(previous, grant.email)
                selection += f" Switch back: `switch {selector}`."
        else:
            selector = current.doc.selector(grant.project_id, grant.email)
            selection = f"Project ready: {grant.project.name}. Select it with `switch {selector}`."
        reply = (
            selection
            + "\n\n"
            + decision.reply
            + "\n\n"
            + PROJECT_HELP
            + "\n\n"
            + project_memory.PRIVACY
        )
    # Model replies use Markdown, just like worker replies. Convert only at the
    # Slack boundary so saved decisions/history and email retain the original text.
    completed = await finish(
        bridge, event_id, record, reply if surface.is_email(record) else to_mrkdwn(reply)
    )
    record["memory_reply"] = decision.reply
    return completed
