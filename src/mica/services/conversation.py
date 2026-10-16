"""Channel-independent selection of meaningful conversational updates."""
from datetime import datetime

from mica.models import StageKind, TaskRecord, TaskStatus


def communication_kind(task: TaskRecord, event_key: str, now: datetime) -> str | None:
    if event_key.startswith("replan:"):
        return "replan"
    if event_key.startswith("decision:"):
        return "decision"
    immediate = {
        TaskStatus.NEEDS_APPROVAL: "approval", TaskStatus.NEEDS_INPUT: "input",
        TaskStatus.SUCCEEDED: "result", TaskStatus.FAILED: "failure",
        TaskStatus.CANCELLED: "cancelled", TaskStatus.RECEIVED: "acknowledgement",
    }
    if task.status in immediate:
        return immediate[task.status]
    replies = [item for item in task.conversation if item.role == "assistant"]
    if not replies:
        return "progress"
    last = replies[-1]
    # Execution starts and automatic retries are meaningful changes, not internal stages.
    if task.status == TaskStatus.RUNNING and not any(
        item.phase == "running" and item.attempt == task.attempts for item in replies
    ):
        return "progress"
    if task.attempts > last.attempt and task.attempts > 1:
        return "progress"
    elapsed = (now - last.created_at).total_seconds()
    phase = communication_phase(task)
    # A meaningful phase change can speak after a minute. During a long phase, report
    # the latest available milestone at most every two minutes. No timer-only filler.
    if elapsed >= 120 or (elapsed >= 60 and phase != last.phase):
        return "progress"
    return None


def communication_phase(task: TaskRecord) -> str:
    if task.status in {TaskStatus.SUBMITTING, TaskStatus.QUEUED}:
        return "waiting"
    if task.status == TaskStatus.RUNNING:
        return "running"
    if task.current_stage in {StageKind.CODE_GENERATION, StageKind.VALIDATION}:
        return "preparing"
    return "planning"
