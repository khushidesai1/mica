"""Amendment choices preserve queue ordering and race safely with completion."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import amendments
from mica.agents.analysis.config import Settings
from mica.agents.analysis.storage import LocalStorage


async def start(agent):
    agent.settings.amendments = True
    first = await agent.accept(payload(), principal="alice@example.com")
    assert await agent.process(first, 0) == "running"
    second = await agent.accept(
        payload("2.0", "1.0", text="Also label the axes"), principal="alice@example.com"
    )
    assert await agent.process(second, 0) == "pending"
    return first, second, await agent.repository.get_connector_state("claude-event:" + second)


def click(event_id, record, action="add"):
    return dict(
        type="block_actions",
        team={"id": "T1"},
        user={"id": "U1"},
        channel={"id": "D1"},
        message={"ts": record["notice_ts"]},
        actions=[{"action_id": amendments.ACTION_PREFIX + action, "value": event_id}],
    )


async def finish(agent, event_id):
    record = await agent.repository.get_connector_state("claude-event:" + event_id)
    agent.test_states[record["job"]] = "SUCCEEDED"
    agent.store.put(
        f"tasks/slack-analysis/events/{event_id}/attempt-a0.json",
        json.dumps(
            {"event_id": event_id, "attempt": 0, "outcome": "complete", "model_cost": 0.1}
        ).encode(),
    )
    assert await agent.process(event_id, record["tick"]) == "complete"


async def test_add_consumes_once_and_third_cannot_overtake_running_task(bridge):  # noqa: F811
    first, second, record = await start(bridge)
    content = bridge.test_slack.chat_update.call_args.kwargs["blocks"]
    assert [b["text"]["text"] for b in content[-1]["elements"]] == [
        "Add this to it",
        "Keep as a follow-up",
    ]
    third = await bridge.accept(payload("3.0", "1.0"), principal="alice@example.com")
    await amendments.handle(bridge, bridge.store, click(second, record))
    await amendments.handle(bridge, bridge.store, click(second, record))
    saved = await bridge.repository.get_connector_state("claude-event:" + second)
    assert saved["status"] == "pending" and saved["previous"] == first
    assert len(amendments.load(bridge.store, first).items) == 1
    parent = await bridge.repository.get_connector_state("claude-event:" + first)
    text = amendments.consume(bridge.store, first, parent["principal"], parent["access_signature"])
    assert "Also label the axes" in text
    assert (
        amendments.consume(bridge.store, first, parent["principal"], parent["access_signature"])
        == ""
    )
    assert await bridge.process(second, saved["tick"]) == "pending"
    assert await bridge.process(third, 0) == "pending"
    assert bridge.test_client.create_job.call_count == 1
    await finish(bridge, first)
    saved = await bridge.repository.get_connector_state("claude-event:" + second)
    assert await bridge.process(second, saved["tick"]) == "complete"
    saved = await bridge.repository.get_connector_state("claude-event:" + second)
    assert saved["consumed_into"] == first
    assert await bridge.process(third, 1) == "running"
    assert bridge.test_client.create_job.call_count == 2


@pytest.mark.parametrize("choice", [None, "follow", "late", "unread"])
async def test_follow_up_default_and_unread_fallback_keep_order(bridge, choice):  # noqa: F811
    first, second, record = await start(bridge)
    if choice in {"follow", "unread"}:
        await amendments.handle(
            bridge, bridge.store, click(second, record, "follow" if choice == "follow" else "add")
        )
    assert await bridge.process(second, 1) == "pending"
    assert bridge.test_client.create_job.call_count == 1
    await finish(bridge, first)
    if choice == "late":
        await amendments.handle(bridge, bridge.store, click(second, record))
        saved = await bridge.repository.get_connector_state("claude-event:" + second)
        assert saved["notice_text"] == amendments.LATE
    assert await bridge.process(second, 2) == "running"
    assert bridge.test_client.create_job.call_count == 2
    saved = await bridge.repository.get_connector_state("claude-event:" + second)
    assert saved["previous"] == first and not saved.get("consumed_into")
    assert amendments.consume(bridge.store, first, "alice@example.com", "anything") == ""
    assert not any(
        block.get("type") == "actions"
        for block in bridge.test_slack.chat_update.call_args.kwargs.get("blocks", [])
    )


@pytest.mark.parametrize("reason", ["off", "old_worker", "reply", "other_user", "email", "files"])
async def test_ineligible_messages_never_offer_buttons(bridge, reason):  # noqa: F811
    first, _, _ = await start(bridge)
    parent = await bridge.repository.get_connector_state("claude-event:" + first)
    record = dict(
        payload=payload("3.0", "1.0"),
        intake_principal="alice@example.com",
        thread_key=parent["thread_key"],
    )
    if reason == "off":
        bridge.settings.amendments = False
    elif reason == "old_worker":
        parent.pop("amendments_enabled")
    elif reason == "reply":
        parent["route"] = "reply"
    elif reason == "other_user":
        record["intake_principal"] = "bob@example.com"
    elif reason == "email":
        record["surface"] = "email"
    else:
        record["payload"]["event"]["files"] = [{"id": "F1"}]
    assert not amendments.eligible(bridge, record, parent)


@pytest.mark.parametrize("field", ["team", "user", "channel", "message"])
async def test_click_identity_is_bound_to_original_message(bridge, field):  # noqa: F811
    first, second, record = await start(bridge)
    action = click(second, record)
    action[field] = {"ts" if field == "message" else "id": "other"}
    with pytest.raises(ValueError, match="Only the requester"):
        await amendments.handle(bridge, bridge.store, action)
    assert not amendments.load(bridge.store, first).items


async def test_access_changes_fail_closed(bridge):  # noqa: F811
    first, second, record = await start(bridge)
    bridge.app.slack_user_map["T1:U1"] = "bob@example.com"
    with pytest.raises(ValueError, match="access changed"):
        await amendments.handle(bridge, bridge.store, click(second, record))
    assert not amendments.load(bridge.store, first).items


async def test_commands_cannot_be_amended(bridge, monkeypatch):  # noqa: F811
    first, _, _ = await start(bridge)
    # Test the handler against a forged action for a classified command, even though no
    # interactive buttons would be rendered for it.
    second = await bridge.accept(payload("4.0", "1.0"), principal="alice@example.com")
    record = await bridge.repository.get_connector_state("claude-event:" + second)
    record.update(amendable=first, notice_ts="9.0", conversation_amendable=False)
    await bridge.repository.set_connector_state("claude-event:" + second, record)
    with pytest.raises(ValueError, match="Commands"):
        await amendments.handle(bridge, bridge.store, click(second, record))


async def test_secret_shape_scrubs_entire_content(bridge):  # noqa: F811
    first, second, record = await start(bridge)
    record["payload"]["event"]["text"] = "password=very-secret\nalso sk-12345678901234"
    await bridge.repository.set_connector_state("claude-event:" + second, record)
    await amendments.handle(bridge, bridge.store, click(second, record))
    saved = amendments.load(bridge.store, first).items[second]
    assert "very-secret" not in saved.text and "12345678901234" not in saved.text


def item(text="addition"):
    return amendments.Addendum(text=text, principal="alice", signature="sig", created=1)


def test_caps_and_closed_manifest(tmp_path):
    storage = LocalStorage(tmp_path)
    target = "a" * 32
    for n in range(amendments.MAX_ITEMS):
        amendments.add(storage, target, f"{n:032x}", item())
    with pytest.raises(ValueError, match="too large"):
        amendments.add(storage, target, "f" * 32, item())
    with pytest.raises(ValueError, match="too large"):
        amendments.add(storage, "b" * 32, "f" * 32, item("é" * 3000))
    amendments.close(storage, target)
    assert amendments.consume(storage, target, "alice", "sig") == ""
    with pytest.raises(ValueError, match="earlier request finished"):
        amendments.add(storage, target, "e" * 32, item())


async def test_close_and_consume_race_has_one_outcome(tmp_path):
    storage = LocalStorage(tmp_path)
    target = "a" * 32
    amendments.add(storage, target, "b" * 32, item())
    closed, text = await asyncio.gather(
        asyncio.to_thread(amendments.close, storage, target),
        asyncio.to_thread(amendments.consume, storage, target, "alice", "sig"),
    )
    assert bool(text) == closed.items["b" * 32].consumed
    assert amendments.load(storage, target).closed


async def test_crash_after_add_write_repairs_receipt_and_choice(bridge, monkeypatch):  # noqa: F811
    first, second, record = await start(bridge)
    parent = await bridge.repository.get_connector_state("claude-event:" + first)
    amendments.add(
        bridge.store,
        first,
        second,
        amendments.Addendum(
            text="already accepted",
            principal=parent["principal"],
            signature=parent["access_signature"],
            created=1,
        ),
    )
    # A subsequent contrary click must not change an already committed choice.
    await amendments.handle(bridge, bridge.store, click(second, record, "follow"))
    saved = await bridge.repository.get_connector_state("claude-event:" + second)
    assert saved["amendment_choice"] == "add"
    assert await bridge.process(second, saved["tick"]) == "pending"


async def test_hook_returns_context_once_and_failure_leaves_normal_flow(tmp_path):
    from mica.agents.analysis.recovery_tools import RecoveryManager
    from mica.agents.analysis.resources import Sampler
    from mica.agents.analysis.runner import Context, build_options

    storage = LocalStorage(tmp_path / "store")
    target = "a" * 32
    amendments.add(storage, target, "b" * 32, item())
    ctx = Context(project_slug="test", workdir=tmp_path / "work", data_path=tmp_path / "data")
    settings = Settings(_env_file=None)
    compute = RecoveryManager(
        settings,
        {"attempt": 0, "tier": "standard", "request_deadline": 9999999999, "provisioning": "SPOT"},
        Sampler(ctx.workdir),
        AsyncMock(),
    )

    async def poll():
        return amendments.consume(storage, target, "alice", "sig")

    compute.poll_addenda = poll
    opts = build_options(ctx, settings, compute_manager=compute)
    hook = opts.hooks["PostToolUse"][-1].hooks[0]
    event = {"hook_event_name": "PostToolUse", "tool_name": "Read"}
    assert "addition" in (await hook(event, None, None))["hookSpecificOutput"]["additionalContext"]
    assert await hook(event, None, None) == {}
    compute.poll_addenda = AsyncMock(side_effect=OSError("unavailable"))
    assert await hook(event, None, None) == {}


@pytest.mark.parametrize("outcome", ["failed", "restarted"])
async def test_handoff_then_worker_failure_is_explicit_without_automatic_repeat(bridge, outcome):  # noqa: F811
    first, second, record = await start(bridge)
    await amendments.handle(bridge, bridge.store, click(second, record))
    parent = await bridge.repository.get_connector_state("claude-event:" + first)
    amendments.consume(bridge.store, first, parent["principal"], parent["access_signature"], 0)
    parent.update(
        status="failed" if outcome == "failed" else "complete",
        attempt=1 if outcome == "restarted" else 0,
    )
    await bridge.repository.set_connector_state("claude-event:" + first, parent)
    assert await bridge.process(second, record["tick"]) == "failed"
    saved = await bridge.repository.get_connector_state("claude-event:" + second)
    assert saved["consumed_into"] == first
    assert "resend this message" in saved["notice_text"]
    assert bridge.test_client.create_job.call_count == 1


async def test_concurrent_hooks_consume_once_and_wrong_access_cannot_consume(tmp_path):
    storage = LocalStorage(tmp_path)
    target = "a" * 32
    amendments.add(storage, target, "b" * 32, item())
    assert amendments.consume(storage, target, "other", "sig") == ""
    assert amendments.consume(storage, target, "alice", "old-signature") == ""
    assert not amendments.load(storage, target).items["b" * 32].consumed
    outputs = await asyncio.gather(
        *[asyncio.to_thread(amendments.consume, storage, target, "alice", "sig") for _ in range(2)]
    )
    assert sum(bool(text) for text in outputs) == 1


async def test_follow_up_long_final_reply_uses_normal_chunks_and_clears_buttons(bridge):  # noqa: F811
    _, second, record = await start(bridge)
    await bridge.notify(second, record, "complete", "Result line.\n" * 1000)
    deliveries = [
        *bridge.test_slack.chat_update.call_args_list,
        *bridge.test_slack.chat_postMessage.call_args_list,
    ]
    final_parts = [call.kwargs for call in deliveries if "Result line." in call.kwargs["text"]]
    assert len(final_parts) > 1
    assert all(part["blocks"] == [] for part in final_parts)


async def test_preexisting_amendment_card_survives_router_removal(bridge):  # noqa: F811
    first, second, record = await start(bridge)
    record.pop("conversation_amendable")
    await bridge.repository.set_connector_state("claude-event:" + second, record)
    await amendments.handle(bridge, bridge.store, click(second, record))
    assert second in amendments.load(bridge.store, first).items


async def test_expired_choice_keeps_followup_and_can_be_reviewed_again(bridge):  # noqa: F811
    first, second, record = await start(bridge)
    record["amendment_expires"] = 1
    await bridge.repository.set_connector_state("claude-event:" + second, record)
    await amendments.KIND.expire(bridge, bridge.store, second)
    expired = await bridge.repository.get_connector_state("claude-event:" + second)
    assert expired["amendment_choice"] == "follow" and expired["previous"] == first
    assert "Request again" in str(bridge.test_slack.chat_update.call_args.kwargs["blocks"])
    assert await bridge.process(second, expired["tick"]) == "pending"
    await amendments.handle(bridge, bridge.store, click(second, expired, "again"))
    renewed = await bridge.repository.get_connector_state("claude-event:" + second)
    assert not renewed.get("amendment_choice") and renewed["amendment_expires"] > 1
    assert not amendments.load(bridge.store, first).items
    await amendments.handle(bridge, bridge.store, click(second, renewed, "add"))
    assert second in amendments.load(bridge.store, first).items
    assert bridge.test_client.create_job.call_count == 1
