"""Configured IAM approvals never inherit project/analysis admission or model authority."""

# ruff: noqa: F811
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401
from test_conversation_tools import Client, tool
from test_email_transport import sent_messages
from test_gmail_intake import email_record as intake_email
from test_gmail_intake import mail, signing  # noqa: F401

from mica.agents.analysis import approvals, conversation, email_delivery, iam_grants, lab, surface
from mica.agents.analysis import cloud_access as ca
from mica.agents.analysis.cloud_access_config import CloudAccessBundle, CloudAccessProposal
from mica.agents.analysis.config import Settings


def bundle(**changes):
    return CloudAccessBundle(
        **{
            "title": "VM access",
            "gcp_project": "test-project",
            "roles": ["roles/editor", "roles/iap.tunnelResourceAccessor"],
            "channel": "T1:CVM",
            "approver": "T1:U1",
            "approver_emails": ["alice@example.com"],
            "allow_lab_members": True,
            "guide_url": "https://example.com/guide",
            "welcome": "Start at Step 2: {guide_url}",
            **changes,
        }
    )


@pytest.fixture
def cloud(bridge, monkeypatch):
    bridge.settings.cloud_access = {"vm": bundle()}
    bridge.cloud_slack = AsyncMock()
    bridge.cloud_slack.chat_postMessage.return_value = {"channel": "DOWNER", "ts": "90.1"}
    bridge.cloud_slack.views_open.return_value = {"view": {"id": "V1"}}
    monkeypatch.setattr(ca, "client", lambda _: bridge.cloud_slack)
    monkeypatch.setattr(
        ca.channel_access,
        "profile",
        lambda app, cfg, team, user: app.slack_user_map.get(team + ":" + user),
    )
    bridge.missing = Mock(return_value={"roles/editor"})
    bridge.grant = Mock()
    bridge.notice = AsyncMock(return_value={"id": "c1", "thread_id": "beef"})
    monkeypatch.setattr(iam_grants, "missing_roles", bridge.missing)
    monkeypatch.setattr(iam_grants, "grant", bridge.grant)
    monkeypatch.setattr(email_delivery, "notice", bridge.notice)
    return bridge


def proposal(email="outsider@example.com", **changes):
    return CloudAccessProposal(bundle="vm", email=email, note="Need a lab VM", **changes)


async def pending(
    bridge,
    *,
    origin="slack",
    requester="bob@example.com",
    source="first",
    email="outsider@example.com",
):
    row = await ca.request(
        bridge,
        proposal(email),
        source,
        requester=requester,
        origin=origin,
        team="T1",
        user="U2",
        channel="CVM",
        thread="1.0",
        email_meta={
            "thread_id": "abcd",
            "message_id": "<req@example.com>",
            "subject": "VM request",
        },
    )
    return await ca.resume(bridge, bridge.store, row.id)


def click(row, action="approve", **changes):
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": row.card_channel},
        "message": {"ts": row.card_ts},
        "actions": [{"action_id": ca.PREFIX + action, "value": row.id}],
        **changes,
    }


async def decide(bridge, row, action="approve", **changes):
    background = BackgroundTasks()
    await ca.handle(bridge, bridge.store, click(row, action, **changes), background)
    await background()
    return await ca.load(bridge, row.id)


def email_record(sender="alice@example.com", text="approve", thread="beef"):
    p = {
        "team_id": "EMAIL",
        "event": {
            "user": sender,
            "channel_type": "email",
            "channel": "EMAIL",
            "ts": "1.0",
            "type": "message",
            "text": text,
        },
        "_email": {
            "mailbox": "mica@example.com",
            "id": "a1",
            "thread_id": thread,
            "subject": "VM request",
        },
    }
    _, thread_key = surface.email_keys(p, sender)
    return {"surface": "email", "intake_principal": sender, "thread_key": thread_key, "payload": p}


def test_default_off_and_strict_operator_configuration():
    assert not Settings(_env_file=None).cloud_access
    assert "request_cloud_access" not in {t["name"] for t in conversation.tools_for(False)}
    config = Settings(
        _env_file=None,
        access_request_owner_id="T1:U1",
        cloud_access={"vm": bundle(approver="").model_dump()},
    )
    assert config.cloud_access["vm"].approver == "T1:U1"
    tools = conversation.tools_for(False, config)
    schema = next(t for t in tools if t["name"] == "request_cloud_access")
    assert set(schema["input_schema"]["properties"]) == {"bundle", "email", "note"}
    assert schema["strict"]
    assert sum(len(t["description"]) for t in conversation.tools_for(True, config)
               if t["name"] != "propose_gcs_operations") <= 4800
    operation = next(t for t in conversation.tools_for(True, config)
                     if t["name"] == "propose_gcs_operations")
    assert len(operation["description"]) <= 400
    assert bundle(approver_emails=[" Alice@EXAMPLE.com "]).approver_emails == ("alice@example.com",)


@pytest.mark.parametrize(
    "changes",
    [
        {"roles": ["roles/owner"]},
        {"roles": ["roles/iam.securityAdmin"]},
        {"roles": ["roles/resourcemanager.projectIamAdmin"]},
        {"roles": ["projects/x/roles/y"]},
        {"roles": []},
        {"channel": "CVM"},
        {"approver_emails": ["a@b.com,c@d.com"]},
        {"guide_url": "javascript:alert(1)"},
        {"guide_url": "https://user:secret@example.com"},
    ],
)
def test_invalid_bundles_rejected(changes):
    with pytest.raises(ValidationError):
        bundle(**changes)


async def test_channel_intake_deduplicates_mentions_and_ignores_replies(cloud):
    p = payload(channel="CVM", channel_type="channel")
    assert await ca.receive(cloud, p)
    p["event"]["type"] = "app_mention"
    assert await ca.receive(cloud, p)
    p["event"].update(ts="2.0", thread_ts="1.0")
    assert await ca.receive(cloud, p)
    assert not await ca.receive(cloud, payload(channel="COTHER"))
    assert cloud.cloud_slack.chat_postMessage.await_count == 1
    cloud.test_client.create_job.assert_not_called()
    assert not any(k.startswith("claude-event:") for k in cloud.repository.connector_state)


async def test_form_prefills_identity_and_reports_note_error(cloud):
    await ca.receive(cloud, payload(channel="CVM"))
    action = cloud.cloud_slack.chat_postMessage.call_args.kwargs["blocks"][0]["elements"][0]
    clicker = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U2"},
        "channel": {"id": "CVM"},
        "message": {"ts": "90.1"},
        "trigger_id": "trigger",
        "actions": [action],
    }
    await ca.handle(cloud, cloud.store, clicker, BackgroundTasks())
    view = cloud.cloud_slack.views_open.call_args.kwargs["view"]
    assert view["blocks"][0]["element"]["initial_value"] == "bob@example.com"
    assert view["blocks"][1]["element"]["max_length"] == 500
    assert not view["blocks"][1].get("optional")
    submission = {
        "type": "view_submission",
        "team": {"id": "T1"},
        "user": {"id": "U2"},
        "view": {
            "id": "V1",
            "callback_id": ca.CALLBACK,
            "private_metadata": view["private_metadata"],
            "state": {
                "values": {
                    "email": {"value": {"value": "outsider@example.com"}},
                    "note": {"value": {"value": " "}},
                }
            },
        },
    }
    with pytest.raises(ca.FormError) as err:
        await ca.handle(cloud, cloud.store, submission, BackgroundTasks())
    assert approvals.modal_error(submission, err.value)["errors"] == {"note": str(err.value)}
    submission["view"]["state"]["values"]["note"]["value"]["value"] = "VM analysis"
    background = BackgroundTasks()
    await ca.handle(cloud, cloud.store, submission, background)
    await background()
    row = await ca.load(cloud, ca.digest("form:" + view["private_metadata"]))
    assert row.state == "pending" and row.target == "outsider@example.com"
    cloud.grant.assert_not_called()


async def test_lab_auto_grant_is_live_and_welcome_targets_grantee(cloud):
    cloud.store.put(lab.KEY, lab.Lab(members={"member@example.com"}).model_dump_json().encode())
    row = await pending(cloud, email="member@example.com")
    assert row.state == "granted" and row.decided_by == "lab_members"
    cloud.grant.assert_called_once()
    cloud.notice.assert_awaited_once()
    assert cloud.notice.call_args.args[2] == "member@example.com"
    assert "https://example.com/guide" in cloud.notice.call_args.args[4]
    cloud.store.put(lab.KEY, lab.Lab().model_dump_json().encode())
    row = await pending(cloud, email="member@example.com", source="new")
    assert row.state == "pending"
    assert cloud.grant.call_count == 1


async def test_allow_flag_and_approver_email_require_explicit_approval(cloud):
    cloud.store.put(lab.KEY, lab.Lab(members={"member@example.com"}).model_dump_json().encode())
    row = await pending(
        cloud, email="member@example.com", origin="email", requester="alice@example.com"
    )
    assert row.state == "pending"
    assert row.card_channel == "DOWNER"
    cloud.grant.assert_not_called()
    cloud.settings.cloud_access["vm"] = bundle(allow_lab_members=False)
    row = await pending(cloud, email="member@example.com", source="different")
    assert row.id == ca.digest("first")  # only one live pending request per target/bundle


async def test_already_granted_sends_no_welcome_or_grant(cloud):
    cloud.missing.return_value = set()
    row = await pending(cloud, origin="email")
    assert row.state == "already"
    cloud.grant.assert_not_called()
    cloud.notice.assert_not_awaited()
    cloud.cloud_slack.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"user": {"id": "U2"}},
        {"team": {"id": "T2"}},
        {"channel": {"id": "COPY"}},
        {"message": {"ts": "copied"}},
        {"container": {"channel_id": "COPY", "message_ts": "90.1"}},
    ],
)
async def test_decisions_are_bound_to_approver_and_original_card(cloud, override):
    row = await pending(cloud)
    with pytest.raises(PermissionError):
        await decide(cloud, row, **override)
    cloud.grant.assert_not_called()


async def test_approval_replay_grants_once_and_denial_never_grants(cloud):
    row = await pending(cloud)
    row = await decide(cloud, row)
    assert row.state == "granted" and row.welcome == "sent"
    await decide(cloud, row)
    await decide(cloud, row, "deny")
    assert cloud.grant.call_count == cloud.notice.await_count == 1
    other = await pending(cloud, source="other", email="denied@example.com")
    other = await decide(cloud, other, "deny")
    assert other.state == "denied"
    await decide(cloud, other)
    assert cloud.grant.call_count == 1
    assert not any(b["type"] == "actions" for b in ca.blocks(other))


async def test_pending_dedup_and_reservation_crash_recovery(cloud, monkeypatch):
    original = ca.save
    monkeypatch.setattr(ca, "save", AsyncMock(side_effect=RuntimeError("lost save")))
    with pytest.raises(RuntimeError):
        await pending(cloud)
    monkeypatch.setattr(ca, "save", original)
    row = await pending(cloud, source="retry")
    duplicate = await pending(cloud, source="third")
    assert row.id == duplicate.id == ca.digest("first")
    assert cloud.cloud_slack.chat_postMessage.await_count == 1


async def test_expiry_and_configuration_change_block_grants(cloud):
    row = await pending(cloud)
    row.expires = time.time() - 1
    await ca.save(cloud, row)
    with pytest.raises(ValueError, match="expired"):
        await decide(cloud, row)
    await ca.expire(cloud, cloud.store, row.id)
    assert (await ca.load(cloud, row.id)).state == "expired"
    assert "Request again" not in json.dumps(ca.blocks(await ca.load(cloud, row.id)))
    other = await pending(cloud, source="new", email="new@example.com")
    cloud.settings.cloud_access["vm"] = bundle(roles=["roles/viewer"])
    with pytest.raises(ValueError, match="changed"):
        await decide(cloud, other)
    await ca.resume(cloud, cloud.store, other.id)
    assert (await ca.load(cloud, other.id)).state == "grant_failed"
    cloud.grant.assert_not_called()


async def test_grant_commit_resumes_welcome_after_crash(cloud):
    row = await pending(cloud)
    cloud.notice.side_effect = TimeoutError("ambiguous")
    with pytest.raises(TimeoutError):
        await decide(cloud, row)
    assert (await ca.load(cloud, row.id)).state == "granted"
    cloud.notice.side_effect = email_delivery.UnconfirmedDelivery("held")
    row = await ca.resume(cloud, cloud.store, row.id)
    assert row.state == "granted" and row.welcome == "unknown"
    assert "unconfirmed" in ca.outcome(row)
    cloud.grant.assert_called_once()


async def test_failed_grant_has_no_welcome(cloud):
    cloud.grant.side_effect = iam_grants.GrantFailed("Organization blocks this account")
    row = await decide(cloud, await pending(cloud))
    assert row.state == "grant_failed" and "Organization" in ca.outcome(row)
    cloud.notice.assert_not_awaited()


async def test_email_target_must_be_sender_or_verbatim_address(cloud):
    record = email_record("bob@example.com", "Give access to my friend")
    answer = await ca.propose(cloud, cloud.store, record, "first", proposal())
    assert "Include the full address" in answer
    assert not cloud.repository.connector_state
    record["payload"]["_email"]["original_text"] = "Forwarded: grant outsider@example.com VM access"
    answer = await ca.propose(cloud, cloud.store, record, "first", proposal())
    assert "for approval" in answer
    row = await ca.load(cloud, ca.digest("email:first"))
    assert row.requester == "bob@example.com" and row.target == "outsider@example.com"
    assert cloud.notice.call_args.args[2] == "alice@example.com"
    assert cloud.cloud_slack.chat_postMessage.call_args.kwargs["channel"] == "U1"
    assert "thread_ts" not in cloud.cloud_slack.chat_postMessage.call_args.kwargs


@pytest.mark.parametrize(
    "text", ["outsider@example.com.evil", "a.outsider@example.com", "outsider@example.com@evil.org"]
)
async def test_email_target_substrings_do_not_authorize(cloud, text):
    assert "Include the full address" in await ca.propose(
        cloud, cloud.store, email_record("bob@example.com", text), "first", proposal()
    )
    cloud.grant.assert_not_called()


@pytest.mark.parametrize(
    "sender,text,thread",
    [
        ("outsider@example.com", "approve", "beef"),
        ("alice@example.com", "approve", "dead"),
        ("alice@example.com", "> approve", "beef"),
        ("alice@example.com", "approve please", "beef"),
        ("alice@example.com", "Here is the text:\napprove", "beef"),
        ("alice@example.com", "On Tuesday Mica wrote:\n> approve", "beef"),
    ],
)
async def test_email_decision_rejects_other_sender_thread_or_quoted_command(
    cloud, sender, text, thread
):
    await pending(cloud, origin="email")
    assert await ca.email_decision(cloud, cloud.store, email_record(sender, text, thread)) is None
    cloud.grant.assert_not_called()


@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_exact_verified_email_decision_and_replay(cloud, action):
    row = await pending(cloud, origin="email")
    record = email_record(text="\n" + action + "\n\nOn Tuesday Mica wrote:\n> request")
    answer = await ca.email_decision(cloud, cloud.store, record)
    assert ("Granted" if action == "approve" else "declined") in answer
    await ca.email_decision(cloud, cloud.store, record)
    assert cloud.grant.call_count == (1 if action == "approve" else 0)
    assert cloud.notice.await_count == (3 if action == "approve" else 2)
    assert (await ca.load(cloud, row.id)).notification == "sent"


async def test_ambiguous_approval_email_is_flagged_and_not_actionable(cloud):
    cloud.notice.side_effect = email_delivery.UnconfirmedDelivery("held")
    row = await pending(cloud, origin="email")
    assert "held for operator review" in json.dumps(ca.blocks(row))
    assert await ca.email_decision(cloud, cloud.store, email_record()) is None
    cloud.grant.assert_not_called()


async def test_slack_tool_only_routes_to_form_and_model_schema(cloud, monkeypatch):
    answer = await ca.propose(cloud, cloud.store, {}, "a", proposal())
    assert "<#CVM>" in answer
    assert not cloud.repository.connector_state
    event_id = await cloud.accept(payload(user="U2", text="Need VM access", channel="D2"))
    record = await cloud.repository.get_connector_state("claude-event:" + event_id)
    from mica.agents.analysis import access

    grant = access.resolve(cloud.app, cloud.settings, cloud.store, record)
    sdk = Client(tool("request_cloud_access", **proposal().model_dump()))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **_: sdk)
    decision = await CONVERSATION_RUN(
        cloud, event_id, record, cloud.store, grant, {}, [], client=sdk
    )
    assert decision.route == "cloud_access"
    assert decision.cloud_access_request == proposal()
    cloud.test_client.create_job.assert_not_called()


async def test_intake_preempts_analysis_identity_and_accept(cloud, monkeypatch):
    from types import SimpleNamespace

    from mica.api import slack_analysis

    monkeypatch.setattr(slack_analysis, "BatchBridge", lambda *args: cloud)
    monkeypatch.setattr(
        cloud, "accept", AsyncMock(side_effect=AssertionError("No analysis intake"))
    )
    monkeypatch.setattr(
        slack_analysis.identity,
        "resolve_email",
        Mock(side_effect=AssertionError("No analysis admission")),
    )
    services = SimpleNamespace(settings=cloud.app, repository=cloud.repository)
    result = await slack_analysis.receive(
        services, payload(channel="CVM", channel_type="channel", user="UNKNOWN")
    )
    assert result == {"ok": True}
    cloud.accept.assert_not_awaited()


async def test_direct_routes_nonmember_email_and_approval_before_conversation(cloud, monkeypatch):
    from mica.agents.analysis import direct

    record = email_record("bob@example.com", "VM access for outsider@example.com", "abcd")
    record.update(status="pending", attempt=0)
    decision = conversation.Decision(
        route="cloud_access", reply="Access request prepared", cloud_access_request=proposal()
    )
    monkeypatch.setattr(conversation, "run", AsyncMock(return_value=decision))
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(direct, "finish", finish)
    assert await direct.handle(cloud, "first", record, cloud.store)
    assert "for approval" in finish.call_args.args[3]
    cloud.grant.assert_not_called()
    reply = email_record(text="approve")
    monkeypatch.setattr(
        conversation, "run", AsyncMock(side_effect=AssertionError("No model for approval"))
    )
    from mica.agents.analysis.registry import Registry

    monkeypatch.setattr(Registry, "load", Mock(side_effect=AssertionError("No project lookup")))
    assert await direct.handle(cloud, "approval", reply, cloud.store)
    assert "Granted" in finish.call_args.args[3]
    assert reply["reply_scope"] == "cloud_access"
    cloud.grant.assert_called_once()


@pytest.mark.parametrize("existing_thread", [False, True])
async def test_member_cloud_request_has_no_default_project_history(
    cloud, mail, monkeypatch, existing_thread
):
    from mica.agents.analysis import access, memory_tasks
    from mica.agents.analysis.registry import Registry

    eid, record = await intake_email(mail)
    text = "Please give outsider@example.com VM access."
    record["payload"]["event"]["text"] = text
    record["payload"]["_email"]["original_text"] = text
    record["payload"]["_email"]["recipients"] = ["alice@example.com", "bob@example.com"]
    Registry(mail.store).update(
        "share", "alice@example.com", "share",
        lambda d: d.share(mail.pid, "alice@example.com", "bob@example.com"),
    )
    registry_before = Registry(mail.store).load().model_dump()
    assert access.resolve(mail.app, mail.settings, mail.store, record).project_id == mail.pid
    path = access.checkpoint_key(record["thread_key"])
    if existing_thread:
        grant = access.resolve(mail.app, mail.settings, mail.store, record)
        state = {
            "private_owner": "alice@example.com", "conversation_origin_event": "earlier",
            "context": {
                "project_slug": mail.pid, "workdir": "/tmp/existing",
                "data_path": str(mail.settings.data_mount_root / "example-bucket/data"),
                "thread_ts": "1.0",
            },
            "turn": 1, "messages": [{"id": "earlier", "user": "Analyze", "assistant": "Done"}],
        }
        from urllib.parse import urlsplit

        uri = urlsplit(grant.project.data_uri)
        state["context"]["data_path"] = str(
            mail.settings.data_mount_root / uri.netloc / uri.path.lstrip("/")
        )
        mail.store.put(path, json.dumps(state).encode())
        checkpoint_before = mail.store.get(path)[0]
    sdk = Client(tool(
        "request_cloud_access", bundle="vm", email="outsider@example.com",
        note="Access for the default Mica project",
    ))
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **_: sdk)
    await mail.repository.set_connector_state("claude-event:" + eid, record)
    assert await mail.process(eid, 0) == "complete"
    record = await mail.repository.get_connector_state("claude-event:" + eid)
    assert record["reply_scope"] == "cloud_access"
    assert not set(record) & {
        "project_id", "project_name", "memory_project_id", "memory_reply", "memory_due",
        "failure_project_id", "failure_project_name",
    }
    assert not memory_tasks.eligible(mail, record)
    assert not memory_tasks.conversation_turn(record)
    assert Registry(mail.store).load().model_dump() == registry_before
    if existing_thread:
        assert mail.store.get(path)[0] == checkpoint_before
    else:
        assert json.loads(mail.store.get(path)[0]) == {"private_owner": "alice@example.com"}
    row = await ca.load(mail, ca.digest("email:" + eid))
    assert row.note == text
    assert row.policy.gcp_project == "test-project"
    assert len(sent_messages(mail)) == 1
    assert sent_messages(mail)[0]["To"] == "alice@example.com"
    assert "for approval" in sent_messages(mail)[0].get_content()
    # Retry the saved cloud decision and deliver with the Mica registry unavailable.
    monkeypatch.setattr(Registry, "load", Mock(side_effect=AssertionError("No project lookup")))
    from mica.agents.analysis import direct

    assert await direct.handle(mail, eid, record, mail.store)
    await email_delivery.deliver(mail, "new-delivery", record, "Cloud result", kind="final")
    assert sent_messages(mail)[-1]["To"] == "alice@example.com"
    assert sent_messages(mail)[-1].get_content().strip() == "Cloud result"
    invalid = {**record, "intake_principal": "forged@example.com"}
    with pytest.raises(PermissionError, match="sender verification"):
        await email_delivery.deliver(mail, "forged-delivery", invalid, "No", kind="final")


async def test_lab_preapproval_disabled_always_asks(cloud):
    cloud.settings.cloud_access["vm"] = bundle(allow_lab_members=False)
    cloud.store.put(lab.KEY, lab.Lab(members={"member@example.com"}).model_dump_json().encode())
    assert (await pending(cloud, email="member@example.com")).state == "pending"
    cloud.grant.assert_not_called()


async def test_duplicate_submission_cannot_reopen_after_decision(cloud):
    row = await pending(cloud)
    duplicate = await pending(cloud, source="duplicate", requester="alice@example.com")
    assert duplicate.id == row.id
    await decide(cloud, row, "deny")
    replay = await pending(cloud, source="duplicate", requester="alice@example.com")
    assert replay.id == row.id and replay.state == "denied"
    cloud.grant.assert_not_called()


async def test_approved_work_stays_reserved_until_its_grant_finishes(cloud):
    row = await pending(cloud)
    await ca.decide(cloud, cloud.store, row, "T1:U1", True)
    row.expires = time.time() - 1
    await ca.save(cloud, row)
    retry = await ca.request(
        cloud, proposal(), "second", requester="alice@example.com", origin="email"
    )
    assert retry.id == row.id and retry.state == "approved"
