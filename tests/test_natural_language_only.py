"""Natural intake, durable in-turn operations and project selection boundaries."""
# ruff: noqa: F811

import copy
import json

import pytest
from analysis_helpers import seed_project
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401
from test_conversation_tools import Client, reply, tool

from mica.agents.analysis import (
    access,
    command_approval,
    command_intent,
    commands,
    conversation,
    memory_tasks,
    user_settings,
)
from mica.agents.analysis.command_intent import Proposal
from mica.agents.analysis.registry import Registry, applied


@pytest.mark.parametrize(
    "text",
    [
        "help",
        "brief",
        "use liver",
        "repos for this project?",
        "rename this analysis",
        "write a script",
        "remember that these are human samples",
    ],
)
async def test_intake_always_reaches_model(bridge, text):
    bridge.decision.return_value = conversation.Decision(route="reply", reply="Understood.")
    event = await bridge.accept(payload(text=text))
    assert await bridge.process(event, 0) == "complete"
    bridge.decision.assert_awaited_once()
    assert bridge.decision.call_args.args[5]["request"] == text


@pytest.mark.parametrize("text", ["stop", "Cancel.", "STOP!!!", "<@BOT> stop."])
async def test_stop_is_the_only_text_shortcut(bridge, text):
    event = await bridge.accept(payload(text=text))
    await bridge.process(event, 0)
    bridge.decision.assert_not_awaited()
    assert commands.cancel_message(text)
    assert not commands.cancel_message("stop after saving")


def test_old_operations_remain_reachable():
    operations = set(Proposal.model_fields["operation"].annotation.__args__)
    assert set(commands.COMMANDS) <= operations | {"transfer"}
    assert not hasattr(commands, "parse")


async def real_loop(bridge, monkeypatch, client, body):
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    event = await bridge.accept(body)
    assert await bridge.process(event, 0) == "complete"
    return await bridge.repository.get_connector_state("claude-event:" + event)


async def test_project_list_is_a_tool_result_and_model_finishes(bridge, monkeypatch):
    client = Client(tool("propose_command", operation="projects"), reply("You have liver."))
    record = await real_loop(bridge, monkeypatch, client, payload(text="Which projects?"))
    result = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"][0]["text"])
    assert result["projects"][0]["id"] == bridge.pid
    assert record["notice_text"] == "You have liver."
    assert record["direct_decision"]["route"] == "reply"
    bridge.test_client.create_job.assert_not_called()


async def test_help_without_a_project_uses_guide_and_no_source_access(bridge, monkeypatch):
    Registry(bridge.store).update(
        "clear",
        "alice@example.com",
        "clear",
        lambda d: d.defaults.clear() or d.bindings.clear() or "cleared",
    )
    client = Client(tool("propose_command", operation="help"), reply("I can help analyze data."))
    await real_loop(bridge, monkeypatch, client, payload(text="What can you do?"))
    assert "read_sources" not in {t["name"] for t in client.calls[0]["tools"]}
    result = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"][0]["text"])
    assert "guide" in result and "command syntax" not in result["guide"]


async def test_approval_stays_terminal(bridge, monkeypatch):
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    client = Client(
        tool("propose_command", operation="share", selector="liver", target="bob@example.com")
    )
    record = await real_loop(bridge, monkeypatch, client, payload(text="Give Bob access"))
    assert len(client.calls) == 1
    assert record["direct_decision"]["route"] == "command"
    assert "Give bob@example.com collaborator access" in record["notice_text"]
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")


async def test_new_thread_switch_discards_previous_project_context(bridge, monkeypatch):
    target = seed_project(
        bridge.store, name="brain", channel="D3", uri="gs://lab-data/beta/", brief=b"B guide"
    )
    Registry(bridge.store).update(
        "default",
        "alice@example.com",
        "default",
        lambda d: d.bind("T1", "D1", "alice@example.com", bridge.pid, private=True) or bridge.pid,
    )
    checked = []

    def inventory(uris, settings, **kwargs):
        checked.extend(uris)
        return {"entries": ["beta/"]}

    monkeypatch.setattr(conversation, "inventory", inventory)
    client = Client(
        tool("propose_command", operation="use", selector="brain"),
        tool("list_folders", paths=[""]),
        reply("Brain has beta/."),
    )
    record = await real_loop(
        bridge, monkeypatch, client, payload(text="Switch to brain and show its datasets")
    )
    assert checked == ["gs://lab-data/beta/"]
    second = json.loads(client.calls[1]["messages"][0]["content"][0]["text"])
    assert second["current_project"]["id"] == target
    assert "Species" not in json.dumps(client.calls[1]["messages"])
    checkpoint = json.loads(bridge.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert checkpoint["context"]["project_slug"] == target
    assert len(checkpoint["messages"]) == 1
    assert checkpoint["messages"][0]["assistant"] == "Brain has beta/."
    assert record["memory_project_id"] == target
    assert record["project_reentry"]["status"] == "ready"


async def test_existing_thread_switch_only_changes_future_default(bridge, monkeypatch):
    target = seed_project(bridge.store, name="brain", channel="D3", uri="gs://lab-data/beta/")
    Registry(bridge.store).update(
        "default",
        "alice@example.com",
        "default",
        lambda d: d.bind("T1", "D1", "alice@example.com", bridge.pid, private=True) or bridge.pid,
    )
    bridge.decision.return_value = conversation.Decision(route="reply", reply="A discussion.")
    first = await bridge.accept(payload(text="Discuss liver"))
    await bridge.process(first, 0)
    client = Client(
        tool("propose_command", operation="use", selector="brain"),
        reply("New messages use brain; this thread stays on liver."),
    )
    record = await real_loop(bridge, monkeypatch, client, payload("2.0", "1.0", "Switch to brain"))
    result = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"][0]["text"])
    assert result["new_threads"]["id"] == target
    assert result["this_thread"]["id"] == bridge.pid
    assert "B guide" not in json.dumps(client.calls)
    checkpoint = json.loads(bridge.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert checkpoint["context"]["project_slug"] == bridge.pid
    assert Registry(bridge.store).load().defaults["alice@example.com"] == target


async def test_operation_replay_with_fresh_model_id_and_started_receipt(bridge, monkeypatch):
    event = await bridge.accept(payload(text="Include scripts"))
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    cmd = commands.Command("scripts", ("on",))
    original = bridge.repository.set_connector_state
    fail = True

    async def save(key, value):
        nonlocal fail
        if fail and any(op["status"] == "done" for op in value.get("operations", {}).values()):
            fail = False
            raise RuntimeError("lost after mutation")
        return await original(key, copy.deepcopy(value))

    monkeypatch.setattr(bridge.repository, "set_connector_state", save)
    with pytest.raises(RuntimeError, match="lost after mutation"):
        await command_intent.execute(bridge, event, record, bridge.store, cmd, grant)
    assert user_settings.effective(bridge.store, grant.email)
    # Intervening preference change must not be undone by the old started receipt.
    user_settings.set_scripts(bridge.store, grant.email, False, "later")
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    assert next(iter(record["operations"].values()))["status"] == "started"
    await command_intent.execute(bridge, event, record, bridge.store, cmd, grant)
    assert not user_settings.effective(bridge.store, grant.email)
    again = await command_intent.execute(bridge, event, record, bridge.store, cmd, grant)
    assert again["return_scripts"] is True  # Receipt describes the original operation.
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    client = Client(reply("Preference handled."))
    await conversation.run(bridge, event, record, bridge.store, grant, {}, [], client=client)
    context = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
    assert next(iter(context["operations"].values()))["status"] == "done"


def test_registry_multiple_operations_and_legacy_receipt(bridge):
    registry = Registry(bridge.store)
    registry.update("same", "alice@example.com", "first", lambda d: "one")
    registry.update("same", "alice@example.com", "second", lambda d: "two")
    doc = registry.load()
    keys = [k for k in doc.applied_events if k.startswith("same:")]
    assert len(keys) == 2
    first = doc.applied_events.pop(next(k for k in keys if doc.applied_events[k].result == "one"))
    doc.applied_events["legacy"] = first
    from mica.agents.analysis.registry import REGISTRY_KEY

    bridge.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    assert registry.update("legacy", "alice@example.com", "first", lambda d: pytest.fail()) == "one"
    assert applied(registry.load(), "legacy", "alice@example.com", "first")


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"direct_decision": {"route": "command"}}, False),
        ({"reply_scope": "account", "direct_decision": {"route": "reply"}}, False),
        (
            {
                "payload": {"event": {"text": "remember this"}},
                "direct_decision": {"route": "reply"},
            },
            True,
        ),
    ],
)
def test_memory_classifies_the_decision(record, expected):
    assert memory_tasks.conversation_turn(record) is expected


@pytest.mark.parametrize("crash_at", ["binding", "detach", "reentry"])
async def test_selection_resumes_each_crash_boundary(bridge, monkeypatch, crash_at):
    target = seed_project(bridge.store, name="brain", channel="D3", uri="gs://lab-data/beta/")
    Registry(bridge.store).update(
        "default",
        "alice@example.com",
        "default",
        lambda d: d.bind("T1", "D1", "alice@example.com", bridge.pid, private=True) or bridge.pid,
    )
    client = Client(
        tool("propose_command", operation="use", selector="brain"), reply("Brain ready.")
    )
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    failed = False
    update = Registry.update
    detach = access.detach_thread

    def interrupted_update(self, event_id, email, operation, change):
        nonlocal failed
        result = update(self, event_id, email, operation, change)
        if crash_at == "binding" and operation.startswith("use:") and not failed:
            failed = True
            raise RuntimeError("selection interrupted")
        return result

    async def interrupted_detach(*args):
        nonlocal failed
        await detach(*args)
        if crash_at == "detach" and not failed:
            failed = True
            raise RuntimeError("selection interrupted")

    async def interrupted_run(*args, **kwargs):
        nonlocal failed
        if crash_at == "reentry" and args[2].get("project_reentry") and not failed:
            failed = True
            raise RuntimeError("selection interrupted")
        return await CONVERSATION_RUN(*args, **kwargs)

    monkeypatch.setattr(Registry, "update", interrupted_update)
    monkeypatch.setattr(access, "detach_thread", interrupted_detach)
    monkeypatch.setattr(conversation, "run", interrupted_run)
    event = await bridge.accept(payload(text="Switch to brain"))
    with pytest.raises(RuntimeError, match="selection interrupted"):
        await bridge.process(event, 0)
    assert await bridge.process(event, 0) == "complete"
    assert len(client.calls) == 2
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    state = json.loads(bridge.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert state["context"]["project_slug"] == target
    assert [row["id"] for row in state["messages"]] == [event]
    receipts = [
        key for key in Registry(bridge.store).load().applied_events if key.startswith(event)
    ]
    assert len(receipts) == 1
    assert await bridge.process(event, 0) == "complete"
    assert len(client.calls) == 2


async def test_revocation_between_selection_and_reentry_exposes_no_project_data(
    bridge, monkeypatch
):
    target = seed_project(
        bridge.store, name="brain", channel="D3", uri="gs://lab-data/beta/", brief=b"SECRET B GUIDE"
    )
    Registry(bridge.store).update(
        "default",
        "alice@example.com",
        "default",
        lambda d: d.bind("T1", "D1", "alice@example.com", bridge.pid, private=True) or bridge.pid,
    )
    detach = access.detach_thread

    async def revoke(*args):
        await detach(*args)

        def change(doc):
            doc.projects[target].owner = "bob@example.com"
            doc.projects[target].members.clear()
            return target

        Registry(bridge.store).update("revocation", "bob@example.com", "revoke", change)

    monkeypatch.setattr(access, "detach_thread", revoke)
    client = Client(
        tool("propose_command", operation="use", selector="brain"),
        reply("You no longer have access."),
    )
    await real_loop(bridge, monkeypatch, client, payload(text="Switch to brain"))
    assert "SECRET B GUIDE" not in json.dumps(client.calls)
    assert "list_folders" not in {t["name"] for t in client.calls[1]["tools"]}


async def test_channel_binding_card_checks_stale_binding(bridge, monkeypatch):
    from test_natural_language_commands import action

    target = seed_project(bridge.store, name="brain", channel="D3")

    def prepare(doc):
        doc.publish(bridge.pid, "alice@example.com", "T1", "C1")
        doc.publish(target, "alice@example.com", "T1", "C1")
        doc.bind("T1", "C1", "alice@example.com", bridge.pid, private=False)
        return target

    Registry(bridge.store).update("setup", "alice@example.com", "prepare", prepare)
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    client = Client(
        tool("propose_command", operation="bind channel", selector="brain", target="C1")
    )
    record = await real_loop(
        bridge, monkeypatch, client, payload(text="Make brain the default project for <#C1>")
    )
    assert (
        "uses liver" in record["notice_text"]
        and "Switch new threads to brain?" in record["notice_text"]
    )
    assert Registry(bridge.store).load().bound("T1", "C1") == bridge.pid
    Registry(bridge.store).update(
        "changed",
        "alice@example.com",
        "rebind",
        lambda d: (
            d.bind("T1", "C1", "alice@example.com", target, private=False, force=True) or target
        ),
    )
    with pytest.raises(ValueError, match="channel.*changed"):
        await command_approval.handle(bridge, bridge.store, action(record["event_id"]))


@pytest.mark.parametrize("violation", ["unpublished", "other_owner"])
async def test_binding_requires_publication_and_both_owners(bridge, violation):
    target = seed_project(bridge.store, name="brain", channel="D3")
    doc = Registry(bridge.store).load()
    if violation == "other_owner":
        doc.publish(target, "alice@example.com", "T1", "C1")
        doc.publish(bridge.pid, "alice@example.com", "T1", "C1")
        doc.bind("T1", "C1", "alice@example.com", bridge.pid, private=False)
        doc.projects[bridge.pid].owner = "bob@example.com"
    grant = access.Grant("alice@example.com", True, {}, doc, target, True, "T1:D1")
    with pytest.raises(ValueError, match="audience|current project's owner"):
        command_approval.authorize(
            bridge,
            bridge.store,
            commands.Command("bind channel", (target, "C1")),
            grant,
            {"payload": payload()},
        )


async def test_resumed_slice_gets_receipts_after_uncheckpointed_tool_result(bridge, monkeypatch):
    from mica.agents.analysis import conversation_state

    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    event = await bridge.accept(payload(text="Include scripts"))
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    record["flexible_harness"] = True
    grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    signed = {"role": "user", "content": [{"type": "text", "text": "Original request"}]}
    await conversation_state.save(
        bridge.store,
        event,
        {
            "version": 1,
            "access": grant.signature,
            "messages": [signed],
            "calls": 1,
            "compactions": 0,
        },
    )
    cmd = commands.Command("scripts", ("on",))
    await command_intent.execute(bridge, event, record, bridge.store, cmd, grant)
    client = Client(reply("Scripts are included."))
    await conversation.run(bridge, event, record, bridge.store, grant, {}, [], client=client)
    messages = client.calls[0]["messages"]
    assert messages[0] == signed
    update = json.loads(messages[1]["content"][0]["text"])
    assert update["operations"][command_intent.operation_key(event, cmd)]["status"] == "done"


async def test_setup_discards_old_transcript_and_retries_guide_turn(bridge, monkeypatch):
    from mica.agents.analysis import conversation_state, direct

    bridge.settings.flexible_harness = True
    event = await bridge.accept(payload(text="Create brain at gs://lab-data/beta/"))
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    await conversation_state.save(
        bridge.store,
        event,
        {
            "version": 1,
            "access": grant.signature,
            "messages": [{"role": "user", "content": "SECRET A GUIDE"}],
            "calls": 1,
            "compactions": 0,
        },
    )
    from analysis_helpers import save_command

    await save_command(
        bridge,
        event,
        command_intent.Proposal(operation="init", selector="brain", scope="gs://lab-data/beta/"),
    )
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    client = Client(
        RuntimeError("lost guide call"),
        reply(
            json.dumps(
                {
                    "reply": "Brain is ready.",
                    "guide": "# Brain\nData: beta/",
                }
            )
        ),
    )
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    with pytest.raises(RuntimeError, match="lost guide call"):
        await direct.handle(bridge, event, record, bridge.store)
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    assert record["direct_init_command"] == ["brain", "gs://lab-data/beta/"]
    assert await direct.handle(bridge, event, record, bridge.store)
    assert "SECRET A GUIDE" not in json.dumps(client.calls)
    assert len(Registry(bridge.store).load().projects) == 2
    assert "Brain is ready" in record["notice_text"]


async def test_channel_binding_confirmation_applies_once(bridge, monkeypatch):
    from test_natural_language_commands import action

    target = seed_project(bridge.store, name="brain", channel="D3")
    Registry(bridge.store).update(
        "publish",
        "alice@example.com",
        "publish",
        lambda doc: doc.publish(target, "alice@example.com", "T1", "C1"),
    )
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    client = Client(
        tool("propose_command", operation="bind channel", selector="brain", target="C1")
    )
    record = await real_loop(
        bridge, monkeypatch, client, payload(text="Make brain the default for <#C1>")
    )
    await command_approval.handle(bridge, bridge.store, action(record["event_id"]))
    doc = Registry(bridge.store).load()
    assert doc.bound("T1", "C1") == target
    await command_approval.handle(bridge, bridge.store, action(record["event_id"]))
    assert Registry(bridge.store).load() == doc


@pytest.mark.parametrize(
    "state",
    [
        {"messages": [{"id": "older", "user": "A discussion"}]},
        {"turn": 1},
        {"conversation_origin_event": "older"},
    ],
)
async def test_contextless_existing_thread_is_never_detached(bridge, state):
    record = {"thread_key": "existing"}
    path = access.checkpoint_key(record["thread_key"])
    bridge.store.put(path, json.dumps(state).encode())
    before = bridge.store.get(path)
    assert not access.unused_context(state, "new")
    await access.detach_thread(bridge.store, record, "new")
    assert bridge.store.get(path) == before
