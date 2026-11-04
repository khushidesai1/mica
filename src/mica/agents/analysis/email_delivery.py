"""Control-plane Gmail outbox. Ambiguous sends are reconciled, never blindly retried."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
import re
import uuid
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from mica.connectors import gmail_client

from . import access, command_intent
from .config import CHECKPOINT_PREFIX
from .storage import Conflict
from .surface import EMAIL_STATE_PREFIX

if TYPE_CHECKING:
    from .batch import BatchBridge

MAX_FILE_BYTES = 17_000_000
MAX_MIME_BYTES = 24_000_000


class Artifact(BaseModel):
    key: str = Field(max_length=512)
    name: str = Field(min_length=1, max_length=200)
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class TerminalResult(BaseModel):
    attempt: int = Field(ge=0)
    text: str = Field(max_length=200_000)
    outcome: Literal["complete", "failed"] = "complete"
    artifacts: list[Artifact] = Field(default_factory=list, max_length=100)


def artifact_prefix(event_id: str, attempt: int) -> str:
    return f"{CHECKPOINT_PREFIX}/events/{event_id}/email/a{attempt}/"


def content_fingerprint(message: EmailMessage) -> str:
    """Compare content independently of Gmail's transport header rewrites."""
    body = message.get_body(preferencelist=("plain",))
    attachments = []
    for part in message.iter_attachments():
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            raise ValueError("Email attachment has no decodable content")
        attachments.append((part.get_filename(), hashlib.sha256(data).hexdigest()))
    content = {
        "headers": {
            name: str(message.get(name, ""))
            for name in ("From", "To", "Subject", "In-Reply-To", "X-Mica-Delivery-ID")
        },
        "text": str(body.get_content()).replace("\r\n", "\n") if body else "",
        "attachments": attachments,
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def sent_match(api: Any, saved: dict[str, Any], thread_id: str) -> str | None:
    """Find a send receipt even when Gmail replaced our RFC822 Message-ID."""
    if saved.get("delivery_id"):
        thread = (
            api.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["X-Mica-Delivery-ID"],
                fields="messages(id,labelIds,payload/headers)",
            )
            .execute()
        )
        if len(thread.get("messages", [])) > 1000:
            return None
        matches = [
            message["id"]
            for message in thread.get("messages", [])
            if "SENT" in message.get("labelIds", [])
            and any(
                header.get("name", "").lower() == "x-mica-delivery-id"
                and header.get("value") == saved["delivery_id"]
                for header in message.get("payload", {}).get("headers", [])
            )
        ]
        if len(matches) != 1:
            return None
        candidate = api.users().messages().get(userId="me", id=matches[0], format="raw").execute()
        encoded = candidate.get("raw", "")
        if (
            "SENT" not in candidate.get("labelIds", [])
            or len(encoded) > (MAX_MIME_BYTES + 64_000) * 4 // 3 + 4
        ):
            return None
        parsed = BytesParser(policy=SMTP).parsebytes(base64.urlsafe_b64decode(encoded + "==="))
        if not isinstance(parsed, EmailMessage) or content_fingerprint(parsed) != saved.get(
            "content_sha256"
        ):
            return None
    else:
        # Preserve compatibility for older outbox operations. A rewritten legacy ID
        # cannot be reconciled automatically; it must remain held for operator review.
        response = (
            api.users()
            .messages()
            .list(userId="me", q=f"in:sent rfc822msgid:{saved['message_id']}", maxResults=2)
            .execute()
        )
        matches = [message["id"] for message in response.get("messages", [])]
    return str(matches[0]) if len(matches) == 1 else None


async def _deliver(
    bridge: BatchBridge,
    event_id: str,
    record: dict[str, Any],
    text: str,
    *,
    kind: str,
    result: TerminalResult | None = None,
) -> str:
    from googleapiclient.errors import HttpError

    if kind not in {"ack", "final"} and not re.fullmatch(r"atomx-[a-f0-9]{24}", kind):
        raise ValueError("Invalid email delivery stage")
    storage = bridge.storage
    key = f"{EMAIL_STATE_PREFIX}/outbox/{event_id}/{kind}.json"
    message_id = f"<mica.{event_id}.{kind}@{gmail_client.mailbox(bridge.app).split('@')[1]}>"
    api = await asyncio.to_thread(gmail_client.service, bridge.app)
    try:
        raw, generation = await asyncio.to_thread(storage.get, key)
        saved = json.loads(raw)
    except FileNotFoundError:
        saved, generation = {}, None
    if saved.get("status") in {"sent", "suppressed"}:
        return str(saved.get("gmail_id") or "suppressed")
    if saved:
        # A crash/timeout can happen after Gmail accepts a message. Never send again merely
        # because search has not indexed it yet. An unresolved operation needs operator review.
        matched = await asyncio.to_thread(
            sent_match, api, saved, record["payload"]["_email"]["thread_id"]
        )
        if matched:
            saved.update(status="sent", gmail_id=matched)
            await asyncio.to_thread(storage.put_if, key, json.dumps(saved).encode(), generation)
            return matched
        raise UnconfirmedDelivery(f"Email {kind} send is unconfirmed; reconcile outbox {event_id}")

    grant = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, storage, record)
    sender = record.get("intake_principal", "")
    if not sender or grant.email != sender:
        raise PermissionError("Email sender verification is unavailable")
    # Audience is server-owned event metadata; model/worker replies cannot widen it.
    account_reply = record.get("reply_scope") == "account"
    audience = record.get("reply_audience", "project_participants")
    if account_reply:
        audience = "sender_only"
        if record.get("account_visibility") != command_intent.visibility(grant):
            # Rebuild a read-only list from current visibility; never replay a mutation here.
            intent = record.get("direct_decision", {}).get("command_proposal") or {}
            exact = record["payload"]["event"].get("text", "").strip().lower()
            if intent.get("operation") == "projects" or exact in {"projects", "switch"}:
                from . import commands
                from .registry import Registry

                text = (
                    commands.run(
                        commands.Command(
                            "projects", (intent["selector"],) if intent.get("selector") else ()
                        ),
                        grant,
                        Registry(storage),
                        bridge.app,
                        record,
                        event_id,
                        bridge.settings,
                    )
                    or "No projects available."
                )
            else:
                text = "Your project access changed while preparing this reply. Please ask again."
            result = None
    # Never deliver project content after revocation, including queued worker output.
    if kind == "final" and not account_reply and grant.project_id and not grant.allowed:
        text, result = (
            "Project access is unavailable. Ask the project owner for access in Slack.",
            None,
        )
    meta = record["payload"]["_email"]
    recipients = [sender]
    dropped = []
    if kind == "final" and audience == "project_participants" and grant.allowed:
        for email in dict.fromkeys(meta.get("recipients", [])):
            if email == sender or gmail_client.is_own(email, bridge.app):
                continue
            if grant.doc.can_use(grant.project_id, email):
                recipients.append(email)
            else:
                dropped.append(email)
    if dropped:
        text += "\n\nLeft off this reply because they are not project members: " + ", ".join(
            dropped
        )
    message = EmailMessage(policy=SMTP)
    message["From"] = bridge.app.gmail_account
    message["To"] = ", ".join(recipients)
    message["Subject"] = meta["subject"]
    message["Message-ID"] = message_id
    delivery_id = uuid.uuid4().hex
    message["X-Mica-Delivery-ID"] = delivery_id
    message["In-Reply-To"] = meta["message_id"]
    message["References"] = meta["message_id"]
    message.set_content(text)
    omitted = []
    included = []
    if result:
        prefix = artifact_prefix(event_id, result.attempt)
        for artifact in result.artifacts:
            if artifact.key != prefix + artifact.sha256:
                raise PermissionError("Artifact belongs to a different event or attempt")
            if len(message.as_bytes()) + (artifact.size + 2) // 3 * 4 + 4096 > MAX_MIME_BYTES:
                omitted.append(artifact.name)
                continue
            data = await asyncio.to_thread(storage.read_prefix, artifact.key, artifact.size + 1)
            if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise ValueError("Email artifact content differs from its manifest")
            media = mimetypes.guess_type(artifact.name)[0] or "application/octet-stream"
            main, sub = media.split("/", 1)
            candidate = copy.deepcopy(message)
            candidate.add_attachment(data, maintype=main, subtype=sub, filename=artifact.name)
            if len(candidate.as_bytes()) + 32_000 > MAX_MIME_BYTES:
                omitted.append(artifact.name)
            else:
                message = candidate
                included.append(artifact.name)
    if omitted:
        text += (
            "\n\nCould not attach "
            + ", ".join(omitted)
            + ": the email size limit was reached. Ask in this email thread for smaller files."
        )
        body = message.get_body(preferencelist=("plain",))
        assert body is not None
        body.set_content(text)
    serialized = message.as_bytes()
    if len(serialized) > MAX_MIME_BYTES:
        raise ValueError("Final MIME message exceeds the email budget")
    # Recheck immediately before send intent; no project data is sent after known revocation.
    current = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, storage, record)
    if (
        current.email != grant.email
        or (
            account_reply and command_intent.visibility(current) != command_intent.visibility(grant)
        )
        or (not account_reply and current.signature != grant.signature)
    ):
        raise RuntimeError("Project membership changed while preparing email; retry")
    saved = {
        "status": "sending",
        "audience": audience,
        "scope": "account" if account_reply else "project",
        "text": "Private AtoMx action (link redacted)" if kind.startswith("atomx-") else text,
        "artifacts": included,
        "message_id": message_id,
        "delivery_id": delivery_id,
        "attempt": result.attempt if result else None,
        "sha256": hashlib.sha256(serialized).hexdigest(),
        "content_sha256": content_fingerprint(message),
    }
    try:
        generation = await asyncio.to_thread(storage.put_if, key, json.dumps(saved).encode(), None)
    except Conflict:
        raise RuntimeError("Another delivery owns this outbox operation; retry") from None
    try:
        response = await asyncio.to_thread(
            api.users()
            .messages()
            .send(
                userId="me",
                body={
                    "raw": base64.urlsafe_b64encode(serialized).decode(),
                    "threadId": meta["thread_id"],
                },
            )
            .execute
        )
    except HttpError as exc:
        # These explicit rejections confirm that Gmail did not accept the send.
        if exc.resp.status in {400, 401, 403, 404, 413, 429}:
            await asyncio.to_thread(storage.delete_if, key, generation)
        raise
    saved.update(status="sent", gmail_id=response["id"])
    await asyncio.to_thread(storage.put_if, key, json.dumps(saved).encode(), generation)
    return str(response["id"])


class UnconfirmedDelivery(RuntimeError):
    """The provider may have accepted the send; inspect the existing outbox."""


async def deliver(
    bridge: BatchBridge,
    event_id: str,
    record: dict[str, Any],
    text: str,
    *,
    kind: str,
    result: TerminalResult | None = None,
) -> str:
    from . import observability

    try:
        return await _deliver(bridge, event_id, record, text, kind=kind, result=result)
    except PermissionError:
        raise  # Expected revocation is not an operational incident.
    except Exception as exc:
        await observability.report(
            bridge,
            "email_delivery_unconfirmed"
            if isinstance(exc, UnconfirmedDelivery)
            else "email_delivery_failed",
            event_id,
            record,
            unit=kind,
        )
        raise
