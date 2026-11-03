"""Natural intent execution, queue ordering, and requester-bound confirmations."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import command_approval, commands, conversation
from mica.agents.analysis.command_intent import Proposal
from mica.agents.analysis.registry import Registry


@pytest.fixture
def chat(bridge, monkeypatch):  # noqa: F811
    decision = AsyncMock(
        return_value=conversation.Decision(reply="Request received.", route="compute")
    )
    monkeypatch.setattr(conversation, "run", decision)
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    bridge.intent = decision

    async def control(_bridge, _event_id, record, _grant):
        proposal = decision.return_value.command_proposal or {}
        operation = proposal.get("operation", "")
        record["conversation_amendable"] = not operation
        return operation if operation in {"cancel", "write revoke"} else "none"

    bridge.control.side_effect = control
    return bridge


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("projects", "projects"),
        ("switch", "projects"),
        ("brief", "brief"),
        ("memory notes/findings.md", "memory"),
        ("scripts on", "scripts"),
        ("share liver bob@example.com", "share"),
        ("share liver <@U2>", "share"),
        ("publish liver <#C123|research>", "publish"),
        ("rename liver liver-new", "rename"),
        ("write revoke liver", "write revoke"),
        ("share the figure", None),
        ("unshare the plot", None),
        ("publish these results", None),
        ("rename this project", None),
        ("scripts please", None),
        ("transfer the samples when you get a chance", None),
        ("write a script", None),
    ],
)
def test_exact_grammar_declines_prose(text, name):
    result = commands.parse(text)
    assert (result.name if result else None) == name


def test_near_command_typo_still_has_usage_error():
    with pytest.raises(ValueError, match="scripts on"):
        commands.parse("scripts onn")


async def test_projects_before_project_admission_and_memory_and_replay(chat):
    Registry(chat.store).update(
        "unbind", "alice@example.com", "clear", lambda d: d.defaults.clear() or "cleared"
    )
    # Remove legacy DM binding too: account discovery must not depend on either default.
    Registry(chat.store).update(
        "unbound", "alice@example.com", "clear", lambda d: d.bindings.clear() or "cleared"
    )
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation="projects").model_dump(),
    )
    original = "Can you list the projects that I am collaborating on?\n\nSent with Spark"
    eid = await chat.accept(payload(text=original))
    assert await chat.process(eid, 0) == "complete"
    assert await chat.process(eid, 0) == "complete"
    saved = await chat.repository.get_connector_state("claude-event:" + eid)
    assert "liver" in saved["notice_text"]
    assert saved["payload"]["event"]["text"] == original
    assert saved["reply_scope"] == "account"
    chat.intent.assert_awaited_once()
    chat.test_client.create_job.assert_not_called()


async def test_shared_request_hands_off_without_project_list(chat):
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation="projects").model_dump(),
    )
    eid = await chat.accept(
        payload(text="What projects can I access?", channel="C1", channel_type="channel")
    )
    assert await chat.process(eid, 0) == "complete"
    saved = await chat.repository.get_connector_state("claude-event:" + eid)
    assert "private conversation" in saved["notice_text"]
    assert "liver" not in saved["notice_text"]


@pytest.mark.parametrize(
    ("proposal", "expected"),
    [
        (Proposal(operation="brief"), "TODO"),
        (Proposal(operation="memory"), "PROJECT.md"),
        (Proposal(operation="scripts", preference="on"), "attach the scripts"),
        (Proposal(operation="use", selector="liver"), "New threads"),
        (Proposal(operation="connect atomx"), "not available"),
    ],
)
async def test_shared_dispatch_executes_and_never_launches_worker(chat, proposal, expected):
    chat.intent.return_value = conversation.Decision(
        reply="Request received.", route="command", command_proposal=proposal.model_dump()
    )
    eid = await chat.accept(payload(text="Natural request"))
    assert await chat.process(eid, 0) == "complete"
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert expected in record["notice_text"]
    if proposal.operation in {"brief", "memory"}:
        assert record.get("reply_scope") != "account"
    chat.test_client.create_job.assert_not_called()


async def test_cancel_bypasses_running_predecessor_and_is_cached(chat):
    active = await chat.accept(payload(text="Plot the data"))
    assert await chat.process(active, 0) == "running"
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation="cancel").model_dump(),
    )
    eid = await chat.accept(payload("2.0", "1.0", "Please stop this analysis"))
    await chat.process(eid, 0)
    saved = await chat.repository.get_connector_state("claude-event:" + active)
    assert saved["cancel_requested"]
    assert saved["cancelled_by"] == eid
    assert chat.test_client.create_job.call_count == 1
    before = chat.intent.await_count
    await chat.process(eid, 1)
    assert chat.intent.await_count == before


async def test_switch_obeys_active_predecessor(chat):
    active = await chat.accept(payload(text="Plot the data"))
    assert await chat.process(active, 0) == "running"
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation="use", selector="liver").model_dump(),
    )
    eid = await chat.accept(payload("2.0", "1.0", "Use liver"))
    assert await chat.process(eid, 0) == "pending"
    saved = await chat.repository.get_connector_state("claude-event:" + eid)
    assert saved["notice_phase"] == "waiting"
    assert not saved.get("control_bypass")


def action(eid, verb="confirm", *, user="U1", channel="D1"):
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": "9.0"},
        "actions": [{"action_id": command_approval.PREFIX + verb, "value": eid}],
    }


async def proposal(chat, operation="rename", **fields):
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation=operation, selector="liver", **fields).model_dump(),
    )
    eid = await chat.accept(payload(text="Natural administrative request"))
    assert await chat.process(eid, 0) == "complete"
    return eid


async def test_mutation_requires_confirmation_pins_target_and_replays_once(chat):
    eid = await proposal(chat, target="liver-renamed")
    buttons = chat.test_slack.chat_update.call_args.kwargs["blocks"][-1]["elements"]
    assert buttons[0]["style"] == "primary" and "style" not in buttons[1]
    assert Registry(chat.store).load().projects[chat.pid].name == "liver"
    await command_approval.handle(chat, chat.store, action(eid))
    doc = Registry(chat.store).load()
    assert doc.projects[chat.pid].name == "liver-renamed"
    assert eid in doc.applied_events
    await command_approval.handle(chat, chat.store, action(eid))
    assert Registry(chat.store).load().projects[chat.pid].name_revision == 1


@pytest.mark.parametrize("kwargs", [{"user": "U2"}, {"channel": "D2"}])
async def test_forged_approval_context_rejected(chat, kwargs):
    eid = await proposal(chat, target="liver-renamed")
    with pytest.raises(ValueError, match="requester"):
        await command_approval.handle(chat, chat.store, action(eid, **kwargs))
    assert Registry(chat.store).load().projects[chat.pid].name == "liver"


async def test_expired_and_cancelled_proposals_cannot_mutate(chat):
    eid = await proposal(chat, target="liver-renamed")
    path = command_approval.key(eid)
    saved = json.loads(chat.store.get(path)[0])
    saved["expires"] = time.time() - 1
    chat.store.put(path, json.dumps(saved).encode())
    with pytest.raises(ValueError, match="expired"):
        await command_approval.handle(chat, chat.store, action(eid))
    assert Registry(chat.store).load().projects[chat.pid].name == "liver"
    saved.update(expires=time.time() + 60, state="pending")
    chat.store.put(path, json.dumps(saved).encode())
    await command_approval.handle(chat, chat.store, action(eid, "cancel"))
    await command_approval.handle(chat, chat.store, action(eid))
    assert Registry(chat.store).load().projects[chat.pid].name == "liver"


async def test_target_change_invalidates_preview_even_when_active_project_unchanged(chat):
    eid = await proposal(chat, target="liver-renamed")
    Registry(chat.store).update(
        "other",
        "alice@example.com",
        "share",
        lambda d: d.share(chat.pid, "alice@example.com", "bob@example.com"),
    )
    with pytest.raises(ValueError, match="settings changed"):
        await command_approval.handle(chat, chat.store, action(eid))


async def test_project_list_records_numbered_choices_for_followups(chat):
    eid = await chat.accept(payload(text="projects"))
    await chat.process(eid, 0)
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    pending = await chat.repository.get_connector_state("command-context:" + record["thread_key"])
    assert pending["context"]["choices"][0] == {"id": chat.pid, "name": "liver"}
    assert "1. liver" in record["notice_text"]


async def test_confirmation_button_failure_retries_presentation_before_completion(chat):
    broken = True

    async def update(**kwargs):
        nonlocal broken
        if broken and any(b["type"] == "actions" for b in kwargs.get("blocks", [])):
            broken = False
            raise TimeoutError("temporary Slack failure")
        return {"ok": True}

    chat.test_slack.chat_update.side_effect = update
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(
            operation="rename", selector="liver", target="new-name"
        ).model_dump(),
    )
    eid = await chat.accept(payload(text="Rename my project"))
    with pytest.raises(TimeoutError):
        await chat.process(eid, 0)
    saved = await chat.repository.get_connector_state("claude-event:" + eid)
    assert saved["status"] == "pending"
    assert await chat.process(eid, 0) == "complete"
    assert Registry(chat.store).load().projects[chat.pid].name == "liver"


async def test_confirmation_retry_finishes_memory_after_mutation_without_repeating(
    chat, monkeypatch
):
    eid = await proposal(chat, target="new-name")
    original = chat.memory_backend.rename_project
    sync = AsyncMock(side_effect=RuntimeError("temporary sync failure"))
    monkeypatch.setattr(chat.memory_backend, "rename_project", sync)
    with pytest.raises(RuntimeError):
        await command_approval.handle(chat, chat.store, action(eid))
    assert Registry(chat.store).load().projects[chat.pid].name_revision == 1
    monkeypatch.setattr(chat.memory_backend, "rename_project", original)
    await command_approval.handle(chat, chat.store, action(eid))
    assert Registry(chat.store).load().projects[chat.pid].name_revision == 1
    saved = json.loads(chat.store.get(command_approval.key(eid))[0])
    assert saved["state"] == "completed"


async def test_approval_snapshot_set_order_does_not_change_permission(chat):
    Registry(chat.store).update(
        "members",
        "alice@example.com",
        "members",
        lambda d: (
            d.projects[chat.pid].members.update({"bob@example.com", "c@example.com"}) or chat.pid
        ),
    )
    eid = await proposal(chat, target="new-name")
    path = command_approval.key(eid)
    saved = json.loads(chat.store.get(path)[0])
    saved["project"]["members"].reverse()
    chat.store.put(path, json.dumps(saved).encode())
    await command_approval.handle(chat, chat.store, action(eid))
    assert Registry(chat.store).load().projects[chat.pid].name == "new-name"


async def test_cancelled_approval_repairs_notification_failure(chat):
    eid = await proposal(chat, target="new-name")
    chat.test_slack.chat_update.side_effect = TimeoutError("Slack unavailable")
    with pytest.raises(TimeoutError):
        await command_approval.handle(chat, chat.store, action(eid, "cancel"))
    assert json.loads(chat.store.get(command_approval.key(eid))[0])["state"] == "cancelled"
    chat.test_slack.chat_update.side_effect = None
    await command_approval.handle(chat, chat.store, action(eid, "cancel"))
    saved = json.loads(chat.store.get(command_approval.key(eid))[0])
    assert not saved["notification_pending"]
    assert Registry(chat.store).load().projects[chat.pid].name == "liver"


async def test_approval_rejects_a_different_message_in_same_dm(chat):
    eid = await proposal(chat, target="new-name")
    forged = action(eid)
    forged["message"]["ts"] = "99.0"
    with pytest.raises(ValueError, match="original request message"):
        await command_approval.handle(chat, chat.store, forged)


async def test_write_revoke_preview_bypasses_compute_without_releasing_its_queue(chat):
    chat.settings.write_admin_ids = "T1:U1"
    active = await chat.accept(payload(text="Plot the data"))
    assert await chat.process(active, 0) == "running"
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation="write revoke", selector="liver").model_dump(),
    )
    revoke = await chat.accept(payload("2.0", "1.0", "Please revoke write access to liver"))
    assert await chat.process(revoke, 0) == "complete"
    saved = await chat.repository.get_connector_state("claude-event:" + revoke)
    assert saved["control_bypass"]
    chat.intent.return_value = conversation.Decision(reply="Request received.", route="compute")
    followup = await chat.accept(payload("3.0", "1.0", "Make another plot"))
    assert await chat.process(followup, 0) == "pending"
    assert chat.test_client.create_job.call_count == 1
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation="cancel").model_dump(),
    )
    cancel = await chat.accept(payload("4.0", "1.0", "Please stop the analysis"))
    await chat.process(cancel, 0)
    active_record = await chat.repository.get_connector_state("claude-event:" + active)
    assert active_record["cancel_requested"]


@pytest.mark.parametrize("operation", ["publish", "unpublish", "write grant", "write revoke"])
async def test_shared_admin_selector_never_resolves_private_project_names(chat, operation):
    chat.intent.return_value = conversation.Decision(
        reply="Request received.",
        route="command",
        command_proposal=Proposal(operation=operation, selector="liver").model_dump(),
    )
    eid = await chat.accept(
        payload(text="Administrative request", channel="C1", channel_type="channel")
    )
    assert await chat.process(eid, 0) == "complete"
    saved = await chat.repository.get_connector_state("claude-event:" + eid)
    assert "private conversation" in saved["notice_text"]
    assert "liver" not in saved["notice_text"] and chat.pid not in saved["notice_text"]
