"""Deadline reminders notify privately without duplicating or changing approval authority."""

# ruff: noqa: F811
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from slack_sdk.errors import SlackApiError
from test_analysis_batch import bridge, payload  # noqa: F401
from test_cloud_access import bundle

from mica.agents.analysis import (
    ambient_delivery,
    approvals,
    atomx_delivery,
    cloud_access,
    job_lifecycle,
    transfers,
)
from mica.agents.analysis import approval_reminders as reminders
from mica.api import slack_analysis

ID = "a" * 32
DEADLINE = 5000


@pytest.fixture
def clock(monkeypatch):
    now = [DEADLINE - 1200]
    monkeypatch.setattr(reminders.time, "time", lambda: now[0])
    return now


@pytest.fixture
def slack(bridge, monkeypatch):
    client = bridge.test_slack
    monkeypatch.setattr(ambient_delivery, "AsyncWebClient", lambda **kw: client)
    client.chat_postMessage.return_value = {"ts": "99.0", "channel": "D1"}
    client.chat_getPermalink.return_value = {"permalink": "https://example.slack.com/archives/D1/p90"}
    client.conversations_history.return_value = {"messages": []}
    client.conversations_open.side_effect = AssertionError("No im:write scope required")
    return client


async def seed(bridge, kind, monkeypatch):
    record = {
        "payload": payload(), "notice_ts": "9.0", "status": "awaiting_approval",
        "gpu_approval": True, "overwrite_approval": True,
    }
    await bridge.repository.set_connector_state("claude-event:" + ID, record)
    if kind in {"compute", "write", "command"}:
        row = {"status": "pending", "state": "pending", "expires_at": DEADLINE,
               "expires": DEADLINE}
        approvals.save(bridge.store, approvals.resolve(kind).key(ID), row, None)
    elif kind == "amendment":
        record.update(status="pending", amendable=True, amendment_expires=DEADLINE)
        await bridge.repository.set_connector_state("claude-event:" + ID, record)
    elif kind == "access":
        await bridge.repository.set_connector_state(approvals.resolve(kind).key(ID), {
            "state": "pending", "expires": DEADLINE, "owner": "T1:UOWNER",
            "user": "UREQUESTER", "owner_channel": "DOWNER", "message_ts": "9.0",
        })
    elif kind == "cloud_access":
        policy = bundle(approver="T1:UOWNER")
        bridge.settings.cloud_access = {"vm": policy}
        row = cloud_access.Request(
            id=ID, bundle="vm", policy=policy, target="other@example.com",
            requester="alice@example.com", note="Request", origin="slack", team="T1",
            user="UREQUESTER", expires=DEADLINE, state="pending", card_channel="CSHARED",
            card_ts="9.0",
        ).model_dump()
        await bridge.repository.set_connector_state(approvals.resolve(kind).key(ID), row)
    else:
        monkeypatch.setattr(transfers.Transfers, "authorize", Mock())
        monkeypatch.setattr(transfers.Transfers, "login", AsyncMock(return_value={
            "status": "valid", "version": "version",
        }))
        await bridge.repository.set_connector_state("transfer-pending:" + ID, {
            "id": ID, "state": "preview", "project": bridge.pid, "requested_by": "a@example.com",
            "slack_user": "U1", "surface": "slack", "login_version": "version", "expires": DEADLINE,
        })
        await bridge.repository.set_connector_state(atomx_delivery.message_key(ID), {
            "channel": "D1", "ts": "9.0",
        })


def test_every_timed_kind_has_reminder_support():
    assert set(reminders.LABELS) == {
        kind.name for kind in approvals.kinds() if kind.lifetime is not None
    } | {"transfer"}


@pytest.mark.parametrize("lifetime, delay", [(3600, 2400), (1800, 600), (600, 0)])
async def test_schedules_twenty_minutes_before_expiry_or_immediately(
    bridge, clock, lifetime, delay,
):
    deadline = clock[0] + lifetime
    await reminders.schedule(bridge, "transfer", ID, deadline)
    args = job_lifecycle.enqueue.call_args.args
    assert args[1] == f"approval-reminder-transfer-{ID}-{deadline}"
    assert args[2].endswith(f"/approval-reminders/transfer/{ID}/{deadline}")
    assert args[3] == delay


async def test_watch_upgrades_existing_expiry_timer_and_preserves_expiry(bridge, clock):
    record = {"approval_timers": {"write": DEADLINE}}
    await approvals.watch(bridge, approvals.resolve("write"), ID, DEADLINE, record)
    assert job_lifecycle.enqueue.await_count == 1
    assert "approval-reminder-write" in job_lifecycle.enqueue.call_args.args[1]
    assert record == {"approval_timers": {"write": DEADLINE}}
    job_lifecycle.enqueue.reset_mock()
    await approvals.watch(bridge, approvals.resolve("cloud_access"), ID, clock[0] + 30,
                          remind=False)
    assert job_lifecycle.enqueue.await_count == 1
    assert "approval-reminder" not in job_lifecycle.enqueue.call_args.args[1]


@pytest.mark.parametrize("kind", reminders.LABELS)
async def test_pending_approval_posts_one_new_private_message_with_original_link(
    bridge, clock, slack, monkeypatch, kind,
):
    await seed(bridge, kind, monkeypatch)
    before = copy.deepcopy(bridge.repository.connector_state)
    await reminders.process(bridge, bridge.store, kind, ID, DEADLINE)
    await reminders.process(bridge, bridge.store, kind, ID, DEADLINE)
    slack.chat_postMessage.assert_awaited_once()
    message = slack.chat_postMessage.call_args.kwargs
    recipient = "DOWNER" if kind == "access" else "UOWNER" if kind == "cloud_access" else "D1"
    assert message["channel"] == recipient
    assert not message.get("thread_ts")
    assert "expires in 20 minutes" in message["text"]
    assert "<https://example.slack.com/archives/D1/p90|Review approval>" in message["text"]
    assert "blocks" not in message  # A plain link needs no approval interaction callback.
    assert "UREQUESTER" not in message["text"]
    slack.chat_update.assert_not_called()
    assert all(bridge.repository.connector_state[k] == v for k, v in before.items())


@pytest.mark.parametrize("kind", reminders.LABELS)
@pytest.mark.parametrize("timing", ["early", "expired", "renewed"])
async def test_delayed_stale_or_early_timer_cannot_notify(
    bridge, clock, slack, monkeypatch, kind, timing,
):
    await seed(bridge, kind, monkeypatch)
    stamp = DEADLINE
    if timing == "early":
        clock[0] = DEADLINE - 1201
    elif timing == "expired":
        clock[0] = DEADLINE
    else:
        stamp -= 3600
    await reminders.process(bridge, bridge.store, kind, ID, stamp)
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("kind", reminders.LABELS)
async def test_resolved_approval_never_notifies(bridge, clock, slack, monkeypatch, kind):
    await seed(bridge, kind, monkeypatch)
    if kind in {"compute", "write", "command"}:
        key = approvals.resolve(kind).key(ID)
        row, generation = approvals.load_json(bridge.store, key)
        row.update(status="approved", state="completed")
        approvals.save(bridge.store, key, row, generation)
    else:
        key = ("transfer-pending:" + ID if kind == "transfer" else "claude-event:" + ID
               if kind == "amendment" else approvals.resolve(kind).key(ID))
        row = await bridge.repository.get_connector_state(key)
        row.update(state="approved", amendment_choice="follow")
        await bridge.repository.set_connector_state(key, row)
    await reminders.process(bridge, bridge.store, kind, ID, DEADLINE)
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("change", ["cancel", "job", "terminal", "email"])
async def test_cancelled_started_terminal_or_email_run_is_not_reminded(
    bridge, clock, slack, monkeypatch, change,
):
    await seed(bridge, "compute", monkeypatch)
    key = "claude-event:" + ID
    row = await bridge.repository.get_connector_state(key)
    if change == "cancel":
        row["cancel_requested"] = True
    elif change == "job":
        row["job"] = "already-running"
    elif change == "terminal":
        row["status"] = "failed"
    else:
        row["surface"] = "email"
    await bridge.repository.set_connector_state(key, row)
    await reminders.process(bridge, bridge.store, "compute", ID, DEADLINE)
    slack.chat_postMessage.assert_not_called()


async def test_reminder_uses_same_lease_as_decision_and_retries_when_busy(
    bridge, clock, slack, monkeypatch,
):
    await seed(bridge, "write", monkeypatch)
    async with approvals.lease(bridge, "claude-poll:" + ID):
        with pytest.raises(RuntimeError):
            await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    slack.chat_postMessage.assert_not_called()
    await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    slack.chat_postMessage.assert_awaited_once()


async def test_explicit_slack_failure_retries_without_losing_reminder(
    bridge, clock, slack, monkeypatch,
):
    await seed(bridge, "write", monkeypatch)
    slack.chat_postMessage.side_effect = SlackApiError("limited", {"error": "ratelimited"})
    with pytest.raises(SlackApiError):
        await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    slack.chat_postMessage.side_effect = None
    await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    assert slack.chat_postMessage.await_count == 2


async def test_ambiguous_send_reconciles_existing_message_instead_of_reposting(
    bridge, clock, slack, monkeypatch,
):
    await seed(bridge, "write", monkeypatch)
    slack.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    client_msg_id = slack.chat_postMessage.call_args.kwargs["client_msg_id"]
    with pytest.raises(RuntimeError, match="uncertain send"):
        await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    slack.conversations_history.return_value = {"messages": [{
        "client_msg_id": client_msg_id, "ts": "99.0",
    }]}
    await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    slack.chat_postMessage.assert_awaited_once()


async def test_duplicate_concurrent_tasks_send_once(bridge, clock, slack, monkeypatch):
    await seed(bridge, "write", monkeypatch)
    results = await asyncio.gather(*(
        reminders.process(bridge, bridge.store, "write", ID, DEADLINE) for _ in range(2)
    ), return_exceptions=True)
    assert all(result is None or isinstance(result, RuntimeError) for result in results)
    slack.chat_postMessage.assert_awaited_once()


async def test_transfer_preview_poll_installs_reminder_for_existing_preview(bridge, clock):
    service = transfers.Transfers(bridge, bridge.store)
    await service.schedule({"id": ID, "state": "preview", "expires": DEADLINE, "tick": 2}, 300)
    assert job_lifecycle.enqueue.call_args_list[0].args[1].startswith("approval-reminder-transfer")
    assert job_lifecycle.enqueue.call_args_list[1].args[1] == f"transfer-{ID}-2"


@pytest.mark.parametrize("seconds, text", [(600, "10 minutes"), (61, "2 minutes"), (1, "1 minute")])
async def test_short_and_delayed_reminders_show_actual_remaining_time(
    bridge, clock, slack, monkeypatch, seconds, text,
):
    await seed(bridge, "command", monkeypatch)
    clock[0] = DEADLINE - seconds
    await reminders.process(bridge, bridge.store, "command", ID, DEADLINE)
    assert f"expires in {text}." in slack.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.parametrize("kind", ["transfer", "cloud_access"])
async def test_changed_transfer_connection_or_cloud_policy_suppresses_reminder(
    bridge, clock, slack, monkeypatch, kind,
):
    await seed(bridge, kind, monkeypatch)
    if kind == "transfer":
        transfers.Transfers.login.return_value = {"status": "valid", "version": "replaced"}
    else:
        bridge.settings.cloud_access = {}
    await reminders.process(bridge, bridge.store, kind, ID, DEADLINE)
    slack.chat_postMessage.assert_not_called()


async def test_missing_card_retries_and_expiry_during_link_lookup_skips_send(
    bridge, clock, slack, monkeypatch,
):
    await seed(bridge, "write", monkeypatch)
    key = "claude-event:" + ID
    row = await bridge.repository.get_connector_state(key)
    row["notice_ts"] = ""
    await bridge.repository.set_connector_state(key, row)
    with pytest.raises(RuntimeError, match="card delivery"):
        await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    row["notice_ts"] = "9.0"
    await bridge.repository.set_connector_state(key, row)

    async def slow_link(**kwargs):
        clock[0] = DEADLINE
        return {"permalink": "https://example.slack.com/archives/D1/p90"}

    slack.chat_getPermalink.side_effect = slow_link
    await reminders.process(bridge, bridge.store, "write", ID, DEADLINE)
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("allowed", [True, False])
async def test_endpoint_requires_dispatcher(bridge, monkeypatch, allowed):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(
        settings=bridge.app, repository=bridge.repository,
    ))))
    monkeypatch.setattr(slack_analysis, "get_principal", AsyncMock(return_value=SimpleNamespace(
        id=bridge.app.cloud_tasks_service_account.lower() if allowed else "worker@example.com",
    )))
    monkeypatch.setattr(slack_analysis, "admit", AsyncMock())
    callback = AsyncMock()
    monkeypatch.setattr(reminders, "process", callback)
    if allowed:
        assert await slack_analysis.remind_approval("write", ID, DEADLINE, request) == {
            "status": "checked",
        }
        callback.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await slack_analysis.remind_approval("write", ID, DEADLINE, request)
        assert exc.value.status_code == 403
        callback.assert_not_called()
