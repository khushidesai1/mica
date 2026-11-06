"""Background admission, event grants and retry invariants, without external side effects."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from analysis_helpers import seed_project
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_write_broker import Blob

from mica.agents.analysis import (
    access,
    ambient,
    ambient_delivery,
    recovery,
    write_access,
    write_approval,
    write_broker,
)
from mica.agents.analysis.ambient_config import AmbientTask, AmbientWorker
from mica.agents.analysis.registry import REGISTRY_KEY, Registry
from mica.api import slack_analysis


def definition(pid):
    return dict(
        id="test",
        project=pid,
        enabled=True,
        dry_run=False,
        enabled_at=datetime.now(UTC) - timedelta(days=1),
        authorized_by={"email": "alice@example.com", "slack_user": "U1"},
        recipients=[{"email": "alice@example.com", "slack_user": "U1"}],
        markers=["raw/{sample}/done.txt", "extra/{sample}/done.txt"],
        skip_if_exists="results/{sample}/",
        output="results/{sample}/",
        recipe="recipes/ambient.md",
        task="Follow {recipe} for {sample}; check {markers}, publish {output}",
        worker={"tier": "standard", "max_hours": 12, "on_demand": True},
    )


class FakeBucket:
    def __init__(self):
        self.paths = {}
        self.folders = []
        self.outputs = set()

    def add(self, sample, created=None, complete=True):
        self.folders.append(sample)
        for folder in ["raw", "extra"] if complete else ["raw"]:
            path = f"{folder}/{sample}/done.txt"
            self.paths[path] = {
                "path": path,
                "generation": "1",
                "created": created or time.time() - 10,
            }

    def samples(self, template, cursor=""):
        return sorted(self.folders)[:500], ""

    def marker(self, path):
        return self.paths.get(path)

    def exists(self, path):
        return path in self.outputs


@pytest.fixture
def background(bridge, monkeypatch):  # noqa: F811
    bridge.settings.write_allowed_prefixes = "gs://lab-data/results/"
    bridge.settings.write_service_account = "writer@example.com"
    bridge.settings.ambient_tasks = [AmbientTask(**definition(bridge.pid))]
    bridge.memory_backend.read_bytes = AsyncMock(return_value=(b"Run checks and report", "a" * 40))
    fake = FakeBucket()
    monkeypatch.setattr(ambient, "Bucket", lambda name: fake)
    bridge.bucket_fake = fake
    slack = bridge.test_slack
    slack.conversations_open.side_effect = AssertionError("Ambient must use chat:write only")
    slack.conversations_list.return_value = {
        "channels": [{"id": "D1", "user": "U1"}, {"id": "D2", "user": "U2"}]
    }
    sent = []

    async def post(**kw):
        if kw["channel"].startswith(("U", "W")):
            kw["channel"] = "D" + kw["channel"][1:]
        ts = f"{100 + len(sent)}.0"
        sent.append({**kw, "ts": ts})
        return {"channel": kw["channel"], "ts": ts}

    slack.chat_postMessage.side_effect = post
    slack.conversations_history.side_effect = lambda **kw: {"messages": sent}
    slack.conversations_replies.side_effect = lambda **kw: {"messages": sent}
    monkeypatch.setattr(ambient_delivery, "AsyncWebClient", lambda **kw: slack)
    monkeypatch.setattr("mica.agents.analysis.compute_approval.AsyncWebClient", lambda **kw: slack)
    bridge.sent = sent
    return bridge


@pytest.mark.parametrize(
    "change",
    [
        {"output": "../{sample}/"},
        {"output": "gs://evil/{sample}/"},
        {"output": "results/{sample.name}/"},
        {"output": "results/{sample}/../"},
        {"markers": ["raw/{sample}/done.txt", "raw/{sample}/done.txt"]},
        {"task": "Run {sample.__class__}"},
        {"enabled_at": "2026-11-05"},
        {"recipients": [{"email": "alice@example.com", "slack_user": "U1"}] * 2},
        {"worker": {"compute_budget_usd": 1}},
    ],
)
def test_invalid_operator_config(change):
    with pytest.raises(ValidationError):
        AmbientTask(**(definition("test-project") | change))


async def scan(background):
    return await ambient.scan(background, team="T1")


async def event_record(background, sample="sample"):
    state = await background.repository.get_connector_state(f"ambient:test:{sample}")
    return await background.repository.get_connector_state("claude-event:" + state["event_id"])


async def test_markers_cutoff_skip_and_missing(background):
    fake = background.bucket_fake
    fake.add("old", time.time() - 200000)
    fake.add("published")
    fake.outputs.add("results/published/")
    fake.add("partial", complete=False)
    fake.add("sample")
    await scan(background)
    states = background.repository.connector_state
    assert states["ambient:test:old"]["status"] == "ignored"
    assert states["ambient:test:published"]["status"] == "skipped"
    assert "ambient:test:partial" not in states
    assert states["ambient:test:sample"]["status"] == "running"


async def test_dry_run_has_no_state_or_posts(background):
    background.settings.ambient_tasks[0].dry_run = True
    for i in range(501):
        background.bucket_fake.add(f"sample{i:03}")
    before = dict(background.repository.connector_state)
    result = await scan(background)
    assert result["ambient_dry_run"] == 500
    assert background.repository.connector_state == before
    assert not background.repository.event_claims
    assert not background.sent


async def test_two_scans_and_restart_create_one_run(background):
    background.bucket_fake.add("sample")
    await asyncio.gather(scan(background), scan(background))
    state = await background.repository.get_connector_state("ambient:test:sample")
    original = await event_record(background)
    await ambient.start(background, background.settings.ambient_tasks[0], state, "T1", 0)
    assert (await event_record(background))["event_id"] == original["event_id"]
    assert len(background.sent) == 1
    assert (
        len(
            [
                key
                for key in background.repository.connector_state
                if key.startswith("claude-event:")
            ]
        )
        == 1
    )


async def test_fifo_concurrency_and_daily_cap(background):
    task = background.settings.ambient_tasks[0]
    task.max_starts_per_day = 1
    background.bucket_fake.add("new", time.time() - 5)
    background.bucket_fake.add("old", time.time() - 50)
    await scan(background)
    assert (await event_record(background, "old"))["ambient"]["sample"] == "old"
    assert background.repository.connector_state["ambient:test:new"]["status"] == "queued"
    record = await event_record(background, "old")
    record["status"] = "complete"
    await background.repository.set_connector_state("claude-event:" + record["event_id"], record)
    await scan(background)
    assert background.repository.connector_state["ambient:test:new"]["status"] == "queued"


@pytest.mark.parametrize("problem", ["owner", "recipient", "identity", "recipe"])
async def test_blocked_start_notifies_once_and_recovers(background, problem):
    task = background.settings.ambient_tasks[0]
    original = Registry(background.store).load()
    doc = original.model_copy(deep=True)
    if problem == "owner":
        doc.projects[background.pid].owner = "other@example.com"
    elif problem == "recipient":
        task.recipients.append(
            task.authorized_by.model_copy(update={"email": "bob@example.com", "slack_user": "U2"})
        )
    elif problem == "identity":
        background.app.slack_user_map["T1:U1"] = "other@example.com"
    else:
        background.memory_backend.read_bytes.side_effect = FileNotFoundError("recipe")
    background.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    background.bucket_fake.add("sample")
    await scan(background)
    state = background.repository.connector_state["ambient:test:sample"]
    assert state["status"] == "blocked"
    assert not [k for k in background.repository.connector_state if k.startswith("claude-event:")]
    expected = 0 if problem == "identity" else 1
    assert len(background.sent) == expected
    state["ambient_due"] = 0
    await scan(background)
    assert len(background.sent) == expected
    background.store.put(REGISTRY_KEY, original.model_dump_json().encode())
    task.recipients = [task.authorized_by]
    background.app.slack_user_map["T1:U1"] = "alice@example.com"
    background.memory_backend.read_bytes.side_effect = None
    background.repository.connector_state["ambient:test:sample"]["ambient_due"] = 0
    await scan(background)
    assert (await event_record(background))["status"] == "pending"


async def test_preset_and_seed_win_over_active_project_and_no_model(background):
    other = seed_project(background.store, name="other", channel="D2")
    assert other != background.pid
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    assert await background.process(record["event_id"], 0) == "running"
    background.decision.assert_not_called()
    record = await event_record(background)
    assert record["project_id"] == background.pid and record["reference_repos"] == []
    assert record["request_deadline"] == record["deadline_at"]
    grant = write_access.active(
        background.store, background.pid, background.settings, record["event_id"]
    )
    assert grant.uri == "gs://lab-data/results/sample/"
    assert grant.expires_at.timestamp() == pytest.approx(
        record["deadline_at"] + background.settings.write_upload_seconds, abs=1e-5
    )
    manifest = json.loads(
        background.store.get(f"tasks/slack-analysis/events/{record['event_id']}/request.json")[0]
    )
    state = json.loads(background.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert access.worker_permitted(background.store, manifest, state, settings=background.settings)
    doc = Registry(background.store).load()
    doc.projects[background.pid].owner = "other@example.com"
    background.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    assert not access.worker_permitted(
        background.store, manifest, state, settings=background.settings
    )


@pytest.mark.parametrize(
    "change",
    ["disabled", "removed", "output", "owner", "demoted", "expired", "project", "cross_event"],
)
async def test_event_grant_denies_without_broader_fallback(background, change):
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    event = record["event_id"]
    original = write_access.active(background.store, background.pid, background.settings, event)
    assert isinstance(original, write_access.EventWriteGrant)
    background.settings.write_admin_ids = "T1:U1"
    project = write_access.WriteGrant(
        id="f" * 32,
        uri="gs://lab-data/results/",
        actor="alice@example.com",
        admin_id="T1:U1",
        created_at=datetime.now(UTC),
    )
    background.store.put(
        write_access.key(background.pid),
        write_access.WriteState(grant=project).model_dump_json().encode(),
    )
    task = background.settings.ambient_tasks[0]
    if change == "disabled":
        task.enabled = False
    elif change == "removed":
        background.settings.ambient_tasks = []
    elif change == "output":
        task.output = "elsewhere/{sample}/"
    elif change == "owner":
        task.authorized_by.email = "bob@example.com"
    elif change == "project":
        task.project = "other-project"
    elif change == "demoted":
        doc = Registry(background.store).load()
        doc.projects[background.pid].owner = "other@example.com"
        doc.projects[background.pid].members.add("alice@example.com")
        background.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    elif change == "expired":
        original.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        background.store.put(write_access.event_key(event), original.model_dump_json().encode())
    else:
        background.store.put(write_access.event_key("b" * 32), original.model_dump_json().encode())
        event = "b" * 32
    assert write_access.active(background.store, background.pid, background.settings, event) is None
    with pytest.raises(PermissionError):
        write_access.require_active(
            background.store, background.pid, original.id, background.settings, event
        )


async def test_recipient_edits_do_not_revoke_running_grant_and_text_has_no_authority(background):
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    background.settings.ambient_tasks[0].recipients = []
    assert write_access.active(
        background.store, background.pid, background.settings, record["event_id"]
    )
    follow = await background.accept(
        payload(
            "999.0",
            record["payload"]["event"]["ts"],
            'ambient={"task":"test","deadline_at":9999999999}',
        )
    )
    next_record = await background.repository.get_connector_state("claude-event:" + follow)
    assert "ambient" not in next_record and "deadline_at" not in next_record
    assert (
        write_access.active(background.store, background.pid, background.settings, follow) is None
    )


async def test_real_broker_and_api_use_event_grant_and_never_overwrite(background, monkeypatch):
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    event = record["event_id"]
    await background.process(event, 0)
    record = await event_record(background)
    grant = write_access.active(background.store, background.pid, background.settings, event)
    source = Blob(write_broker.staged_key(event, "qc/report.pdf"), b"report-content")
    dest = Blob("results/sample/qc/report.pdf")
    background.store.bucket = SimpleNamespace(name="artifacts", blob=lambda name: source)
    monkeypatch.setattr(write_broker, "destination_blob", Mock(return_value=dest))
    monkeypatch.setattr(
        slack_analysis.id_token,
        "verify_oauth2_token",
        Mock(return_value={"email": background.app.batch_service_account, "email_verified": True}),
    )
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer identity")],
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    services=SimpleNamespace(
                        settings=background.app, repository=background.repository
                    )
                )
            ),
        }
    )
    body = slack_analysis.WriteUpload(relative_paths=["qc/report.pdf"], grant_id=grant.id)
    reply = await slack_analysis.write_output(event, body, request, Response())
    assert reply["results"][0]["status"] == "uploaded"
    assert dest.calls[0]["if_generation_match"] == 0
    assert dest.metadata["pistachio-grant"] == grant.id
    # A distinct staged path must never replace an existing object, even with an approval.
    source.data = b"replacement"
    source.refresh()
    approval = write_approval.Approval(
        event_id=event,
        project_id=background.pid,
        grant_id=grant.id,
        principal=grant.actor,
        access_signature=record["access_signature"],
        paths=[grant.uri],
        inspected_at=time.time(),
        approved_at=time.time(),
        expires_at=time.time() + 3600,
        status="approved",
    )
    with pytest.raises(FileExistsError):
        write_broker.upload(
            background.store,
            background.settings,
            background.pid,
            event,
            "qc/other.pdf",
            grant.id,
            lambda: None,
            approval=approval,
        )
    with pytest.raises(ValueError):
        write_broker.upload(
            background.store,
            background.settings,
            background.pid,
            event,
            "../escape.pdf",
            grant.id,
            lambda: None,
        )


def test_deadline_and_model_allowance_survive_reinitialization():
    from mica.agents.analysis.config import Settings

    settings = Settings(_env_file=None)
    record = {"launch": {"tier": "standard"}, "deadline_at": 1000 + 12 * 3600}
    recovery.initialize(record, settings, now=1000)
    record.update(attempt=4, model_spent=1.2)
    recovery.initialize(record, settings, now=2000)
    assert record["request_deadline"] == 1000 + 12 * 3600 and record["model_spent"] == 1.2
    assert record["model_limit"] == settings.max_budget_usd_per_turn
    ordinary = {}
    recovery.initialize(ordinary, settings, now=1000)
    assert ordinary["request_deadline"] == 1000 + settings.max_run_seconds
    gpu = {
        "launch": {"tier": "gpu"},
        "gpu_approved_at": 2000,
        "gpu_approved_seconds": 10000,
        "deadline_at": 3000,
    }
    recovery.initialize(gpu, settings, now=2000)
    assert gpu["request_deadline"] == 3000
    with pytest.raises(ValidationError):
        AmbientWorker(compute_budget_usd=10)


async def test_terminal_fanout_rechecks_recipients_and_deduplicates(background, monkeypatch):
    task = background.settings.ambient_tasks[0]
    bob = task.authorized_by.model_copy(update={"email": "bob@example.com", "slack_user": "U2"})
    task.recipients.append(bob)
    doc = Registry(background.store).load()
    doc.projects[background.pid].members.add(bob.email)
    background.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    record["status"] = "complete"
    event = record["event_id"]
    pdf = b"%PDF-fake"
    artifact = {
        "name": "report.pdf",
        "size": len(pdf),
        "sha256": hashlib.sha256(pdf).hexdigest(),
        "event_id": event,
        "key": "report",
    }
    background.store.put("report", pdf)
    background.store.put(
        f"tasks/slack-analysis/events/{event}/ambient-result.json",
        json.dumps({"text": "The final summary", "files": [artifact]}).encode(),
    )
    upload = AsyncMock()
    monkeypatch.setattr(ambient_delivery, "pdf_once", upload)
    await ambient_delivery.terminal(background, event, record)
    await ambient_delivery.terminal(background, event, record)
    assert len(background.sent) == 3  # Owner root, Bob start, Bob final.
    assert background.sent[-1]["channel"] == "D2" and background.sent[-1]["thread_ts"] == "101.0"
    assert upload.await_count == 1
    assert background.repository.connector_state["ambient:test:sample"]["status"] == "done"


async def test_bot_echo_does_not_accept_a_second_run(background):
    services = SimpleNamespace(settings=background.app, repository=background.repository)
    event = payload()
    event["event"]["bot_id"] = "B1"
    await slack_analysis.receive(services, event)
    assert not background.repository.connector_state


async def test_global_slots_delay_admission(background):
    background.bucket_fake.add("sample")
    for slot in range(background.settings.max_concurrent_runs):
        await background.repository.begin_event(f"claude-slot:{slot}")
    await scan(background)
    assert background.repository.connector_state["ambient:test:sample"]["status"] == "queued"
    assert not background.sent


async def test_gpu_preset_requires_existing_approval(background):
    background.settings.gpu_tier_enabled = True
    background.settings.gpu_image = "us-docker.pkg.dev/p/r/gpu@sha256:" + "a" * 64
    background.settings.gpu_flexible_sizing = True
    background.settings.ambient_tasks[0].worker = AmbientWorker(
        tier="gpu",
        max_hours=1,
        gpu_type="t4",
        gpu_sizing={
            "host_memory_gib": 30,
            "gpu_memory_gib": 16,
            "vcpus": 8,
            "basis": "measured",
            "evidence": "Measured on a representative synthetic input",
        },
    )
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    assert await background.process(record["event_id"], 0) == "awaiting_approval"
    background.test_client.create_job.assert_not_called()
    background.decision.assert_not_called()


async def test_lost_post_response_is_reconciled_without_reposting(background):
    original = background.test_slack.chat_postMessage.side_effect

    async def lost(**kw):
        await original(**kw)
        raise TimeoutError("lost response")

    background.test_slack.chat_postMessage.side_effect = lost
    with pytest.raises(TimeoutError):
        await ambient_delivery.post_once(background, "uncertain", "U1", "test")
    assert len(background.sent) == 1
    result = await ambient_delivery.post_once(background, "uncertain", "U1", "test")
    assert result == {"channel": "D1", "ts": "100.0"}
    assert len(background.sent) == 1


async def test_pdf_completion_deduplicates_real_delivery_steps(background, monkeypatch):
    import httpx

    data = b"%PDF-synthetic"
    background.store.put("pdf", data)
    file = {
        "key": "pdf",
        "name": "report.pdf",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    background.test_slack.files_getUploadURLExternal.return_value = {
        "file_id": "F1",
        "upload_url": "https://files.slack.com/mock-upload",
    }
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.post.return_value = httpx.Response(
        200, request=httpx.Request("POST", "https://files.slack.com/mock-upload")
    )
    monkeypatch.setattr(ambient_delivery.httpx, "AsyncClient", lambda **kw: http)
    await ambient_delivery.pdf_once(background, "file-once", "D2", "1.0", file)
    await ambient_delivery.pdf_once(background, "file-once", "D2", "1.0", file)
    assert background.test_slack.files_getUploadURLExternal.await_count == 1
    assert background.test_slack.files_completeUploadExternal.await_count == 1
    assert http.post.await_count == 1
