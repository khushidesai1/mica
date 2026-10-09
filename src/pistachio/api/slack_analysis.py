"""Authenticated dispatch for the Slack-only Claude Spot worker."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import BaseModel, Field

from pistachio.agents.analysis import (
    access,
    channel_access,
    conversation_memory,
    identity,
    write_access,
    write_broker,
)
from pistachio.agents.analysis.batch import BatchBridge, digest
from pistachio.agents.analysis.data_links import message_text
from pistachio.agents.analysis.storage import GCSStorage
from pistachio.api.auth import get_principal
from pistachio.api.dependencies import Services

router = APIRouter(prefix="/api/v1/internal/slack-analysis", tags=["internal"])


async def receive(services: Services, payload: dict[str, Any]) -> dict[str, object]:
    event = payload.get("event", {})
    if (
        payload.get("type") != "event_callback"
        or event.get("bot_id")
        or event.get("subtype") not in {None, "file_share"}
        or event.get("type") not in {"app_mention", "message"}
    ):
        return {"ok": True}
    user, channel = event.get("user", ""), event.get("channel", "")
    team, timestamp = payload.get("team_id", ""), event.get("ts", "")
    if (
        not user
        or not re.fullmatch(r"[A-Z0-9]+", channel)
        or not re.fullmatch(r"\d+\.\d+", timestamp)
    ):
        return {"ok": True}
    if event.get("channel_type") != "im" and event["type"] == "message":
        bot = next(
            (x.get("user_id") for x in payload.get("authorizations", []) if x.get("is_bot")), ""
        )
        if bot and f"<@{bot}>" in event.get("text", ""):
            return {"ok": True}
        thread = event.get("thread_ts")
        key = digest(f"{team}:{channel}:{thread}")
        if not thread or not await services.repository.get_connector_state(f"claude-thread:{key}"):
            return {"ok": True}
    bridge = BatchBridge(services.settings, services.repository)
    storage = (
        GCSStorage(services.settings.gcs_bucket)
        if bridge.settings.access_mode == "channel"
        else None
    )
    if message_text(event.get("text", "")).lower() in {
        "enable this channel",
        "disable this channel",
        "agent channels",
    }:
        key = "claude-onboarding:" + digest(f"{team}:{channel}:{timestamp}")
        claim = await services.repository.begin_event(key, lease_seconds=60)
        if claim.status != "claimed":
            raise HTTPException(503, "Channel onboarding is busy; retry")
        try:
            if not await services.repository.get_connector_state(key):
                reply = await asyncio.to_thread(
                    channel_access.admin_command,
                    services.settings,
                    bridge.settings,
                    storage,
                    payload,
                    key,
                )
                if reply:
                    await identity.post(
                        services.settings.slack_bot_token,
                        channel,
                        event.get("thread_ts") or timestamp,
                        reply,
                        event_id=key,
                    )
                    await services.repository.set_connector_state(key, {"delivered": True})
        finally:
            await services.repository.release_event(key, claim.token)
        return {"ok": True}
    principal = await asyncio.to_thread(
        identity.resolve_email, services.settings, bridge.settings, storage, team, user
    )
    if principal is None:
        if identity.is_private(event) or event["type"] == "app_mention":
            key = "claude-denial:" + digest(f"{team}:{channel}:{timestamp}")
            claim = await services.repository.begin_event(key, lease_seconds=30)
            if claim.status == "claimed":
                try:
                    if not await services.repository.get_connector_state(key):
                        await identity.post(
                            services.settings.slack_bot_token,
                            channel,
                            event.get("thread_ts") or timestamp,
                            identity.NOT_ENABLED,
                            event_id=key,
                        )
                        await services.repository.set_connector_state(key, {"delivered": True})
                finally:
                    await services.repository.release_event(key, claim.token)
        return {"ok": True}
    if not services.settings.anthropic_api_key.get_secret_value():
        raise HTTPException(503, "Claude API credential has not been configured")
    event_id = await bridge.accept(payload, principal=principal)
    return {"ok": True, "event_id": event_id}


@router.post("/process/{event_id}/{tick}", include_in_schema=False)
async def process(event_id: str, tick: int, request: Request) -> dict[str, str]:
    services: Services = request.app.state.services
    if services.settings.slack_agent_backend != "claude_batch":
        raise HTTPException(503, "Claude Slack rollout is disabled")
    principal = await get_principal(request)
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(403, "Untrusted task dispatcher")
    if not re.fullmatch(r"[a-f0-9]{32}", event_id) or tick < 0:
        raise HTTPException(400, "Invalid event")
    status = await BatchBridge(services.settings, services.repository).process(event_id, tick)
    return {"status": status}


@router.post("/memory/{event_id}", include_in_schema=False)
async def process_memory(event_id: str, request: Request) -> dict[str, str]:
    from pistachio.agents.analysis import memory_tasks

    services: Services = request.app.state.services
    if services.settings.slack_agent_backend != "claude_batch":
        raise HTTPException(503, "Claude Slack rollout is disabled")
    principal = await get_principal(request)
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(403, "Untrusted task dispatcher")
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise HTTPException(400, "Invalid event")
    try:
        await memory_tasks.process(
            BatchBridge(services.settings, services.repository),
            event_id,
            GCSStorage(services.settings.gcs_bucket),
        )
    except Exception:
        # after_turn logs only the failure type. Do not let an upstream validation
        # exception (which can contain model text) become a server traceback.
        raise HTTPException(503, "Memory editor did not complete; retry") from None
    return {"status": "complete"}


@router.post("/knowledge-mirror/{project_id}/{commit_stamp}", include_in_schema=False)
async def mirror_knowledge(project_id: str, commit_stamp: str, request: Request) -> dict[str, int]:
    from pistachio.agents.analysis import knowledge_mirror

    services: Services = request.app.state.services
    if services.settings.slack_agent_backend != "claude_batch":
        raise HTTPException(503, "Claude Slack rollout is disabled")
    principal = await get_principal(request)
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(403, "Untrusted task dispatcher")
    try:
        knowledge_mirror.changes_key(project_id, commit_stamp)
    except ValueError as exc:
        raise HTTPException(400, "Invalid knowledge commit") from exc
    count = await knowledge_mirror.process(
        BatchBridge(services.settings, services.repository),
        GCSStorage(services.settings.gcs_bucket),
        project_id,
        commit_stamp,
    )
    return {"mirrored": count}


@router.post("/transfers/{pending_id}/{tick}", include_in_schema=False)
async def process_transfer(pending_id: str, tick: int, request: Request) -> dict[str, str]:
    from pistachio.agents.analysis.transfers import Transfers

    services: Services = request.app.state.services
    principal = await get_principal(request)
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(403, "Untrusted task dispatcher")
    if not re.fullmatch(r"[a-f0-9]{32}", pending_id) or tick < 0:
        raise HTTPException(400, "Invalid transfer request")
    bridge = BatchBridge(services.settings, services.repository)
    state = await Transfers(bridge, GCSStorage(services.settings.gcs_bucket)).process(
        pending_id, tick
    )
    return {"status": state}


@router.get("/credentials/{event_id}", include_in_schema=False)
async def credentials(event_id: str, request: Request, response: Response) -> dict[str, str]:
    await worker_context(event_id, request)
    services: Services = request.app.state.services
    # Only the trusted worker bootstrap receives these. Never put them in GCS or job metadata.
    response.headers["Cache-Control"] = "no-store"
    return {
        "ANTHROPIC_API_KEY": services.settings.anthropic_api_key.get_secret_value(),
        "SLACK_BOT_TOKEN": services.settings.slack_bot_token,
    }


async def worker_context(
    event_id: str, request: Request
) -> tuple[BatchBridge, GCSStorage, dict[str, Any]]:
    services: Services = request.app.state.services
    if services.settings.slack_agent_backend != "claude_batch":
        raise HTTPException(503, "Claude Slack rollout is disabled")
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise HTTPException(400, "Invalid event")
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token,
            GoogleRequest(),  # type: ignore[no-untyped-call]
            audience=services.settings.control_plane_url,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid worker identity") from exc
    if claims.get("email") != services.settings.batch_service_account or not claims.get(
        "email_verified"
    ):
        raise HTTPException(403, "Untrusted worker")
    record = await services.repository.get_connector_state(f"claude-event:{event_id}")
    if not record or record["status"] not in {"submitting", "running"}:
        raise HTTPException(403, "This worker grant is inactive")
    bridge = BatchBridge(services.settings, services.repository)
    storage = GCSStorage(services.settings.gcs_bucket)
    grant = await asyncio.to_thread(
        access.resolve, services.settings, bridge.settings, storage, record
    )
    if (
        not grant.allowed
        or record.get("cancel_requested")
        or record.get("principal") != grant.email
        or record.get("access_signature") != grant.signature
        or record.get("memory_epoch", 0)
        != await asyncio.to_thread(access.memory_epoch, storage, grant)
        or (
            record.get("compute_version")
            and not await asyncio.to_thread(
                conversation_memory.is_current, storage, grant, record["thread_key"], record
            )
        )
    ):
        raise HTTPException(403, "This worker grant is inactive")
    return bridge, storage, record


class WriteUpload(BaseModel):
    relative_path: str = Field(min_length=1, max_length=512)
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.post("/compute/{event_id}", include_in_schema=False)
async def compute_operation(event_id: str, request: Request, response: Response) -> dict[str, Any]:
    from pistachio.agents.analysis import compute_control, pricing
    from pistachio.agents.analysis.compute_quotes import Provisioning, Rates

    bridge, storage, _ = await worker_context(event_id, request)
    response.headers["Cache-Control"] = "no-store"
    if int(request.headers.get("content-length", "0")) > 12000:
        raise HTTPException(413, "Compute request too large")
    raw = await request.body()
    if len(raw) > 12000:
        raise HTTPException(413, "Compute request too large")
    try:
        body = compute_control.ComputeCall.model_validate_json(raw)
    except ValueError as exc:
        raise HTTPException(422, "Invalid compute request") from exc
    mutex = f"claude-poll:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=180)
    if claim.status != "claimed":
        raise HTTPException(409, "Controller busy; retry shortly")
    try:
        # Revalidate access and attempt after acquiring the same lock used by cancellation.
        bridge, storage, record = await worker_context(event_id, request)

        def get_rates(provisioning: Provisioning) -> Rates:
            return pricing.current(storage, bridge.app.gcp_region, provisioning)

        import time

        result = await asyncio.to_thread(
            compute_control.operate, event_id, record, body, bridge.settings, get_rates, time.time()
        )
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await bridge.repository.release_event(mutex, claim.token)


@router.post("/write/{event_id}", include_in_schema=False)
async def write_output(
    event_id: str, body: WriteUpload, request: Request, response: Response
) -> dict[str, str]:
    bridge, storage, record = await worker_context(event_id, request)
    response.headers["Cache-Control"] = "no-store"
    frozen = record.get("write_grant")
    if not frozen or frozen.get("id") != body.grant_id:
        raise HTTPException(403, "This run has no matching write grant.")
    try:
        relative = write_broker.relative_path(body.relative_path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    def authorize() -> None:
        grant = access.resolve(bridge.app, bridge.settings, storage, record)
        if (
            not grant.allowed
            or grant.project_id != record.get("project_id")
            or grant.email != record.get("principal")
            or grant.signature != record.get("access_signature")
            or access.memory_epoch(storage, grant) != record.get("memory_epoch", 0)
            or not conversation_memory.is_current(storage, grant, record["thread_key"], record)
        ):
            raise PermissionError("Project access changed. GCS publishing is blocked.")
        write_access.require_active(storage, grant.project_id, body.grant_id, bridge.settings)

    # A retry cannot race another upload of the same event/path. Revokes never wait on this lock.
    mutex = "claude-write:" + event_id + ":" + hashlib.sha256(relative.encode()).hexdigest()
    claim = await bridge.repository.begin_event(mutex, lease_seconds=600)
    if claim.status != "claimed":
        raise HTTPException(409, "This output is already being uploaded. Retry shortly.")
    try:
        return await asyncio.to_thread(
            write_broker.upload,
            storage,
            bridge.settings,
            record["project_id"],
            event_id,
            relative,
            body.grant_id,
            authorize,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await bridge.repository.release_event(mutex, claim.token)
