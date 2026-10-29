"""Operator-controlled admission and durable Slack intake during a stack migration.

Freezing stops new API writes, not already-running workers or requests. The operator
must drain those before copying data. The Slack inbox lives only in shared Firestore;
it is the deliberate exception to a frozen service's write prohibition.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Annotated, Any, Literal, cast

from fastapi import Depends, HTTPException, Request

from mica.api.auth import Principal, get_principal
from mica.config import Settings
from mica.storage.base import Repository

Mode = Literal["open", "draining", "frozen"]
PREFIX = "migration-slack:"
MAX_PAYLOAD_BYTES = 256_000
RETRY_HEADERS = {"Retry-After": "60", "Cache-Control": "no-store"}


def unavailable() -> HTTPException:
    return HTTPException(
        503,
        "Mica is temporarily undergoing maintenance. Please retry shortly.",
        headers=RETRY_HEADERS,
    )


async def mode(settings: Settings, repository: Repository) -> Mode:
    selected: Any = settings.migration_default_mode
    if settings.migration_service:
        try:
            state = await repository.get_connector_state(
                f"migration-admission:{settings.migration_service}"
            )
        except Exception:
            raise unavailable() from None
        if state is not None:
            selected = state.get("mode")
    if not isinstance(selected, str) or selected not in {"open", "draining", "frozen"}:
        raise unavailable()
    return cast(Mode, selected)


async def require(
    settings: Settings, repository: Repository, *, draining: bool = False
) -> None:
    selected = await mode(settings, repository)
    if selected != "open" and not (draining and selected == "draining"):
        raise unavailable()


async def admit(request: Request, *, draining: bool = False) -> None:
    services = request.app.state.services
    await require(services.settings, services.repository, draining=draining)


async def api_principal(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> Principal:
    """Authenticate before admission; only existing-work callbacks may drain."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return principal
    settings = request.app.state.services.settings
    path = request.url.path
    if (
        path.startswith("/api/v1/internal/")
        and settings.env in {"staging", "production"}
        and principal.id != settings.cloud_tasks_service_account.lower()
    ):
        raise HTTPException(403, "Untrusted internal principal")
    draining = path in {
        "/api/v1/internal/batch/completion",
        "/api/v1/internal/tools/build-completion",
        "/api/v1/internal/maintenance/reconcile-batch",
    } or bool(re.fullmatch(r"/api/v1/internal/tasks/[^/]+/process", path)) or path.startswith(
        "/api/v1/internal/maintenance/replay-slack/"
    )
    await admit(request, draining=draining)
    return principal


async def buffer_slack(request: Request, payload: dict[str, Any]) -> bool:
    """Called only after Slack signature verification; acknowledge only durable data."""
    services = request.app.state.services
    if await mode(services.settings, services.repository) == "open":
        return False
    if payload.get("type") != "event_callback":
        return True
    event = payload.get("event", {})
    if (
        not isinstance(event, dict)
        or event.get("bot_id")
        or event.get("type") not in {"message", "app_mention"}
        or event.get("subtype") not in {None, "file_share"}
    ):
        return True
    if not all(isinstance(payload.get(k), str) and payload[k] for k in ("team_id", "event_id")):
        raise HTTPException(400, "Slack event has no durable identity")
    if not re.fullmatch(r"[A-Z0-9]+", str(event.get("channel", ""))) or not re.fullmatch(
        r"\d+\.\d+", str(event.get("ts", ""))
    ):
        raise HTTPException(400, "Invalid Slack event")
    # Store only fields used by intake. Rich-text blocks can duplicate password text.
    saved = {
        k: payload[k] for k in ("type", "team_id", "event_id", "authorizations") if k in payload
    }
    saved["event"] = {
        k: event[k]
        for k in ("type", "channel", "channel_type", "user", "ts", "thread_ts", "text",
                  "files", "subtype")
        if k in event
    }
    from mica.agents.analysis.batch import BatchBridge
    from mica.agents.analysis.transfers import scrub_password_paste

    saved = await scrub_password_paste(
        BatchBridge(services.settings, services.repository), saved
    )
    if len(json.dumps(saved).encode()) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "Slack event exceeds the maintenance inbox limit")
    # Keep event IDs distinct: Slack can send both message and app_mention for
    # one message, and normal intake deliberately ignores one of those shapes.
    # The ordinary intake receipt still deduplicates actual work by message ts.
    identity = f"{payload['team_id']}:{payload['event_id']}"
    identifier = hashlib.sha256(identity.encode()).hexdigest()
    key = PREFIX + identifier
    repository: Repository = services.repository
    claim = await repository.begin_event(key, lease_seconds=60)
    if claim.status != "claimed":
        raise unavailable()
    try:
        if not await repository.get_connector_state(key):
            await repository.set_connector_state(
                key,
                {"status": "buffered", "created": time.time(), "payload": saved,
                 "source_service": services.settings.migration_service},
            )
    finally:
        await repository.release_event(key, claim.token)
    return True


async def replay_slack(request: Request, identifier: str) -> dict[str, str]:
    """The authenticated operator replays one receipt, retaining it on any failure."""
    services = request.app.state.services
    await admit(request, draining=True)
    if not re.fullmatch(r"[a-f0-9]{64}", identifier):
        raise HTTPException(400, "Invalid migration receipt")
    repository: Repository = services.repository
    owner = await repository.get_connector_state("migration-replay") or {}
    if not services.settings.migration_service or owner.get("service") != (
        services.settings.migration_service
    ):
        raise HTTPException(409, "This service does not own migration replay")
    # One replayer across both stacks preserves ordering when the CLI drains oldest first.
    mutex = "migration-replay-lock"
    claim = await repository.begin_event(mutex, lease_seconds=300)
    if claim.status != "claimed":
        raise unavailable()
    try:
        await admit(request, draining=True)
        owner = await repository.get_connector_state("migration-replay") or {}
        if owner.get("service") != services.settings.migration_service:
            raise HTTPException(409, "Migration replay ownership changed")
        key = PREFIX + identifier
        receipt = await repository.get_connector_state(key)
        if receipt is None:
            raise HTTPException(404, "Migration receipt not found")
        if receipt.get("status") == "replayed":
            return {"status": "replayed"}
        from mica.connectors.slack import dispatch_slack_event

        try:
            async with asyncio.timeout(240):
                await dispatch_slack_event(services, receipt["payload"])
        except TimeoutError:
            raise unavailable() from None
        # Intake's normal idempotency repairs a crash between enqueue and this receipt.
        await repository.set_connector_state(
            key,
            {"status": "replayed", "created": receipt["created"],
             "replayed": time.time(), "service": services.settings.migration_service},
        )
        return {"status": "replayed"}
    finally:
        await repository.release_event(mutex, claim.token)
