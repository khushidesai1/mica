"""Concurrency-safe in-memory repository for tests and local development."""

from __future__ import annotations

import asyncio
import secrets
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from mica.models import MemoryItem, Project, ProjectSummary, TaskRecord, ToolRecord
from mica.storage import failures
from mica.storage.base import EventClaim, FailureProjection, preserve_conversation
from mica.storage.failures import FailureInvestigation, Incident, Observation


class InMemoryRepository:
    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.tasks: dict[str, TaskRecord] = {}
        self.memories: dict[str, MemoryItem] = {}
        self.summaries: dict[str, ProjectSummary] = {}
        self.tools: dict[str, ToolRecord] = {}
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.events: set[str] = set()
        self.event_claims: dict[str, tuple[str, datetime]] = {}
        self.channel_projects: dict[str, tuple[str, datetime | None]] = {}
        self.connector_state: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, Incident] = {}
        self.failure_groups: dict[str, tuple[str, float]] = {}
        self.failure_receipts: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def create_project(self, project: Project) -> Project:
        async with self._lock:
            if project.id in self.projects or any(
                existing.slug == project.slug for existing in self.projects.values()
            ):
                raise ValueError("project id or slug already exists")
            self.projects[project.id] = deepcopy(project)
        return deepcopy(project)

    async def get_project(self, project_id: str) -> Project | None:
        value = self.projects.get(project_id)
        return deepcopy(value) if value else None

    async def get_project_by_slug(self, slug: str) -> Project | None:
        for project in self.projects.values():
            if project.slug == slug:
                return deepcopy(project)
        return None

    async def list_projects(self, principal: str) -> list[Project]:
        return sorted(
            [deepcopy(p) for p in self.projects.values() if p.can_access(principal)],
            key=lambda p: p.updated_at,
            reverse=True,
        )

    async def list_all_projects(self) -> list[Project]:
        return sorted(
            [deepcopy(project) for project in self.projects.values()],
            key=lambda project: project.updated_at,
            reverse=True,
        )

    async def update_project(self, project: Project) -> Project:
        async with self._lock:
            if project.id not in self.projects:
                raise KeyError(project.id)
            self.projects[project.id] = deepcopy(project)
        return deepcopy(project)

    async def create_task(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            if task.id in self.tasks:
                raise ValueError("task already exists")
            self.tasks[task.id] = deepcopy(task)
        return deepcopy(task)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        value = self.tasks.get(task_id)
        return deepcopy(value) if value else None

    async def update_task(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            if task.id not in self.tasks:
                raise KeyError(task.id)
            current = self.tasks[task.id]
            # User interactions can arrive while a planner or executor still holds an older
            # snapshot. Never let that stale worker erase a follow-up or cancellation.
            if (current.interaction_revision > task.interaction_revision
                    or current.development_version > task.development_version):
                return deepcopy(current)
            preserve_conversation(current, task)
            self.tasks[task.id] = deepcopy(task)
        return deepcopy(task)

    async def compare_and_swap_task(
        self, task: TaskRecord, expected_version: int
    ) -> TaskRecord | None:
        async with self._lock:
            current = self.tasks[task.id]
            if (current.development_version != expected_version
                    or current.interaction_revision != task.interaction_revision):
                return None
            preserve_conversation(current, task)
            task.development_version = expected_version + 1
            self.tasks[task.id] = deepcopy(task)
            return deepcopy(task)

    async def list_tasks(self, project_id: str, limit: int = 50) -> list[TaskRecord]:
        values = [
            deepcopy(task) for task in self.tasks.values() if task.envelope.project_id == project_id
        ]
        return sorted(values, key=lambda task: task.created_at, reverse=True)[:limit]

    async def put_memory(self, item: MemoryItem) -> MemoryItem:
        async with self._lock:
            self.memories[item.id] = deepcopy(item)
        return deepcopy(item)

    async def get_memory(self, memory_id: str) -> MemoryItem | None:
        value = self.memories.get(memory_id)
        return deepcopy(value) if value else None

    async def list_memories(self, project_id: str) -> list[MemoryItem]:
        values = [deepcopy(m) for m in self.memories.values() if m.project_id == project_id]
        return sorted(values, key=lambda memory: memory.updated_at, reverse=True)

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._lock:
            return self.memories.pop(memory_id, None) is not None

    async def put_summary(self, summary: ProjectSummary) -> ProjectSummary:
        async with self._lock:
            self.summaries[summary.project_id] = deepcopy(summary)
        return deepcopy(summary)

    async def get_summary(self, project_id: str) -> ProjectSummary | None:
        value = self.summaries.get(project_id)
        return deepcopy(value) if value else None

    async def put_tool(self, tool: ToolRecord) -> ToolRecord:
        async with self._lock:
            self.tools[tool.id] = deepcopy(tool)
        return deepcopy(tool)

    async def get_tool(self, tool_id: str) -> ToolRecord | None:
        value = self.tools.get(tool_id)
        return deepcopy(value) if value else None

    async def list_tools(self) -> list[ToolRecord]:
        return sorted(
            [deepcopy(tool) for tool in self.tools.values()], key=lambda tool: tool.name.lower()
        )

    async def get_session_items(self, session_id: str) -> list[dict[str, Any]]:
        return deepcopy(self.sessions.get(session_id, []))

    async def replace_session_items(self, session_id: str, items: list[dict[str, Any]]) -> None:
        async with self._lock:
            self.sessions[session_id] = deepcopy(items)

    async def mark_event(self, external_event_id: str) -> bool:
        async with self._lock:
            if external_event_id in self.events:
                return False
            self.events.add(external_event_id)
            return True

    async def begin_event(
        self, external_event_id: str, lease_seconds: int = 1200
    ) -> EventClaim:
        now = datetime.now(UTC)
        async with self._lock:
            if external_event_id in self.events:
                return EventClaim("completed")
            current = self.event_claims.get(external_event_id)
            if current is not None and current[1] > now:
                return EventClaim("processing")
            token = secrets.token_urlsafe(24)
            self.event_claims[external_event_id] = (
                token,
                now + timedelta(seconds=lease_seconds),
            )
            return EventClaim("claimed", token)

    async def complete_event(self, external_event_id: str, token: str) -> None:
        async with self._lock:
            current = self.event_claims.get(external_event_id)
            if current is None or current[0] != token:
                return
            self.event_claims.pop(external_event_id, None)
            self.events.add(external_event_id)

    async def release_event(self, external_event_id: str, token: str) -> None:
        async with self._lock:
            current = self.event_claims.get(external_event_id)
            if current is not None and current[0] == token:
                self.event_claims.pop(external_event_id, None)

    async def set_channel_project(
        self, key: str, project_id: str, expires_at: datetime | None = None
    ) -> None:
        async with self._lock:
            self.channel_projects[key] = (project_id, expires_at)

    async def get_channel_project(self, key: str) -> str | None:
        binding = self.channel_projects.get(key)
        if binding is None:
            return None
        project_id, expires_at = binding
        if expires_at and expires_at < datetime.now(UTC):
            async with self._lock:
                self.channel_projects.pop(key, None)
            return None
        return project_id

    async def set_connector_state(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self.connector_state[key] = deepcopy(value)

    async def get_connector_state(self, key: str) -> dict[str, Any] | None:
        value = self.connector_state.get(key)
        return deepcopy(value) if value else None

    async def patch_connector_state(
        self, key: str, updates: dict[str, Any], remove: tuple[str, ...] = ()
    ) -> None:
        async with self._lock:
            value = self.connector_state[key]
            value.update(deepcopy(updates))
            for field in remove:
                value.pop(field, None)

    async def due_connector_states(
        self, field: FailureProjection, now: float, limit: int = 10
    ) -> list[tuple[str, dict[str, Any]]]:
        async with self._lock:
            rows = [(key, value) for key, value in self.connector_state.items()
                    if isinstance(value.get(field), (int, float)) and value[field] <= now]
            rows.sort(key=lambda item: (item[1][field], item[0]))
            return deepcopy(rows[:limit])

    async def request_failure(self, scope: str, event_id: str) -> Incident | None:
        async with self._lock:
            group = self.failure_groups.get(failures.digest(scope, "request", event_id))
            row = self.failures.get(group[0]) if group else None
            return row.model_copy(deep=True) if row else None

    async def recent_failures(self, since: float, limit: int = 200) -> list[Incident]:
        async with self._lock:
            rows = sorted((r for r in self.failures.values() if r.last_seen >= since),
                          key=lambda r: (r.last_seen, r.id), reverse=True)
            return [r.model_copy(deep=True) for r in rows[:limit]]

    async def record_failure(self, observation: Observation, now: float) -> Incident:
        async with self._lock:
            receipt = self.failure_receipts.get(observation.receipt)
            if receipt and receipt[1] > now and receipt[0] in self.failures:
                return self.failures[receipt[0]].model_copy(deep=True)
            group = self.failure_groups.get(observation.fingerprint)
            old = self.failures.get(group[0]) if group else None
            row = failures.observed(old, observation, now, secrets.token_hex(16))
            self.failures[row.id] = row
            self.failure_groups[observation.fingerprint] = (row.id, row.expires_at)
            self.failure_receipts[observation.receipt] = (row.id, now + failures.RETENTION)
            return row.model_copy(deep=True)

    async def get_failure(self, key: str) -> Incident | None:
        async with self._lock:
            row = self.failures.get(key)
            return row.model_copy(deep=True) if row else None

    async def resolve_failure(
        self, key: str, count: int, actor: str, channel: str, slack_ts: str, now: float
    ) -> Incident | None:
        async with self._lock:
            row = self.failures.get(key)
            update = failures.resolved(row, count, actor, channel, slack_ts, now) if row else None
            if update:
                self.failures[key] = update
            return update.model_copy(deep=True) if update else None

    async def due_failures(self, now: float, limit: int = 20) -> list[Incident]:
        async with self._lock:
            return [r.model_copy(deep=True) for r in failures.prioritized(
                list(self.failures.values()), now, limit
            )]

    async def claim_failure(self, key: str, channel: str, now: float) -> Incident | None:
        async with self._lock:
            row = self.failures.get(key)
            update = failures.claimed(row, channel, secrets.token_hex(16), now) if row else None
            if update:
                self.failures[key] = update
            return update.model_copy(deep=True) if update else None

    async def finish_failure(
        self,
        key: str,
        token: str,
        now: float,
        *,
        slack_ts: str = "",
        sent_count: int = 0,
        retry_after: float = 0,
        uncertain: bool = False,
        request_status: str = "",
        sent_resolved_count: int = 0,
        card_version: int = 0,
        investigation: FailureInvestigation | None = None,
        sent_investigation: str = "",
    ) -> None:
        async with self._lock:
            row = self.failures.get(key)
            update = (
                failures.finished(
                    row,
                    token,
                    now,
                    slack_ts=slack_ts,
                    sent_count=sent_count,
                    retry_after=retry_after,
                    uncertain=uncertain,
                    request_status=request_status,
                    sent_resolved_count=sent_resolved_count,
                    card_version=card_version,
                    investigation=investigation,
                    sent_investigation=sent_investigation,
                )
                if row
                else None
            )
            if update:
                self.failures[key] = update

    async def purge_failures(self, now: float, limit: int = 200) -> int:
        async with self._lock:
            count = 0
            for key, row in list(self.failures.items()):
                if row.expires_at < now and row.lease_until < now and count < limit:
                    del self.failures[key]
                    count += 1
            for store in (self.failure_groups, self.failure_receipts):
                for key, value in list(store.items()):
                    if value[1] < now and count < limit:
                        del store[key]
                        count += 1
            return count
