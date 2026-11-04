"""Owner approval must name a project and preserve membership/channel boundaries."""
# ruff: noqa: F811

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import BackgroundTasks, HTTPException
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401
from test_conversation_tools import Client, tool

from mica.agents.analysis import access, conversation, direct
from mica.agents.analysis import access_requests as requests
from mica.agents.analysis.registry import Registry


@pytest.fixture
def approvals(bridge, monkeypatch):
    bridge.settings.access_request_owner_id = "T1:U1"
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    client = AsyncMock()
    client.conversations_open.side_effect = AssertionError("im:write is not installed")
    client.chat_postMessage.return_value = {"ts": "90.1", "channel": "DOWNER"}
    client.views_open.return_value = {"view": {"id": "V1"}}
    monkeypatch.setattr(requests, "client_for", lambda _: client)
    bridge.approval_client = client
    return bridge


async def request(bridge, *, private=False, member=False):
    if member:
        Registry(bridge.store).update(
            "share",
            "alice@example.com",
            "share",
            lambda d: d.share(bridge.pid, "alice@example.com", "bob@example.com"),
        )
    event_id = await bridge.accept(
        payload(
            user="U2",
            channel="D2" if private else "C1",
            channel_type="im" if private else "channel",
            kind="message" if private else "app_mention",
            text="Run Cellpose-SAM on my slide",
        ),
        principal="bob@example.com",
    )
    record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    answer = await requests.propose(bridge, record, grant)
    row = await bridge.repository.get_connector_state(requests.key(record["access_request_id"]))
    return event_id, record, grant, requests.AccessRequest.model_validate(row), answer


def click(row, action="review", **overrides):
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "DOWNER"},
        "message": {"ts": row.message_ts},
        "container": {"channel_id": "DOWNER", "message_ts": row.message_ts},
        "trigger_id": "trigger",
        "actions": [{"action_id": requests.PREFIX + action, "value": row.id}],
        **overrides,
    }


async def review(bridge, row):
    await requests.handle(bridge, bridge.store, click(row), BackgroundTasks())
    view = bridge.approval_client.views_open.call_args.kwargs["view"]
    return {
        "type": "view_submission",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "view": {
            **view,
            "id": "V1",
            "state": {
                "values": {
                    "project": {
                        "project": {
                            "selected_option": {"value": bridge.pid},
                        }
                    }
                }
            },
        },
    }


async def test_member_unpublished_channel_goes_to_owner_before_model_or_worker(
    approvals, monkeypatch
):
    bridge = approvals
    event_id, record, grant, row, answer = await request(bridge, member=True)
    assert not grant.allowed and grant.doc.can_use(bridge.pid, grant.email)
    assert "owner an approval card" in answer
    router = AsyncMock(side_effect=AssertionError("No model for denied channel"))
    monkeypatch.setattr(conversation, "run", router)
    assert await direct.handle(bridge, event_id, record, bridge.store)
    router.assert_not_called()
    bridge.test_client.create_job.assert_not_called()
    assert bridge.approval_client.chat_postMessage.await_count == 1
    assert bridge.approval_client.chat_postMessage.call_args.kwargs["channel"] == "U1"
    assert not Registry(bridge.store).load().bound("T1", "C1")
    action = requests.blocks(row)[-1]["elements"][0]
    assert action["style"] == "primary"


async def test_channel_approval_is_explicit_scoped_and_not_execution(approvals):
    bridge = approvals
    _, record, _, row, _ = await request(bridge, member=True)
    submission = await review(bridge, row)
    view = submission["view"]
    assert "everyone who can view that channel" in json.dumps(view)
    background = BackgroundTasks()
    await requests.handle(bridge, bridge.store, submission, background)
    current = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    assert current.allowed and current.project_id == bridge.pid
    assert current.project.owner == "alice@example.com"
    assert not current.project.co_owners
    bridge.test_client.create_job.assert_not_called()
    await background()
    assert (
        "Send your request again"
        in bridge.approval_client.chat_postMessage.call_args.kwargs["text"]
    )


async def test_private_member_approval_never_publishes_and_replay_cannot_undo_revocation(approvals):
    bridge = approvals
    _, _, _, row, _ = await request(bridge, private=True)
    submission = await review(bridge, row)
    await requests.handle(bridge, bridge.store, submission, BackgroundTasks())
    registry = Registry(bridge.store)
    assert registry.load().can_use(bridge.pid, "bob@example.com")
    assert not registry.load().projects[bridge.pid].published_channels
    registry.update(
        "revoke",
        "alice@example.com",
        "unshare",
        lambda d: d.share(
            bridge.pid,
            "alice@example.com",
            "bob@example.com",
            remove=True,
        ),
    )
    await requests.handle(bridge, bridge.store, submission, BackgroundTasks())
    assert not registry.load().can_use(bridge.pid, "bob@example.com")


@pytest.mark.parametrize(
    "override",
    [
        {"user": {"id": "U2"}},
        {"team": {"id": "T2"}},
        {"channel": {"id": "C1"}},
        {"message": {"ts": "91.0"}},
        {"container": {"channel_id": "C1", "message_ts": "90.1"}},
    ],
)
async def test_other_people_workspaces_or_copied_messages_cannot_approve(approvals, override):
    _, _, _, row, _ = await request(approvals)
    with pytest.raises(PermissionError):
        await requests.handle(approvals, approvals.store, click(row, **override), BackgroundTasks())
    approvals.approval_client.views_open.assert_not_called()
    assert not Registry(approvals.store).load().can_use(approvals.pid, "bob@example.com")


async def test_denial_deduplicates_without_granting_or_rerunning(approvals):
    bridge = approvals
    _, record, grant, row, _ = await request(bridge, private=True)
    before = Registry(bridge.store).load()
    background = BackgroundTasks()
    await requests.handle(bridge, bridge.store, click(row, "deny"), background)
    await background()
    assert Registry(bridge.store).load() == before
    assert "declined" in await requests.propose(bridge, record, grant)
    assert bridge.approval_client.chat_postMessage.await_count == 2  # owner + requester
    assert not any(
        b["type"] == "actions"
        for b in bridge.approval_client.chat_update.call_args.kwargs["blocks"]
    )


@pytest.mark.parametrize("change", ["ownership", "revision", "identity", "selection", "nonce"])
async def test_stale_or_tampered_approval_never_grants_access(approvals, change):
    bridge = approvals
    _, _, _, row, _ = await request(bridge, private=True)
    submission = await review(bridge, row)
    if change in {"ownership", "revision"}:
        registry = Registry(bridge.store)

        def mutate(doc):
            if change == "ownership":
                doc.projects[bridge.pid].owner = "someone@example.com"
            else:
                doc.projects[bridge.pid].access_revision += 1
            return bridge.pid

        registry.update("changed", "alice@example.com", change, mutate)
    elif change == "identity":
        bridge.app.slack_user_map["T1:U2"] = "different@example.com"
    elif change == "selection":
        submission["view"]["state"]["values"]["project"]["project"]["selected_option"]["value"] = (
            "other"
        )
    else:
        submission["view"]["private_metadata"] = json.dumps({"id": row.id, "nonce": "old"})
    with pytest.raises((PermissionError, ValueError)):
        await requests.handle(bridge, bridge.store, submission, BackgroundTasks())
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")


async def test_unknown_send_is_not_reposted_or_claimed_delivered(approvals):
    bridge = approvals
    bridge.approval_client.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await request(bridge, private=True)
    _, _, _, _, answer = await request(bridge, private=True)
    assert "couldn't deliver" in answer
    assert bridge.approval_client.chat_postMessage.await_count == 1


async def test_model_can_request_access_but_cannot_choose_identity_or_grant(approvals, monkeypatch):
    bridge = approvals
    event_id = await bridge.accept(payload(user="U2", channel="D2", text="Analyze my data"))
    record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    sdk = Client(tool("request_access"))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **_: sdk)
    assert await direct.handle(bridge, event_id, record, bridge.store)
    assert "owner an approval card" in record["notice_text"]
    assert {t["name"] for t in sdk.calls[0]["tools"]} == {"request_access", "propose_command"}
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")
    bridge.test_client.create_job.assert_not_called()


async def test_crash_after_registry_commit_does_not_repeat_grant(approvals, monkeypatch):
    bridge = approvals
    _, _, _, row, _ = await request(bridge, private=True)
    submission = await review(bridge, row)
    original = requests.save

    async def fail_after_grant(bridge, row):
        if row.state == "approved":
            raise RuntimeError("crash")
        await original(bridge, row)

    monkeypatch.setattr(requests, "save", fail_after_grant)
    with pytest.raises(RuntimeError):
        await requests.handle(bridge, bridge.store, submission, BackgroundTasks())
    registry = Registry(bridge.store)
    registry.update(
        "revoke",
        "alice@example.com",
        "unshare",
        lambda d: d.share(
            bridge.pid,
            "alice@example.com",
            "bob@example.com",
            remove=True,
        ),
    )
    monkeypatch.setattr(requests, "save", original)
    await requests.handle(bridge, bridge.store, copy.deepcopy(submission), BackgroundTasks())
    assert not registry.load().can_use(bridge.pid, "bob@example.com")


async def test_expired_request_cannot_be_approved(approvals):
    bridge = approvals
    _, _, _, row, _ = await request(bridge, private=True)
    row.expires = 1
    await requests.save(bridge, row)
    with pytest.raises(ValueError, match="expired"):
        await requests.handle(bridge, bridge.store, click(row), BackgroundTasks())
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")


async def test_signed_route_dispatches_owner_actions_before_legacy_mapping(approvals, monkeypatch):
    from test_slack_projects import _headers

    from mica.connectors import slack as connector

    bridge = approvals
    _, _, _, row, _ = await request(bridge, private=True)
    bridge.app.slack_signing_secret = "test-secret"
    body = urlencode({"payload": json.dumps(click(row, "deny"))}).encode()
    http = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(
            repository=bridge.repository, settings=bridge.app,
        ))), body=AsyncMock(return_value=body), headers=_headers(body, "wrong"),
    )
    with pytest.raises(HTTPException) as error:
        await connector.slack_actions(http, BackgroundTasks())
    assert error.value.status_code == 401
    monkeypatch.setattr("mica.api.migration.admit", AsyncMock())
    monkeypatch.setattr("mica.agents.analysis.storage.GCSStorage", lambda _: bridge.store)
    http.headers = _headers(body, "test-secret")
    background = BackgroundTasks()
    assert await connector.slack_actions(http, background) == {}
    saved = await bridge.repository.get_connector_state(requests.key(row.id))
    assert saved["state"] == "denied"


async def test_access_expiry_notifies_requester_and_safe_renewal_never_grants(approvals):
    bridge = approvals
    _, _, _, row, _ = await request(bridge, private=True)
    row.expires = 1
    await requests.save(bridge, row)
    await requests.KIND.expire(bridge, bridge.store, row.id)
    saved = requests.AccessRequest.model_validate(
        await bridge.repository.get_connector_state(requests.key(row.id))
    )
    assert saved.state == "expired" and saved.notification == "sent"
    message = bridge.approval_client.chat_postMessage.call_args.kwargs
    assert message["channel"] == row.channel and "Request again" in str(message["blocks"])
    retry = click(saved, "again", user={"id": "U2"}, channel={"id": row.channel},
                  message={"ts": saved.notification_ts},
                  container={"channel_id": row.channel, "message_ts": saved.notification_ts})
    forged = copy.deepcopy(retry)
    forged["message"]["ts"] = "other"
    with pytest.raises(PermissionError, match="original conversation"):
        await requests.handle(bridge, bridge.store, forged, BackgroundTasks())
    await requests.handle(bridge, bridge.store, retry, BackgroundTasks())
    renewed = requests.AccessRequest.model_validate(
        await bridge.repository.get_connector_state(requests.key(row.id))
    )
    assert renewed.state == "pending" and not renewed.review and renewed.expires > 1
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")
    bridge.test_client.create_job.assert_not_called()
