"""Operator review is durable, scoped to its card, and never hides new failures."""

# ruff: noqa: F811
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import BackgroundTasks
from test_failure_digest import clock, observation, rejected, slack  # noqa: F401

from mica.agents.analysis import failure_actions, failure_cards, observability
from mica.agents.analysis.config import Settings
from mica.storage import failures
from mica.storage.failures import NEVER, FailureAssessment, FailureInvestigation


@pytest.fixture
def operators(slack, monkeypatch):
    monkeypatch.setattr(failure_actions, "AsyncWebClient", lambda **kw: slack)
    return Settings(failure_channel="CFAILURES", failure_operator_ids="T1:U1")


async def delivered(repository, settings, operators):
    row = await repository.record_failure(
        observation(request_scoped=True, event_id="a" * 32, surface="slack"), 1000
    )
    await observability.flush(repository, settings, operators)
    return await repository.get_failure(row.id)


def action(row, name="resolve", **changes):
    payload = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "CFAILURES"},
        "message": {"ts": row.slack_ts},
        "container": {"channel_id": "CFAILURES", "message_ts": row.slack_ts},
        "trigger_id": "trigger",
        "actions": [
            {
                "action_id": "mica_failure_" + name,
                "value": json.dumps({"id": row.id, "count": row.count}),
            }
        ],
    }
    payload.update(changes)
    return payload


async def test_resolve_updates_same_card_and_replay_preserves_reviewer(
    repository, settings, operators, slack, clock
):
    row = await delivered(repository, settings, operators)
    background = BackgroundTasks()
    await failure_actions.handle(repository, settings, operators, action(row), background)
    # Persisted before Slack update, with no task state changes or false recovery.
    saved = await repository.get_failure(row.id)
    assert saved.resolved and saved.resolved_by == "T1:U1"
    assert not repository.connector_state
    await background()
    slack.chat_postMessage.assert_awaited_once()
    slack.chat_update.assert_awaited_once()
    params = slack.chat_update.call_args.kwargs
    assert params["ts"] == row.slack_ts
    assert "Resolved" in str(params["blocks"]) and "Mark resolved" not in str(params["blocks"])
    assert "not job recovery" in params["text"]
    before = saved.resolved_at
    clock[0] += 1
    await failure_actions.handle(repository, settings, operators, action(row), BackgroundTasks())
    saved = await repository.get_failure(row.id)
    assert saved.resolved_at == before and saved.resolved_by == "T1:U1"
    assert not await repository.due_failures(clock[0] + 1000)


@pytest.mark.parametrize(
    "changes",
    [
        {"user": {"id": "U2"}},
        {"team": {"id": "T2"}},
        {"channel": {"id": "COTHER"}},
        {"message": {"ts": "999.9"}},
        {"container": {"channel_id": "COTHER", "message_ts": "123.1"}},
    ],
)
async def test_operator_and_exact_message_context_required(
    repository, settings, operators, slack, clock, changes
):
    row = await delivered(repository, settings, operators)
    with pytest.raises(PermissionError):
        await failure_actions.handle(
            repository, settings, operators, action(row, **changes), BackgroundTasks()
        )
    assert not (await repository.get_failure(row.id)).resolved
    slack.chat_update.assert_not_awaited()


async def test_stale_button_cannot_resolve_unseen_reports(
    repository, settings, operators, slack, clock
):
    row = await delivered(repository, settings, operators)
    await repository.record_failure(row.observation.model_copy(update={"occurrence": "new"}), 1001)
    await failure_actions.handle(repository, settings, operators, action(row), BackgroundTasks())
    assert not (await repository.get_failure(row.id)).resolved
    assert "New reports arrived" in slack.chat_postEphemeral.call_args.kwargs["text"]


async def test_new_failure_reopens_but_duplicate_does_not(repository, settings, operators, clock):
    row = await delivered(repository, settings, operators)
    await repository.resolve_failure(row.id, row.count, "T1:U1", row.channel, row.slack_ts, 1000)
    replay = await repository.record_failure(row.observation, 1001)
    assert replay.resolved
    updated = await repository.record_failure(
        row.observation.model_copy(update={"occurrence": "new"}), 1002
    )
    assert not updated.resolved and updated.resolved_by == "T1:U1"
    assert "Reopened" in str(failure_cards.blocks(updated, ["Worker failed."], "failed"))


async def test_resolution_during_delivery_cannot_be_overwritten(
    repository, settings, operators, clock
):
    row = await delivered(repository, settings, operators)
    await repository.record_failure(row.observation.model_copy(update={"occurrence": "new"}), 1000)
    sending = await repository.claim_failure(row.id, row.channel, 1000)
    await repository.resolve_failure(row.id, 2, "T1:U1", row.channel, row.slack_ts, 1001)
    await repository.finish_failure(
        row.id,
        sending.lease_token,
        1002,
        slack_ts=row.slack_ts,
        sent_count=2,
        sent_resolved_count=0,
        card_version=failure_cards.VERSION,
    )
    saved = await repository.get_failure(row.id)
    assert saved.resolved and saved.delivery == "pending" and saved.next_attempt == 1002


async def test_user_requests_take_priority_over_older_service_noise(repository):
    for i in range(20):
        await repository.record_failure(observation(str(i), project_id=str(i)).model_copy(
            update={"kind": "infrastructure", "cause": "infra_http_error"}
        ), 1000)
    for i in range(5):
        await repository.record_failure(
            observation(f"user-{i}", request_scoped=True, event_id=str(i)), 1010
        )
    rows = await repository.due_failures(1100, limit=5)
    assert len(rows) == 5 and all(row.user_related for row in rows[:4])
    assert not rows[4].user_related
    assert failures.document(rows[0])["request_due"] == 1010
    assert failures.document(rows[0])["background_due"] == NEVER
    assert failures.document(rows[4])["background_due"] == 1000


async def test_infrastructure_only_cycle_is_quieter(repository):
    for i in range(10):
        await repository.record_failure(observation(str(i), project_id=str(i)).model_copy(
            update={"kind": "infrastructure", "cause": "infra_http_error"}
        ), 1000)
    assert len(await repository.due_failures(1100, limit=5)) == 1


async def test_native_card_rejection_uses_actionable_compatible_blocks(
    repository, settings, operators, slack, clock
):
    slack.chat_postMessage.side_effect = [rejected("invalid_blocks"), {"ts": "123.1"}]
    row = await delivered(repository, settings, operators)
    assert row.delivery == "sent"
    assert slack.chat_postMessage.await_count == 2
    first, fallback = [call.kwargs for call in slack.chat_postMessage.call_args_list]
    assert any(block["type"] == "card" for block in first["blocks"])
    assert all(block["type"] not in {"card", "rich_text"} for block in fallback["blocks"])
    assert "Mark resolved" in str(fallback["blocks"]) and "Details" in str(fallback["blocks"])
    assert first["client_msg_id"] == fallback["client_msg_id"]


async def test_details_uses_stored_incident_not_payload_content(
    repository, settings, operators, slack, clock
):
    row = await delivered(repository, settings, operators)
    payload = action(row, "details", response_url="https://evil.example", text="private input")
    await failure_actions.handle(repository, settings, operators, payload, BackgroundTasks())
    view = slack.views_open.call_args.kwargs["view"]
    assert row.id in str(view) and "private input" not in str(view) and "evil" not in str(view)
    assert not (await repository.get_failure(row.id)).resolved


async def test_details_formats_complete_assessment_and_keeps_dynamic_values_literal(repository):
    row = await repository.record_failure(
        observation(
            request_scoped=True,
            event_id="a" * 32,
            actor="operator@example.org",
            project_name="*Literal project* <@U1>",
            service="mica",
            route="/worker/{id}",
            revision="release-1",
            http_status=500,
            api_status=403,
        ),
        1000,
    )
    row.investigation = FailureInvestigation(
        key="evidence",
        evidence={},
        status="complete",
        assessment=FailureAssessment(
            summary="A complete explanation " * 20,
            likely_cause="A suspected cause with *literal asterisks* <@U1>.",
            next_check="Inspect the recorded worker control response.",
        ),
    )
    view = failure_cards.details_view(row, ["The worker control operation failed."])
    headings = [b["text"]["text"] for b in view["blocks"] if b["type"] == "header"]
    assert headings == [
        row.observation.project_name,
        "Assessment",
        "Recorded evidence",
        "Technical references",
    ]
    text = json.dumps(view)
    assert row.investigation.assessment.summary in text  # Full text, unlike the card preview.
    assert row.id in text and "release-1" in text and "403" in text
    assert '"bold": true' in text and '"type": "mrkdwn"' not in text
    assert '"type": "user"' not in text and '"type": "link"' not in text
    for native in (True, False):
        blocks = failure_cards.blocks(row, [], "failed", native=native)
        assert blocks[-1]["type"] == ("card" if native else "actions")
        assert row.investigation.assessment.next_check not in json.dumps(blocks)
    assert len(view["blocks"]) < 100


async def test_details_preserves_reopened_review_and_missing_assessment(repository):
    row = await repository.record_failure(observation(request_scoped=True), 1000)
    row.resolved_at, row.resolved_by, row.resolved_count, row.count = 1001, "T1:U1", 1, 2
    view = failure_cards.details_view(row, ["Worker stopped."])
    text = json.dumps(view)
    assert "Reopened" in text and "open again" in text and "T1:U1" in text
    assert "Assessment unavailable" in text and "Worker stopped." in text


async def test_card_keeps_metadata_inside_card_and_within_limits(repository):
    row = await repository.record_failure(
        observation(request_scoped=True, event_id="event", actor="x" * 254, project_name="p" * 200),
        1000,
    )
    (card,) = failure_cards.blocks(row, ["Error " * 500, "Other issue"], "failed")
    assert "Medium priority · Compute" in card["subtext"]["text"]
    assert '"type": "tag"' not in json.dumps(card)
    assert len(card["title"]["text"]) <= 150 and len(card["subtitle"]["text"]) <= 150
    assert len(card["body"]["text"]) <= 200 and len(card["subtext"]["text"]) <= 200


async def test_card_prefers_request_diagnosis_and_lookup_outage_never_claims_recovery(
    repository, settings, operators, slack, clock, monkeypatch
):
    row = await delivered(repository, settings, operators)
    stored = repository.failures[row.id]
    stored.causes = ["infra_http_error", "compute_failed"]
    stored.request_status = "recovered"
    await repository.record_failure(row.observation.model_copy(update={"occurrence": "new"}), 1000)
    monkeypatch.setattr(repository, "get_connector_state", AsyncMock(side_effect=TimeoutError()))
    await observability.flush(repository, settings, operators)
    card = slack.chat_update.call_args.kwargs["blocks"][0]
    assert "worker stopped" in card["body"]["text"]
    assert "Needs investigation" in card["subtext"]["text"]


async def test_signed_slack_route_dispatches_operator_action_and_rejects_forgery(
    repository, settings, operators, slack, clock, monkeypatch
):
    from test_slack_projects import _headers

    from mica.connectors import slack as connector

    row = await delivered(repository, settings, operators)
    settings.slack_signing_secret = "test-secret"
    payload = action(row)
    body = urlencode({"payload": json.dumps(payload)}).encode()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=SimpleNamespace(repository=repository, settings=settings)
            )
        ),
        body=AsyncMock(return_value=body),
        headers=_headers(body, "wrong-secret"),
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await connector.slack_actions(request, BackgroundTasks())
    assert error.value.status_code == 401
    assert not (await repository.get_failure(row.id)).resolved
    monkeypatch.setenv("AGENT_FAILURE_CHANNEL", "CFAILURES")
    monkeypatch.setenv("AGENT_FAILURE_OPERATOR_IDS", "T1:U1")
    monkeypatch.setattr("mica.api.migration.admit", AsyncMock())
    request.headers = _headers(body, "test-secret")
    background = BackgroundTasks()
    assert await connector.slack_actions(request, background) == {}
    assert (await repository.get_failure(row.id)).resolved
    assert len(background.tasks) == 1
