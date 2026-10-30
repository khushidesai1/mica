"""Compact operator cards with a standard Block Kit compatibility rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mica.storage.failures import Incident

VERSION = 1
PREFIX = "mica_failure_"


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def blocks(
    row: Incident, explanations: list[str], status: str, *, link: str = "", native: bool = True
) -> list[dict[str, Any]]:
    o = row.observation
    category = {
        "compute": "Compute",
        "routing": "Routing",
        "turn": "Conversation",
        "access": "Access",
        "reference": "Repository",
        "memory": "Memory",
        "delivery": "Delivery",
        "transfer": "Transfer",
        "catalog": "Tool catalog",
        "infrastructure": "Infrastructure",
        "test": "Test",
    }.get(o.kind, "Other")
    priority = (
        "High priority · User request" if row.user_related else "Low priority · Service health"
    )
    state = {
        "failed": "Failed / stopped",
        "active": "Retrying / still active",
        "complete": "Completed · follow-up needed",
        "recovered": "Recovered automatically",
    }.get(status, "Needs investigation")
    if row.resolved:
        state = "Resolved by operator"
    elif row.resolved_at:
        state = "Reopened · " + state
    title = (
        (o.project_name or o.project_id or "User request")
        if row.user_related
        else "Mica service health"
    )
    who = o.actor or ("Requester unknown" if row.user_related else "No linked user request")
    subtitle = clipped("Requester: " + who, 150) if row.user_related else who
    explanation = explanations[0] if explanations else "An operational failure was recorded."
    if len(explanations) > 1:
        explanation = (
            clipped(explanation, 160) + f" (+{len(explanations) - 1} related issues; see Details)"
        )
    latest = datetime.fromtimestamp(row.last_seen, UTC).strftime("%b %d, %H:%M UTC")
    subtext = clipped(f"{state}\n{row.count} reports · Latest {latest}", 200)
    value = json.dumps({"id": row.id, "count": row.count}, separators=(",", ":"))
    actions: list[dict[str, Any]] = []
    if link:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open request thread"},
                "action_id": PREFIX + "open",
                "url": link,
            }
        )
    actions.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Details"},
            "action_id": PREFIX + "details",
            "value": value,
        }
    )
    if not row.resolved:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Mark resolved"},
                "style": "primary",
                "action_id": PREFIX + "resolve",
                "value": value,
            }
        )
    tags = [
        {
            "type": "tag",
            "text": priority,
            "color": "red" if row.user_related and not row.resolved else "gray",
        },
        {"type": "tag", "text": category, "color": "blue" if row.user_related else "gray"},
    ]
    if row.resolved:
        tags.append({"type": "tag", "text": "Resolved", "color": "green"})
    if native:
        return [
            {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": tags}]},
            {
                "type": "card",
                "title": {"type": "plain_text", "text": clipped(title, 150)},
                "subtitle": {"type": "plain_text", "text": subtitle},
                "body": {"type": "plain_text", "text": clipped(explanation, 200)},
                "subtext": {"type": "plain_text", "text": subtext},
                "actions": actions,
            },
        ]
    return [
        {
            "type": "context",
            "elements": [{"type": "plain_text", "text": " · ".join(t["text"] for t in tags)}],
        },
        {
            "type": "section",
            "text": {"type": "plain_text", "text": clipped(title, 150)},
            "fields": [
                {"type": "plain_text", "text": subtitle},
                {"type": "plain_text", "text": state},
            ],
        },
        {"type": "section", "text": {"type": "plain_text", "text": clipped(explanation, 3000)}},
        {"type": "context", "elements": [{"type": "plain_text", "text": subtext.split("\n")[-1]}]},
        {"type": "actions", "elements": actions},
    ]
