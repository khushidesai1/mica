"""Best-effort user notifications for asynchronous channel tasks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Awaitable
from email.message import EmailMessage
from typing import Any, Protocol

from google.auth import default
from googleapiclient.discovery import build
from slack_sdk import WebClient

from pistachio.config import Settings
from pistachio.connectors.routing import (
    bind_task_delivery_route,
    origin_delivery_route,
    preferred_delivery_route,
    task_delivery_route,
)
from pistachio.models import (
    ApprovalKind,
    ApprovalStatus,
    DeliveryRoute,
    TaskRecord,
    TaskSource,
    TaskStatus,
)
from pistachio.services.artifacts import prepare_artifact_delivery
from pistachio.storage.base import Repository


class TaskNotifier(Protocol):
    async def notify(self, task: TaskRecord) -> None: ...


class ChannelNotifier:
    def __init__(self, settings: Settings, repository: Repository | None = None) -> None:
        self.settings = settings
        self.repository = repository

    async def notify(self, task: TaskRecord) -> None:
        deliveries: list[Awaitable[None]] = []
        slack_route = await self._slack_route(task)
        if slack_route is not None and self.settings.slack_bot_token:
            deliveries.append(self._slack(task, slack_route))
        if task.envelope.source == TaskSource.EMAIL and self.settings.gmail_account:
            deliveries.append(self._gmail(task))
        if deliveries:
            # One unavailable surface must not suppress updates to the others.
            results = await asyncio.gather(*deliveries, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def _slack_route(self, task: TaskRecord) -> DeliveryRoute | None:
        origin = origin_delivery_route(task)
        if origin is not None and origin.source == TaskSource.SLACK:
            return origin
        if self.repository is None:
            return None
        existing = await task_delivery_route(self.repository, task.id, TaskSource.SLACK)
        if existing is not None:
            return existing
        return await preferred_delivery_route(
            self.repository,
            task.envelope.project_id,
            task.envelope.principal_id,
            TaskSource.SLACK,
        )

    async def _slack(self, task: TaskRecord, route: DeliveryRoute) -> None:
        # Connector state survives separate workers and process restarts. Serialize updates
        # to avoid creating two status messages when callbacks arrive together.
        if self.repository is None:
            await self._deliver_slack(task, route, {}, "")
            return
        key = "slack-conversation:" + hashlib.sha256(
            f"{task.id}:{route.conversation_id}".encode()
        ).hexdigest()
        claim = await self.repository.begin_event(key, lease_seconds=1200)
        if claim.status != "claimed":
            raise RuntimeError("Slack delivery is already in progress")
        try:
            state = await self.repository.get_connector_state(key) or {}
            await self._deliver_slack(task, route, state, key)
        finally:
            await self.repository.release_event(key, claim.token)

    async def _deliver_slack(
        self, task: TaskRecord, route: DeliveryRoute, state: dict[str, Any], key: str
    ) -> None:
        client = WebClient(token=self.settings.slack_bot_token)
        channel = route.conversation_id.removeprefix("slack:").split(":")[-1]
        delivered_ids = list(state.get("delivered_ids", []))
        thread_ts = route.thread_id or str(state.get("thread_ts", ""))
        for message in task.conversation:
            if message.id in delivered_ids:
                continue
            if message.role == "user" and message.source == TaskSource.SLACK:
                # The originating Slack message is already visible in this thread.
                delivered_ids.append(message.id)
                continue
            kwargs: dict[str, Any] = {"channel": channel, "text": message.text}
            if message.role == "user":
                kwargs["blocks"] = [
                    {"type": "context", "elements": [{"type": "plain_text", "text": "You"}]},
                    *[{"type": "section", "text": {"type": "plain_text", "text": chunk}}
                      for chunk in (message.text[i:i + 3000]
                                    for i in range(0, len(message.text), 3000))],
                ]
            if message.kind == "approval" and task.status == TaskStatus.NEEDS_APPROVAL:
                kwargs["blocks"] = self._approval_blocks(task, message.text)
            elif message.kind == "acknowledgement" and task.status not in {
                TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED,
            }:
                kwargs["blocks"] = _running_task_actions(task, message.text)
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            response = await asyncio.to_thread(client.chat_postMessage, **kwargs)
            thread_ts = thread_ts or str(response.get("ts", ""))
            delivered_ids.append(message.id)
            state.update(delivered_ids=delivered_ids, thread_ts=thread_ts)
            if self.repository is not None:
                await bind_task_delivery_route(
                    self.repository, task.id, route.model_copy(update={"thread_id": thread_ts})
                )
                # Checkpoint each successful post so a later failure retries only the tail.
                await self.repository.set_connector_state(key, state)
        if (task.status == TaskStatus.SUCCEEDED and task.result and task.result.artifacts
                and not state.get("artifacts_delivered")):
            prepared = await prepare_artifact_delivery(task, self.settings)
            try:
                uploaded = list(state.get("uploaded_files", []))
                for file in prepared.files:
                    if file.name in uploaded:
                        continue
                    upload: dict[str, Any] = {
                        "channel": channel, "file": str(file.path), "filename": file.name,
                        "title": (f"{file.artifact_count} task artifacts"
                                  if file.artifact_count > 1 else file.name),
                    }
                    if thread_ts:
                        upload["thread_ts"] = thread_ts
                    await asyncio.to_thread(client.files_upload_v2, **upload)
                    uploaded.append(file.name)
                    state["uploaded_files"] = uploaded
                    if self.repository is not None:
                        await self.repository.set_connector_state(key, state)
                state["artifacts_delivered"] = True
                if self.repository is not None:
                    await self.repository.set_connector_state(key, state)
            finally:
                prepared.cleanup()

    def _approval_blocks(self, task: TaskRecord, text: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}
        ]
        for approval in (item for item in task.approvals if item.status == ApprovalStatus.PENDING):
            raw_fields = approval.payload.get("fields", approval.payload.get("parameters", []))
            fields = raw_fields if isinstance(raw_fields, list) else []
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{approval.title}*\n{approval.detail}"[:3000],
                    },
                }
            )
            elements: list[dict[str, Any]] = []
            if 0 < len(fields) <= 40:
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Review settings"},
                        "style": "primary",
                        "action_id": "review_task_approval",
                        "value": json.dumps(
                            {"task_id": task.id, "approval_id": approval.id},
                            separators=(",", ":"),
                        ),
                    }
                )
            elif approval.kind == ApprovalKind.COMPUTE:
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve resource usage"},
                        "style": "primary",
                        "action_id": "approve_task_approval",
                        "value": json.dumps(
                            {"task_id": task.id, "approval_id": approval.id},
                            separators=(",", ":"),
                        ),
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Approve compute plan?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": approval.detail[:3000],
                            },
                            "confirm": {"type": "plain_text", "text": "Approve"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    }
                )
            if self.settings.control_plane_url:
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open in Pistachio"},
                        "url": (f"{self.settings.control_plane_url.rstrip('/')}/?task={task.id}"),
                        "action_id": f"open_approval_{approval.id}"[:255],
                    }
                )
            if elements:
                blocks.append({"type": "actions", "elements": elements})
        return blocks[:50]

    async def _gmail(self, task: TaskRecord) -> None:
        message = EmailMessage()
        message["To"] = task.envelope.principal_id
        message["From"] = self.settings.gmail_account
        message["Subject"] = f"Pistachio · {task.public_name} · {task.status.value}"
        state_key = f"gmail-conversation:{task.id}"
        state = (
            await self.repository.get_connector_state(state_key) or {}
            if self.repository is not None else {}
        )
        delivered = state.get("delivered_ids", [])
        pending = [item for item in task.conversation
                   if item.role == "assistant" and item.id not in delivered]
        if not pending:
            return
        message.set_content("\n\n".join(item.text for item in pending))
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        thread_id = task.envelope.conversation_id.removeprefix("gmail:")

        def send() -> None:
            credentials, _ = default(  # type: ignore[no-untyped-call]
                scopes=["https://www.googleapis.com/auth/gmail.send"]
            )
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            body: dict[str, str] = {"raw": raw}
            if thread_id:
                body["threadId"] = thread_id
            service.users().messages().send(userId="me", body=body).execute()

        await asyncio.to_thread(send)
        if self.repository is not None:
            await self.repository.set_connector_state(
                state_key, {"delivered_ids": delivered + [item.id for item in pending]}
            )


def _running_task_actions(task: TaskRecord, text: str) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel task"},
                    "style": "danger",
                    "action_id": "cancel_task",
                    "value": json.dumps({"task_id": task.id}),
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Cancel this task?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Pistachio will stop planning or execution as soon as possible."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Cancel task"},
                        "deny": {"type": "plain_text", "text": "Keep running"},
                    },
                }
            ],
        }
    ]


