"""Slack DM Events API adapter with explicit 14-day project bindings."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import timedelta
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from slack_sdk import WebClient
from starlette.datastructures import Headers

from mica.api.dependencies import Services
from mica.connectors.routing import (
    bind_delivery_route,
    task_delivery_route,
)
from mica.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    DeliveryRoute,
    ProjectCreate,
    TaskEnvelope,
    TaskRecord,
    TaskSource,
    TaskStatus,
    utcnow,
)
from mica.services.tasks import TaskInteractionConflict

router = APIRouter(prefix="/integrations/slack", tags=["integrations"])
PROJECT_COMMAND = re.compile(r"^(?:/project|project:|switch project)\s*([a-z0-9-]+)$", re.I)
APPROVAL_PAGE_SIZE = 40
CANCEL_COMMAND = re.compile(r"^(?:cancel|stop)(?:\s+(?:task|this))?[.!]?$", re.I)


def verify_slack_request(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _reply(
    services: Services,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    *,
    thread_ts: str = "",
) -> None:
    if not services.settings.slack_bot_token:
        return
    client = WebClient(token=services.settings.slack_bot_token)
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    await asyncio.to_thread(client.chat_postMessage, **kwargs)


def _project_actions(has_projects: bool) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    if has_projects:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Select project"},
                "action_id": "select_project",
            }
        )
    elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Create project"},
            "style": "primary",
            "action_id": "create_project",
        }
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Choose the project that should own this conversation, or create a new one. "
                    "Project memory and artifacts remain isolated."
                ),
            },
        },
        {"type": "actions", "elements": elements},
    ]


def _create_project_modal(channel: str, team_id: str = "") -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "create_project_submit",
        "private_metadata": json.dumps({"channel": channel, "team_id": team_id}),
        "title": {"type": "plain_text", "text": "Create project"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "name_block",
                "label": {"type": "plain_text", "text": "Project name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "name",
                    "placeholder": {"type": "plain_text", "text": "Azizilab AML"},
                    "max_length": 120,
                },
            },
            {
                "type": "input",
                "block_id": "slug_block",
                "label": {"type": "plain_text", "text": "Project slug"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "slug",
                    "placeholder": {"type": "plain_text", "text": "azizilab-aml"},
                    "max_length": 80,
                },
            },
            {
                "type": "input",
                "block_id": "members_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Additional member emails"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "members",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "researcher@example.org, collaborator@example.org",
                    },
                },
            },
        ],
    }


def _select_project_modal(channel: str, projects: list[Any], team_id: str = "") -> dict[str, Any]:
    options = [
        {
            "text": {"type": "plain_text", "text": f"{project.name} ({project.slug})"[:75]},
            "value": project.id,
        }
        for project in projects[:100]
    ]
    return {
        "type": "modal",
        "callback_id": "select_project_submit",
        "private_metadata": json.dumps({"channel": channel, "team_id": team_id}),
        "title": {"type": "plain_text", "text": "Select project"},
        "submit": {"type": "plain_text", "text": "Select"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "project_block",
                "label": {"type": "plain_text", "text": "Project"},
                "element": {
                    "type": "static_select",
                    "action_id": "project",
                    "options": options,
                },
            }
        ],
    }


async def _open_modal(services: Services, trigger_id: str, view: dict[str, Any]) -> None:
    client = WebClient(token=services.settings.slack_bot_token)
    await asyncio.to_thread(client.views_open, trigger_id=trigger_id, view=view)


def _view_value(payload: dict[str, Any], block: str, action: str) -> dict[str, Any]:
    values = payload.get("view", {}).get("state", {}).get("values", {})
    value = values.get(block, {}).get(action, {}) if isinstance(values, dict) else {}
    return value if isinstance(value, dict) else {}


def _slack_principal(services: Services, payload: dict[str, Any]) -> tuple[str, str]:
    user = payload.get("user", {})
    user_id = str(user.get("id", "")) if isinstance(user, dict) else ""
    team = payload.get("team", {})
    team_id = str(team.get("id", "")) if isinstance(team, dict) else ""
    team_id = team_id or str(payload.get("team_id", ""))
    principal = services.settings.slack_user_map.get(f"{team_id}:{user_id}", "")
    principal = principal or services.settings.slack_user_map.get(user_id, "")
    return user_id, principal.lower()


def _interaction_channel(payload: dict[str, Any]) -> str:
    channel = payload.get("channel", {})
    if isinstance(channel, dict) and channel.get("id"):
        return str(channel["id"])
    container = payload.get("container", {})
    if isinstance(container, dict):
        return str(container.get("channel_id", ""))
    return ""


def _interaction_thread(payload: dict[str, Any]) -> str:
    container = payload.get("container", {})
    if isinstance(container, dict):
        return str(container.get("thread_ts") or container.get("message_ts") or "")
    return ""


def _interaction_team(payload: dict[str, Any]) -> str:
    team = payload.get("team", {})
    if isinstance(team, dict):
        return str(team.get("id", ""))
    return str(payload.get("team_id", ""))


async def _bind_project_delivery(
    services: Services,
    *,
    project_id: str,
    principal: str,
    user_id: str,
    team_id: str,
    channel: str,
) -> None:
    expires_at = utcnow() + timedelta(days=14)
    await services.repository.set_channel_project(
        f"slack:{team_id}:{user_id}:{channel}", project_id, expires_at
    )
    await bind_delivery_route(
        services.repository,
        DeliveryRoute(
            project_id=project_id,
            principal_id=principal,
            source=TaskSource.SLACK,
            conversation_id=f"slack:{team_id}:{channel}",
            expires_at=expires_at,
        ),
    )


async def _task_slack_route(services: Services, task: TaskRecord) -> DeliveryRoute | None:
    if task.envelope.source == TaskSource.SLACK:
        return DeliveryRoute(
            project_id=task.envelope.project_id,
            principal_id=task.envelope.principal_id,
            source=TaskSource.SLACK,
            conversation_id=task.envelope.conversation_id,
            thread_id=task.envelope.thread_id,
        )
    return await task_delivery_route(services.repository, task.id, TaskSource.SLACK)


def _approval_fields(approval: ApprovalRequest) -> list[dict[str, Any]]:
    raw = approval.payload.get("fields", approval.payload.get("parameters", []))
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _approval_modal(
    task_id: str,
    approval: ApprovalRequest,
    channel: str,
    thread_ts: str,
    *,
    offset: int = 0,
    draft_id: str = "",
) -> dict[str, Any]:
    fields = _approval_fields(approval)
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": approval.detail[:3000]},
        }
    ]
    # Slack modals allow at most 100 blocks. Option fields need a second custom-value
    # input, so keep enough space for the description and all generated controls.
    for index, field in enumerate(fields[offset:offset + APPROVAL_PAGE_SIZE], start=offset):
        label = str(field.get("label") or field.get("name") or field.get("id") or "Setting")
        proposed = field.get("proposed_value", field.get("value", ""))
        if isinstance(proposed, list):
            initial_value = ", ".join(str(item) for item in proposed)
        elif proposed is None:
            initial_value = ""
        else:
            initial_value = str(proposed)
        options = field.get("options", [])
        if isinstance(options, list) and options:
            values = [
                str(value)
                for value in options
                if not re.match(r"^(approve|change|other)\b", str(value), re.I)
            ]
            if initial_value and initial_value not in values:
                values.insert(0, initial_value)
            slack_options = []
            for option_index, raw_value in enumerate(values[:99]):
                needs_custom = bool(re.match(r"^(provide|specify)\b", raw_value, re.I))
                slack_options.append(
                    {
                        "text": {"type": "plain_text", "text": raw_value[:75]},
                        "value": f"__custom__:{option_index}" if needs_custom else raw_value[:150],
                    }
                )
            slack_options.append(
                {"text": {"type": "plain_text", "text": "Other…"}, "value": "__other__"}
            )
            element: dict[str, Any] = {
                "type": "static_select",
                "action_id": "value",
                "placeholder": {"type": "plain_text", "text": "Select a value"},
                "options": slack_options,
            }
            initial_option = next(
                (option for option in slack_options if option["value"] == initial_value[:150]),
                None,
            )
            if initial_option is not None:
                element["initial_option"] = initial_option
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"field_{index}",
                    "label": {"type": "plain_text", "text": label[:2000]},
                    "element": element,
                }
            )
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"custom_{index}",
                    "optional": True,
                    "label": {"type": "plain_text", "text": f"Custom {label}"[:2000]},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "Only used for Other…"},
                    },
                }
            )
        else:
            element = {"type": "plain_text_input", "action_id": "value"}
            if initial_value:
                element["initial_value"] = initial_value[:3000]
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"field_{index}",
                    "label": {"type": "plain_text", "text": label[:2000]},
                    "element": element,
                }
            )
    if not fields:
        blocks.extend([
            {
                "type": "input",
                "block_id": "decision",
                "label": {"type": "plain_text", "text": "Your decision"},
                "element": {
                    "type": "static_select", "action_id": "value",
                    "options": [
                        {"text": {"type": "plain_text", "text": label}, "value": value}
                        for label, value in (("Approve", "approve"), ("Decline", "decline"))
                    ],
                },
            },
            {
                "type": "input", "block_id": "response", "optional": True,
                "label": {"type": "plain_text", "text": "Decision details (if needed)"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            },
        ])
    more = offset + APPROVAL_PAGE_SIZE < len(fields)
    title = "Review settings" if fields else "Review approval"
    if len(fields) > APPROVAL_PAGE_SIZE:
        pages = (len(fields) - 1) // APPROVAL_PAGE_SIZE + 1
        title = f"Settings {offset // APPROVAL_PAGE_SIZE + 1}/{pages}"
    metadata = json.dumps(
        {
            "task_id": task_id,
            "approval_id": approval.id,
            "channel": channel,
            "thread_ts": thread_ts,
            "offset": offset,
            "draft_id": draft_id,
        },
        separators=(",", ":"),
    )
    return {
        "type": "modal",
        "callback_id": "task_approval_submit",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": (
            "Next" if more else "Confirm settings" if fields else "Submit decision"
        )},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _submitted_approval_values(
    payload: dict[str, Any], approval: ApprovalRequest, *, offset: int = 0
) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    fields = _approval_fields(approval)
    for index, field in enumerate(fields[offset:offset + APPROVAL_PAGE_SIZE], start=offset):
        field_id = str(field.get("id") or field.get("name") or f"field_{index}")
        selected = _view_value(payload, f"field_{index}", "value")
        selected_option = selected.get("selected_option")
        if isinstance(selected_option, dict):
            value = str(selected_option.get("value", "")).strip()
            if value == "__other__" or value.startswith("__custom__:"):
                value = str(
                    _view_value(payload, f"custom_{index}", "value").get("value", "")
                ).strip()
                if not value:
                    errors[f"custom_{index}"] = "Enter a custom value for this setting."
        else:
            value = str(selected.get("value", "")).strip()
        if not value:
            errors[f"field_{index}"] = "Enter a value for this setting."
        values[field_id] = value
    return values, errors


@router.post("/actions")
async def slack_actions(request: Request) -> dict[str, object]:
    services: Services = request.app.state.services
    body = await request.body()
    if not verify_slack_request(
        body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
        services.settings.slack_signing_secret,
    ):
        raise HTTPException(status_code=401, detail="invalid Slack signature")
    encoded_payload = parse_qs(body.decode()).get("payload", [""])[0]
    try:
        payload = json.loads(encoded_payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid Slack payload") from exc

    from mica.agents.analysis import command_approval, transfer_interactions, write_approval

    if write_approval.is_action(payload) or command_approval.is_action(payload):
        from slack_sdk.web.async_client import AsyncWebClient

        from mica.agents.analysis.batch import BatchBridge
        from mica.agents.analysis.storage import GCSStorage

        try:
            if command_approval.is_action(payload):
                return await command_approval.handle(
                    BatchBridge(services.settings, services.repository),
                    GCSStorage(services.settings.gcs_bucket), payload,
                )
            return await write_approval.handle(
                BatchBridge(services.settings, services.repository),
                GCSStorage(services.settings.gcs_bucket),
                payload,
            )
        except (ValueError, PermissionError) as exc:
            await AsyncWebClient(
                token=services.settings.slack_bot_token, timeout=5
            ).chat_postEphemeral(
                channel=payload.get("channel", {}).get("id", ""),
                user=payload.get("user", {}).get("id", ""),
                text=str(exc),
            )
            return {}

    if transfer_interactions.is_transfer(payload):
        from mica.agents.analysis.batch import BatchBridge
        from mica.agents.analysis.storage import GCSStorage
        from mica.agents.analysis.transfers import Transfers

        return await transfer_interactions.handle(
            Transfers(BatchBridge(services.settings, services.repository),
                      GCSStorage(services.settings.gcs_bucket)), payload,
        )

    user_id, principal = _slack_principal(services, payload)
    if not principal:
        raise HTTPException(status_code=403, detail="Slack identity is not mapped")

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        actions = payload.get("actions", [])
        action = actions[0] if isinstance(actions, list) and actions else {}
        action_id = str(action.get("action_id", "")) if isinstance(action, dict) else ""
        channel = _interaction_channel(payload)
        team_id = _interaction_team(payload)
        trigger_id = str(payload.get("trigger_id", ""))
        if not channel or not trigger_id:
            raise HTTPException(status_code=400, detail="Slack interaction context is incomplete")
        if action_id == "create_project":
            await _open_modal(services, trigger_id, _create_project_modal(channel, team_id))
        elif action_id == "select_project":
            projects = await services.repository.list_projects(principal)
            if projects:
                await _open_modal(
                    services, trigger_id, _select_project_modal(channel, projects, team_id)
                )
            else:
                await _open_modal(services, trigger_id, _create_project_modal(channel, team_id))
        elif action_id == "cancel_task":
            try:
                context = json.loads(str(action.get("value", "{}")))
                task_id = str(context["task_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise HTTPException(status_code=400, detail="invalid task context") from exc
            task = await services.tasks.require_access(task_id, principal)
            route = await _task_slack_route(services, task)
            expected_channel = (
                route.conversation_id.removeprefix("slack:").split(":")[-1] if route else ""
            )
            if route is None or expected_channel != channel:
                raise HTTPException(status_code=400, detail="task channel does not match")
            await services.tasks.cancel(task.id, principal)
        elif action_id == "approve_task_approval":
            try:
                context = json.loads(str(action.get("value", "{}")))
                task_id = str(context["task_id"])
                approval_id = str(context["approval_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise HTTPException(status_code=400, detail="invalid approval context") from exc
            task = await services.tasks.require_access(task_id, principal)
            route = await _task_slack_route(services, task)
            expected_channel = (
                route.conversation_id.removeprefix("slack:").split(":")[-1] if route else ""
            )
            if route is None or expected_channel != channel:
                raise HTTPException(status_code=400, detail="approval channel does not match task")
            approval = next((item for item in task.approvals if item.id == approval_id), None)
            if approval is None or approval.status != ApprovalStatus.PENDING:
                raise HTTPException(status_code=400, detail="approval is no longer pending")
            if approval.kind != ApprovalKind.COMPUTE:
                raise HTTPException(status_code=400, detail="approval requires detailed review")
            updated = await services.tasks.decide_approval(
                task.id,
                approval.id,
                principal,
                ApprovalDecision(approved=True),
                defer_replan=True,
            )
            if updated.status == TaskStatus.RECEIVED:
                await services.dispatcher.enqueue(updated.id)
        elif action_id == "review_task_approval":
            try:
                context = json.loads(str(action.get("value", "{}")))
                task_id = str(context["task_id"])
                approval_id = str(context["approval_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise HTTPException(status_code=400, detail="invalid approval context") from exc
            task = await services.tasks.require_access(task_id, principal)
            route = await _task_slack_route(services, task)
            expected_channel = (
                route.conversation_id.removeprefix("slack:").split(":")[-1] if route else ""
            )
            if route is None or expected_channel != channel:
                raise HTTPException(status_code=400, detail="approval channel does not match task")
            approval = next((item for item in task.approvals if item.id == approval_id), None)
            if approval is None or approval.status != ApprovalStatus.PENDING:
                raise HTTPException(status_code=400, detail="approval is no longer pending")
            draft_id = ""
            if len(_approval_fields(approval)) > APPROVAL_PAGE_SIZE:
                draft_id = secrets.token_hex(16)
                await services.repository.set_connector_state(
                    f"slack-approval:{principal}:{draft_id}",
                    {
                        "fingerprint": hashlib.sha256(
                            approval.model_dump_json().encode()
                        ).hexdigest(),
                        "pages": {},
                        "expires_at": time.time() + 3600,
                    },
                )
            await _open_modal(
                services,
                trigger_id,
                _approval_modal(
                    task.id,
                    approval,
                    channel,
                    route.thread_id or _interaction_thread(payload),
                    draft_id=draft_id,
                ),
            )
        return {"ok": True}

    if payload_type != "view_submission":
        return {"ok": True}

    view = payload.get("view", {})
    callback_id = str(view.get("callback_id", "")) if isinstance(view, dict) else ""
    metadata = str(view.get("private_metadata", "{}")) if isinstance(view, dict) else "{}"
    try:
        parsed_metadata = json.loads(metadata)
        channel = str(parsed_metadata.get("channel", ""))
        team_id = str(parsed_metadata.get("team_id", ""))
    except json.JSONDecodeError:
        parsed_metadata = {}
        channel = ""
        team_id = ""

    if callback_id == "task_approval_submit":
        submitted_fields = view.get("state", {}).get("values", {})
        error_block = next(
            (key for key in submitted_fields if key.startswith("field_") or key == "decision"),
            "field_0",
        )
        task_id = str(parsed_metadata.get("task_id", ""))
        approval_id = str(parsed_metadata.get("approval_id", ""))
        if not channel or not task_id or not approval_id:
            return {
                "response_action": "errors",
                "errors": {error_block: "This approval context is incomplete. Reopen it."},
            }
        try:
            task = await services.tasks.require_access(task_id, principal)
        except LookupError:
            return {
                "response_action": "errors",
                "errors": {error_block: "This task is no longer available."},
            }
        route = await _task_slack_route(services, task)
        expected_channel = (
            route.conversation_id.removeprefix("slack:").split(":")[-1] if route else ""
        )
        if route is None or expected_channel != channel:
            return {
                "response_action": "errors",
                "errors": {error_block: "This approval does not belong to this Slack thread."},
            }
        approval = next((item for item in task.approvals if item.id == approval_id), None)
        if approval is None or approval.status != ApprovalStatus.PENDING:
            return {
                "response_action": "errors",
                "errors": {error_block: "This approval is no longer pending."},
            }
        fields = _approval_fields(approval)
        try:
            offset = int(parsed_metadata.get("offset", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid approval page") from None
        if offset < 0 or offset % APPROVAL_PAGE_SIZE or offset >= max(1, len(fields)):
            raise HTTPException(status_code=400, detail="invalid approval page")
        error_block = f"field_{offset}" if fields else "decision"
        if route.thread_id and parsed_metadata.get("thread_ts") != route.thread_id:
            return {
                "response_action": "errors",
                "errors": {error_block: "This approval does not belong to this Slack thread."},
            }
        values, errors = _submitted_approval_values(payload, approval, offset=offset)
        if errors:
            return {"response_action": "errors", "errors": errors}
        approved = True
        response = json.dumps({"values": values}, separators=(",", ":"))
        if not fields:
            selection = _view_value(payload, "decision", "value").get("selected_option") or {}
            decision = selection.get("value")
            if decision not in {"approve", "decline"}:
                return {"response_action": "errors", "errors": {"decision": "Choose a decision."}}
            approved = decision == "approve"
            response = str(_view_value(payload, "response", "value").get("value") or "").strip()
        if len(fields) > APPROVAL_PAGE_SIZE:
            draft_id = str(parsed_metadata.get("draft_id", ""))
            key = f"slack-approval:{principal}:{draft_id}"
            draft = await services.repository.get_connector_state(key) or {}
            fingerprint = hashlib.sha256(approval.model_dump_json().encode()).hexdigest()
            pages = draft.get("pages", {})
            if (
                draft.get("fingerprint") != fingerprint
                or draft.get("expires_at", 0) < time.time()
                or any(str(page) not in pages for page in range(0, offset, APPROVAL_PAGE_SIZE))
            ):
                return {
                    "response_action": "errors",
                    "errors": {error_block: "This review expired or changed. Reopen the approval."},
                }
            pages[str(offset)] = values
            await services.repository.set_connector_state(key, {**draft, "pages": pages})
            if offset + APPROVAL_PAGE_SIZE < len(fields):
                return {
                    "response_action": "update",
                    "view": _approval_modal(
                        task.id, approval, channel, route.thread_id or "",
                        offset=offset + APPROVAL_PAGE_SIZE, draft_id=draft_id,
                    ),
                }
            values = {name: value for page in range(0, len(fields), APPROVAL_PAGE_SIZE)
                      for name, value in pages[str(page)].items()}
            response = json.dumps({"values": values}, separators=(",", ":"))
        try:
            updated = await services.tasks.decide_approval(
                task.id,
                approval.id,
                principal,
                ApprovalDecision(approved=approved, response=response),
                defer_replan=True,
            )
        except (LookupError, ValueError) as exc:
            return {
                "response_action": "errors",
                "errors": {error_block: str(exc)[:150]},
            }
        if updated.status == TaskStatus.RECEIVED:
            await services.dispatcher.enqueue(updated.id)
        return {"response_action": "clear"}

    if not channel:
        return {"response_action": "errors", "errors": {"name_block": "Missing DM context."}}

    if callback_id == "create_project_submit":
        name = str(_view_value(payload, "name_block", "name").get("value", "")).strip()
        slug = str(_view_value(payload, "slug_block", "slug").get("value", "")).strip().lower()
        member_text = str(_view_value(payload, "members_block", "members").get("value", ""))
        members = {value.strip().lower() for value in member_text.split(",") if value.strip()}
        if not name:
            return {"response_action": "errors", "errors": {"name_block": "Enter a name."}}
        try:
            project = await services.projects.create(
                principal, ProjectCreate(name=name, slug=slug, members=members)
            )
        except ValueError:
            return {
                "response_action": "errors",
                "errors": {"slug_block": "Use a unique slug with letters, digits, or hyphens."},
            }
    elif callback_id == "select_project_submit":
        selected = _view_value(payload, "project_block", "project").get("selected_option", {})
        project_id = str(selected.get("value", "")) if isinstance(selected, dict) else ""
        selected_project = await services.repository.get_project(project_id)
        if selected_project is None or not selected_project.can_access(principal):
            return {
                "response_action": "errors",
                "errors": {"project_block": "That project is no longer available."},
            }
        project = selected_project
    else:
        return {"ok": True}

    await _bind_project_delivery(
        services,
        project_id=project.id,
        principal=principal,
        user_id=user_id,
        team_id=team_id,
        channel=channel,
    )
    await _reply(services, channel, f"Active project: {project.name} ({project.slug}).")
    return {"response_action": "clear"}


@router.post("/events")
async def slack_events(request: Request) -> dict[str, object]:
    services: Services = request.app.state.services
    body = await request.body()
    if not verify_slack_request(
        body,
        request.headers.get("x-slack-request-timestamp", ""),
        request.headers.get("x-slack-signature", ""),
        services.settings.slack_signing_secret,
    ):
        raise HTTPException(status_code=401, detail="invalid Slack signature")
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if services.settings.slack_agent_backend == "claude_batch":
        from mica.api.slack_analysis import receive

        return await receive(services, payload)
    event = payload.get("event", {})
    if (
        payload.get("type") != "event_callback"
        or event.get("type") != "message"
        or event.get("channel_type") != "im"
        or event.get("bot_id")
        or event.get("subtype")
    ):
        return {"ok": True}
    user_id = str(event.get("user", ""))
    team_id = str(payload.get("team_id", ""))
    principal = services.settings.slack_user_map.get(f"{team_id}:{user_id}", "")
    principal = (principal or services.settings.slack_user_map.get(user_id, "")).lower()
    channel = str(event.get("channel", ""))
    if not principal:
        await _reply(services, channel, "Your Slack identity is not mapped to a Mica user.")
        return {"ok": True}
    text = str(event.get("text", "")).strip()
    binding_key = f"slack:{team_id}:{user_id}:{channel}"
    command = PROJECT_COMMAND.match(text)
    if command:
        project = await services.repository.get_project_by_slug(command.group(1).lower())
        if project is None or not project.can_access(principal):
            await _reply(services, channel, "That project is unavailable to your account.")
            return {"ok": True}
        await _bind_project_delivery(
            services,
            project_id=project.id,
            principal=principal,
            user_id=user_id,
            team_id=team_id,
            channel=channel,
        )
        await _reply(services, channel, f"Active project: {project.name} ({project.slug}).")
        return {"ok": True}
    project_id = await services.repository.get_channel_project(binding_key)
    if not project_id:
        # Preserve bindings created before workspace IDs became part of the correlation key.
        project_id = await services.repository.get_channel_project(f"slack:{user_id}:{channel}")
    if not project_id:
        projects = await services.repository.list_projects(principal)
        await _reply(
            services,
            channel,
            "Select or create a project before submitting a task.",
            _project_actions(bool(projects)),
        )
        return {"ok": True}
    project = await services.repository.get_project(project_id)
    if project is None or not project.can_access(principal):
        await _reply(services, channel, "Your project binding is no longer authorized.")
        return {"ok": True}
    thread_ts = str(event.get("thread_ts") or "")
    event_key = f"slack:{team_id}:{payload.get('event_id', event.get('client_msg_id', ''))}"
    if thread_ts:
        conversation_id = f"slack:{team_id}:{channel}"
        tasks = await services.repository.list_tasks(project.id, limit=100)
        parent = next(
            (
                item
                for item in tasks
                if item.envelope.conversation_id == conversation_id
                and item.envelope.thread_id == thread_ts
            ),
            None,
        )
        if parent is None:
            # API and historical web tasks can continue in their mirrored Slack thread.
            for candidate in tasks:
                route = await task_delivery_route(
                    services.repository, candidate.id, TaskSource.SLACK
                )
                if (route is not None and route.conversation_id == conversation_id
                        and route.thread_id == thread_ts
                        and route.principal_id.lower() == principal):
                    parent = candidate
                    break
        if parent is not None and CANCEL_COMMAND.fullmatch(text):
            if await services.repository.mark_event(event_key):
                await services.tasks.cancel(parent.id, principal)
            return {"ok": True, "task_id": parent.id}
        if parent is not None and parent.status in {
            TaskStatus.RECEIVED,
            TaskStatus.PLANNING,
            TaskStatus.NEEDS_INPUT,
            TaskStatus.NEEDS_APPROVAL,
            TaskStatus.SUBMITTING,
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
        } and text:
            try:
                updated, added = await services.tasks.add_context(
                    parent.id,
                    principal,
                    text,
                    source=TaskSource.SLACK,
                    external_event_id=event_key,
                )
            except TaskInteractionConflict:
                return {"ok": True, "task_id": parent.id}
            if added:
                await services.dispatcher.enqueue(updated.id)
            return {"ok": True, "task_id": updated.id}
    attachments = []
    for item in event.get("files", []):
        url = str(item.get("url_private_download") or item.get("url_private") or "")
        if not url:
            continue
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {services.settings.slack_bot_token}"},
            )
            response.raise_for_status()
        upload = UploadFile(
            file=BytesIO(response.content),
            filename=str(item.get("name", "slack-upload.bin")),
            headers=Headers(
                {"content-type": str(item.get("mimetype", "application/octet-stream"))}
            ),
        )
        attachments.append(await services.uploads.accept(upload, principal))
    envelope = TaskEnvelope(
        project_id=project.id,
        source=TaskSource.SLACK,
        external_event_id=(
            event_key
        ),
        conversation_id=f"slack:{team_id}:{channel}",
        thread_id=str(event.get("thread_ts") or event.get("ts") or ""),
        principal_id=principal,
        prompt=text,
        attachments=attachments,
    )
    task, created = await services.tasks.create(envelope)
    if created:
        await services.dispatcher.enqueue(task.id)
    return {"ok": True, "task_id": task.id}
