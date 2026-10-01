"""Gmail History/Pub/Sub adapter with explicit project-thread binding."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from email.message import EmailMessage
from email.utils import parseaddr
from io import BytesIO
from typing import Annotated, Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from google.auth import default
from googleapiclient.discovery import build
from markupsafe import escape
from starlette.datastructures import Headers

from pistachio.api.auth import Principal, get_principal
from pistachio.api.dependencies import Services
from pistachio.models import TaskEnvelope, TaskSource

router = APIRouter(prefix="/integrations/gmail", tags=["integrations"])
PROJECT_SUBJECT = re.compile(r"\[project:([a-z0-9-]+)\]", re.I)


def _gmail_service(scopes: list[str]) -> Any:
    credentials, _ = default(scopes=scopes)  # type: ignore[no-untyped-call]
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _headers(message: dict[str, object]) -> dict[str, str]:
    payload = message.get("payload", {})
    values = payload.get("headers", []) if isinstance(payload, dict) else []
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in values
        if isinstance(item, dict)
    }


def _plain_text(payload: dict[str, Any]) -> str:
    mime = str(payload.get("mimeType", ""))
    body = payload.get("body", {})
    if mime == "text/plain" and isinstance(body, dict) and body.get("data"):
        return base64.urlsafe_b64decode(str(body["data"]) + "===").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) if isinstance(payload.get("parts"), list) else []:
        if isinstance(part, dict):
            text = _plain_text(part)
            if text:
                return text
    return ""


def _attachment_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if payload.get("filename"):
        found.append(payload)
    for part in payload.get("parts", []) if isinstance(payload.get("parts"), list) else []:
        if isinstance(part, dict):
            found.extend(_attachment_parts(part))
    return found


def _signature(secret: str, principal: str, thread: str, project: str, expiry: int) -> str:
    value = f"{principal}\n{thread}\n{project}\n{expiry}".encode()
    return hmac.new(secret.encode(), value, hashlib.sha256).hexdigest()


async def _send_email(
    services: Services, recipient: str, subject: str, text: str, thread_id: str = ""
) -> None:
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = services.settings.gmail_account
    message["Subject"] = subject
    message.set_content(text)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    def send() -> None:
        api = _gmail_service(["https://www.googleapis.com/auth/gmail.send"])
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        api.users().messages().send(userId="me", body=body).execute()

    await asyncio.to_thread(send)


async def _selection_email(services: Services, principal: str, thread_id: str) -> None:
    projects = await services.repository.list_projects(principal)
    expiry = int(time.time()) + 3600
    links = []
    for project in projects:
        params = {
            "principal": principal,
            "thread": thread_id,
            "project": project.id,
            "expiry": expiry,
            "signature": _signature(
                services.settings.signed_link_secret,
                principal,
                thread_id,
                project.id,
                expiry,
            ),
        }
        links.append(
            f"- {project.name}: {services.settings.control_plane_url.rstrip('/')}"
            f"/integrations/gmail/select?{urlencode(params)}"
        )
    text = "Choose a project for this email thread:\n" + (
        "\n".join(links) if links else "No authorized projects are available."
    )
    await _send_email(services, principal, "Choose a Pistachio project", text, thread_id)


async def _ingest_message(services: Services, api: Any, message_id: str) -> None:
    message = await asyncio.to_thread(
        api.users().messages().get(userId="me", id=message_id, format="full").execute
    )
    headers = _headers(message)
    principal = parseaddr(headers.get("from", ""))[1].lower()
    if not principal or principal == services.settings.gmail_account.lower():
        return
    thread_id = str(message.get("threadId", ""))
    binding_key = f"gmail:{thread_id}"
    project_id = await services.repository.get_channel_project(binding_key)
    if not project_id:
        match = PROJECT_SUBJECT.search(headers.get("subject", ""))
        if match:
            project = await services.repository.get_project_by_slug(match.group(1).lower())
            if project and project.can_access(principal):
                project_id = project.id
                await services.repository.set_channel_project(binding_key, project.id)
    if not project_id:
        await _selection_email(services, principal, thread_id)
        return
    project = await services.repository.get_project(project_id)
    if project is None or not project.can_access(principal):
        await _selection_email(services, principal, thread_id)
        return
    prompt = _plain_text(message.get("payload", {})).strip()
    if not prompt:
        return
    attachments = []
    for part in _attachment_parts(message.get("payload", {})):
        body = part.get("body", {})
        encoded = body.get("data", "") if isinstance(body, dict) else ""
        attachment_id = body.get("attachmentId", "") if isinstance(body, dict) else ""
        if not encoded and attachment_id:
            attachment = await asyncio.to_thread(
                api.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute
            )
            encoded = attachment.get("data", "")
        if not encoded:
            continue
        data = base64.urlsafe_b64decode(str(encoded) + "===")
        upload = UploadFile(
            file=BytesIO(data),
            filename=str(part.get("filename", "email-upload.bin")),
            headers=Headers(
                {"content-type": str(part.get("mimeType", "application/octet-stream"))}
            ),
        )
        attachments.append(await services.uploads.accept(upload, principal))
    task, created = await services.tasks.create(
        TaskEnvelope(
            project_id=project.id,
            source=TaskSource.EMAIL,
            external_event_id=f"gmail:{message_id}",
            conversation_id=f"gmail:{thread_id}",
            principal_id=principal,
            prompt=prompt,
            attachments=attachments,
        )
    )
    if created:
        await services.dispatcher.enqueue(task.id)
    await _send_email(
        services,
        principal,
        f"Re: {headers.get('subject', 'Pistachio task')}",
        f"I’m on it — I’ve started {task.public_name}. I’ll keep you posted in this thread.",
        thread_id,
    )


@router.post("/push")
async def gmail_push(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, bool]:
    services: Services = request.app.state.services
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(status_code=403, detail="untrusted Pub/Sub principal")
    envelope = await request.json()
    encoded = str(envelope.get("message", {}).get("data", ""))
    notice = json.loads(base64.b64decode(encoded).decode())
    new_history_id = str(notice["historyId"])
    state = await services.repository.get_connector_state("gmail-history") or {}
    old_history_id = str(state.get("history_id", ""))
    if not old_history_id:
        await services.repository.set_connector_state(
            "gmail-history", {"history_id": new_history_id}
        )
        return {"ok": True}

    def fetch() -> tuple[object, list[str]]:
        api = _gmail_service(["https://www.googleapis.com/auth/gmail.readonly"])
        ids: list[str] = []
        request_call = (
            api.users()
            .history()
            .list(userId="me", startHistoryId=old_history_id, historyTypes=["messageAdded"])
        )
        while request_call is not None:
            response = request_call.execute()
            for entry in response.get("history", []):
                for added in entry.get("messagesAdded", []):
                    ids.append(str(added["message"]["id"]))
            request_call = api.users().history().list_next(request_call, response)
        return api, list(dict.fromkeys(ids))

    api, message_ids = await asyncio.to_thread(fetch)
    for message_id in message_ids:
        await _ingest_message(services, api, message_id)
    await services.repository.set_connector_state("gmail-history", {"history_id": new_history_id})
    return {"ok": True}


@router.get("/select", response_class=HTMLResponse)
async def select_gmail_project(
    request: Request,
    principal: str,
    thread: str,
    project: str,
    expiry: int,
    signature: str,
) -> HTMLResponse:
    services: Services = request.app.state.services
    expected = _signature(services.settings.signed_link_secret, principal, thread, project, expiry)
    if expiry < int(time.time()) or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=400, detail="expired or invalid selection link")
    selected = await services.repository.get_project(project)
    if selected is None or not selected.can_access(principal):
        raise HTTPException(status_code=403, detail="project access denied")
    await services.repository.set_channel_project(f"gmail:{thread}", selected.id)
    return HTMLResponse(
        f"<h1>Project selected</h1><p>This email thread is now bound to "
        f"{escape(selected.name)}. You may return to Gmail.</p>"
    )


async def renew_watch(services: Services) -> dict[str, object]:
    if not services.settings.gmail_pubsub_topic:
        raise ValueError("Gmail Pub/Sub topic is not configured")

    def watch() -> dict[str, object]:
        api = _gmail_service(["https://www.googleapis.com/auth/gmail.modify"])
        return cast(
            dict[str, object],
            api.users()
            .watch(userId="me", body={"topicName": services.settings.gmail_pubsub_topic})
            .execute(),
        )

    response = await asyncio.to_thread(watch)
    await services.repository.set_connector_state(
        "gmail-history", {"history_id": str(response["historyId"])}
    )
    return response
