"""Conservative project-memory promotion, retrieval, and compaction."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from openai import AsyncOpenAI

from mica.config import Settings
from mica.models import (
    MemoryItem,
    MemoryKind,
    MemoryStatus,
    ProjectSummary,
    TaskRecord,
    TaskStatus,
    utcnow,
)
from mica.storage.base import Repository

TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}")
SECRET_PATTERN = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}|\bghp_[A-Za-z0-9]{16,}|"
    r"\bxox[baprs]-|\b(?:password|api[_ -]?key)\s*[:=]|\bbearer\s+[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


class MemoryPolicyViolation(ValueError):
    pass


def keywords(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(text)}


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


class MemoryManager:
    """Memory is advisory context, never authorization for scientific work."""

    AUTO_PROMOTED_KINDS = {
        MemoryKind.USER_PREFERENCE,
        MemoryKind.USER_CORRECTION,
        MemoryKind.DATASET_FACT,
        MemoryKind.TOOL_FACT,
        MemoryKind.PROJECT_CONVENTION,
        MemoryKind.RESULT_POINTER,
        MemoryKind.PENDING_ITEM,
    }

    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings
        self._openai: AsyncOpenAI | None = None

    @property
    def openai(self) -> AsyncOpenAI:
        if self._openai is None:
            self._openai = AsyncOpenAI()
        return self._openai

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not os.getenv("OPENAI_API_KEY"):
            return [[] for _ in texts]
        try:
            response = await self.openai.embeddings.create(
                model=self.settings.embedding_model,
                input=texts,
            )
        except Exception:
            return [[] for _ in texts]
        return [list(item.embedding) for item in response.data]

    async def promote(self, item: MemoryItem) -> MemoryItem:
        """Store safe explicit memories; inferred memories remain candidates."""
        if len(item.content) > 8_000:
            raise MemoryPolicyViolation("memory item is too large; store an artifact pointer")
        if SECRET_PATTERN.search(item.content):
            raise MemoryPolicyViolation("memory item appears to contain a credential or secret")
        if item.kind not in self.AUTO_PROMOTED_KINDS:
            item.status = MemoryStatus.CANDIDATE
        if not item.embedding:
            item.embedding = (await self._embed([item.content]))[0]
        item.updated_at = utcnow()
        return await self.repository.put_memory(item)

    async def promote_task_result(self, task: TaskRecord) -> MemoryItem | None:
        if task.status != TaskStatus.SUCCEEDED or task.result is None:
            return None
        artifact_names = ", ".join(artifact.name for artifact in task.result.artifacts) or "none"
        content = (
            f"Task {task.id} completed: {task.result.summary}. "
            f"Artifacts: {artifact_names}. This is a prior result pointer, not authorization "
            "for a new scientific action."
        )
        return await self.promote(
            MemoryItem(
                project_id=task.envelope.project_id,
                kind=MemoryKind.RESULT_POINTER,
                content=content,
                tags=keywords(task.envelope.prompt),
                entities={task.id, *(artifact.name for artifact in task.result.artifacts)},
                source_task_id=task.id,
                provenance=f"completed task {task.id}",
            )
        )

    async def supersede(self, old: MemoryItem, replacement: MemoryItem) -> MemoryItem:
        old.status = MemoryStatus.SUPERSEDED
        old.updated_at = utcnow()
        await self.repository.put_memory(old)
        replacement.supersedes_id = old.id
        replacement.version = old.version + 1
        return await self.promote(replacement)

    async def retrieve(
        self,
        project_id: str,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        limit = limit or self.settings.memory_result_limit
        candidates = [
            item
            for item in await self.repository.list_memories(project_id)
            if item.status in {MemoryStatus.ACTIVE, MemoryStatus.PINNED}
        ]
        if not candidates:
            return []
        query_embedding = (await self._embed([query]))[0]
        query_terms = keywords(query)
        now = datetime.now(UTC)

        def score(item: MemoryItem) -> float:
            item_tags = set(item.tags)
            item_entities = set(item.entities)
            item_terms = item_tags | item_entities | keywords(item.content)
            overlap = len(query_terms & item_terms) / max(1, len(query_terms | item_terms))
            semantic = cosine_similarity(query_embedding, item.embedding)
            age_days = max(0.0, (now - item.updated_at).total_seconds() / 86_400)
            recency = math.exp(-age_days / 30.0)
            exact = 1.0 if query_terms & item_entities else 0.0
            return (0.45 * semantic) + (0.25 * overlap) + (0.2 * recency) + (0.1 * exact)

        selected = sorted(candidates, key=score, reverse=True)[:limit]
        for item in selected:
            item.last_used_at = utcnow()
            await self.repository.put_memory(item)
        return selected

    async def inactivate_stale(self, project_id: str) -> int:
        cutoff = utcnow() - timedelta(days=self.settings.memory_inactive_days)
        count = 0
        for item in await self.repository.list_memories(project_id):
            if item.status == MemoryStatus.ACTIVE and item.updated_at < cutoff:
                item.status = MemoryStatus.INACTIVE
                item.updated_at = utcnow()
                await self.repository.put_memory(item)
                count += 1
        return count


class ProjectCompactor:
    """Creates deterministic project summaries and clears noisy active history."""

    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def should_compact(self, project_id: str) -> bool:
        summary = await self.repository.get_summary(project_id)
        tasks = await self.repository.list_tasks(project_id, limit=100)
        completed = [task for task in tasks if task.status == TaskStatus.SUCCEEDED]
        source_ids = set(summary.source_task_ids if summary else [])
        uncompacted = [task for task in completed if task.id not in source_ids]
        token_estimate = sum(len(task.envelope.prompt) // 4 for task in uncompacted)
        return (
            len(uncompacted) >= self.settings.compaction_task_threshold
            or token_estimate >= self.settings.compaction_token_threshold
        )

    async def compact(self, project_id: str, *, force: bool = False) -> ProjectSummary | None:
        if not force and not await self.should_compact(project_id):
            return await self.repository.get_summary(project_id)
        previous = await self.repository.get_summary(project_id)
        tasks = await self.repository.list_tasks(project_id, limit=100)
        completed = sorted(
            [task for task in tasks if task.status == TaskStatus.SUCCEEDED],
            key=lambda task: task.created_at,
        )
        if not completed:
            return previous
        lines: list[str] = []
        if previous:
            lines.append(previous.summary)
        for task in completed:
            if previous and task.id in previous.source_task_ids:
                continue
            result = task.result.summary if task.result else "Completed without a result summary."
            lines.append(f"- {task.id}: {result}")
        summary_text = "\n".join(lines)[-16_000:]
        summary = ProjectSummary(
            project_id=project_id,
            version=(previous.version + 1 if previous else 1),
            summary=summary_text,
            source_task_ids=[task.id for task in completed],
            tasks_since_compaction=0,
            estimated_source_tokens=sum(len(task.envelope.prompt) // 4 for task in completed),
        )
        await self.repository.put_summary(summary)
        return summary


def combine_memory_text(items: Iterable[MemoryItem]) -> str:
    return "\n".join(
        f"- [{item.kind.value}; {item.status.value}; {item.provenance}; "
        f"confidence={item.confidence:.2f}] {item.content}"
        for item in items
    )
