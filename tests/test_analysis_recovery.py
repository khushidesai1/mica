"""Capacity recovery is bounded, evidence-based and owned by the controller."""

import json

# ruff: noqa: F811
import pytest
from analysis_helpers import memory_message as message
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import direct, recovery
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import LocalStorage
from pistachio.agents.analysis.tiers import launch_choice

EVENT = "a" * 32


def record(settings, *, tier="standard", mode="SPOT"):
    result = {
        "project_id": "project",
        "launch": {
            "tier": tier,
            "provisioning": mode,
            "category": "analysis",
            "reason": "Input size",
        },
    }
    recovery.initialize(result, settings, now=100)
    return result


def capacity(**kwargs):
    return recovery.RecoverCall(
        attempt=0,
        cause="capacity",
        memory_gib=100,
        evidence="Image headers: 100 GiB expanded working arrays",
        handoff="Run saved script",
        **kwargs,
    )


@pytest.mark.parametrize("tier", ["standard", "large", "xlarge"])
def test_launch_respects_analysis_size_and_uniform_deadline(tier):
    settings = Settings(_env_file=None)
    choice = launch_choice(
        direct.WorkerChoice(category="analysis", tier=tier, reason="Measured input requirement"),
        settings,
    )
    assert choice["tier"] == tier and choice["provisioning"] == "SPOT"
    state = record(settings, tier=tier)
    assert recovery.limits(state, settings, now=100)[0] == settings.max_run_seconds


def test_model_reserve_survives_missing_usage_and_deadline_does_not_reset():
    settings = Settings(_env_file=None)
    state = record(settings)
    seconds, allowance = recovery.limits(state, settings, now=100)
    assert allowance == pytest.approx(4)
    state["model_reserved"] = allowance
    recovery.settle(state, None)
    recovery.settle(state, None)
    assert state["model_spent"] == allowance
    state.update(attempt=1, resize_count=1, provisioning="STANDARD")
    assert recovery.limits(state, settings, now=200) == (seconds - 100, 1)


def test_capacity_chooses_destination_directly_and_rejects_unknown_kills(tmp_path):
    settings = Settings(_env_file=None)
    storage = LocalStorage(tmp_path)
    state = record(settings)
    body = capacity().model_copy(update={"memory_gib": 250})
    result = recovery.operate(EVENT, state, body, settings, storage, now=101)
    assert result["status"] == "checkpoint"
    assert state["recovery_pending"]["tier"] == "xlarge"
    assert recovery.operate(EVENT, state, body, settings, storage, now=102) == result
    assert recovery.classify(137, {}) == "failed"
    assert recovery.classify(137, {"peak_gib": 63}) == "failed"
    assert recovery.classify(137, {"oom_kills": 1}) == "oom"
    assert recovery.classify(50001, {"oom_kills": 1}) == "preempted"


def test_capacity_requires_actual_shortfall_and_rejects_oversized_or_stale(tmp_path):
    settings = Settings(_env_file=None)
    storage = LocalStorage(tmp_path)
    for updates in ({"memory_gib": 5}, {"memory_gib": 900}, {"attempt": 1}):
        with pytest.raises(ValueError):
            recovery.operate(
                EVENT,
                record(settings),
                capacity().model_copy(update=updates),
                settings,
                storage,
                now=101,
            )
    with pytest.raises(ValueError):
        recovery.RecoverCall.model_validate({**capacity().model_dump(), "machine": "arbitrary"})
    with pytest.raises(ValueError):
        recovery.RecoverCall.model_validate({**capacity().model_dump(), "memory_gib": float("inf")})


def test_constraints_and_one_resize_are_enforced(tmp_path):
    settings = Settings(_env_file=None)
    storage = LocalStorage(tmp_path)
    for change in (
        {"resize_count": 1},
        {"cancel_requested": True},
        {"constraints": {"max_tier": "standard"}},
        {"attempt": 2},
    ):
        state = record(settings)
        state.update(change)
        with pytest.raises(ValueError):
            recovery.operate(EVENT, state, capacity(), settings, storage, now=101)


async def test_inventory_then_compute_in_one_request(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *args: {"entries": [{"size_bytes": 123}]})
    chat.decision.side_effect = [
        direct.Decision(route="inventory", reply="Checking input size"),
        direct.Decision(
            route="compute",
            reply="Working",
            worker=direct.WorkerChoice(
                category="full_ingest", tier="large", reason="Full sample ingest"
            ),
        ),
    ]
    state = await message(chat, "Ingest the whole sample", "1.0")
    assert state["status"] == "running" and state["tier"] == "large"
    assert chat.decision.call_args.args[2]["data_inventory"]["entries"][0]["size_bytes"] == 123
    assert chat.test_client.create_job.call_count == 1


async def test_preemption_restarts_once_and_cancellation_targets_successor(chat, monkeypatch):
    monkeypatch.setattr(recovery, "exit_code", lambda *args: 50001)
    chat.decision.return_value = direct.Decision(route="compute", reply="Working")
    initial = await message(chat, "Plot data", "1.0")
    event = initial["job"].rsplit("claude-", 1)[-1]
    chat.test_states[initial["job"]] = "FAILED"
    await chat.process(event, 1)
    await chat.process(event, 2)
    current = await chat.repository.get_connector_state("claude-event:" + event)
    assert current["job"].endswith("-r1")
    assert current["tier"] == "standard" and current["provisioning"] == "STANDARD"
    assert current["request_deadline"] == initial["request_deadline"]
    assert chat.test_client.create_job.call_count == 2
    await message(chat, "cancel", "2.0", thread="1.0")
    assert chat.test_client.delete_job.call_args.kwargs["name"] == current["job"]


async def test_oom_without_final_report_recovers_but_publication_is_not_replayed(chat, monkeypatch):
    monkeypatch.setattr(recovery, "exit_code", lambda *args: 137)
    chat.decision.return_value = direct.Decision(route="compute", reply="Working")
    initial = await message(chat, "Plot data", "1.0")
    event = initial["job"].rsplit("claude-", 1)[-1]
    prefix = recovery.event_prefix(event)
    chat.store.put(
        f"{prefix}/resources-a0.json",
        json.dumps(
            {
                "event_id": event,
                "attempt": 0,
                "oom_kills": 1,
                "peak_gib": 64,
            }
        ).encode(),
    )
    chat.test_states[initial["job"]] = "FAILED"
    await chat.process(event, 1)
    await chat.process(event, 2)
    current = await chat.repository.get_connector_state("claude-event:" + event)
    assert current["tier"] == "large" and current["attempt"] == 1
    assert current["model_spent"] == pytest.approx(4)
    assert "since the last save" in current["recovery_notice"]
    assert chat.test_client.create_job.call_count == 2

    chat.store.put(f"{prefix}/finalizing-a1.json", b"{}")
    chat.test_states[current["job"]] = "FAILED"
    await chat.process(event, 3)
    failed = await chat.repository.get_connector_state("claude-event:" + event)
    assert failed["status"] == "failed"
    assert chat.test_client.create_job.call_count == 2


async def test_preemption_then_oom_uses_three_attempts_without_resetting_guards(chat, monkeypatch):
    code = [50001]
    monkeypatch.setattr(recovery, "exit_code", lambda *args: code[0])
    chat.decision.return_value = direct.Decision(route="compute", reply="Working")
    first = await message(chat, "Analyze the sample", "1.0")
    event = first["job"].rsplit("claude-", 1)[-1]
    chat.test_states[first["job"]] = "FAILED"
    await chat.process(event, 1)
    await chat.process(event, 2)
    second = await chat.repository.get_connector_state("claude-event:" + event)
    code[0] = 137
    chat.store.put(
        f"{recovery.event_prefix(event)}/resources-a1.json",
        json.dumps(
            {
                "event_id": event,
                "attempt": 1,
                "oom_kills": 1,
            }
        ).encode(),
    )
    chat.test_states[second["job"]] = "FAILED"
    await chat.process(event, 3)
    await chat.process(event, 4)
    third = await chat.repository.get_connector_state("claude-event:" + event)
    assert third["attempt"] == 2 and third["tier"] == "large"
    assert third["resize_count"] == third["preemption_count"] == 1
    assert third["model_spent"] + third["model_reserved"] == first["model_limit"]
    assert third["request_deadline"] == first["request_deadline"]
    chat.store.put(
        f"{recovery.event_prefix(event)}/resources-a2.json",
        json.dumps(
            {
                "event_id": event,
                "attempt": 2,
                "oom_kills": 1,
            }
        ).encode(),
    )
    chat.test_states[third["job"]] = "FAILED"
    await chat.process(event, 5)
    await chat.process(event, 6)
    final = await chat.repository.get_connector_state("claude-event:" + event)
    assert final["status"] == "failed" and chat.test_client.create_job.call_count == 3


async def test_capacity_request_waits_for_terminal_and_duplicate_create_reconciles(chat):
    chat.decision.return_value = direct.Decision(route="compute", reply="Working")
    initial = await message(chat, "Analyze data", "1.0")
    event = initial["job"].rsplit("claude-", 1)[-1]
    state = await chat.repository.get_connector_state("claude-event:" + event)
    recovery.operate(event, state, capacity(), chat.settings, chat.store, now=state["created"])
    await chat.repository.set_connector_state("claude-event:" + event, state)
    await chat.process(event, 1)
    assert chat.test_client.create_job.call_count == 1
    chat.test_states[state["job"]] = "SUCCEEDED"
    await chat.process(event, 2)
    await chat.process(event, 3)
    await chat.process(event, 3)
    assert chat.test_client.create_job.call_count == 2


def test_missing_or_foreign_checkpoints_fall_back(tmp_path):
    store = LocalStorage(tmp_path)
    prefix = recovery.event_prefix(EVENT)
    store.put(
        f"{prefix}/partial-a0.json",
        json.dumps(
            {
                "event_id": "b" * 32,
                "attempt": 0,
                "archive": "foreign.tar.gz",
                "state": {},
            }
        ).encode(),
    )
    assert recovery.verified_resume(store, EVENT, 0) is None


def test_reported_model_overage_is_charged_before_any_successor():
    settings = Settings(_env_file=None)
    state = record(settings)
    state["model_reserved"] = 4
    recovery.settle(state, 5.1)
    assert state["model_spent"] == 5.1
    with pytest.raises(ValueError, match="allowance"):
        recovery.limits(state, settings, now=200)


async def test_original_deadline_stops_queued_worker_without_relaunch(bridge, monkeypatch):
    from test_analysis_batch import payload

    from pistachio.agents.analysis import batch

    now = [1000.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    event = await bridge.accept(payload())
    await bridge.process(event, 0)
    state = await bridge.repository.get_connector_state(f"claude-event:{event}")
    bridge.test_states[state["job"]] = "SCHEDULED"
    now[0] = state["request_deadline"] + 1
    await bridge.process(event, 1)
    bridge.test_client.delete_job.assert_called_once_with(name=state["job"])
    assert bridge.test_client.create_job.call_count == 1
    bridge.test_states[state["job"]] = "FAILED"
    await bridge.process(event, 2)
    latest = await bridge.repository.get_connector_state(f"claude-event:{event}")
    assert latest["status"] == "failed"
    assert "original time" in latest["notice_text"]
    assert bridge.test_client.create_job.call_count == 1


async def test_expired_interrupted_submission_does_not_create_a_worker(bridge, monkeypatch):
    from test_analysis_batch import payload

    from pistachio.agents.analysis import batch

    now = [1000.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    event = await bridge.accept(payload())
    create = bridge.test_client.create_job.side_effect
    bridge.test_client.create_job.side_effect = RuntimeError("submission interrupted")
    with pytest.raises(RuntimeError):
        await bridge.process(event, 0)
    state = await bridge.repository.get_connector_state(f"claude-event:{event}")
    assert state["status"] == "submitting"
    now[0] = state["request_deadline"] + 1
    bridge.test_client.create_job.side_effect = create
    await bridge.process(event, 0)
    assert bridge.test_client.create_job.call_count == 1
    latest = await bridge.repository.get_connector_state(f"claude-event:{event}")
    assert latest["status"] == "failed" and "original time" in latest["notice_text"]
