"""Public activity labels derived from tool lifecycle events."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TaskProgress:
    task_id: str
    title: str
    phase: str
    status: Literal["in_progress", "complete", "error"] = "in_progress"


@dataclass(frozen=True)
class ActivityState:
    state: Literal["working", "thinking"]

    @property
    def label(self) -> str:
        return f"Mica is {self.state}"


ProgressUpdate = str | TaskProgress | ActivityState


def check_in_due(
    started: float, last_check_in: float | None, now: float, threshold: int, interval: int
) -> bool:
    """First check-in after the threshold, then one interval after successful delivery."""
    return now - started >= threshold and (last_check_in is None or now - last_check_in >= interval)


def elapsed_label(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = ([f"{days}d"] if days else []) + ([f"{hours}h"] if hours else [])
    return " ".join([*parts, f"{minutes}m"])


def run_check_in(elapsed: float, activity_age: float, completed: int, active: bool) -> str:
    waiting = (
        "Waiting for the current step to report completion."
        if active
        else "Waiting for the next analysis result."
    )
    return (
        f"Run elapsed: {elapsed_label(elapsed)}. {completed} tool steps completed. "
        f"Worker check-in; last tool activity {elapsed_label(activity_age)} ago. {waiting}"
    )
