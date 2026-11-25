"""Stage text changes and request a review card, without any GitHub credentials."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from claude_agent_sdk import SdkMcpTool, tool
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from . import pull_request_files as files
from .storage import Storage

TOOL_NAME = "mcp__pistachio__propose_pull_request"


class Proposer:
    def __init__(
        self,
        storage: Storage,
        request: dict[str, Any],
        event_id: str,
        control_url: str,
    ) -> None:
        self.storage, self.event_id, self.url = storage, event_id, control_url.rstrip("/")
        self.pin = files.Pin.model_validate(request["pull_request"])
        self.attempt = int(request.get("recovery", {}).get("attempt", 0))
        self.lock = asyncio.Lock()
        self.pending: files.Proposal | None = None

    def prompt(self) -> str:
        return (
            f"PR target: {self.pin.repo} at {self.pin.commit}; edit contrib/"
            f"{self.pin.repo}/, run repository tests offline where possible, "
            "then call propose_pull_request. The requester must approve its patch "
            "before a draft PR opens."
        )

    async def propose(self, work: Path, args: dict[str, Any]) -> dict[str, str]:
        async with self.lock:
            index, base = await asyncio.to_thread(files.load_checkout, self.storage, self.pin)
            changes = await asyncio.to_thread(files.scan_changes, work, self.pin, index, base)
            proposal = files.Proposal(
                id=uuid4().hex,
                commit=self.pin.commit,
                title=args["title"],
                body=args["body"],
                checks=args["checks"],
                changes=changes,
            )
            files.validate(proposal, self.pin, index, base)
            # A network retry of the same proposal must reuse its identity and card.
            if self.pending and proposal.model_dump(exclude={"id"}) == self.pending.model_dump(
                exclude={"id"}
            ):
                proposal = self.pending
            self.pending = proposal
            await asyncio.to_thread(
                self.storage.put,
                files.staged_key(self.event_id, proposal.id),
                files.encoded(proposal),
            )
            credential = await asyncio.to_thread(
                id_token.fetch_id_token,
                GoogleRequest(),  # type: ignore[no-untyped-call]
                self.url,
            )
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{self.url}/api/v1/internal/slack-analysis/pull-request/{self.event_id}",
                    headers={"Authorization": f"Bearer {credential}"},
                    json={"proposal_id": proposal.id, "attempt": self.attempt},
                )
            if response.is_error:
                try:
                    detail = str(response.json().get("detail", "Proposal could not be confirmed"))
                except ValueError:
                    detail = "Proposal could not be confirmed; retry"
                raise ValueError(detail)
            return dict(response.json())

    def tool(self, work: Path) -> SdkMcpTool[Any]:
        @tool(
            "propose_pull_request",
            "Propose changes in this run's contrib checkout. Posts the exact patch for requester "
            "approval; nothing is pushed until approved, and the result is a draft PR. "
            "Provide a title, summary body, and checks you actually ran with their results. "
            "Text files only; no workflows, secrets, executable edits, links "
            "or existing PR updates.",
            {"title": str, "body": str, "checks": str},
        )
        async def propose_pull_request(args: dict[str, Any]) -> dict[str, Any]:
            try:
                result = await self.propose(work, args)
                return {"content": [{"type": "text", "text": json.dumps(result)}]}
            except (ValueError, OSError, httpx.HTTPError) as exc:
                message = (
                    str(exc) if isinstance(exc, ValueError) else "Proposal interrupted; retry."
                )
                return {"isError": True, "content": [{"type": "text", "text": message}]}

        return propose_pull_request
