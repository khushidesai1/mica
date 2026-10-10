"""Job-based launch policy and on-demand lifecycle regressions."""

# ruff: noqa: F811
import json
import time
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat, payload  # noqa: F401

from pistachio.agents.analysis import direct, recovery
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.recovery_tools import RecoveryManager
from pistachio.agents.analysis.resources import Sampler
from pistachio.agents.analysis.tiers import launch_choice


@pytest.mark.parametrize(
    "category,tier,instruction,wanted,mode",
    [
        ("analysis", "standard", None, "standard", "SPOT"),
        ("analysis", "large", None, "large", "SPOT"),
        ("analysis", "xlarge", True, "xlarge", "STANDARD"),
        ("training", "standard", None, "large", "STANDARD"),
        ("full_segmentation", "large", False, "large", "SPOT"),
        ("full_ingest", "xlarge", None, "xlarge", "STANDARD"),
    ],
)
def test_launch_table(category, tier, instruction, wanted, mode):
    choice = launch_choice(
        direct.WorkerChoice(
            category=category, tier=tier, on_demand=instruction, reason="Measured input requirement"
        ),
        Settings(_env_file=None),
    )
    assert (choice["tier"], choice["provisioning"]) == (wanted, mode)


def test_cannot_silently_clamp_and_explicit_small_initial_tier():
    worker = direct.WorkerChoice(category="training", tier="xlarge", reason="Measured requirement")
    with pytest.raises(ValueError):
        launch_choice(worker, Settings(_env_file=None, max_tier="large"))
    worker = worker.model_copy(update={"tier": "standard", "tier_explicit": True})
    assert launch_choice(worker, Settings(_env_file=None))["tier"] == "standard"


@pytest.mark.parametrize("tier", ["standard", "large", "xlarge"])
def test_worker_has_long_commands_and_no_quote_gate(tmp_path, tier):
    settings = Settings(_env_file=None)
    manager = RecoveryManager(
        settings,
        {
            "attempt": 0,
            "tier": tier,
            "provisioning": "SPOT",
            "request_deadline": time.time() + 604800,
        },
        Sampler(tmp_path),
        AsyncMock(),
        save_stage=AsyncMock(),
    )
    assert manager.deny("Bash", {"timeout": 6 * 3600 * 1000}) == ""
    assert {t.name for t in manager.tools()} == {"recover_worker", "checkpoint_stage"}
    assert "$" not in manager.prompt() and tier in manager.prompt()


@pytest.fixture
async def running_heavy(chat):
    chat.decision.return_value = direct.Decision(
        route="compute",
        reply="Training.",
        worker=direct.WorkerChoice(category="training", reason="Train Amici on a full sample."),
    )
    event = await chat.accept(payload(text="Train Amici"))
    assert await chat.process(event, 0) == "running"
    record = await chat.repository.get_connector_state(f"claude-event:{event}")
    job = chat.test_client.create_job.call_args.kwargs["job"]
    assert job.allocation_policy.instances[0].policy.provisioning_model.name == "STANDARD"
    task = job.task_groups[0].task_spec
    assert 0 < task.max_run_duration.total_seconds() <= 604800
    assert task.max_retry_count == 0
    return event, record


async def test_on_demand_cancel_keeps_slot_until_deletion(chat, running_heavy):
    event, record = running_heavy
    cancel = await chat.accept(payload("2.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    chat.test_client.delete_job.assert_called_once_with(name=record["job"])
    assert "claude-large-slot:0" in chat.repository.event_claims
    del chat.test_states[record["job"]]
    assert await chat.process(event, 1) == "failed"
    assert await chat.process(cancel, 1) == "complete"
    assert "claude-large-slot:0" not in chat.repository.event_claims
    chat.test_client.create_job.assert_called_once()


async def test_on_demand_overdue_job_is_deleted_before_slot_release(
    chat, running_heavy, monkeypatch
):
    event, record = running_heavy
    monkeypatch.setattr(
        recovery.time, "time", lambda: record["started"] + record["max_run_seconds"] + 3601
    )
    assert await chat.process(event, 1) == "running"
    chat.test_client.delete_job.assert_called_once_with(name=record["job"])
    assert "claude-large-slot:0" in chat.repository.event_claims
    chat.test_states[record["job"]] = "DELETION_IN_PROGRESS"
    assert await chat.process(event, 2) == "running"
    assert "claude-large-slot:0" in chat.repository.event_claims
    del chat.test_states[record["job"]]
    assert await chat.process(event, 3) == "failed"
    assert "claude-large-slot:0" not in chat.repository.event_claims
    chat.test_client.create_job.assert_called_once()


@pytest.mark.parametrize("state,status", [("SUCCEEDED", "complete"), ("FAILED", "failed")])
async def test_finished_on_demand_job_releases_large_slot(chat, running_heavy, state, status):
    event, record = running_heavy
    chat.store.put(
        f"{recovery.event_prefix(event)}/attempt-a0.json",
        json.dumps(
            {"event_id": event, "attempt": 0, "model_cost": 0.2, "outcome": "complete"}
        ).encode(),
    )
    chat.test_states[record["job"]] = state
    assert await chat.process(event, 1) == status
    assert "claude-large-slot:0" not in chat.repository.event_claims
    chat.test_client.create_job.assert_called_once()
