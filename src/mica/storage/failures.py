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

    @property
    def fingerprint(self) -> str:
        return digest(self.scope, self.kind, self.cause, self.project_id)

    @property
    def receipt(self) -> str:
        return digest(self.scope, self.kind, self.cause, self.occurrence)


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


def observed(old: Incident | None, observation: Observation, now: float, episode: str) -> Incident:
    if old is None or now - old.last_seen >= QUIET:
        return Incident(
            id=episode,
            observation=observation,
            first_seen=now,
            last_seen=now,
            expires_at=now + RETENTION,
            next_attempt=now,
        )
    row = old.model_copy(deep=True)
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
) -> Incident | None:
    if row.lease_token != token or row.lease_until < now:
        return None
    row = row.model_copy(deep=True)
    row.lease_token, row.lease_until = "", 0
    if uncertain and not row.slack_ts:
        row.delivery, row.next_attempt = "unconfirmed", now + 3600
    elif slack_ts:
        row.slack_ts, row.sent_count = slack_ts, sent_count
        row.delivery_attempts = 0
        row.delivery = "sent" if sent_count == row.count else "pending"
        row.next_attempt = NEVER if row.delivery == "sent" else now
    else:
        row.delivery = "pending"
        row.next_attempt = now + max(60, retry_after)
    return row
