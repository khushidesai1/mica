"""Requester-bound GPU confirmation, revalidated immediately before submission."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slack_sdk.web.async_client import AsyncWebClient

from . import access, agent_session, approvals, surface
from .storage import Conflict, Storage
from .tiers import gpu_choices, gpu_estimate, gpu_mode, gpu_seconds, resolve, selected_gpu_choice

if TYPE_CHECKING:
    from .batch import BatchBridge

PREFIX = "mica_gpu_"


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    principal: str
    project_id: str
    access_signature: str
    choices: str
    image: str
    seconds: int = Field(gt=0)
    hourly_estimate: float = Field(gt=0)
    model_allowance: float = Field(gt=0)
    flexible_harness: bool = False
    expires_at: float
    approved_at: float = 0
    status: Literal["pending", "approved", "cancelled", "expired"] = "pending"


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid GPU request")
    # Existing control-plane-only prefix; task/project prefixes are worker-writable.
    return f"write-access/compute-approvals/{event_id}.json"


def load(storage: Storage, event_id: str) -> tuple[Approval, str] | None:
    return approvals.load_model(storage, key(event_id), Approval)


def choices(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record["launch"], sort_keys=True).encode()).hexdigest()


def validate(
    bridge: BatchBridge, storage: Storage, record: dict[str, Any], approval: Approval
) -> None:
    shapes = [resolve("gpu", bridge.settings, name) for name in gpu_choices(record)]
    current = access.resolve(bridge.app, bridge.settings, storage, record)
    if (
        not current.allowed
        or not current.has_context
        or current.project_id != approval.project_id
        or current.email != approval.principal
        or current.signature != approval.access_signature
    ):
        raise ValueError("Project access changed. Request a new GPU run.")
    if (
        choices(record) != approval.choices
        or bridge.settings.gpu_image != approval.image
        or gpu_seconds(record, bridge.settings) != approval.seconds
        or max(gpu_estimate(bridge.settings, item) for item in shapes) != approval.hourly_estimate
        or selected_gpu_choice(record) not in gpu_choices(record)
        or bridge.settings.max_budget_usd_per_turn != approval.model_allowance
        or bool(record.get("flexible_harness")) != approval.flexible_harness
    ):
        raise ValueError("GPU execution choices changed. Request fresh confirmation.")
    if record.get("recovery_version") and (
        record["tier"] != "gpu"
        or record["provisioning"] != gpu_mode(record)
        or record.get("gpu_fallback_count", 0) > len(shapes) - 1
        or record["attempt"]
        > (
            32 + 8 + record.get("gpu_fallback_count", 0)
            if record["launch"].get("flexible_compute")
            else 1 + record.get("gpu_fallback_count", 0)
        )
        or record["resize_count"]
        or record["request_deadline"] > approval.approved_at + approval.seconds + 1
    ):
        raise ValueError("GPU execution exceeds the confirmed limits.")


async def ready(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage
) -> bool:
    if record.get("launch", {}).get("tier") != "gpu":
        return True
    shape = resolve("gpu", bridge.settings, selected_gpu_choice(record))
    if surface.is_email(record):
        raise ValueError(
            "GPU execution needs confirmation in your private Slack conversation with Mica. "
            "Please make this GPU request there; no GPU job has started."
        )
    saved = await asyncio.to_thread(load, storage, event_id)
    if saved is None:
        approval = Approval(
            event_id=event_id,
            principal=record["principal"],
            project_id=record["project_id"],
            access_signature=record["access_signature"],
            choices=choices(record),
            image=bridge.settings.gpu_image,
            seconds=gpu_seconds(record, bridge.settings),
            hourly_estimate=max(
                gpu_estimate(bridge.settings, resolve("gpu", bridge.settings, name))
                for name in gpu_choices(record)
            ),
            model_allowance=bridge.settings.max_budget_usd_per_turn,
            flexible_harness=bool(record.get("flexible_harness")),
            expires_at=KIND.deadline(),
        )
        try:
            await asyncio.to_thread(
                approvals.save, storage, key(event_id), approval, None
            )
        except Conflict:
            saved = await asyncio.to_thread(load, storage, event_id)
            assert saved is not None
            approval = saved[0]
    else:
        approval = saved[0]
    await asyncio.to_thread(validate, bridge, storage, record, approval)
    if approval.status == "approved" and 0 < approval.approved_at <= approval.expires_at:
        if time.time() >= approval.approved_at + approval.seconds:
            raise ValueError("The confirmed GPU run's time allowance expired.")
        record["gpu_approved_at"] = approval.approved_at
        record["gpu_approved_seconds"] = approval.seconds
        if record["status"] == "awaiting_approval":
            record["status"] = "pending"
        return True
    if approval.status == "expired" or approvals.expired(approval.expires_at):
        await expire_record(bridge, storage, event_id, record)
        return False
    if approval.status == "cancelled":
        record.update(status="failed", cancel_requested=approval.status == "cancelled")
        await bridge.notify(
            event_id, record, "failed", "GPU request cancelled. Nothing started."
        )
        return False
    mode = record["launch"]["provisioning"]
    compute = approval.hourly_estimate * approval.seconds / 3600
    hourly = approval.hourly_estimate
    alternatives = gpu_choices(record)[1:]
    def describe(name: str) -> str:
        candidate = resolve("gpu", bridge.settings, name)
        mode_label = "Spot" if gpu_mode(record, name) == "SPOT" else "on-demand"
        return (
            f"{candidate.gpu_count} NVIDIA {str(candidate.gpu_type).upper()} "
            f"({candidate.gpu_memory_gib} GiB VRAM each, {candidate.memory_gib} GiB host RAM, "
            f"{candidate.vcpus} CPUs; {mode_label})"
        )

    fallback = (
        "If unavailable, automatically try "
        + ", ".join(describe(name) for name in alternatives)
        + " within this same time and cost estimate. "
        if alternatives
        else ""
    )
    requested = record["launch"].get("requested_gpu_type", shape.gpu_type)
    if requested and requested != shape.gpu_type:
        fallback = (
            f"Selected {str(shape.gpu_type).upper()} to meet the assessed needs and cost. "
            + fallback
        )
    text = (
        f"Approve this GPU run on {describe(selected_gpu_choice(record))}?\n\n"
        + fallback
        + f"\nTime allowance: {approval.seconds / 3600:g} hours including startup/retries.\n"
        f"Estimated compute and disk: ${compute:.2f}; "
        + (f"model usage check-in at ${approval.model_allowance:.2f}; work continues "
           "unless you cancel. " if approval.flexible_harness
           else f"model allowance: ${approval.model_allowance:.2f}. ")
        +
        "\n\nThis is an estimate, not a billing cap. Startup can take several minutes. "
        + (
            "Saved work can continue on replacement Spot workers within this same allowance."
            if mode == "SPOT"
            else "On-demand; completed stages can continue within this same allowance. No resize."
        )
    )
    assessment = record["launch"].get("gpu_sizing")
    if assessment:
        text += (
            "\n\n*Sizing assessment* · " + str(assessment["basis"]).replace("_", " ")
            + "\n" + html.escape(str(assessment["evidence"]), quote=False)
            + ("\n*Parallelism:* " + html.escape(str(assessment["multi_gpu_reason"]), quote=False)
               if assessment.get("multi_gpu_reason") else "")
        )
    await approvals.watch(bridge, KIND, event_id, approval.expires_at, record)
    record.update(status="awaiting_approval", gpu_approval=True)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if record.get("notice_text") != text:
        await approvals.request_card(
            bridge, event_id, record,
            AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                *(
                    [
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "static_select",
                                    "action_id": PREFIX + "allowance",
                                    "placeholder": {
                                        "type": "plain_text",
                                        "text": "Change total allowance",
                                    },
                                    "options": [
                                        {
                                            "text": {
                                                "type": "plain_text",
                                                "text": (
                                                    f"{seconds / 3600:g} hours · "
                                                    f"~${seconds / 3600 * hourly:.2f}"
                                                ),
                                            },
                                            "value": f"{event_id}:{seconds}",
                                        }
                                        for seconds in sorted(
                                            {approval.seconds, 7200, 28800, 86400, 259200, 604800}
                                        )
                                        if seconds
                                        <= min(
                                            bridge.settings.max_run_seconds,
                                            bridge.settings.gpu_max_seconds,
                                        )
                                    ],
                                }
                            ],
                        }
                    ]
                    if record["launch"].get("flexible_compute")
                    else []
                ),
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "action_id": PREFIX + action,
                            "value": event_id,
                            **({"style": "primary"} if action == "approve" else {}),
                        }
                        for action, label in [("approve", "Approve GPU run"), ("cancel", "Cancel")]
                    ],
                },
            ],
        )
        record.update(notice_text=text, notice_phase="gpu_approval")
    return False


def is_action(payload: dict[str, Any]) -> bool:
    return KIND.matches(payload)



async def _handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", [])
    if len(actions) != 1 or actions[0].get("action_id") not in {
        PREFIX + "approve",
        PREFIX + "cancel",
        PREFIX + "allowance",
        PREFIX + "again",
    }:
        raise ValueError("Invalid GPU action")
    action = actions[0]
    changing = action["action_id"] == PREFIX + "allowance"
    selected = str(action.get("selected_option", {}).get("value", ""))
    event_id = selected.split(":")[0] if changing else str(action.get("value", ""))
    key(event_id)
    async with approvals.lease(bridge, f"claude-poll:{event_id}"):
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        saved = await asyncio.to_thread(load, storage, event_id)
        if not record or not saved:
            raise ValueError("Unknown GPU request")
        event = record["payload"]["event"]
        approvals.requester(payload, record)
        approval, generation = saved
        await asyncio.to_thread(validate, bridge, storage, record, approval)
        if action["action_id"] == PREFIX + "again":
            if approval.status not in {"expired", "pending"}:
                return {}
            async with approvals.lease(bridge, "claude-intake:" + record["thread_key"]):
                if record["status"] != "awaiting_approval":
                    await approvals.renew_run(bridge, event_id, record)
                if approval.status == "expired":
                    approval.status, approval.expires_at = "pending", KIND.deadline()
                    await asyncio.to_thread(
                        approvals.save, storage, key(event_id), approval, generation
                    )
                await ready(bridge, event_id, record, storage)
                await approvals.save_record(bridge, event_id, record)
                await bridge.schedule(event_id, record["tick"])
            return {}
        if approval.status == "expired":
            await expire_record(bridge, storage, event_id, record)
            return {}
        if record["status"] != "awaiting_approval" or approval.status != "pending":
            return {}
        if approvals.expired(approval.expires_at):
            await expire_record(bridge, storage, event_id, record)
            raise ValueError("This GPU request expired. Use Request again for fresh confirmation.")
        if changing:
            seconds = int(selected.split(":", 1)[1])
            if not record["launch"].get("flexible_compute") or not 600 <= seconds <= min(
                bridge.settings.max_run_seconds, bridge.settings.gpu_max_seconds
            ):
                raise ValueError("Choose an available compute allowance.")
            record["launch"]["max_seconds"] = seconds
            approval.seconds, approval.choices = seconds, choices(record)
            await asyncio.to_thread(
                approvals.save, storage, key(event_id), approval, generation
            )
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
            await ready(bridge, event_id, record, storage)
            await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
            return {}
        approval.status = "approved" if actions[0]["action_id"].endswith("approve") else "cancelled"
        approval.approved_at = time.time() if approval.status == "approved" else 0
        await asyncio.to_thread(
            approvals.save, storage, key(event_id), approval, generation
        )
        record.update(
            status="pending" if approval.status == "approved" else "failed",
            cancel_requested=approval.status == "cancelled",
        )
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        if approval.status == "approved":
            await bridge.schedule(event_id, record["tick"])
            await approvals.resumed_card(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
                event["channel"], record["notice_ts"], "Preparing approved GPU run…",
                f"GPU compute approved for up to {approval.seconds / 3600:g} hours. "
                "Checking remaining launch requirements.",
                native_session=agent_session.eligible(bridge, record),
            )
        else:
            await approvals.card(
                AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5),
                event["channel"], record["notice_ts"], "Cancelled. Nothing started.",
                state="cancelled",
            )
        return {}


async def expire_record(
    bridge: BatchBridge, storage: Storage, event_id: str, record: dict[str, Any]
) -> None:
    saved = await asyncio.to_thread(load, storage, event_id)
    if saved:
        await approvals.expire_run(bridge, storage, KIND, event_id, record, *saved)


async def expire(bridge: BatchBridge, storage: Storage, event_id: str) -> None:
    async with approvals.lease(bridge, f"claude-poll:{event_id}"):
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        if record and record.get("gpu_approval"):
            await expire_record(bridge, storage, event_id, record)


async def decide(bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: Any
                 ) -> dict[str, Any]:
    return await _handle(bridge, storage, payload)


KIND = approvals.Kind("compute", (PREFIX,), key, 3600, decide, expire)


async def handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    from fastapi import BackgroundTasks

    return await approvals.handle(bridge, storage, payload, BackgroundTasks())
