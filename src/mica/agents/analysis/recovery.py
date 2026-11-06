"""Price-free, lease-serialized recovery of one logical analysis request."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from .config import CHECKPOINT_PREFIX, MAX_MEMORY_TOTAL_KB, Settings
from .reference_repos import MAX_BYTES as REFERENCE_MAX_BYTES
from .storage import Storage
from .tiers import ORDER, TIERS, Record, gpu_seconds, resolve, selected_gpu, selected_gpu_choice

if TYPE_CHECKING:
    from .batch import BatchBridge

log = logging.getLogger(__name__)

# Request manifests contain up to 16 MiB of project memory and five 2 MiB references,
# unlike the small telemetry/result records read below. JSON escaping can cost six
# bytes per source byte; reserve 8 MiB for file metadata and bounded control state.
# A range read returns only the actual object size, not an allocation of this limit.
REQUEST_JSON_LIMIT = (
    6 * ((MAX_MEMORY_TOTAL_KB + 32768) * 1024 + 5 * REFERENCE_MAX_BYTES) + 8 * 1024 * 1024
)


def event_prefix(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid analysis event ID.")
    return f"{CHECKPOINT_PREFIX}/events/{event_id}"


class RecoverCall(Record):
    attempt: int = Field(ge=0, le=64)
    cause: Literal["capacity", "oom", "disk", "continue"]
    memory_gib: float | None = Field(default=None, gt=0, le=10000)
    task_memory_gib: float | None = Field(default=None, gt=0, le=10000)
    disk_gb: float | None = Field(default=None, gt=0, le=100000)
    evidence: str = Field(min_length=10, max_length=2000)
    handoff: str = Field(min_length=1, max_length=3000)


def initialize(record: dict[str, Any], settings: Settings, *, now: float) -> None:
    if record.get("recovery_version"):
        return
    choice = record.get("launch", {})
    record.update(
        recovery_version=1,
        attempt=0,
        resize_count=0,
        preemption_count=0,
        continuation_count=0,
        tier=choice.get("tier", "standard"),
        provisioning=choice.get("provisioning", "SPOT"),
        launch_reason=choice.get("reason", ""),
        constraints=choice.get("constraints", {}),
        request_deadline=record.get("gpu_approved_at", now)
        + (
            record.get("gpu_approved_seconds", gpu_seconds(record, settings))
            if choice.get("tier") == "gpu"
            else settings.max_run_seconds
        ),
        model_limit=settings.max_budget_usd_per_turn,
        model_spent=0.0,
        attempt_ledger=[],
    )
    if "deadline_at" in record:
        record["request_deadline"] = min(record["request_deadline"], record["deadline_at"])


def remaining_recoveries(record: dict[str, Any], settings: Settings) -> int:
    if record["tier"] == "gpu":
        return int(
            record["provisioning"] == "SPOT"
            and record["preemption_count"]
            < (8 if record.get("launch", {}).get("flexible_compute") else 1)
        )
    maximum = record.get("constraints", {}).get("max_tier", settings.max_tier)
    maximum = ORDER[min(ORDER.index(maximum), ORDER.index(settings.max_tier))]
    resize = not record["resize_count"] and ORDER.index(record["tier"]) < ORDER.index(maximum)
    preempt = record["provisioning"] == "SPOT" and not record["preemption_count"]
    return int(resize) + int(preempt)


def limits(record: dict[str, Any], settings: Settings, *, now: float) -> tuple[int, float]:
    seconds = int(record["request_deadline"] - now)
    remaining = record["model_limit"] - record["model_spent"]
    reserve = record["model_limit"] * 0.1 * remaining_recoveries(record, settings)
    allowance = max(0.0, remaining - reserve)
    if seconds < 1 or allowance < 0.1:
        raise ValueError("This request reached its original time or model allowance.")
    return seconds, allowance


def settle(record: dict[str, Any], cost: float | None) -> None:
    if any(row["attempt"] == record["attempt"] for row in record["attempt_ledger"]):
        return
    reserved = record["model_reserved"]
    known = isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
    charged = float(cost) if known and cost is not None else reserved
    record["model_spent"] += charged
    record["attempt_ledger"].append(
        {
            "attempt": record["attempt"],
            "model_cost": charged,
            "model_unknown": not known,
            "tier": record["tier"],
            "provisioning": record["provisioning"],
        }
    )


def read_json(storage: Storage, key: str, limit: int = 256 * 1024) -> dict[str, Any] | None:
    try:
        raw = storage.read_prefix(key, limit + 1)
        if len(raw) > limit:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, ValueError):
        return None


def measurements(storage: Storage, event_id: str, attempt: int) -> dict[str, Any]:
    data = read_json(storage, f"{event_prefix(event_id)}/resources-a{attempt}.json") or {}
    if data.get("event_id") != event_id or data.get("attempt") != attempt:
        return {}
    return data


def classify(code: int | None, observed: dict[str, Any]) -> str:
    if code == 50001:
        return "preempted"
    kills = observed.get("oom_kills")
    if isinstance(kills, int) and kills > 0:
        return "oom"
    return "failed"


def destination(record: dict[str, Any], body: RecoverCall, settings: Settings) -> str:
    if record["tier"] == "gpu":
        raise ValueError(
            "GPU workers do not resize. Use a smaller crop or batch size and request a new run."
        )
    if record["resize_count"]:
        raise ValueError("The automatic capacity recovery has already been used.")
    maximum = record.get("constraints", {}).get("max_tier", settings.max_tier)
    cap = min(ORDER.index(maximum), ORDER.index(settings.max_tier))
    current = TIERS[record["tier"]]
    memory = max(body.memory_gib or 0, body.task_memory_gib or 0)
    disk = body.disk_gb or 0
    if (
        body.cause != "oom"
        and memory * 1.25 <= current.memory_gib - 6
        and disk * 1.25 <= current.workspace_gb
    ):
        raise ValueError("The supplied requirement fits the current worker; no resize is needed.")
    for name in ORDER[ORDER.index(current.name) + 1 : cap + 1]:
        shape = resolve(name, settings)
        if memory * 1.25 <= shape.memory_gib - 6 and disk * 1.25 <= shape.workspace_gb:
            return name
    raise ValueError(
        f"No permitted larger worker fits the requirement ({memory:g} GiB working memory, "
        f"{disk:g} GB scratch, plus headroom). "
        "A chunked method or a higher service capacity is needed."
    )


def operate(
    event_id: str,
    record: dict[str, Any],
    body: RecoverCall,
    settings: Settings,
    storage: Storage,
    *,
    now: float,
) -> dict[str, Any]:
    if (
        not record.get("recovery_version")
        or body.attempt != record["attempt"]
        or record.get("cancel_requested")
    ):
        raise ValueError("This worker attempt is no longer current.")
    identity = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    pending = record.get("recovery_pending")
    if pending:
        if pending["id"] != identity:
            raise ValueError("A different recovery is already pending.")
        return {"status": "checkpoint"}
    limits(record, settings, now=now)
    if body.cause == "continue":
        if record["tier"] == "gpu" and not record.get("launch", {}).get("flexible_compute"):
            raise ValueError("This legacy GPU approval does not include worker continuations.")
        if record.get("continuation_count", 0) >= 32 or record["attempt"] >= 64:
            raise ValueError("This request has used its continuation allowance.")
        # Continue only at a saved boundary; no concurrent replacement or arbitrary resize.
        saved = verified_resume(storage, event_id, body.attempt)
        if not saved or not saved.get("stages", {}).get("stages"):
            raise ValueError("Save a completed stage before requesting continuation.")
        record["recovery_pending"] = {
            "id": identity,
            "attempt": body.attempt,
            "tier": record["tier"],
            "provisioning": record["provisioning"],
            "cause": "continue",
            "handoff": body.handoff,
            "evidence": body.evidence,
        }
        return {"status": "checkpoint"}
    observed = measurements(storage, event_id, body.attempt)
    if body.cause == "oom" and classify(None, observed) != "oom":
        raise ValueError("An OOM recovery needs the trusted worker's OOM telemetry.")
    if body.cause == "disk" and not (
        isinstance(observed.get("min_free_disk_gb"), (int, float))
        and observed["min_free_disk_gb"] < 1
        and body.disk_gb
    ):
        raise ValueError("A disk recovery needs measured exhausted scratch capacity.")
    record["recovery_pending"] = {
        "id": identity,
        "attempt": body.attempt,
        "tier": destination(record, body, settings),
        "provisioning": record.get("constraints", {}).get("provisioning", "STANDARD"),
        "cause": body.cause,
        "handoff": body.handoff,
        "evidence": body.evidence,
    }
    return {"status": "checkpoint"}


async def prepare_attempt(
    bridge: BatchBridge, event_id: str, record: dict[str, Any]
) -> tuple[str, int, int]:
    storage = bridge.storage
    initialize(record, bridge.settings, now=time.time())
    if "turn_start" not in record:
        record["turn_start"] = (
            read_json(storage, f"projects/_threads/{record['thread_key']}/checkpoint.json") or {}
        )
    seconds, allowance = limits(record, bridge.settings, now=time.time())
    record.setdefault("model_reserved", allowance)
    suffix = f"-r{record['attempt']}" if record["attempt"] else ""
    record["job"] = (
        f"projects/{bridge.app.gcp_project}/locations/{bridge.app.gcp_region}/jobs/claude-{event_id}{suffix}"
    )
    # Heavy runs have no shared concurrency cap. Keep an event-scoped lease for
    # submission/recovery bookkeeping; existing records retain their original pool.
    pool = "claude-slot" if record["tier"] == "standard" else f"claude-large-slot:{event_id}"
    slots = bridge.settings.max_concurrent_runs if pool == "claude-slot" else 1
    from .calibration import history

    record["worker_history"] = await asyncio.to_thread(history, storage, record["project_id"])
    return pool, slots, seconds


def worker_manifest(record: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return {
        "recovery": {
            **({"gpu_type": selected_gpu(record)} if record["tier"] == "gpu" else {}),
            **(
                {"gpu_profile": selected_gpu_choice(record)}
                if record["tier"] == "gpu" and record["launch"].get("gpu_profile")
                else {}
            ),
            **{
                key: record[key]
                for key in (
                    "tier",
                    "provisioning",
                    "attempt",
                    "request_deadline",
                    "launch_reason",
                    "resize_count",
                    "preemption_count",
                    "worker_history",
                    "constraints",
                )
            },
        },
        "resume": record.get("resume"),
        "turn_start": record["turn_start"],
    }


async def release(bridge: BatchBridge, record: dict[str, Any]) -> None:
    if record.get("slot_token"):
        await bridge.repository.release_event(
            f"{record.get('slot_pool', 'claude-slot')}:{record['slot']}", record["slot_token"]
        )
    for key in ("slot", "slot_token", "slot_pool", "submitted", "started", "model_reserved"):
        record.pop(key, None)


def verified_resume(storage: Storage, event_id: str, attempt: int) -> dict[str, Any] | None:
    prefix = event_prefix(event_id)
    for name in (f"attempt-a{attempt}.json", f"partial-a{attempt}.json"):
        value = read_json(storage, f"{prefix}/{name}")
        if not value or value.get("event_id") != event_id or value.get("attempt") != attempt:
            continue
        archive = value.get("archive", "")
        if not re.fullmatch(
            re.escape(prefix) + rf"/(?:handoff|partial)-a{attempt}-[a-f0-9]{{32}}\.tar\.gz", archive
        ):
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", value.get("sha256", "")) or not value.get(
            "state", {}
        ).get("context"):
            continue
        try:
            if storage.read_prefix(archive, 2) != b"\x1f\x8b":
                continue
        except FileNotFoundError:
            continue
        return value
    return None


def exit_code(client: Any, job: str) -> int | None:
    try:
        codes = [
            event.task_execution.exit_code
            for task in client.list_tasks(parent=f"{job}/taskGroups/group0")
            for event in task.status.status_events
            if event.task_execution.exit_code
        ]
        return int(codes[-1]) if codes else None
    except Exception:
        return None


async def terminal(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], client: Any, state: str
) -> bool:
    storage = bridge.storage
    prefix = event_prefix(event_id)
    attempt = record["attempt"]
    result = read_json(storage, f"{prefix}/attempt-a{attempt}.json") or {}
    if result.get("event_id") != event_id or result.get("attempt") != attempt:
        result = {}
    settle(record, result.get("model_cost"))
    pending = record.get("recovery_pending")
    observed = measurements(storage, event_id, attempt)
    code = await asyncio.to_thread(exit_code, client, record["job"]) if state == "FAILED" else None
    outcome = (
        "cancelled"
        if record.get("cancel_requested")
        else "stopped"
        if result.get("outcome") == "stopped"
        else "resized"
        if pending
        else "complete"
        if state == "SUCCEEDED" and result.get("outcome") == "complete"
        else classify(code, observed)
    )
    if outcome == "stopped":
        record["stop_reason"] = result.get("stop_reason", "interrupted")
    record["attempt_outcome"] = outcome
    from .calibration import capture

    try:
        await asyncio.to_thread(capture, storage, event_id, record, outcome)
    except Exception:
        log.warning("Worker history could not be saved; request outcome is unchanged.")
    if outcome in {"cancelled", "stopped", "complete"}:
        return False
    # A killed worker may already have published results. Never replay uncertain side effects.
    if read_json(storage, f"{prefix}/finalizing-a{attempt}.json") is not None:
        record["compute_failure"] = (
            "The worker stopped during final delivery. Saved outputs remain; "
            "delivery must be reconciled before repeating work."
        )
        return False
    try:
        if time.time() >= record["request_deadline"]:
            raise ValueError("This request reached its original time allowance.")
        if pending:
            if pending["attempt"] != attempt:
                raise ValueError("The recovery request belongs to a stale attempt.")
            destination_tier, mode, cause = (
                pending["tier"],
                pending["provisioning"],
                pending["cause"],
            )
            continuation = cause == "continue"
            resize, preempt = int(not continuation), 0
        elif (
            outcome == "preempted"
            and record["provisioning"] == "SPOT"
            and record["preemption_count"]
            < (8 if record.get("launch", {}).get("flexible_compute") else 1)
        ):
            destination_tier = record["tier"]
            mode = record.get("constraints", {}).get("provisioning", "STANDARD")
            cause, resize, preempt, continuation = "preempted", 0, 1, False
        elif outcome == "oom":
            body = RecoverCall(
                attempt=attempt,
                cause="oom",
                evidence="Trusted cgroup OOM counter increased",
                handoff="Resume saved work and repeat the unfinished step.",
            )
            destination_tier = destination(record, body, bridge.settings)
            mode = record.get("constraints", {}).get("provisioning", "STANDARD")
            cause, resize, preempt, continuation = "oom", 1, 0, False
        else:
            raise ValueError(
                "The worker stopped before finishing; no supported automatic recovery remains."
                if outcome == "preempted"
                else "The worker stopped without verified capacity or preemption evidence. "
                "A larger worker has not been inferred from this failure."
            )
        if (
            attempt >= 64
            or (resize and record["resize_count"])
            or (continuation and record.get("continuation_count", 0) >= 32)
        ):
            raise ValueError("This request has used its automatic recovery allowance.")
        resume = verified_resume(storage, event_id, attempt)
        successor = dict(
            record,
            tier=destination_tier,
            provisioning=mode,
            attempt=attempt + 1,
            resize_count=record["resize_count"] + resize,
            preemption_count=record["preemption_count"] + preempt,
            continuation_count=record.get("continuation_count", 0) + int(continuation),
        )
        limits(successor, bridge.settings, now=time.time())
        record.update(successor, resume=resume, recovery_cause=cause)
        if resume and pending:
            resume["handoff"] = pending["handoff"]
        record.pop("recovery_pending", None)
        record["status"] = "pending"
        mode_text = "an on-demand" if mode == "STANDARD" else "a Spot"
        notice = (
            "Continuing from saved progress within your approved allowance."
            if continuation
            else f"The Spot worker was reclaimed; restarting on {mode_text} worker."
            if preempt
            else "This step needs more capacity than expected. "
            "I'm moving to a larger worker and continuing from saved progress."
            if resume
            else "This step needs more capacity than expected. "
            "I'm restarting it on a larger worker; work since the last save will be repeated."
        )
        record["recovery_notice"] = notice
        await release(bridge, record)
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        await bridge.notify(event_id, record, "starting", notice)
        return True
    except ValueError as exc:
        record["compute_failure"] = str(exc)
        if state == "SUCCEEDED":
            record["status"] = "failed"
            await release(bridge, record)
            await bridge.notify(event_id, record, "failed", str(exc))
            return True
        return False
