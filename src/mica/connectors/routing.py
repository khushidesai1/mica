"""Durable delivery routes that keep task execution independent from its ingress channel."""

from __future__ import annotations

import hashlib
from datetime import datetime

from mica.models import DeliveryRoute, TaskRecord, TaskSource
from mica.storage.base import Repository


def _route_key(scope: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return f"delivery-{scope}:{digest}"


async def bind_delivery_route(repository: Repository, route: DeliveryRoute) -> None:
    """Set the preferred route for one user in one project and channel."""
    await repository.set_connector_state(
        _route_key(
            "preferred",
            route.project_id,
            route.principal_id.lower(),
            route.source.value,
        ),
        route.model_dump(mode="json"),
    )


async def preferred_delivery_route(
    repository: Repository,
    project_id: str,
    principal_id: str,
    source: TaskSource,
    *,
    now: datetime | None = None,
) -> DeliveryRoute | None:
    data = await repository.get_connector_state(
        _route_key("preferred", project_id, principal_id.lower(), source.value)
    )
    if not data:
        return None
    route = DeliveryRoute.model_validate(data)
    if _expired(route, now):
        return None
    return route


async def bind_task_delivery_route(
    repository: Repository, task_id: str, route: DeliveryRoute
) -> None:
    """Remember the exact conversation/thread used to deliver updates for a task."""
    await repository.set_connector_state(
        _route_key("task", task_id, route.source.value),
        route.model_dump(mode="json"),
    )


async def task_delivery_route(
    repository: Repository,
    task_id: str,
    source: TaskSource,
    *,
    now: datetime | None = None,
) -> DeliveryRoute | None:
    data = await repository.get_connector_state(_route_key("task", task_id, source.value))
    if not data:
        return None
    route = DeliveryRoute.model_validate(data)
    return None if _expired(route, now) else route


def _expired(route: DeliveryRoute, now: datetime | None = None) -> bool:
    if route.expires_at is None:
        return False
    reference_time = now or datetime.now(route.expires_at.tzinfo)
    return route.expires_at <= reference_time


def origin_delivery_route(task: TaskRecord) -> DeliveryRoute | None:
    """Translate a channel-originated task into a delivery route."""
    if task.envelope.source not in {TaskSource.SLACK, TaskSource.EMAIL}:
        return None
    return DeliveryRoute(
        project_id=task.envelope.project_id,
        principal_id=task.envelope.principal_id,
        source=task.envelope.source,
        conversation_id=task.envelope.conversation_id,
        thread_id=task.envelope.thread_id,
    )
