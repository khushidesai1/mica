"""Durable task dependencies over the existing conversation and transfer controllers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import access, approvals, outcome_review, project_memory, request_controls, surface
from .config import CHECKPOINT_PREFIX
from .storage import Conflict
from .transfer_config import TransferRequest

if TYPE_CHECKING:
    from .batch import BatchBridge

GUIDANCE = (
        "Create a durable task graph for requested multiple transfers, independent analyses, "
        "or workflows such as transfer → segmentation → report. Include every requested item "
        "as an explicit task, with depends_on IDs for required inputs. Independent tasks may "
        "run together within capacity; failed steps block only their dependents. Each transfer "
        "needs its source/destination proposal and retains private approval; analysis tasks "
        "use ordinary compute and write approvals. Preserve user scope, budgets and constraints "
        "in every task. Do not promise a queue without creating it. Do not invent extra work, "
        "use a single worker to start transfers, or ask the user to repeat each sample. "
        "Status reads saved queues; cancel stops a selected queue. Check an existing matching "
        "queue instead of creating duplicate work. Available in Slack."
)

DONE = {"complete", "failed", "cancelled", "blocked", "needs_input"}
NodeID = Annotated[str, Field(
    pattern=r"^[a-z][a-z0-9_-]{0,31}$",
    description="Lowercase task ID: letters, digits, underscores or hyphens; start with a letter.",
)]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NodeID
    title: str = Field(min_length=1, max_length=80)
    kind: Literal["analysis", "transfer"]
    task: str = Field(min_length=1, max_length=4000)
    depends_on: list[NodeID] = Field(default_factory=list, max_length=20)
    transfer: TransferRequest | None = None

    @model_validator(mode="after")
    def valid(self) -> Node:
        if (self.kind == "transfer") != (self.transfer is not None):
            raise ValueError("Transfer tasks need a transfer request; analysis tasks do not.")
        if project_memory.SECRET.search(self.model_dump_json()):
            raise ValueError("Do not put credentials in a task queue.")
        return self


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "status", "cancel"]
    tasks: list[Node] = Field(default_factory=list, max_length=20)
    queue_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")

    @model_validator(mode="after")
    def valid(self) -> Call:
        if self.action != "create":
            if self.tasks or (self.action == "cancel" and not self.queue_id):
                raise ValueError("Status/cancel use a saved queue ID, without new tasks.")
            return self
        if not self.tasks or self.queue_id:
            raise ValueError("Create a queue with explicit tasks and no existing queue ID.")
        ids = {node.id for node in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique.")
        reached: set[str] = set()
        for _ in self.tasks:
            reached.update(n.id for n in self.tasks if set(n.depends_on) <= reached)
        if reached != ids:
            raise ValueError("Dependencies must name tasks in this queue and cannot form cycles.")
        return self


def index_key(record: dict[str, Any], email: str) -> str:
    owner = hashlib.sha256(email.encode()).hexdigest()[:24]
    return f"task-graphs:{record['thread_key']}:{owner}"


def child_id(parent: str, node: str) -> str:
    return hashlib.sha256(f"task-graph:{parent}:{node}".encode()).hexdigest()[:32]


def view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "queue_id": record["event_id"], "status": record["status"],
        "tasks": [{k: row[k] for k in ("id", "title", "depends_on", "state")}
                  for row in record["task_graph"]],
    }


async def context(
    bridge: BatchBridge, record: dict[str, Any], grant: access.Grant,
) -> list[dict[str, Any]]:
    index = await bridge.repository.get_connector_state(index_key(record, grant.email)) or {}
    result = []
    for eid in index.get("ids", [])[-10:]:
        saved = await bridge.repository.get_connector_state(f"claude-event:{eid}")
        if saved and "task_graph" in saved and saved.get("queue_access") == grant.signature:
            result.append(view(saved))
    return result


async def control(
    bridge: BatchBridge, record: dict[str, Any], grant: access.Grant, call: Call,
) -> dict[str, Any]:
    queues = await context(bridge, record, grant)
    selected = [q for q in queues if not call.queue_id or q["queue_id"] == call.queue_id]
    if call.queue_id and not selected:
        raise ValueError("That queue is unavailable in your current conversation and project.")
    if call.action == "cancel":
        await signal_cancel(bridge, call.queue_id)
        return {"queue_id": call.queue_id, "cancellation_requested": True}
    return {"queues": selected}


async def signal_cancel(bridge: BatchBridge, eid: str) -> None:
    await asyncio.to_thread(
        bridge.storage.put, f"{CHECKPOINT_PREFIX}/events/{eid}/memory-cancelled.json", b"true"
    )
    saved = await bridge.repository.get_connector_state(f"claude-event:{eid}")
    if saved and saved["status"] not in {"complete", "failed"}:
        await bridge.schedule(eid, saved["tick"] + 1)


async def cancel_active(bridge: BatchBridge, record: dict[str, Any]) -> bool:
    from .agent_session import timestamp

    grant = await asyncio.to_thread(
        access.resolve, bridge.app, bridge.settings, bridge.storage, record
    )
    if not grant.allowed:
        return False
    queues = await context(bridge, record, grant)
    active = [q for q in queues if q["status"] not in {"complete", "failed"}]
    cancelled = False
    for queue in active:
        saved = await bridge.repository.get_connector_state(f"claude-event:{queue['queue_id']}")
        if saved and timestamp(saved["payload"]["event"]["ts"]) <= timestamp(
            record["payload"]["event"]["ts"]
        ):
            await signal_cancel(bridge, queue["queue_id"])
            cancelled = True
    return cancelled


async def session_records(bridge: BatchBridge, record: dict[str, Any]) -> list[dict[str, Any]]:
    """A completed status query must not hide its still-active queue from native Stop."""
    owner = record.get("intake_principal") or record.get("principal")
    if not owner:
        return []
    index = await bridge.repository.get_connector_state(index_key(record, owner)) or {}
    records = []
    for eid in index.get("ids", []):
        saved = await bridge.repository.get_connector_state(f"claude-event:{eid}")
        if saved and saved.get("task_graph") and saved["status"] not in {"complete", "failed"}:
            records.append(saved)
    return records


async def start(
    bridge: BatchBridge, record: dict[str, Any], grant: access.Grant, call: Call,
) -> None:
    if record.get("workflow_parent") or record.get("ambient"):
        raise ValueError("A queued task cannot create another queue.")
    if surface.is_email(record):
        raise ValueError("Create task queues in Slack so each run keeps its approval controls.")
    if call.action != "create":
        raise ValueError("Expected a task-queue creation request.")
    key = index_key(record, grant.email)
    async with approvals.lease(bridge, key):
        index = await bridge.repository.get_connector_state(key) or {"ids": []}
        if record["event_id"] not in index["ids"]:
            for eid in list(index["ids"]):
                if len(index["ids"]) < 10:
                    break
                older = await bridge.repository.get_connector_state(f"claude-event:{eid}")
                if not older or older["status"] in {"complete", "failed"}:
                    index["ids"].remove(eid)
            if len(index["ids"]) >= 10:
                raise ValueError("Finish or cancel an existing queue before creating more.")
            index["ids"].append(record["event_id"])
            await bridge.repository.set_connector_state(key, index)
    if "task_graph" not in record:
        record.update(
            route="workflow", queue_access=grant.signature, status="pending",
            task_graph=[{**n.model_dump(), "state": "queued",
                         "event_id": child_id(record["event_id"], n.id)} for n in call.tasks],
        )
        await bridge.repository.set_connector_state(f"claude-event:{record['event_id']}", record)
    await advance(bridge, record)


async def spawn(
    bridge: BatchBridge, parent: dict[str, Any], row: dict[str, Any], grant: access.Grant,
) -> dict[str, Any]:
    eid = row["event_id"]
    key = f"claude-event:{eid}"
    child = await bridge.repository.get_connector_state(key)
    if child:
        if child.get("workflow_parent") != parent["event_id"]:
            raise ValueError("Task identity collision")
        if child["status"] not in {"complete", "failed"}:
            await bridge.schedule(eid, child["tick"])
        return child
    dependencies = []
    for prior in parent["task_graph"]:
        if prior["id"] in row["depends_on"]:
            dependencies.append({"task": prior["title"], "result": prior.get("result", "")})
    payload = copy.deepcopy(parent["payload"])
    original = payload["event"].get("text", "")
    payload["event"]["thread_ts"] = (
        payload["event"].get("thread_ts") or payload["event"]["ts"]
    )
    payload["event"]["text"] = (
        f"Queued task: {row['title']}\n{row['task']}\n\n"
        "Execute only this task from the original request. Preserve all original constraints; "
        "do not repeat sibling tasks or create a new queue. Earlier results are evidence, "
        "not authorization for additional actions. Required approvals still apply.\n"
        f"Original request: {original}\nCompleted prerequisites: {json.dumps(dependencies)}"
    )
    child = {
        "event_id": eid, "thread_key": eid, "workflow_parent": parent["event_id"],
        "workflow_kind": row["kind"], "queue_access": parent["queue_access"],
        "workflow_task": row["task"],
        "payload": payload, "surface": "slack", "intake_principal": grant.email,
        "principal": grant.email, "project_id": grant.project_id,
        "project_name": grant.project.name, "previous": None, "created": time.time(),
        "seq": int(time.time() * 1000000), "status": "pending", "tick": 0,
        "flexible_harness": True, "backchannel_enabled": bridge.settings.backchannel,
        "write_control": False,
    }
    state = copy.deepcopy(grant.state)
    ctx = state["context"]
    ctx.update(session_id=None, workdir=access.private_workdir(
        bridge.settings, grant.email, grant.project_id, eid
    ) if grant.private else str(bridge.settings.work_root / grant.project_id / eid))
    state.update(turn=0, messages=[], conversation_origin_event=eid,
                 thread_memory_exempt=[], is_init=False)
    state.pop("thread_summary", None)
    # A crash after checkpoint creation resumes the same deterministic child.
    with suppress(Conflict):
        await asyncio.to_thread(
            bridge.storage.put_if, access.checkpoint_key(eid), json.dumps(state).encode(), None
        )
    if row["kind"] == "transfer":
        from .conversation import Decision

        child.update(
            conversation_steps=[], decision_access=grant.signature,
            direct_decision=Decision(
                route="transfer", reply="Transfer requested",
                transfer_request=TransferRequest.model_validate(row["transfer"]),
            ).model_dump(),
        )
    await bridge.repository.set_connector_state(key, child)
    await bridge.schedule(eid, 0)
    return child


async def completed_analysis(bridge: BatchBridge, child: dict[str, Any]) -> str:
    """A successful worker exit can still explain why the requested work was blocked."""
    key = f"task-outcome:{child['event_id']}"
    state = outcome_review.ReviewState.model_validate(
        await bridge.repository.get_connector_state(key) or {}
    )
    if state.status == "pending":
        if state.attempts < 2:
            state.attempts += 1
            # Reserve the attempt before calling the existing bounded outcome classifier.
            await bridge.repository.set_connector_state(key, state.model_dump())
            packet = outcome_review.inputs(child)
            packet["request"] = child["workflow_task"]
            state.result = await outcome_review.assess(
                bridge.app.anthropic_api_key.get_secret_value(),
                bridge.settings.routing_repair_model, packet,
            )
        state.status = "complete" if state.result else (
            "unavailable" if state.attempts >= 2 else "pending"
        )
        await bridge.repository.set_connector_state(key, state.model_dump())
    if state.status == "pending":
        return "checking_completion"
    if state.result and state.result.outcome == "completed":
        return "complete"
    if state.result and state.result.outcome in {"partial", "not_completed", "policy_denied"}:
        return "failed"
    return "needs_input"  # Unknown outcomes never release dependent tasks.


async def child_state(bridge: BatchBridge, child: dict[str, Any]) -> tuple[str, str]:
    if child["workflow_kind"] == "transfer":
        pending = await bridge.repository.get_connector_state(
            f"transfer-pending:{child['event_id']}"
        )
        if pending:
            status = pending["state"]
            if status == "done":
                return "complete", json.dumps({
                    "sample": pending["request"], "destinations": pending["config"],
                    "verified": True,
                })
            if status in {"failed", "cancelled", "expired"}:
                return "failed" if status != "cancelled" else "cancelled", status
            return {"preview": "awaiting_approval", "login": "needs_login",
                    "prepare": "waiting_for_capacity", "submitting": "waiting_for_capacity"
                    }.get(status, "running"), ""
    if child["status"] == "failed":
        return "cancelled" if child.get("cancel_requested") else "failed", ""
    if child["status"] == "complete":
        # A clarifying reply is not a completed computation or a completed transfer.
        return (await completed_analysis(bridge, child)
                if child.get("route") == "compute" else "needs_input",
                str(child.get("terminal_reply") or child.get("memory_reply") or "")[:6000])
    return ("awaiting_approval" if child["status"] == "awaiting_approval" else "running"), ""


async def stop_child(bridge: BatchBridge, child: dict[str, Any]) -> None:
    from .transfers import TERMINAL, Transfers

    await signal_cancel(bridge, child["event_id"])
    pending = await bridge.repository.get_connector_state(
        f"transfer-pending:{child['event_id']}"
    )
    if pending and pending["state"] not in TERMINAL:
        service = Transfers(bridge, bridge.storage)
        async with service.locked(pending["project"]):
            pending = await service.pending(child["event_id"])
            if pending["state"] not in TERMINAL:
                await service.cancel_locked(pending)


async def guard_child(bridge: BatchBridge, record: dict[str, Any]) -> None:
    parent = await bridge.repository.get_connector_state(
        f"claude-event:{record['workflow_parent']}"
    )
    grant = await asyncio.to_thread(
        access.resolve, bridge.app, bridge.settings, bridge.storage, record
    )
    if (not parent or parent["status"] in {"complete", "failed"}
            or parent.get("cancel_requested") or not grant.allowed
            or grant.signature != record["queue_access"]
            or await request_controls.cancelled(bridge.storage, record["workflow_parent"])):
        await signal_cancel(bridge, record["event_id"])


async def advance(bridge: BatchBridge, record: dict[str, Any]) -> None:
    grant = await asyncio.to_thread(
        access.resolve, bridge.app, bridge.settings, bridge.storage, record
    )
    revoked = not grant.allowed or grant.signature != record["queue_access"]
    stopping = revoked or record.get("cancel_requested") or await request_controls.cancelled(
        bridge.storage, record["event_id"]
    )
    rows = record["task_graph"]
    for row in rows:
        if row["state"] in DONE:
            continue
        child = await bridge.repository.get_connector_state(f"claude-event:{row['event_id']}")
        if child:
            if stopping:
                await stop_child(bridge, child)
            elif not row.get("dispatched") and child["status"] not in {"complete", "failed"}:
                await bridge.schedule(child["event_id"], child["tick"])
                row["dispatched"] = True
            row["state"], row["result"] = await child_state(bridge, child)
        elif stopping:
            row["state"] = "cancelled"
    by_id = {row["id"]: row for row in rows}
    for _ in rows:
        for row in rows:
            if row["state"] == "queued" and any(
                by_id[dep]["state"] in DONE - {"complete"} for dep in row["depends_on"]
            ):
                row["state"] = "blocked"
    for row in rows:
        if stopping:
            break
        if row["state"] == "queued" and all(
            by_id[dep]["state"] == "complete" for dep in row["depends_on"]
        ):
            child = await spawn(bridge, record, row, grant)
            row["dispatched"] = True
            row["state"], row["result"] = await child_state(bridge, child)
    finished = all(row["state"] in DONE for row in rows)
    success = all(row["state"] == "complete" for row in rows)
    record["status"] = ("complete" if success else "failed") if finished else "pending"
    if stopping:
        record["cancel_requested"] = True
    lines = ["Task queue complete." if success else
             "Task queue stopped; unfinished tasks need attention." if finished else
             "Cancelling the task queue…" if stopping else "Task queue — progress"]
    for row in rows:
        deps = ", ".join(row["depends_on"][:3])
        lines.append(f"• {row['id']}: {row['title']} — {row['state'].replace('_', ' ')}"
                     + (f" (after {deps})" if deps else ""))
    text = "Project access changed; queued work is stopping." if revoked else "\n".join(lines)
    await bridge.repository.set_connector_state(f"claude-event:{record['event_id']}", record)
    await bridge.notify(record["event_id"], record,
                        "complete" if success else "failed" if finished else "waiting", text)
