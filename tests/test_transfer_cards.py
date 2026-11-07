"""Transfer cards update in place and bind approval to the exact privately delivered preview."""

# ruff: noqa: F811
import copy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_atomx_delivery import private_delivery_client, private_record, refusal  # noqa: F401

from mica.agents.analysis import atomx_delivery, transfer_cards, transfer_interactions, transfers
from mica.agents.analysis.transfer_config import TransferProject, TransferRequest

EID = "a" * 32
OWNER = "alice@example.com"


@pytest.fixture
async def transfer(bridge, private_record, monkeypatch):
    private_record["payload"]["team_id"] = "T1"
    await bridge.repository.set_connector_state("claude-event:" + EID, private_record)
    service = transfers.Transfers(bridge, bridge.store)
    service.schedule = AsyncMock()
    service.authorize = Mock()
    service.available = AsyncMock(return_value={})
    service.login = AsyncMock(return_value={"status": "valid", "version": "revision"})
    service.release = AsyncMock()
    monkeypatch.setattr(transfer_interactions, "AsyncWebClient", lambda **kw: bridge.test_slack)
    monkeypatch.setattr(transfer_interactions, "resolve_email", lambda *args: (
        OWNER if args[-1] == "U1" else "someone-else@example.com"
    ))
    p = {
        "id": EID, "state": "preview", "project": bridge.pid, "requested_by": OWNER,
        "team": "T1", "slack_user": "U1", "channel": "D1", "thread": "2.0",
        "attempt": 0, "expires": time.time() + 1800, "login_version": "revision",
        "request": {"sample": "Slide 1", "as_sample": "TMA37", "export_root": "/export"},
        "preview": {"sample": "Slide 1", "export_root": "/export", "run_folder": "run-1",
                    "n_files": 1234, "total_bytes": 1234000000, "plan_fingerprint": "verified"},
        "config": {"bucket": "lab-data", "raw_prefix": "raw/",
                   "additional_prefix": "additional/", "status_prefix": "status/"},
    }
    await service.save(p)
    return service, p


def action(p, operation="confirm", **changes):
    result = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "D1"}, "message": {"ts": "9.0"},
        "actions": [{"action_id": "mica_transfer_" + operation,
                     "value": p["id"] + ":" + transfer_cards.token(p)}],
    }
    result.update(changes)
    return result


async def test_approval_shows_exact_source_destinations_and_buttons_without_link(transfer):
    service, p = transfer
    await service.notify(p, "Review this transfer.", ("confirm", "cancel"))
    message = service.bridge.test_slack.chat_postMessage.call_args.kwargs
    text = json.dumps(message["blocks"])
    for value in ("Slide 1", "TMA37", "/export / run-1", "1,234", "1.2 GB",
                  "gs://lab-data/raw/TMA37/", "gs://lab-data/additional/TMA37/"):
        assert value in text
    assert "/integrations/atomx" not in text
    buttons = message["blocks"][-1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Approve transfer", "Cancel transfer"]
    assert all("url" not in b for b in buttons)
    assert not any(k.startswith("atomx-link:") for k in service.repo.connector_state)


async def test_full_long_paths_remain_literal_and_reviewable(transfer):
    _, p = transfer
    p["preview"]["export_root"] = "/" + "folder/" * 450 + "<@UOTHER>"
    blocks = transfer_cards.blocks(p, "Preview", ("confirm", "cancel"))
    sections = [b["text"] for b in blocks if b["type"] == "section" and "text" in b]
    assert all(b["type"] == "plain_text" and len(b["text"]) <= 3000 for b in sections)
    assert p["preview"]["export_root"] in "".join(b["text"] for b in sections)


async def test_progress_approval_and_result_update_one_private_message(transfer):
    service, p = transfer
    for state, text, buttons in [
        ("inventory", "Checking files…", ("cancel",)),
        ("preview", "Review transfer", ("confirm", "cancel")),
        ("running", "Transferred 10 files.", ("cancel",)),
        ("running", "Transferred 20 files.", ("cancel",)),
        ("done", "Verified 1,234 files.", ()),
    ]:
        p["state"] = state
        await service.notify(p, text, buttons)
        await service.notify(p, text, buttons)
    client = service.bridge.test_slack
    client.chat_postMessage.assert_awaited_once()
    assert client.chat_update.await_count == 4
    assert all(c.kwargs["ts"] == "9.0" for c in client.chat_update.call_args_list)
    blocks = client.chat_update.call_args.kwargs["blocks"]
    assert blocks[0]["type"] == "plan" and blocks[0]["title"] == "Transfer complete"
    assert "Verified 1,234 files." in str(blocks)
    assert all(b["type"] != "actions" for b in blocks)


async def test_adopts_existing_thinking_message_and_routing_cannot_overwrite_it(
    transfer, private_record
):
    service, p = transfer
    private_record["notice_ts"] = "3.0"
    await service.repo.set_connector_state("claude-event:" + EID, private_record)
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    client = service.bridge.test_slack
    client.chat_postMessage.assert_not_awaited()
    assert client.chat_update.call_args.kwargs["ts"] == "3.0"
    private_record["transfer_status_owned"] = True
    await service.bridge.notify(EID, private_record, "complete", "Preparing the transfer.")
    client.chat_update.assert_awaited_once()
    assert private_record["terminal_reply"] == "Preparing the transfer."


async def test_request_hands_existing_activity_message_to_transfer_controller(
    transfer, private_record
):
    service, _ = transfer
    eid = "b" * 32
    private_record["notice_ts"] = "3.0"
    await service.repo.set_connector_state("claude-event:" + eid, private_record)
    service.configuration = AsyncMock(return_value=TransferProject(bucket="lab-data"))
    grant = SimpleNamespace(email=OWNER, project_id=service.bridge.pid,
                            project=SimpleNamespace(data_uri="gs://lab-data/", access_revision=1))
    await service.request(grant, private_record, eid, TransferRequest(sample="sample"))
    assert private_record["transfer_status_owned"]
    assert private_record["reply_handoff"] == "transfer"
    client = service.bridge.test_slack
    client.chat_postMessage.assert_not_awaited()
    assert client.chat_update.call_args.kwargs["ts"] == "3.0"
    await service.bridge.notify(eid, private_record, "complete", "Preparing the transfer.")
    client.chat_update.assert_awaited_once()


async def test_shared_request_approval_belongs_to_delivered_private_card(
    transfer, private_record, monkeypatch
):
    from mica.agents.analysis import access

    service, p = transfer
    private_record["payload"]["event"].update(channel="CPROJECT", channel_type="channel")
    await service.repo.set_connector_state("claude-event:" + EID, private_record)
    p["channel"] = "CPROJECT"
    monkeypatch.setattr(
        access, "resolve", lambda *args: SimpleNamespace(email=OWNER, private=False)
    )
    service.bridge.test_slack.conversations_open.return_value = {"channel": {"id": "DPRIVATE"}}
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    await transfer_interactions.handle(service, action(p, channel={"id": "CPROJECT"}))
    service.schedule.assert_not_awaited()
    await transfer_interactions.handle(service, action(p, channel={"id": "DPRIVATE"}))
    assert (await service.pending(EID))["state"] == "submitting"
    assert service.bridge.test_slack.chat_update.call_args.kwargs["channel"] == "DPRIVATE"


async def test_approved_once_and_current_card_becomes_queued_status(transfer):
    service, p = transfer
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    await transfer_interactions.handle(service, action(p))
    saved = await service.pending(EID)
    assert saved["state"] == "submitting" and saved["confirmed_by"] == OWNER
    service.schedule.assert_awaited_once()
    assert service.bridge.test_slack.chat_update.call_args.kwargs["blocks"][0]["type"] == "plan"
    await transfer_interactions.handle(service, action(p))
    service.schedule.assert_awaited_once()


async def test_ordinary_preview_poll_upgrades_legacy_link_without_reposting(transfer):
    service, p = transfer
    p["preview_text"] = "Review transfer"
    p["notice"] = ["Review transfer", ["confirm", "cancel"]]
    await service.advance(p)
    await service.advance(await service.pending(EID))
    service.bridge.test_slack.chat_postMessage.assert_awaited_once()
    service.bridge.test_slack.chat_update.assert_not_awaited()
    assert "Approve transfer" in str(service.bridge.test_slack.chat_postMessage.call_args.kwargs)


@pytest.mark.parametrize("changes", [
    {"team": {"id": "OTHER"}}, {"user": {"id": "U2"}},
    {"channel": {"id": "DOTHER"}}, {"message": {"ts": "1.0"}},
    {"actions": [{"action_id": "mica_transfer_confirm", "value": EID}]},
    {"actions": [{"action_id": "mica_transfer_confirm", "value": EID + ":" + "f" * 64}]},
])
async def test_copied_stale_or_foreign_actions_cannot_approve(transfer, changes):
    service, p = transfer
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    await transfer_interactions.handle(service, action(p, **changes))
    assert (await service.pending(EID))["state"] == "preview"
    service.schedule.assert_not_awaited()


@pytest.mark.parametrize("field", ["config", "preview", "login_version", "expires", "attempt"])
async def test_plan_changed_after_card_was_displayed_cannot_be_approved(transfer, field):
    service, p = transfer
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    original_action = action(p)
    changed = copy.deepcopy(p)
    if field == "config":
        changed[field]["raw_prefix"] = "different/"
    elif field == "preview":
        changed[field]["plan_fingerprint"] = "different"
    else:
        changed[field] = 0 if field == "expires" else 2 if field == "attempt" else "different"
    await service.save(changed)
    await transfer_interactions.handle(service, original_action)
    assert (await service.pending(EID))["state"] == "preview"
    service.schedule.assert_not_awaited()


async def test_expired_confirmation_and_revoked_access_cannot_start_transfer(transfer):
    service, p = transfer
    p["expires"] = 1
    await service.notify(p, "Expired preview", ("confirm", "cancel"))
    await transfer_interactions.handle(service, action(p))
    service.schedule.assert_not_awaited()
    p["expires"] = time.time() + 1800
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    service.authorize.side_effect = ValueError("Membership revoked")
    await transfer_interactions.handle(service, action(p))
    service.schedule.assert_not_awaited()


async def test_cancel_updates_card_without_starting_job(transfer):
    service, p = transfer
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    await transfer_interactions.handle(service, action(p, "cancel"))
    assert (await service.pending(EID))["state"] == "cancelled"
    service.schedule.assert_not_awaited()
    assert "Transfer cancelled" in str(service.bridge.test_slack.chat_update.call_args.kwargs)


async def test_unknown_first_post_is_reconciled_without_duplicate(transfer):
    service, p = transfer
    client = service.bridge.test_slack
    client.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(RuntimeError, match="retry"):
        await service.notify(p, "Review transfer", ("confirm", "cancel"))
    client.auth_test.return_value = {"user_id": "UBOT"}
    client.conversations_replies.return_value = {"messages": [{
        "ts": "9.0", "user": "UBOT",
        "client_msg_id": hashlib.md5(atomx_delivery.message_key(EID).encode()).hexdigest(),
    }]}
    await service.notify(p, "Review transfer", ("confirm", "cancel"))
    client.chat_postMessage.assert_awaited_once()
    client.chat_update.assert_awaited_once()


async def test_inconclusive_first_post_stays_held_without_second_post(transfer):
    service, p = transfer
    client = service.bridge.test_slack
    client.chat_postMessage.side_effect = TimeoutError()
    client.auth_test.return_value = {"user_id": "UBOT"}
    client.conversations_replies.return_value = {"messages": []}
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await service.notify(p, "Review transfer", ("confirm", "cancel"))
    client.chat_postMessage.assert_awaited_once()
    await transfer_interactions.handle(service, action(p))
    service.schedule.assert_not_awaited()


async def test_activity_falls_back_without_losing_cancel(transfer):
    service, p = transfer
    p["state"] = "running"
    client = service.bridge.test_slack
    client.chat_postMessage.side_effect = [refusal("invalid_blocks"), {"ts": "9.0"}]
    await service.notify(p, "Transferred 20 files.", ("cancel",))
    blocks = client.chat_postMessage.call_args.kwargs["blocks"]
    assert blocks[0]["type"] == "context"
    assert blocks[-1]["elements"][0]["text"]["text"] == "Cancel transfer"


async def test_full_search_failure_remains_in_status_dropdown(transfer):
    _, p = transfer
    p["state"] = "failed"
    text = "Searched folder\n" * 140 + "Ask again with this exact export."
    blocks = transfer_cards.blocks(p, text, ())
    assert blocks[0]["tasks"][-1]["details"]["elements"][0]["elements"][0]["text"] == text


async def test_missing_post_receipt_does_not_allow_blind_repost(transfer):
    service, p = transfer
    client = service.bridge.test_slack
    client.chat_postMessage.return_value = {"ok": True}
    client.auth_test.return_value = {"user_id": "UBOT"}
    client.conversations_replies.return_value = {"messages": []}
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await service.notify(p, "Review transfer", ("confirm", "cancel"))
    client.chat_postMessage.assert_awaited_once()


async def test_signed_click_is_acknowledged_before_cloud_checks(transfer, monkeypatch):
    from fastapi import BackgroundTasks, HTTPException
    from test_slack_projects import _headers

    from mica.connectors import slack

    service, p = transfer
    service.app.slack_signing_secret = "test-secret"
    body = urlencode({"payload": json.dumps(action(p))}).encode()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(
            repository=service.repo, settings=service.app,
        ))),
        body=AsyncMock(return_value=body), headers=_headers(body, "wrong-secret"),
    )
    background = BackgroundTasks()
    with pytest.raises(HTTPException) as error:
        await slack.slack_actions(request, background)
    assert error.value.status_code == 401 and not background.tasks
    request.headers = _headers(body, "test-secret")
    monkeypatch.setattr("mica.api.migration.admit", AsyncMock())
    monkeypatch.setattr(transfers, "Transfers", lambda *args: service)
    monkeypatch.setattr("mica.agents.analysis.storage.GCSStorage", lambda *args: service.store)
    handler = AsyncMock(return_value={})
    monkeypatch.setattr(transfer_interactions, "handle", handler)
    assert await slack.slack_actions(request, background) == {}
    handler.assert_not_awaited()
    assert len(background.tasks) == 1
    await background()
    handler.assert_awaited_once_with(service, action(p))
