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
    "reference_repo_failed",
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
    "github_catalog_failed": ("catalog", "The scheduled GitHub tool-catalog refresh failed."),
    "infra_http_error": ("infrastructure", "Cloud Run recorded a failed HTTP request."),
    "infra_platform_error": ("infrastructure", "Cloud Run reported a platform error."),
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
    "reference_repo_failed": (
        "reference",
        "A project reference repository could not be loaded; the request continues without it.",
    ),
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


def request_context(record: dict[str, Any]) -> dict[str, Any]:
    """Only previously authorized identity and bounded Slack coordinates, never content."""
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
    payload = record.get("payload", {})
    surface = record.get("surface")
    if surface not in {"email", "slack"}:
        surface = (
            "email" if payload.get("_email") else "slack" if payload.get("event") else "background"
        )
    event = payload.get("event", {}) if surface == "slack" else {}
    channel = str(event.get("channel", ""))
    thread = str(event.get("thread_ts") or event.get("ts", ""))
    return {
        "project_id": reference(str(project_id)) if project_id else "",
        "project_name": label(project_name),
        "actor": label(actor, 254),
        "surface": surface,
        "request_scoped": surface in {"email", "slack"},
        "slack_channel": channel if re.fullmatch(r"[CDG][A-Z0-9]{1,30}", channel) else "",
        "slack_thread_ts": thread if re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,10}", thread) else "",
    }


def request_scope(app: AppSettings) -> str:
    return f"{app.gcp_project}:{app.env}:{app.control_plane_url}"


async def correlate_platform(
    repository: Repository, app: AppSettings, observation: Observation
) -> Observation:
    """Resolve only internal process IDs; a platform status never establishes identity."""
    if (
        observation.source != "cloud_run"
        or observation.route != "/api/v1/internal/slack-analysis/process/{event_id}/{attempt}"
        or not re.fullmatch(r"[a-f0-9]{32}", observation.event_id)
    ):
        return observation
    record = await repository.get_connector_state(f"claude-event:{observation.event_id}")
    if not record:
        return observation.model_copy(update={"event_id": ""})
    context = request_context(record)
    if not context["request_scoped"]:
        return observation.model_copy(update={"event_id": ""})
    return observation.model_copy(
        update={"scope": request_scope(app), "receipt_scope": observation.scope, **context}
    )


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
        observation = Observation(
            scope=request_scope(app),
            kind=kind,
            cause=cause,
            occurrence=digest(occurrence),
            event_id=reference(event_id) if event_id else "",
            **request_context(record),
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


def summarize(row: Incident, *, request_status: str = "unknown") -> str:
    o = row.observation
    summary = CATALOG.get(o.cause, ("unknown", "An operational failure was recorded."))[1]
    first = datetime.fromtimestamp(row.first_seen, UTC).strftime("%Y-%m-%d %H:%M UTC")
    last = datetime.fromtimestamp(row.last_seen, UTC).strftime("%Y-%m-%d %H:%M UTC")
    if o.source == "cloud_run" and not o.request_scoped:
        return (
            f"Mica infrastructure health alert\n{summary}\n"
            "No specific user request has been linked to this alert.\n"
            f"Source: Cloud Run · route: {escape(o.route)} · HTTP: {o.http_status or 'n/a'}\n"
            f"Revision: {escape(o.revision or 'unknown')}\n"
            f"Occurrences: {row.count} · first {first} · latest {last}\n"
            f"Service: {escape(o.service)} · project/requester unavailable in platform logs\n"
            f"Incident: {row.id} · {o.cause} · recovery not confirmed"
        )
    project = o.project_name or o.project_id or "No project assigned"
    who = o.actor or (
        "Background maintenance" if o.surface == "background" else "Requester unknown"
    )
    if o.request_scoped:
        # Completion of a parent request does not prove recovery of memory, delivery,
        # reference loading or a handed-off transfer. Keep every diagnosis visible.
        states = {
            "complete": (
                "Request handling completed; recovery of the reported operations is not confirmed."
            ),
            "recovered": "Request completed after the earlier processing errors.",
            "failed": "Request failed or stopped; recovery not confirmed.",
            "active": "Request is still active or retrying; final outcome not yet known.",
            "unknown": "Request outcome could not be checked; recovery not confirmed.",
        }
        causes = row.causes or [o.cause]
        explanations = [CATALOG[c][1] for c in causes if c in CATALOG and c != "infra_http_error"]
        if not explanations:
            explanations = ["Mica encountered a service error while processing this request."]
        details = "\n".join(explanations)
        platform = (
            f"\nService evidence: HTTP {o.http_status} · {escape(o.revision or o.service)}"
            if o.http_status
            else ""
        )
        return (
            f"Mica request issue · {escape(project)}\n{details}\n"
            f"Requester: {escape(who)} · {o.surface}\n"
            f"{states.get(request_status, states['unknown'])}{platform}\n"
            f"Reports: {row.count} · first {first} · latest {last}\n"
            f"Project: {escape(o.project_id or 'unassigned')} · request: {escape(o.event_id)}\n"
            f"Incident: {row.id} · {', '.join(causes)}"
        )
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
            request_status = "unknown"
            if row.observation.request_scoped:
                try:
                    record = await repository.get_connector_state(
                        f"claude-event:{row.observation.event_id}"
                    )
                    if record:
                        status = record.get("status")
                        request_status = status if status in {"complete", "failed"} else "active"
                        if request_status == "complete" and set(
                            row.causes or [row.observation.cause]
                        ) <= {
                            "infra_http_error",
                            "routing_exhausted",
                            "conversation_failed",
                            "compute_control_failed",
                            "compute_failed",
                            "compute_deadline",
                            "access_unverifiable",
                            "bucket_unavailable",
                        }:
                            request_status = "recovered"
                except Exception:
                    pass  # Status lookup must not hide an existing incident.
            review_after = (
                300
                if row.observation.request_scoped and request_status in {"active", "unknown"}
                else 0
            )
            if (
                row.slack_ts
                and row.sent_count == row.count
                and row.request_status == request_status
            ):
                await repository.finish_failure(
                    row.id,
                    token,
                    time.time(),
                    slack_ts=row.slack_ts,
                    sent_count=row.count,
                    retry_after=review_after,
                    request_status=request_status,
                )
                continue
            try:
                params: dict[str, Any] = dict(
                    channel=row.channel,
                    text=summarize(row, request_status=request_status),
                    parse="none",
                    mrkdwn=False,
                )
                o = row.observation
                if o.slack_channel and o.slack_thread_ts:
                    try:
                        result = await client.chat_getPermalink(
                            channel=o.slack_channel, message_ts=o.slack_thread_ts
                        )
                        link = result.get("permalink", "")
                        if isinstance(link, str) and re.fullmatch(
                            r"https://[a-z0-9-]+\.slack\.com/archives/[CDG][A-Z0-9]+/p[0-9]+", link
                        ):
                            block_text = params["text"]
                            if len(block_text) > 3000:
                                block_text = block_text[:2999] + "…"
                            params["blocks"] = [
                                {
                                    "type": "section",
                                    "text": {"type": "plain_text", "text": block_text},
                                },
                                {
                                    "type": "context",
                                    "elements": [
                                        {"type": "mrkdwn", "text": f"<{link}|Open request thread>"}
                                    ],
                                },
                            ]
                    except Exception:
                        pass  # Thread links are optional, and grant no new Slack access.
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
                    # Recheck active requests so a temporary 5xx does not leave a permanent
                    # failure label after the request completes. Never count this as an error.
                    retry_after=review_after,
                    request_status=request_status,
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
