"""Authenticated dispatch for the Slack-only Claude Spot worker."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from pistachio.agents.analysis import access, identity
from pistachio.agents.analysis.batch import BatchBridge, digest
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
    principal = identity.email_for(services.settings, team, user)
    if bridge.settings.allowed_user_ids and user not in bridge.settings.csv("allowed_user_ids"):
        principal = None
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
    event_id = await bridge.accept(payload)
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


@router.get("/credentials/{event_id}", include_in_schema=False)
async def credentials(event_id: str, request: Request, response: Response) -> dict[str, str]:
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
    ):
        raise HTTPException(403, "This worker grant is inactive")
    # Only the trusted worker bootstrap receives these. Never put them in GCS or Batch job metadata.
    response.headers["Cache-Control"] = "no-store"
    return {
        "ANTHROPIC_API_KEY": services.settings.anthropic_api_key.get_secret_value(),
        "SLACK_BOT_TOKEN": services.settings.slack_bot_token,
    }
