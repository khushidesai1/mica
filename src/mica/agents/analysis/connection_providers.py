"""Composition root for personal connection commands; core dispatch stays provider-neutral."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

from . import commands
from .connections import deliver_private

if TYPE_CHECKING:
    from .access import Grant
    from .batch import BatchBridge
    from .storage import Storage


async def command(
    provider: str,
    bridge: BatchBridge,
    storage: Storage,
    cmd: commands.Command,
    grant: Grant,
    record: dict[str, Any],
    event_id: str,
) -> str:
    if provider == "atomx":
        from .transfers import Transfers

        return await Transfers(bridge, storage).command(cmd, grant, record, event_id)
    if provider == "github":
        from .github_connections import Connections

        connection = Connections(bridge.app, bridge.settings, bridge.repository)
        connection.enabled(record)
        if cmd.name == "connect github":
            connection.validate_runtime()
            link = await connection.issue(grant.email, "connection", event_id)
            await deliver_private(
                bridge,
                grant.email,
                record,
                event_id,
                "Connect your GitHub account using this private link (expires in 15 minutes): "
                + link,
                "connection",
                namespace="github",
                label="GitHub",
                client_factory=AsyncWebClient,
            )
            return ("I sent your private GitHub connection link. "
                    "After connecting, resend your PR request.")
        if cmd.name == "disconnect github":
            revoked = await connection.disconnect(grant.email)
            return "GitHub disconnected. Pending PR approvals can no longer run." + (
                ""
                if revoked
                else " Also revoke Mica in GitHub Settings → Applications; "
                "GitHub could not confirm revocation."
            )
        if cmd.name == "github status":
            result = await connection.status(grant.email)
            lines = ["Your GitHub connection: " + result["status"]]
            if result.get("login"):
                lines[0] += " as @" + result["login"]
                lines += result["repositories"]
                lines.append(f"{result['repository_count']} repositories allow PRs.")
            lines.append("Install Mica on selected repositories: " + connection.install_url)
            return "\n".join(lines)
    raise ValueError("Unknown personal connection command")


async def status(
    provider: str, bridge: BatchBridge, storage: Storage, grant: Grant, record: dict[str, Any]
) -> dict[str, Any]:
    if provider == "atomx":
        from .transfers import Transfers

        value = await Transfers(bridge, storage).connections.status(grant.email)
        return {"status": value.get("status") or "not connected"}
    if provider == "github":
        from .github_connections import Connections

        connection = Connections(bridge.app, bridge.settings, bridge.repository)
        connection.enabled(record)
        return await connection.status(grant.email)
    raise ValueError("Unknown personal connection provider")
