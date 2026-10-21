"""Best-effort incident recording and leased, one-way Slack summaries."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from uuid import NAMESPACE_URL, uuid5

from mica.config import Settings as AppSettings
from mica.storage.base import Repository
from mica.storage.failures import Incident, Observation, digest

from .config import Settings
from .project_memory import SECRET

if TYPE_CHECKING:
    from .access import Grant
    from .batch import BatchBridge

log = logging.getLogger("mica.failure_reporter")
Cause = Literal[
    "routing_exhausted",
    "conversation_failed",
    "access_unverifiable",
    "bucket_unavailable",
    "compute_failed",
    "compute_control_failed",
    "compute_deadline",
    "memory_editor_failed",
    "memory_compaction_retry",
    "memory_compaction_exhausted",
    "email_delivery_failed",
    "email_delivery_unconfirmed",
    "slack_delivery_failed",
    "atomx_inventory_failed",
    "atomx_pull_failed",
    "atomx_auth_failed",
    "atomx_control_failed",
    "atomx_delivery_failed",
    "smoke_test",
]
CATALOG: dict[str, tuple[str, str]] = {
    "routing_exhausted": ("routing", "Request routing exhausted its automatic retries."),
    "conversation_failed": ("turn", "The conversation could not complete an internal operation."),
    "access_unverifiable": (
        "access",
        "Project access could not be verified within the grace period.",
    ),
    "bucket_unavailable": ("access", "The service could not verify access to project storage."),
    "compute_failed": ("compute", "The worker stopped without completing the request."),
    "compute_control_failed": ("compute", "The service could not submit or inspect its worker."),
    "compute_deadline": ("compute", "The request exhausted its computation time allowance."),
    "memory_editor_failed": ("memory", "Project memory editing failed; its task may retry."),
    "memory_compaction_retry": ("memory", "Project memory compaction failed and needs a retry."),
    "memory_compaction_exhausted": (
        "memory",
        "Project memory compaction exhausted its safe attempts.",
    ),
    "email_delivery_failed": ("delivery", "An email response could not be delivered."),
    "email_delivery_unconfirmed": (
        "delivery",
        "An email send is unconfirmed and needs outbox review.",
    ),
    "slack_delivery_failed": ("delivery", "A Slack response could not be delivered."),
    "atomx_inventory_failed": ("transfer", "The AtoMx inventory worker failed."),
    "atomx_pull_failed": ("transfer", "The AtoMx transfer worker failed."),
    "atomx_auth_failed": ("transfer", "The saved AtoMx connection failed authentication."),
    "atomx_control_failed": ("transfer", "The AtoMx controller could not advance a transfer."),
    "atomx_delivery_failed": ("delivery", "A private AtoMx Slack response could not be delivered."),
    "smoke_test": ("test", "Synthetic Mica incident; no user job failed."),
}


def label(value: object, limit: int = 100) -> str:
    text = str(value or "")
    if SECRET.search(text) or "https://" in text or "gs://" in text:
        return "[redacted]"
    return re.sub(r"[\r\n\x00-\x1f]", " ", text)[:limit]


def reference(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) else digest(value)[:32]


def capture_project(record: dict[str, Any], grant: Grant, *, target: str = "") -> bool:
    """Snapshot authorized request context, including early account/command replies.

    Diagnostic fields never grant access or change routing. A retry must not replace
    the original context with a newly selected default project.
    """
    if target:
        if not grant.doc.can_use(target, grant.email):
            return False
        if (
            not grant.private
            and grant.channel_key not in grant.doc.projects[target].published_channels
        ):
            return False
    elif not grant.allowed or "failure_project_id" in record:
        return False
    changed = bool(target) and not record.get("failure_project_target", False)
    if target:
        record["failure_project_target"] = True
    project_id = target or grant.project_id
    if record.get("failure_project_id") != project_id:
        record.update(
            failure_project_id=project_id,
            failure_project_name=grant.doc.projects[project_id].name,
        )
        return True
    return bool(changed)


async def record_failure(
    repository: Repository,
    app: AppSettings,
    cause: Cause,
    *,
    occurrence: str,
    event_id: str = "",
    record: dict[str, Any] | None = None,
) -> None:
    """No external identity lookup, arbitrary error text, or exception propagation."""
    try:
        record = record or {}
        kind, _ = CATALOG[cause]
        actor = str(record.get("principal") or record.get("intake_principal") or "")
        if not re.fullmatch(r"[^\s<>@]{1,64}@[^\s<>@]{1,190}", actor):
            actor = ""
        if record.get("failure_project_target") and record.get("failure_project_id"):
            project_id = record["failure_project_id"]
            project_name = record.get("failure_project_name")
        else:
            project_id = record.get("project_id") or record.get("failure_project_id")
            project_name = (
                record.get("project_name")
                if record.get("project_id")
                else record.get("failure_project_name") or record.get("project_name")
            )
        observation = Observation(
            scope=f"{app.gcp_project}:{app.env}:{app.control_plane_url}",
            kind=kind,
            cause=cause,
            occurrence=digest(occurrence),
            event_id=reference(event_id) if event_id else "",
            project_id=reference(str(project_id)) if project_id else "",
            project_name=label(project_name),
            actor=label(actor, 254),
            surface=(
                record["surface"]
                if record.get("surface") in {"email", "slack"}
                else (
                    "email"
                    if record.get("payload", {}).get("_email")
                    else ("slack" if record.get("payload", {}).get("event") else "background")
                )
            ),
        )
        async with asyncio.timeout(3):
            await repository.record_failure(observation, time.time())
    except Exception:
        # Reporter logs are explicitly identifiable; never log the failed payload/exception.
        log.warning(
            "failure_reporter persistence_unavailable cause=%s",
            cause if cause in CATALOG else "unknown",
        )


async def report(
    bridge: BatchBridge,
    cause: Cause,
    event_id: str,
    record: dict[str, Any],
    *,
    unit: str = "",
) -> None:
    await record_failure(
        bridge.repository,
        bridge.app,
        cause,
        event_id=event_id,
        record=record,
        occurrence=f"{event_id}:{unit or 'terminal'}",
    )


def summarize(row: Incident) -> str:
    o = row.observation
    summary = CATALOG.get(o.cause, ("unknown", "An operational failure was recorded."))[1]
    project = o.project_name or o.project_id or "No project assigned"
    who = o.actor or (
        "Background maintenance" if o.surface == "background" else "Requester unknown"
    )
    first = datetime.fromtimestamp(row.first_seen, UTC).strftime("%Y-%m-%d %H:%M UTC")
    last = datetime.fromtimestamp(row.last_seen, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"Mica failure · {escape(project)}\n{summary}\n"
        f"Latest requester: {escape(who)} · {o.surface}\n"
        f"Occurrences: {row.count} · first {first} · latest {last}\n"
        f"Project: {escape(o.project_id or 'unassigned')} · event: {escape(o.event_id or 'none')}\n"
        f"Incident: {row.id} · {o.cause} · recovery not confirmed"
    )


async def flush(repository: Repository, app: AppSettings, settings: Settings) -> dict[str, int]:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    counts = {
        "failure_sent": 0,
        "failure_pending": 0,
        "failure_unconfirmed": 0,
        "failure_reconciled": 0,
    }
    if not settings.failure_channel:
        return counts
    # No SDK-level retries of a potentially accepted first post.
    client = AsyncWebClient(token=app.slack_bot_token, timeout=5, retry_handlers=[])
    async with asyncio.timeout(45):
        for item in await repository.due_failures(time.time(), limit=5):
            row = await repository.claim_failure(item.id, settings.failure_channel, time.time())
            if not row:
                continue
            if row.delivery == "unconfirmed":
                # History access is optional. Its absence never authorizes another post.
                found = ""
                try:
                    who = await client.auth_test()
                    history = await client.conversations_history(
                        channel=row.channel,
                        oldest=str(row.send_started - 30),
                        latest=str(row.send_started + 90),
                        inclusive=True,
                        limit=100,
                    )
                    messages: list[dict[str, Any]] = history.get("messages", [])
                    matches = [
                        m
                        for m in messages
                        if m.get("client_msg_id")
                        == str(uuid5(NAMESPACE_URL, "mica-failure:" + row.id))
                        and m.get("user") == who["user_id"]
                    ]
                    if len(matches) == 1:
                        candidate = str(matches[0].get("ts", ""))
                        if re.fullmatch(r"\d+\.\d+", candidate):
                            found = candidate
                except Exception:
                    pass
                await repository.finish_failure(
                    row.id,
                    row.lease_token,
                    time.time(),
                    slack_ts=found,
                    sent_count=0,
                    uncertain=not bool(found),
                )
                counts["failure_reconciled" if found else "failure_unconfirmed"] += 1
                if not found:
                    log.warning("failure_reporter delivery_unconfirmed incident=%s", row.id)
                continue
            token = row.lease_token
            try:
                params: dict[str, Any] = dict(
                    channel=row.channel, text=summarize(row), parse="none", mrkdwn=False
                )
                if row.slack_ts:
                    response = await client.chat_update(ts=row.slack_ts, **params)
                else:
                    response = await client.chat_postMessage(
                        **params,
                        unfurl_links=False,
                        unfurl_media=False,
                        client_msg_id=str(uuid5(NAMESPACE_URL, "mica-failure:" + row.id)),
                    )
                ts = str(response["ts"])
                if not re.fullmatch(r"\d+\.\d+", ts):
                    raise ValueError("Missing delivery receipt")
            except Exception as exc:
                # Only explicit API rejections prove a first post was not accepted.
                rejected = False
                delay = min(3600, 60 * 2 ** min(row.delivery_attempts, 6))
                if isinstance(exc, SlackApiError):
                    error = exc.response.get("error", "")
                    rejected = error in {
                        "ratelimited",
                        "channel_not_found",
                        "not_in_channel",
                        "is_archived",
                        "missing_scope",
                        "invalid_auth",
                        "token_revoked",
                        "account_inactive",
                        "no_permission",
                        "restricted_action",
                        "msg_too_long",
                        "invalid_arguments",
                    }
                    if exc.response.status_code == 429:
                        rejected = True
                        delay = max(delay, int(exc.response.headers.get("Retry-After", "60")))
                uncertain = not row.slack_ts and not rejected
                await repository.finish_failure(
                    row.id,
                    token,
                    time.time(),
                    retry_after=delay,
                    uncertain=uncertain,
                )
                counts["failure_unconfirmed" if uncertain else "failure_pending"] += 1
                log.warning(
                    "failure_reporter delivery_%s incident=%s",
                    "unconfirmed" if uncertain else "pending",
                    row.id,
                )
                if isinstance(exc, SlackApiError) and exc.response.status_code == 429:
                    break
            else:
                await repository.finish_failure(
                    row.id,
                    token,
                    time.time(),
                    slack_ts=ts,
                    sent_count=row.count,
                )
                counts["failure_sent"] += 1
    return counts


async def maintenance(
    repository: Repository, app: AppSettings, *, purge: bool = False
) -> dict[str, int]:
    try:
        if purge:
            async with asyncio.timeout(30):
                return {"failures_purged": await repository.purge_failures(time.time())}
        return await flush(repository, app, Settings())
    except Exception:
        log.warning("failure_reporter maintenance_unavailable")
        return {"failure_maintenance_errors": 1}
