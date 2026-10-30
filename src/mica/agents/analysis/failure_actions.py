"""Operator-only Slack incident actions, independent of task/project permissions."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import BackgroundTasks
from slack_sdk.web.async_client import AsyncWebClient

from mica.config import Settings as AppSettings
from mica.storage.base import Repository

from . import failure_cards, observability
from .config import Settings


def is_action(payload: dict[str, Any]) -> bool:
    actions = payload.get("actions")
    return (
        payload.get("type") == "block_actions"
        and isinstance(actions, list)
        and len(actions) == 1
        and isinstance(actions[0], dict)
        and str(actions[0].get("action_id", "")).startswith(failure_cards.PREFIX)
    )


async def refresh(repository: Repository, app: AppSettings, settings: Settings, key: str) -> None:
    try:
        await observability.flush(repository, app, settings, incident_id=key)
    except Exception:
        # The durable pending update remains available to scheduled maintenance.
        observability.log.warning("failure_reporter card_refresh_pending incident=%s", key)


async def handle(
    repository: Repository,
    app: AppSettings,
    settings: Settings,
    payload: dict[str, Any],
    background: BackgroundTasks,
) -> dict[str, object]:
    action = payload["actions"][0]
    action_id = action.get("action_id")
    if action_id == failure_cards.PREFIX + "open":
        return {}  # Acknowledge URL buttons; never use payload URLs for network calls.
    if action_id not in {failure_cards.PREFIX + "resolve", failure_cards.PREFIX + "details"}:
        raise ValueError("Unknown failure action.")
    team = str(payload.get("team", {}).get("id", ""))
    user = str(payload.get("user", {}).get("id", ""))
    actor = f"{team}:{user}"
    if not re.fullmatch(r"T[A-Z0-9]+:[UW][A-Z0-9]+", actor) or actor not in settings.csv(
        "failure_operator_ids"
    ):
        raise PermissionError("Only designated failure operators can review this alert.")
    channel = str(payload.get("channel", {}).get("id", ""))
    container = payload.get("container", {})
    ts = str(payload.get("message", {}).get("ts") or container.get("message_ts", ""))
    if (
        not settings.failure_channel
        or channel != settings.failure_channel
        or container.get("channel_id", channel) != channel
        or container.get("message_ts", ts) != ts
    ):
        raise PermissionError("This action must come from the configured failure channel.")
    try:
        value = json.loads(action.get("value", ""))
        key, count = value["id"], value["count"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError
        if type(count) is not int or count < 1:
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid incident context.") from exc
    row = await repository.get_failure(key)
    if not row or not ts or row.channel != channel or row.slack_ts != ts:
        raise PermissionError("The incident does not match this message.")
    client = AsyncWebClient(token=app.slack_bot_token, timeout=5, retry_handlers=[])
    if action_id == failure_cards.PREFIX + "details":
        trigger = str(payload.get("trigger_id", ""))
        if not trigger:
            raise ValueError("Missing Slack interaction context.")
        explanations = [
            observability.CATALOG[cause][1]
            for cause in row.causes or [row.observation.cause]
            if cause in observability.CATALOG
        ]
        await client.views_open(
            trigger_id=trigger,
            view=failure_cards.details_view(row, explanations),
        )
        return {}
    result = await repository.resolve_failure(key, count, actor, channel, ts, time.time())
    if result is None:
        await client.chat_postEphemeral(
            channel=channel,
            user=user,
            text=(
                "New reports arrived after this card was shown. "
                "Review the updated card before resolving it."
            ),
        )
    background.add_task(refresh, repository, app, settings, key)
    return {}
