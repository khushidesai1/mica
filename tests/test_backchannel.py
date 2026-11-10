# ruff: noqa: F811
"""Project peers coordinate without acquiring one another's private transcripts or authority."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from test_analysis_batch import bridge, payload  # noqa: F401
from test_conversation_tools import Client, reply, run, tool, turn  # noqa: F401

from mica.agents.analysis import backchannel as bc
from mica.agents.analysis import conversation, runner
from mica.agents.analysis.backchannel_worker import Client as WorkerClient
from mica.agents.analysis.config import Settings
from mica.agents.analysis.recovery_tools import RecoveryManager
from mica.agents.analysis.resources import Sampler
from mica.agents.analysis.storage import Conflict, LocalStorage
from mica.agents.analysis.transfers import Transfers
from mica.api import slack_analysis


@pytest.fixture
def store(tmp_path):
    return LocalStorage(tmp_path / "storage")


def send(store, text="Tuning is in progress; ask before choosing parameters.", **kwargs):
    project = kwargs.pop("project", "project-a")
    thread = kwargs.pop("thread", "thread-a")
    event = kwargs.pop("event", "event-a")
    return bc.publish(store, project, thread, event, 0,
                      bc.Call(action="publish", topic="segmentation", text=text, **kwargs))


def messages(store, **kwargs):
    return bc.read(store, "project-a", bc.Call(**kwargs))["messages"]


def test_peers_exchange_question_recipe_and_acknowledgment(store):
    send(store)
    question = send(store, "Which tested recipe should the next sample use?", thread="thread-b",
                    kind="question")
    recipe = send(store, "Method v1; same input prior; seed=7; parameter=0.2; evidence: test-1.",
                  kind="recipe", reply_to=question["id"])
    answer = send(store, "Using this recipe for the next sample.", thread="thread-b",
                  kind="answer", reply_to=recipe["id"])
    assert answer["published"] and not answer["adoption_confirmed"]
    assert len(messages(store)) == 4
    exact = messages(store, message_id=recipe["id"])[0]
    assert exact["thread"] == "thread-a" and exact["reply_to"] == question["id"]
    assert bc.read(store, "project-b", bc.Call())["messages"] == []
    assert bc.load(store, "project-a").sequence == 4


def test_duplicate_and_conflicting_recipes(store):
    first = send(store, "Method v1; seed 7", kind="recipe")
    assert send(store, "Method v1; seed 7", kind="recipe") == first
    second = send(store, "Method v2; seed 99", kind="recipe", thread="thread-b")
    assert first["id"] != second["id"]
    assert len(bc.load(store, "project-a").recipes) == 2
    assert {m["id"] for m in messages(store)} == {first["id"], second["id"]}


def test_cas_conflict_preserves_both_writers(store, monkeypatch):
    original = store.put_if
    raced = False

    def put(key, raw, generation):
        nonlocal raced
        if not raced:
            raced = True
            send(store, "Peer update", thread="peer")
            raise Conflict("raced")
        return original(key, raw, generation)

    monkeypatch.setattr(store, "put_if", put)
    send(store, "Original update")
    assert [m["text"] for m in messages(store)] == ["Peer update", "Original update"]
    assert [m["seq"] for m in messages(store)] == [1, 2]


def test_pagination_retention_and_recipe_preservation(store):
    recipe = send(store, "Pinned recipe", kind="recipe")
    for i in range(bc.MAX_NOTES + 8):
        send(store, f"Update {i}")
    value = bc.read(store, "project-a", bc.Call(after=0))
    assert value["retention_gap"] and value["has_more"]
    assert value["messages"][0]["id"] == recipe["id"]
    seen = [m["seq"] for m in value["messages"]]
    while value["has_more"]:
        value = bc.read(store, "project-a", bc.Call(after=value["cursor"]))
        seen.extend(m["seq"] for m in value["messages"])
    assert len(seen) == len(set(seen)) == bc.MAX_NOTES + 1
    assert seen[-1] == bc.load(store, "project-a").sequence
    recent = bc.read(store, "project-a", bc.Call())
    assert recent["older_omitted"] and not recent["has_more"]
    assert recent["recipes"][0]["id"] == recipe["id"]


def test_recipe_capacity_rejects_without_replacing(store):
    for i in range(bc.MAX_RECIPES):
        send(store, f"Recipe {i}", kind="recipe")
    with pytest.raises(ValueError, match="history is full"):
        send(store, "Overflow", kind="recipe")
    assert len(bc.load(store, "project-a").recipes) == bc.MAX_RECIPES


@pytest.mark.parametrize("kwargs", [
    {"topic": "../other"}, {"topic": ""}, {"text": ""}, {"text": "x" * 2001},
    {"text": "é" * 1001}, {"text": "x" * 8001, "kind": "recipe"},
    {"text": "token ghp_" + "a" * 36}, {"kind": "answer"}, {"project": "forged"},
    {"thread": "forged"}, {"attempt": 3}, {"after": 1},
])
def test_invalid_publications(kwargs):
    with pytest.raises(ValueError):
        bc.Call.model_validate({"action": "publish", "topic": "topic", "text": "hello", **kwargs})


def test_natural_topic_names_match_shared_slug(store):
    call = bc.Call(action="publish", topic=" Segmentation Recipe ", text="Still investigating")
    bc.publish(store, "project-a", "a", "a", 0, call)
    assert messages(store, topic="segmentation-recipe")[0]["text"] == "Still investigating"


def test_replies_cannot_cross_project_or_topic(store):
    parent = send(store)
    with pytest.raises(ValueError, match="same project topic"):
        send(store, kind="answer", reply_to=parent["id"], project="project-b")
    with pytest.raises(ValueError, match="same project topic"):
        bc.publish(store, "project-a", "b", "b", 0, bc.Call(
            action="publish", topic="different", kind="answer", text="yes",
            reply_to=parent["id"],
        ))


async def test_worker_polls_new_updates_once_and_replays_on_new_attempt(store):
    async def transport(args):
        return bc.read(store, "project-a", bc.Call.model_validate(args))

    client = WorkerClient(transport, "thread-b")
    send(store)
    assert "Tuning is in progress" in await client.poll()
    assert await client.poll(force=True) == ""
    send(store, "New recipe", kind="recipe")
    assert await client.poll() == ""  # Ordinary hooks are throttled.
    assert "New recipe" in await client.poll(force=True)
    assert "New recipe" in await WorkerClient(transport, "thread-b").poll()


async def test_worker_outage_never_advances_cursor_or_claims_success(store):
    transport = AsyncMock(side_effect=RuntimeError("private provider error"))
    client = WorkerClient(transport, "thread-b")
    assert "unavailable" in await client.poll()
    assert client.cursor is None
    result = await client.tool().handler({"action": "publish", "topic": "x", "text": "hello"})
    assert result["isError"] and "private provider error" not in str(result)
    assert "unconfirmed" in str(result)


async def test_sdk_hooks_reconsider_before_execution_and_deliver_after_tool(tmp_path, store):
    async def transport(args):
        return bc.read(store, "project-a", bc.Call.model_validate(args))

    compute = RecoveryManager(Settings(_env_file=None), {
        "attempt": 0, "tier": "standard", "request_deadline": time.time() + 60,
        "provisioning": "STANDARD",
    }, Sampler(tmp_path), AsyncMock())
    compute.backchannel = WorkerClient(transport, "thread-b")
    options = runner.build_options(runner.Context(tmp_path, tmp_path / "data"),
                                   Settings(_env_file=None), compute_manager=compute)
    assert "mcp__pistachio__project_backchannel" in options.allowed_tools
    send(store, "Tuning is unfinished")
    guard = options.hooks["PreToolUse"][0].hooks[0]
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "true", "timeout": 1000}}
    result = await guard(event, None, {})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Tuning is unfinished" in result["hookSpecificOutput"]["permissionDecisionReason"]
    # The update does not permanently deny execution and never cancels the current run.
    assert await guard(event, None, {}) == {}
    assert not compute.stop.is_set()
    send(store, "Ready: exact method", kind="recipe")
    compute.backchannel.last_poll = -float("inf")
    hook = options.hooks["PostToolUse"][0].hooks[0]
    result = await hook({"hook_event_name": "PostToolUse"}, None, {})
    assert "Ready: exact method" in result["hookSpecificOutput"]["additionalContext"]


async def test_conversation_uses_same_project_board_without_private_history(turn):  # noqa: F811
    bridge, _, record, grant = turn
    record["payload"]["event"]["text"] = "PRIVATE CONVERSATION DETAIL"
    sdk = Client(tool(bc.TOOL, action="publish", topic="segmentation",
                      kind="question", text="Which method is ready?"), reply())
    assert (await run(turn, sdk)).route == "reply"
    board = bc.load(bridge.store, grant.project_id)
    assert len(board.notes) == 1 and board.notes[0].thread == record["thread_key"]
    assert "PRIVATE CONVERSATION DETAIL" not in board.model_dump_json()
    assert "project_backchannel" in sdk.calls[0]["messages"][0]["content"][0]["text"]


async def test_conversation_revocation_prevents_publication(turn):  # noqa: F811
    bridge, _, _, grant = turn
    bridge.app.slack_user_map["T1:U1"] = "bob@example.com"
    sdk = Client(tool(bc.TOOL, action="publish", topic="x", text="forbidden"))
    decision = await run(turn, sdk)
    assert "access changed" in decision.reply
    assert bc.load(bridge.store, grant.project_id).sequence == 0


def test_tool_feature_and_access_boundaries():
    assert bc.TOOL not in {t["name"] for t in conversation.tools_for(False)}
    assert bc.TOOL not in {t["name"] for t in conversation.tools_for(
        True, Settings(_env_file=None, backchannel=False)
    )}


async def test_intake_latches_capability_for_background_and_human_launches(bridge):
    eid = await bridge.accept(payload())
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    assert record["backchannel_enabled"]
    bridge.settings.backchannel = False
    await bridge.accept(payload())  # Intake replay cannot rewrite an existing capability.
    assert (await bridge.repository.get_connector_state("claude-event:" + eid))[
        "backchannel_enabled"
    ]
    eid = await bridge.accept(payload("2.0"))
    assert not (await bridge.repository.get_connector_state("claude-event:" + eid))[
        "backchannel_enabled"
    ]


@pytest.mark.parametrize("case", ["ok", "stale", "old_worker", "disabled", "revoked", "oversize"])
async def test_broker_binds_attempt_and_rechecks_after_lock(bridge, monkeypatch, case):  # noqa: F811
    event = "a" * 32
    record = {"event_id": event, "project_id": "project-a", "thread_key": "thread-a",
              "attempt": 2, "backchannel_enabled": case != "old_worker"}
    context = AsyncMock(return_value=(bridge, bridge.store, record))
    if case == "revoked":
        context.side_effect = [(bridge, bridge.store, record), HTTPException(403, "revoked")]
    if case == "disabled":
        bridge.settings.backchannel = False
    monkeypatch.setattr(slack_analysis, "worker_context", context)
    raw = json.dumps({"attempt": 1 if case == "stale" else 2,
                      "call": {"action": "publish", "topic": "x", "text": "hello"}}).encode()
    if case == "oversize":
        raw = b" " * 12001
    request = SimpleNamespace(body=AsyncMock(return_value=raw))
    if case != "ok":
        with pytest.raises(HTTPException) as error:
            await slack_analysis.project_coordination(event, request, Response())
        assert error.value.status_code == (413 if case == "oversize" else 403)
        assert bc.load(bridge.store, "project-a").sequence == 0
    else:
        result = await slack_analysis.project_coordination(event, request, Response())
        assert result["published"] and context.await_count == 2
        assert bc.load(bridge.store, "project-a").notes[0].attempt == 2
    claim = await bridge.repository.begin_event(f"claude-poll:{event}", lease_seconds=1)
    assert claim.status == "claimed"  # Error paths release the lease too.


async def test_verified_transfer_shares_only_project_readiness(bridge):  # noqa: F811
    controller = Transfers(bridge, bridge.store)
    pending = {"id": "a" * 32, "project": "project-a", "team": "T1", "channel": "D1",
               "thread": "1.0", "requested_by": "private@example.com", "attempt": 1,
               "inventory": "PRIVATE INVENTORY", "config": {"source": "PRIVATE SOURCE"}}
    marker = {"sample": "TMA1", "n_files": 10, "total_bytes": 100, "secret": "PRIVATE"}
    await controller.share_completion(pending, marker)
    await controller.share_completion(pending, marker)
    doc = bc.load(bridge.store, "project-a")
    assert doc.sequence == 1
    assert doc.notes[0].source == "transfer"
    assert json.loads(doc.notes[0].text) == {
        "sample": "TMA1", "status": "transfer_verified", "files": 10, "bytes": 100,
    }
    assert "PRIVATE" not in doc.model_dump_json() and "private@example" not in doc.model_dump_json()


async def test_parallel_worker_polls_serialize_cursor(store):
    async def transport(args):
        await asyncio.sleep(0)
        return bc.read(store, "project-a", bc.Call.model_validate(args))

    send(store)
    client = WorkerClient(transport, "other")
    values = await asyncio.gather(client.poll(force=True), client.poll(force=True))
    assert sum(bool(v) for v in values) == 1


async def test_initial_hook_keeps_recipe_catalog_when_recent_notes_are_own(store):
    recipe = send(store, "Stable recipe", kind="recipe")
    for i in range(15):
        send(store, f"Consumer progress {i}", thread="consumer")

    async def transport(args):
        return bc.read(store, "project-a", bc.Call.model_validate(args))

    client = WorkerClient(transport, "consumer")
    context = await client.poll()
    assert recipe["id"] in context and '"older_omitted": true' in context
