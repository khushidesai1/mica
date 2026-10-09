# ruff: noqa: F811
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_analysis_batch import bridge  # noqa: F401

from pistachio.agents.analysis import knowledge_mirror, memory_tasks
from pistachio.api import slack_analysis


def request(bridge):
    return Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    services=SimpleNamespace(settings=bridge.app, repository=bridge.repository)
                )
            ),
        }
    )


async def test_background_endpoints_require_task_dispatcher(bridge, monkeypatch):
    monkeypatch.setattr(
        slack_analysis,
        "get_principal",
        AsyncMock(return_value=SimpleNamespace(id="stranger@example.com")),
    )
    with pytest.raises(HTTPException) as error:
        await slack_analysis.process_memory("a" * 32, request(bridge))
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await slack_analysis.mirror_knowledge("liver", "invalid", request(bridge))
    assert error.value.status_code == 403


async def test_memory_route_dispatches_and_propagates_transient_errors(bridge, monkeypatch):
    monkeypatch.setattr(
        slack_analysis,
        "get_principal",
        AsyncMock(return_value=SimpleNamespace(id="control@example.com")),
    )
    process = AsyncMock()
    monkeypatch.setattr(memory_tasks, "process", process)
    assert await slack_analysis.process_memory("a" * 32, request(bridge)) == {"status": "complete"}
    process.side_effect = TimeoutError()
    with pytest.raises(HTTPException) as error:
        await slack_analysis.process_memory("a" * 32, request(bridge))
    assert error.value.status_code == 503
    assert error.value.detail == "Memory editor did not complete; retry"


async def test_mirror_route_validates_and_dispatches(bridge, monkeypatch):
    monkeypatch.setattr(
        slack_analysis,
        "get_principal",
        AsyncMock(return_value=SimpleNamespace(id="control@example.com")),
    )
    process = AsyncMock(return_value=1)
    monkeypatch.setattr(knowledge_mirror, "process", process)
    stamp = "20261009T123456123456-" + "a" * 16
    assert await slack_analysis.mirror_knowledge("liver", stamp, request(bridge)) == {"mirrored": 1}
    with pytest.raises(HTTPException) as error:
        await slack_analysis.mirror_knowledge("liver", "../../other", request(bridge))
    assert error.value.status_code == 400
