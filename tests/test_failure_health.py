"""Quiet retries still retain evidence, wake on new failures and escalate when unresolved."""

# ruff: noqa: F811
from unittest.mock import AsyncMock

import pytest
from test_failure_digest import clock, slack  # noqa: F401

from mica.agents.analysis import failure_cards, failure_health, failure_monitor, observability
from mica.agents.analysis.config import Settings
from mica.storage.failures import NEVER, Observation

CONFIG = Settings(failure_channel="CFAILURES", failure_investigation_enabled=False)


def record(**updates):
    return {"status": "complete", "surface": "slack", "project_id": "p_test", **updates}


def receipt(**updates):
    return {
        "event_id": "event", "project_id": "p_test", "head": "a" * 40,
        "entry_sha256": "b" * 64, "verified_at": 1100, "rejected": {}, **updates,
    }


async def report(repository, settings, cause="memory_editor_failed", data=None):
    data = data or record(memory_pending=1000)
    await repository.set_connector_state("claude-event:event", data)
    await observability.record_failure(
        repository, settings, cause, occurrence=cause, event_id="event", record=data,
    )
    return next(iter(repository.failures.values()))


async def test_memory_retry_recovered_before_first_alert(repository, settings, slack, clock):
    row = await report(repository, settings)
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_not_awaited()
    assert repository.failures[row.id].next_attempt == 1300
    await repository.set_connector_state(
        "claude-event:event", record(memory_editor_done=True, memory_receipt=receipt()),
    )
    clock[0] = 1301
    await observability.flush(repository, settings, CONFIG)
    saved = repository.failures[row.id]
    assert saved.request_status == "recovered" and saved.next_attempt >= NEVER
    assert saved.count == 1 and saved.sent_count == 0 and not saved.slack_ts
    slack.chat_postMessage.assert_not_awaited()


async def test_pending_memory_alerts_after_hour_then_updates_recovery(
    repository, settings, slack, clock,
):
    row = await report(repository, settings)
    await observability.flush(repository, settings, CONFIG)
    clock[0] = 4601
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()
    assert repository.failures[row.id].next_attempt == 4901
    await repository.set_connector_state(
        "claude-event:event",
        record(memory_editor_done=True, memory_receipt=receipt(verified_at=4700)),
    )
    clock[0] = 4902
    await observability.flush(repository, settings, CONFIG)
    slack.chat_update.assert_awaited_once()
    assert repository.failures[row.id].request_status == "recovered"
    assert "Recovery verified" in str(slack.chat_update.call_args.kwargs["blocks"])


async def test_failed_turn_memory_still_alerts_if_save_stays_pending(
    repository, settings, slack, clock,
):
    await report(repository, settings, data=record(status="failed", memory_pending=1000))
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_not_awaited()
    clock[0] = 4601
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()


@pytest.mark.parametrize("changes", [
    {"memory_editor_done": False},
    {"memory_pending": 1000},
    {"memory_receipt": receipt(event_id="different")},
    {"memory_receipt": receipt(project_id="different")},
    {"memory_receipt": receipt(verified_at=999)},
    {"memory_receipt": receipt(head="")},
    {"memory_receipt": receipt(entry_sha256="")},
])
async def test_recovery_requires_matching_verified_receipt(repository, settings, clock, changes):
    data = record(memory_editor_done=True, memory_receipt=receipt()) | changes
    row = await report(repository, settings, data=data)
    assert failure_health.status(row, data) == "complete"


async def test_validation_exclusions_are_explicit_and_do_not_hide_partial_import(
    repository, settings, slack, clock,
):
    data = record(memory_editor_done=True, memory_receipt=receipt(rejected={"code/file": "limit"}))
    row = await report(repository, settings, data=data)
    await observability.flush(repository, settings, CONFIG)
    saved = repository.failures[row.id]
    assert saved.request_status == "saved_with_exclusions"
    slack.chat_postMessage.assert_not_awaited()
    rendered = str(failure_cards.details_view(saved, []))
    assert "excluded" in rendered and "retained" in rendered
    data["memory_receipt"]["import_review"] = {"status": "partial"}
    assert failure_health.status(row, data) == "complete"
    assert failure_health.should_notify(row, "complete", data, 1000)


async def test_memory_receipt_cannot_clear_delivery_failure(repository, settings, slack, clock):
    data = record(memory_editor_done=True, memory_receipt=receipt())
    await report(repository, settings, data=data)
    await report(repository, settings, "slack_delivery_failed", data)
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()
    assert next(iter(repository.failures.values())).request_status == "complete"


async def test_new_failure_during_quiet_review_is_not_lost(repository, settings, clock):
    row = await report(repository, settings)
    claimed = await repository.claim_failure(row.id, "CFAILURES", 1000)
    await observability.record_failure(
        repository, settings, "slack_delivery_failed", occurrence="delivery",
        event_id="event", record=record(),
    )
    await repository.finish_failure(
        row.id, claimed.lease_token, 1000, reviewed_count=1,
        request_status="recovered", retry_after=NEVER,
    )
    current = repository.failures[row.id]
    assert current.next_attempt == 1000 and current.request_status == ""
    assert current.sent_count == 0 and current.count == 2


async def test_new_failure_reopens_log_only_incident(repository, settings, slack, clock):
    await report(repository, settings, data=record(
        memory_editor_done=True, memory_receipt=receipt(),
    ))
    await observability.flush(repository, settings, CONFIG)
    clock[0] = 1300
    await report(repository, settings, "slack_delivery_failed")
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()


async def test_compaction_waits_for_success_evidence(repository, settings, slack, clock):
    row = await repository.record_failure(Observation(
        scope="test", kind="memory", cause="memory_compaction_retry", occurrence="retry",
        project_id="p_test", event_id="head",
    ), 1000)
    await repository.set_connector_state("memory-maintenance:p_test", {"outcome": "nothing"})
    await observability.flush(repository, settings, CONFIG)
    assert repository.failures[row.id].request_status == "unknown"
    slack.chat_postMessage.assert_not_awaited()
    await repository.set_connector_state(
        "memory-maintenance:p_test", {"outcome": "nothing", "last_success_at": 1200},
    )
    clock[0] = 1301
    await observability.flush(repository, settings, CONFIG)
    assert repository.failures[row.id].request_status == "recovered"
    slack.chat_postMessage.assert_not_awaited()


async def test_compaction_exhaustion_alerts_immediately(repository, settings, slack, clock):
    await repository.record_failure(Observation(
        scope="test", kind="memory", cause="memory_compaction_exhausted", occurrence="exhausted",
        project_id="p_test",
    ), 1000)
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()


async def test_new_background_failure_after_recovery_gets_fresh_grace_period(
    repository, settings, slack, clock,
):
    observation = Observation(
        scope="test", kind="memory", cause="memory_compaction_retry", occurrence="first",
        project_id="p_test",
    )
    first = await repository.record_failure(observation, 1000)
    await repository.set_connector_state("memory-maintenance:p_test", {"last_success_at": 1100})
    clock[0] = 1200
    await observability.flush(repository, settings, CONFIG)
    assert repository.failures[first.id].request_status == "recovered"
    clock[0] = 10000
    second = await repository.record_failure(
        observation.model_copy(update={"occurrence": "new"}), clock[0],
    )
    assert second.id != first.id and second.first_seen == clock[0]
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_not_awaited()
    assert repository.failures[first.id].request_status == "recovered"


async def test_healthy_worker_polling_is_quiet_but_stalls_alert(repository, settings, slack, clock):
    await report(repository, settings, "compute_control_failed", record(
        status="running", stall_check_at=5000,
    ))
    clock[0] = 1400
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_not_awaited()
    await report(repository, settings, "request_stalled", record(status="running"))
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()


async def test_recovered_retries_do_not_escalate_service_outage(repository, settings, clock):
    for event in ("one", "two", "three"):
        data = record(status="running", stall_check_at=5000)
        await repository.set_connector_state("claude-event:" + event, data)
        await observability.record_failure(
            repository, settings, "compute_control_failed", occurrence=event,
            event_id=event, record=data,
        )
    result = await failure_monitor.escalations(repository, settings, 1400)
    assert result == {"failure_breadth_groups": 0}
    for event in ("one", "two", "three"):
        await repository.set_connector_state("claude-event:" + event, record(status="failed"))
    result = await failure_monitor.escalations(repository, settings, 1400)
    assert result == {"failure_breadth_groups": 1}


async def test_full_log_mode_can_post_transients(repository, settings, slack, clock):
    await report(repository, settings)
    await observability.flush(
        repository, settings, CONFIG.model_copy(update={"failure_alert_mode": "all"}),
    )
    slack.chat_postMessage.assert_awaited_once()


async def test_lookup_failure_does_not_fabricate_recovery(repository, settings, slack, clock):
    row = await report(repository, settings)
    repository.get_connector_state = AsyncMock(side_effect=TimeoutError)
    clock[0] = 4601
    await observability.flush(repository, settings, CONFIG)
    slack.chat_postMessage.assert_awaited_once()
    assert repository.failures[row.id].request_status == "unknown"
