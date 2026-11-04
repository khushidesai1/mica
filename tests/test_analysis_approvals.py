"""Registered approvals keep existing actions and their durable decision boundaries."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from test_analysis_batch import bridge  # noqa: F401

from mica.agents.analysis import approvals
from mica.agents.analysis.storage import Conflict, LocalStorage
from mica.api import slack_analysis


@pytest.mark.parametrize("kind", approvals.kinds(), ids=lambda k: k.name)
async def test_dispatch_preserves_all_prefixes_and_modal_callback(monkeypatch, kind):
    decision = AsyncMock(return_value={"kind": kind.name})
    registered = replace(kind, decide=decision)
    monkeypatch.setattr(approvals, "kinds", lambda: (registered,))
    payloads = [
        {"type": "block_actions", "actions": [{"action_id": prefix + "approve"}]}
        for prefix in kind.prefixes
    ]
    if kind.callback:
        payloads.append({"type": "view_submission", "view": {"callback_id": kind.callback}})
    for payload in payloads:
        assert approvals.is_action(payload)
        assert await approvals.handle(None, None, payload, BackgroundTasks()) == {"kind": kind.name}
    assert decision.await_count == len(payloads)


async def test_mixed_actions_are_rejected_before_any_decision(monkeypatch):
    calls = [AsyncMock(), AsyncMock()]
    kinds = [replace(kind, decide=callback)
             for kind, callback in zip(approvals.kinds(), calls, strict=False)]
    monkeypatch.setattr(approvals, "kinds", lambda: tuple(kinds))
    payload = {"type": "block_actions", "actions": [
        {"action_id": kind.prefixes[0] + "approve"} for kind in kinds
    ]}
    with pytest.raises(ValueError, match="one approval action"):
        await approvals.handle(None, None, payload, BackgroundTasks())
    assert all(not callback.called for callback in calls)


def test_shared_documents_never_overwrite_a_newer_generation(tmp_path):
    storage = LocalStorage(tmp_path)
    path = approvals.resolve("command").key("a" * 32)
    initial = approvals.save(storage, path, {"state": "pending"}, None)
    assert approvals.load_json(storage, path) == ({"state": "pending"}, initial)
    approvals.save(storage, path, {"state": "completed"}, initial)
    with pytest.raises(Conflict):
        approvals.save(storage, path, {"state": "pending"}, initial)
    with pytest.raises(Conflict):
        approvals.save(storage, path, {"state": "pending"}, None)
    assert approvals.load_json(storage, path)[0]["state"] == "completed"


async def test_timer_uses_existing_authenticated_queue_and_stable_deadline(bridge, monkeypatch):  # noqa: F811
    from mica.agents.analysis import job_lifecycle

    monkeypatch.setattr(approvals.time, "time", lambda: 10.1)
    await approvals.watch(bridge, approvals.resolve("write"), "a" * 32, 70.2)
    args = job_lifecycle.enqueue.call_args.args
    assert args[1] == "approval-write-" + "a" * 32 + "-71"
    assert args[2] == "/api/v1/internal/slack-analysis/approvals/write/" + "a" * 32
    assert args[3] == 61


@pytest.mark.parametrize("allowed", [False, True])
async def test_expiry_endpoint_requires_task_dispatcher(bridge, monkeypatch, allowed):  # noqa: F811
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(
        settings=bridge.app, repository=bridge.repository,
    ))))
    callback = AsyncMock()
    kind = replace(approvals.resolve("write"), expire=callback)
    monkeypatch.setattr(approvals, "resolve", lambda _: kind)
    monkeypatch.setattr(slack_analysis, "get_principal", AsyncMock(return_value=SimpleNamespace(
        id=bridge.app.cloud_tasks_service_account.lower() if allowed else "worker@example.com"
    )))
    monkeypatch.setattr(slack_analysis, "admit", AsyncMock())
    if allowed:
        result = await slack_analysis.expire_approval("write", "a" * 32, request)
        assert result == {"status": "checked"}
        callback.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await slack_analysis.expire_approval("write", "a" * 32, request)
        assert error.value.status_code == 403
        callback.assert_not_called()
