"""Typed incident state and pure transitions shared by repository implementations."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel

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
) -> Incident | None:
    if row.lease_token != token or row.lease_until < now:
        return None
    row = row.model_copy(deep=True)
    row.lease_token, row.lease_until = "", 0
    if uncertain and not row.slack_ts:
        row.delivery, row.next_attempt = "unconfirmed", now + 3600
    elif slack_ts:
        row.slack_ts, row.sent_count = slack_ts, sent_count
        row.request_status = request_status
        row.delivery_attempts = 0
        row.delivery = "sent" if sent_count == row.count else "pending"
        row.next_attempt = (
            (now + retry_after if retry_after else NEVER) if row.delivery == "sent" else now
        )
    else:
        row.delivery = "pending"
        row.next_attempt = now + max(60, retry_after)
    return row
