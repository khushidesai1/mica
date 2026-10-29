"""Platform intake is bounded, durable, idempotent and independent of app reporting."""

import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from mica.agents.analysis import observability
from mica.api import routes
from mica.api.auth import Principal
from mica.services import failure_logs


def envelope(status=503, **changes):
    entry = {
        "logName": "projects/test/logs/run.googleapis.com%2Frequests",
        "insertId": "platform-entry-1",
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "project_id": "test",
                "service_name": "mica",
                "revision_name": "mica-00001",
            },
        },
        "httpRequest": {
            "status": status,
            "requestUrl": "https://mica.example/api/v1/internal/tasks/private-task/process?token=secret",
        },
        "textPayload": "password=secret raw exception and user content",
    }
    entry.update(changes)
    return {
        "message": {
            "messageId": "message-1",
            "data": base64.b64encode(json.dumps(entry).encode()).decode(),
        }
    }


def parse(value):
    return failure_logs.parse(json.dumps(value).encode(), project="test", service="mica")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_safe_http_metadata_including_503(status):
    observation = parse(envelope(status))
    assert observation is not None
    assert observation.http_status == status
    assert observation.route == "/api/v1/internal/tasks/{task_id}/process"
    assert observation.source == "cloud_run"
    stored = observation.model_dump_json()
    assert "private-task" not in stored and "secret" not in stored and "password" not in stored


@pytest.mark.parametrize(
    "change",
    [
        {"logName": "projects/test/logs/run.googleapis.com%2Fstderr"},
        {"logName": "projects/other/logs/run.googleapis.com%2Frequests"},
        {
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"project_id": "test", "service_name": "other"},
            }
        },
        {"httpRequest": {"status": 200}},
        {"httpRequest": {"status": "503"}},
    ],
)
def test_wrong_sources_and_nonfailures_ignored(change):
    assert parse(envelope(**change)) is None


@pytest.mark.parametrize("path", failure_logs.REPORTER_PATHS)
def test_reporter_requests_never_feed_back(path):
    assert (
        parse(
            envelope(
                httpRequest={
                    "status": 503,
                    "requestUrl": "https://mica.example" + path + "/?private=secret",
                }
            )
        )
        is None
    )


def test_system_error_without_request_and_no_false_recovery():
    item = parse(
        envelope(
            logName="projects/test/logs/run.googleapis.com%2Fvarlog%2Fsystem",
            severity="ERROR",
            httpRequest={},
        )
    )
    assert item and item.cause == "infra_platform_error" and item.route == "platform"
    assert (
        parse(
            envelope(
                logName="projects/test/logs/run.googleapis.com%2Fvarlog%2Fsystem", severity="INFO"
            )
        )
        is None
    )


def test_unknown_route_never_copies_arbitrary_path():
    item = parse(
        envelope(
            httpRequest={
                "status": 500,
                "requestUrl": "https://mica.example/private/secret?token=secret",
            }
        )
    )
    assert item and item.route == "unmatched route"
    assert "secret" not in item.model_dump_json()


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"message": {"data": "invalid"}},
        {"message": {"data": base64.b64encode(b"[]").decode()}},
    ],
)
def test_malformed_acknowledged_as_ignored(value):
    assert parse(value) is None


async def test_duplicate_publications_count_once_and_new_occurrence_counts(repository):
    first = envelope()
    replay = copy.deepcopy(first)
    replay["message"]["messageId"] = "message-2"
    for value in (first, replay):
        item = parse(value)
        assert item
        await repository.record_failure(item, 100)
    assert next(iter(repository.failures.values())).count == 1
    item = parse(envelope(insertId="platform-entry-2"))
    assert item
    await repository.record_failure(item, 101)
    assert next(iter(repository.failures.values())).count == 2
    row = next(iter(repository.failures.values()))
    summary = observability.summarize(row)
    assert "recovery not confirmed" in summary and "unavailable in platform logs" in summary


def test_missing_insert_id_uses_stable_message_id():
    assert parse(envelope(insertId="")) == parse(envelope(insertId=""))
    value = envelope(insertId="")
    value["message"].pop("messageId")
    assert parse(value) is None


async def invoke(body, services):
    app = FastAPI()
    app.include_router(routes.router)
    request = Request(
        {"type": "http", "app": app},
        receive=AsyncMock(return_value={"type": "http.request", "body": body}),
    )
    return await routes.ingest_failure_log(request, Principal(id="control@example.com"), services)


async def test_endpoint_ack_only_after_durable_acceptance(repository, settings, monkeypatch):
    settings.gcp_project = "test"
    monkeypatch.setenv("K_SERVICE", "mica")
    services = SimpleNamespace(settings=settings, repository=repository)
    body = json.dumps(envelope()).encode()
    assert await invoke(body, services) == {"status": "recorded"}
    assert await invoke(body, services) == {"status": "recorded"}
    assert next(iter(repository.failures.values())).count == 1
    monkeypatch.setattr(repository, "record_failure", AsyncMock(side_effect=RuntimeError("secret")))
    with pytest.raises(HTTPException) as error:
        await invoke(body, services)
    assert error.value.status_code == 503 and "secret" not in error.value.detail


async def test_endpoint_requires_internal_identity(repository, settings, monkeypatch):
    settings.env = "production"
    settings.cloud_tasks_service_account = "different@example.com"
    with pytest.raises(HTTPException) as error:
        await invoke(
            json.dumps(envelope()).encode(),
            SimpleNamespace(settings=settings, repository=repository),
        )
    assert error.value.status_code == 403


async def test_disabled_and_oversize_are_bounded(repository, settings, monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    services = SimpleNamespace(settings=settings, repository=repository)
    assert await invoke(json.dumps(envelope()).encode(), services) == {"status": "ignored"}
    assert await invoke(b"x" * (failure_logs.MAX_ENVELOPE + 1), services) == {"status": "ignored"}
    assert not repository.failures
