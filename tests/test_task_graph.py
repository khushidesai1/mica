"""Queues use real durable receipts; execution and approval gates stay in existing controllers."""

# ruff: noqa: F811
import copy
import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_conversation_tools import Client, reply, tool

from mica.agents.analysis import access, conversation, outcome_review, request_controls, task_graph


def node(name, deps=(), kind="analysis"):
    return {
        "id": name,
        "title": name,
        "kind": kind,
        "task": f"Process {name}.",
        "depends_on": list(deps),
        "transfer": {"sample": name} if kind == "transfer" else None,
    }


async def create(chat, *nodes):
    chat.settings.flexible_harness = True
    plan = task_graph.Call(action="create", tasks=nodes)
    chat.decision.return_value = conversation.Decision(
        route="workflow",
        reply="Queue requested",
        task_queue=plan,
    )
    eid = await chat.accept(
        payload(text="Transfer and segment the requested samples, then report.")
    )
    await chat.process(eid, 0)
    parent = await chat.repository.get_connector_state("claude-event:" + eid)
    assert parent["route"] == "workflow"
    return parent


async def child(chat, parent, name):
    return await chat.repository.get_connector_state(
        "claude-event:" + task_graph.child_id(parent["event_id"], name)
    )


async def done(chat, parent, name, *, status="complete", route="compute"):
    record = await child(chat, parent, name)
    record.update(status=status, route=route, terminal_reply=f"Verified output for {name}.")
    await chat.repository.set_connector_state("claude-event:" + record["event_id"], record)
    await chat.repository.set_connector_state("task-outcome:" + record["event_id"], {
        "status": "complete", "result": {"outcome": "completed", "blockers": []},
    })


@pytest.mark.parametrize(
    "tasks",
    [
        [],
        [node("a"), node("a")],
        [node("a", ["missing"])],
        [node("a", ["a"])],
        [node("a", ["b"]), node("b", ["a"])],
    ],
)
def test_rejects_invalid_graphs(tasks):
    with pytest.raises(ValueError):
        task_graph.Call(action="create", tasks=tasks)


async def test_fanout_join_and_duplicate_polls_survive_restart(chat):
    parent = await create(chat, node("left"), node("right"), node("report", ["left", "right"]))
    assert await child(chat, parent, "left") and await child(chat, parent, "right")
    assert not await child(chat, parent, "report")
    before = copy.deepcopy(await child(chat, parent, "left"))
    await task_graph.advance(chat, parent)
    assert await child(chat, parent, "left") == before
    await done(chat, parent, "left")
    await task_graph.advance(chat, parent)
    assert not await child(chat, parent, "report")
    await done(chat, parent, "right")
    # A new controller invocation reconstructs the plan entirely from durable records.
    parent = await chat.repository.get_connector_state("claude-event:" + parent["event_id"])
    await task_graph.advance(chat, parent)
    report = await child(chat, parent, "report")
    text = report["payload"]["event"]["text"]
    assert "Verified output for left" in text and "Verified output for right" in text
    await done(chat, parent, "report")
    await task_graph.advance(chat, parent)
    assert parent["status"] == "complete"
    assert all(row["state"] == "complete" for row in parent["task_graph"])
    chat.test_client.create_job.assert_not_called()


async def test_failure_blocks_transitive_dependents_but_not_independent_work(chat):
    parent = await create(
        chat, node("bad"), node("independent"), node("after", ["bad"]), node("last", ["after"])
    )
    await done(chat, parent, "bad", status="failed")
    await task_graph.advance(chat, parent)
    assert [r["state"] for r in parent["task_graph"]] == ["failed", "running", "blocked", "blocked"]
    assert not await child(chat, parent, "after") and not await child(chat, parent, "last")
    await done(chat, parent, "independent")
    await task_graph.advance(chat, parent)
    assert parent["status"] == "failed"


async def test_clarification_is_not_successful_input_for_dependents(chat):
    parent = await create(chat, node("question"), node("after", ["question"]))
    await done(chat, parent, "question", route="reply")
    await task_graph.advance(chat, parent)
    assert [r["state"] for r in parent["task_graph"]] == ["needs_input", "blocked"]


@pytest.mark.parametrize("outcome,expected", [
    ("completed", "complete"), ("partial", "failed"), ("not_completed", "failed"),
    ("unknown", "needs_input"), ("clarification", "needs_input"),
])
async def test_worker_exit_requires_completed_task_before_releasing_dependents(
    chat, monkeypatch, outcome, expected
):
    parent = await create(chat, node("segment"), node("report", ["segment"]))
    saved = await child(chat, parent, "segment")
    saved.update(status="complete", route="compute", memory_reply="Segmentation stopped at QC.")
    await chat.repository.set_connector_state("claude-event:" + saved["event_id"], saved)
    classifier = AsyncMock(return_value=outcome_review.Review(
        outcome=outcome, blockers=["other"] if outcome in {"partial", "not_completed"} else [],
    ))
    monkeypatch.setattr(outcome_review, "assess", classifier)
    await task_graph.advance(chat, parent)
    assert parent["task_graph"][0]["state"] == expected
    assert bool(await child(chat, parent, "report")) == (expected == "complete")
    assert classifier.call_args.args[2]["request"] == "Process segment."
    await task_graph.advance(chat, parent)
    classifier.assert_awaited_once()


async def test_unavailable_completion_review_is_bounded_and_never_releases_dependency(
    chat, monkeypatch
):
    parent = await create(chat, node("one"), node("after", ["one"]))
    saved = await child(chat, parent, "one")
    saved.update(status="complete", route="compute")
    await chat.repository.set_connector_state("claude-event:" + saved["event_id"], saved)
    classifier = AsyncMock(return_value=None)
    monkeypatch.setattr(outcome_review, "assess", classifier)
    await task_graph.advance(chat, parent)
    assert parent["task_graph"][0]["state"] == "checking_completion"
    await task_graph.advance(chat, parent)
    await task_graph.advance(chat, parent)
    assert classifier.await_count == 2
    assert [r["state"] for r in parent["task_graph"]] == ["needs_input", "blocked"]
    assert not await child(chat, parent, "after")


async def test_transfer_handoff_and_approval_do_not_release_dependency(chat):
    parent = await create(chat, node("sample", kind="transfer"), node("segment", ["sample"]))
    transfer = await child(chat, parent, "sample")
    assert transfer["direct_decision"]["transfer_request"]["sample"] == "sample"
    transfer.update(status="complete", route="transfer")
    await chat.repository.set_connector_state("claude-event:" + transfer["event_id"], transfer)
    pending = {
        "state": "preview",
        "request": {"sample": "sample"},
        "config": {"bucket": "lab-data"},
    }
    for state in ["preview", "submitting", "running"]:
        pending["state"] = state
        await chat.repository.set_connector_state(
            "transfer-pending:" + transfer["event_id"], pending
        )
        await task_graph.advance(chat, parent)
        assert not await child(chat, parent, "segment")
    pending["state"] = "done"
    await chat.repository.set_connector_state("transfer-pending:" + transfer["event_id"], pending)
    await task_graph.advance(chat, parent)
    assert await child(chat, parent, "segment")


async def test_children_have_isolated_checkpoints_and_no_inherited_session_or_approval(chat):
    parent = await create(chat, node("one"), node("two"))
    states = []
    for name in ["one", "two"]:
        record = await child(chat, parent, name)
        states.append(json.loads(chat.store.get(access.checkpoint_key(record["thread_key"]))[0]))
        assert record["previous"] is None and "write_grant" not in record
        assert "gpu_approval" not in record
    assert states[0]["context"]["workdir"] != states[1]["context"]["workdir"]
    assert all(s["context"]["session_id"] is None for s in states)
    assert states[0]["context"]["project_slug"] == chat.pid


async def test_cancellation_stops_children_and_never_spawns_waiting_dependents(chat):
    parent = await create(chat, node("one"), node("after", ["one"]))
    await task_graph.signal_cancel(chat, parent["event_id"])
    await request_controls.reconcile_cancel(chat, parent["event_id"], parent)
    assert parent["status"] == "pending"  # Parent waits for child shutdown.
    await task_graph.advance(chat, parent)
    one = await child(chat, parent, "one")
    assert await request_controls.cancelled(chat.store, one["event_id"])
    assert not await child(chat, parent, "after")
    await chat.process(one["event_id"], 0)
    await task_graph.advance(chat, parent)
    assert parent["status"] == "failed"
    chat.test_client.create_job.assert_not_called()


async def test_access_change_stops_graph_without_launching_dependents(chat):
    from mica.agents.analysis.registry import Registry

    parent = await create(chat, node("one"), node("after", ["one"]))

    def change(doc):
        doc.projects[chat.pid].access_revision += 1
        return chat.pid

    Registry(chat.store).update("change", "alice@example.com", "change", change)
    await task_graph.advance(chat, parent)
    one = await child(chat, parent, "one")
    assert await request_controls.cancelled(chat.store, one["event_id"])
    assert not await child(chat, parent, "after")
    assert "access changed" in parent["notice_text"]


async def test_queue_status_does_not_wait_behind_active_graph_and_stop_still_reaches_it(chat):
    parent = await create(chat, node("one"))
    chat.decision.return_value = conversation.Decision(
        route="reply", reply="Your queue is running."
    )
    eid = await chat.accept(payload("2.0", "1.0", "How is my queue doing?"))
    assert await chat.process(eid, 0) == "complete"
    supplied = chat.decision.call_args.args[5]
    assert supplied["task_queues"][0]["queue_id"] == parent["event_id"]
    status_record = await chat.repository.get_connector_state("claude-event:" + eid)
    from mica.agents.analysis import agent_session

    active = await task_graph.session_records(chat, status_record)
    assert agent_session.desired([status_record, *active]) == "processing"
    cancel = await chat.accept(payload("3.0", "1.0", "cancel"))
    await chat.process(cancel, 0)
    assert await request_controls.cancelled(chat.store, parent["event_id"])


async def test_delayed_stop_does_not_cancel_a_newer_queue(chat):
    parent = await create(chat, node("one"))
    delayed = copy.deepcopy(parent)
    delayed["payload"]["event"]["ts"] = "0.5"
    assert not await task_graph.cancel_active(chat, delayed)
    assert not await request_controls.cancelled(chat.store, parent["event_id"])


async def test_status_and_cancellation_are_requester_and_conversation_scoped(chat):
    parent = await create(chat, node("one"))
    foreign = copy.deepcopy(parent)
    foreign["thread_key"] = "f" * 32
    grant = access.resolve(chat.app, chat.settings, chat.store, foreign)
    with pytest.raises(ValueError, match="unavailable"):
        await task_graph.control(
            chat, foreign, grant, task_graph.Call(action="cancel", queue_id=parent["event_id"])
        )
    assert not await request_controls.cancelled(chat.store, parent["event_id"])


async def test_shared_member_cannot_stop_another_requesters_queue(chat):
    from mica.agents.analysis.registry import Registry

    def publish(doc):
        doc.share(chat.pid, "alice@example.com", "bob@example.com")
        doc.publish(chat.pid, "alice@example.com", "T1", "C1")
        doc.bind("T1", "C1", "alice@example.com", chat.pid, private=False, force=True)
        return chat.pid

    Registry(chat.store).update("publish", "alice@example.com", "publish", publish)
    chat.settings.flexible_harness = True
    chat.decision.return_value = conversation.Decision(
        route="workflow", reply="Queued", task_queue=task_graph.Call(
            action="create", tasks=[node("one")],
        ),
    )
    eid = await chat.accept(payload(channel="C1", channel_type="channel"))
    await chat.process(eid, 0)
    message = payload("2.0", "1.0", "cancel", channel="C1",
                      channel_type="channel", kind="app_mention")
    message["event"]["user"] = "U2"
    other = await chat.accept(message)
    assert await chat.process(other, 0) == "complete"
    assert not await request_controls.cancelled(chat.store, eid)
    parent = await chat.repository.get_connector_state("claude-event:" + eid)
    assert parent["status"] == "pending"
    assert not await request_controls.cancelled(chat.store, task_graph.child_id(eid, "one"))


async def test_queue_children_still_require_compute_approval_and_cancel_before_dispatch(chat):
    from mica.agents.analysis import compute_approval

    parent = await create(chat, node("one"))
    one = await child(chat, parent, "one")
    chat.decision.return_value = conversation.Decision(route="compute", reply="Run the analysis.")
    original = compute_approval.ready
    compute_approval.ready = AsyncMock(return_value=False)
    try:
        await chat.process(one["event_id"], 0)
        compute_approval.ready.assert_awaited_once()
        chat.test_client.create_job.assert_not_called()
        await task_graph.signal_cancel(chat, parent["event_id"])
        await chat.process(one["event_id"], 1)
        assert (await child(chat, parent, "one"))["status"] == "failed"
        chat.test_client.create_job.assert_not_called()
    finally:
        compute_approval.ready = original


async def test_crash_after_child_save_before_dispatch_recovers_same_identity(chat, monkeypatch):
    parent = await create(chat, node("one"), node("after", ["one"]))
    await done(chat, parent, "one")
    chat.schedule.side_effect = RuntimeError("queue temporarily unavailable")
    with pytest.raises(RuntimeError, match="temporarily"):
        await task_graph.advance(chat, parent)
    saved = await child(chat, parent, "after")
    assert saved
    chat.schedule.side_effect = None
    parent = await chat.repository.get_connector_state("claude-event:" + parent["event_id"])
    await task_graph.advance(chat, parent)
    assert (await child(chat, parent, "after"))["event_id"] == saved["event_id"]
    assert [r["state"] for r in parent["task_graph"]] == ["complete", "running"]
    assert any(c.args == (saved["event_id"], 0) for c in chat.schedule.call_args_list)


async def test_shared_queue_runs_keep_distinct_output_prefixes(chat):
    from mica.agents.analysis.config import CHECKPOINT_PREFIX
    from mica.agents.analysis.registry import Registry

    def publish(doc):
        doc.publish(chat.pid, "alice@example.com", "T1", "C1")
        doc.bind("T1", "C1", "alice@example.com", chat.pid, private=False, force=True)
        return chat.pid

    Registry(chat.store).update("publish", "alice@example.com", "publish", publish)
    chat.settings.flexible_harness = True
    chat.decision.return_value = conversation.Decision(
        route="workflow", reply="Queued", task_queue=task_graph.Call(
            action="create", tasks=[node("first"), node("second")]
        ),
    )
    eid = await chat.accept(payload(channel="C1", channel_type="channel"))
    await chat.process(eid, 0)
    parent = await chat.repository.get_connector_state("claude-event:" + eid)
    chat.decision.return_value = conversation.Decision(route="compute", reply="Run this sample.")
    prefixes = []
    for name in ["first", "second"]:
        saved = await child(chat, parent, name)
        assert await chat.process(saved["event_id"], 0) == "running"
        manifest = json.loads(chat.store.get(
            f"{CHECKPOINT_PREFIX}/events/{saved['event_id']}/request.json"
        )[0])
        assert not manifest["private"]
        prefixes.append(manifest["run_prefix"])
    assert len(set(prefixes)) == 2
    assert all(prefix.startswith(f"projects/{chat.pid}/runs/") for prefix in prefixes)
    assert chat.test_client.create_job.call_count == 2


async def test_real_conversation_tool_creates_all_requested_transfers(chat, monkeypatch):
    from mica.agents.analysis.transfers import Transfers

    monkeypatch.setattr(Transfers, "configuration", AsyncMock())
    sdk = Client(
        tool(
            "task_queue",
            action="create",
            tasks=[
                node("sample1", kind="transfer"),
                node("sample2", kind="transfer"),
            ],
        )
    )
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    eid = await chat.accept(payload(text="Queue transfers for sample1 and sample2."))
    await chat.process(eid, 0)
    parent = await chat.repository.get_connector_state("claude-event:" + eid)
    assert len(parent["task_graph"]) == 2
    assert await child(chat, parent, "sample1") and await child(chat, parent, "sample2")
    chat.test_client.create_job.assert_not_called()


async def test_invalid_provider_graph_is_repaired_before_any_child_is_saved(chat, monkeypatch):
    sdk = Client(
        tool("task_queue", action="create", tasks=[node("bad", ["bad"])]),
        tool("task_queue", action="create", tasks=[node("valid")]),
    )
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    eid = await chat.accept(payload(text="Queue the analysis."))
    await chat.process(eid, 0)
    assert sdk.calls[-1]["messages"][-1]["content"][0]["is_error"]
    parent = await chat.repository.get_connector_state("claude-event:" + eid)
    assert not await child(chat, parent, "bad")
    assert await child(chat, parent, "valid")


async def test_queued_analysis_uses_normal_compute_dispatch_and_rejects_nested_graphs(
    chat, monkeypatch
):
    parent = await create(chat, node("one"))
    one = await child(chat, parent, "one")
    sdk = Client(tool("task_queue", action="create", tasks=[node("nested")]), reply("Need input."))
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    await chat.process(one["event_id"], 0)
    assert "task_queue" not in {t["name"] for t in sdk.calls[0]["tools"]}
    assert sdk.calls[-1]["messages"][-1]["content"][0]["is_error"]
    chat.test_client.create_job.assert_not_called()
