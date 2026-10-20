"""Control-plane Gmail outbox. Ambiguous sends are reconciled, never blindly retried."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
from email.message import EmailMessage
from email.policy import SMTP
from typing import TYPE_CHECKING, Any, Literal

from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

from mica.connectors import gmail_client

from . import access
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


async def deliver(
    bridge: BatchBridge,
    event_id: str,
    record: dict[str, Any],
    text: str,
    *,
    kind: Literal["ack", "final"],
    result: TerminalResult | None = None,
) -> str:
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
        response = await asyncio.to_thread(
            api.users()
            .messages()
            .list(
                userId="me",
                q=f"in:sent rfc822msgid:{message_id}",
                maxResults=2,
            )
            .execute
        )
        matches = response.get("messages", [])
        if len(matches) == 1:
            saved.update(status="sent", gmail_id=matches[0]["id"])
            await asyncio.to_thread(storage.put_if, key, json.dumps(saved).encode(), generation)
            return str(matches[0]["id"])
        raise RuntimeError(f"Email {kind} send is unconfirmed; reconcile outbox {event_id}")

    grant = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, storage, record)
    sender = record.get("intake_principal", "")
    if not sender or grant.email != sender:
        raise PermissionError("Email sender verification is unavailable")
    # Never deliver project content after revocation, including queued worker output.
    if kind == "final" and grant.project_id and not grant.allowed:
        text, result = (
            "Project access is unavailable. Ask the project owner for access in Slack.",
            None,
        )
    meta = record["payload"]["_email"]
    recipients = [sender]
    dropped = []
    if kind == "final" and grant.allowed:
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
    if current.signature != grant.signature:
        raise RuntimeError("Project membership changed while preparing email; retry")
    saved = {
        "status": "sending",
        "text": text,
        "artifacts": included,
        "message_id": message_id,
        "attempt": result.attempt if result else None,
        "sha256": hashlib.sha256(serialized).hexdigest(),
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
