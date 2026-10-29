"""Firestore repository used by Cloud Run deployments."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from mica.models import (
    MemoryItem,
    Project,
    ProjectSummary,
    TaskRecord,
    ToolProposal,
    ToolRecord,
)
from mica.storage import failures
from mica.storage.base import EventClaim, preserve_conversation
from mica.storage.failures import Incident, Observation

T = TypeVar("T", bound=BaseModel)


def deserialize_tool(data: dict[str, Any]) -> ToolRecord:
    """Preserve proposal-only metadata when reading the shared tool collection."""
    model_type = ToolProposal if "why_needed" in data else ToolRecord
    return model_type.model_validate(data)


class FirestoreRepository:
    """Small async Firestore adapter with explicit collection boundaries."""

    def __init__(self, project_id: str | None = None) -> None:
        try:
            from google.cloud.firestore import AsyncClient
        except ImportError as exc:  # pragma: no cover - optional cloud path
            raise RuntimeError("google-cloud-firestore is required") from exc
        self.client = AsyncClient(project=project_id or None)

    @staticmethod
    def _dump(model: BaseModel) -> dict[str, Any]:
        return model.model_dump(mode="json")

    async def _put(self, collection: str, key: str, model: T) -> T:
        await self.client.collection(collection).document(key).set(self._dump(model))
        return model

    async def _get(self, collection: str, key: str, model_type: type[T]) -> T | None:
        snapshot = await self.client.collection(collection).document(key).get()
        return model_type.model_validate(snapshot.to_dict()) if snapshot.exists else None

    async def create_project(self, project: Project) -> Project:
        if await self.get_project(project.id) or await self.get_project_by_slug(project.slug):
            raise ValueError("project id or slug already exists")
        return await self._put("projects", project.id, project)

    async def get_project(self, project_id: str) -> Project | None:
        return await self._get("projects", project_id, Project)

    async def get_project_by_slug(self, slug: str) -> Project | None:
        query = self.client.collection("projects").where("slug", "==", slug).limit(1)
        async for snapshot in query.stream():
            return Project.model_validate(snapshot.to_dict())
        return None

    async def list_projects(self, principal: str) -> list[Project]:
        projects: dict[str, Project] = {}
        collection = self.client.collection("projects")
        for field in ("owner", "members"):
            query = (
                collection.where(field, "array_contains", principal)
                if field == "members"
                else collection.where(field, "==", principal)
            )
            async for snapshot in query.stream():
                project = Project.model_validate(snapshot.to_dict())
                projects[project.id] = project
        return sorted(projects.values(), key=lambda p: p.updated_at, reverse=True)

    async def list_all_projects(self) -> list[Project]:
        return [
            Project.model_validate(document.to_dict())
            async for document in self.client.collection("projects").stream()
        ]

    async def update_project(self, project: Project) -> Project:
        return await self._put("projects", project.id, project)

    async def create_task(self, task: TaskRecord) -> TaskRecord:
        if await self.get_task(task.id):
            raise ValueError("task already exists")
        return await self._put("tasks", task.id, task)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self._get("tasks", task_id, TaskRecord)

    async def update_task(self, task: TaskRecord) -> TaskRecord:
        from google.cloud import firestore

        reference = self.client.collection("tasks").document(task.id)
        transaction = self.client.transaction()

        @firestore.async_transactional
        async def update_if_current(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(task.id)
            current = TaskRecord.model_validate(snapshot.to_dict())
            # Preserve newer user controls when an in-flight worker writes an older snapshot.
            if (current.interaction_revision > task.interaction_revision
                    or current.development_version > task.development_version):
                return current
            preserve_conversation(current, task)
            transaction.set(reference, self._dump(task))
            return task

        return cast(TaskRecord, await update_if_current(transaction))

    async def compare_and_swap_task(
        self, task: TaskRecord, expected_version: int
    ) -> TaskRecord | None:
        from google.cloud import firestore

        reference = self.client.collection("tasks").document(task.id)
        transaction = self.client.transaction()

        @firestore.async_transactional
        async def compare(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(task.id)
            current = TaskRecord.model_validate(snapshot.to_dict())
            if (current.development_version != expected_version
                    or current.interaction_revision != task.interaction_revision):
                return None
            updated = task.model_copy(update={"development_version": expected_version + 1})
            preserve_conversation(current, updated)
            transaction.set(reference, self._dump(updated))
            return updated

        return cast(TaskRecord | None, await compare(transaction))

    async def list_tasks(self, project_id: str, limit: int = 50) -> list[TaskRecord]:
        query = (
            self.client.collection("tasks")
            .where("envelope.project_id", "==", project_id)
            .order_by("created_at", direction="DESCENDING")
            .limit(limit)
        )
        return [TaskRecord.model_validate(doc.to_dict()) async for doc in query.stream()]

    async def put_memory(self, item: MemoryItem) -> MemoryItem:
        return await self._put("memories", item.id, item)

    async def get_memory(self, memory_id: str) -> MemoryItem | None:
        return await self._get("memories", memory_id, MemoryItem)

    async def list_memories(self, project_id: str) -> list[MemoryItem]:
        query = self.client.collection("memories").where("project_id", "==", project_id)
        return [MemoryItem.model_validate(doc.to_dict()) async for doc in query.stream()]

    async def delete_memory(self, memory_id: str) -> bool:
        reference = self.client.collection("memories").document(memory_id)
        snapshot = await reference.get()
        if not snapshot.exists:
            return False
        await reference.delete()
        return True

    async def put_summary(self, summary: ProjectSummary) -> ProjectSummary:
        return await self._put("project_summaries", summary.project_id, summary)

    async def get_summary(self, project_id: str) -> ProjectSummary | None:
        return await self._get("project_summaries", project_id, ProjectSummary)

    async def put_tool(self, tool: ToolRecord) -> ToolRecord:
        return await self._put("tools", tool.id, tool)

    async def get_tool(self, tool_id: str) -> ToolRecord | None:
        snapshot = await self.client.collection("tools").document(tool_id).get()
        return deserialize_tool(snapshot.to_dict()) if snapshot.exists else None

    async def list_tools(self) -> list[ToolRecord]:
        return [
            deserialize_tool(doc.to_dict())
            async for doc in self.client.collection("tools").stream()
        ]

    async def get_session_items(self, session_id: str) -> list[dict[str, Any]]:
        snapshot = await self.client.collection("sessions").document(session_id).get()
        if not snapshot.exists:
            return []
        return list((snapshot.to_dict() or {}).get("items", []))

    async def replace_session_items(self, session_id: str, items: list[dict[str, Any]]) -> None:
        await self.client.collection("sessions").document(session_id).set({"items": items})

    async def mark_event(self, external_event_id: str) -> bool:
        from google.api_core.exceptions import AlreadyExists

        reference = self.client.collection("events").document(external_event_id)
        try:
            await reference.create({"seen": True})
        except AlreadyExists:
            return False
        return True

    async def begin_event(
        self, external_event_id: str, lease_seconds: int = 1200
    ) -> EventClaim:
        from google.cloud import firestore

        reference = self.client.collection("events").document(external_event_id)
        transaction = self.client.transaction()
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(24)

        @firestore.async_transactional
        async def claim(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                status = data.get("status")
                if status == "completed" or (status is None and data.get("seen")):
                    return EventClaim("completed")
                lease_expires_at = data.get("lease_expires_at")
                if status == "processing" and lease_expires_at and lease_expires_at > now:
                    return EventClaim("processing")
            transaction.set(
                reference,
                {
                    "status": "processing",
                    "claim_token": token,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
            )
            return EventClaim("claimed", token)

        return cast(EventClaim, await claim(transaction))

    async def complete_event(self, external_event_id: str, token: str) -> None:
        from google.cloud import firestore

        reference = self.client.collection("events").document(external_event_id)
        transaction = self.client.transaction()

        @firestore.async_transactional
        async def complete(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else {}
            if not data or data.get("claim_token") != token:
                return
            transaction.set(
                reference,
                {
                    "status": "completed",
                    "seen": True,
                    "completed_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                },
            )

        await complete(transaction)

    async def release_event(self, external_event_id: str, token: str) -> None:
        from google.cloud import firestore

        reference = self.client.collection("events").document(external_event_id)
        transaction = self.client.transaction()

        @firestore.async_transactional
        async def release(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else {}
            if data and data.get("status") == "processing" and data.get("claim_token") == token:
                transaction.delete(reference)

        await release(transaction)

    async def set_channel_project(
        self, key: str, project_id: str, expires_at: datetime | None = None
    ) -> None:
        await (
            self.client.collection("channel_projects")
            .document(key)
            .set({"project_id": project_id, "expires_at": expires_at})
        )

    async def get_channel_project(self, key: str) -> str | None:
        snapshot = await self.client.collection("channel_projects").document(key).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        expires_at = data.get("expires_at")
        if expires_at and expires_at < datetime.now(UTC):
            await snapshot.reference.delete()
            return None
        return data.get("project_id")

    async def set_connector_state(self, key: str, value: dict[str, Any]) -> None:
        await self.client.collection("connector_state").document(key).set(value)

    async def get_connector_state(self, key: str) -> dict[str, Any] | None:
        snapshot = await self.client.collection("connector_state").document(key).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def record_failure(self, observation: Observation, now: float) -> Incident:
        from google.cloud import firestore

        collection = self.client.collection("failures")
        receipt = self.client.collection("failure_occurrences").document(observation.receipt)
        group = self.client.collection("failure_groups").document(observation.fingerprint)
        episode = secrets.token_hex(16)

        @firestore.async_transactional
        async def record(transaction):  # type: ignore[no-untyped-def]
            seen = await receipt.get(transaction=transaction)
            pointer = await group.get(transaction=transaction)
            prior = seen.to_dict() or {}
            if prior.get("expires_at", 0) > now:
                saved = await collection.document(prior["incident"]).get(transaction=transaction)
                if saved.exists:
                    return Incident.model_validate(saved.to_dict())
            data = pointer.to_dict() or {}
            old = None
            if data.get("incident"):
                snapshot = await collection.document(data["incident"]).get(transaction=transaction)
                if snapshot.exists:
                    old = Incident.model_validate(snapshot.to_dict())
            row = failures.observed(old, observation, now, episode)
            transaction.set(collection.document(row.id), failures.document(row))
            transaction.set(group, {"incident": row.id, "expires_at": row.expires_at})
            transaction.set(receipt, {"incident": row.id, "expires_at": now + failures.RETENTION})
            return row

        return cast(Incident, await record(self.client.transaction()))

    async def get_failure(self, key: str) -> Incident | None:
        snapshot = await self.client.collection("failures").document(key).get()
        return Incident.model_validate(snapshot.to_dict()) if snapshot.exists else None

    async def resolve_failure(
        self, key: str, count: int, actor: str, channel: str, slack_ts: str, now: float
    ) -> Incident | None:
        from google.cloud import firestore

        reference = self.client.collection("failures").document(key)

        @firestore.async_transactional
        async def resolve(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            row = failures.resolved(
                Incident.model_validate(snapshot.to_dict()), count, actor, channel, slack_ts, now
            )
            if row:
                transaction.set(reference, failures.document(row))
            return row

        return cast(Incident | None, await resolve(self.client.transaction()))

    async def due_failures(self, now: float, limit: int = 20) -> list[Incident]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        rows = []
        # The third bounded query includes older documents without queue projections.
        for field in ("request_due", "background_due", "next_attempt"):
            query = (
                self.client.collection("failures")
                .where(filter=FieldFilter(field, "<=", now))  # type: ignore[no-untyped-call]
                .order_by(field)
                .limit(limit)
            )
            rows.extend([Incident.model_validate(doc.to_dict()) async for doc in query.stream()])
        return failures.prioritized(rows, now, limit)

    async def claim_failure(self, key: str, channel: str, now: float) -> Incident | None:
        from google.cloud import firestore

        reference = self.client.collection("failures").document(key)
        token = secrets.token_hex(16)

        @firestore.async_transactional
        async def claim(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            row = failures.claimed(Incident.model_validate(snapshot.to_dict()), channel, token, now)
            if row:
                transaction.set(reference, failures.document(row))
            return row

        return cast(Incident | None, await claim(self.client.transaction()))

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
    ) -> None:
        from google.cloud import firestore

        reference = self.client.collection("failures").document(key)

        @firestore.async_transactional
        async def finish(transaction):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return
            row = failures.finished(
                Incident.model_validate(snapshot.to_dict()),
                token,
                now,
                slack_ts=slack_ts,
                sent_count=sent_count,
                retry_after=retry_after,
                uncertain=uncertain,
                request_status=request_status,
                sent_resolved_count=sent_resolved_count,
                card_version=card_version,
            )
            if row:
                transaction.set(reference, failures.document(row))

        await finish(self.client.transaction())

    async def purge_failures(self, now: float, limit: int = 200) -> int:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter

        @firestore.async_transactional
        async def remove(transaction, reference):  # type: ignore[no-untyped-def]
            snapshot = await reference.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            if data and data["expires_at"] < now and data.get("lease_until", 0) < now:
                transaction.delete(reference)
                return 1
            return 0

        count = 0
        for name in ("failures", "failure_groups", "failure_occurrences"):
            if count >= limit:
                break
            query = (
                self.client.collection(name)
                .where(filter=FieldFilter("expires_at", "<", now))  # type: ignore[no-untyped-call]
                .order_by("expires_at")
                .limit(limit - count)
            )
            async for doc in query.stream():
                count += await remove(self.client.transaction(), doc.reference)
        return count
