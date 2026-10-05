"""Offline checks of the existing-webhook/Spot adapter and cold-start persistence."""

import io
import json
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import batch_v1
from starlette.requests import Request

from pistachio.agents.analysis import batch
from pistachio.agents.analysis.batch import BatchBridge, job_spec
from pistachio.agents.analysis.batch_worker import restore, snapshot
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import LocalStorage
from pistachio.api import slack_analysis


def payload(
    ts="1.0",
    thread=None,
    text="plot",
    channel="D1",
    *,
    user="U1",
    kind="message",
    channel_type="im",
):
    event = dict(type=kind, channel_type=channel_type, channel=channel, user=user, ts=ts, text=text)
    if thread:
        event["thread_ts"] = thread
    return dict(type="event_callback", team_id="T1", event_id=f"E{ts}", event=event)


@pytest.fixture
def bridge(monkeypatch, settings, repository, tmp_path):
    settings.slack_agent_backend = "claude_batch"
    settings.slack_agent_image = "us-docker.pkg.dev/project/images/slack@sha256:" + "a" * 64
    settings.gcp_project, settings.gcs_bucket = "existing-project", "existing-artifacts"
    settings.batch_service_account = "worker@example.com"
    settings.cloud_tasks_service_account = "control@example.com"
    settings.control_plane_url = "https://existing.run.app"
    settings.slack_user_map = {"T1:U1": "alice@example.com", "T1:U2": "bob@example.com"}
    agent_settings = Settings(
        _env_file=None,
        access_mode="mapped",
        memory_auto_edit=False,
        data_buckets="lab-data",
        storage="local",
    )
    monkeypatch.setattr(batch, "Settings", lambda: agent_settings)
    client = Mock()
    states = {}

    def create(**kw):
        name = f"{kw['parent']}/jobs/{kw['job_id']}"
        if name in states:
            raise AlreadyExists("already submitted")
        states[name] = "RUNNING"

    def get(name):
        if name not in states:
            raise NotFound("gone")
        return batch_v1.Job(status=batch_v1.JobStatus(state=states[name]))

    client.create_job.side_effect = create
    client.get_job.side_effect = get
    monkeypatch.setattr(batch.batch_v1, "BatchServiceClient", lambda: client)
    storage = LocalStorage(tmp_path / "storage")
    monkeypatch.setattr(batch, "GCSStorage", lambda bucket: storage)
    monkeypatch.setattr(slack_analysis, "GCSStorage", lambda bucket: storage)
    monkeypatch.setattr(BatchBridge, "schedule", AsyncMock())
    slack = AsyncMock()
    slack.chat_postMessage.return_value = {"ts": "9.0"}
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack)
    from analysis_helpers import seed_project

    from pistachio.agents.analysis import direct

    pid = seed_project(storage)
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(return_value=direct.Decision(route="compute", reply="Computation needed.")),
    )
    slack.conversations_replies.return_value = {"messages": []}
    result = BatchBridge(settings, repository)
    result.test_client, result.test_states, result.test_slack = client, states, slack
    result.store, result.pid = storage, pid
    return result


def test_job_reuses_spot_resources_and_has_no_credentials(bridge):
    bridge.app.batch_nested_sandbox_opt_in = True
    bridge.settings.loading_emoji = ":pistachio-loading:"
    job = job_spec(bridge.app, bridge.settings, "a" * 32)
    task = job.task_groups[0].task_spec
    assert task.max_retry_count == 0
    assert task.max_run_duration.seconds == 3600
    assert job.allocation_policy.instances[0].policy.provisioning_model.name == "SPOT"
    assert job.allocation_policy.service_account.email == "worker@example.com"
    assert job.allocation_policy.network.network_interfaces[0].no_external_ip_address
    assert task.runnables[0].container.volumes == [
        "/mnt/stateful_partition/gcs/lab-data:/mnt/gcs/lab-data:ro"
    ]
    assert "ro,allow_other" in task.volumes[0].mount_options
    assert "seccomp=unconfined" in task.runnables[0].container.options
    assert not any("KEY" in x or "TOKEN" in x for x in task.runnables[0].environment.variables)
    assert task.runnables[0].environment.variables["AGENT_LOADING_EMOJI"] == ":pistachio-loading:"
    bridge.app.env = "production"
    assert (
        not job_spec(bridge.app, bridge.settings, "a" * 32)
        .task_groups[0]
        .task_spec.runnables[0]
        .container.options
    )


def test_proc_exception_is_separate_and_staging_only(bridge):
    bridge.app.batch_nested_sandbox_opt_in = True
    assert not bridge.settings.batch_proc_mount_opt_in
    bridge.settings.batch_proc_mount_opt_in = True
    options = (
        job_spec(bridge.app, bridge.settings, "a" * 32)
        .task_groups[0]
        .task_spec.runnables[0]
        .container.options
    )
    assert "systempaths=unconfined" in options
    assert (
        "--privileged" not in options and "SYS_ADMIN" not in options and "--pid=host" not in options
    )
    bridge.app.env = "production"
    with pytest.raises(ValueError, match="staging"):
        job_spec(bridge.app, bridge.settings, "a" * 32)


async def test_dedup_serial_order_parallel_threads_and_polling(bridge):
    first = await bridge.accept(payload())
    duplicate = payload()
    duplicate.update(event_id="different-event-id")
    duplicate["event"]["type"] = "app_mention"
    assert await bridge.accept(duplicate) == first
    second = await bridge.accept(payload("2.0", "1.0"))
    other = await bridge.accept(payload("3.0"))
    assert await bridge.process(first, 0) == "running"
    assert await bridge.process(second, 0) == "pending"
    assert await bridge.process(other, 0) == "running"
    assert bridge.test_client.create_job.call_count == 2
    # Old deliveries do not re-execute; polls get new named tasks beyond the queue's 5 retries.
    assert await bridge.process(first, 0) == "running"
    for tick in range(1, 7):
        assert await bridge.process(first, tick) == "running"
    bridge.schedule.assert_any_await(first, 7)
    assert bridge.test_client.create_job.call_count == 2
    for name in bridge.test_states:
        if name.endswith(first):
            bridge.test_states[name] = "SUCCEEDED"
    assert await bridge.process(first, 7) == "complete"
    assert await bridge.process(second, 1) == "running"
    assert bridge.test_client.create_job.call_count == 3


async def test_intake_recovers_interrupted_receipt_write(bridge, monkeypatch):
    original = bridge.repository.set_connector_state
    failed = False

    async def fail_once(key, value):
        nonlocal failed
        if key.startswith("claude-event:") and not failed:
            failed = True
            raise RuntimeError("interrupted")
        await original(key, value)

    monkeypatch.setattr(bridge.repository, "set_connector_state", fail_once)
    with pytest.raises(RuntimeError, match="interrupted"):
        await bridge.accept(payload())
    second = await bridge.accept(payload("2.0", "1.0"))
    record = await bridge.repository.get_connector_state(f"claude-event:{second}")
    first = record["previous"]
    assert await bridge.repository.get_connector_state(f"claude-event:{first}")
    assert await bridge.process(second, 0) == "pending"
    bridge.schedule.assert_any_await(first, 0)


async def test_submission_retry_keeps_job_and_slot(bridge, monkeypatch):
    event_id = await bridge.accept(payload())
    original = bridge.repository.set_connector_state
    failed = False

    async def fail_once(key, record):
        nonlocal failed
        if record.get("status") == "running" and not failed:
            failed = True
            raise RuntimeError("lost response")
        await original(key, record)

    monkeypatch.setattr(bridge.repository, "set_connector_state", fail_once)
    with pytest.raises(RuntimeError, match="lost response"):
        await bridge.process(event_id, 0)
    assert await bridge.process(event_id, 0) == "running"
    assert len(bridge.test_states) == 1
    assert (
        len([key for key in bridge.repository.event_claims if key.startswith("claude-slot")]) == 1
    )


async def test_concurrency_limit_and_failed_job_release(bridge):
    ids = [await bridge.accept(payload(f"{n}.0")) for n in range(1, 5)]
    assert [await bridge.process(x, 0) for x in ids] == ["running"] * 3 + ["pending"]
    first_job = next(name for name in bridge.test_states if name.endswith(ids[0]))
    bridge.test_states[first_job] = "DELETION_IN_PROGRESS"
    assert await bridge.process(ids[0], 1) == "running"
    assert await bridge.process(ids[3], 1) == "pending"
    del bridge.test_states[first_job]
    bridge.test_slack.chat_postMessage.side_effect = RuntimeError("Slack unavailable")
    assert await bridge.process(ids[0], 2) == "failed"
    assert await bridge.process(ids[3], 2) == "running"


def test_checkpoint_roundtrip_and_traversal(tmp_path):
    home, restored = tmp_path / "source", tmp_path / "restored"
    for rel in (
        "work/liver/D1-1.0/code/plot.py",
        ".claude/projects/thread/session.jsonl",
        "state.db",
    ):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)
    (home / "work/link").symlink_to(home / "state.db")
    (home / ".claude/credentials.json").write_text("secret")
    archive = tmp_path / "checkpoint.tar.gz"
    snapshot(archive, home)
    restore(archive, restored)
    assert (restored / "work/liver/D1-1.0/code/plot.py").is_file()
    assert (restored / ".claude/projects/thread/session.jsonl").is_file()
    assert not (restored / ".claude/credentials.json").exists()
    assert not (restored / "work/link").exists()
    with tarfile.open(archive, "w:gz") as tar:
        entry = tarfile.TarInfo("work/../../outside")
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        restore(archive, restored)


async def test_existing_dm_routes_and_worker_credential_boundary(bridge, monkeypatch):
    services = SimpleNamespace(settings=bridge.app, repository=bridge.repository)
    from pydantic import SecretStr

    bridge.app.anthropic_api_key = SecretStr("test-key")
    bridge.app.slack_bot_token = "test-slack"
    result = await slack_analysis.receive(services, payload())
    event_id = result["event_id"]
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer identity")],
            "app": SimpleNamespace(state=SimpleNamespace(services=services)),
        }
    )
    verify = Mock(return_value={"email": "wrong@example.com", "email_verified": True})
    monkeypatch.setattr(slack_analysis.id_token, "verify_oauth2_token", verify)
    with pytest.raises(HTTPException) as error:
        await slack_analysis.credentials(event_id, request, Response())
    assert error.value.status_code == 403
    verify.return_value["email"] = bridge.app.batch_service_account
    with pytest.raises(HTTPException):
        await slack_analysis.credentials(event_id, request, Response())  # Not running yet.
    await bridge.process(event_id, 0)
    monkeypatch.setattr(slack_analysis, "GCSStorage", lambda bucket: bridge.store)
    response = Response()
    keys = await slack_analysis.credentials(event_id, request, response)
    assert keys == {"ANTHROPIC_API_KEY": "test-key", "SLACK_BOT_TOKEN": "test-slack"}
    assert response.headers["Cache-Control"] == "no-store"
    bridge.app.slack_agent_backend = "existing"
    with pytest.raises(HTTPException) as error:
        await slack_analysis.credentials(event_id, request, Response())
    assert error.value.status_code == 503


async def test_untracked_channel_messages_are_ignored(bridge):
    services = SimpleNamespace(settings=bridge.app, repository=bridge.repository)
    body = payload()
    body["event"].update(channel_type="channel", channel="C1", thread_ts="0.0")
    assert await slack_analysis.receive(services, body) == {"ok": True}
    assert not bridge.repository.connector_state


@pytest.mark.parametrize("direct_revision", [False, True])
async def test_worker_cold_start_resume_and_expired_checkpoint(
    tmp_path, monkeypatch, direct_revision
):
    from dataclasses import asdict

    import httpx
    from analysis_helpers import seed_project
    from test_analysis_slack import FakeRunner

    from pistachio.agents.analysis import batch_worker
    from pistachio.agents.analysis.config import CHECKPOINT_PREFIX
    from pistachio.agents.analysis.runner import Context

    home = tmp_path / "worker"
    home.mkdir()
    settings = Settings(
        _env_file=None,
        storage="local",
        access_mode="mapped",
        data_buckets="lab-data",
        anthropic_api_key="fake",
        slack_bot_token="fake",
        work_root=home / "work",
        state_db=home / "state.db",
    )
    storage = LocalStorage(tmp_path / "gcs")

    class Blob:
        def __init__(self, key):
            self.key = key

        def download_to_filename(self, target):
            try:
                shutil.copyfile(storage.path(self.key), target)
            except FileNotFoundError as exc:
                raise NotFound("expired") from exc

    storage.bucket = SimpleNamespace(blob=Blob)
    pid = seed_project(storage, brief=b"Frozen original brief")
    runner = FakeRunner()
    slack = AsyncMock()
    slack.auth_test.return_value = {"user_id": "BOT"}
    slack.chat_postMessage.return_value = {"ts": "9.0"}
    slack.users_info.return_value = {"user": {"real_name": "Alice"}}
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.get.return_value = httpx.Response(
        200, json={}, request=httpx.Request("GET", "https://test")
    )
    monkeypatch.setattr(batch_worker, "Settings", lambda **kw: settings)
    monkeypatch.setattr(batch_worker, "GCSStorage", lambda bucket: storage)
    monkeypatch.setattr(batch_worker, "check", lambda settings: None)
    monkeypatch.setattr(batch_worker, "Runner", lambda settings, **kwargs: runner)
    monkeypatch.setattr(batch_worker, "AsyncWebClient", lambda **kw: slack)
    monkeypatch.setattr(batch_worker.id_token, "fetch_id_token", lambda *args: "identity")
    monkeypatch.setattr(batch_worker.httpx, "AsyncClient", lambda **kw: http)
    monkeypatch.setenv("PISTACHIO_CONTROL_PLANE_URL", "https://existing.run.app")
    pointer = "projects/_threads/thread/checkpoint.json"
    ctx = Context(
        home / f"work/{pid}/D1-1.0",
        Path("/mnt/gcs/lab-data/liver"),
        pid,
        "Frozen original brief",
        thread_ts="1.0",
    )
    storage.put(
        pointer,
        json.dumps({"context": asdict(ctx), "turn": 0, "is_init": False}, default=str).encode(),
    )
    from pistachio.agents.analysis import access
    from pistachio.agents.analysis.registry import Registry

    grant = access.Grant(
        "alice@example.com", True, {}, Registry(storage).load(), pid, True, "T1:D1"
    )
    ctx.workdir = Path(access.private_workdir(settings, grant, "thread", 0))
    storage.put(
        pointer,
        json.dumps({"context": asdict(ctx), "turn": 0, "is_init": False}, default=str).encode(),
    )
    for turn in range(1, 4):
        storage.put(
            f"{CHECKPOINT_PREFIX}/events/event-{turn}/request.json",
            json.dumps(
                {
                    "payload": payload(f"{turn}.0", "1.0" if turn > 1 else None),
                    "thread_key": "thread",
                    "protocol": 2,
                    "principal": "alice@example.com",
                    "project_id": pid,
                    "private": True,
                    "access_signature": grant.signature,
                }
            ).encode(),
        )
        await batch_worker.main(f"event-{turn}")
        saved = json.loads(storage.get(pointer)[0])
        assert saved["turn"] == turn
        expected = "Revised in chat" if direct_revision and turn > 1 else "Frozen original brief"
        assert saved["context"]["project_md"] == expected
        if direct_revision and turn == 1:
            saved["context"]["project_md"] = "Revised in chat"
            saved["messages"].append({"user": "Use sample B", "assistant": "Noted."})
            storage.put(pointer, json.dumps(saved).encode())
        if direct_revision and turn > 1:
            assert "Use sample B" in runner.calls[-1][2]
            assert (
                Path(saved["context"]["workdir"]) / "outputs/PROJECT.md"
            ).read_text() == expected
        from pistachio.agents.analysis.registry import read_brief, write_brief

        write_brief(storage, pid, b"Changed brief for future threads", read_brief(storage, pid)[1])
        shutil.rmtree(home)  # End this disposable worker; preserve only GCS.
        home.mkdir()
        if turn == 2:
            storage.path(saved["archive"]).unlink()  # Lifecycle expiration.
    assert [call[0] for call in runner.calls] == [None, "session", None]
    assert "Earlier context is unavailable" in runner.calls[2][2]
    assert len({call[3] for call in runner.calls}) == 1  # Stable cwd across all jobs.


async def test_receipt_ack_precedes_worker_and_deduplicates(bridge):
    seen = []
    create = bridge.test_client.create_job.side_effect

    def submit(**kwargs):
        assert bridge.test_slack.chat_postMessage.await_count == 1
        seen.append(kwargs["job"])
        return create(**kwargs)

    bridge.test_client.create_job.side_effect = submit
    first = await bridge.accept(payload(text="plot data"))
    assert await bridge.process(first, 0) == "running"
    assert await bridge.process(first, 0) == "running"
    assert bridge.test_slack.chat_postMessage.await_count == 1
    record = await bridge.repository.get_connector_state(f"claude-event:{first}")
    assert record["notice_ts"] == "9.0"
    assert seen[0].task_groups[0].task_spec.runnables[0].container.volumes == [
        "/mnt/stateful_partition/gcs/lab-data:/mnt/gcs/lab-data:ro"
    ]
    job = next(iter(bridge.test_states))
    bridge.test_states[job] = "SCHEDULED"
    record["created"] -= 130
    await bridge.repository.set_connector_state(f"claude-event:{first}", record)
    await bridge.process(first, 1)
    assert "2 min so far" in bridge.test_slack.chat_update.call_args.kwargs["text"]


async def test_notify_status_blocks_are_removed_for_final_reply(bridge):
    from pistachio.agents.analysis.slack_format import status_blocks

    record = {"payload": payload()}
    await bridge.notify("status-test", record, "starting", "Spinning up…")
    posted = bridge.test_slack.chat_postMessage.call_args.kwargs
    assert posted["blocks"] == status_blocks(bridge.settings.loading_emoji, "Spinning up…")
    await bridge.notify("status-test", record, "starting", "Getting set up…")
    assert "blocks" in bridge.test_slack.chat_update.call_args.kwargs
    await bridge.notify("status-test", record, "complete", "Here's the answer.")
    assert bridge.test_slack.chat_update.call_args.kwargs == {
        "channel": "D1",
        "ts": "9.0",
        "text": "Here's the answer.",
    }


async def test_dynamic_project_buckets_and_outside_project_rejection(bridge, monkeypatch):
    from google.cloud import storage

    catalog = Mock(
        return_value=[SimpleNamespace(name="lab-data"), SimpleNamespace(name="new-data")]
    )
    monkeypatch.setattr(storage, "Client", lambda: SimpleNamespace(list_buckets=catalog))
    bridge.settings.data_project = "existing-project"
    first = await bridge.accept(
        payload(
            text=(
                "init new-project <https://console.cloud.google.com/storage/browser/"
                "new-data/folder?project=existing-project|data> --force"
            )
        )
    )
    from pistachio.agents.analysis import direct

    direct.decide.return_value = direct.Decision(route="reply", reply="Draft.", draft="New brief")
    assert await bridge.process(first, 0) == "complete"
    direct.decide.return_value = direct.Decision(route="compute", reply="Plotting.")
    compute = await bridge.accept(payload("1.1", "1.0", "plot"))
    assert await bridge.process(compute, 0) == "running"
    catalog.assert_called_with(project="existing-project")
    job = bridge.test_client.create_job.call_args.kwargs["job"]
    container = job.task_groups[0].task_spec.runnables[0].container
    assert container.volumes == ["/mnt/stateful_partition/gcs/new-data:/mnt/gcs/new-data:ro"]
    assert (
        job.task_groups[0].task_spec.runnables[0].environment.variables["AGENT_DATA_BUCKETS"]
        == "new-data"
    )
    other = await bridge.accept(payload("2.0", text="init other-project gs://outside-project/"))
    assert await bridge.process(other, 0) == "failed"
    assert bridge.test_client.create_job.call_count == 1
    assert "outside-project" in bridge.test_slack.chat_update.call_args.kwargs["text"]


async def test_frozen_thread_bucket_survives_channel_rebind(bridge):
    event_id = await bridge.accept(payload("2.0", "1.0"))
    record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    store = batch.GCSStorage("unused")
    store.put(
        f"projects/_threads/{record['thread_key']}/checkpoint.json",
        json.dumps(
            {
                "context": {"data_path": "/mnt/gcs/lab-data/original/"},
            }
        ).encode(),
    )
    settings, mounts = bridge.resolve_data(record)
    assert mounts == {"lab-data"}
    assert settings.data_buckets == "lab-data"


async def test_status_api_failure_does_not_block_worker(bridge):
    bridge.test_slack.chat_postMessage.side_effect = RuntimeError("Slack temporarily unavailable")
    event_id = await bridge.accept(payload())
    assert await bridge.process(event_id, 0) == "running"
    assert bridge.test_client.create_job.call_count == 1
