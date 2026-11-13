"""Deadline-bound private reminders; decisions remain on their original approval cards."""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import approvals, atomx_delivery, job_lifecycle
from .ambient_delivery import post_once
from .storage import Storage

if TYPE_CHECKING:
    from .batch import BatchBridge

LEAD_SECONDS = 20 * 60
LABELS = {
    "compute": "compute approval",
    "write": "file-operation approval",
    "command": "project confirmation",
    "amendment": "request update",
    "access": "project access approval",
    "cloud_access": "cloud access approval",
    "transfer": "transfer approval",
}


@dataclass(frozen=True)
class Reminder:
    deadline: float
    user: str
    channel: str
    ts: str
    dm_channel: str = ""


def validate(kind: str, identifier: str) -> None:
    if kind not in LABELS or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid approval reminder")


async def schedule(bridge: BatchBridge, kind: str, identifier: str, deadline: float) -> None:
    validate(kind, identifier)
    if approvals.expired(deadline):
        return
    stamp = math.ceil(deadline)
    await job_lifecycle.enqueue(
        bridge.app, f"approval-reminder-{kind}-{identifier}-{stamp}",
        f"/api/v1/internal/slack-analysis/approval-reminders/{kind}/{identifier}/{stamp}",
        max(0, stamp - LEAD_SECONDS - int(time.time())),
    )


def from_record(record: dict[str, Any], deadline: float) -> Reminder | None:
    from . import surface

    if surface.is_email(record) or record.get("cancel_requested"):
        return None
    event = record["payload"]["event"]
    channel = event["channel"]
    return Reminder(
        deadline, event["user"], channel, record.get("notice_ts", ""),
        channel if event.get("channel_type") == "im" and channel.startswith("D") else "",
    )


async def pending(
    bridge: BatchBridge, storage: Storage, kind: str, identifier: str,
) -> Reminder | None:
    """Read current authority under the same lease as the kind's decision handler."""
    repo = bridge.repository
    if kind == "transfer":
        from .transfers import Transfers

        row = await repo.get_connector_state("transfer-pending:" + identifier)
        if not row or row["state"] != "preview" or row.get("surface") == "email":
            return None
        transfers = Transfers(bridge, storage)
        try:
            transfers.authorize(row["project"], row["requested_by"], row)
            login = await transfers.login(row["requested_by"])
        except ValueError:
            return None
        if login.get("status") != "valid" or login.get("version") != row["login_version"]:
            return None
        message = await repo.get_connector_state(atomx_delivery.message_key(identifier)) or {}
        return Reminder(
            row["expires"], row["slack_user"], message.get("channel", ""), message.get("ts", ""),
            message.get("channel", ""),
        )
    registered = approvals.resolve(kind)
    if kind in {"access", "cloud_access"}:
        row = await repo.get_connector_state(registered.key(identifier))
        if not row or row["state"] != "pending":
            return None
        if kind == "access":
            return Reminder(
                row["expires"], row["owner"].split(":", 1)[1],
                row.get("owner_channel", ""), row.get("message_ts", ""),
                row.get("owner_channel", ""),
            )
        from . import cloud_access

        request = cloud_access.Request.model_validate(row)
        if not cloud_access.unchanged(bridge, request):
            return None
        return Reminder(
            request.expires, request.policy.approver.split(":", 1)[1],
            request.card_channel, request.card_ts,
            request.card_channel if request.origin == "email" else "",
        )
    record = await repo.get_connector_state("claude-event:" + identifier)
    if not record:
        return None
    if kind == "amendment":
        if (record.get("status") != "pending" or not record.get("amendable")
                or record.get("amendment_choice") or not record.get("amendment_expires")):
            return None
        return from_record(record, record["amendment_expires"])
    saved = await asyncio.to_thread(approvals.load_json, storage, registered.key(identifier))
    if not saved:
        return None
    row = saved[0]
    if kind == "command":
        if row["state"] != "pending":
            return None
        return from_record(record, row["expires"])
    flag = "gpu_approval" if kind == "compute" else "overwrite_approval"
    if (row["status"] != "pending" or not record.get(flag) or record.get("job")
            or record.get("status") != "awaiting_approval"):
        return None
    return from_record(record, row["expires_at"])


async def deliver(bridge: BatchBridge, kind: str, identifier: str, row: Reminder, stamp: int
                  ) -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    now = time.time()
    if (not math.isfinite(row.deadline) or math.ceil(row.deadline) != stamp
            or row.deadline <= now or row.deadline - now > LEAD_SECONDS):
        return
    if not row.channel or not row.ts:
        raise RuntimeError("Approval card delivery is still pending; retry reminder")
    client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5)
    link = await client.chat_getPermalink(channel=row.channel, message_ts=row.ts)
    url = str(link["permalink"])
    # Reuse the known approver DM. Otherwise chat:write accepts a user ID, as on
    # access approval cards; no new im:write scope is required.
    channel = row.dm_channel or row.user
    if not re.fullmatch(r"[DUW][A-Z0-9]+", channel):
        raise RuntimeError("Approval reminder requires a private conversation")
    # Network calls can cross the deadline; don't deliver a known-expired reminder.
    remaining = row.deadline - time.time()
    if remaining <= 0:
        return
    minutes = math.ceil(remaining / 60)
    duration = f"{minutes} minute{'s' if minutes != 1 else ''}"
    text = f"<@{row.user}>, your {LABELS[kind]} expires in {duration}. Review it before it expires."
    await post_once(
        bridge, f"approval-reminder:{kind}:{identifier}:{stamp}", channel,
        text + f" <{url}|Review approval>",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [{
                "type": "button", "text": {"type": "plain_text", "text": "Review approval"},
                "url": url, "action_id": "approval_reminder_link",
            }]},
        ],
    )


async def process(bridge: BatchBridge, storage: Storage, kind: str, identifier: str, stamp: int
                  ) -> None:
    validate(kind, identifier)
    if kind == "transfer":
        row = await bridge.repository.get_connector_state("transfer-pending:" + identifier)
        if not row:
            return
        lock = "transfer-lock:" + row["project"]
    elif kind == "command":
        lock = "command-approval:" + identifier
    elif kind in {"access", "cloud_access"}:
        lock = approvals.resolve(kind).key(identifier)
    else:
        lock = "claude-poll:" + identifier
    async with approvals.lease(bridge, lock, seconds=240, busy_error=RuntimeError):
        reminder = await pending(bridge, storage, kind, identifier)
        if reminder:
            await deliver(bridge, kind, identifier, reminder, stamp)
