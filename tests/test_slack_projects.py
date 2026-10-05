from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from pistachio.api.app import create_app
from pistachio.config import Settings
from pistachio.connectors.notifications import ChannelNotifier
from pistachio.connectors.routing import bind_delivery_route, task_delivery_route
from pistachio.models import (
    ApprovalKind,
    ApprovalRequest,
    ArtifactRef,
    ConversationMessage,
    DeliveryRoute,
    TaskContract,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskSource,
    TaskStatus,
)
from pistachio.storage.memory import InMemoryRepository


class FakeWebClient:
    messages: list[dict[str, object]] = []
    views: list[dict[str, object]] = []
    uploads: list[dict[str, object]] = []
    edits: list[dict[str, object]] = []

    def __init__(self, token: str) -> None:
        self.token = token

    def chat_postMessage(self, **kwargs: object) -> dict[str, str]:
        self.messages.append(kwargs)
        return {"ts": f"posted-{len(self.messages)}"}

    def chat_update(self, **kwargs: object) -> dict[str, str]:
        self.edits.append(kwargs)
        return {"ts": str(kwargs["ts"])}

    def views_open(self, **kwargs: object) -> None:
        self.views.append(kwargs)

    def files_upload_v2(self, **kwargs: object) -> None:
        file_path = str(kwargs["file"])
        self.uploads.append({**kwargs, "content": Path(file_path).read_bytes()})


class FakeDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[str] = []

    async def enqueue(self, task_id: str) -> None:
        self.task_ids.append(task_id)


def _headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/x-www-form-urlencoded",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


def _client(tmp_path, monkeypatch, slack_user_map=None) -> TestClient:
    FakeWebClient.messages.clear()
    FakeWebClient.views.clear()
    FakeWebClient.uploads.clear()
    monkeypatch.setattr("pistachio.connectors.slack.WebClient", FakeWebClient)
    monkeypatch.setattr("pistachio.connectors.notifications.WebClient", FakeWebClient)
    settings = Settings(
        env="test",
        auth_mode="dev",
        dev_user_email="alice@example.org",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
        slack_bot_token="test-token",
        slack_signing_secret="test-secret",
        slack_user_map=slack_user_map or {"U123": "alice@example.org"},
    )
    return TestClient(app=create_app(settings=settings, repository=InMemoryRepository()))


def test_unbound_dm_offers_project_creation(tmp_path, monkeypatch) -> None:
    payload = {
        "type": "event_callback",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "channel_type": "im",
            "channel": "D123",
            "user": "U123",
            "text": "hello",
        },
    }
    body = json.dumps(payload).encode()
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/integrations/slack/events",
            content=body,
            headers={**_headers(body, "test-secret"), "content-type": "application/json"},
        )

    assert response.status_code == 200
    actions = FakeWebClient.messages[-1]["blocks"][1]["elements"]  # type: ignore[index]
    assert [item["action_id"] for item in actions] == ["create_project"]


def test_slack_modal_creates_and_binds_project(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        action_payload = {
            "type": "block_actions",
            "trigger_id": "trigger-1",
            "user": {"id": "U123"},
            "channel": {"id": "D123"},
            "actions": [{"action_id": "create_project"}],
        }
        action_body = urlencode(
            {"payload": json.dumps(action_payload, separators=(",", ":"))}
        ).encode()
        action_response = client.post(
            "/integrations/slack/actions",
            content=action_body,
            headers=_headers(action_body, "test-secret"),
        )
        assert action_response.status_code == 200
        assert FakeWebClient.views[-1]["view"]["callback_id"] == "create_project_submit"  # type: ignore[index]

        submission_payload = {
            "type": "view_submission",
            "user": {"id": "U123"},
            "view": {
                "callback_id": "create_project_submit",
                "private_metadata": json.dumps({"channel": "D123"}),
                "state": {
                    "values": {
                        "name_block": {"name": {"value": "AML Atlas"}},
                        "slug_block": {"slug": {"value": "aml-atlas"}},
                        "members_block": {
                            "members": {"value": "bob@example.org, carol@example.org"}
                        },
                    }
                },
            },
        }
        submission_body = urlencode(
            {"payload": json.dumps(submission_payload, separators=(",", ":"))}
        ).encode()
        submission_response = client.post(
            "/integrations/slack/actions",
            content=submission_body,
            headers=_headers(submission_body, "test-secret"),
        )
        projects = client.get("/api/v1/projects").json()

    assert submission_response.json() == {"response_action": "clear"}
    assert projects[0]["slug"] == "aml-atlas"
    assert set(projects[0]["members"]) == {
        "alice@example.org",
        "bob@example.org",
        "carol@example.org",
    }
    assert FakeWebClient.messages[-1]["text"] == "Active project: AML Atlas (aml-atlas)."


def test_slack_task_uses_workspace_channel_and_thread_correlation(tmp_path, monkeypatch) -> None:
    with _client(
        tmp_path,
        monkeypatch,
        {
            "T123:U123": "alice@example.org",
            "U123": "someone-else@example.org",
        },
    ) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "AML Atlas", "slug": "aml-atlas"}
        ).json()
        dispatcher = FakeDispatcher()
        client.app.state.services.dispatcher = dispatcher

        bind_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-bind",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "project: aml-atlas",
                "ts": "1712345678.000001",
            },
        }
        bind_body = json.dumps(bind_payload).encode()
        client.post(
            "/integrations/slack/events",
            content=bind_body,
            headers={**_headers(bind_body, "test-secret"), "content-type": "application/json"},
        )

        task_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-task",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "Run differential expression",
                "ts": "1712345678.000002",
            },
        }
        task_body = json.dumps(task_payload).encode()
        response = client.post(
            "/integrations/slack/events",
            content=task_body,
            headers={**_headers(task_body, "test-secret"), "content-type": "application/json"},
        )
        task_id = response.json()["task_id"]
        task = client.get(f"/api/v1/tasks/{task_id}").json()

    assert task["envelope"]["project_id"] == project["id"]
    assert task["envelope"]["conversation_id"] == "slack:T123:D123"
    assert task["envelope"]["thread_id"] == "1712345678.000002"
    assert task["envelope"]["external_event_id"] == "slack:T123:Ev-task"
    assert dispatcher.task_ids == [task_id]
    assert FakeWebClient.messages[-1]["thread_ts"] == "1712345678.000002"
    assert FakeWebClient.messages[-1]["text"] == task["conversation"][-1]["text"]
    assert task_id not in str(FakeWebClient.messages[-1]["text"])


def test_slack_retry_does_not_repeat_acknowledgement_or_enqueue(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        client.post("/api/v1/projects", json={"name": "AML Atlas", "slug": "aml-atlas"})
        dispatcher = FakeDispatcher()
        client.app.state.services.dispatcher = dispatcher

        bind_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-bind-retry",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "project: aml-atlas",
                "ts": "1712345678.000101",
            },
        }
        bind_body = json.dumps(bind_payload).encode()
        client.post(
            "/integrations/slack/events",
            content=bind_body,
            headers={**_headers(bind_body, "test-secret"), "content-type": "application/json"},
        )

        task_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-task-retry",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "Run differential expression",
                "ts": "1712345678.000102",
            },
        }
        task_body = json.dumps(task_payload).encode()
        headers = {
            **_headers(task_body, "test-secret"),
            "content-type": "application/json",
        }
        first = client.post("/integrations/slack/events", content=task_body, headers=headers)
        messages_after_first_delivery = len(FakeWebClient.messages)
        second = client.post("/integrations/slack/events", content=task_body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(FakeWebClient.messages) == messages_after_first_delivery
    assert dispatcher.task_ids == [first.json()["task_id"]]


def test_slack_thread_updates_and_cancels_the_active_task(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        client.post("/api/v1/projects", json={"name": "AML Atlas", "slug": "aml-atlas"})
        dispatcher = FakeDispatcher()
        client.app.state.services.dispatcher = dispatcher

        bind_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-bind-chat",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "project: aml-atlas",
                "ts": "1712345678.000201",
            },
        }
        bind_body = json.dumps(bind_payload).encode()
        client.post(
            "/integrations/slack/events",
            content=bind_body,
            headers={**_headers(bind_body, "test-secret"), "content-type": "application/json"},
        )
        initial_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-task-chat",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "Analyze the cohort",
                "ts": "1712345678.000202",
            },
        }
        initial_body = json.dumps(initial_payload).encode()
        initial = client.post(
            "/integrations/slack/events",
            content=initial_body,
            headers={**_headers(initial_body, "test-secret"), "content-type": "application/json"},
        )
        task_id = initial.json()["task_id"]
        assert FakeWebClient.messages[-1]["blocks"][1]["elements"][0]["action_id"] == (
            "cancel_task"
        )

        follow_up_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-context-chat",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "Only include treatment-naive patients",
                "thread_ts": "1712345678.000202",
                "ts": "1712345678.000203",
            },
        }
        follow_up_body = json.dumps(follow_up_payload).encode()
        follow_up = client.post(
            "/integrations/slack/events",
            content=follow_up_body,
            headers={
                **_headers(follow_up_body, "test-secret"),
                "content-type": "application/json",
            },
        )
        assert follow_up.json()["task_id"] == task_id
        revised = client.get(f"/api/v1/tasks/{task_id}").json()
        assert revised["context_updates"][0]["message"] == (
            "Only include treatment-naive patients"
        )
        assert dispatcher.task_ids == [task_id, task_id]
        assert FakeWebClient.messages[-1]["text"] == revised["conversation"][-1]["text"]

        cancel_payload = {
            "type": "event_callback",
            "team_id": "T123",
            "event_id": "Ev-cancel-chat",
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": "D123",
                "user": "U123",
                "text": "cancel",
                "thread_ts": "1712345678.000202",
                "ts": "1712345678.000204",
            },
        }
        cancel_body = json.dumps(cancel_payload).encode()
        cancelled = client.post(
            "/integrations/slack/events",
            content=cancel_body,
            headers={**_headers(cancel_body, "test-secret"), "content-type": "application/json"},
        )
        assert cancelled.json()["task_id"] == task_id
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "CANCELLED"
        assert "stopped this run" in str(FakeWebClient.messages[-1]["text"])


def test_slack_hyperparameter_approval_modal_edits_and_confirms(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "AML Atlas", "slug": "aml-atlas"}
        ).json()
        dispatcher = FakeDispatcher()
        client.app.state.services.dispatcher = dispatcher
        approval = ApprovalRequest(
            task_id="pending",
            kind=ApprovalKind.HYPERPARAMETER,
            title="Tool hyperparameters need confirmation",
            detail="Review one tool setting before execution.",
            payload={
                "fields": [
                    {
                        "id": "scanpy.n_neighbors",
                        "label": "Scanpy: neighbors",
                        "proposed_value": "15",
                        "options": ["10", "15", "30"],
                        "scientific_impact": "Changes graph connectivity.",
                    }
                ]
            },
        )
        task = TaskRecord(
            envelope=TaskEnvelope(
                project_id=project["id"],
                source=TaskSource.SLACK,
                external_event_id="slack:T123:Ev-approval",
                conversation_id="slack:T123:D123",
                thread_id="1712345678.000010",
                principal_id="alice@example.org",
                prompt="Run clustering",
            ),
            status=TaskStatus.NEEDS_APPROVAL,
            contract=TaskContract(objective="Run clustering", requires_execution=True),
            approvals=[approval],
            planner_version=1,
        )
        approval.task_id = task.id
        repository = client.app.state.services.repository
        asyncio.run(repository.create_task(task))

        action_payload = {
            "type": "block_actions",
            "trigger_id": "trigger-approval",
            "team": {"id": "T123"},
            "user": {"id": "U123"},
            "channel": {"id": "D123"},
            "container": {"thread_ts": "1712345678.000010"},
            "actions": [
                {
                    "action_id": "review_task_approval",
                    "value": json.dumps({"task_id": task.id, "approval_id": approval.id}),
                }
            ],
        }
        action_body = urlencode(
            {"payload": json.dumps(action_payload, separators=(",", ":"))}
        ).encode()
        action_response = client.post(
            "/integrations/slack/actions",
            content=action_body,
            headers=_headers(action_body, "test-secret"),
        )
        view = FakeWebClient.views[-1]["view"]  # type: ignore[index]

        submission_payload = {
            "type": "view_submission",
            "user": {"id": "U123"},
            "view": {
                "callback_id": "task_approval_submit",
                "private_metadata": view["private_metadata"],  # type: ignore[index]
                "state": {
                    "values": {
                        "field_0": {
                            "value": {
                                "selected_option": {"text": {"type": "plain_text"}, "value": "30"}
                            }
                        },
                        "custom_0": {"value": {"value": ""}},
                    }
                },
            },
        }
        submission_body = urlencode(
            {"payload": json.dumps(submission_payload, separators=(",", ":"))}
        ).encode()
        submission_response = client.post(
            "/integrations/slack/actions",
            content=submission_body,
            headers=_headers(submission_body, "test-secret"),
        )
        updated = asyncio.run(repository.get_task(task.id))

    assert action_response.status_code == 200
    assert view["callback_id"] == "task_approval_submit"  # type: ignore[index]
    assert submission_response.json() == {"response_action": "clear"}
    assert updated is not None
    assert updated.approvals[0].status == "approved"
    assert updated.approvals[0].payload["decision"] == "Scanpy: neighbors: 30"
    assert dispatcher.task_ids == [task.id]
    assert FakeWebClient.messages[-1]["thread_ts"] == "1712345678.000010"


def test_slack_approval_notification_stays_in_task_thread(tmp_path, monkeypatch) -> None:
    FakeWebClient.messages.clear()
    monkeypatch.setattr("pistachio.connectors.notifications.WebClient", FakeWebClient)
    settings = Settings(
        env="test",
        artifact_root=tmp_path,
        slack_bot_token="test-token",
        control_plane_url="https://pistachio.example",
    )
    approval = ApprovalRequest(
        task_id="task-1",
        kind=ApprovalKind.HYPERPARAMETER,
        title="Confirm settings",
        detail="Review the proposed value.",
        payload={"fields": [{"id": "resolution", "proposed_value": "0.8"}]},
    )
    task = TaskRecord(
        id="task-1",
        envelope=TaskEnvelope(
            project_id="project-1",
            source=TaskSource.SLACK,
            conversation_id="slack:T123:D123",
            thread_id="1712345678.000020",
            principal_id="alice@example.org",
            prompt="Cluster cells",
        ),
        status=TaskStatus.NEEDS_APPROVAL,
        approvals=[approval],
    )

    task.conversation.append(ConversationMessage(
        event_key="approval", kind="approval", text="Please review these settings.",
    ))
    asyncio.run(ChannelNotifier(settings).notify(task))

    message = FakeWebClient.messages[-1]
    assert message["channel"] == "D123"
    assert message["thread_ts"] == "1712345678.000020"
    actions = message["blocks"][2]["elements"]  # type: ignore[index]
    native_action = next(item for item in actions if item["action_id"] == "review_task_approval")
    assert json.loads(native_action["value"]) == {
        "task_id": "task-1",
        "approval_id": approval.id,
    }


def test_slack_delivers_four_artifacts_individually_and_bundles_five(
    tmp_path, monkeypatch
) -> None:
    FakeWebClient.messages.clear()
    FakeWebClient.uploads.clear()
    monkeypatch.setattr("pistachio.connectors.notifications.WebClient", FakeWebClient)
    settings = Settings(
        env="test",
        artifact_root=tmp_path,
        slack_bot_token="test-token",
    )

    def completed_task(count: int) -> TaskRecord:
        artifacts = []
        for index in range(count):
            path = tmp_path / f"figure-{count}-{index + 1}.png"
            path.write_bytes(f"figure {index + 1}".encode())
            artifacts.append(
                ArtifactRef(name=path.name, uri=path.resolve().as_uri(), media_type="image/png")
            )
        return TaskRecord(
            envelope=TaskEnvelope(
                project_id="project-1",
                source=TaskSource.SLACK,
                conversation_id="slack:T123:D123",
                thread_id="1712345678.000040",
                principal_id="alice@example.org",
                prompt="Create the necessary figures",
            ),
            status=TaskStatus.SUCCEEDED,
            result=TaskResult(summary="Done", artifacts=artifacts, exit_code=0),
        )

    asyncio.run(ChannelNotifier(settings).notify(completed_task(4)))

    assert len(FakeWebClient.uploads) == 4
    assert all(upload["thread_ts"] == "1712345678.000040" for upload in FakeWebClient.uploads)
    assert [upload["filename"] for upload in FakeWebClient.uploads] == [
        f"figure-4-{index}.png" for index in range(1, 5)
    ]

    FakeWebClient.uploads.clear()
    bundled_task = completed_task(5)
    asyncio.run(ChannelNotifier(settings).notify(bundled_task))

    assert len(FakeWebClient.uploads) == 1
    upload = FakeWebClient.uploads[0]
    assert upload["filename"] == f"{bundled_task.public_name}-artifacts.zip"
    with zipfile.ZipFile(BytesIO(upload["content"])) as archive:  # type: ignore[arg-type]
        assert archive.namelist() == [f"figure-5-{index}.png" for index in range(1, 6)]


def test_slack_compute_approval_button_confirms_and_enqueues(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "AML Atlas", "slug": "aml-atlas"}
        ).json()
        dispatcher = FakeDispatcher()
        client.app.state.services.dispatcher = dispatcher
        approval = ApprovalRequest(
            task_id="pending",
            kind=ApprovalKind.COMPUTE,
            title="Compute plan needs approval",
            detail="more than 64 GiB RAM requested; runtime exceeds six hours",
            payload={"memory_gib": 96, "timeout_hours": 8},
        )
        task = TaskRecord(
            envelope=TaskEnvelope(
                project_id=project["id"],
                source=TaskSource.SLACK,
                external_event_id="slack:T123:Ev-compute",
                conversation_id="slack:T123:D123",
                thread_id="1712345678.000030",
                principal_id="alice@example.org",
                prompt="Run the large analysis",
            ),
            status=TaskStatus.NEEDS_APPROVAL,
            approvals=[approval],
        )
        approval.task_id = task.id
        repository = client.app.state.services.repository
        asyncio.run(repository.create_task(task))

        monkeypatch.setattr("pistachio.connectors.notifications.WebClient", FakeWebClient)
        asyncio.run(client.app.state.services.tasks._notify(task))
        approval_message = FakeWebClient.messages[-1]
        actions = approval_message["blocks"][2]["elements"]  # type: ignore[index]
        approve = next(item for item in actions if item["action_id"] == "approve_task_approval")

        action_payload = {
            "type": "block_actions",
            "trigger_id": "trigger-compute",
            "team": {"id": "T123"},
            "user": {"id": "U123"},
            "channel": {"id": "D123"},
            "container": {"thread_ts": "1712345678.000030"},
            "actions": [approve],
        }
        action_body = urlencode(
            {"payload": json.dumps(action_payload, separators=(",", ":"))}
        ).encode()
        response = client.post(
            "/integrations/slack/actions",
            content=action_body,
            headers=_headers(action_body, "test-secret"),
        )
        updated = asyncio.run(repository.get_task(task.id))

    assert approve["text"]["text"] == "Approve resource usage"  # type: ignore[index]
    assert approve["confirm"]["confirm"]["text"] == "Approve"  # type: ignore[index]
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert updated is not None
    assert updated.status == TaskStatus.RECEIVED
    assert updated.approvals[0].status == "approved"
    assert dispatcher.task_ids == [task.id]
    assert FakeWebClient.messages[-1]["thread_ts"] == "1712345678.000030"


def test_web_task_updates_use_bound_slack_route(tmp_path, monkeypatch) -> None:
    FakeWebClient.messages.clear()
    monkeypatch.setattr("pistachio.connectors.notifications.WebClient", FakeWebClient)
    settings = Settings(
        env="test",
        artifact_root=tmp_path,
        slack_bot_token="test-token",
    )
    repository = InMemoryRepository()
    task = TaskRecord(
        id="task-from-web",
        envelope=TaskEnvelope(
            project_id="project-1",
            source=TaskSource.WEB,
            conversation_id="web-session",
            principal_id="alice@example.org",
            prompt="Run clustering",
        ),
        status=TaskStatus.PLANNING,
    )
    asyncio.run(
        bind_delivery_route(
            repository,
            DeliveryRoute(
                project_id="project-1",
                principal_id="alice@example.org",
                source=TaskSource.SLACK,
                conversation_id="slack:T123:D123",
            ),
        )
    )
    notifier = ChannelNotifier(settings, repository)

    task.conversation.append(ConversationMessage(event_key="start", text="I’m preparing the plan."))
    asyncio.run(notifier.notify(task))
    task.status = TaskStatus.RUNNING
    task.conversation.append(ConversationMessage(
        event_key="running", text="The analysis is running.",
    ))
    asyncio.run(notifier.notify(task))
    delivered = asyncio.run(task_delivery_route(repository, task.id, TaskSource.SLACK))

    assert FakeWebClient.messages[0]["channel"] == "D123"
    assert "thread_ts" not in FakeWebClient.messages[0]
    assert len(FakeWebClient.messages) == 2
    assert FakeWebClient.messages[1]["thread_ts"] == "posted-1"
    assert delivered is not None
    assert delivered.thread_id == "posted-1"


def test_slack_reply_continues_web_originated_task(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "AML Atlas", "slug": "aml-atlas"}
        ).json()
        services = client.app.state.services
        services.dispatcher = FakeDispatcher()
        asyncio.run(services.repository.set_channel_project(
            "slack:T123:U123:D123", project["id"],
        ))
        asyncio.run(bind_delivery_route(services.repository, DeliveryRoute(
            project_id=project["id"], principal_id="alice@example.org",
            source=TaskSource.SLACK, conversation_id="slack:T123:D123",
        )))
        task, _ = asyncio.run(services.tasks.create(TaskEnvelope(
            project_id=project["id"], principal_id="alice@example.org",
            source=TaskSource.WEB, conversation_id="web-session", prompt="Analyze the samples",
        )))
        route = asyncio.run(task_delivery_route(services.repository, task.id, TaskSource.SLACK))
        payload = {
            "type": "event_callback", "team_id": "T123", "event_id": "followup-from-slack",
            "event": {
                "type": "message", "channel_type": "im", "channel": "D123", "user": "U123",
                "text": "Keep the colors consistent.", "thread_ts": route.thread_id,
                "ts": "1712345678.000500",
            },
        }
        body = json.dumps(payload).encode()
        response = client.post(
            "/integrations/slack/events", content=body,
            headers={**_headers(body, "test-secret"), "content-type": "application/json"},
        )
        assert response.json()["task_id"] == task.id
        saved = client.get(f"/api/v1/tasks/{task.id}").json()
        assert saved["conversation"][-2]["text"] == "Keep the colors consistent."
        assert FakeWebClient.messages[-1]["text"] == saved["conversation"][-1]["text"]
        assert FakeWebClient.messages[-1]["thread_ts"] == route.thread_id
        assert len(asyncio.run(services.repository.list_tasks(project["id"]))) == 1


def _open_pending_approval(client, approval):
    project = client.post(
        "/api/v1/projects", json={"name": "Review", "slug": "review"}
    ).json()
    task = TaskRecord(
        envelope=TaskEnvelope(
            project_id=project["id"], source=TaskSource.SLACK,
            conversation_id="slack:T123:D123", thread_id="review-thread",
            principal_id="alice@example.org", prompt="Analyze the data",
        ),
        status=TaskStatus.NEEDS_APPROVAL, planner_version=1,
        contract=TaskContract(objective="Analyze the data", requires_execution=True),
        approvals=[approval],
    )
    approval.task_id = task.id
    asyncio.run(client.app.state.services.repository.create_task(task))
    client.app.state.services.dispatcher = FakeDispatcher()
    payload = {
        "type": "block_actions", "trigger_id": "trigger-review",
        "team": {"id": "T123"}, "user": {"id": "U123"}, "channel": {"id": "D123"},
        "actions": [{
            "action_id": "review_task_approval",
            "value": json.dumps({"task_id": task.id, "approval_id": approval.id}),
        }],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    response = client.post(
        "/integrations/slack/actions", content=body, headers=_headers(body, "test-secret")
    )
    assert response.status_code == 200
    return task, FakeWebClient.views[-1]["view"]


def _submit_review(client, view, values, *, user="U123"):
    payload = {
        "type": "view_submission", "team": {"id": "T123"}, "user": {"id": user},
        "view": {**view, "state": {"values": values}},
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return client.post(
        "/integrations/slack/actions", content=body, headers=_headers(body, "test-secret")
    )


def _large_approval(count=81):
    return ApprovalRequest(
        task_id="pending", kind=ApprovalKind.HYPERPARAMETER,
        title="Confirm all settings", detail="Review each setting before execution.",
        payload={"fields": [
            {"id": f"setting_{i}", "label": f"Setting {i}", "proposed_value": str(i)}
            for i in range(count)
        ]},
    )


def test_large_slack_approval_collects_all_pages_before_execution(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        task, view = _open_pending_approval(client, _large_approval())
        services = client.app.state.services
        for offset, stop in ((0, 40), (40, 80), (80, 81)):
            assert len(view["blocks"]) <= 100
            assert json.loads(view["private_metadata"])["offset"] == offset
            values = {f"field_{i}": {"value": {"value": str(i + 100)}}
                      for i in range(offset, stop)}
            response = _submit_review(client, view, values)
            assert response.status_code == 200
            if stop < 81:
                assert response.json()["response_action"] == "update"
                view = response.json()["view"]
                # A duplicate page delivery returns the same next page without approving.
                assert _submit_review(client, {
                    **view, "private_metadata": json.dumps({
                        **json.loads(view["private_metadata"]), "offset": offset,
                    }),
                }, values).json()["view"] == view
                saved = asyncio.run(services.repository.get_task(task.id))
                assert saved.approvals[0].status == "pending"
                assert services.dispatcher.task_ids == []
        assert response.json() == {"response_action": "clear"}
        saved = asyncio.run(services.repository.get_task(task.id))
        assert saved.approvals[0].status == "approved"
        for i in range(81):
            assert f"Setting {i}: {i + 100}" in saved.approvals[0].payload["decision"]
        assert services.dispatcher.task_ids == [task.id]


def test_large_slack_approval_rejects_stale_or_incomplete_review(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        task, view = _open_pending_approval(client, _large_approval(41))
        services = client.app.state.services
        metadata = json.loads(view["private_metadata"])
        second_page = {**view, "private_metadata": json.dumps({**metadata, "offset": 40})}
        values = {"field_40": {"value": {"value": "140"}}}
        skipped = _submit_review(client, second_page, values)
        assert skipped.json()["response_action"] == "errors"
        first_values = {f"field_{i}": {"value": {"value": str(i)}} for i in range(40)}
        second_page = _submit_review(client, view, first_values).json()["view"]
        # A changed approval must not reuse decisions collected for the older version.
        saved = asyncio.run(services.repository.get_task(task.id))
        saved.approvals[0].payload["fields"][0]["proposed_value"] = "new value"
        asyncio.run(services.repository.update_task(saved))
        stale = _submit_review(client, second_page, values)
        assert stale.json()["response_action"] == "errors"
        assert services.dispatcher.task_ids == []
        assert asyncio.run(services.repository.get_task(task.id)).approvals[0].status == "pending"


def test_slack_review_without_fields_can_approve_exact_tool(tmp_path, monkeypatch):
    from pistachio.models import ToolProposal

    with _client(tmp_path, monkeypatch) as client:
        services = client.app.state.services
        tool = ToolProposal(
            name="ExampleTool", source_url="https://example.org/tool.git", version="1.2.3",
            commit_sha="a" * 40, install_recipe="pip install tool==1.2.3",
            why_needed="Required analysis format",
        )
        asyncio.run(services.repository.put_tool(tool))
        approval = ApprovalRequest(
            task_id="pending", kind=ApprovalKind.TOOL_ADD,
            title="Approve ExampleTool 1.2.3",
            detail="Source: https://example.org/tool.git, v1.2.3",
            payload={"tool_ids": [tool.id]},
        )
        task, view = _open_pending_approval(client, approval)
        assert view["title"]["text"] == "Review approval"
        response = _submit_review(client, view, {
            "decision": {"value": {"selected_option": {"value": "approve"}}},
        })
        assert response.json() == {"response_action": "clear"}
        saved = asyncio.run(services.repository.get_task(task.id))
        assert saved.approvals[0].status == "approved"
        assert saved.status == TaskStatus.NEEDS_INPUT  # Wait for the exact tool build.
        assert services.dispatcher.task_ids == []
        approved_tool = asyncio.run(services.repository.get_tool(tool.id))
        assert approved_tool.status == "approved"
        assert approved_tool.commit_sha == "a" * 40


def test_slack_review_without_fields_preserves_explicit_scientific_consent(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        task, view = _open_pending_approval(client, ApprovalRequest(
            task_id="pending", kind=ApprovalKind.SCIENTIFIC_STEP,
            title="Choose the population", detail="Confirm which samples should be included.",
        ))
        values = {"decision": {"value": {"selected_option": {"value": "approve"}}}}
        response = _submit_review(client, view, values)
        assert response.json()["response_action"] == "errors"
        values["response"] = {"value": {"value": "Use treatment-naive samples only."}}
        assert _submit_review(client, view, values).json() == {"response_action": "clear"}
        saved = asyncio.run(client.app.state.services.repository.get_task(task.id))
        assert "Use treatment-naive samples only." in saved.contract.authorized_scientific_actions


def test_slack_review_without_fields_can_decline(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        task, view = _open_pending_approval(client, ApprovalRequest(
            task_id="pending", kind=ApprovalKind.TOOL_ADD,
            title="Review proposed tool", detail="This tool requires your approval.",
        ))
        response = _submit_review(client, view, {
            "decision": {"value": {"selected_option": {"value": "decline"}}},
        })
        assert response.json() == {"response_action": "clear"}
        services = client.app.state.services
        saved = asyncio.run(services.repository.get_task(task.id))
        assert saved.approvals[0].status == "rejected"
        assert saved.status == TaskStatus.CANCELLED
        assert services.dispatcher.task_ids == []


def test_all_approval_notifications_have_native_actions_and_no_website_links(tmp_path):
    notifier = ChannelNotifier(Settings(env="test", artifact_root=tmp_path,
                                        control_plane_url="https://pistachio.example"))
    for approval in [_large_approval(), *[
        ApprovalRequest(task_id="pending", kind=kind, title="Review", detail="Decision needed")
        for kind in ApprovalKind
    ]]:
        task = TaskRecord(
            envelope=TaskEnvelope(project_id="project", conversation_id="slack:T123:D123",
                                  principal_id="alice@example.org", prompt="Analyze"),
            approvals=[approval],
        )
        elements = [element for block in notifier._approval_blocks(task, "Review this")
                    for element in block.get("elements", [])]
        assert elements
        assert all("url" not in element for element in elements)
        assert elements[0]["action_id"] in {"review_task_approval", "approve_task_approval"}
