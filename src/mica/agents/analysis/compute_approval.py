"""Requester-bound GPU confirmation, revalidated immediately before submission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from slack_sdk.web.async_client import AsyncWebClient

from . import access, surface
from .storage import Conflict, Storage
from .tiers import gpu_estimate, resolve

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
    expires_at: float
    approved_at: float = 0
    status: Literal["pending", "approved", "cancelled"] = "pending"


def key(event_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise ValueError("Invalid GPU request")
    return f"compute-approvals/{event_id}.json"


def load(storage: Storage, event_id: str) -> tuple[Approval, str] | None:
    try:
        raw, generation = storage.get(key(event_id))
        return Approval.model_validate_json(raw), generation
    except FileNotFoundError:
        return None


def choices(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record["launch"], sort_keys=True).encode()).hexdigest()


def validate(
    bridge: BatchBridge, storage: Storage, record: dict[str, Any], approval: Approval
) -> None:
    shape = resolve("gpu", bridge.settings, record["launch"].get("gpu_type", "l4"))
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
        or shape.max_seconds != approval.seconds
        or gpu_estimate(bridge.settings, shape) != approval.hourly_estimate
        or bridge.settings.max_budget_usd_per_turn != approval.model_allowance
    ):
        raise ValueError("GPU execution choices changed. Request fresh confirmation.")
    if record.get("recovery_version") and (
        record["tier"] != "gpu"
        or record["provisioning"] != record["launch"]["provisioning"]
        or record["attempt"] > 1
        or record["resize_count"]
        or record["request_deadline"] > approval.approved_at + approval.seconds + 1
    ):
        raise ValueError("GPU execution exceeds the confirmed limits.")


async def ready(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], storage: Storage
) -> bool:
    if record.get("launch", {}).get("tier") != "gpu":
        return True
    shape = resolve("gpu", bridge.settings, record["launch"].get("gpu_type", "l4"))
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
            seconds=shape.max_seconds,
            hourly_estimate=gpu_estimate(bridge.settings, shape),
            model_allowance=bridge.settings.max_budget_usd_per_turn,
            expires_at=time.time() + 3600,
        )
        try:
            await asyncio.to_thread(
                storage.put_if, key(event_id), approval.model_dump_json().encode(), None
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
        if record["status"] == "awaiting_approval":
            record["status"] = "pending"
        return True
    if approval.status == "cancelled" or time.time() >= approval.expires_at:
        record.update(status="failed", cancel_requested=approval.status == "cancelled")
        await bridge.notify(
            event_id, record, "failed", "GPU request cancelled or expired. Nothing started."
        )
        return False
    mode = record["launch"]["provisioning"]
    compute = approval.hourly_estimate * approval.seconds / 3600
    text = (
        f"Approve this GPU run on 1 NVIDIA {str(shape.gpu_type).upper()} "
        f"({shape.gpu_memory_gib} GiB GPU memory, {shape.vcpus} CPUs, "
        f"{shape.memory_gib} GiB RAM)? "
        f"Time allowance: {approval.seconds / 3600:g} hours including startup/retries. "
        f"Estimated compute and disk: ${compute:.2f}; "
        f"model allowance: ${approval.model_allowance:.2f}. "
        "This is an estimate, not a billing cap. Startup can take several minutes. "
        + (
            "Spot may restart once on Spot within the same allowance."
            if mode == "SPOT"
            else "On-demand; no automatic restart or resize."
        )
    )
    record.update(status="awaiting_approval", gpu_approval=True)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    if record.get("notice_text") != text:
        await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_update(
            channel=record["payload"]["event"]["channel"],
            ts=record["notice_ts"],
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "action_id": PREFIX + action,
                            "value": event_id,
                        }
                        for action, label in [("approve", "Approve GPU run"), ("cancel", "Cancel")]
                    ],
                },
            ],
        )
        record.update(notice_text=text, notice_phase="gpu_approval")
    return False


def is_action(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "block_actions" and any(
        str(a.get("action_id", "")).startswith(PREFIX) for a in payload.get("actions", [])
    )


async def handle(bridge: BatchBridge, storage: Storage, payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", [])
    if len(actions) != 1 or actions[0].get("action_id") not in {
        PREFIX + "approve",
        PREFIX + "cancel",
    }:
        raise ValueError("Invalid GPU action")
    event_id = str(actions[0].get("value", ""))
    key(event_id)
    mutex = f"claude-poll:{event_id}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=60)
    if claim.status != "claimed":
        raise ValueError("Request is being updated. Try again shortly.")
    try:
        record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
        saved = await asyncio.to_thread(load, storage, event_id)
        if not record or not saved:
            raise ValueError("Unknown GPU request")
        event = record["payload"]["event"]
        if (
            payload.get("team", {}).get("id") != record["payload"]["team_id"]
            or payload.get("user", {}).get("id") != event["user"]
            or payload.get("channel", {}).get("id") != event["channel"]
            or payload.get("message", {}).get("ts") != record.get("notice_ts")
        ):
            raise ValueError("Only the requester can confirm this run in its original thread.")
        approval, generation = saved
        await asyncio.to_thread(validate, bridge, storage, record, approval)
        if record["status"] != "awaiting_approval" or approval.status != "pending":
            return {}
        if time.time() >= approval.expires_at:
            raise ValueError("This GPU request expired. Send it again.")
        approval.status = "approved" if actions[0]["action_id"].endswith("approve") else "cancelled"
        approval.approved_at = time.time() if approval.status == "approved" else 0
        await asyncio.to_thread(
            storage.put_if, key(event_id), approval.model_dump_json().encode(), generation
        )
        record.update(
            status="pending" if approval.status == "approved" else "failed",
            cancel_requested=approval.status == "cancelled",
        )
        await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
        if approval.status == "approved":
            await bridge.schedule(event_id, record["tick"])
        text = (
            "GPU run approved. Preparing this run…"
            if approval.status == "approved"
            else "Cancelled. Nothing started."
        )
        await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).chat_update(
            channel=event["channel"],
            ts=record["notice_ts"],
            text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
        return {}
    finally:
        await bridge.repository.release_event(mutex, claim.token)
