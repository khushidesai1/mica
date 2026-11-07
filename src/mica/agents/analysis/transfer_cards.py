"""Requester-facing transfer approvals and compact activity, with literal source paths."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from html import unescape
from typing import Any

from .progress import TaskProgress
from .slack_format import activity_blocks

PREFIX = "mica_transfer_"
TERMINAL = {"done", "failed", "cancelled", "expired"}


def token(pending: dict[str, Any]) -> str:
    """Bind a click to the displayed plan, connection and approval lifetime, not a poll tick."""
    fields = ("id", "attempt", "request", "config", "preview", "login_version", "expires")
    value = {name: pending.get(name) for name in fields}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def size(value: int) -> str:
    for unit, divisor in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 1000)):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit}"
    return f"{value:,} bytes"


def button(p: dict[str, Any], action: str, label: str) -> dict[str, Any]:
    return {
        "type": "button", "text": {"type": "plain_text", "text": label},
        "action_id": PREFIX + action, "value": p["id"] + ":" + token(p),
        **({"style": "primary"} if action == "confirm" else {}),
    }


def sections(label: str, value: str) -> list[dict[str, Any]]:
    # Full paths must remain reviewable; never silently truncate the approved destination.
    text = label + "\n" + value
    return [
        {"type": "section", "text": {"type": "plain_text", "text": text[i:i + 2900]}}
        for i in range(0, len(text), 2900)
    ]


def blocks(
    p: dict[str, Any], text: str, buttons: tuple[str, ...], *, login_url: str = ""
) -> list[dict[str, Any]]:
    state = p["state"]
    request, preview, config = (p.get(k) or {} for k in ("request", "preview", "config"))
    target = str(request.get("as_sample") or request.get("sample") or "sample")
    actions = []
    if state == "preview" and "confirm" in buttons:
        actions.append(button(p, "confirm", "Approve transfer"))
    if login_url:
        actions.append({
            "type": "button", "text": {"type": "plain_text", "text": "Connect AtoMx"},
            "action_id": PREFIX + "connection_link", "url": login_url,
        })
    if state not in TERMINAL and state != "cancelling":
        actions.append(button(p, "cancel", "Cancel transfer"))
    result: list[dict[str, Any]]
    if state == "preview" and preview:
        source = f"{preview['export_root']} / {preview['run_folder']}"
        deadline = datetime.fromtimestamp(float(p["expires"]), UTC).strftime("%H:%M UTC")
        result = [
            {"type": "header", "text": {"type": "plain_text", "text": "Approve AtoMx transfer"}},
            {"type": "section", "fields": [
                {"type": "plain_text", "text": "Sample\n" + str(preview["sample"])},
                {"type": "plain_text", "text": "Save as\n" + target},
                {"type": "plain_text", "text": (
                    f"{int(preview['n_files']):,} files · {size(int(preview['total_bytes']))}"
                )},
            ]},
            *sections("Source export / run", source),
            *sections("Raw files", f"gs://{config['bucket']}/{config['raw_prefix']}{target}/"),
            *sections("Additional files", (
                f"gs://{config['bucket']}/{config['additional_prefix']}{target}/"
            )),
            {"type": "context", "elements": [{"type": "plain_text", "text": (
                f"Uses your saved personal connection · Approve by {deadline}\n"
                "Existing files are kept. New uploads are verified before completion."
            )}]},
        ]
    elif state == "login":
        result = [
            {"type": "header", "text": {"type": "plain_text", "text": "Connect AtoMx"}},
            *sections("Personal connection needed", unescape(text)),
        ]
    else:
        title = {
            "prepare": "Preparing transfer", "inventory": "Checking AtoMx files",
            "preview": "Awaiting transfer approval", "submitting": "Transfer approved · queued",
            "running": "Transferring files", "cancelling": "Stopping transfer",
            "done": "Transfer complete", "failed": "Transfer stopped",
            "cancelled": "Transfer cancelled", "expired": "Transfer approval expired",
        }.get(state, "Preparing transfer")
        result = activity_blocks(
            [TaskProgress("transfer", target, title,
                          "error" if state == "failed" else
                          "complete" if state in TERMINAL else "in_progress")],
            headline=title, check_in=unescape(text),
        )
        # Retain the full last status in the disclosure, including a terminal explanation.
        result[0]["title"] = title
        result[0]["tasks"][-1]["details"] = {
            "type": "rich_text", "elements": [{
                "type": "rich_text_section", "elements": [{"type": "text", "text": unescape(text)}],
            }],
        }
    if actions:
        result.append({"type": "actions", "elements": actions})
    return result
