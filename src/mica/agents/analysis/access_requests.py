"""Owner-reviewed project access; model output never grants permissions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import BackgroundTasks
from pydantic import BaseModel, Field
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import access, approvals, identity, surface
from .registry import ProjectEntry, Registry, RegistryDoc
from .storage import Storage

if TYPE_CHECKING:
    from .batch import BatchBridge

PREFIX = "mica_access_request_"
CALLBACK = PREFIX + "approve"
log = logging.getLogger(__name__)


class AccessRequest(BaseModel):
    id: str
    team: str
    user: str
    email: str
    channel: str
    thread: str
    private: bool
    project: str = ""
    approved_project: str = ""
    owner: str
    request: str
    expires: float
    state: Literal["pending", "approved", "denied", "expired"] = "pending"
    owner_channel: str = ""
    message_ts: str = ""
    delivery: str = "pending"
    review: dict[str, Any] = Field(default_factory=dict)
    decided_by: str = ""
    decided_at: float = 0
    notification: str = "pending"
    notification_ts: str = ""


def key(request_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("Invalid access request.")
    return "access-request:" + request_id


async def save(bridge: BatchBridge, row: AccessRequest) -> None:
    await bridge.repository.set_connector_state(key(row.id), row.model_dump())


def client_for(bridge: BatchBridge) -> AsyncWebClient:
    return AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5, retry_handlers=[])


def blocks(row: AccessRequest) -> list[dict[str, Any]]:
    scope = "Project membership" if row.private else "Project access in a channel"
    text = (
        f"*Access approval needed · {scope}*\nRequester: <@{row.user}>\n"
        f"Email: {escape(row.email)}\n"
        + ("From their private conversation" if row.private else f"Channel: <#{row.channel}>")
    )
    result: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "section",
            "text": {"type": "plain_text", "text": row.request or "Project work requested"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": (
                        "Awaiting your review · expires after 24 hours. "
                        "Choose the exact project before approving."
                        if row.state == "pending"
                        else ("Expired · no permissions changed" if row.state == "expired"
                              else f"{row.state.capitalize()} · reviewed by {row.decided_by}")
                    ),
                }
            ],
        },
    ]
    if row.state == "expired":
        result.extend(approvals.expiry_blocks(KIND, row.id,
            "This access approval expired. The request is saved; no permissions changed."))
    if row.state == "pending":
        result.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "action_id": PREFIX + action,
                        "value": row.id,
                        **({"style": "primary"} if action == "review" else {}),
                    }
                    for action, label in [("review", "Approve access…"), ("deny", "Deny")]
                ],
            }
        )
    return result


def requester_reply(row: AccessRequest) -> str:
    if row.state == "expired":
        return ("This access approval expired. Your request is saved; "
                "use Request again for a fresh review.")
    if row.state == "denied":
        return "The owner declined this access request. Please contact them before trying again."
    if row.state == "approved":
        return (
            "The owner reviewed this request. "
            "Send a fresh message so I can check your current access."
        )
    if not row.message_ts:
        return (
            "This conversation needs project access, but I couldn't deliver the owner "
            "approval card. Please contact the owner."
        )
    reason = (
        "You need permission for this project."
        if row.private
        else "Project work isn't authorized for you in this channel yet."
    )
    return (
        reason + " I've sent the owner an approval card. Nothing has been run; "
        "I'll let you know their decision."
    )


async def propose(bridge: BatchBridge, record: dict[str, Any], grant: access.Grant) -> str:
    """Deduplicate per person/scope/day; only verified Slack identities may request."""
    owner = bridge.settings.access_request_owner_id
    payload, event = record["payload"], record["payload"]["event"]
    team = str(payload.get("team_id", ""))
    if (
        surface.is_email(record)
        or grant.allowed
        or not grant.email
        or not owner
        or not owner.startswith(team + ":")
    ):
        return access.deny_text(grant)
    # Existing users with an unselected DM need selection, not another grant.
    if grant.private and not grant.project_id and grant.doc.visible(grant.email):
        return (
            "You already have project access. Tell me which of your projects to use, "
            "or ask me to list them."
        )
    request_id = hashlib.sha256(
        (
            f"{team}:{event['user']}:{event['channel']}:{grant.project_id}:"
            f"{int(time.time() // 86400)}"
        ).encode()
    ).hexdigest()[:32]
    async with approvals.lease(bridge, key(request_id)):
        saved = await bridge.repository.get_connector_state(key(request_id))
        row = (
            AccessRequest.model_validate(saved)
            if saved
            else AccessRequest(
                id=request_id,
                team=team,
                user=event["user"],
                email=grant.email,
                channel=event["channel"],
                thread=event.get("thread_ts") or event["ts"],
                private=grant.private,
                project=grant.project_id,
                owner=owner,
                request=str(event.get("text", ""))[:1000],
                expires=KIND.deadline(),
            )
        )
        if row.email != grant.email or row.owner != owner:
            raise ValueError("Access request identity changed.")
        if not row.message_ts and row.delivery == "pending":
            client = client_for(bridge)
            # An ambiguous send stays held, never blindly posts another approval.
            row.delivery = "sending"
            await save(bridge, row)
            try:
                sent = await client.chat_postMessage(
                    # chat:write supports an owner user ID and returns the DM ID;
                    # conversations.open would require an unnecessary im:write scope.
                    channel=owner.split(":", 1)[1],
                    text="Mica access approval needed",
                    blocks=blocks(row),
                    unfurl_links=False,
                    unfurl_media=False,
                    client_msg_id=str(uuid5(NAMESPACE_URL, key(row.id))),
                )
            except SlackApiError as exc:
                row.delivery = "pending" if exc.response.get("error") else "unknown"
                await save(bridge, row)
                raise
            row.owner_channel = str(sent["channel"])
            if not re.fullmatch(r"D[A-Z0-9]+", row.owner_channel):
                raise ValueError("The owner approval requires a private conversation.")
            row.message_ts, row.delivery = sent["ts"], "sent"
            await save(bridge, row)
        await approvals.watch(bridge, KIND, row.id, row.expires)
        record["access_request_id"] = row.id
        return requester_reply(row)



def is_action(payload: dict[str, Any]) -> bool:
    return KIND.matches(payload)


async def notify_decision(
    bridge: BatchBridge, storage: Storage, request_id: str, *, retry: bool = False
) -> None:
    """Retry card edits safely; never repeat an ambiguous requester notification."""
    claim = await bridge.repository.begin_event(key(request_id), lease_seconds=60)
    if claim.status != "claimed":
        return
    try:
        raw = await bridge.repository.get_connector_state(key(request_id))
        row = AccessRequest.model_validate(raw)
        client = client_for(bridge)
        await approvals.card(client, row.owner_channel, row.message_ts,
                             f"Mica access request {row.state}", blocks(row))
        if row.notification != "pending":
            return
        row.notification = "sending"
        await save(bridge, row)
        reply = (
            "The owner approved project access. Send your request again to start; "
            "existing write and compute approvals still apply."
            if row.state == "approved"
            else ("This access approval expired. Your request is saved; no permissions changed."
                  if row.state == "expired"
                  else "The owner declined this access request. Nothing was run.")
        )
        if row.state == "approved":
            current = await asyncio.to_thread(Registry(storage).load)
            permitted = current.can_use(row.approved_project, row.email)
            if not row.private:
                permitted = (
                    permitted
                    and current.bound(row.team, row.channel) == row.approved_project
                    and f"{row.team}:{row.channel}"
                    in current.projects[row.approved_project].published_channels
                )
            if not permitted:
                reply = (
                    "The owner reviewed this request, but access has since changed. "
                    "Please contact them."
                )
        sent = await client.chat_postMessage(
            channel=row.channel,
            thread_ts=row.thread,
            text=reply,
            client_msg_id=str(uuid5(NAMESPACE_URL, key(row.id) + f":{row.state}:{row.expires}")),
            blocks=approvals.expiry_blocks(KIND, row.id, reply) if row.state == "expired" else None,
            unfurl_links=False,
            unfurl_media=False,
        )
        row.notification, row.notification_ts = "sent", str(sent["ts"])
        await save(bridge, row)
    except Exception:
        log.warning("access_request_notification_pending id=%s", request_id)
        if retry:
            raise
    finally:
        await bridge.repository.release_event(key(request_id), claim.token)


async def _handle(
    bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: BackgroundTasks
) -> dict[str, Any]:
    team, user = payload.get("team", {}).get("id", ""), payload.get("user", {}).get("id", "")
    actor = f"{team}:{user}"
    again = payload.get("type") == "block_actions" and len(payload.get("actions", [])) == 1 and (
        payload["actions"][0].get("action_id") == PREFIX + "again"
    )
    if not bridge.settings.access_request_owner_id or (
        not again and actor != bridge.settings.access_request_owner_id
    ):
        raise PermissionError("Only the designated Mica owner can review access requests.")
    submission = payload.get("type") == "view_submission"
    if submission:
        metadata = json.loads(payload["view"].get("private_metadata", "{}"))
        if not isinstance(metadata, dict):
            raise ValueError("Invalid access approval context.")
        request_id = metadata.get("id", "")
        verb = "approve"
    else:
        action = payload["actions"][0]
        request_id, verb = str(action.get("value", "")), action["action_id"].removeprefix(PREFIX)
    if verb not in {"review", "deny", "approve", "again"}:
        raise ValueError("Unknown access action.")
    async with approvals.lease(bridge, key(request_id)):
        raw = await bridge.repository.get_connector_state(key(request_id))
        if not raw:
            raise ValueError("This access request is unavailable.")
        row = AccessRequest.model_validate(raw)
        requester_again = again and actor != row.owner
        if row.team != team or row.owner != bridge.settings.access_request_owner_id:
            raise PermissionError("This request belongs to another owner or workspace.")
        if requester_again:
            container = payload.get("container", {})
            if (user != row.user or payload.get("channel", {}).get("id") != row.channel
                    or payload.get("message", {}).get("ts") != row.notification_ts
                    or container.get("channel_id", row.channel) != row.channel
                    or container.get("message_ts", row.notification_ts) != row.notification_ts
                    or not row.notification_ts):
                raise PermissionError("Use the expired request card in your original conversation.")
            principal = await asyncio.to_thread(identity.resolve_email,
                bridge.app, bridge.settings, storage, team, user)
            if principal != row.email:
                raise PermissionError("Requester identity changed.")
        elif row.owner != actor:
            raise PermissionError("This request belongs to another owner or workspace.")
        if submission:
            if (
                not row.review
                or metadata.get("nonce") != row.review.get("nonce")
                or payload["view"].get("id") != row.review.get("view_id")
            ):
                raise PermissionError("This approval view is stale. Reopen the original card.")
        elif not requester_again:
            channel, ts = payload.get("channel", {}).get("id"), payload.get("message", {}).get("ts")
            container = payload.get("container", {})
            if (
                channel != row.owner_channel
                or ts != row.message_ts
                or not ts
                or container.get("channel_id", channel) != channel
                or container.get("message_ts", ts) != ts
            ):
                raise PermissionError("Use the original access request card in your DM.")
        if again:
            if row.state == "expired":
                row.state, row.expires, row.review = "pending", KIND.deadline(), {}
                row.notification, row.delivery = "pending", "refreshing"
                await save(bridge, row)
            elif row.state != "pending" or row.delivery != "refreshing":
                return {}
            await approvals.card(client_for(bridge), row.owner_channel, row.message_ts,
                                 "Mica access approval needed", blocks(row))
            if row.notification_ts:
                await approvals.card(client_for(bridge), row.channel, row.notification_ts,
                                     "A fresh owner review was requested. Nothing has been run.")
            await approvals.watch(bridge, KIND, row.id, row.expires)
            row.delivery = "sent"
            await save(bridge, row)
            return {}
        if row.state != "pending":
            background.add_task(notify_decision, bridge, storage, row.id)
            return {}
        if approvals.expired(row.expires):
            row.state, row.review, row.notification = "expired", {}, "pending"
            await save(bridge, row)
            background.add_task(notify_decision, bridge, storage, row.id)
            raise ValueError("This request expired. Use Request again for a fresh owner review.")
        if verb == "deny":
            row.state, row.decided_by, row.decided_at = "denied", actor, time.time()
        else:
            principal = await asyncio.to_thread(
                identity.resolve_email, bridge.app, bridge.settings, storage, team, user
            )
            requester = await asyncio.to_thread(
                identity.resolve_email, bridge.app, bridge.settings, storage, row.team, row.user
            )
            if not principal or requester != row.email:
                raise PermissionError("The owner or requester identity could not be verified.")
            registry = Registry(storage)
            doc = await asyncio.to_thread(registry.load)
            if verb == "review":
                choices = {
                    pid: p.model_dump(mode="json")
                    for pid, p in doc.projects.items()
                    if doc.owns(pid, principal)
                    and (not row.project or pid == row.project)
                    and (
                        row.private
                        or not doc.bound(row.team, row.channel)
                        or doc.bound(row.team, row.channel) == pid
                    )
                }
                if not choices:
                    raise ValueError(
                        "You must own the requested project to approve it. No access was changed."
                    )
                if len(choices) > 100:
                    raise ValueError(
                        "Use the existing share/publish commands to select the project."
                    )
                binding = doc.bindings.get(f"{row.team}:{row.channel}")
                row.review = {
                    "nonce": secrets.token_hex(16),
                    "principal": principal,
                    "projects": choices,
                    "binding": binding.model_dump(mode="json") if binding else None,
                }
                detail = (
                    f"Grant {row.email} membership of the selected project "
                    "and access to its data and shared memory."
                )
                if not row.private:
                    detail += (
                        f" Also publish and link it to channel {row.channel}: "
                        "everyone who can view that channel can read Mica's replies, "
                        "including future members."
                    )
                detail += (
                    " This grants no source-write permission, cloud IAM, or automatic execution."
                )
                await save(bridge, row)
                opened = await client_for(bridge).views_open(
                    trigger_id=payload["trigger_id"],
                    view={
                        "type": "modal",
                        "callback_id": CALLBACK,
                        "title": {"type": "plain_text", "text": "Approve project access"},
                        "submit": {"type": "plain_text", "text": "Approve access"},
                        "close": {"type": "plain_text", "text": "Cancel"},
                        "private_metadata": json.dumps(
                            {"id": row.id, "nonce": row.review["nonce"]}
                        ),
                        "blocks": [
                            {"type": "section", "text": {"type": "plain_text", "text": detail}},
                            {
                                "type": "input",
                                "block_id": "project",
                                "label": {"type": "plain_text", "text": "Project you own"},
                                "element": {
                                    "type": "static_select",
                                    "action_id": "project",
                                    "options": [
                                        {
                                            "text": {
                                                "type": "plain_text",
                                                "text": f"{p['name']} ({pid})"[:75],
                                            },
                                            "value": pid,
                                        }
                                        for pid, p in sorted(choices.items())
                                    ],
                                },
                            },
                        ],
                    },
                )
                row.review["view_id"] = opened["view"]["id"]
                await save(bridge, row)
                return {}
            selected = (payload["view"].get("state", {}).get("values", {})
                        .get("project", {}).get("project", {}).get("selected_option") or {}).get(
                            "value"
                        )
            if not isinstance(selected, str):
                raise ValueError("Select one project.")
            if principal != row.review["principal"] or selected not in row.review["projects"]:
                raise PermissionError("Choose a project from this approval's authorized list.")

            def change(current: RegistryDoc) -> str:
                binding = current.bindings.get(f"{row.team}:{row.channel}")
                if (
                    not current.owns(selected, principal)
                    or current.projects[selected]
                    != ProjectEntry.model_validate(row.review["projects"][selected])
                    or (binding.model_dump(mode="json") if binding else None)
                    != row.review["binding"]
                ):
                    raise ValueError(
                        "Project access or channel settings changed. "
                        "Reopen the card to review again."
                    )
                if not current.can_use(selected, row.email):
                    current.share(selected, principal, row.email)
                if not row.private:
                    current.publish(selected, principal, row.team, row.channel)
                    current.bind(row.team, row.channel, principal, selected, private=False)
                return selected

            await asyncio.to_thread(
                registry.update, key(row.id), principal, "approve " + selected, change
            )
            row.state, row.decided_by, row.decided_at = "approved", actor, time.time()
            row.approved_project = selected
        await save(bridge, row)
        background.add_task(notify_decision, bridge, storage, row.id)
        return {}



async def expire(bridge: BatchBridge, storage: Storage, request_id: str) -> None:
    async with approvals.lease(bridge, key(request_id)):
        raw = await bridge.repository.get_connector_state(key(request_id))
        if not raw:
            return
        row = AccessRequest.model_validate(raw)
        if row.state not in {"pending", "expired"} or not approvals.expired(row.expires):
            return
        if row.state == "pending":
            row.state, row.review, row.notification = "expired", {}, "pending"
            await save(bridge, row)
    await notify_decision(bridge, storage, request_id, retry=True)


KIND = approvals.Kind("access", (PREFIX,), key, 86400, _handle, expire, CALLBACK)


async def handle(
    bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: BackgroundTasks
) -> dict[str, Any]:
    return await approvals.handle(bridge, storage, payload, background)
