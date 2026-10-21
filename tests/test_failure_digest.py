"""Incident counts and delivery remain correct across retries, races, and outages."""

# ruff: noqa: F811
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_gmail_intake import SENDER, email_record, mail, signing  # noqa: F401

from mica.agents.analysis import email_delivery, memory_compaction, observability
from mica.agents.analysis.config import Settings
from mica.agents.analysis.transfers import Transfers
from mica.api import routes
from mica.storage.failures import QUIET, RETENTION, Observation


def observation(unit="event-1", **kwargs):
    return Observation(
        scope="project:staging:service",
        kind="compute",
        cause="compute_failed",
        occurrence=unit,
        **kwargs,
    )


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(observability.time, "time", lambda: value[0])
    return value


@pytest.fixture
def slack(monkeypatch):
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "123.1"}
    client.chat_update.return_value = {"ts": "123.1"}
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: client)
    return client


def rejected(error="not_in_channel", status=200, headers=None):
    return SlackApiError(
        "not accepted",
        AsyncSlackResponse(
            client=None,
            http_verb="POST",
            api_url="https://slack.com/api/chat.postMessage",
            req_args={},
            data={"ok": False, "error": error},
            headers=headers or {},
            status_code=status,
        ),
    )


async def test_concurrent_redelivery_and_real_attempts(repository):
    await asyncio.gather(
        *(repository.record_failure(observation(str(i % 57)), 1000) for i in range(285))
    )
    assert len(repository.failures) == 1
    assert next(iter(repository.failures.values())).count == 57
    assert len(repository.failure_receipts) == 57
    await repository.record_failure(observation("other", project_id="another"), 1001)
    assert len(repository.failures) == 2


async def test_quiet_episode_keeps_history_and_replay_does_not_reopen(repository):
    first = await repository.record_failure(observation(), 1000)
    replay = await repository.record_failure(observation(), 1000 + QUIET + 1)
    assert replay.id == first.id and replay.count == 1
    new = await repository.record_failure(observation("new-request"), 1000 + QUIET + 2)
    assert new.id != first.id and new.count == 1
    assert len(repository.failures) == 2


async def test_no_channel_still_records_and_never_posts(repository, settings, slack):
    await observability.record_failure(
        repository,
        settings,
        "compute_failed",
        occurrence="run",
        event_id="run",
    )
    assert len(repository.failures) == 1
    await observability.flush(repository, settings, Settings())
    slack.chat_postMessage.assert_not_awaited()


async def test_recorder_error_does_not_raise_or_leak(repository, settings, monkeypatch, caplog):
    monkeypatch.setattr(
        repository, "record_failure", AsyncMock(side_effect=ValueError("password=x"))
    )
    await observability.record_failure(repository, settings, "compute_failed", occurrence="run")
    assert "persistence_unavailable" in caplog.text and "password=x" not in caplog.text


async def test_only_fixed_causes_and_safe_metadata_are_stored(repository, settings):
    await observability.record_failure(
        repository,
        settings,
        "compute_failed",
        occurrence="password=do-not-store",
        event_id="https://private.example/#credential",
        record={
            "project_name": "password=private",
            "principal": "<@U1>",
            "payload": {"event": {"text": "private message"}},
            "error": "private trace",
        },
    )
    row = next(iter(repository.failures.values()))
    text = row.model_dump_json() + observability.summarize(row)
    for private in ("do-not-store", "credential", "private message", "private trace", "<@U1>"):
        assert private not in text
    assert row.observation.project_name == "[redacted]"
    await observability.record_failure(repository, settings, "password=secret", occurrence="run")
    assert len(repository.failures) == 1


async def test_rejected_first_send_retries_without_new_failure(repository, settings, slack, clock):
    row = await repository.record_failure(observation(), clock[0])
    slack.chat_postMessage.side_effect = [rejected(), {"ts": "123.1"}]
    config = Settings(failure_channel="CFAILURES")
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].delivery == "pending"
    clock[0] += 400
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].delivery == "sent"
    assert repository.failures[row.id].count == 1
    assert slack.chat_postMessage.await_count == 2


async def test_ambiguous_post_is_not_blindly_retried(repository, settings, slack, clock):
    row = await repository.record_failure(observation(), clock[0])
    slack.chat_postMessage.side_effect = TimeoutError("maybe accepted")
    await observability.flush(repository, settings, Settings(failure_channel="CFAILURES"))
    assert repository.failures[row.id].delivery == "unconfirmed"
    clock[0] += 400
    await repository.record_failure(observation("new-attempt"), clock[0])
    await observability.flush(repository, settings, Settings(failure_channel="CFAILURES"))
    slack.chat_postMessage.assert_awaited_once()
    assert repository.failures[row.id].count == 2


async def test_crash_after_send_intent_fences_an_old_worker(repository):
    row = await repository.record_failure(observation(), 1000)
    old = await repository.claim_failure(row.id, "CFAILURES", 1000)
    assert await repository.claim_failure(row.id, "CFAILURES", 1001) is None
    recovered = await repository.claim_failure(row.id, "CFAILURES", 1100)
    assert recovered.delivery == "unconfirmed"
    await repository.finish_failure(row.id, old.lease_token, 1101, slack_ts="123.1", sent_count=1)
    assert repository.failures[row.id].delivery == "unconfirmed"


async def test_occurrence_during_send_remains_pending_and_updates_one_message(
    repository,
    settings,
    slack,
    clock,
):
    row = await repository.record_failure(observation(), clock[0])

    async def post(**kwargs):
        await repository.record_failure(observation("another-attempt"), clock[0])
        return {"ts": "123.1"}

    slack.chat_postMessage.side_effect = post
    config = Settings(failure_channel="CFAILURES")
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].delivery == "pending"
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].delivery == "sent"
    assert repository.failures[row.id].sent_count == 2
    slack.chat_postMessage.assert_awaited_once()
    assert "Occurrences: 2" in slack.chat_update.call_args.kwargs["text"]


async def test_known_message_update_timeout_is_safe_to_retry(repository, settings, slack, clock):
    row = await repository.record_failure(observation(), clock[0])
    config = Settings(failure_channel="CFAILURES")
    await observability.flush(repository, settings, config)
    await repository.record_failure(observation("next"), clock[0])
    slack.chat_update.side_effect = [TimeoutError(), {"ts": "123.1"}]
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].delivery == "pending"
    clock[0] += 400
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].sent_count == 2
    slack.chat_postMessage.assert_awaited_once()


async def test_rate_limit_defers_and_stops_batch(repository, settings, slack, clock):
    for i in range(3):
        await repository.record_failure(observation(str(i), project_id=str(i)), clock[0])
    slack.chat_postMessage.side_effect = rejected("ratelimited", 429, {"Retry-After": "900"})
    await observability.flush(repository, settings, Settings(failure_channel="CFAILURES"))
    slack.chat_postMessage.assert_awaited_once()
    rows = list(repository.failures.values())
    assert rows[0].next_attempt == clock[0] + 900
    assert rows[0].delivery == "pending"


async def test_retention_is_bounded_and_keeps_active_incidents(repository):
    old = await repository.record_failure(observation(), 1000)
    active = await repository.record_failure(
        observation("active", project_id="active"), 1000 + RETENTION
    )
    count = await repository.purge_failures(1000 + RETENTION + 1, limit=2)
    assert count <= 2 and old.id not in repository.failures
    assert active.id in repository.failures
    await repository.purge_failures(1000 + RETENTION + 1)
    assert all(item[0] == active.id for item in repository.failure_receipts.values())


async def test_maintenance_runs_before_disabled_or_failed_shared_reconciler(settings, monkeypatch):
    maintenance = AsyncMock(return_value={"failure_sent": 1})
    monkeypatch.setattr(observability, "maintenance", maintenance)
    monkeypatch.setattr(routes, "_require_internal_principal", lambda *args: None)
    service = SimpleNamespace(
        repository=object(),
        settings=settings,
        tasks=SimpleNamespace(
            reconcile_batch_jobs=AsyncMock(side_effect=RuntimeError("unavailable"))
        ),
    )
    with pytest.raises(RuntimeError):
        await routes.reconcile_batch_jobs(SimpleNamespace(id="operator"), service)
    maintenance.assert_awaited_once()


async def test_failed_email_delivery_is_recorded_even_after_compute_success(mail, monkeypatch):
    eid, record = await email_record(mail)
    record["status"] = "complete"
    monkeypatch.setattr(
        email_delivery, "_deliver", AsyncMock(side_effect=TimeoutError("password=x"))
    )
    with pytest.raises(TimeoutError):
        await email_delivery.deliver(mail, eid, record, "secret answer", kind="final")
    row = next(iter(mail.repository.failures.values()))
    assert row.observation.cause == "email_delivery_failed"
    assert row.observation.actor == SENDER and row.observation.surface == "email"
    assert record["status"] == "complete"
    assert "secret answer" not in row.model_dump_json()


async def test_terminal_compute_failure_precedes_failed_notification(bridge):
    record = {
        "status": "failed",
        "worker_stopped": True,
        "principal": "alice@example.com",
        "project_id": "p1",
        "payload": {"event": {"channel": "D1", "ts": "1.0"}},
    }
    bridge.test_slack.chat_postMessage.side_effect = TimeoutError()
    await bridge.notify("event", record, "failed", "failed worker")
    assert {r.observation.cause for r in bridge.repository.failures.values()} == {
        "compute_failed",
        "slack_delivery_failed",
    }


async def test_expected_cancellation_and_validation_do_not_alert(bridge):
    record = {
        "status": "failed",
        "cancel_requested": True,
        "worker_stopped": True,
        "payload": {"event": {"channel": "D1", "ts": "1.0"}},
    }
    await bridge.notify("cancel", record, "cancelled", "Cancelled")
    record.pop("cancel_requested")
    record.pop("worker_stopped")
    await bridge.notify("validation", record, "failed", "Choose a project")
    assert not bridge.repository.failures


async def test_atomx_inventory_failure_and_cancellation(bridge):
    transfer = Transfers(bridge, bridge.storage)
    pending = {
        "id": "event",
        "project": "p1",
        "requested_by": "alice@example.com",
        "job_kind": "inventory",
        "job_run": "run1",
    }
    await transfer.write_failure(pending, "job_failed")
    await transfer.write_failure(pending, "job_failed")
    row = next(iter(bridge.repository.failures.values()))
    assert row.observation.cause == "atomx_inventory_failed" and row.count == 1
    await transfer.write_failure(pending, "cancelled by alice@example.com")
    assert len(bridge.repository.failures) == 1


async def test_memory_compaction_exhaustion_is_recorded(chat, monkeypatch):
    from test_memory_compaction import seed

    base = await seed(chat)
    monkeypatch.setattr(memory_compaction, "compact", AsyncMock(side_effect=ValueError("secret")))
    await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    row = next(iter(chat.repository.failures.values()))
    assert row.observation.cause == "memory_compaction_exhausted"
    assert "secret" not in row.model_dump_json()
    await memory_compaction.process(chat, chat.pid, base.head_oid, chat.store)
    assert row.count == 1


async def test_ambiguous_post_reconciles_its_own_message_without_reposting(
    repository,
    settings,
    slack,
    clock,
):
    from uuid import NAMESPACE_URL, uuid5

    row = await repository.record_failure(observation(), clock[0])
    slack.chat_postMessage.side_effect = TimeoutError()
    config = Settings(failure_channel="CFAILURES")
    await observability.flush(repository, settings, config)
    clock[0] += 3601
    slack.auth_test.return_value = {"user_id": "UBOT"}
    slack.conversations_history.return_value = {
        "messages": [
            {
                "user": "UBOT",
                "client_msg_id": str(uuid5(NAMESPACE_URL, "mica-failure:" + row.id)),
                "ts": "123.1",
            },
        ]
    }
    result = await observability.flush(repository, settings, config)
    assert result["failure_reconciled"] == 1
    assert repository.failures[row.id].slack_ts == "123.1"
    await observability.flush(repository, settings, config)
    slack.chat_postMessage.assert_awaited_once()
    slack.chat_update.assert_awaited_once()
    assert repository.failures[row.id].delivery == "sent"


async def test_missing_history_permission_leaves_uncertain_post_held(
    repository,
    settings,
    slack,
    clock,
):
    row = await repository.record_failure(observation(), clock[0])
    slack.chat_postMessage.side_effect = TimeoutError()
    config = Settings(failure_channel="CFAILURES")
    await observability.flush(repository, settings, config)
    clock[0] += 3601
    slack.auth_test.side_effect = rejected("missing_scope")
    await observability.flush(repository, settings, config)
    assert repository.failures[row.id].delivery == "unconfirmed"
    assert repository.failures[row.id].next_attempt == clock[0] + 3600
    slack.chat_postMessage.assert_awaited_once()


async def test_concurrent_flushes_post_once(repository, settings, slack, clock):
    await repository.record_failure(observation(), clock[0])
    config = Settings(failure_channel="CFAILURES")
    await asyncio.gather(*(observability.flush(repository, settings, config) for _ in range(5)))
    slack.chat_postMessage.assert_awaited_once()


async def test_large_loop_produces_one_message_with_latest_count(
    repository, settings, slack, clock
):
    for i in range(57):
        await repository.record_failure(observation(str(i)), clock[0])
    await observability.flush(repository, settings, Settings(failure_channel="CFAILURES"))
    slack.chat_postMessage.assert_awaited_once()
    assert "Occurrences: 57" in slack.chat_postMessage.call_args.kwargs["text"]
    assert slack.chat_postMessage.call_args.kwargs["mrkdwn"] is False
