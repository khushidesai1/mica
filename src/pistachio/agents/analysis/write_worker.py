"""Stage only explicitly selected outputs; the controller authorizes and publishes them."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from .outputs import files_under
from .storage import Storage
from .write_broker import staged_key

if TYPE_CHECKING:
    from .runner import Context, TurnResult

log = logging.getLogger(__name__)


@dataclass
class Publication:
    uploaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def publish(
    storage: Storage,
    request: dict[str, Any],
    event_id: str,
    control_url: str,
    ctx: Context,
    result: TurnResult,
) -> Publication:
    outcome = Publication()
    root = ctx.workdir / "outputs/gcs"
    # Recheck symlinks after computation, and publish only this turn's changed files.
    selected = set(files_under(root))
    paths = [p for p in result.files if p in selected]
    if not paths:
        return outcome
    if result.error:
        outcome.errors.append("GCS files were not uploaded because computation stopped early.")
        return outcome
    grant = request.get("write_grant")
    if not grant:
        outcome.errors.append("GCS files were not uploaded: this run has no write grant.")
        return outcome
    if len(paths) > 50:
        outcome.errors.append("GCS publishing supports 50 files per run. Split the request.")
        return outcome
    url = control_url.rstrip("/")
    try:
        token = await asyncio.to_thread(id_token.fetch_id_token, GoogleRequest(), url)  # type: ignore[no-untyped-call]
        async with httpx.AsyncClient(timeout=300) as client:
            for path in paths:
                relative = str(path.relative_to(root))
                if path.stat().st_size > int(request.get("write_max_mb", 512)) * 1024 * 1024:
                    outcome.errors.append(f"{path.name}: exceeds the GCS file-size limit.")
                    continue
                await asyncio.to_thread(storage.upload, staged_key(event_id, relative), path)
                response = await client.post(
                    f"{url}/api/v1/internal/slack-analysis/write/{event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"relative_path": relative, "grant_id": grant["id"]},
                )
                if response.status_code in {403, 409, 413, 422}:
                    detail = response.json().get("detail", "Upload was refused.")
                    outcome.errors.append(f"{path.name}: {detail}")
                    if response.status_code == 403:
                        break
                    continue
                response.raise_for_status()
                outcome.uploaded.append(str(response.json()["uri"]))
    except Exception:
        log.exception("GCS output publication failed for %s", event_id)
        outcome.errors.append("GCS delivery could not be confirmed; staged files are preserved.")
    return outcome
