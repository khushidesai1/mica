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
    await slack_analysis.receive(
        services(bridge),
        payload(
            channel="C1",
            user="UNKNOWN",
            channel_type="channel",
        ),
    )
    assert reply.await_count + bridge.test_slack.chat_postMessage.await_count == 0


async def test_unmapped_mention_and_copy_get_one_threaded_reply(bridge, monkeypatch):
    reply = AsyncMock()
    monkeypatch.setattr("pistachio.connectors.slack._reply", reply)
    body = payload(
        channel="C1",
        user="UNKNOWN",
        kind="app_mention",
        channel_type="channel",
        text="<@BOT> hello",
    )
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


async def test_reused_name_never_inherits_other_users_brief(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *a: {"entries": []})

    async def decide(key, model, context, **kwargs):
        return direct.Decision(route="reply", reply="Draft ready.", guide=context["brief"])

    chat.decision.side_effect = decide
    from pistachio.agents.analysis import knowledge

    knowledge.commit(
        chat.store,
        chat.pid,
        {"PROJECT.md": b"ALICE-SECRET"},
        knowledge.load(chat.store, chat.pid, chat.settings),
        chat.settings,
        event_id="secret",
        user_key="alice",
    )
    for body in [
        payload(text="init brain gs://lab-data/brain/ --force"),
        payload("2.0", text="init liver gs://lab-data/bob/", channel="D2", user="U2"),
    ]:
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


async def message(chat, text, ts, *, user="U1", channel="D1", thread=None):
    body = payload(
        ts,
        thread,
        text,
        channel,
        user=user,
        kind="message" if channel.startswith("D") else "app_mention",
        channel_type="im" if channel.startswith("D") else "channel",
    )
    response = await slack_analysis.receive(services(chat), body)
    await chat.process(response["event_id"], 0)
    return await chat.repository.get_connector_state("claude-event:" + response["event_id"])


@pytest.mark.parametrize("command", ["brief", "save brief", "change the brief", "cancel"])
async def test_nonmember_channel_commands_never_read_or_compute(chat, command):
    await message(chat, "publish liver <#C1>", "10.0")
    await message(chat, "use liver", "11.0", channel="C1")
    await message(chat, "Describe project", "12.0", channel="C1")
    calls = chat.decision.await_count
    result = await message(chat, command, "13.0", user="U2", channel="C1", thread="12.0")
    assert "private project" in result["notice_text"]
    assert "Species" not in result["notice_text"]
    assert chat.decision.await_count == calls
    chat.test_client.delete_job.assert_not_called()


async def test_share_use_unshare_and_natural_language_commands(chat):
    await message(chat, "share liver <@U2>", "10.0")
    await message(chat, "use liver", "11.0", user="U2", channel="D2")
    await message(chat, "use log scale", "12.0", user="U2", channel="D2")
    assert chat.decision.call_args.args[2]["request"] == "use log scale"
    await message(chat, "unshare liver <mailto:bob@example.com|bob@example.com>", "13.0")
    calls = chat.decision.await_count
    result = await message(chat, "brief", "14.0", user="U2", channel="D2", thread="12.0")
    assert "private project" in result["notice_text"]
    assert chat.decision.await_count == calls


async def test_channel_publication_is_explicit_and_revocable(chat):
    result = await message(chat, "use liver", "10.0", channel="C1")
    assert "publish" in result["notice_text"]
    await message(chat, "publish liver <#C1>", "11.0")
    await message(chat, "use liver", "12.0", channel="C1")
    await message(chat, "Describe project", "13.0", channel="C1")
    calls = chat.decision.await_count
    await message(chat, "unpublish liver <#C1>", "14.0")
    result = await message(chat, "Describe project", "15.0", channel="C1", thread="13.0")
    assert "unpublished" in result["notice_text"]
    assert chat.decision.await_count == calls


async def test_worker_grant_rechecks_membership_and_protocol(chat):
    import json

    from pistachio.agents.analysis.access import worker_permitted
    from pistachio.agents.analysis.config import CHECKPOINT_PREFIX
    from pistachio.agents.analysis.registry import Registry

    await message(chat, "share liver <@U2>", "10.0")
    await message(chat, "use liver", "11.0", user="U2", channel="D2")
    chat.decision.return_value = direct.Decision(route="compute", reply="Compute.")
    body = payload("12.0", text="plot", user="U2", channel="D2")
    event_id = (await slack_analysis.receive(services(chat), body))["event_id"]
    await chat.process(event_id, 0)
    request = json.loads(chat.store.get(f"{CHECKPOINT_PREFIX}/events/{event_id}/request.json")[0])
    state = json.loads(
        chat.store.get(f"projects/_threads/{request['thread_key']}/checkpoint.json")[0]
    )
    assert worker_permitted(chat.store, request, state)
    assert request["run_prefix"].startswith("projects/_users/")
    for change in ({"principal": ""}, {"project_id": "other"}, {"protocol": 1}, {"private": False}):
        assert not worker_permitted(chat.store, {**request, **change}, state)
    Registry(chat.store).update(
        "revoke",
        "alice@example.com",
        "unshare",
        lambda d: d.share(chat.pid, "alice@example.com", "bob@example.com", remove=True),
    )
    assert not worker_permitted(chat.store, request, state)


async def test_queued_compute_denied_after_unshare(chat):
    await message(chat, "share liver <@U2>", "10.0")
    await message(chat, "use liver", "11.0", user="U2", channel="D2")
    chat.settings.max_concurrent_runs = 1
    await chat.repository.begin_event("claude-slot:0", lease_seconds=10800)
    chat.decision.return_value = direct.Decision(route="compute", reply="Compute.")
    body = payload("12.0", text="plot", user="U2", channel="D2")
    event_id = (await slack_analysis.receive(services(chat), body))["event_id"]
    assert await chat.process(event_id, 0) == "pending"
    await message(chat, "unshare liver <@U2>", "13.0")
    assert await chat.process(event_id, 1) == "failed"
    chat.test_client.create_job.assert_not_called()
