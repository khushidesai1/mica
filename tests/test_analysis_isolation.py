"""Cross-user regressions exercised through signed-webhook payload handling."""
# ruff: noqa: F811
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import direct
from pistachio.api import slack_analysis


def services(bridge):
    bridge.app.anthropic_api_key = SecretStr("offline-test")
    return SimpleNamespace(settings=bridge.app, repository=bridge.repository)


async def test_unmapped_channel_chatter_is_silent(bridge, monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr("pistachio.connectors.slack._reply", reply)
    await slack_analysis.receive(services(bridge), payload(
        channel="C1", user="UNKNOWN", channel_type="channel",
    ))
    assert reply.await_count + bridge.test_slack.chat_postMessage.await_count == 0


async def test_unmapped_mention_and_copy_get_one_threaded_reply(bridge, monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr("pistachio.connectors.slack._reply", reply)
    body = payload(channel="C1", user="UNKNOWN", kind="app_mention",
                   channel_type="channel", text="<@BOT> hello")
    body["authorizations"] = [{"is_bot": True, "user_id": "BOT"}]
    await slack_analysis.receive(services(bridge), body)
    body["event"]["type"] = "message"
    await slack_analysis.receive(services(bridge), body)
    assert reply.await_count == 0
    bridge.test_slack.chat_postMessage.assert_awaited_once()
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["thread_ts"] == "1.0"


async def test_slack_email_allowlist_is_enforced(bridge):
    bridge.app.allowed_emails = ("alice@example.com",)
    response = await slack_analysis.receive(services(bridge), payload(user="U2", channel="D2"))
    assert "event_id" not in response
    assert not any(k.startswith("claude-event:") for k in bridge.repository.connector_state)


@pytest.mark.xfail(strict=True, reason="M3b: private project identifiers")
async def test_reused_name_never_inherits_other_users_brief(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *a: {"entries": []})
    async def decide(key, model, context):
        return direct.Decision(route="reply", reply="Draft ready.", draft=context["brief"])
    chat.decision.side_effect = decide
    chat.projects.save_brief("liver", b"ALICE-SECRET")
    for body in [payload(text="init brain gs://lab-data/brain/ --force"),
                 payload("2.0", text="init liver gs://lab-data/bob/", channel="D2", user="U2")]:
        response = await slack_analysis.receive(services(chat), body)
        await chat.process(response["event_id"], 0)
    assert "ALICE-SECRET" not in str(chat.decision.call_args)


@pytest.mark.parametrize("user,allowed", [("UNKNOWN", ""), ("U1", "U2")])
async def test_disabled_dm_is_threaded_and_deduplicated(bridge, user, allowed):
    bridge.settings.allowed_user_ids = allowed
    body = payload(user=user)
    await slack_analysis.receive(services(bridge), body)
    await slack_analysis.receive(services(bridge), body)
    bridge.test_slack.chat_postMessage.assert_awaited_once()
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["thread_ts"] == "1.0"
