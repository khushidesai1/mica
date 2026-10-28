"""Trusted SDK tools. Analysis processes receive no cloud credentials or network access."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, tool
from claude_agent_sdk.types import Message

from .checkpoints import Stages
from .config import Settings
from .recovery import RecoverCall
from .resources import Sampler
from .tiers import GPU_TIERS, TIERS

REQUEST_TOOL = "mcp__pistachio__recover_worker"
STAGE_TOOL = "mcp__pistachio__checkpoint_stage"


def restored_cost(session_id: str | None, home: Path | None = None) -> float | None:
    """Pinned CLI 2.1.286 restores this cumulative total from its transcript.

    Read only the tail of the exact session file; no user prose is used for accounting.
    Missing accounting remains unknown, never an invented zero.
    """
    if session_id is None:
        return 0.0
    if not re.fullmatch(r"[a-f0-9-]{36}", session_id):
        return None
    root = (home or Path.home()) / ".claude/projects"
    files = list(root.glob(f"*/{session_id}.jsonl"))
    if len(files) != 1 or files[0].is_symlink():
        return None
    try:
        with files[0].open("rb") as stream:
            stream.seek(max(0, files[0].stat().st_size - 2 * 1024**2))
            lines = stream.read().splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "cost-state" and record.get("sessionId") == session_id:
                value = record.get("totalCostUSD")
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    return float(value)
    except OSError:
        pass
    return None


class RecoveryManager:
    def __init__(
        self,
        settings: Settings,
        manifest: dict[str, Any],
        sampler: Sampler,
        operate: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        save_stage: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        status: Callable[[str], Awaitable[None]] | None = None,
        save_result: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        stages: Stages | None = None,
    ) -> None:
        self.settings, self.manifest, self.sampler = settings, manifest, sampler
        self.operate, self.save_stage, self.status = operate, save_stage, status
        self.save_result, self.stages = save_result, stages
        self.attempt = int(manifest["attempt"])
        self.tier = str(manifest["tier"])
        self.pending = False
        self.handoff = ""
        self.session_id: str | None = None
        self.model_cost: float | None = None
        self.sdk_cost_total: float | None = None
        self.stop_reason = ""
        self.stop = asyncio.Event()
        self.poll_addenda: Callable[[], Awaitable[str]] | None = None
        self.persist_resources: Callable[[], Awaitable[None]] | None = None
        self.stop_tools: Callable[[], Awaitable[None]] | None = None
        self.started = time.time()
        self.job_deadline = min(
            float(manifest["request_deadline"]), self.started + settings.max_run_seconds
        )
        self.deadline = self.job_deadline

    def request_stop(self, reason: str) -> None:
        if not self.stop.is_set():
            self.stop_reason = reason
            self.stop.set()

    def prompt(self) -> str:
        shape = (
            GPU_TIERS[self.manifest.get("gpu_type", "l4")]
            if self.tier == "gpu"
            else TIERS[self.tier]
        )
        mode = "on-demand" if self.manifest["provisioning"] == "STANDARD" else "Spot"
        history = "\n".join(self.manifest.get("worker_history", []))
        remaining = max(0, int(self.manifest.get("request_deadline", time.time()) - time.time()))
        recovery_policy = (
            "This GPU worker cannot resize or move to a CPU tier. Chunk work within the "
            "confirmed resources; save progress and explain if the request cannot fit. "
            "Verified Spot interruptions and checkpointed continuations keep the same GPU "
            "tier, mode and total approved allowance. "
            if self.tier == "gpu"
            else (
                "If measured input requirements or resource feedback show the next step cannot "
                "fit, "
                "call recover_worker with the evidence and remaining steps. The application can "
                "automatically continue the same request on a suitable larger worker once. "
            )
        )
        return (
            "\n## Worker (trusted application policy)\n"
            f"Worker: {shape.name}, {shape.machine}, {shape.vcpus} CPUs, "
            f"{shape.memory_gib} GiB RAM, {mode}. "
            f"Launch reason: {self.manifest.get('launch_reason') or 'ordinary analysis'}.\n"
            f"Approved time remaining at launch: {remaining} seconds. "
            "Use compute_allowance for the current remaining time during long work. "
            "This worker was sized to finish the job. Run the requested work directly. "
            "Set explicit Bash timeouts within the remaining request deadline. "
            f"{recovery_policy}"
            "Never deliberately start undersized, silently reduce scientific scope, or ask the "
            "user to resubmit for a larger worker. "
            "High RAM alone is not proof of insufficient capacity. "
            "Cancelled and stopped worker_history rows are not resource failures. "
            "Only outcome=oom or oom_kills > 0 establishes an OOM. "
            "Check per-task RAM and concurrency; fewer parallel processes may be appropriate. "
            "Save completed expensive stages under checkpoints/ with checkpoint_stage after "
            "all writers close. Include input versions, method and parameters in fingerprints; "
            "validate them before reuse. Scratch and running processes do not survive a restart. "
            "Durable saved stages survive a completed or stopped request in this project/thread. "
            "Use saved_checkpoints to list or restore them before recomputing expensive work. "
            "Cellpose results save automatically; general scripts must save completed stages. "
            "Runtime estimates are advisory: long healthy processes may continue to the approved "
            "deadline. If more time is needed, save progress and ask for an additional allowance; "
            "Do not silently drop scope or repeat saved computation. "
            "Use recover_worker cause=continue "
            "only at a saved boundary when a fresh worker is needed within this same allowance. "
            "Keep commands foreground and preserve sandbox network and local-binding rules.\n"
            + history
        )

    def tools(self) -> list[SdkMcpTool[Any]]:
        schema = RecoverCall.model_json_schema()
        schema["properties"].pop("attempt")
        schema["required"].remove("attempt")

        @tool(
            "recover_worker",
            "Save progress and recover from a demonstrated capacity shortfall.",
            schema,
        )
        async def request(args: dict[str, Any]) -> dict[str, Any]:
            return await self.call(args)

        result = [request]

        @tool("compute_allowance", "Read the approved deadline and remaining compute time.", {})
        async def allowance(_args: dict[str, Any]) -> dict[str, Any]:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "deadline_unix": self.deadline,
                                "remaining_seconds": max(0, int(self.deadline - time.time())),
                                "model_spent_this_worker_usd": self.model_cost,
                                "policy": (
                                    "Continue healthy work within this allowance. "
                                    "Save completed stages and ask for a new allowance "
                                    "before more work if the remaining time is insufficient."
                                ),
                            }
                        ),
                    }
                ]
            }

        result.append(allowance)
        if self.stages:

            @tool(
                "saved_checkpoints",
                "List durable stages in this project/thread, or restore one by checkpoint_id. "
                "Validate its input/method fingerprint before reuse. Page with offset; "
                "existing files are preserved.",
                {
                    "type": "object",
                    "properties": {
                        "checkpoint_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                        "offset": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": False,
                },
            )
            async def saved(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    assert self.stages is not None
                    value = (
                        await asyncio.to_thread(
                            self.stages.restore_saved, str(args["checkpoint_id"])
                        )
                        if args.get("checkpoint_id")
                        else await asyncio.to_thread(
                            self.stages.available, int(args.get("offset", 0))
                        )
                    )
                    return {"content": [{"type": "text", "text": json.dumps(value)}]}
                except (ValueError, OSError) as exc:
                    return {"isError": True, "content": [{"type": "text", "text": str(exc)[:500]}]}

            result.append(saved)
        if self.save_stage:

            @tool(
                "checkpoint_stage",
                "Save a completed, closed scientific stage for recovery.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 300},
                        "fingerprint": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                    "required": ["path", "fingerprint"],
                    "additionalProperties": False,
                },
            )
            async def stage(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    assert self.save_stage is not None
                    value = await self.save_stage(args)
                    return {"content": [{"type": "text", "text": json.dumps(value)}]}
                except (ValueError, OSError) as exc:
                    return {"isError": True, "content": [{"type": "text", "text": str(exc)[:500]}]}

            result.append(stage)
        return result

    async def call(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            body = RecoverCall.model_validate({**args, "attempt": self.attempt})
            value = await self.operate(body.model_dump())
            if value["status"] == "checkpoint":
                self.pending = True
                self.handoff = str(args.get("handoff", ""))
                self.stop_reason = "worker_continuation"
                self.stop.set()
            if value.get("notice") and self.status:
                with suppress(Exception):  # Status delivery cannot undo accepted intent.
                    await self.status(value["notice"])
            return {"content": [{"type": "text", "text": json.dumps(value)}]}
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)[:500]}]}

    def deny(self, name: str, args: dict[str, Any]) -> str:
        if self.stop.is_set() and not self.pending:
            return "This run has stopped; no further tools may run."
        if self.pending:
            return "This attempt is checkpointing; no further tools may run."
        remaining = self.deadline - time.time()
        if remaining <= 0:
            return "This attempt reached its authorized time allowance."
        if name == "Bash":
            timeout = args.get("timeout", 120000)
            if not isinstance(timeout, (int, float)) or timeout < 1:
                return "Supply a positive explicit Bash timeout."
            if timeout > remaining * 1000:
                return "This timeout exceeds the remaining authorized attempt duration."
        return ""


async def run_query(
    prompt: str, options: ClaudeAgentOptions, manager: RecoveryManager
) -> AsyncIterator[Message]:
    """An application-controlled interrupt; never rely solely on hook stop semantics."""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async def interrupt() -> None:
            while not manager.stop.is_set():
                remaining = manager.deadline - time.time()
                if remaining <= 0:
                    manager.stop_reason = "compute_time_allowance"
                    manager.stop.set()
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(manager.stop.wait(), timeout=min(5, remaining))
            if manager.stop_reason != "worker_continuation" and manager.stop_tools:
                with suppress(Exception):
                    await asyncio.wait_for(manager.stop_tools(), 5)
            # A pending in-process MCP tool can hold the interrupt response open.
            # Bound the interrupt itself, not only the later disconnect.
            with suppress(Exception):
                await asyncio.wait_for(client.interrupt(), 5)
            # Broken/unsupported interrupt must not leave paid work running indefinitely.
            await asyncio.sleep(30)
            with suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5)

        stopper = asyncio.create_task(interrupt())
        try:
            async for message in client.receive_response():
                yield message
        finally:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper
