"""Private GitHub consent entry and requester-bound OAuth callback."""

from __future__ import annotations

import asyncio
import hmac
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from slack_sdk.web.async_client import AsyncWebClient

from mica.agents.analysis import access
from mica.agents.analysis.batch import BatchBridge
from mica.agents.analysis.connections import deliver_private
from mica.agents.analysis.github_connections import Connections
from mica.api.connection_forms import HEADERS, body, origin, page_response, response
from mica.api.migration import require

router = APIRouter(prefix="/integrations/github", include_in_schema=False)
COOKIE, BINDING = "mica_github_session", "mica_github_binding"
PATH = "/integrations/github"
PAGE = Path(__file__).with_name("github_form.html").read_text()


def service(request: Request) -> tuple[BatchBridge, Connections]:
    services = request.app.state.services
    bridge = BatchBridge(services.settings, services.repository)
    if not bridge.settings.pull_requests:
        raise ValueError("GitHub connections are not enabled.")
    return bridge, Connections(services.settings, bridge.settings, services.repository)


async def check_origin_event(connection: Connections, event_id: str) -> dict[str, Any]:
    record = await connection.repo.get_connector_state("claude-event:" + event_id)
    if not record:
        raise ValueError("The original connection request is unavailable")
    connection.enabled(record)
    return record


@router.get("")
async def page() -> Response:
    return page_response(PAGE)


@router.post("/exchange")
async def exchange(request: Request) -> Response:
    try:
        _, connection = service(request)
        origin(request, connection.app.control_plane_url)
        value = await body(request, 512)
        _, link = await connection.validate_link(str(value.get("token", "")))
        await check_origin_event(connection, link["event"])
        await require(connection.app, connection.repo)
        token = await connection.exchange(str(value.get("token", "")))
        result = response({"status": "ready"})
        result.set_cookie(
            COOKIE, token, max_age=900, secure=True, httponly=True, samesite="strict", path=PATH
        )
        return result
    except (ValueError, KeyError, TypeError):
        return response(
            {"detail": "Request a fresh private GitHub connection link from Mica."}, 400
        )


@router.get("/session")
async def session(request: Request) -> Response:
    try:
        _, connection = service(request)
        token = request.cookies.get(COOKIE, "")
        row = await connection.session(token)
        await check_origin_event(connection, row["event"])
        return response({"owner": row["owner"], "csrf": connection.mac("csrf", token)})
    except (ValueError, KeyError, TypeError):
        return response(
            {"detail": "Request a fresh private GitHub connection link from Mica."}, 400
        )


@router.post("/start")
async def start(request: Request) -> Response:
    try:
        _, connection = service(request)
        origin(request, connection.app.control_plane_url)
        token = request.cookies.get(COOKIE, "")
        if not token or not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), connection.mac("csrf", token)
        ):
            raise ValueError("Invalid form session")
        row = await connection.session(token)
        await check_origin_event(connection, row["event"])
        await require(connection.app, connection.repo)
        url, binding = await connection.start(token)
        result = response({"url": url})
        result.set_cookie(
            BINDING, binding, max_age=600, secure=True, httponly=True, samesite="lax", path=PATH
        )
        return result
    except (ValueError, KeyError, TypeError):
        return response(
            {"detail": "This connection session changed or expired. Start again from Slack."}, 400
        )


@router.get("/callback")
async def callback(request: Request) -> Response:
    try:
        bridge, connection = service(request)
        await require(connection.app, connection.repo)
        query = request.query_params

        async def authorize(owner: str, event: str) -> None:
            record = await check_origin_event(connection, event)
            grant = await asyncio.to_thread(
                access.resolve, bridge.app, bridge.settings, bridge.storage, record
            )
            if grant.email != owner or not grant.private:
                raise ValueError("The original requester is no longer verified")

        result = await connection.callback(
            query.get("state", ""),
            request.cookies.get(BINDING, ""),
            query.get("code", ""),
            query.get("error", ""),
            authorize=authorize,
        )
        message = (
            f"Connected GitHub @{result['login']} to {result['owner']}. Return to Mica in Slack."
        )
        # The saved connection is authoritative; a failed receipt cannot roll it back.
        try:
            record = await check_origin_event(connection, result["event"])
            await deliver_private(
                bridge,
                result["owner"],
                record,
                result["event"],
                message,
                "connected-" + result["revision"],
                namespace="github",
                label="GitHub",
                client_factory=AsyncWebClient,
            )
        except Exception:
            message += " Ask Mica for GitHub status if the Slack confirmation does not arrive."
        response_value = HTMLResponse("<p>" + escape(message) + "</p>", headers=HEADERS)
    except Exception:
        # Neither authorization codes nor provider token responses belong in error output.
        response_value = HTMLResponse(
            "<p>GitHub was not connected. Request a fresh connection link from Mica in Slack.</p>",
            status_code=400,
            headers=HEADERS,
        )
    response_value.delete_cookie(BINDING, path=PATH, secure=True, httponly=True, samesite="lax")
    response_value.delete_cookie(COOKIE, path=PATH, secure=True, httponly=True, samesite="strict")
    return response_value
