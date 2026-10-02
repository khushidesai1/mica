# ruff: noqa: F811
"""Offline onboarding and admission tests through the signed-webhook receiver boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import access, direct, identity
from pistachio.agents.analysis import channel_access as ca
from pistachio.agents.analysis.config import Settings
from pistachio.api import slack_analysis


@pytest.fixture
def channels(chat, monkeypatch):
    chat.settings.access_mode = "channel"
    chat.settings.access_admin_ids = "T1:U1"
    chat.settings.access_cache_seconds = 0
    chat.app.slack_bot_token = "fake-onboarding-token"
    chat.app.anthropic_api_key = SecretStr("fake-test-key")
    chat.settings.slack_bot_token = SecretStr(chat.app.slack_bot_token)
    ca._CACHE.clear()
    api = Mock()
    people = {"CACCESS1": ["UBOT", "U1", "U2"], "CACCESS2": ["UBOT", "U1", "U2"]}
    profiles = {"U1": "alice@example.com", "U2": "bob@example.com", "U3": "carol@example.com"}
    api.auth_test.return_value = {"ok": True, "team_id": "T1", "user_id": "UBOT"}
    api.conversations_info.return_value = {"ok": True, "channel": {"is_member": True}}
    api.conversations_members.side_effect = lambda channel, **kwargs: {
        "ok": True,
        "members": list(people[channel]),
        "response_metadata": {"next_cursor": ""},
    }
    api.users_info.side_effect = lambda user: {
        "ok": True,
        "user": {"id": user, "profile": {"email": profiles.get(user, "")}},
    }
    api.users_lookupByEmail.side_effect = lambda email: {
        "ok": True,
        "user": {"id": next(uid for uid, e in profiles.items() if e == email)},
    }
    monkeypatch.setattr(ca, "WebClient", lambda **kwargs: api)
    chat.member_api, chat.members, chat.profiles = api, people, profiles
    return chat


async def receive(chat, body):
    return await slack_analysis.receive(
        SimpleNamespace(settings=chat.app, repository=chat.repository), body
    )


async def onboard(chat, channel="CACCESS1", command="enable this channel", user="U1", ts="1.0"):
    return await receive(
        chat,
        payload(
            ts,
            text=f"<@UBOT> {command}",
            channel=channel,
            user=user,
            kind="app_mention",
            channel_type="channel",
        ),
    )


async def test_default_denies_until_admin_onboards_and_inviting_alone_cannot_grant(channels):
    assert Settings(_env_file=None).access_mode == "channel"
    assert await receive(channels, payload()) == {"ok": True}
    channels.decision.assert_not_awaited()
    await onboard(channels, user="U2", ts="0.0")
    assert not ca.load(channels.store).channels
    channels.member_api.conversations_info.assert_not_called()
    await onboard(channels)
    assert ca.load(channels.store).channels["T1:CACCESS1"].enabled
    # Channel onboarding does not publish or share the existing private project.
    grant = access.resolve(
        channels.app,
        channels.settings,
        channels.store,
        {"payload": payload(channel="CACCESS1", channel_type="channel"), "thread_key": "empty"},
    )
    assert not grant.allowed


async def test_any_enabled_channel_admits_without_manual_maps_or_email_allowlist(channels):
    await onboard(channels)
    await onboard(channels, "CACCESS2", ts="2.0")
    channels.app.slack_user_map = {}
    channels.app.allowed_emails = ("unrelated@example.com",)
    channels.settings.allowed_user_ids = "U99"
    assert (
        identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U2")
        == "bob@example.com"
    )
    channels.members["CACCESS1"].remove("U2")
    assert (
        identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U2")
        == "bob@example.com"
    )
    await onboard(channels, "CACCESS2", "disable this channel", ts="3.0")
    assert (
        identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U2") is None
    )
    # A new member becomes eligible, retaining an email-based personal memory identity.
    channels.members["CACCESS1"].append("U3")
    assert (
        identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U3")
        == "carol@example.com"
    )


async def test_member_cache_expires_fails_closed_and_disable_bypasses_cache(channels, monkeypatch):
    await onboard(channels)
    channels.settings.access_cache_seconds = 30
    clock = [100.0]
    monkeypatch.setattr(ca, "monotonic", lambda: clock[0])

    def check():
        return identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U2")

    assert check() == "bob@example.com"
    channels.members["CACCESS1"].remove("U2")
    clock[0] = 129
    assert check() == "bob@example.com"
    clock[0] = 131
    channels.member_api.conversations_members.side_effect = RuntimeError("Slack unavailable")
    assert check() is None
    channels.member_api.conversations_members.side_effect = lambda **kwargs: {
        "ok": True,
        "members": ["U2"],
    }
    assert check() == "bob@example.com"
    await onboard(channels, command="disable this channel", ts="3.0")
    assert check() is None


async def test_old_enable_replay_cannot_undo_disable_and_replies_are_deduplicated(channels):
    await onboard(channels)
    before = channels.test_slack.chat_postMessage.await_count
    await onboard(channels)
    assert channels.test_slack.chat_postMessage.await_count == before
    await onboard(channels, command="disable this channel", ts="2.0")
    event_id = next(iter(ca.load(channels.store).operations))
    body = payload(
        text="<@UBOT> enable this channel",
        channel="CACCESS1",
        kind="app_mention",
        channel_type="channel",
    )
    result = ca.admin_command(channels.app, channels.settings, channels.store, body, event_id)
    assert "disabled" in result
    assert not ca.load(channels.store).channels["T1:CACCESS1"].enabled


async def test_queued_jobs_and_workers_recheck_channel_membership(channels):
    await onboard(channels)
    channels.decision.return_value = direct.Decision(route="compute", reply="Computing")
    intake = await receive(channels, payload("2.0"))
    event_id = intake["event_id"]
    assert await channels.process(event_id, 0) == "running"
    record = await channels.repository.get_connector_state("claude-event:" + event_id)
    grant = access.resolve(channels.app, channels.settings, channels.store, record)
    import json

    request = json.loads(
        channels.store.get(f"tasks/slack-analysis/events/{event_id}/request.json")[0]
    )
    assert access.worker_permitted(channels.store, request, grant.state, settings=channels.settings)
    channels.members["CACCESS1"].remove("U1")
    assert not access.worker_permitted(
        channels.store, request, grant.state, settings=channels.settings
    )
    channels.members["CACCESS1"].append("U1")
    queued = await receive(channels, payload("3.0", "2.0", "Next plot"))
    channels.members["CACCESS1"].remove("U1")
    record["status"] = "complete"
    await channels.repository.set_connector_state("claude-event:" + event_id, record)
    before = channels.test_client.create_job.call_count
    assert await channels.process(queued["event_id"], 0) == "complete"
    assert channels.test_client.create_job.call_count == before
    assert channels.decision.await_count == 1


async def test_profiles_require_correct_workspace_human_and_readable_email(channels):
    await onboard(channels)
    channels.profiles["U2"] = ""
    assert ca.email_for(channels.app, channels.settings, channels.store, "T1", "U2") is None
    channels.profiles["U2"] = "bob@example.com"
    channels.member_api.auth_test.return_value["team_id"] = "T_OTHER"
    assert ca.email_for(channels.app, channels.settings, channels.store, "T1", "U2") is None
    channels.member_api.auth_test.return_value["team_id"] = "T1"
    for field in ("deleted", "is_bot", "is_app_user"):
        channels.member_api.users_info.side_effect = lambda user, field=field: {
            "ok": True,
            "user": {"id": user, field: True, "profile": {"email": "bob@example.com"}},
        }
        assert ca.email_for(channels.app, channels.settings, channels.store, "T1", "U2") is None


async def test_share_target_must_be_enabled_but_unshare_can_remove_former_member(channels):
    await onboard(channels)
    assert (
        ca.target_email(channels.app, channels.settings, channels.store, "T1", "<@U2>")
        == "bob@example.com"
    )
    assert (
        ca.target_email(
            channels.app, channels.settings, channels.store, "T1", "<mailto:bob@example.com|Bob>"
        )
        == "bob@example.com"
    )
    channels.members["CACCESS1"].remove("U2")
    with pytest.raises(ValueError):
        ca.target_email(channels.app, channels.settings, channels.store, "T1", "<@U2>")
    assert (
        ca.target_email(
            channels.app, channels.settings, channels.store, "T1", "bob@example.com", removing=True
        )
        == "bob@example.com"
    )


def test_membership_pagination_is_complete_and_repeating_cursors_fail_closed():
    api = Mock()
    api.conversations_members.side_effect = [
        {"ok": True, "members": ["U1"], "response_metadata": {"next_cursor": "next"}},
        {"ok": True, "members": ["U2"]},
    ]
    assert ca.members(api, "C1") == {"U1", "U2"}
    assert api.conversations_members.call_args.kwargs["cursor"] == "next"
    api.conversations_members.side_effect = None
    api.conversations_members.return_value = {
        "ok": True,
        "members": ["U1"],
        "response_metadata": {"next_cursor": "same"},
    }
    with pytest.raises(ValueError, match="incomplete"):
        ca.members(api, "C1")


async def test_intake_identity_cannot_switch_while_queued(channels):
    await onboard(channels)
    event = await receive(channels, payload("2.0"))
    channels.profiles["U1"] = "other@example.com"
    record = await channels.repository.get_connector_state("claude-event:" + event["event_id"])
    grant = access.resolve(channels.app, channels.settings, channels.store, record)
    assert not grant.email and not grant.state
    channels.decision.assert_not_awaited()


async def test_unavailable_channel_does_not_override_membership_in_another(channels):
    await onboard(channels)
    await onboard(channels, "CACCESS2", ts="2.0")

    def fetch(channel, **kwargs):
        if channel == "CACCESS1":
            raise RuntimeError("not_in_channel")
        return {"ok": True, "members": ["U2"]}

    channels.member_api.conversations_members.side_effect = fetch
    assert (
        ca.email_for(channels.app, channels.settings, channels.store, "T1", "U2")
        == "bob@example.com"
    )
    assert ca.email_for(channels.app, channels.settings, channels.store, "T1", "U3") is None


async def test_onboarding_verifies_bot_invitation_email_permissions_and_admin_listing(channels):
    channels.member_api.conversations_info.return_value["channel"]["is_member"] = False
    await onboard(channels)
    assert not ca.load(channels.store).channels
    channels.member_api.conversations_info.return_value["channel"]["is_member"] = True
    channels.profiles["U1"] = ""
    await onboard(channels, ts="2.0")
    assert not ca.load(channels.store).channels
    channels.profiles["U1"] = "alice@example.com"
    await onboard(channels, ts="3.0")
    await receive(channels, payload("4.0", text="agent channels"))
    assert "<#CACCESS1>" in channels.test_slack.chat_postMessage.call_args.kwargs["text"]
    await receive(channels, payload("5.0", text="agent channels", user="U2", channel="D2"))
    assert "<#CACCESS1>" not in channels.test_slack.chat_postMessage.call_args.kwargs["text"]
