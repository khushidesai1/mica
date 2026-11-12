"""Evidence-based recovery and notification policy, separate from incident recording."""

from __future__ import annotations

import re
from typing import Any

from mica.storage.base import Repository
from mica.storage.failures import RECOVERABLE_PROCESSING, Incident, priority

RECHECK_SECONDS = 300
MEMORY_GRACE_SECONDS = 3600
QUIET_STATUSES = {"recovered", "saved_with_exclusions"}
TRANSIENT_PROCESSING = {"compute_control_failed", "infra_http_error"}
COMPACTION = {"memory_compaction_retry", "memory_compaction_exhausted"}


async def inspect(repository: Repository, row: Incident) -> tuple[str, dict[str, Any] | None]:
    record = None
    causes = set(row.causes or [row.observation.cause])
    if not row.resolved:
        if row.observation.request_scoped:
            record = await repository.get_connector_state(
                f"claude-event:{row.observation.event_id}"
            )
        elif causes <= COMPACTION and row.observation.project_id:
            record = await repository.get_connector_state(
                f"memory-maintenance:{row.observation.project_id}"
            )
    return status(row, record), record


def memory_verified(row: Incident, record: dict[str, Any]) -> bool:
    receipt = record.get("memory_receipt") or {}
    return bool(
        record.get("memory_editor_done") is True
        and not record.get("memory_pending")
        and receipt.get("event_id") == row.observation.event_id
        and receipt.get("project_id") == row.observation.project_id
        and re.fullmatch(r"[a-f0-9]{40}", str(receipt.get("head", "")))
        and re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("entry_sha256", "")))
        and isinstance(receipt.get("verified_at"), (int, float))
        and receipt["verified_at"] >= row.cause_seen.get("memory_editor_failed", row.last_seen)
    )


def status(row: Incident, record: dict[str, Any] | None) -> str:
    if row.resolved:
        return row.request_status or "unknown"
    if not record:
        return "unknown"
    causes = set(row.causes or [row.observation.cause])
    if not row.user_related:
        if causes <= COMPACTION and record.get("last_success_at", 0) >= row.last_seen:
            return "recovered"
        return "unknown"
    result = record.get("status")
    if result not in {"complete", "failed"}:
        return "active"
    if result != "complete":
        return "failed"
    if causes <= RECOVERABLE_PROCESSING:
        return "recovered"
    if causes <= RECOVERABLE_PROCESSING | {"memory_editor_failed"} and memory_verified(row, record):
        receipt = record["memory_receipt"]
        if receipt.get("rejected"):
            # Partial imports have an explicit review failure, not just ordinary
            # worker validation exclusions. Never hide those behind a saved thread.
            return "complete" if "import_review" in receipt else "saved_with_exclusions"
        return "recovered"
    return "complete"


def review_after(row: Incident, outcome: str) -> int:
    causes = set(row.causes or [row.observation.cause])
    if row.resolved or outcome in QUIET_STATUSES:
        return 0
    if row.user_related and outcome in {"unknown", "active"}:
        return RECHECK_SECONDS
    if "memory_editor_failed" in causes or causes <= COMPACTION:
        return RECHECK_SECONDS
    return 0


def should_notify(
    row: Incident, outcome: str, record: dict[str, Any] | None, now: float,
) -> bool:
    """Retain every observation; notify only when intervention may be needed."""
    if row.resolved or outcome in QUIET_STATUSES:
        return False
    causes = set(row.causes or [row.observation.cause])
    if causes == {"smoke_test"}:
        return True
    if row.user_related and causes <= TRANSIENT_PROCESSING and outcome in {"active", "unknown"}:
        # A healthy polling heartbeat is positive progress evidence. A stopped
        # chain is independently detected by the existing stalled-request monitor.
        if record and record.get("stall_check_at", 0) > now:
            return False
        return now - row.first_seen >= RECHECK_SECONDS
    if "memory_editor_failed" in causes and causes <= (
        RECOVERABLE_PROCESSING | {"memory_editor_failed"}
    ) and (outcome in {"complete", "unknown"} or causes == {"memory_editor_failed"}):
        if record and record.get("memory_editor_done"):
            return True  # Unverifiable receipt / partial import needs investigation.
        pending = (record or {}).get("memory_pending") or row.cause_seen.get(
            "memory_editor_failed", row.first_seen
        )
        return bool(now - pending >= MEMORY_GRACE_SECONDS)
    if causes <= COMPACTION:
        if "memory_compaction_exhausted" in causes:
            return True
        since = max(row.first_seen, (record or {}).get("last_success_at", 0))
        return bool(now - since >= MEMORY_GRACE_SECONDS)
    # Isolated platform errors and routine catalog refreshes remain searchable logs.
    # Sustained intake failures become high priority via the existing duration rule.
    return priority(row, outcome) != "low"
