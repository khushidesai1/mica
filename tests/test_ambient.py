"""Background admission, event grants and retry invariants, without external side effects."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
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
    conversation,
    identity,
    recovery,
    tiers,
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


def receipt_key(background, sample="sample"):
    return background.repository.connector_state[f"ambient-sample:test:{sample}"]["key"]


async def event_record(background, sample="sample"):
    state = await background.repository.get_connector_state(receipt_key(background, sample))
    return await background.repository.get_connector_state("claude-event:" + state["event_id"])


def replace_markers(background, generation="2", created=None):
    for path, marker in list(background.bucket_fake.paths.items()):
        background.bucket_fake.paths[path] = {
            **marker, "generation": generation, "created": created or time.time() - 1,
        }


async def test_replaced_markers_create_linked_arrival_with_memory_and_previous_decision(background):
    background.bucket_fake.add("sample")
    background.decision.return_value = conversation.Decision(
        route="reply", reply="Already processed."
    )
    await scan(background)
    first = receipt_key(background)
    event = await event_record(background)
    assert await background.process(event["event_id"], 0) == "complete"
    replace_markers(background)
    await scan(background)
    second = receipt_key(background)
    assert second != first
    arrival = await ambient.latest_arrival(background, "test", "sample")
    assert arrival["previous"] == first
    assert arrival["earlier_arrivals"][0]["decision"] == {
        "route": "reply", "reason": "Already processed.",
    }
    event = await event_record(background)
    assert "arrived again" in background.sent[-1]["text"]
    assert "Already processed." in event["payload"]["event"]["text"]
    assert await background.process(event["event_id"], 0) == "complete"
    context = background.decision.call_args.args[5]
    assert "project_memory" in context and "memory_paths" in context["project_memory"]
    assert "Already processed." in context["request"]
    assert (await scan(background))["ambient_queued"] == 0
    assert receipt_key(background) == second
    assert len([k for k in background.repository.connector_state
                if k.startswith("ambient-arrival:")]) == 2


@pytest.mark.parametrize("status", ["done", "ignored"])
async def test_legacy_receipts_deduplicate_generations_and_allow_new_arrival(background, status):
    background.bucket_fake.add("sample")
    key = "ambient:test:sample"
    legacy = {
        "key": key, "task": "test", "project": background.pid, "sample": "sample",
        "status": status, "markers": list(background.bucket_fake.paths.values()),
        "marker_at": time.time() - 10,
    }
    await background.repository.create_connector_state(key, legacy)
    assert (await scan(background))["ambient_queued"] == 0
    assert not background.sent
    replace_markers(background)
    assert (await scan(background))["ambient_queued"] == 1
    assert (await ambient.latest_arrival(background, "test", "sample"))["previous"] == key
    assert background.repository.connector_state[key] == legacy
    assert (await scan(background))["ambient_queued"] == 0


async def test_cutoff_is_per_arrival_and_output_is_reported_not_skipped(background):
    background.bucket_fake.add("sample", time.time() - 200000)
    background.bucket_fake.outputs.add("results/sample/")
    await scan(background)
    first = receipt_key(background)
    assert background.repository.connector_state[first]["status"] == "ignored"
    replace_markers(background)
    await scan(background)
    event = await event_record(background)
    assert '"output_exists": true' in event["payload"]["event"]["text"]
    assert (await ambient.latest_arrival(background, "test", "sample"))["previous"] == first
    assert await background.process(event["event_id"], 0) == "running"


async def test_changed_queued_markers_supersede_without_block_or_quota(background):
    background.bucket_fake.add("sample")
    leases = [await background.repository.begin_event(f"claude-slot:{i}")
              for i in range(background.settings.max_concurrent_runs)]
    await scan(background)
    first = receipt_key(background)
    replace_markers(background)
    await scan(background)
    second = receipt_key(background)
    assert first != second
    assert background.repository.connector_state[first]["status"] == "superseded"
    assert "ambient_due" not in background.repository.connector_state[first]
    assert background.repository.connector_state[second]["status"] == "queued"
    assert not background.sent
    assert not background.repository.connector_state["ambient-task:test"]["starts"]
    for i, lease in enumerate(leases):
        await background.repository.release_event(f"claude-slot:{i}", lease.token)
    await scan(background)
    assert background.repository.connector_state["ambient-task:test"]["starts"] == [second]


async def test_marker_change_during_start_supersedes_without_notifying(background):
    background.bucket_fake.add("sample")
    task = background.settings.ambient_tasks[0]
    markers = list(background.bucket_fake.paths.values())
    state = {"key": ambient.arrival_key(task.id, "sample", markers), "sample": "sample",
             "task": task.id, "markers": markers, "status": "queued", "ambient_due": 0}
    replace_markers(background)
    await ambient.start(background, task, state, "T1", 0)
    assert state["status"] == "superseded" and "ambient_due" not in state
    assert not background.sent


async def test_lost_latest_pointer_write_does_not_duplicate_arrival(background, monkeypatch):
    background.bucket_fake.add("sample")
    save = background.repository.set_connector_state

    async def fail_pointer(key, state):
        if key.startswith("ambient-sample:"):
            raise httpx.ReadTimeout("lost pointer write")
        await save(key, state)

    monkeypatch.setattr(background.repository, "set_connector_state", fail_pointer)
    assert (await scan(background))["ambient_errors"] == 1
    monkeypatch.setattr(background.repository, "set_connector_state", save)
    await scan(background)
    assert len([k for k in background.repository.connector_state
                if k.startswith("ambient-arrival:")]) == 1
    assert (await event_record(background))["status"] == "pending"


@pytest.mark.parametrize("route", ["reply", "no_reply", "storage", "transfer", "workflow",
                                  "command", "access", "cloud_access", "pr"])
async def test_noncompute_decisions_finish_owner_only(background, route):
    task = background.settings.ambient_tasks[0]
    task.recipients.append(
        task.authorized_by.model_copy(update={"email": "bob@example.com", "slack_user": "U2"})
    )
    doc = Registry(background.store).load()
    doc.projects[background.pid].members.add("bob@example.com")
    background.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    background.decision.return_value = conversation.Decision(
        route="compute" if route == "pr" else route, reply="This sample was already processed.",
        pull_request_repo="owner/repo" if route == "pr" else None,
    )
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    assert await background.process(record["event_id"], 0) == "complete"
    state = await ambient.latest_arrival(background, "test", "sample")
    assert state["status"] == "done" and state["decision"]["route"] == "reply"
    assert not state.get("notices")
    assert all(m["channel"] == "D1" for m in background.sent)
    background.test_client.create_job.assert_not_called()


async def test_compute_envelope_replaces_resources_and_keeps_configured_worker_text(background):
    background.decision.return_value = conversation.Decision(
        route="compute", reply="Run an unrelated task", reference_repos=["evil/repo"],
        write_paths=["gs://lab-data/unrelated/"],
        worker=tiers.WorkerChoice(tier="gpu", on_demand=False, expected_hours=100,
                                 gpu_type="h100", allow_resize=True, min_memory_gib=1000,
                                 reason="Model requested larger resources"),
    )
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    assert await background.process(record["event_id"], 0) == "running"
    record = await event_record(background)
    decision = record["direct_decision"]
    assert decision["worker"]["tier"] == "standard"
    assert decision["worker"]["on_demand"] and not decision["worker"]["allow_resize"]
    assert decision["worker"]["expected_hours"] == 12
    assert not decision["worker"]["gpu_type"] and not decision["reference_repos"]
    assert not decision["write_paths"] and not record.get("pull_request_repo")
    manifest = json.loads(background.store.get(
        f"tasks/slack-analysis/events/{record['event_id']}/request.json"
    )[0])
    assert manifest["payload"]["event"]["text"] == record["ambient"]["worker_prompt"]
    assert "Follow recipes/ambient.md" in manifest["payload"]["event"]["text"]
    assert "Run an unrelated task" not in manifest["payload"]["event"]["text"]


async def test_markers_cutoff_output_facts_and_missing(background):
    fake = background.bucket_fake
    fake.add("old", time.time() - 200000)
    fake.add("published")
    fake.outputs.add("results/published/")
    fake.add("partial", complete=False)
    fake.add("sample")
    await scan(background)
    states = background.repository.connector_state
    assert states[receipt_key(background, "old")]["status"] == "ignored"
    assert states[receipt_key(background, "published")]["status"] == "running"
    assert states[receipt_key(background, "published")]["output_exists"]
    assert "ambient:test:partial" not in states
    assert states[receipt_key(background)]["status"] == "queued"


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
    state = await background.repository.get_connector_state(receipt_key(background))
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
    assert (await ambient.latest_arrival(background, "test", "new"))["status"] == "queued"
    record = await event_record(background, "old")
    record["status"] = "complete"
    await background.repository.set_connector_state("claude-event:" + record["event_id"], record)
    await scan(background)
    assert (await ambient.latest_arrival(background, "test", "new"))["status"] == "queued"


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
    state = background.repository.connector_state[receipt_key(background)]
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
    background.repository.connector_state[receipt_key(background)]["ambient_due"] = 0
    await scan(background)
    assert (await event_record(background))["status"] == "pending"
    assert "reason" not in background.repository.connector_state[receipt_key(background)]
    await scan(background)
    assert background.test_slack.chat_update.await_count == expected
    if expected:
        edit = background.test_slack.chat_update.call_args.kwargs
        assert edit["ts"] == background.sent[0]["ts"]
        assert "recovered and started" in edit["text"]
    await scan(background)
    assert background.test_slack.chat_update.await_count == expected


@pytest.mark.parametrize("failure", [
    json.JSONDecodeError("PRIVATE_RESPONSE", "PRIVATE_BODY", 0),
    httpx.HTTPStatusError("PRIVATE_RESPONSE", request=httpx.Request("POST", "https://example.org"),
                          response=httpx.Response(502)),
    httpx.ReadTimeout("PRIVATE_RESPONSE"),
    identity.IdentityUnavailable("PRIVATE_RESPONSE"),
])
async def test_dependency_failure_retries_without_block_alert_or_quota(background, caplog, failure):
    background.bucket_fake.add("sample")
    background.memory_backend.read_bytes.side_effect = failure
    result = await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert result["ambient_retrying"] == 1 and result["ambient_blocked"] == 0
    assert state["status"] == "retrying" and state["ambient_due"] > time.time()
    assert "PRIVATE_" not in state["reason"] and "PRIVATE_" not in caplog.text
    assert not background.sent
    assert not background.repository.connector_state["ambient-task:test"]["starts"]
    background.memory_backend.read_bytes.side_effect = None
    state["ambient_due"] = 0
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["status"] == "running" and "reason" not in state
    eid = state["event_id"]
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["event_id"] == eid and len(background.sent) == 1
    background.test_slack.chat_update.assert_not_awaited()


async def test_failed_enqueue_response_keeps_reservation_identity_and_deadline(
    background, monkeypatch
):
    background.bucket_fake.add("sample")
    accept = background.accept

    async def lost_response(*args, **kwargs):
        await accept(*args, **kwargs)
        raise httpx.ReadTimeout("accepted, response lost")

    monkeypatch.setattr(background, "accept", lost_response)
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["status"] == "retrying"
    original = {k: state[k] for k in ["root", "start", "deadline_at", "event_id"]}
    assert background.repository.connector_state["ambient-task:test"]["active"] == [state["key"]]
    monkeypatch.setattr(background, "accept", accept)
    state["ambient_due"] = 0
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["status"] == "running" and "reason" not in state
    assert {k: state[k] for k in original} == original
    assert background.repository.connector_state["ambient-task:test"]["starts"] == [state["key"]]
    assert len(background.sent) == 1
    assert sum(k.startswith("claude-event:") for k in background.repository.connector_state) == 1


async def test_dependency_failure_does_not_abort_other_task_admission(background):
    task = background.settings.ambient_tasks[0]
    background.settings.ambient_tasks.append(task.model_copy(update={"id": "other"}))
    background.bucket_fake.add("sample")
    background.memory_backend.read_bytes.side_effect = [
        httpx.ReadTimeout("unavailable"), (b"recipe", "head"), (b"recipe", "head"),
    ]
    result = await scan(background)
    assert result["ambient_retrying"] == 1 and result["ambient_started"] == 1
    assert (await ambient.latest_arrival(background, "other", "sample"))["status"] == "retrying"
    assert background.repository.connector_state[receipt_key(background)]["status"] == "running"


async def recovered_receipt(background):
    """A receipt written by the old controller after a genuine startup block recovered."""
    background.bucket_fake.add("sample")
    background.memory_backend.read_bytes.side_effect = FileNotFoundError("recipe")
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    background.memory_backend.read_bytes.side_effect = None
    state["ambient_due"] = 0
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    state["reason"] = "The configured project recipe is missing"
    state.pop("recovery_notice_pending", None)
    return state


async def test_existing_running_receipt_clears_stale_reason_and_edits_original_notice(background):
    state = await recovered_receipt(background)
    original = {k: state[k] for k in ["event_id", "root", "start", "deadline_at"]}
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert "reason" not in state and "recovery_notice_pending" not in state
    assert {k: state[k] for k in original} == original
    assert len(background.sent) == 2  # Original block and run-root; recovery only edits.
    background.test_slack.chat_update.assert_awaited_once()
    assert background.test_slack.chat_update.call_args.kwargs["ts"] == background.sent[0]["ts"]


@pytest.mark.parametrize("uncertain", [False, True])
async def test_recovery_edit_requires_identity_and_confirmed_delivery(background, uncertain):
    state = await recovered_receipt(background)
    if uncertain:
        key = ambient_delivery.message_key(state["key"] + ":blocked")
        saved = background.repository.connector_state[key]
        saved.pop("sent", None)
        saved["sending"] = True
    else:
        background.app.slack_user_map["T1:U1"] = "other@example.com"
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["status"] == "running" and "reason" not in state
    assert state["recovery_notice_pending"]
    background.test_slack.chat_update.assert_not_awaited()
    assert len(background.sent) == 2


async def test_recovery_edit_retries_after_completion_without_restarting_work(background):
    state = await recovered_receipt(background)
    background.test_slack.chat_update.side_effect = httpx.ReadTimeout("ambiguous edit")
    record = await event_record(background)
    record["status"] = "complete"
    await background.repository.set_connector_state("claude-event:" + record["event_id"], record)
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["status"] == "done" and "reason" not in state
    assert state["recovery_notice_pending"] and state["ambient_due"] > time.time()
    assert not background.repository.connector_state["ambient-task:test"]["active"]
    background.test_slack.chat_update.side_effect = None
    state["ambient_due"] = 0
    await scan(background)
    state = background.repository.connector_state[receipt_key(background)]
    assert state["status"] == "done" and "ambient_due" not in state
    assert "recovery_notice_pending" not in state
    assert background.test_slack.chat_update.await_count == 2
    assert len(background.sent) == 2
    assert sum(k.startswith("claude-event:") for k in background.repository.connector_state) == 1


async def test_recovery_reloads_terminal_state_under_delivery_lease(background):
    state = await recovered_receipt(background)
    stale = dict(state)
    state.update(status="done", recovery_notice_pending=True)
    lease = await background.repository.begin_event("ambient-terminal:" + state["event_id"])
    await ambient.reconcile_startup_status(background, stale)
    background.test_slack.chat_update.assert_not_awaited()
    await background.repository.release_event("ambient-terminal:" + state["event_id"], lease.token)
    await ambient.reconcile_startup_status(background, stale)
    assert background.repository.connector_state[state["key"]]["status"] == "done"


async def test_finished_legacy_receipt_recovers_notice_without_admission(background):
    state = await recovered_receipt(background)
    state["status"] = "done"
    state.pop("ambient_due", None)
    background.repository.connector_state["ambient-task:test"]["active"] = []
    await scan(background)
    saved = background.repository.connector_state[state["key"]]
    assert saved["status"] == "done" and "reason" not in saved and "ambient_due" not in saved
    assert "recovery_notice_pending" not in saved
    background.test_slack.chat_update.assert_awaited_once()
    assert len(background.sent) == 2
    assert sum(k.startswith("claude-event:") for k in background.repository.connector_state) == 1


async def test_preset_and_seed_win_over_active_project_with_model_context(background):
    other = seed_project(background.store, name="other", channel="D2")
    assert other != background.pid
    background.settings.ambient_tasks.append(AmbientTask(**(definition(other) | {"id": "other"})))
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    assert await background.process(record["event_id"], 0) == "running"
    background.decision.assert_awaited_once()
    model_context = background.decision.call_args.args[5]
    assert "gs://lab-data/results/sample/" in model_context["write_access"]
    assert "Create new objects only" in model_context["write_access"]
    assert "after confirmation" not in model_context["write_access"]
    record = await event_record(background)
    assert record["project_id"] == background.pid and record["reference_repos"] == []
    assert record["request_deadline"] == record["deadline_at"]
    assert record["status"] == "running" and not record.get("compute_approval")
    job = background.test_client.create_job.call_args.kwargs["job"]
    worker_environment = job.task_groups[0].task_spec.runnables[0].environment.variables
    definitions = json.loads(worker_environment["AGENT_AMBIENT_TASKS"])
    assert [task["project"] for task in definitions] == [background.pid]
    assert definitions[0]["recipe"] == "recipes/ambient.md"
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


@pytest.mark.parametrize("configured,expected", [(None, True), (False, False), (True, True)])
async def test_task_script_delivery_overrides_owner_default(background, configured, expected):
    background.settings.return_scripts_default = True
    background.settings.ambient_tasks[0].return_scripts = configured
    background.bucket_fake.add("sample")
    await scan(background)
    record = await event_record(background)
    assert record["ambient"]["return_scripts"] is configured
    assert await background.process(record["event_id"], 0) == "running"
    manifest = json.loads(
        background.store.get(f"tasks/slack-analysis/events/{record['event_id']}/request.json")[0]
    )
    assert manifest.get("return_scripts", False) is expected


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
    source_factory = Mock(
        side_effect=AssertionError("Nested export must fail before staging reads")
    )
    background.store.bucket.blob = source_factory
    with pytest.raises(ValueError, match="already includes results/sample/"):
        write_broker.upload(
            background.store,
            background.settings,
            background.pid,
            event,
            "results/sample/qc/report.pdf",
            grant.id,
            lambda: None,
        )
    source_factory.assert_not_called()
    background.store.bucket.blob = lambda name: source
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
    assert not any(m["channel"] == "D2" for m in background.sent)
    assert await background.process(record["event_id"], 0) == "running"
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
    collaborator_posts = [m for m in background.sent if m["channel"] == "D2"]
    assert len(collaborator_posts) == 2  # Bob's start and final; owner has routing progress.
    assert collaborator_posts[-1]["thread_ts"] == collaborator_posts[0]["ts"]
    assert upload.await_count == 1
    assert background.repository.connector_state[receipt_key(background)]["status"] == "done"


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
    assert background.repository.connector_state[receipt_key(background)]["status"] == "queued"
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
    background.decision.assert_awaited_once()


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
