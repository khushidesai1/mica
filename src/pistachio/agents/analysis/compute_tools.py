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

from .compute_quotes import Estimate
from .config import Settings
from .resources import Sampler
from .tiers import TIERS

ESTIMATE_TOOL = "mcp__pistachio__estimate_worker"
REQUEST_TOOL = "mcp__pistachio__request_worker"
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


class ComputeManager:
    def __init__(
        self,
        settings: Settings,
        manifest: dict[str, Any],
        sampler: Sampler,
        operate: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        save_stage: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        status: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings, self.manifest, self.sampler = settings, manifest, sampler
        self.operate, self.save_stage, self.status = operate, save_stage, status
        self.approved = bool(manifest.get("approved"))
        self.pending = False
        self.handoff = ""
        self.session_id: str | None = None
        self.model_cost: float | None = None
        self.sdk_cost_total: float | None = None
        self.stop_reason = ""
        self.stop = asyncio.Event()
        self.started = time.time()
        probe_seconds = min(
            manifest.get("probe_seconds", 1800), manifest.get("probe_affordable_seconds", 1800)
        )
        self.job_deadline = self.started + max(1, settings.max_run_seconds - 120)
        self.deadline = (
            min(self.job_deadline, self.started + probe_seconds)
            if not self.approved
            else (
                min(
                    self.job_deadline,
                    self.started + manifest.get("duration", settings.max_run_seconds),
                )
            )
        )

    def prompt(self) -> str:
        shape = TIERS[self.settings.tier]
        larger = "; ".join(
            f"{t.name}: {t.vcpus} CPUs/{t.memory_gib} GiB"
            for t in TIERS.values()
            if t.vcpus > shape.vcpus
        )
        free = self.sampler.sample().free
        mode = "on-demand" if self.manifest.get("provisioning", "SPOT") == "STANDARD" else "Spot"
        threshold = self.manifest.get("on_demand_step_minutes")
        reason = (self.manifest.get("launch_reason") or "default worker selection").rstrip(".")
        total = self.manifest.get("on_demand_total_hours", 4)
        provisioning_policy = (
            f"This worker was chosen at launch: {mode}, {shape.name} ({reason}). "
            "Still estimate before long commands; on the same tier and mode, request_worker "
            "continues here without moving. Ask for a different worker only if the work doesn't "
            "fit in memory or the launch choice is clearly wrong. On-demand is for one long "
            "uninterrupted process (training, full-sample segmentation or ingest, or any single "
            f"command of {threshold}+ minutes). Work made of shorter commands stays on Spot "
            f"unless the total exceeds {total:g} hours. Save each unit's result in the workspace "
            "as it finishes. Set longest_step_minutes to the longest single command, not the "
            "total.\n"
            if threshold is not None
            else ""
        )
        history = "\n".join(self.manifest.get("worker_history", []))
        if history:
            history += "\n"
        return (
            "\n## Compute (trusted application policy)\n"
            f"Worker: {shape.name}, {shape.machine}, {mode}; effective {self.sampler.cpus:g} CPUs, "
            f"{self.sampler.memory_gib or shape.memory_gib:.1f} GiB RAM; "
            f"observed free workspace disk {free if free is not None else 'unknown'} GB.\n"
            f"Available larger tiers: {larger or 'none'}. "
            f"Automatic infrastructure limit for this entire request: "
            f"${self.manifest.get('limit', 0):.2f}; prior spend/reservations "
            f"${self.manifest.get('spent', 0):.2f}. Model budget is separate.\n"
            "Before work over ten minutes or half the available memory, probe a representative "
            "subset (including dense inputs), then call estimate_worker with remaining time, "
            "memory and disk requirements on the requested tier. Do not assume linear CPU scaling. "
            "Call request_worker with its quote ID to authorize the expensive step or resize. "
            "The application asks the user only when the shared budget is insufficient. "
            "An out-of-range estimate means no fit; do not force it onto xlarge.\n"
            f"{provisioning_policy}"
            f"{history}"
            f"Probe with {self.settings.sci_python} {self.settings.tools_dir}/measure.py "
            "--label analysis -- <command>. Check individual task memory as well as total RAM. "
            "Probes are bounded; do not launch the full pipeline to estimate it.\n"
            f"Bash ceiling for this attempt: {self.settings.max_run_seconds} seconds, including "
            "time already used. Set an explicit timeout from the estimate plus margin, capped "
            "by remaining time. A chosen ten-minute timeout is your own choice, not a platform "
            "limit. OOM, preemption and cancellation can stop work. Keep commands foreground.\n"
            "Before request_worker, save scripts and give a handoff describing completed work and "
            "exact remaining steps. Put disposable data/spill under scratch/ (lost on resize). "
            "Put completed, closed stages under checkpoints/ and call checkpoint_stage to save "
            "them. A running stage cannot resume mid-process; only verified saved stages survive. "
            "Use fewer Dask workers if an individual task needs more RAM; never bypass sandbox "
            "network or local-binding controls. Verify requested outputs before claiming success.\n"
        )

    def tools(self) -> list[SdkMcpTool[Any]]:
        schema = Estimate.model_json_schema()
        schema["required"].append("longest_step_minutes")
        # Stored quotes may omit this field; new agent estimates must supply a number.
        schema["properties"]["longest_step_minutes"] = schema["properties"]["longest_step_minutes"][
            "anyOf"
        ][0]

        @tool(
            "estimate_worker",
            "Quote remaining compute from measured workload evidence.",
            schema,
        )
        async def estimate(args: dict[str, Any]) -> dict[str, Any]:
            if args.get("longest_step_minutes") is None:
                raise ValueError("Estimate the longest single command in longest_step_minutes.")
            return await self.call(
                "estimate", {"estimate": Estimate.model_validate(args).model_dump()}
            )

        @tool(
            "request_worker",
            "Accept a compute quote and continue or checkpoint for resizing.",
            {
                "type": "object",
                "properties": {
                    "quote_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                    "handoff": {"type": "string", "minLength": 1, "maxLength": 3000},
                },
                "required": ["quote_id", "handoff"],
                "additionalProperties": False,
            },
        )
        async def request(args: dict[str, Any]) -> dict[str, Any]:
            if set(args) != {"quote_id", "handoff"}:
                return {"isError": True, "content": [{"type": "text", "text": "Invalid request."}]}
            return await self.call("request", args)

        result = [estimate, request]
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

    async def call(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            value = await self.operate({"action": action, "attempt": self.settings.attempt, **args})
            if value["status"] == "continue":
                self.approved = True
                self.deadline = min(self.job_deadline, float(value["deadline"]))
            elif value["status"] == "checkpoint":
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
            if not self.approved and timeout > 600000:
                return "Probe first; call estimate_worker and request_worker before a long command."
        return ""


async def run_query(
    prompt: str, options: ClaudeAgentOptions, manager: ComputeManager
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
            await client.interrupt()
            # Broken/unsupported interrupt must not leave paid work running indefinitely.
            await asyncio.sleep(30)
            await client.disconnect()

        stopper = asyncio.create_task(interrupt())
        try:
            async for message in client.receive_response():
                yield message
        finally:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper
