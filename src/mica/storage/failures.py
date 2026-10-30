"""Typed incident state and pure transitions shared by repository implementations."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DAY = 86400
RETENTION = 30 * DAY
QUIET = DAY
NEVER = 1e15


class Observation(BaseModel):
    scope: str
    kind: str
    cause: str
    occurrence: str
    project_id: str = ""
    project_name: str = ""
    actor: str = ""
    surface: str = "background"
    event_id: str = ""
    source: Literal["application", "cloud_run"] = "application"
    service: str = ""
    route: str = ""
    http_status: int = 0
    revision: str = ""
    request_scoped: bool = False
    slack_channel: str = ""
    slack_thread_ts: str = ""
    receipt_scope: str = ""
    api_status: int = 0

    @property
    def fingerprint(self) -> str:
        if self.request_scoped and self.event_id:
            # A request may fail before project routing and across several phases.
            # Platform and semantic reports must select the same incident in either order.
            return digest(self.scope, "request", self.event_id)
        return digest(self.scope, self.kind, self.cause, self.project_id)

    @property
    def receipt(self) -> str:
        return digest(self.receipt_scope or self.scope, self.kind, self.cause, self.occurrence)


def digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


class FailureAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Slack clips the card preview separately. Preserve modestly longer sentences in
    # Details instead of rejecting an otherwise useful provider response.
    summary: str = Field(min_length=1, max_length=500)
    likely_cause: str = Field(min_length=1, max_length=500)
    next_check: str = Field(min_length=1, max_length=500)


class FailureInvestigation(BaseModel):
    key: str
    evidence: dict[str, Any]
    status: Literal["pending", "complete", "unavailable"] = "pending"
    attempts: int = 0
    assessment: FailureAssessment | None = None


class Incident(BaseModel):
    id: str
    observation: Observation
    first_seen: float
    last_seen: float
    expires_at: float
    count: int = 1
    sent_count: int = 0
    channel: str = ""
    slack_ts: str = ""
    delivery: Literal["pending", "sending", "unconfirmed", "sent"] = "pending"
    next_attempt: float = 0
    lease_token: str = ""
    lease_until: float = 0
    send_started: float = 0
    delivery_attempts: int = 0
    causes: list[str] = []
    request_status: str = ""
    resolved_at: float = 0
    resolved_by: str = ""
    resolved_count: int = 0
    sent_resolved_count: int = 0
    card_version: int = 0
    investigation: FailureInvestigation | None = None
    sent_investigation: str = ""

    @property
    def resolved(self) -> bool:
        return self.resolved_at > 0 and self.resolved_count == self.count

    @property
    def user_related(self) -> bool:
        o = self.observation
        return o.request_scoped or bool(o.event_id and o.surface in {"slack", "email"})


def document(row: Incident) -> dict[str, Any]:
    """Single-field queue projections; no composite index or infrastructure change."""
    return {
        **row.model_dump(),
        "request_due": row.next_attempt if row.user_related else NEVER,
        "background_due": NEVER if row.user_related else row.next_attempt,
    }


def prioritized(rows: list[Incident], now: float, limit: int) -> list[Incident]:
    due = sorted(
        {r.id: r for r in rows if r.next_attempt <= now and r.lease_until <= now}.values(),
        key=lambda r: (r.next_attempt, r.id),
    )
    requests = [r for r in due if r.user_related]
    background = [r for r in due if not r.user_related]
    # Reserve room for one service-health issue, without letting a noisy backlog
    # displace user failures. With no request failures, send at most one per cycle.
    slots = max(0, limit - int(bool(background) and limit > 1))
    selected = requests[:slots]
    return (selected + background[:1])[:limit]


def resolved(
    row: Incident, count: int, actor: str, channel: str, slack_ts: str, now: float
) -> Incident | None:
    """Compare-and-set the exact occurrence count the operator actually reviewed."""
    if not slack_ts or row.channel != channel or row.slack_ts != slack_ts or row.count != count:
        return None
    if row.resolved:
        return row
    row = row.model_copy(deep=True)
    row.resolved_at, row.resolved_by, row.resolved_count = now, actor, count
    row.next_attempt = max(now, row.lease_until)
    # Preserve an active delivery lease; its stale rendering must finish as pending.
    if not row.lease_token:
        row.delivery = "pending"
    return row


def observed(old: Incident | None, observation: Observation, now: float, episode: str) -> Incident:
    if old is None or (not observation.request_scoped and now - old.last_seen >= QUIET):
        return Incident(
            id=episode,
            observation=observation,
            first_seen=now,
            last_seen=now,
            expires_at=now + RETENTION,
            next_attempt=now,
            causes=[observation.cause],
        )
    row = old.model_copy(deep=True)
    row.causes = list(dict.fromkeys([*(row.causes or [row.observation.cause]), observation.cause]))
    if observation.request_scoped:
        previous = row.observation
        context = {
            field: getattr(observation, field) or getattr(previous, field)
            for field in (
                "project_id",
                "project_name",
                "actor",
                "slack_channel",
                "slack_thread_ts",
                "service",
                "route",
                "http_status",
                "revision",
                "api_status",
            )
        }
        if observation.project_id and observation.project_id != previous.project_id:
            context["project_name"] = observation.project_name
        # Late platform delivery adds evidence without replacing a more useful diagnosis.
        if observation.source == "cloud_run" and previous.source == "application":
            observation = previous
        observation = observation.model_copy(update=context)
    row.observation = observation
    row.count += 1
    row.last_seen = max(row.last_seen, now)
    row.expires_at = row.last_seen + RETENTION
    if row.delivery == "sent":
        row.delivery = "pending"
        row.next_attempt = now
    return row


def claimed(row: Incident, channel: str, token: str, now: float) -> Incident | None:
    if row.next_attempt > now or row.lease_until > now:
        return None
    row = row.model_copy(deep=True)
    if row.delivery == "sending" and not row.slack_ts:
        # The prior process may have posted before dying. Never post it again blindly.
        row.delivery = "unconfirmed"
    row.channel = row.channel or channel
    if row.delivery != "unconfirmed":
        row.delivery = "sending"
    row.lease_token, row.lease_until = token, now + 90
    row.next_attempt = row.lease_until
    if row.delivery != "unconfirmed":
        row.send_started = now
    row.delivery_attempts += 1
    return row


def finished(
    row: Incident,
    token: str,
    now: float,
    *,
    slack_ts: str = "",
    sent_count: int = 0,
    retry_after: float = 0,
    uncertain: bool = False,
    request_status: str = "",
    sent_resolved_count: int = 0,
    card_version: int = 0,
    investigation: FailureInvestigation | None = None,
    sent_investigation: str = "",
) -> Incident | None:
    if row.lease_token != token or row.lease_until < now:
        return None
    row = row.model_copy(deep=True)
    row.lease_token, row.lease_until = "", 0
    if investigation is not None:
        row.investigation = investigation
    if uncertain and not row.slack_ts:
        row.delivery, row.next_attempt = "unconfirmed", now + 3600
    elif slack_ts:
        row.slack_ts, row.sent_count = slack_ts, sent_count
        row.request_status = request_status
        row.sent_resolved_count = sent_resolved_count
        row.card_version = card_version
        row.sent_investigation = sent_investigation
        row.delivery_attempts = 0
        row.delivery = (
            "sent"
            if sent_count == row.count and sent_resolved_count == row.resolved_count
            else "pending"
        )
        row.next_attempt = (
            (now + retry_after if retry_after else NEVER) if row.delivery == "sent" else now
        )
        if row.investigation and row.investigation.status == "pending" and not row.resolved:
            row.next_attempt = now
    else:
        row.delivery = "pending"
        row.next_attempt = now + max(60, retry_after)
    return row
