"""Requester-facing transfer approvals and compact activity, with literal source paths."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from html import unescape
from typing import Any

from .progress import TaskProgress
from .slack_format import activity_blocks, status_blocks

PREFIX = "mica_transfer_"
TERMINAL = {"done", "failed", "cancelled", "expired"}
PHASES = {
    "listing": "Checking AtoMx source files",
    "downloading": "Downloading to worker VM",
    "checking_downloads": "Checking downloaded files",
    "uploading": "Uploading to GCS",
    "verifying": "Verifying GCS transfer",
    "complete": "Transfer complete",
}


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


def progress_text(progress: dict[str, Any]) -> str:
    """Show each leg separately; consumed upload bytes are not verified completion."""
    def count(name: str) -> int:
        value = progress.get(name)
        return value if type(value) is int and value >= 0 else 0

    total, files = count("total_bytes"), count("n_files")

    def amount(done: int) -> str:
        if not total:
            return size(done)
        return f"{size(done)} of {size(total)} ({min(100, done * 100 // total)}%)"

    phase = progress.get("phase")
    if not isinstance(phase, str):
        phase = ""
    if phase == "listing":
        return "Checking the AtoMx source-file list before downloading."
    if phase in {"downloading", "checking_downloads"}:
        label = PHASES[phase]
        return (
            f"{label}: {amount(count('download_bytes'))}. "
            f"{count('download_files'):,} of {files:,} files downloaded. "
            "GCS uploads start after the download checks finish."
        )
    if phase == "uploading":
        return (
            f"Uploading to GCS: {amount(count('upload_bytes'))} streamed or checked. "
            f"Verified in GCS: {count('files_done'):,} of {files:,} files "
            f"({size(count('bytes_done'))})."
        )
    if phase in {"verifying", "complete"}:
        return (
            f"{PHASES[phase]}. Verified in GCS: {count('files_done'):,} of {files:,} files "
            f"({amount(count('bytes_done'))})."
        )
    # Old workers report only verified GCS objects, never bytes downloaded onto the VM.
    return (
        f"Verified in GCS: {count('files_done'):,} files ({size(count('bytes_done'))}). "
        "This worker does not report download progress."
    )


def button(p: dict[str, Any], action: str, label: str) -> dict[str, Any]:
    return {
        "type": "button", "text": {"type": "plain_text", "text": label},
        "action_id": PREFIX + action, "value": p["id"] + ":" + token(p),
        **({"style": "primary"} if action == "confirm" else {}),
    }


def sections(label: str, value: str) -> list[dict[str, Any]]:
    # Full paths must remain reviewable; never silently truncate the approved destination.
    text = (label + "\n" if label else "") + value
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
        if state == "running":
            title = PHASES.get(p.get("transfer_phase", ""), title)
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
        if state in {"cancelled", "expired", "preview"}:
            result = status_blocks("", title, detail=unescape(text), state="waiting")
        elif state in TERMINAL:
            # Results and failures must be readable without opening the activity disclosure.
            result.extend(sections("", unescape(text)))
    if actions:
        result.append({"type": "actions", "elements": actions})
    return result
