"""Public activity labels derived from tool lifecycle events."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TaskProgress:
    task_id: str
    title: str
    phase: str
    status: Literal["in_progress", "complete", "error"] = "in_progress"


ProgressUpdate = str | TaskProgress
