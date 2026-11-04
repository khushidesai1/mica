"""Requester-bound Slack approval for natural-language administrative changes."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

from . import access, approvals, commands, observability, surface, write_access
from .identity import member_email, resolve_email
from .registry import ProjectEntry
from .storage import Conflict, Storage

if TYPE_CHECKING:
    from .batch import BatchBridge

PREFIX = "mica_command_"
ADMIN = {name for name, policy in commands.COMMANDS.items() if policy.approval}


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid confirmation")
    return f"projects/_command_approvals/{event_id}.json"


def is_action(payload: dict[str, Any]) -> bool:
    return KIND.matches(payload)



def authorize(
    bridge: BatchBridge,
    storage: Storage,
    cmd: commands.Command,
    grant: access.Grant,
    record: dict[str, Any],
) -> None:
    if surface.is_email(record) or not grant.private or not grant.email:
        raise ValueError("Administrative changes require your private Slack conversation.")
    if cmd.name not in ADMIN or cmd.force or not cmd.args:
        raise ValueError("Unsupported administrative change")
    pid = cmd.args[0]
    if cmd.name.startswith("write "):
        event = record["payload"]["event"]
        admin = f"{record['payload']['team_id']}:{event['user']}"
        if admin not in bridge.settings.csv("write_admin_ids"):
            raise ValueError("Only designated write-access administrators can change writes.")
        if not grant.doc.can_use(pid, grant.email):
            raise ValueError("That project is unavailable.")
        if cmd.name == "write grant":
            write_access.validate_scope(cmd.args[1], bridge.settings)
            write_access.validate_project(cmd.args[1], grant.doc, pid, grant.email)
    elif not grant.doc.owns(pid, grant.email):
        raise ValueError("Only the project owner can change this setting.")


async def propose(
    bridge: BatchBridge,
    event_id: str,
    record: dict[str, Any],
    storage: Storage,
    cmd: commands.Command,
    grant: access.Grant,
) -> bool:
    from .direct import finish

    if surface.is_email(record):
        return await finish(bridge, event_id, record, surface.EMAIL_READ_ONLY)
    authorize(bridge, storage, cmd, grant, record)
    observability.capture_project(record, grant, target=cmd.args[0])
    if cmd.name == "data-root":
        await asyncio.to_thread(bridge.resolve_data, record, uri=cmd.args[1])
    event, team = record["payload"]["event"], record["payload"]["team_id"]
    if commands.spec(cmd.name).recipient:
        recipient = await asyncio.to_thread(
            member_email,
            bridge.app,
            bridge.settings,
            storage,
            team,
            cmd.args[1],
            removing=cmd.name in {"unshare", "remove-owner"},
        )
        cmd = commands.Command(cmd.name, (cmd.args[0], recipient))
    if cmd.name in {"add-owner", "remove-owner"}:
        # Validate role constraints before offering a confirmation; mutate only a copy.
        grant.doc.model_copy(deep=True).set_co_owner(
            cmd.args[0], grant.email, cmd.args[1], remove=cmd.name == "remove-owner"
        )
    if cmd.name in {"repos add", "repos remove"}:
        grant.doc.model_copy(deep=True).set_reference_repo(
            cmd.args[0],
            grant.email,
            cmd.args[1],
            remove=cmd.name == "repos remove",
            org=bridge.settings.reference_github_org,
        )
    proposal = {
        "command": asdict(cmd),
        "event_id": event_id,
        "operation_id": event_id,
        "principal": grant.email,
        "user": event["user"],
        "team": team,
        "channel": event["channel"],
        "thread_key": record["thread_key"],
        "notice_ts": record.get("notice_ts", ""),
        "project": grant.doc.projects[cmd.args[0]].model_dump(mode="json"),
        "expires": KIND.deadline(),
        "state": "pending",
    }
    try:
        generation = await asyncio.to_thread(
            approvals.save, storage, key(event_id), proposal, None
        )
    except Conflict:
        saved = await asyncio.to_thread(approvals.load_json, storage, key(event_id))
        assert saved is not None
        proposal, generation = saved
    if proposal["state"] == "expired" or (
        proposal["state"] == "pending" and approvals.expired(proposal["expires"])
    ):
        await expire_record(bridge, storage, event_id, record, proposal, generation)
        return True
    if proposal["state"] != "pending":
        return await finish(
            bridge, event_id, record, "This confirmation has ended. Send a new request."
        )
    saved = proposal["command"]
    text = (
        f"Confirm {saved['name']} for {proposal['project']['name']} "
        f"({saved['args'][0]})"
        + (": " + " · ".join(saved["args"][1:]) if len(saved["args"]) > 1 else "")
        + ". This request expires in 10 minutes."
    )
    if saved["name"] == "repos add":
        text += (
            " Project members and published channel audiences may receive content from this "
            "repository. Mica reads only repositories enabled for its AziziLab GitHub App; "
            "no GitHub write access is granted."
        )
    if saved["name"] == "repos remove":
        text += (
            " New requests omit this repository; running requests may stop. "
            "Previously delivered content is not recalled."
        )
    if saved["name"] == "data-root":
        text += (
            f" Current location: {proposal['project']['data_uri']}. "
            "This affects every project member and future requests in existing conversations. "
            "Running requests using the old location may stop. Files and write permissions "
            "are unchanged."
        )
    if saved["name"] == "add-owner":
        text += (
            " They can manage project sharing, other co-owners, publication, name and data "
            "location. Existing owners retain their roles. This grants no cloud IAM or "
            "write-administrator permissions."
        )
    if saved["name"] == "remove-owner":
        text += " They keep collaborator access. The original owner remains an owner."
    if saved["name"] == "share":
        text += (
            " This grants the named email address access to the project and its shared memory. "
            "They can use email without Slack membership or channel publication."
        )
    if saved["name"] == "publish":
        text += " Everyone who can view that channel, including future members, may see replies."
    await bridge.notify(event_id, record, "replying", text)
    if record.get("notice_text") != text:
        raise RuntimeError("Confirmation presentation needs retry")
    if proposal["notice_ts"] != record["notice_ts"]:
        proposal["notice_ts"] = record["notice_ts"]
        await asyncio.to_thread(
            approvals.save, storage, key(event_id), proposal, generation
        )
    await approvals.card(
            AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
        channel=event["channel"],
        ts=record["notice_ts"],
        text=text,
        blocks=[
            {"type": "section", "text": {"type": "plain_text", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "action_id": PREFIX + action,
                        "value": event_id,
                        **({"style": "primary"} if action == "confirm" else {}),
                    }
                    for action, label in [("confirm", "Confirm"), ("cancel", "Cancel")]
                ],
            },
        ],
    )
    await approvals.watch(bridge, KIND, event_id, proposal["expires"], record)
    return await finish(bridge, event_id, record, text)


async def _handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    from .direct import dispatch_command

    action = payload["actions"][0]
    verb, event_id = action["action_id"].removeprefix(PREFIX), str(action["value"])
    if verb not in {"confirm", "cancel", "again"}:
        raise ValueError("Unknown confirmation action")
    path = key(event_id)
    async with approvals.lease(
        bridge, f"command-approval:{event_id}", seconds=240,
        busy="This confirmation is already being processed.",
    ):
        saved = await asyncio.to_thread(approvals.load_json, storage, path)
        if saved is None:
            raise ValueError("This confirmation is unavailable.")
        proposal, generation = saved
        team = payload.get("team", {}).get("id", "")
        user = payload.get("user", {}).get("id", "")
        channel = payload.get("channel", {}).get("id", "")
        if (team, user, channel) != (proposal["team"], proposal["user"], proposal["channel"]):
            raise ValueError("Only the requester can confirm in the original conversation.")
        principal = await asyncio.to_thread(
            resolve_email, bridge.app, bridge.settings, storage, team, user
        )
        if principal != proposal["principal"]:
            raise ValueError("Requester identity changed.")
        if payload.get("message", {}).get("ts") != proposal.get("notice_ts"):
            raise ValueError("Confirm from the original request message.")
        if verb == "again":
            if proposal["state"] not in {"pending", "expired"}:
                return {}
            record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
            if not record or record["thread_key"] != proposal["thread_key"]:
                raise ValueError("The original request is unavailable.")
            grant = await asyncio.to_thread(
                access.resolve, bridge.app, bridge.settings, storage, record
            )
            args = proposal["command"]
            cmd = commands.Command(args["name"], tuple(args["args"]), args["force"])
            authorize(bridge, storage, cmd, grant, record)
            record.pop("approval_expired", None)
            if proposal["state"] == "expired":
                proposal.update(state="pending", expires=KIND.deadline(),
                                project=grant.doc.projects[cmd.args[0]].model_dump(mode="json"))
                await asyncio.to_thread(approvals.save, storage, path, proposal, generation)
            await propose(bridge, event_id, record, storage, cmd, grant)
            await approvals.save_record(bridge, event_id, record)
            return {}
        if proposal["state"] == "cancelled" and proposal.get("notification_pending"):
            await approvals.card(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
                channel, proposal["notice_ts"], "Change cancelled.", [],
            )
            proposal["notification_pending"] = False
            await asyncio.to_thread(approvals.save, storage, path, proposal, generation)
            return {}
        if proposal["state"] in {"completed", "cancelled", "expired"}:
            return {}
        if proposal["state"] == "pending" and approvals.expired(proposal["expires"]):
            record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
            if record:
                await expire_record(bridge, storage, event_id, record, proposal, generation)
            raise ValueError("This confirmation expired. Use Request again for fresh confirmation.")
        if verb == "cancel":
            if proposal["state"] != "pending":
                raise ValueError("This change has already started.")
            proposal["state"] = "cancelled"
            proposal["notification_pending"] = True
            generation = await asyncio.to_thread(
                approvals.save, storage, path, proposal, generation
            )
            await approvals.card(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
                channel, proposal["notice_ts"], "Change cancelled.", [],
            )
            proposal["notification_pending"] = False
            await asyncio.to_thread(approvals.save, storage, path, proposal, generation)
            return {}
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if not record or record["thread_key"] != proposal["thread_key"]:
            raise ValueError("The original request is unavailable.")
        grant = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, storage, record
        )
        args = proposal["command"]
        cmd = commands.Command(args["name"], tuple(args["args"]), args["force"])
        authorize(bridge, storage, cmd, grant, record)
        observability.capture_project(record, grant, target=cmd.args[0])
        pid = cmd.args[0]
        # An already-applied operation must be able to finish memory synchronization and
        # delivery after a crash. Its durable receipt prevents repeating its mutation.
        applied = proposal["operation_id"] in grant.doc.applied_events
        if cmd.name.startswith("write "):
            applied = proposal["operation_id"] in write_access.load(storage, pid).events
        if not applied and grant.doc.projects[pid] != ProjectEntry.model_validate(
            proposal["project"]
        ):
            raise ValueError("Project settings changed. Request a fresh confirmation.")
        if proposal["state"] == "pending":
            proposal["state"] = "executing"
            generation = await asyncio.to_thread(
                approvals.save, storage, path, proposal, generation
            )
        record["command_expected_project"] = proposal["project"]
        try:
            await dispatch_command(bridge, proposal["operation_id"], record, storage, cmd, grant)
        finally:
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        proposal["state"] = "completed"
        proposal["result"] = record.get("notice_text", "")
        await asyncio.to_thread(approvals.save, storage, path, proposal, generation)
        return {}


async def expire_record(
    bridge: BatchBridge, storage: Storage, event_id: str, record: dict[str, Any],
    proposal: dict[str, Any], generation: str,
) -> None:
    if (proposal["state"] not in {"pending", "expired"}
            or not approvals.expired(proposal["expires"])):
        return
    proposal["state"] = "expired"
    await asyncio.to_thread(approvals.save, storage, key(event_id), proposal, generation)
    record["status"] = "complete"  # Release the queue without claiming the change ran.
    await approvals.expired_card(bridge, KIND, event_id, record)
    await approvals.save_record(bridge, event_id, record)


async def expire(bridge: BatchBridge, storage: Storage, event_id: str) -> None:
    async with approvals.lease(bridge, f"command-approval:{event_id}", seconds=240):
        saved = await asyncio.to_thread(approvals.load_json, storage, key(event_id))
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if saved and record:
            await expire_record(bridge, storage, event_id, record, *saved)


async def decide(bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: Any
                 ) -> dict[str, Any]:
    return await _handle(bridge, storage, payload)


KIND = approvals.Kind("command", (PREFIX,), key, 600, decide, expire)


async def handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    from fastapi import BackgroundTasks

    return await approvals.handle(bridge, storage, payload, BackgroundTasks())
