"""Narrow private AtoMx connection and approval form, with no general web UI."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from mica.agents.analysis import atomx_connections
from mica.agents.analysis.batch import BatchBridge
from mica.agents.analysis.storage import GCSStorage
from mica.agents.analysis.transfers import Transfers
from mica.api.connection_forms import body, origin, page_response, response
from mica.api.dependencies import Services
from mica.api.migration import require

router = APIRouter(prefix="/integrations/atomx", include_in_schema=False)
COOKIE = "mica_atomx_session"
PAGE = Path(__file__).with_name("atomx_form.html").read_text()


def service(request: Request) -> tuple[Transfers, atomx_connections.Connections]:
    services: Services = request.app.state.services
    bridge = BatchBridge(services.settings, services.repository)
    transfer = Transfers(bridge, GCSStorage(services.settings.gcs_bucket))
    return transfer, atomx_connections.Connections(
        services.settings, bridge.settings, services.repository
    )


@router.get("")
async def page() -> Response:
    return page_response(PAGE)

@router.post("/exchange")
async def exchange(request: Request) -> Response:
    _, connections = service(request)
    try:
        origin(request, connections.app.control_plane_url)
        value = await body(request, 512)
        await connections.validate_link(str(value.get("token", "")))
        await require(connections.app, connections.repo)
        token = await connections.exchange(str(value.get("token", "")))
        result = response({"status": "ready"})
        result.set_cookie(
            COOKIE,
            token,
            max_age=atomx_connections.LIFETIME,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/integrations/atomx",
        )
        return result
    except (ValueError, KeyError, TypeError):
        return response(
            {
                "detail": (
                    "This private link is unavailable, expired or already opened. Ask "
                    "Mica for a fresh link."
                )
            },
            400,
        )


@router.get("/session")
async def session(request: Request) -> Response:
    transfer, connections = service(request)
    try:
        token = request.cookies.get(COOKIE, "")
        value = await connections.session(token)
        current = await connections.status(value["owner"])
        detail = (
            "Save or replace your existing AtoMx username and password. Mica "
            "stores them encrypted for your account only. Saved credentials "
            "have not been tested until a transfer connects."
        )
        if value["purpose"] == "approval":
            pending = await transfer.pending(value["pending"])
            if pending["requested_by"] != value["owner"] or pending["state"] != "preview":
                raise ValueError("Approval unavailable")
            transfer.authorize(pending["project"], value["owner"], pending)
            detail = pending["preview_text"]
        return response(
            {
                "owner": value["owner"],
                "host": connections.provider(),
                "purpose": value["purpose"],
                "detail": detail,
                "saved": current.get("status") in {"saved", "valid", "invalid"},
                "csrf": connections.mac("csrf", token),
            }
        )
    except (ValueError, KeyError, TypeError):
        return response(
            {"detail": "This private session is unavailable. Ask Mica for a fresh link."}, 400
        )


@router.post("/action")
async def action(request: Request) -> Response:
    transfer, connections = service(request)
    try:
        origin(request, connections.app.control_plane_url)
        token = request.cookies.get(COOKIE, "")
        if not token or not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), connections.mac("csrf", token)
        ):
            raise ValueError("Invalid form session")
        await connections.session(token)
        await require(connections.app, connections.repo)
        key = "atomx-session:" + hashlib.sha256(token.encode()).hexdigest()
        async with connections.locked(key):
            value = await connections.session(token)
            submitted = await body(request)
            selected = submitted.get("action")
            if value["purpose"] == "connection" and selected == "save":
                await connections.save(
                    value["owner"],
                    value["revision"],
                    str(submitted.get("username", "")),
                    str(submitted.get("password", "")),
                )

            elif value["purpose"] == "connection" and selected == "forget":
                await connections.forget(value["owner"], value["revision"])

            elif value["purpose"] == "approval" and selected in {"confirm", "cancel"}:
                if selected == "confirm":
                    await transfer.confirm(value["pending"], value["owner"])
                else:
                    await transfer.cancel(value["pending"], value["owner"])
            else:
                raise ValueError("This action is unavailable")
            value["used"] = True
            await connections.repo.set_connector_state(key, value)
        # Pending jobs recheck the revision on their next durable tick, and all new
        # bootstrap/token requests are immediately denied for the old revision.
        return response({"status": "complete"})
    except (ValueError, PermissionError, KeyError, TypeError):
        return response(
            {
                "detail": (
                    "This action could not complete. Your connection or access may "
                    "have changed; ask Mica for a fresh link."
                )
            },
            400,
        )


async def worker(request: Request, run: str) -> tuple[Transfers, dict[str, Any]]:
    import asyncio

    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    from mica.agents.analysis import atomx_worker

    transfer, _ = service(request)
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            request.headers.get("authorization", "").removeprefix("Bearer "),
            GoogleRequest(),  # type: ignore[no-untyped-call]
            audience=transfer.app.control_plane_url,
        )
        if claims.get("email") != (
            transfer.settings.transfer_service_account or transfer.app.batch_service_account
        ) or not claims.get("email_verified"):
            raise ValueError("Untrusted worker")
        await require(transfer.app, transfer.bridge.repository, draining=True)
        pending = await atomx_worker.authorize(
            transfer, run, request.headers.get("x-mica-transfer-grant", "")
        )
        return transfer, pending
    except (ValueError, KeyError, TypeError):
        raise HTTPException(403, "Inactive transfer grant") from None


@router.post("/worker/{run}/bootstrap")
async def bootstrap(request: Request, run: str) -> Response:
    transfer, pending = await worker(request, run)
    login = await transfer.connections.decrypt(pending["requested_by"], pending["login_version"])
    return response(
        {
            "login": login,
            "sftp": {
                "host": transfer.settings.transfer_host,
                "port": transfer.settings.transfer_port,
                "host_key": transfer.settings.transfer_host_key,
            },
            "run_id": run,
            "requested_by": pending["requested_by"],
            "connection_revision": pending["login_version"],
            "destinations": pending["config"],
            "kind": pending["job_kind"],
            # A pull copies the previewed AtoMx folder; an inventory searches by request name.
            "sample": (
                pending["preview"]["sample"]
                if pending["job_kind"] == "pull"
                else pending["request"]["sample"]
            ),
            "as_sample": pending["request"]["as_sample"] or pending["request"]["sample"],
            "export_root": pending.get("preview", {}).get(
                "export_root", pending["request"]["export_root"]
            ),
            "plan_fingerprint": pending.get("preview", {}).get("plan_fingerprint"),
        }
    )


@router.post("/worker/{run}/gcs-token")
async def gcs_token(request: Request, run: str) -> Response:
    import asyncio

    from mica.agents.analysis import atomx_worker

    transfer, pending = await worker(request, run)
    if pending["job_kind"] != "pull":
        raise HTTPException(403, "Inventory has no writer grant")
    return response(await asyncio.to_thread(atomx_worker.writer_token, transfer, pending))
