from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from pistachio.api.app import create_app
from pistachio.config import Settings
from pistachio.models import (
    ArtifactRef,
    MemoryItem,
    MemoryKind,
    Project,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskStatus,
)
from pistachio.storage.memory import InMemoryRepository


def test_project_api_enforces_iap_identity(tmp_path) -> None:
    settings = Settings(
        env="test",
        auth_mode="iap",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    app = create_app(settings=settings, repository=InMemoryRepository())
    alice = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.org"}
    bob = {"x-goog-authenticated-user-email": "accounts.google.com:bob@example.org"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/projects",
            headers=alice,
            json={"slug": "api-project", "name": "API Project", "members": []},
        )
        assert response.status_code == 201
        project_id = response.json()["id"]
        assert client.get(f"/api/v1/projects/{project_id}", headers=alice).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}", headers=bob).status_code == 403


@pytest.mark.parametrize("auth_mode", ["dev", "iap", "oidc"])
def test_headless_service_has_no_browser_routes(tmp_path, auth_mode) -> None:
    settings = Settings(
        env="test",
        auth_mode=auth_mode,
        artifact_root=tmp_path,
        oidc_audience="https://pistachio.example",
    )
    app = create_app(settings=settings, repository=InMemoryRepository())
    with TestClient(app) as client:
        for path in (
            "/",
            "/?task=old-task",
            "/assets/pista_logo.svg",
            "/auth/login",
            "/auth/callback",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
        ):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 404, path
            assert "location" not in response.headers
            assert "set-cookie" not in response.headers
        assert client.post("/auth/logout").status_code == 404
        assert client.get("/api/v1/healthz").json() == {"status": "ok"}
        # The machine-readable schema remains available to API clients.
        assert "/api/v1/tasks" in client.get("/openapi.json").json()["paths"]


def test_oidc_api_auth_and_service_callbacks_survive_ui_removal(tmp_path, monkeypatch) -> None:
    settings = Settings(
        env="test",
        auth_mode="oidc",
        artifact_root=tmp_path,
        oidc_audience="https://pistachio.example",
        allowed_emails=("alice@example.org",),
        cloud_tasks_service_account="worker@example.iam.gserviceaccount.com",
        signed_link_secret="test-signing-secret",
        slack_agent_backend="claude_batch",
    )

    def verify(token, request, audience):
        assert audience == settings.oidc_audience
        if token == "invalid":
            raise ValueError("invalid token")
        return {"email": token}

    async def process(bridge, event_id, tick):
        assert event_id == "a" * 32 and tick == 0
        return "completed"

    monkeypatch.setattr("pistachio.api.auth.id_token.verify_oauth2_token", verify)
    monkeypatch.setattr("pistachio.api.slack_analysis.BatchBridge.process", process)
    app = create_app(settings=settings, repository=InMemoryRepository())
    with TestClient(app) as client:
        # Previously signed browser cookies no longer grant API access.
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "email": "alice@example.org",
                        "exp": int(time.time()) + 3600,
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(
            settings.signed_link_secret.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        client.cookies.set("pistachio_session", f"{encoded}.{signature}")
        assert client.get("/api/v1/projects").status_code == 401
        assert (
            client.get("/api/v1/projects", headers={"Authorization": "Bearer invalid"}).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/projects", headers={"Authorization": "Bearer bob@example.org"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/v1/projects", headers={"Authorization": "Bearer alice@example.org"}
            ).status_code
            == 200
        )
        # Internal service identity remains independent of the human email allowlist.
        response = client.post(
            f"/api/v1/internal/slack-analysis/process/{'a' * 32}/0",
            headers={
                "Authorization": f"Bearer {settings.cloud_tasks_service_account}",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"status": "completed"}


def test_task_context_and_cancel_controls_are_durable(tmp_path) -> None:
    settings = Settings(
        env="test",
        auth_mode="dev",
        dev_user_email="alice@example.org",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    repository = InMemoryRepository()
    project = asyncio.run(
        repository.create_project(
            Project(slug="interactive", name="Interactive", owner="alice@example.org")
        )
    )
    original = asyncio.run(
        repository.create_task(
            TaskRecord(
                envelope=TaskEnvelope(
                    project_id=project.id,
                    conversation_id="conv-interactive",
                    principal_id="alice@example.org",
                    prompt="Analyze the cohort.",
                ),
                status="PLANNING",
            )
        )
    )
    app = create_app(settings=settings, repository=repository)

    class RecordingDispatcher:
        task_ids: list[str] = []

        async def enqueue(self, task_id: str) -> None:
            self.task_ids.append(task_id)

    with TestClient(app) as client:
        dispatcher = RecordingDispatcher()
        client.app.state.services.dispatcher = dispatcher
        response = client.post(
            f"/api/v1/tasks/{original.id}/context",
            json={"message": "Only include treatment-naive patients."},
        )
        assert response.status_code == 202
        revised = response.json()
        assert revised["status"] == "RECEIVED"
        assert revised["interaction_revision"] == 1
        assert revised["context_updates"][0]["message"] == (
            "Only include treatment-naive patients."
        )
        assert "Additional user context" in revised["envelope"]["prompt"]
        assert dispatcher.task_ids == [original.id]

        cancelled = client.post(f"/api/v1/tasks/{original.id}/cancel").json()
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["interaction_revision"] == 2
        assert cancelled["cancel_requested_at"] is not None

    stale = original.model_copy(update={"status": "SUCCEEDED"})
    saved = asyncio.run(repository.update_task(stale))
    assert saved.status == "CANCELLED"


def test_memory_api_includes_friendly_task_name_and_artifact_names(tmp_path) -> None:
    settings = Settings(
        env="test",
        auth_mode="iap",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    repository = InMemoryRepository()
    project = asyncio.run(
        repository.create_project(
            Project(slug="memories", name="Memories", owner="alice@example.org")
        )
    )
    task = asyncio.run(
        repository.create_task(
            TaskRecord(
                envelope=TaskEnvelope(
                    project_id=project.id,
                    conversation_id="conv-memory",
                    principal_id="alice@example.org",
                    prompt="Create a plot.",
                ),
                result=TaskResult(
                    summary="Created the requested plot.",
                    artifacts=[ArtifactRef(name="plot.png", uri="gs://outputs/plot.png")],
                ),
            )
        )
    )
    asyncio.run(
        repository.put_memory(
            MemoryItem(
                project_id=project.id,
                kind=MemoryKind.RESULT_POINTER,
                content=f"Task {task.id} completed. Artifacts: plot.png.",
                source_task_id=task.id,
                provenance="completed task",
            )
        )
    )
    app = create_app(settings=settings, repository=repository)
    headers = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.org"}

    with TestClient(app) as client:
        response = client.get(f"/api/v1/projects/{project.id}/memories", headers=headers)

    assert response.status_code == 200
    assert response.json()[0]["source_task_name"] == task.public_name
    assert response.json()[0]["artifact_names"] == ["plot.png"]
    assert task.id not in response.json()[0]["source_task_name"]


def test_artifact_delivery_downloads_four_files_and_bundles_five(tmp_path) -> None:
    settings = Settings(
        env="test",
        auth_mode="iap",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    repository = InMemoryRepository()
    project = asyncio.run(
        repository.create_project(
            Project(slug="delivery", name="Delivery", owner="alice@example.org")
        )
    )

    def completed_task(count: int, *, include_code: bool = False) -> TaskRecord:
        artifacts = []
        for index in range(count):
            content = f"artifact {index + 1}".encode()
            path = tmp_path / f"output-{count}-{index + 1}.png"
            path.write_bytes(content)
            artifacts.append(
                ArtifactRef(
                    name=path.name,
                    uri=path.resolve().as_uri(),
                    media_type="image/png",
                    size_bytes=len(content),
                    checksum_sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        if include_code:
            code = b'print("analysis")\n'
            code_path = tmp_path / "generated_analysis.py"
            code_path.write_bytes(code)
            artifacts.append(
                ArtifactRef(
                    name=code_path.name,
                    uri=code_path.resolve().as_uri(),
                    media_type="text/x-python",
                    size_bytes=len(code),
                    checksum_sha256=hashlib.sha256(code).hexdigest(),
                )
            )
        return asyncio.run(
            repository.create_task(
                TaskRecord(
                    envelope=TaskEnvelope(
                        project_id=project.id,
                        conversation_id=f"conv-{count}-{include_code}",
                        principal_id="alice@example.org",
                        prompt="Create only the requested outputs.",
                    ),
                    result=TaskResult(summary="Done", artifacts=artifacts, exit_code=0),
                )
            )
        )

    four = completed_task(4)
    four_with_code = completed_task(4, include_code=True)
    five = completed_task(5)
    app = create_app(settings=settings, repository=repository)
    headers = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.org"}

    with TestClient(app) as client:
        individual_manifest = client.get(
            f"/api/v1/tasks/{four.id}/artifact-delivery", headers=headers
        )
        individual = client.get(f"/api/v1/tasks/{four.id}/artifacts/0/download", headers=headers)
        bundle_manifest = client.get(f"/api/v1/tasks/{five.id}/artifact-delivery", headers=headers)
        code_manifest = client.get(
            f"/api/v1/tasks/{four_with_code.id}/artifact-delivery", headers=headers
        )
        bundle = client.get(f"/api/v1/tasks/{five.id}/artifacts/archive", headers=headers)

    assert individual_manifest.status_code == 200
    assert len(individual_manifest.json()) == 4
    assert not any(item["bundled"] for item in individual_manifest.json())
    assert individual.content == b"artifact 1"
    assert individual.headers["content-type"].startswith("image/png")
    assert len(code_manifest.json()) == 5
    assert not any(item["bundled"] for item in code_manifest.json())
    assert code_manifest.json()[-1]["name"] == "generated_analysis.py"
    assert bundle_manifest.json() == [
        {
            "name": f"{five.public_name}-artifacts.zip",
            "media_type": "application/zip",
            "artifact_count": 5,
            "bundled": True,
            "download_url": f"/api/v1/tasks/{five.id}/artifacts/archive",
        }
    ]
    with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
        assert archive.namelist() == [f"output-5-{index}.png" for index in range(1, 6)]


def test_project_tasks_endpoint_lists_only_authorized_project_tasks(tmp_path) -> None:
    settings = Settings(
        env="test",
        auth_mode="iap",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    repository = InMemoryRepository()
    project = asyncio.run(
        repository.create_project(
            Project(slug="history", name="History", owner="alice@example.org")
        )
    )
    asyncio.run(
        repository.create_task(
            TaskRecord(
                envelope=TaskEnvelope(
                    project_id=project.id,
                    conversation_id="conv-history",
                    principal_id="alice@example.org",
                    prompt="Recover this task.",
                )
            )
        )
    )
    app = create_app(settings=settings, repository=repository)
    alice = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.org"}
    bob = {"x-goog-authenticated-user-email": "accounts.google.com:bob@example.org"}

    with TestClient(app) as client:
        response = client.get(f"/api/v1/projects/{project.id}/tasks", headers=alice)
        forbidden = client.get(f"/api/v1/projects/{project.id}/tasks", headers=bob)

    assert response.status_code == 200
    assert [item["prompt"] for item in response.json()] == ["Recover this task."]
    assert response.json()[0]["public_name"]
    assert "generated_script" not in response.json()[0]
    assert forbidden.status_code == 403


def test_plan_and_stage_detail_endpoints(tmp_path) -> None:
    settings = Settings(
        env="test",
        auth_mode="iap",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    repository = InMemoryRepository()
    project = asyncio.run(
        repository.create_project(
            Project(slug="details", name="Details", owner="alice@example.org")
        )
    )
    task = asyncio.run(
        repository.create_task(
            TaskRecord(
                envelope=TaskEnvelope(
                    project_id=project.id,
                    conversation_id="conv-details",
                    principal_id="alice@example.org",
                    prompt="Describe the existing plan.",
                ),
                planner_version=2,
            )
        )
    )
    app = create_app(settings=settings, repository=repository)
    headers = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.org"}
    with TestClient(app) as client:
        plan = client.get(f"/api/v1/tasks/{task.id}/plan", headers=headers)
        stages = client.get(f"/api/v1/tasks/{task.id}/stages", headers=headers)

    assert plan.status_code == 200
    assert plan.json()["planner_version"] == 2
    assert stages.status_code == 200
    assert stages.json() == {
        "planner_version": 2,
        "current_stage": None,
        "stages": [],
    }


def test_batch_completion_event_is_released_after_failed_processing(tmp_path, monkeypatch) -> None:
    settings = Settings(
        env="test",
        auth_mode="dev",
        dev_user_email="alice@example.org",
        store_backend="memory",
        execution_backend="local",
        artifact_root=tmp_path,
    )
    repository = InMemoryRepository()
    project = asyncio.run(
        repository.create_project(
            Project(slug="completion-retry", name="Completion retry", owner="alice@example.org")
        )
    )
    task = asyncio.run(
        repository.create_task(
            TaskRecord(
                envelope=TaskEnvelope(
                    project_id=project.id,
                    conversation_id="conv-completion-retry",
                    principal_id="alice@example.org",
                    prompt="Run the analysis.",
                ),
                status=TaskStatus.QUEUED,
                attempts=1,
            )
        )
    )
    result_payload = TaskResult(summary="Failed", exit_code=1).model_dump_json()

    class FakeBlob:
        def download_as_text(self) -> str:
            return result_payload

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            assert name.endswith("/attempt-1/worker-result.json")
            return FakeBlob()

    class FakeStorageClient:
        def __init__(self, project: str) -> None:
            del project

        def bucket(self, name: str) -> FakeBucket:
            del name
            return FakeBucket()

    class FlakyCompletionService:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_batch(self, task_id: str, attempt: int, result: TaskResult):
            assert task_id == task.id
            assert attempt == 1
            assert result.exit_code == 1
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary repair failure")
            return task.model_copy(update={"status": TaskStatus.FAILED})

    monkeypatch.setattr("google.cloud.storage.Client", FakeStorageClient)
    app = create_app(settings=settings, repository=repository)
    completion = FlakyCompletionService()
    encoded = base64.b64encode(
        json.dumps({"kind": "completion", "task_id": task.id, "attempt": 1}).encode()
    ).decode()
    payload = {"message": {"messageId": "completion-1", "data": encoded}}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.app.state.services.tasks = completion
        first = client.post("/api/v1/internal/batch/completion", json=payload)
        second = client.post("/api/v1/internal/batch/completion", json=payload)
        duplicate = client.post("/api/v1/internal/batch/completion", json=payload)

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json()["status"] == TaskStatus.FAILED.value
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    assert completion.calls == 2


def test_web_task_identity_is_always_server_assigned(tmp_path):
    settings = Settings(env="test", auth_mode="iap", artifact_root=tmp_path)
    repo = InMemoryRepository()
    app = create_app(settings=settings, repository=repo)
    headers = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.org"}
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", headers=headers, json={"slug": "forged", "name": "Forged"}
        ).json()
        body = {
            "project_id": project["id"],
            "prompt": "hello",
            "source": "slack",
            "conversation_id": "victim",
            "external_event_id": "process:victim",
        }
        first = client.post("/api/v1/tasks", headers=headers, json=body)
        second = client.post("/api/v1/tasks", headers=headers, json=body)
        assert first.status_code == second.status_code == 202
        a, b = first.json()["envelope"], second.json()["envelope"]
        assert a["source"] == b["source"] == "web"
        assert a["conversation_id"] != b["conversation_id"] != "victim"
        assert a["external_event_id"] != b["external_event_id"] != "process:victim"
        assert a["principal_id"] == "alice@example.org"


def test_deployed_dev_auth_is_rejected_and_allowlist_normalized():
    import pytest

    for env in ("staging", "production"):
        with pytest.raises(ValueError, match="dev authentication"):
            Settings(_env_file=None, env=env, auth_mode="dev")
    for values in ([" Alice@Example.org "], (" Alice@Example.org ",), " Alice@Example.org "):
        assert Settings(_env_file=None, allowed_emails=values).allowed_emails == (
            "alice@example.org",
        )


@pytest.mark.parametrize("substitutions", [{}, {"_TOOL_ID": ""}, {"_TOOL_ID": None}])
def test_deployment_build_notifications_are_ignored(tmp_path, monkeypatch, substitutions):
    from unittest.mock import AsyncMock

    repo = InMemoryRepository()
    lookup = AsyncMock(side_effect=AssertionError("Deployment is not a tool build"))
    monkeypatch.setattr(repo, "get_tool", lookup)
    app = create_app(
        settings=Settings(env="test", auth_mode="dev", artifact_root=tmp_path),
        repository=repo,
    )
    payload = {
        "message": {
            "messageId": "deployment-build",
            "data": base64.b64encode(
                json.dumps({"status": "SUCCESS", "substitutions": substitutions}).encode()
            ).decode(),
        }
    }
    with TestClient(app) as client:
        response = client.post("/api/v1/internal/tools/build-completion", json=payload)
        assert response.status_code == 200
        assert response.json() == {"status": "ignored"}
        assert client.post("/api/v1/internal/tools/build-completion", json=payload).json() == {
            "status": "duplicate"
        }
    lookup.assert_not_awaited()


def test_tool_build_notification_still_updates_tool(tmp_path):
    from pistachio.models import ToolRecord, ToolStatus

    repo = InMemoryRepository()
    tool = ToolRecord(
        name="fixture",
        source_url="https://example.org/tool",
        version="1",
        install_recipe="fixture",
        status=ToolStatus.BUILDING,
    )
    asyncio.run(repo.put_tool(tool))
    app = create_app(
        settings=Settings(env="test", auth_mode="dev", artifact_root=tmp_path),
        repository=repo,
    )
    payload = {
        "message": {
            "messageId": "tool-build",
            "data": base64.b64encode(
                json.dumps(
                    {
                        "status": "SUCCESS",
                        "substitutions": {"_TOOL_ID": tool.id},
                        "results": {"images": [{"digest": "sha256:fixture"}]},
                    }
                ).encode()
            ).decode(),
        }
    }
    with TestClient(app) as client:
        response = client.post("/api/v1/internal/tools/build-completion", json=payload)
    assert response.status_code == 200
    saved = asyncio.run(repo.get_tool(tool.id))
    assert saved.status == ToolStatus.AVAILABLE
    assert saved.image_digest == "sha256:fixture"
