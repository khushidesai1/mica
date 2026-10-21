"""Run-bound transfer credentials, isolated from general analysis worker grants."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from typing import TYPE_CHECKING, Any

from .atomx_connections import digest

if TYPE_CHECKING:
    from .transfers import Transfers


async def authorize(service: Transfers, run: str, token: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", run) or not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("Inactive transfer grant")
    grant = await service.repo.get_connector_state("atomx-run:" + run)
    if (
        not grant
        or grant["expires"] <= time.time()
        or not hmac.compare_digest(grant["token_hash"], digest(token))
    ):
        raise ValueError("Inactive transfer grant")
    pending = await service.pending(grant["pending"])
    if (
        pending.get("job_run") != run
        or pending.get("job_terminal")
        or pending["state"] not in {"prepare", "submitting", "inventory", "running"}
        or pending.get("cancelled_by")
        or pending["requested_by"] != grant["owner"]
        or pending["login_version"] != grant["revision"]
        or pending["job_kind"] != grant["kind"]
    ):
        raise ValueError("Inactive transfer grant")
    if grant["kind"] == "pull" and pending.get("confirmed_by") != grant["owner"]:
        raise ValueError("Transfer has not been confirmed")
    await asyncio.to_thread(service.authorize, pending["project"], grant["owner"], pending)
    connection = await service.connections.get(grant["owner"])
    if connection.get("revision") != grant["revision"] or connection.get("status") not in {
        "saved",
        "valid",
    }:
        raise ValueError("Personal connection changed")
    return pending


def writer_token(service: Transfers, pending: dict[str, Any]) -> dict[str, Any]:
    """A five-minute token limited to this sample's destinations, never a shared writer key."""
    import google.auth
    from google.auth import downscoped, impersonated_credentials
    from google.auth.transport.requests import Request

    config = pending["config"]
    sample = pending["request"]["as_sample"] or pending["request"]["sample"]
    base = f"projects/_/buckets/{config['bucket']}/objects/"
    prefixes = [base + config[key] + sample + "/" for key in ("raw_prefix", "additional_prefix")]
    exact = [
        base + config["status_prefix"] + sample + suffix
        for suffix in (".in_progress.json", ".done.json", ".failed.json")
    ]
    condition = " || ".join(
        [f"resource.name.startsWith({json.dumps(p)})" for p in prefixes]
        + [f"resource.name == {json.dumps(p)}" for p in exact]
    )
    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])  # type: ignore[no-untyped-call]
    writer = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service.settings.write_service_account,
        target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
        lifetime=300,
    )  # type: ignore[no-untyped-call]
    boundary = downscoped.CredentialAccessBoundary(
        rules=[
            downscoped.AccessBoundaryRule(  # type: ignore[no-untyped-call]
                available_resource=f"//storage.googleapis.com/projects/_/buckets/{config['bucket']}",
                available_permissions=["inRole:roles/storage.objectAdmin"],
                availability_condition=downscoped.AvailabilityCondition(expression=condition),  # type: ignore[no-untyped-call]
            )
        ]
    )
    credentials = downscoped.Credentials(writer, boundary)  # type: ignore[no-untyped-call]
    credentials.refresh(Request())  # type: ignore[no-untyped-call]
    if credentials.expiry is None or not credentials.token:
        raise ValueError("Writer token unavailable")
    return {"token": credentials.token, "expires": credentials.expiry.isoformat()}
