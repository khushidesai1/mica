"""Agents SDK Session implementation backed by the repository contract."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, cast

from agents.items import TResponseInputItem

from pistachio.models import TaskEnvelope
from pistachio.storage.base import Repository


def session_key(envelope: TaskEnvelope) -> str:
    scope = "\0".join((envelope.project_id, envelope.principal_id, envelope.conversation_id))
    return "conv_" + hashlib.sha256(scope.encode()).hexdigest()[:32]


class RepositorySession:
    """Four-method Agents SDK session protocol using application persistence."""

    session_settings: Any = None

    def __init__(self, session_id: str, repository: Repository) -> None:
        self.session_id = session_id
        self.repository = repository
        self._lock = asyncio.Lock()

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        items = cast(
            list[TResponseInputItem],
            await self.repository.get_session_items(self.session_id),
        )
        if limit is None:
            return items
        if limit <= 0:
            return []
        return items[-limit:]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        async with self._lock:
            existing = await self.repository.get_session_items(self.session_id)
            serialized = cast(list[dict[str, Any]], items)
            await self.repository.replace_session_items(self.session_id, [*existing, *serialized])

    async def pop_item(self) -> TResponseInputItem | None:
        async with self._lock:
            existing = await self.repository.get_session_items(self.session_id)
            if not existing:
                return None
            item = existing.pop()
            await self.repository.replace_session_items(self.session_id, existing)
            return cast(TResponseInputItem, item)

    async def clear_session(self) -> None:
        async with self._lock:
            await self.repository.replace_session_items(self.session_id, [])
