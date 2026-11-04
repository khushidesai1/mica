"""Compact operator cards with a standard Block Kit compatibility rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from mica.storage.failures import Incident

VERSION = 3
PREFIX = "mica_failure_"
CATEGORIES = {
    "compute": "Compute",
    "runtime": "Package setup",
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
}


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_label(row: Incident, status: str) -> str:
    state = {
        "failed": "Failed / stopped",
        "active": "Retrying / still active",
        "complete": "Completed · follow-up needed",
        "recovered": "Recovered automatically",
    }.get(status, "Needs investigation")
    if row.resolved:
        return "Resolved by operator"
    return "Reopened · " + state if row.resolved_at else state


def title(row: Incident) -> str:
    o = row.observation
    return (
        (o.project_name or o.project_id or "User request")
        if row.user_related
        else "Mica service health"
    )


def preview(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= 200:
        return text
    return text[:199].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def blocks(
    row: Incident, explanations: list[str], status: str, *, link: str = "", native: bool = True
) -> list[dict[str, Any]]:
    o = row.observation
    category = CATEGORIES.get(o.kind, "Other")
    priority = (
        "High priority · User request" if row.user_related else "Low priority · Service health"
    )
    state = status_label(row, status)
    who = o.actor or ("Requester unknown" if row.user_related else "No linked user request")
    subtitle = clipped("Requester: " + who, 150) if row.user_related else who
    explanation = explanations[0] if explanations else "An operational failure was recorded."
    if len(explanations) > 1:
        explanation = (
            clipped(explanation, 160) + f" (+{len(explanations) - 1} related issues; see Details)"
        )
    if (
        row.investigation
        and row.investigation.status == "complete"
        and row.investigation.assessment
    ):
        explanation = row.investigation.assessment.summary
    latest = datetime.fromtimestamp(row.last_seen, UTC).strftime("%b %d, %H:%M UTC")
    subtext = clipped(
        f"{state}\n{row.count} reports · Latest {latest}\nFull assessment in Details", 200
    )
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
                "title": {"type": "plain_text", "text": clipped(title(row), 150)},
                "subtitle": {"type": "plain_text", "text": subtitle},
                "body": {"type": "plain_text", "text": preview(explanation)},
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
            "text": {"type": "plain_text", "text": clipped(title(row), 150)},
            "fields": [
                {"type": "plain_text", "text": subtitle},
                {"type": "plain_text", "text": state},
            ],
        },
        {"type": "section", "text": {"type": "plain_text", "text": preview(explanation)}},
        {"type": "context", "elements": [{"type": "plain_text", "text": subtext}]},
        {"type": "actions", "elements": actions},
    ]


def heading(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": clipped(text, 150)}}


def paragraph(label: str, text: str) -> dict[str, Any]:
    # Only our labels carry styling. Model/identity/diagnostic text stays literal,
    # so asterisks, Slack mention syntax and URLs cannot become formatting or links.
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {"type": "text", "text": label + "\n", "style": {"bold": True}},
                    {"type": "text", "text": text},
                ],
            }
        ],
    }


def facts(values: list[tuple[str, str]]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for label, value in values:
        if elements:
            elements.append({"type": "text", "text": "\n"})
        elements.extend(
            [
                {"type": "text", "text": label + ": ", "style": {"bold": True}},
                {"type": "text", "text": value},
            ]
        )
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}


def details_view(row: Incident, explanations: list[str]) -> dict[str, Any]:
    """A readable incident brief, keeping the full evidence behind the card's action."""
    o = row.observation
    status = row.request_status or "unknown"
    priority = (
        "High priority · User request" if row.user_related else "Low priority · Service health"
    )
    blocks: list[dict[str, Any]] = [
        heading(title(row)),
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": (f"{CATEGORIES.get(o.kind, 'Other')} · {priority}"),
                }
            ],
        },
        facts(
            [
                ("Status", status_label(row, status)),
                (
                    "Requester",
                    o.actor
                    or ("Requester unknown" if row.user_related else "No linked user request"),
                ),
                ("Project", o.project_name or o.project_id or "Unassigned"),
                (
                    "Source",
                    {"slack": "Slack", "email": "Email"}.get(o.surface, "Background service"),
                ),
            ]
        ),
        {"type": "divider"},
        heading("Assessment"),
    ]
    item = row.investigation
    assessment = item.assessment if item and item.status == "complete" else None
    if assessment:
        blocks.extend(
            [
                paragraph("What happened", assessment.summary),
                paragraph("Likely cause · unconfirmed", assessment.likely_cause),
                paragraph("Next check", assessment.next_check),
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "plain_text",
                            "text": (
                                "Claude assessed the saved diagnostics. "
                                "The likely cause still needs verification."
                            ),
                        }
                    ],
                },
            ]
        )
    else:
        pending = bool(item and item.status == "pending" and not row.resolved)
        blocks.append(
            paragraph(
                "Assessment pending" if pending else "Assessment unavailable",
                "The recorded evidence below remains available for investigation.",
            )
        )
    blocks.extend(
        [
            {"type": "divider"},
            heading("Recorded evidence"),
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "style": "bullet",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": text}],
                            }
                            for text in explanations or ["An operational failure was recorded."]
                        ],
                    }
                ],
            },
            paragraph(
                "Request outcome",
                {
                    "recovered": "The request completed after the earlier processing errors.",
                    "complete": (
                        "Request handling completed. "
                        "Recovery of the reported operations is not confirmed."
                    ),
                    "failed": "The request failed or stopped. Recovery is not confirmed.",
                    "active": (
                        "The request is still active or retrying. "
                        "Its final outcome is not yet known."
                    ),
                }.get(
                    status, "The request outcome could not be checked. Recovery is not confirmed."
                )
                if row.user_related
                else "This incident has not been linked to a specific user request.",
            ),
            facts(
                [
                    ("Reports", str(row.count)),
                    (
                        "First seen",
                        datetime.fromtimestamp(row.first_seen, UTC).strftime(
                            "%b %d, %Y · %H:%M UTC"
                        ),
                    ),
                    (
                        "Latest report",
                        datetime.fromtimestamp(row.last_seen, UTC).strftime(
                            "%b %d, %Y · %H:%M UTC"
                        ),
                    ),
                ]
            ),
            {"type": "divider"},
            heading("Technical references"),
            facts(
                [
                    (label, value)
                    for label, value in [
                        ("Incident", row.id),
                        ("Request", o.event_id),
                        ("Project ID", o.project_id),
                        ("Failure codes", ", ".join(row.causes or [o.cause])),
                        ("Service", o.service),
                        ("Route", o.route),
                        ("Revision", o.revision),
                        ("HTTP status", str(o.http_status) if o.http_status else ""),
                        ("Cloud API status", str(o.api_status) if o.api_status else ""),
                    ]
                    if value
                ]
            ),
        ]
    )
    if row.resolved_at:
        blocks.extend(
            [
                {"type": "divider"},
                heading("Operator review"),
                facts(
                    [
                        ("Reviewed by", row.resolved_by),
                        (
                            "Reviewed at",
                            datetime.fromtimestamp(row.resolved_at, UTC).strftime(
                                "%b %d, %Y · %H:%M UTC"
                            ),
                        ),
                        ("Reports reviewed", str(row.resolved_count)),
                    ]
                ),
                paragraph(
                    "Resolution",
                    (
                        "New reports arrived after the last review; this incident is open again."
                        if not row.resolved
                        else "Marked resolved by the operator. "
                        "This records an investigation decision, not job recovery."
                    ),
                ),
            ]
        )
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Failure details"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }
