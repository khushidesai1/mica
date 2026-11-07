"""Impact and post-turn monitoring without messages, cloud jobs or real user fixtures."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import (
    failure_cards,
    failure_investigation,
    failure_monitor,
    observability,
    outcome_review,
)
from mica.agents.analysis.config import Settings
from mica.storage import failures
from mica.storage.failure_catalog import CATALOG
from mica.storage.failures import NEVER, Observation


def record(**changes):
    return {
        "status": "complete",
        "tick": 1,
        "surface": "slack",
        "principal": "user@example.com",
        "payload": {"event": {"text": "Commit this note.", "channel": "DTEST", "ts": "123.4"}},
        "terminal_reply": "I cannot commit to a repository.",
        **changes,
    }


def obs(event="a", cause="compute_failed", scope="test", **changes):
    return Observation(
        scope=scope,
        kind=CATALOG[cause].kind,
        cause=cause,
        occurrence=event,
        event_id=event,
        request_scoped=True,
        surface="slack",
        **changes,
    )


def test_catalog_requires_explicit_impact():
    assert set(CATALOG) >= set(observability.Cause.__args__)
    assert all(info.priority in {"high", "medium", "low"} for info in CATALOG.values())


async def test_highest_impact_recovery_and_new_blocker(repository):
    row = await repository.record_failure(obs(), 1000)
    assert failures.priority(row) == "medium"
    row = await repository.record_failure(obs(cause="slack_delivery_failed"), 1001)
    assert failures.priority(row) == "high"
    assert failures.priority(row, "recovered") == "low"
    repository.failures[row.id].request_status = "recovered"
    row = await repository.record_failure(
        obs(cause="request_blocked", blockers=["capability"]), 1002
    )
    assert failures.priority(row) == "high" and row.request_status == ""
    assert len(repository.failures) == 1


async def test_queue_orders_impact_and_reserves_low_slot(repository):
    for i in range(5):
        await repository.record_failure(obs(f"medium-{i}"), 1000)
    low = await repository.record_failure(obs("low", "memory_editor_failed"), 1000)
    high = await repository.record_failure(
        Observation(scope="test", kind="service", cause="requests_failing", occurrence="outage"),
        1100,
    )
    rows = await repository.due_failures(1200, 5)
    assert rows[0].id == high.id and rows[-1].id == low.id
    assert [failures.priority(r) for r in rows] == ["high", "medium", "medium", "medium", "low"]
    assert failures.document(high)["high_due"] == 1100
    assert failures.document(low)["low_due"] == 1000
    assert failures.document(low)["high_due"] == NEVER


@pytest.mark.parametrize(
    "route,seconds,expected",
    [
        ("/integrations/gmail/push", 600, "high"),
        ("/integrations/slack/events", 60, "low"),
        ("unmatched route", 86400, "low"),
        ("/api/v1/internal/slack-analysis/transfers/{pending_id}/{tick}", 601, "high"),
    ],
)
async def test_sustained_intake_and_brief_or_unmatched_noise(repository, route, seconds, expected):
    o = Observation(
        scope="cloud",
        kind="infrastructure",
        cause="infra_http_error",
        occurrence="first",
        route=route,
        http_status=500,
        source="cloud_run",
    )
    await repository.record_failure(o, 1000)
    row = await repository.record_failure(
        o.model_copy(update={"occurrence": "second"}), 1000 + seconds
    )
    assert failures.priority(row) == expected


@pytest.mark.parametrize(
    "cause,status,label",
    [
        ("request_stalled", "active", "High · Mica not responding"),
        ("request_blocked", "complete", "Medium · Couldn't complete"),
        ("memory_editor_failed", "complete", "Low · Background"),
        ("compute_control_failed", "recovered", "Low · Background"),
    ],
)
async def test_cards_and_evidence_use_same_priority(repository, cause, status, label):
    row = await repository.record_failure(obs(cause=cause), 1000)
    row.request_status = status
    for native in (True, False):
        rendered = failure_cards.blocks(row, [CATALOG[cause].explanation], status, native=native)
        assert label.split(" · ")[0] + " priority" in str(rendered)
        if cause == "request_blocked":
            assert "Couldn't complete requested work" in str(rendered)
            assert "Completed · follow-up needed" not in str(rendered)
    assert label in str(failure_cards.details_view(row, []))
    assert failure_investigation.evidence(row, status, None, CATALOG)[
        "priority"
    ] == failures.priority(row)


async def test_stall_sweep_ignores_old_terminal_and_approval_records(repository, settings):
    for name, value in {
        "old": record(status="running"),
        "done": record(stall_check_at=900),
        "approval": record(status="awaiting_approval", stall_check_at=900),
        "stalled": record(status="running", stall_check_at=900, stall_check_tick=4),
    }.items():
        await repository.set_connector_state("claude-event:" + name, value)
    assert (await failure_monitor.stalls(repository, settings, 1000))["requests_stalled"] == 1
    assert len(repository.failures) == 1
    row = next(iter(repository.failures.values()))
    assert row.observation.event_id == "stalled" and failures.priority(row) == "high"
    saved = await repository.get_connector_state("claude-event:stalled")
    assert saved["status"] == "running" and "stall_check_at" not in saved
    await repository.patch_connector_state(
        "claude-event:stalled",
        {
            "stall_check_at": 900,
            "stall_check_tick": 4,
        },
    )
    await failure_monitor.stalls(repository, settings, 1001)
    assert repository.failures[row.id].count == 1
    await repository.patch_connector_state(
        "claude-event:stalled",
        {
            "stall_check_at": 900,
            "stall_check_tick": 5,
        },
    )
    await failure_monitor.stalls(repository, settings, 1002)
    assert repository.failures[row.id].count == 2


async def test_ticks_refresh_stamps_and_preserve_request_fields(repository, settings, monkeypatch):
    monkeypatch.setattr(failure_monitor.time, "time", lambda: 1000)
    await repository.set_connector_state("claude-event:a", record(status="running", job="real-job"))
    await failure_monitor.tick(repository, settings, Settings(), "a")
    row = await repository.get_connector_state("claude-event:a")
    assert row["stall_check_at"] == 1900 and row["job"] == "real-job"
    monkeypatch.setattr(failure_monitor.time, "time", lambda: 1200)
    await failure_monitor.tick(repository, settings, Settings(), "a")
    assert (await repository.get_connector_state("claude-event:a"))["stall_check_at"] == 2100
    await repository.patch_connector_state("claude-event:a", {"status": "complete"})
    await failure_monitor.tick(repository, settings, Settings(), "a")
    row = await repository.get_connector_state("claude-event:a")
    assert "stall_check_at" not in row and row["outcome_review_due"] == 1200


async def test_stall_does_not_race_active_poll(repository, settings):
    await repository.set_connector_state(
        "claude-event:a", record(status="running", stall_check_at=900)
    )
    claim = await repository.begin_event("claude-poll:a")
    await failure_monitor.stalls(repository, settings, 1000)
    assert not repository.failures
    await repository.release_event("claude-poll:a", claim.token)


async def test_process_error_still_leaves_watchdog_stamp(bridge, monkeypatch):
    event = await bridge.accept(payload())
    monkeypatch.setattr(
        bridge, "advance", AsyncMock(side_effect=RuntimeError("synthetic interruption"))
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await bridge.process(event, 0)
    saved = await bridge.repository.get_connector_state("claude-event:" + event)
    assert saved["stall_check_at"] > 0 and saved["status"] == "pending"


async def test_breadth_counts_distinct_recent_requests_not_blockers_or_replays(
    repository, settings
):
    scope = observability.request_scope(settings)
    for event in ("a", "b"):
        await repository.record_failure(obs(event, scope=scope), 1000)
    assert not failure_monitor.breadth(list(repository.failures.values()), scope, 1100)
    for _ in range(3):
        await repository.record_failure(obs("c", scope=scope), 1000)
    await repository.record_failure(obs("blocked", "request_blocked", scope), 1000)
    await repository.record_failure(obs("other-scope", scope="different"), 1000)
    assert failure_monitor.breadth(list(repository.failures.values()), scope, 1100) == {
        "compute": 3
    }
    await failure_monitor.escalations(repository, settings, 1100)
    await failure_monitor.escalations(repository, settings, 1200)
    alerts = [r for r in repository.failures.values() if r.observation.cause == "requests_failing"]
    assert len(alerts) == 1 and alerts[0].count == 1
    assert alerts[0].observation.affected_requests == 3
    assert not failure_monitor.breadth(list(repository.failures.values()), scope, 3000)


async def queue_review(repository, settings, event="a", **changes):
    settings.anthropic_api_key = SecretStr("test-key")
    row = record(**changes)
    await outcome_review.enqueue(repository, settings, Settings(), event, row, 1000)
    await repository.set_connector_state("claude-event:" + event, row)


@pytest.mark.parametrize(
    "outcome", ["completed", "clarification", "policy_denied", "in_progress", "unknown"]
)
async def test_nonfailure_reviews_do_not_record_incidents(
    repository, settings, monkeypatch, outcome
):
    await queue_review(repository, settings)
    call = AsyncMock(return_value=outcome_review.Review(outcome=outcome, blockers=[]))
    monkeypatch.setattr(outcome_review, "assess", call)
    await outcome_review.review_due(repository, settings, Settings(), 1000)
    assert not repository.failures
    assert (await repository.get_connector_state("claude-event:a"))["outcome_review_due"] == NEVER
    # A stale request writer cannot re-spend a finished review.
    await repository.patch_connector_state("claude-event:a", {"outcome_review_due": 1000})
    await outcome_review.review_due(repository, settings, Settings(), 1001)
    call.assert_awaited_once()


async def test_invalid_reviews_retry_later_once_then_stop(repository, settings, monkeypatch):
    await queue_review(repository, settings)
    call = AsyncMock(return_value=None)
    monkeypatch.setattr(outcome_review, "assess", call)
    await outcome_review.review_due(repository, settings, Settings(), 1000)
    await outcome_review.review_due(repository, settings, Settings(), 1001)
    assert call.await_count == 1
    await outcome_review.review_due(repository, settings, Settings(), 1300)
    await outcome_review.review_due(repository, settings, Settings(), 1600)
    assert call.await_count == 2 and not repository.failures
    state = await repository.get_connector_state("claude-outcome:a")
    assert state["status"] == "unavailable" and state["attempts"] == 2


async def test_blocked_review_keeps_input_out_of_incident_card_and_assessment(
    repository, settings, monkeypatch
):
    await queue_review(repository, settings, terminal_reply="private reply: password=hidden")
    monkeypatch.setattr(
        outcome_review,
        "assess",
        AsyncMock(
            return_value=outcome_review.Review(
                outcome="not_completed",
                blockers=["capability"],
            )
        ),
    )
    await outcome_review.review_due(repository, settings, Settings(), 1000)
    row = next(iter(repository.failures.values()))
    assert row.observation.slack_channel == "DTEST" and row.observation.slack_thread_ts == "123.4"
    assert failures.priority(row) == "medium"
    blocks = failure_cards.blocks(row, [], "complete")
    assert "Capability" in str(blocks)
    combined = (
        row.model_dump_json()
        + json.dumps(blocks)
        + json.dumps(failure_cards.details_view(row, []))
        + json.dumps(failure_investigation.evidence(row, "complete", record(), CATALOG))
        + json.dumps(await repository.get_connector_state("claude-outcome:a"))
    )
    assert "private reply" not in combined and "password=hidden" not in combined
    assert "Commit this note" not in combined


async def test_recovered_control_failure_does_not_hide_uncompleted_work(
    repository, settings, monkeypatch
):
    old = await repository.record_failure(
        obs("a", "compute_control_failed", observability.request_scope(settings)), 900
    )
    repository.failures[old.id].request_status = "recovered"
    await queue_review(repository, settings)
    monkeypatch.setattr(
        outcome_review,
        "assess",
        AsyncMock(
            return_value=outcome_review.Review(
                outcome="partial",
                blockers=["compute_hardware"],
            )
        ),
    )
    await outcome_review.review_due(repository, settings, Settings(), 1000)
    assert len(repository.failures) == 1
    saved = await repository.get_failure(old.id)
    assert "request_blocked" in saved.causes and failures.priority(saved) == "medium"


async def test_persistence_retry_reuses_classification(repository, settings, monkeypatch):
    await queue_review(repository, settings)
    call = AsyncMock(return_value=outcome_review.Review(outcome="partial", blockers=["capability"]))
    monkeypatch.setattr(outcome_review, "assess", call)
    save = repository.record_failure
    monkeypatch.setattr(repository, "record_failure", AsyncMock(side_effect=OSError("offline")))
    with pytest.raises(OSError):
        await outcome_review.review_due(repository, settings, Settings(), 1000)
    monkeypatch.setattr(repository, "record_failure", save)
    await outcome_review.review_due(repository, settings, Settings(), 1300)
    assert len(repository.failures) == 1
    call.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"cancel_requested": True},
        {"cancelled_by": "other"},
        {"approval_expired": "compute"},
        {"consumed_into": "parent"},
        {"status": "running"},
        {"reply_handoff": "transfer"},
        {"reply_handoff": "atomx"},
        {"direct_decision": {"route": "cloud_access"}, "reply_scope": "account"},
        {"direct_decision": {"route": "access"}},
        {"access_request_id": "pending-approval"},
        {"notice_phase": "denied"},
        {"stop_reason": "access_revoked"},
    ],
)
async def test_ineligible_requests_are_not_queued(repository, settings, changes):
    row = record(**changes)
    await outcome_review.enqueue(repository, settings, Settings(), "a", row, 1000)
    assert "outcome_review_due" not in row


@pytest.mark.parametrize("handoff", ["transfer", "atomx"])
@pytest.mark.parametrize("cached", [False, True])
async def test_queued_transfer_preparation_never_emits_semantic_failure(
    repository, settings, monkeypatch, handoff, cached
):
    settings.anthropic_api_key = SecretStr("test-key")
    await repository.set_connector_state(
        "claude-event:a",
        record(reply_handoff=handoff, terminal_reply="Preparing your AtoMx transfer request.",
               outcome_review_due=1000),
    )
    if cached:
        await repository.set_connector_state("claude-outcome:a", {
            "attempts": 1, "status": "classified",
            "result": {"outcome": "not_completed", "blockers": ["other"]},
        })
    call = AsyncMock()
    monkeypatch.setattr(outcome_review, "assess", call)
    await outcome_review.review_due(repository, settings, Settings(), 1000)
    call.assert_not_awaited()
    assert not repository.failures
    assert (await repository.get_connector_state("claude-outcome:a"))["status"] == "complete"
    assert (await repository.get_connector_state("claude-event:a"))["outcome_review_due"] == NEVER


@pytest.mark.parametrize("status", ["complete", "failed"])
async def test_background_incident_does_not_hide_task_blocker(
    repository, settings, monkeypatch, status
):
    row = await repository.record_failure(
        obs("a", "memory_editor_failed", observability.request_scope(settings)), 900
    )
    await queue_review(repository, settings, status=status)
    monkeypatch.setattr(outcome_review, "assess", AsyncMock(return_value=outcome_review.Review(
        outcome="not_completed", blockers=["software_environment"],
    )))
    await outcome_review.review_due(repository, settings, Settings(), 1000)
    assert len(repository.failures) == 1
    assert set(repository.failures[row.id].causes) == {"memory_editor_failed", "request_blocked"}


def test_progress_is_not_a_final_reply_but_terminal_legacy_notices_are():
    value = record(
        terminal_reply="", notice_text="Still waiting on a worker…", notice_phase="queued"
    )
    assert outcome_review.inputs(value)["final_reply"] == ""
    assert not outcome_review.eligible(value)
    value.update(notice_phase="failed", notice_text="Required package could not be installed.")
    assert outcome_review.eligible(value)
    assert outcome_review.inputs(value)["final_reply"] == value["notice_text"]


def test_failed_transfer_setup_still_gets_outcome_review():
    assert outcome_review.eligible(record(
        status="failed", direct_decision={"route": "transfer"},
        terminal_reply="Couldn't complete: the source is unavailable.",
    ))


@pytest.mark.parametrize(
    "body",
    [
        {"stop_reason": "end_turn", "content": []},
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "request_outcome",
                    "input": {"outcome": "partial", "blockers": ["arbitrary private text"]},
                }
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": "request_outcome",
                    "input": {"outcome": "completed", "blockers": ["capability"]},
                }
            ],
        },
    ],
)
async def test_provider_output_must_be_one_valid_enum_tool(monkeypatch, body):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    assert await outcome_review.assess("key", "model", outcome_review.inputs(record())) is None


def test_review_input_is_bounded_and_excludes_arbitrary_receipt_fields():
    value = record(
        terminal_reply="r" * 13000,
        secret="hidden",
        direct_decision={
            "route": "compute",
            "reply": "t" * 5000,
        },
    )
    value["payload"]["event"]["text"] = "u" * 5000
    packet = outcome_review.inputs(value)
    assert list(map(len, packet.values())) == [4000, 4000, 12000]
    assert "hidden" not in json.dumps(packet)
