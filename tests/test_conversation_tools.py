# ruff: noqa: F811
"""Scripted provider responses exercise the real tool loop and durable dispatch offline."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_analysis_batch import (  # noqa: F401
    CONVERSATION_CONTROL,
    CONVERSATION_RUN,
    bridge,
    payload,
)

from mica.agents.analysis import (
    access,
    command_intent,
    conversation,
    direct,
    prompts,
    reference_repos,
)


class Block:
    def __init__(self, data):
        self.data = data
        self.__dict__.update(data)

    def model_dump(self, **kwargs):
        return copy.deepcopy(self.data)


class Client:
    def __init__(self, *frames):
        self.frames = list(frames)
        self.calls = []
        self.beta = self.messages = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return SimpleNamespace(stop_reason=frame[0], content=[Block(b) for b in frame[1]])


def reply(text="Evidence-backed answer.", *, stop="end_turn"):
    return stop, [{"type": "text", "text": text}]


def tool(name, **args):
    return "tool_use", [{"type": "tool_use", "id": "t1", "name": name, "input": args}]


@pytest.fixture
async def turn(bridge, monkeypatch):  # noqa: F811
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "control", CONVERSATION_CONTROL)
    event_id = await bridge.accept(payload(text="Please inspect the source"))
    record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    return bridge, event_id, record, grant


async def run(turn, client, **kwargs):
    bridge, event_id, record, grant = turn
    return await conversation.run(
        bridge,
        event_id,
        record,
        bridge.store,
        grant,
        {"request": "Inspect", "brief": "duplicate", "project_md": "guide"},
        [],
        client=client,
        **kwargs,
    )


async def test_plain_reply_private_thinking_and_provider_settings(turn):
    client = Client(
        (
            "end_turn",
            [
                {"type": "thinking", "thinking": "private", "signature": "signed"},
                {"type": "text", "text": "The answer."},
            ],
        )
    )
    decision = await run(turn, client)
    assert decision.reply == "The answer."
    request = client.calls[0]
    assert request["thinking"] == {"type": "adaptive", "display": "updates"}
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert request["fallbacks"] == "default"
    assert request["betas"] == [conversation.FALLBACK_BETA, conversation.PROGRESS_BETA]
    assert request["output_config"]["effort"] == "medium"
    context = json.loads(request["messages"][0]["content"][0]["text"])
    assert "brief" not in context and context["project_md"] == "guide"
    record = turn[2]
    assert record["conversation_steps"] == [] and not record["direct_decision_pending"]
    assert "private" not in json.dumps(record["direct_decision"])
    assert record["routing_budget"] <= direct.ROUTING_SECONDS


async def test_conversation_catalog_includes_validated_saved_environments(turn):
    from test_environment_catalog import runtime

    from mica.agents.analysis.runtime_store import RuntimeStore

    bridge = turn[0]
    bridge.settings.runtimes_enabled = True
    record = runtime(bridge.settings)
    RuntimeStore(bridge.store, bridge.settings).update_index(record)
    client = Client(reply())
    await run(turn, client)
    context = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
    assert any(
        row["id"] == record.id
        for row in context["compute_capabilities"]["environments"]["cpu"]
    )


async def test_approved_public_reference_is_read_directly_without_project_registration(
    turn, monkeypatch
):
    bridge, event_id, record, grant = turn
    repo = "khushidesai1/nympheas-temp"
    assert not grant.project.reference_repos
    fetch = AsyncMock(
        return_value=reference_repos.RepoSnapshot(
            repo=repo, commit="a" * 40, files={"src/qc.py": "def qc(min_counts=20): pass"}
        )
    )
    monkeypatch.setattr(reference_repos, "fetch", fetch)
    sdk = Client(tool("read_sources", repos=[repo], file_ids=[]), reply("The default is 20."))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    assert await direct.handle(bridge, event_id, record, bridge.store)
    fetch.assert_awaited_once_with(bridge.settings, repo)
    assert record["information_repos"] == [repo]
    assert "min_counts=20" in json.dumps(sdk.calls[1]["messages"])
    assert not grant.project.reference_repos
    bridge.test_client.create_job.assert_not_called()


async def test_markdown_reply_is_formatted_for_slack_and_retry_preserves_original(
    turn, monkeypatch
):
    bridge, event_id, record, _ = turn
    text = (
        "**1. Run and slide level**\n"
        "- Compare counts & genes.\n"
        "- See [source](https://example.org/qc).\n"
        "- Keep `**literal**` and `<cell>` unchanged.\n"
        "```python\nvalue = '**literal**'\n```"
    )
    sdk = Client(reply(text))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    assert await direct.handle(bridge, event_id, record, bridge.store)
    rendered = bridge.test_slack.chat_update.call_args.kwargs["text"]
    assert rendered.startswith("*1. Run and slide level*\n- Compare counts &amp; genes.")
    assert "<https://example.org/qc|source>" in rendered
    assert "`**literal**` and `<cell>`" in rendered
    assert "```python\nvalue = '**literal**'\n```" in rendered
    assert record["direct_decision"]["reply"] == record["memory_reply"] == text
    state = json.loads(bridge.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert state["messages"][-1]["assistant"] == text
    updates = bridge.test_slack.chat_update.await_count
    assert await direct.handle(bridge, event_id, record, bridge.store)
    assert bridge.test_slack.chat_update.await_count == updates
    assert len(sdk.calls) == 1
    bridge.test_client.create_job.assert_not_called()


async def test_unordered_reads_and_append_only_thinking(turn, monkeypatch):
    first = tool("list_folders", paths=["raw/"])
    first[1].insert(0, {"type": "thinking", "thinking": "private", "signature": "signed"})
    client = Client(
        first,
        tool("read_sources", repos=[], file_ids=[]),
        tool("list_folders", paths=["processed/"]),
        reply(),
    )
    listing = Mock(return_value={"folders": [{"complete": False}]})
    read = AsyncMock(
        return_value={"references": [{"repo": "azizilab/nympheas", "commit": "abc"}], "files": []}
    )
    monkeypatch.setattr(conversation, "inventory", listing)
    monkeypatch.setattr(conversation, "read_information", read)
    turn[0].resolve_data = Mock(return_value=(turn[0].settings, None))
    assert (await run(turn, client)).route == "reply"
    assert listing.call_count == 2 and read.await_count == 1
    assert [s["tool"] for s in turn[2]["conversation_steps"]] == [
        "list_folders",
        "read_sources",
        "list_folders",
    ]
    assert turn[2]["information_repos"] == ["azizilab/nympheas"]
    assert client.calls[1]["messages"][1]["content"][0] == first[1][0]
    assert '"complete": false' in client.calls[1]["messages"][2]["content"][0]["content"][0]["text"]
    assert all(
        set(s) == {"tool", "argument_bytes", "status"} for s in turn[2]["conversation_steps"]
    )


@pytest.mark.parametrize("deadline", [False, True])
async def test_cap_and_deadline_reserve_final_no_tools(turn, monkeypatch, deadline):
    turn[0].settings.conversation_max_calls = 1
    if deadline:
        conversation.start(turn[2])
        turn[2]["routing_budget"] = 10
        client = Client(reply("Partial evidence and exact gap."))
    else:
        client = Client(
            tool("list_folders", paths=["../denied"]), reply("Partial evidence and exact gap.")
        )
    decision = await run(turn, client)
    assert decision.reply == "Partial evidence and exact gap."
    assert client.calls[-1]["tool_choice"] == {"type": "none"}
    assert len(client.calls) == (1 if deadline else 2)


async def test_bad_input_is_repairable(turn):
    client = Client(
        tool("view_files", ids=[str(i) for i in range(5)]), reply("Please attach the image.")
    )
    assert (await run(turn, client)).route == "reply"
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    assert turn[2]["conversation_steps"][0]["status"] == "error"


async def test_access_change_between_tools_stops(turn, monkeypatch):
    grant = turn[3]
    monkeypatch.setattr(access, "resolve", Mock(side_effect=[grant, replace(grant, allowed=False)]))
    client = Client(tool("view_files", ids=[]), tool("view_files", ids=[]))
    result = await run(turn, client)
    assert result.reply == "Project access changed. Send a new request."
    assert len(turn[2]["conversation_steps"]) == 1


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
async def test_unfinished_model_uses_retry_path(turn, stop):
    with pytest.raises(direct.RoutingUnavailable):
        await run(turn, Client(reply(stop=stop)))
    assert not turn[2].get("direct_decision")


async def test_terminal_worker_is_saved_and_replayed_without_model(turn, monkeypatch):
    bridge, event_id, record, grant = turn
    client = Client(
        tool(
            "start_worker",
            task="Count rows in the requested CSV",
            expected_hours=0.5,
            tier="standard",
            write_paths=["gs://lab-data/results/count.csv"],
        )
    )
    decision = await run(turn, client)
    saved = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    assert saved["direct_decision"]["write_paths"] == decision.write_paths
    assert saved["decision_access"] == grant.signature
    assert await direct.handle(bridge, event_id, record, bridge.store) is False
    launch = copy.deepcopy(record["launch"])
    assert await direct.handle(bridge, event_id, record, bridge.store) is False
    assert record["launch"] == launch
    assert len(client.calls) == 1
    bridge.test_client.create_job.assert_not_called()


async def test_worker_arguments_are_validated_without_provider_grammar(turn):
    bridge, _, record, _ = turn
    client = Client(
        tool("start_worker", task="Run the request", expected_hours=0.5, tier="invented"),
        reply("I cannot allocate that unsupported worker."),
    )
    decision = await run(turn, client)
    specs = {spec["name"]: spec for spec in client.calls[0]["tools"]}
    assert specs["start_worker"]["strict"] is False
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    assert decision.route == "reply"
    assert "launch" not in record and "gpu_approval" not in record and "slot" not in record
    bridge.test_client.create_job.assert_not_called()


async def test_128_gib_gpu_request_explains_limit_without_downgrade_or_allocation(
    turn, monkeypatch
):
    bridge, event_id, _, _ = turn
    bridge.settings.gpu_tier_enabled = True
    bridge.settings.gpu_image = "us-docker.pkg.dev/project/images/gpu@sha256:" + "b" * 64
    sdk = Client(
        tool(
            "start_worker",
            task="Train with GPU and 128 GiB host RAM",
            expected_hours=3,
            tier="gpu",
            gpu_type="l4",
            constraints={"min_memory_gib": 128},
        ),
        # A repair turn must not discard the explicit minimum and launch the smaller VM.
        tool("start_worker", task="Try with less RAM", expected_hours=3, tier="gpu"),
    )
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    assert await bridge.process(event_id, 0) == "complete"
    assert len(sdk.calls) == 1
    bridge.test_client.create_job.assert_not_called()
    saved = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    assert "launch" not in saved and "gpu_approval" not in saved and "slot" not in saved
    assert "128 GiB" in saved["direct_decision"]["reply"]
    assert "32 GiB host RAM" in saved["direct_decision"]["reply"]
    assert "owner needs to enable" in saved["direct_decision"]["reply"]
    context = json.loads(sdk.calls[0]["messages"][0]["content"][0]["text"])
    assert [
        w["host_memory_gib"]
        for w in context["compute_capabilities"]["workers"]
        if w["tier"] == "gpu"
    ] == [32, 30]
    assert await bridge.process(event_id, 0) == "complete"
    bridge.test_client.create_job.assert_not_called()


async def test_transfer_is_terminal_and_durable(turn):
    client = Client(tool("propose_transfer", sample="sample-7"))
    result = await run(turn, client)
    assert result.route == "transfer" and result.transfer_request.sample == "sample-7"
    assert turn[2]["conversation_steps"][0]["status"] == "terminal"


async def test_no_project_exposes_only_command(turn):
    bridge, event_id, record, grant = turn
    turn = bridge, event_id, record, replace(grant, allowed=False, project_id="")
    client = Client(reply("Which project would you like to use?"))
    await run(turn, client)
    assert [t["name"] for t in client.calls[0]["tools"]] == ["propose_command"]


async def test_command_policies_and_stale_choices(turn):
    _, _, record, grant = turn
    proposal = command_intent.Proposal(
        operation="share", selector="this project", target="bob@example.com"
    )
    with pytest.raises(ValueError, match="private conversation"):
        conversation.validate_command(proposal, replace(grant, private=False), record)
    record["intent_referents"] = {"expires": 0, "visibility": command_intent.visibility(grant)}
    with pytest.raises(ValueError, match="expired"):
        conversation.validate_command(proposal, grant, record)


async def test_request_skips_control_check_without_active_predecessor(turn, monkeypatch):
    bridge, event_id, _, _ = turn
    sdk = Client(reply())
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    control = AsyncMock()
    monkeypatch.setattr(conversation, "control", control)
    assert await bridge.process(event_id, 0) == "complete"
    control.assert_not_awaited()
    assert len(sdk.calls) == 1


async def test_active_predecessor_cancel_bypasses_queue(turn, monkeypatch):
    bridge, previous, record, _ = turn
    record["status"] = "running"
    await bridge.repository.set_connector_state(f"claude-event:{previous}", record)
    next_id = await bridge.accept(payload("2.0", "1.0", "stop this"))
    check = AsyncMock(return_value="cancel")
    monkeypatch.setattr(conversation, "control", check)
    await bridge.process(next_id, 0)
    check.assert_awaited_once()
    stored = await bridge.repository.get_connector_state(f"claude-event:{next_id}")
    assert stored["cancel_control"] is True


def test_strict_schema_budget():
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "$ref",
        "$defs",
        "description",
    }

    def check(node):
        if isinstance(node, list):
            for item in node:
                check(item)
        elif isinstance(node, dict):
            assert set(node) <= allowed
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for key, value in node.items():
                if key in {"properties", "$defs"}:
                    for child in value.values():
                        check(child)
                else:
                    check(value)

    for spec in conversation.tools_for(True):
        assert spec["strict"] is (spec["name"] != "start_worker")
        check(spec["input_schema"])
    assert len(prompts.CONVERSATION) <= 1502
    assert sum(map(len, conversation.DESCRIPTIONS.values())) <= 3910


async def test_natural_init_uses_structured_guide_without_worker(turn, monkeypatch):
    bridge, event_id, record, _ = turn
    record["payload"]["event"]["text"] = "Create fresh-project using gs://lab-data/fresh/"
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    sdk = Client(
        tool(
            "propose_command",
            operation="init",
            selector="fresh-project",
            scope="gs://lab-data/fresh/",
        ),
        reply(
            json.dumps(
                {"reply": "Project ready.", "guide": "# Fresh project\nData: gs://lab-data/fresh/"}
            )
        ),
    )
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    monkeypatch.setattr(direct, "inventory", Mock(return_value={"complete": True, "folders": []}))
    assert await direct.handle(bridge, event_id, record, bridge.store) is True
    assert record["direct_init"] is True
    assert "format" in sdk.calls[1]["output_config"]
    assert "tools" not in sdk.calls[1]
    assert sdk.calls[1]["thinking"] == {"type": "adaptive"}
    assert sdk.calls[1]["betas"] == [conversation.FALLBACK_BETA]
    stored = json.loads(bridge.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert stored["context"]["project_slug"] != bridge.pid
    assert "Fresh project" in stored["context"]["project_md"]
    bridge.test_client.create_job.assert_not_called()


async def test_new_loop_refusal_exhausts_existing_three_attempt_recovery(turn, monkeypatch):
    from mica.agents.analysis import batch

    bridge, event_id, _, _ = turn
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    sdk = Client(reply(stop="refusal"), reply(stop="max_tokens"), reply(stop="refusal"))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    for tick, timestamp in enumerate((100, 130, 190)):
        now[0] = timestamp
        status = await bridge.process(event_id, tick)
    record = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    assert status == "failed" and record["failure_cause"] == "routing_exhausted"
    assert record["routing_failures"] == 3 and len(sdk.calls) == 3
    bridge.test_client.create_job.assert_not_called()


async def test_email_write_paths_are_repairable_read_only_error(turn, monkeypatch):
    from mica.agents.analysis.surface import EMAIL_READ_ONLY

    turn[2]["surface"] = "email"
    monkeypatch.setattr(access, "resolve", Mock(return_value=turn[3]))
    sdk = Client(
        tool(
            "start_worker",
            task="Create the requested report",
            expected_hours=0.5,
            write_paths=["gs://lab-data/results/report.csv"],
        ),
        reply(EMAIL_READ_ONLY),
    )
    result = await run(turn, sdk)
    assert result.reply == EMAIL_READ_ONLY and result.route == "reply"
    assert sdk.calls[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.parametrize(
    "principal,user,channel",
    [
        ("alice@example.com", "U1", "D1"),
        ("bob@example.com", "U2", "D2"),
    ],
)
async def test_every_authorized_principal_uses_conversation(
    turn, monkeypatch, principal, user, channel
):
    from mica.agents.analysis.registry import Registry

    bridge = turn[0]

    def authorize(doc):
        doc.share(bridge.pid, "alice@example.com", principal)
        doc.bind("T1", channel, principal, bridge.pid, private=True)
        return bridge.pid

    if user == "U2":
        Registry(bridge.store).update("share", "alice@example.com", "fixture", authorize)
    client = Client(reply("A direct answer."))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    event_id = await bridge.accept(payload("9.0", text="Explain QC", user=user, channel=channel))
    assert await bridge.process(event_id, 0) == "complete"
    saved = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    assert (
        saved["conversation_steps"] == []
        and saved["direct_decision"]["reply"] == "A direct answer."
    )
    assert len(client.calls) == 1
    bridge.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("decision_saved", [True, False])
async def test_pending_legacy_decisions_restart_through_conversation(
    turn, monkeypatch, decision_saved
):
    bridge, event_id, record, _ = turn
    record.update(
        command_intent={"route": "command", "proposal": {"operation": "cancel"}},
        intent_actor="alice@example.com",
        direct_decision={"route": "compute", "reply": "Old"},
        direct_decision_pending=False,
        decision_access="old",
        route="compute",
        launch={"tier": "xlarge"},
        routing_retry_at=10**12,
        routing_failures=2,
        routing_budget=0,
    )
    if not decision_saved:
        # A failed legacy classifier may have exhausted its budget before saving a decision.
        for field in ("command_intent", "direct_decision"):
            record.pop(field)
    await bridge.repository.set_connector_state(f"claude-event:{event_id}", record)
    client = Client(reply("Current conversation answer."))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    assert await bridge.process(event_id, 0) == "complete"
    saved = await bridge.repository.get_connector_state(f"claude-event:{event_id}")
    assert saved["direct_decision"]["reply"] == "Current conversation answer."
    assert "command_intent" not in saved and "intent_actor" not in saved and "launch" not in saved
    assert len(client.calls) == 1
    bridge.test_client.create_job.assert_not_called()
    bridge.test_client.delete_job.assert_not_called()


@pytest.mark.parametrize("job", [{"job": "active"}, {"recovery_version": 1}])
def test_legacy_running_worker_keeps_its_approved_launch(job):
    record = {**job, "route": "compute", "launch": {"tier": "gpu"}, "direct_decision": {}}
    before = copy.deepcopy(record)
    conversation.start(record)
    assert record == before


@pytest.mark.parametrize(
    "operation,amendable",
    [
        ("cancel", False),
        ("write revoke", False),
        ("none", False),
        ("none", True),
    ],
)
async def test_control_result_keeps_commands_out_of_amendments(
    turn, monkeypatch, operation, amendable
):
    bridge, event_id, record, grant = turn
    client = Client(reply(json.dumps({"operation": operation, "amendable": amendable})))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    assert await conversation.control(bridge, event_id, record, grant) == operation
    assert record["conversation_amendable"] is (operation == "none" and amendable)
    assert await conversation.control(bridge, event_id, record, grant) == operation
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "prefix,expected",
    [
        (
            [{"type": "text", "text": "Found the QC table. Checking counts."}],
            "Found the QC table. Checking counts.",
        ),
        (
            [{"type": "thinking", "thinking": "Found the QC table.", "signature": "signed"}],
            "Found the QC table.",
        ),
        (
            [
                {"type": "text", "text": "First note."},
                {"type": "thinking", "thinking": "Latest note.", "signature": "signed"},
            ],
            "Latest note.",
        ),
        (
            [
                {"type": "fallback"},
                {"type": "thinking", "thinking": "PRIVATE", "signature": "signed"},
                {"type": "text", "text": "Public note."},
            ],
            "Public note.",
        ),
        (
            [
                {"type": "fallback"},
                {"type": "thinking", "thinking": "PRIVATE", "signature": "signed"},
            ],
            "Looking at the attached files…",
        ),
        (
            [{"type": "thinking", "thinking": "", "signature": "signed"}],
            "Looking at the attached files…",
        ),
        ([], "Looking at the attached files…"),
    ],
)
async def test_conversation_notes_precede_tools_and_preserve_signed_blocks(turn, prefix, expected):
    bridge = turn[0]
    bridge.notify = AsyncMock()
    blocks = prefix + tool("view_files", ids=[])[1]
    client = Client(("tool_use", blocks), reply("Final answer."))
    decision = await run(turn, client)
    assert decision.reply == "Final answer."
    statuses = [call.args[3] for call in bridge.notify.await_args_list]
    assert statuses[1] == expected
    if expected != "Looking at the attached files…":
        assert statuses.count("Mica is thinking") == 1
    assert client.calls[1]["messages"][1] == {"role": "assistant", "content": blocks}
    assert "PRIVATE" not in repr(statuses)
    assert expected not in json.dumps(turn[2]["conversation_steps"])
    assert decision.reply not in statuses


def test_conversation_note_never_uses_final_or_post_tool_text():
    blocks = [{"type": "text", "text": "Final answer."}]
    assert conversation.progress_note(blocks, thinking_updates=True) == ""
    assert (
        conversation.progress_note(tool("view_files", ids=[])[1] + blocks, thinking_updates=True)
        == ""
    )


async def test_optional_display_rejection_retries_without_reading_thinking(turn):
    import httpx
    from anthropic import BadRequestError

    error = BadRequestError(
        "thinking.display updates is not supported by the fallback",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
        body={"error": {"type": "invalid_request_error"}},
    )
    blocks = [{"type": "thinking", "thinking": "PRIVATE", "signature": "signed"}] + tool(
        "view_files", ids=[]
    )[1]
    client = Client(error, ("tool_use", blocks), reply())
    turn[0].notify = AsyncMock()
    await run(turn, client)
    assert len(client.calls) == 3
    assert client.calls[0]["thinking"]["display"] == "updates"
    assert all(call["thinking"] == {"type": "adaptive"} for call in client.calls[1:])
    assert all(call["betas"] == [conversation.FALLBACK_BETA] for call in client.calls[1:])
    assert client.calls[1]["timeout"] <= client.calls[0]["timeout"]
    assert "PRIVATE" not in repr(turn[0].notify.await_args_list)


async def test_conversation_status_is_literal_and_email_has_no_progress(turn):
    from mica.agents.analysis import batch

    bridge, event_id, record, _ = turn
    bridge.test_slack.reset_mock()
    note = "Found <!channel> & *bold*. Checking counts."
    record["notice_ts"] = "9.0"
    await bridge.notify(event_id, record, "replying", note)
    sent = bridge.test_slack.chat_update.call_args.kwargs
    assert sent["text"] == "Found &lt;!channel&gt; &amp; *bold*. Checking counts."
    assert sent["blocks"][0]["title"] == "Found <!channel> & *bold*."
    bridge.test_slack.reset_mock()
    # Exercise the same nonterminal path used by conversation notes on email requests.
    from unittest.mock import patch

    with patch.object(batch.surface, "is_email", return_value=True):
        await bridge.notify(event_id, record, "replying", "Next public note.")
    bridge.test_slack.chat_update.assert_not_called()
    bridge.test_slack.chat_postMessage.assert_not_called()
