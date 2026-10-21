"""Migration must hold intake durably while existing work can finish safely."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from mica.agents.analysis import atomx_connections
from mica.agents.analysis.batch import BatchBridge
from mica.agents.analysis.config import Settings as AnalysisSettings
from mica.api import atomx, migration, slack_analysis
from mica.api.app import create_app
from mica.config import Settings
from mica.connectors import gmail, slack
from mica.storage.memory import InMemoryRepository

CONTROL = "control@example.iam.gserviceaccount.com"
AUTH = {"x-goog-authenticated-user-email": CONTROL}


@pytest.fixture
async def runtime(tmp_path):
    settings = Settings(
        _env_file=None, env="test", artifact_root=tmp_path, auth_mode="iap",
        migration_service="old-service", migration_default_mode="frozen",
        slack_signing_secret="signature-test", slack_agent_backend="claude_batch",
        cloud_tasks_service_account=CONTROL,
    )
    repository = InMemoryRepository()
    app = create_app(settings=settings, repository=repository)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://mica.test",
    ) as client:
        yield SimpleNamespace(settings=settings, repository=repository, app=app, client=client)


def event(text="Analyze the sample", timestamp="100.001"):
    return {
        "type": "event_callback", "team_id": "T1", "event_id": "Ev1",
        "event": {"type": "message", "channel_type": "im", "channel": "D1",
                  "user": "U1", "ts": timestamp, "text": text},
    }


def signed(settings, payload):
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.slack_signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return {"content": body, "headers": {
        "x-slack-request-timestamp": timestamp, "x-slack-signature": "v0=" + signature,
    }}


async def set_mode(runtime, mode):
    await runtime.repository.set_connector_state(
        "migration-admission:" + runtime.settings.migration_service, {"mode": mode}
    )


def buffered(runtime):
    return {k: v for k, v in runtime.repository.connector_state.items()
            if k.startswith(migration.PREFIX)}


async def test_signed_slack_is_durable_and_deduplicated_without_dispatch(runtime, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(slack, "dispatch_slack_event", dispatch)
    payload = event()
    for callback in (payload, payload):
        response = await runtime.client.post(
            "/integrations/slack/events", **signed(runtime.settings, callback)
        )
        assert response.status_code == 200
        assert response.json()["maintenance"] is True
    assert len(buffered(runtime)) == 1
    assert next(iter(buffered(runtime).values()))["payload"] == payload
    dispatch.assert_not_awaited()
    challenge = await runtime.client.post(
        "/integrations/slack/events",
        **signed(runtime.settings, {"type": "url_verification", "challenge": "verify"}),
    )
    assert challenge.json() == {"challenge": "verify"}
    assert (await runtime.client.get("/api/v1/healthz")).status_code == 200


async def test_slack_is_not_acknowledged_if_its_durable_write_fails(runtime, monkeypatch):
    original = runtime.repository.set_connector_state
    monkeypatch.setattr(runtime.repository, "set_connector_state", AsyncMock(side_effect=OSError))
    response = await runtime.client.post(
        "/integrations/slack/events", **signed(runtime.settings, event())
    )
    assert response.status_code >= 500
    assert not buffered(runtime)
    monkeypatch.setattr(runtime.repository, "set_connector_state", original)
    response = await runtime.client.post(
        "/integrations/slack/events", **signed(runtime.settings, event())
    )
    assert response.status_code == 200 and len(buffered(runtime)) == 1


async def test_slack_keeps_app_mentions_and_resends_as_distinct_events(runtime):
    message = event()
    mention = {**message, "event_id": "Ev2", "event": {
        **message["event"], "type": "app_mention",
    }}
    resend = {**event(timestamp="100.002"), "event_id": "Ev3"}
    for payload in (message, mention, resend):
        response = await runtime.client.post(
            "/integrations/slack/events", **signed(runtime.settings, payload)
        )
        assert response.status_code == 200
    saved = [record["payload"] for record in buffered(runtime).values()]
    assert {record["event_id"] for record in saved} == {"Ev1", "Ev2", "Ev3"}
    assert any(record["event"]["type"] == "app_mention" for record in saved)


async def test_slack_authentication_and_password_scrubbing_precede_retention(runtime):
    payload = event("password: do-not-retain-this")
    payload["event"]["blocks"] = [{"text": "password: do-not-retain-this"}]
    invalid = await runtime.client.post("/integrations/slack/events", json=payload)
    assert invalid.status_code == 401 and not buffered(runtime)
    accepted = await runtime.client.post(
        "/integrations/slack/events", **signed(runtime.settings, payload)
    )
    assert accepted.status_code == 200
    assert "do-not-retain-this" not in json.dumps(runtime.repository.connector_state)
    huge = {**event("x" * migration.MAX_PAYLOAD_BYTES, "100.002"), "event_id": "Ev2"}
    refused = await runtime.client.post(
        "/integrations/slack/events", **signed(runtime.settings, huge)
    )
    assert refused.status_code == 413 and len(buffered(runtime)) == 1


async def test_replay_repairs_enqueue_failure_and_does_not_create_a_second_request(
    runtime, monkeypatch
):
    payload = event()
    await runtime.client.post("/integrations/slack/events", **signed(runtime.settings, payload))
    key = next(iter(buffered(runtime)))
    identifier = key.removeprefix(migration.PREFIX)
    path = "/api/v1/internal/maintenance/replay-slack/" + identifier
    bridge = BatchBridge(runtime.settings, runtime.repository)
    schedule = AsyncMock(side_effect=[RuntimeError("queue unavailable"), None])
    monkeypatch.setattr(bridge, "schedule", schedule)

    async def receive(services, saved):
        return {"event_id": await bridge.accept(saved, principal="owner@example.org")}

    monkeypatch.setattr(slack_analysis, "receive", receive)
    await set_mode(runtime, "draining")
    # Neither a user nor the wrong stack may release held work.
    assert (await runtime.client.post(path, headers=AUTH)).status_code == 409
    await runtime.repository.set_connector_state("migration-replay", {"service": "old-service"})
    forbidden = await runtime.client.post(
        path, headers={"x-goog-authenticated-user-email": "other@example.org"}
    )
    assert forbidden.status_code == 403
    failed = await runtime.client.post(path, headers=AUTH)
    assert failed.status_code == 500
    assert runtime.repository.connector_state[key]["status"] == "buffered"
    succeeded = await runtime.client.post(path, headers=AUTH)
    assert succeeded.status_code == 200
    assert runtime.repository.connector_state[key]["status"] == "replayed"
    assert "payload" not in runtime.repository.connector_state[key]
    requests = {k: v for k, v in runtime.repository.connector_state.items()
                if k.startswith("claude-event:")}
    assert len(requests) == 1
    assert next(iter(requests.values()))["payload"] == payload
    assert (await runtime.client.post(path, headers=AUTH)).status_code == 200
    assert schedule.await_count == 2
    assert schedule.await_args_list[0] == schedule.await_args_list[1]


async def test_replay_cannot_run_frozen_or_with_another_replayer(runtime, monkeypatch):
    await runtime.client.post("/integrations/slack/events", **signed(runtime.settings, event()))
    identifier = next(iter(buffered(runtime))).removeprefix(migration.PREFIX)
    path = "/api/v1/internal/maintenance/replay-slack/" + identifier
    await runtime.repository.set_connector_state("migration-replay", {"service": "old-service"})
    dispatch = AsyncMock()
    monkeypatch.setattr(slack, "dispatch_slack_event", dispatch)
    assert (await runtime.client.post(path, headers=AUTH)).status_code == 503
    await set_mode(runtime, "draining")
    claim = await runtime.repository.begin_event("migration-replay-lock", lease_seconds=300)
    assert (await runtime.client.post(path, headers=AUTH)).status_code == 503
    dispatch.assert_not_awaited()
    await runtime.repository.release_event("migration-replay-lock", claim.token)


async def test_gmail_remains_retryable_without_consuming_or_advancing_cursor(runtime, monkeypatch):
    consume = AsyncMock()
    monkeypatch.setattr(gmail, "consume", consume)
    sentinel = {"history_id": "123", "page": "next-page"}
    await runtime.repository.set_connector_state("gmail-watch", sentinel)
    for selected in ("draining", "frozen"):
        await set_mode(runtime, selected)
        result = await runtime.client.post("/integrations/gmail/push", headers=AUTH, json={})
        assert result.status_code == 503 and result.headers["retry-after"] == "60"
    assert await runtime.repository.get_connector_state("gmail-watch") == sentinel
    consume.assert_not_awaited()
    invalid = await runtime.client.post("/integrations/gmail/push", json={})
    assert invalid.status_code == 401


async def test_drain_allows_existing_job_ticks_and_completions_but_freeze_stops_them(
    runtime, monkeypatch
):
    process = AsyncMock(return_value="complete")
    monkeypatch.setattr(BatchBridge, "process", process)
    route = "/api/v1/internal/slack-analysis/process/" + "a" * 32 + "/1"
    await set_mode(runtime, "draining")
    assert (await runtime.client.post(route, headers=AUTH)).status_code == 200
    # A completed callback's duplicate receipt must still be acknowledged while draining.
    await runtime.repository.mark_event("batch:done")
    completion = "/api/v1/internal/batch/completion"
    response = await runtime.client.post(
        completion, headers=AUTH, json={"message": {"messageId": "done"}}
    )
    assert response.json() == {"status": "duplicate"}
    for path in ("/api/v1/projects", "/api/v1/internal/gmail/renew-watch",
                 "/api/v1/internal/tools/catalog", "/api/v1/internal/maintenance/compact-idle"):
        response = await runtime.client.post(path, headers=AUTH, json={})
        assert response.status_code == 503
    await set_mode(runtime, "frozen")
    assert (await runtime.client.post(route, headers=AUTH)).status_code == 503
    assert (await runtime.client.post(completion, headers=AUTH, json={})).status_code == 503
    assert process.await_count == 1
    assert (await runtime.client.get("/api/v1/projects", headers=AUTH)).status_code == 200


async def test_frozen_atomx_does_not_consume_links_or_save_credentials(runtime, monkeypatch):
    app = runtime.settings
    app.signed_link_secret = "migration-test-application-key-with-entropy"
    app.control_plane_url = "https://mica.test"
    connections = atomx_connections.Connections(app, AnalysisSettings(), runtime.repository)
    monkeypatch.setattr(atomx, "service", lambda request: (SimpleNamespace(), connections))
    link = await connections.issue("owner@example.org", "connection", "event")
    token = link.split("#")[1]
    headers = {"Origin": "https://mica.test"}
    before = json.dumps(runtime.repository.connector_state, sort_keys=True)
    exchange = await runtime.client.post(
        "/integrations/atomx/exchange", headers=headers, json={"token": token}
    )
    assert exchange.status_code == 503
    assert json.dumps(runtime.repository.connector_state, sort_keys=True) == before
    invalid = await runtime.client.post(
        "/integrations/atomx/exchange", headers=headers, json={"token": "invalid"}
    )
    assert invalid.status_code == 400
    await set_mode(runtime, "open")
    assert (await runtime.client.post(
        "/integrations/atomx/exchange", headers=headers, json={"token": token}
    )).status_code == 200
    session = (await runtime.client.get("/integrations/atomx/session")).json()
    headers["X-CSRF-Token"] = session["csrf"]
    await set_mode(runtime, "frozen")
    before = json.dumps(runtime.repository.connector_state, sort_keys=True)
    result = await runtime.client.post(
        "/integrations/atomx/action", headers=headers,
        json={"action": "save", "username": "login", "password": "do-not-save"},
    )
    assert result.status_code == 503
    assert json.dumps(runtime.repository.connector_state, sort_keys=True) == before


async def test_frozen_startup_skips_tool_writes_and_bad_control_fails_closed(runtime, monkeypatch):
    bootstrap = AsyncMock()
    monkeypatch.setattr("mica.api.app.ensure_bootstrap_tools", bootstrap)
    async with runtime.app.router.lifespan_context(runtime.app):
        bootstrap.assert_not_awaited()
    await set_mode(runtime, "not-a-mode")
    async with runtime.app.router.lifespan_context(runtime.app):
        bootstrap.assert_not_awaited()
    result = await runtime.client.post("/api/v1/projects", headers=AUTH, json={})
    assert result.status_code == 503
    # Missing or invalid identity is still rejected before consulting maintenance state.
    assert (await runtime.client.post("/api/v1/projects", json={})).status_code == 401


async def test_service_modes_are_independent_with_shared_firestore(runtime):
    await set_mode(runtime, "open")
    other = runtime.settings.model_copy(update={"migration_service": "new-service"})
    assert await migration.mode(runtime.settings, runtime.repository) == "open"
    assert await migration.mode(other, runtime.repository) == "frozen"
    assert isinstance(runtime.app, FastAPI)
