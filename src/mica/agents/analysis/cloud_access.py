"""Configured cloud-access approvals, separate from analysis and project membership."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from mica.connectors.gmail_client import request_text

from . import approvals, channel_access, email_delivery, iam_grants, identity, lab, surface
from .cloud_access_config import CloudAccessBundle, CloudAccessProposal
from .storage import Storage

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

    from .batch import BatchBridge

PREFIX = "mica_cloud_access_"
CALLBACK = PREFIX + "submit"


class FormError(ValueError):
    def __init__(self, text: str, block_id: str) -> None:
        super().__init__(text)
        self.block_id = block_id


class Request(BaseModel):
    id: str
    bundle: str
    policy: CloudAccessBundle
    target: str
    requester: str
    note: str
    origin: Literal["slack", "email"]
    team: str = ""
    user: str = ""
    channel: str = ""
    thread: str = ""
    email_thread: str = ""
    email_message: str = ""
    email_subject: str = ""
    expires: float
    state: Literal[
        "checking", "pending", "approved", "granted", "grant_failed", "already", "denied", "expired"
    ] = "checking"
    decided_by: str = ""
    decided_at: float = 0
    reason: str = ""
    card_channel: str = ""
    card_ts: str = ""
    card_delivery: str = "pending"
    welcome: str = "pending"
    notification: str = "pending"
    notification_text: str = ""
    approval_mail: dict[str, dict[str, str]] = Field(default_factory=dict)
    requires_review: bool = False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def key(identifier: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid cloud access request.")
    return "cloud-access:" + identifier


def client(bridge: BatchBridge) -> AsyncWebClient:
    return AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5, retry_handlers=[])


async def save(bridge: BatchBridge, row: Request) -> None:
    await bridge.repository.set_connector_state(key(row.id), row.model_dump(mode="json"))


async def load(bridge: BatchBridge, identifier: str) -> Request:
    value = await bridge.repository.get_connector_state(key(identifier))
    if not value:
        raise ValueError("This access request is unavailable.")
    return Request.model_validate(value)


def configured(bridge: BatchBridge, name: str) -> CloudAccessBundle:
    bundle = bridge.settings.cloud_access.get(name)
    if bundle is None:
        raise ValueError("This cloud access bundle is not enabled.")
    return bundle


def unchanged(bridge: BatchBridge, row: Request) -> bool:
    return bridge.settings.cloud_access.get(row.bundle) == row.policy


def outcome(row: Request) -> str:
    title, target = row.policy.title, row.target
    if row.state == "already":
        return f"{target} already has {title}. No grant or welcome email was sent."
    if row.state == "granted":
        delivery = (
            "A welcome email was sent to them."
            if row.welcome == "sent"
            else (
                "The welcome email is unconfirmed and held for operator review; "
                "it will not be resent."
                if row.welcome == "unknown"
                else "Their welcome email is pending."
            )
        )
        return f"Granted {title} to {target}. {delivery}"
    if row.state == "grant_failed":
        return row.reason or "The access grant failed. Please contact the administrator."
    if row.state == "denied":
        return f"The approver declined {title} for {target}. No access was changed."
    if row.state == "expired":
        return (
            "This access request expired. No access was changed. Submit a new request to continue."
        )
    if row.state == "approved":
        return "Access was approved; the grant is being completed."
    return f"Sent {title} for {target} for approval. No access has been granted."


def blocks(row: Request) -> list[dict[str, Any]]:
    text = (
        f"<@{row.policy.approver.split(':')[1]}> · *{escape(row.policy.title)} approval*\n"
        f"Requester: {escape(row.requester)}\nTarget: {escape(row.target)}"
    )
    result: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "section", "text": {"type": "plain_text", "text": row.note}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": "Awaiting approval · expires after 7 days."
                    if row.state == "pending"
                    else outcome(row),
                }
            ],
        },
    ]
    held = [
        email for email, receipt in row.approval_mail.items() if receipt.get("status") == "unknown"
    ]
    if held:
        result.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "plain_text",
                        "text": "Approval email unconfirmed; held for operator review: "
                        + ", ".join(held),
                    }
                ],
            }
        )
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
                        **({"style": "primary"} if action == "approve" else {}),
                    }
                    for action, label in (("approve", "Approve"), ("deny", "Deny"))
                ],
            }
        )
    return result


async def approval_card(bridge: BatchBridge, row: Request) -> None:
    if row.card_ts:
        await approvals.card(
            client(bridge), row.card_channel, row.card_ts, outcome(row), blocks(row)
        )
        return
    if row.card_delivery != "pending":
        return  # An unconfirmed Slack send must not create another actionable card.
    row.card_delivery = "sending"
    await save(bridge, row)
    args: dict[str, Any] = dict(
        channel=row.channel if row.origin == "slack" else row.policy.approver.split(":")[1],
        text=f"{row.policy.title} approval needed",
        blocks=blocks(row),
        client_msg_id=str(uuid5(NAMESPACE_URL, key(row.id) + ":card")),
        unfurl_links=False,
        unfurl_media=False,
    )
    if row.origin == "slack":
        args["thread_ts"] = row.thread
    try:
        sent = await client(bridge).chat_postMessage(**args)
    except SlackApiError as exc:
        row.card_delivery = "pending" if exc.response.get("error") else "unknown"
        await save(bridge, row)
        raise
    row.card_channel, row.card_ts, row.card_delivery = str(sent["channel"]), str(sent["ts"]), "sent"
    if row.origin == "email" and not row.card_channel.startswith("D"):
        raise ValueError("Approval for email requests requires the approver's DM.")
    await save(bridge, row)


async def approval_emails(bridge: BatchBridge, row: Request) -> None:
    if row.origin != "email":
        return
    for email in row.policy.approver_emails:
        receipt = row.approval_mail.get(email, {})
        if receipt.get("status") == "sent":
            continue
        try:
            receipt = await email_delivery.notice(
                bridge,
                key(row.id) + ":approve:" + email,
                email,
                f"Approve {row.policy.title} request [{row.id[:8]}]",
                f"{row.requester} requests {row.policy.title} for {row.target}.\n\n"
                f"Note: {row.note}\n\nReply with approve or deny as your first line. "
                "This request expires after 7 days. Nothing has been granted yet.",
            )
        except email_delivery.UnconfirmedDelivery:
            row.approval_mail[email] = {"status": "unknown"}
        else:
            if not receipt["thread_id"]:
                row.approval_mail[email] = {"status": "unknown"}
            else:
                await bridge.repository.set_connector_state(
                    "cloud-access-email:" + receipt["thread_id"], {"id": row.id}
                )
                row.approval_mail[email] = {**receipt, "status": "sent"}
        await save(bridge, row)


async def notify(bridge: BatchBridge, row: Request) -> None:
    if row.card_ts:
        await approval_card(bridge, row)
    if row.notification != "pending":
        return
    if not row.notification_text:
        row.notification_text = outcome(row)
        await save(bridge, row)
    if row.origin == "email":
        if not row.requires_review:
            return  # Immediate outcomes use the request's normal final email outbox.
        try:
            await email_delivery.notice(
                bridge,
                key(row.id) + ":decision",
                row.requester,
                row.email_subject,
                row.notification_text,
                thread_id=row.email_thread,
                in_reply_to=row.email_message,
            )
        except email_delivery.UnconfirmedDelivery:
            row.notification = "unknown"
        else:
            row.notification = "sent"
    else:
        row.notification = "sending"
        await save(bridge, row)
        try:
            await client(bridge).chat_postMessage(
                channel=row.channel,
                thread_ts=row.thread,
                text=row.notification_text,
                client_msg_id=str(uuid5(NAMESPACE_URL, key(row.id) + ":outcome")),
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackApiError as exc:
            row.notification = "pending" if exc.response.get("error") else "unknown"
            await save(bridge, row)
            raise
        row.notification = "sent"
    await save(bridge, row)


async def advance(bridge: BatchBridge, storage: Storage, row: Request) -> None:
    """Called under the request lease. A durable timer also resumes interrupted side effects."""
    if row.state in {"checking", "pending", "approved"} and not unchanged(bridge, row):
        row.state, row.reason = (
            "grant_failed",
            "This request cannot continue because the configured access bundle changed. "
            "Check existing access before submitting a new request.",
        )
        await save(bridge, row)
    if row.state in {"checking", "pending"} and approvals.expired(row.expires):
        row.state = "expired"
        await save(bridge, row)
    if row.state == "checking":
        try:
            missing = await asyncio.to_thread(
                iam_grants.missing_roles,
                row.policy.gcp_project,
                row.target,
                row.policy.roles,
                settings=bridge.settings,
            )
        except iam_grants.GrantFailed as exc:
            row.state, row.reason = "grant_failed", str(exc)
        else:
            if not missing:
                row.state = "already"
            else:
                members = await asyncio.to_thread(lab.load, storage)
                automatic = row.policy.allow_lab_members and row.target in members.members
                if row.origin == "email" and row.requester in row.policy.approver_emails:
                    automatic = False  # The approver explicitly confirms an extracted target.
                row.state = "approved" if automatic else "pending"
                row.requires_review = not automatic
                if automatic:
                    row.decided_by, row.decided_at = "lab_members", time.time()
        await save(bridge, row)
    if (
        row.state == "approved"
        and row.decided_by == "lab_members"
        and row.target not in (await asyncio.to_thread(lab.load, storage)).members
    ):
        row.state, row.decided_by = "pending", ""
        row.requires_review = True
        await save(bridge, row)
    if row.state == "pending":
        await approvals.watch(bridge, KIND, row.id, row.expires)
        await approval_card(bridge, row)
        await approval_emails(bridge, row)
        if row.card_ts:
            await approval_card(bridge, row)
        return
    if row.state == "approved":
        try:
            await asyncio.to_thread(
                iam_grants.grant,
                row.policy.gcp_project,
                row.target,
                row.policy.roles,
                settings=bridge.settings,
            )
        except iam_grants.GrantFailed as exc:
            row.state, row.reason = "grant_failed", str(exc)
        else:
            row.state = "granted"
        await save(bridge, row)
    if row.state == "granted" and row.welcome != "sent":
        try:
            await email_delivery.notice(
                bridge,
                key(row.id) + ":welcome",
                row.target,
                f"Your {row.policy.title}",
                row.policy.welcome.replace("{guide_url}", row.policy.guide_url),
            )
        except email_delivery.UnconfirmedDelivery:
            row.welcome = "unknown"
        else:
            row.welcome = "sent"
        await save(bridge, row)
    # 'Already' has no welcome or decision email, only the normal request reply.
    if row.state == "already" and row.origin == "email":
        return
    await notify(bridge, row)


async def resume(bridge: BatchBridge, storage: Storage, identifier: str) -> Request:
    async with approvals.lease(bridge, key(identifier), seconds=300, busy_error=RuntimeError):
        row = await load(bridge, identifier)
        await advance(bridge, storage, row)
        return row


async def request(
    bridge: BatchBridge,
    proposal: CloudAccessProposal,
    source: str,
    *,
    requester: str,
    origin: Literal["slack", "email"],
    team: str = "",
    user: str = "",
    channel: str = "",
    thread: str = "",
    email_meta: dict[str, Any] | None = None,
) -> Request:
    policy = configured(bridge, proposal.bundle)
    identifier = digest(source)
    source_key = "cloud-access-source:" + identifier
    source_details = {"bundle": proposal.bundle, "target": proposal.email, "requester": requester}
    index = "cloud-access-pending:" + digest(proposal.bundle + ":" + proposal.email)
    async with approvals.lease(bridge, index, seconds=60, busy_error=RuntimeError):
        alias = await bridge.repository.get_connector_state(source_key)
        if alias:
            if alias["details"] != source_details:
                raise ValueError("This request was already submitted with different details.")
            row = await load(bridge, alias["id"])
            await approvals.watch(bridge, KIND, row.id, time.time() + 30)
            return row
        prior = await bridge.repository.get_connector_state(key(identifier))
        if prior:
            row = Request.model_validate(prior)
            if (row.bundle, row.target, row.requester) != (
                proposal.bundle,
                proposal.email,
                requester,
            ):
                raise ValueError("This request was already submitted with different details.")
            await approvals.watch(bridge, KIND, row.id, time.time() + 30)
            return row
        reservation = await bridge.repository.get_connector_state(index)
        if reservation:
            prior = await bridge.repository.get_connector_state(key(reservation["id"]))
            row = Request.model_validate(prior or reservation["request"])
            if not prior:
                await save(bridge, row)  # Recover a crash after reserving the target.
            if row.state == "approved" or (
                row.state in {"checking", "pending"} and not approvals.expired(row.expires)
            ):
                await bridge.repository.set_connector_state(
                    source_key, {"id": row.id, "details": source_details}
                )
                await approvals.watch(bridge, KIND, row.id, time.time() + 30)
                return row
        meta = email_meta or {}
        row = Request(
            id=identifier,
            bundle=proposal.bundle,
            policy=policy,
            target=proposal.email,
            requester=identity.recipient_email(requester),
            note=proposal.note,
            origin=origin,
            team=team,
            user=user,
            channel=channel,
            thread=thread,
            expires=KIND.deadline(),
            email_thread=str(meta.get("thread_id", "")),
            email_message=str(meta.get("message_id", "")),
            email_subject=str(meta.get("subject") or "Cloud access request")[:180],
        )
        # Persist the recoverable reservation before the request, avoiding a second pending row
        # if the process dies between writes. No IAM/email side effects run under this lock.
        await approvals.watch(bridge, KIND, row.id, time.time() + 30)
        await bridge.repository.set_connector_state(
            index, {"id": row.id, "request": row.model_dump(mode="json")}
        )
        await save(bridge, row)
    return row


async def receive(bridge: BatchBridge, payload: dict[str, Any]) -> bool:
    event = payload["event"]
    scope = f"{payload.get('team_id', '')}:{event['channel']}"
    name = next((k for k, b in bridge.settings.cloud_access.items() if b.channel == scope), None)
    if name is None:
        return False
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        return True
    identifier = digest(scope + ":" + event["ts"])
    prompt_key = "cloud-access-prompt:" + identifier
    async with approvals.lease(bridge, prompt_key, busy_error=RuntimeError):
        row = await bridge.repository.get_connector_state(prompt_key)
        if row and row.get("delivery") != "failed":
            return True
        row = dict(
            bundle=name,
            team=payload["team_id"],
            channel=event["channel"],
            thread=event["ts"],
            delivery="sending",
        )
        await bridge.repository.set_connector_state(prompt_key, row)
        text = "Request " + configured(bridge, name).title
        try:
            sent = await client(bridge).chat_postMessage(
                channel=event["channel"],
                thread_ts=event["ts"],
                text=text,
                blocks=[
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": text[:75]},
                                "action_id": PREFIX + "request",
                                "value": identifier,
                            }
                        ],
                    }
                ],
                client_msg_id=str(uuid5(NAMESPACE_URL, prompt_key)),
            )
        except SlackApiError as exc:
            if exc.response.get("error"):
                row["delivery"] = "failed"
                await bridge.repository.set_connector_state(prompt_key, row)
            raise
        row.update(delivery="sent", message_ts=str(sent["ts"]))
        await bridge.repository.set_connector_state(prompt_key, row)
    return True


async def open_form(bridge: BatchBridge, payload: dict[str, Any], identifier: str) -> None:
    key(identifier)
    prompt = await bridge.repository.get_connector_state("cloud-access-prompt:" + identifier)
    team, user = payload.get("team", {}).get("id", ""), payload.get("user", {}).get("id", "")
    if not prompt or (
        team,
        payload.get("channel", {}).get("id"),
        payload.get("message", {}).get("ts"),
    ) != (prompt["team"], prompt["channel"], prompt.get("message_ts")):
        raise PermissionError("Use the original request button in its configured channel.")
    bundle = configured(bridge, prompt["bundle"])
    if bundle.channel != team + ":" + prompt["channel"]:
        raise PermissionError("This request channel changed.")
    email = await asyncio.to_thread(channel_access.profile, bridge.app, bridge.settings, team, user)
    if not email:
        raise PermissionError("An active workspace account with a readable email is required.")
    nonce = secrets.token_hex(16)
    form = {**prompt, "team": team, "user": user, "email": email, "expires": time.time() + 1800}
    await bridge.repository.set_connector_state("cloud-access-form:" + nonce, form)
    result = await client(bridge).views_open(
        trigger_id=payload["trigger_id"],
        view={
            "type": "modal",
            "callback_id": CALLBACK,
            "private_metadata": nonce,
            "title": {"type": "plain_text", "text": bundle.title[:24]},
            "submit": {"type": "plain_text", "text": "Request access"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "email",
                    "label": {"type": "plain_text", "text": "Email receiving access"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value",
                        "initial_value": email,
                        "max_length": 254,
                    },
                },
                {
                    "type": "input",
                    "block_id": "note",
                    "label": {"type": "plain_text", "text": "Why is access needed?"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value",
                        "multiline": True,
                        "max_length": 500,
                    },
                },
            ],
        },
    )
    form["view_id"] = result["view"]["id"]
    await bridge.repository.set_connector_state("cloud-access-form:" + nonce, form)


async def decide(
    bridge: BatchBridge, storage: Storage, row: Request, actor: str, approved: bool
) -> None:
    if not unchanged(bridge, row):
        raise ValueError("This access bundle changed. Submit a new request.")
    if row.state != "pending":
        if row.state == "expired" or approvals.expired(row.expires):
            raise ValueError("This approval expired. Submit a new request.")
        return  # Duplicate authorized decisions do not reverse an earlier decision.
    if approvals.expired(row.expires):
        raise ValueError("This approval expired. Submit a new request.")
    await approvals.watch(bridge, KIND, row.id, time.time() + 30)
    row.state = "approved" if approved else "denied"
    row.decided_by, row.decided_at = actor, time.time()
    await save(bridge, row)


async def handle(
    bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: BackgroundTasks
) -> dict[str, Any]:
    team, user = payload.get("team", {}).get("id", ""), payload.get("user", {}).get("id", "")
    if payload.get("type") == "view_submission":
        nonce = str(payload.get("view", {}).get("private_metadata", ""))
        key(nonce)
        form = await bridge.repository.get_connector_state("cloud-access-form:" + nonce)
        if (
            not form
            or (team, user, payload["view"].get("id"))
            != (form["team"], form["user"], form.get("view_id"))
            or approvals.expired(form["expires"])
        ):
            raise PermissionError("Reopen the request form from its original button.")
        if configured(bridge, form["bundle"]).channel != team + ":" + form["channel"]:
            raise PermissionError("The configured request channel changed.")
        email = await asyncio.to_thread(
            channel_access.profile, bridge.app, bridge.settings, team, user
        )
        if email != form["email"]:
            raise PermissionError("The requester's workspace identity changed.")
        values = payload["view"].get("state", {}).get("values", {})
        target = values.get("email", {}).get("value", {}).get("value", "")
        note = values.get("note", {}).get("value", {}).get("value", "")
        try:
            target = identity.recipient_email(target)
        except ValueError as exc:
            raise FormError(str(exc), "email") from None
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
            raise FormError("Add a short note of 1–500 characters.", "note")
        proposal = CloudAccessProposal(bundle=form["bundle"], email=target, note=note)
        row = await request(
            bridge,
            proposal,
            "form:" + nonce,
            requester=email,
            origin="slack",
            team=team,
            user=user,
            channel=form["channel"],
            thread=form["thread"],
        )
        background.add_task(resume, bridge, storage, row.id)
        return {}
    action = payload["actions"][0]
    identifier = str(action.get("value", ""))
    if action["action_id"] == PREFIX + "request":
        await open_form(bridge, payload, identifier)
        return {}
    if action["action_id"] not in {PREFIX + "approve", PREFIX + "deny"}:
        raise ValueError("Unknown cloud access action.")
    async with approvals.lease(bridge, key(identifier), seconds=300):
        row = await load(bridge, identifier)
        if f"{team}:{user}" != row.policy.approver:
            raise PermissionError("Only the configured approver can decide this request.")
        if (
            payload.get("channel", {}).get("id") != row.card_channel
            or not row.card_ts
            or payload.get("message", {}).get("ts") != row.card_ts
            or payload.get("container", {}).get("channel_id", row.card_channel) != row.card_channel
            or payload.get("container", {}).get("message_ts", row.card_ts) != row.card_ts
        ):
            raise PermissionError("Decide from the original approval card.")
        principal = await asyncio.to_thread(
            channel_access.profile, bridge.app, bridge.settings, team, user
        )
        if not principal:
            raise PermissionError("The approver's workspace identity is unavailable.")
        await decide(
            bridge, storage, row, f"{team}:{user}", action["action_id"] == PREFIX + "approve"
        )
    background.add_task(resume, bridge, storage, row.id)
    return {}


async def propose(
    bridge: BatchBridge,
    storage: Storage,
    record: dict[str, Any],
    event_id: str,
    proposal: CloudAccessProposal,
) -> str:
    bundle = configured(bridge, proposal.bundle)
    if not surface.is_email(record):
        team, channel = bundle.channel.split(":")
        return f"Request {bundle.title} with the form in <#{channel}> (slack://channel?team={team}&id={channel})."
    sender = str(record.get("intake_principal", ""))
    if not surface.valid_email_record(record, sender):
        raise PermissionError("A verified email sender is required.")
    text = record["payload"]["event"].get("text", "")
    original = record["payload"]["_email"].get("original_text", text)
    if proposal.email != sender and not re.search(
        r"(?<![\w@.+-])" + re.escape(proposal.email) + r"(?![\w@.+-])", original, re.IGNORECASE
    ):
        return "Which email should receive access? Include the full address in your message."
    row = await request(
        bridge,
        proposal,
        "email:" + event_id,
        requester=sender,
        origin="email",
        email_meta=record["payload"]["_email"],
    )
    row = await resume(bridge, storage, row.id)
    return outcome(row)


async def email_decision(
    bridge: BatchBridge, storage: Storage, record: dict[str, Any]
) -> str | None:
    if not bridge.settings.cloud_access or not surface.is_email(record):
        return None
    sender = str(record.get("intake_principal", ""))
    if not surface.valid_email_record(record, sender):
        return None
    thread = record["payload"]["_email"]["thread_id"]
    index = await bridge.repository.get_connector_state("cloud-access-email:" + thread)
    if not index:
        return None
    first = next(
        (
            line.strip()
            for line in request_text(record["payload"]["event"].get("text", "")).splitlines()
            if line.strip()
        ),
        "",
    )
    if first not in {"approve", "deny"}:
        return None
    async with approvals.lease(bridge, key(index["id"]), seconds=300):
        row = await load(bridge, index["id"])
        if sender not in row.policy.approver_emails or not unchanged(bridge, row):
            return None
        if row.approval_mail.get(sender, {}).get("thread_id") != thread:
            return None
        await decide(bridge, storage, row, sender, first == "approve")
    return outcome(await resume(bridge, storage, row.id))


async def expire(bridge: BatchBridge, storage: Storage, identifier: str) -> None:
    await resume(bridge, storage, identifier)


KIND = approvals.Kind(
    "cloud_access", (PREFIX,), key, 7 * 86400, handle, expire, CALLBACK, error_block="email"
)
