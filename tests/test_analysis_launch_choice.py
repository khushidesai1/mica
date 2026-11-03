"""Job-based launch policy and on-demand lifecycle regressions."""

# ruff: noqa: F811
import json
import time
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat, payload  # noqa: F401

from mica.agents.analysis import conversation, recovery, tiers
from mica.agents.analysis.config import Settings
from mica.agents.analysis.recovery_tools import RecoveryManager
from mica.agents.analysis.resources import Sampler
from mica.agents.analysis.tiers import WorkerUnavailable, capabilities, launch_choice


@pytest.mark.parametrize(
    "hours,tier,instruction,wanted,mode",
    [
        (None, "standard", None, "standard", "SPOT"),
        (1.5, "large", None, "large", "SPOT"),
        (0.5, "xlarge", True, "xlarge", "STANDARD"),
        (2, "standard", None, "standard", "STANDARD"),
        (8, "large", False, "large", "SPOT"),
        (4, "xlarge", None, "xlarge", "STANDARD"),
    ],
)
def test_launch_table(hours, tier, instruction, wanted, mode):
    choice = launch_choice(
        tiers.WorkerChoice(
            expected_hours=hours, tier=tier, on_demand=instruction,
            reason="Measured input requirement"
        ),
        Settings(_env_file=None),
    )
    assert (choice["tier"], choice["provisioning"]) == (wanted, mode)
    assert "category" not in choice


def test_duration_threshold_is_configured_and_old_categories_never_size_workers():
    legacy = tiers.WorkerChoice.model_validate(
        {"category": "training", "reason": "Historical training record"}
    )
    assert "category" not in legacy.model_dump()
    assert launch_choice(legacy, Settings(_env_file=None))["tier"] == "standard"
    settings = Settings(_env_file=None, on_demand_after_hours=4)
    worker = legacy.model_copy(update={"expected_hours": 3})
    assert launch_choice(worker, settings)["provisioning"] == "SPOT"
    settings.on_demand_after_hours = 3
    assert launch_choice(worker, settings)["provisioning"] == "STANDARD"
    # A long estimate chooses provisioning, not a new execution ceiling.
    long = tiers.WorkerChoice(expected_hours=720, reason="Measured month-long workload")
    assert launch_choice(long, settings)["provisioning"] == "STANDARD"
    assert tiers.resolve("standard", settings).max_seconds == settings.max_run_seconds


def test_cannot_silently_clamp_and_explicit_small_initial_tier():
    worker = tiers.WorkerChoice(category="training", tier="xlarge", reason="Measured requirement")
    with pytest.raises(ValueError):
        launch_choice(worker, Settings(_env_file=None, max_tier="large"))
    worker = worker.model_copy(update={"tier": "standard", "tier_explicit": True})
    assert launch_choice(worker, Settings(_env_file=None))["tier"] == "standard"


def test_cpu_memory_requirement_is_preserved_and_never_silently_undersized():
    settings = Settings(_env_file=None)
    worker = tiers.WorkerChoice(
        category="analysis", tier="large", min_memory_gib=128,
        reason="Explicit 128 GiB host requirement",
    )
    choice = launch_choice(worker, settings)
    assert choice["tier"] == "large" and choice["constraints"]["min_memory_gib"] == 128
    with pytest.raises(WorkerUnavailable, match="64 GiB host RAM"):
        launch_choice(worker.model_copy(update={"tier": "standard"}), settings)
    with pytest.raises(WorkerUnavailable, match="GPU VRAM"):
        launch_choice(worker.model_copy(update={"min_gpu_memory_gib": 16}), settings)


def test_compute_catalog_reflects_enabled_resources_and_effective_time_limits():
    settings = Settings(
        _env_file=None, max_tier="large", max_run_seconds=3600,
        gpu_tier_enabled=True, gpu_types="t4", gpu_max_seconds=7200,
        gpu_image="us-docker.pkg.dev/project/images/gpu@sha256:" + "b" * 64,
    )
    catalog = capabilities(settings)
    assert [(w["tier"], w["host_memory_gib"], w["gpu_memory_gib"])
            for w in catalog["workers"]] == [
        ("standard", 64, 0), ("large", 256, 0), ("gpu", 30, 16),
    ]
    assert all(w["max_seconds"] == 3600 for w in catalog["workers"])
    assert catalog["gpu_default_seconds"] == 3600
    assert not catalog["workers"][-1]["can_resize"]
    assert not catalog["provider_capacity_confirmed"] and not catalog["can_change_limits"]
    settings.gpu_tier_enabled = False
    settings.max_tier = "standard"
    assert [w["tier"] for w in capabilities(settings)["workers"]] == ["standard"]


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
    assert {t.name for t in manager.tools()} == {
        "recover_worker",
        "checkpoint_stage",
        "compute_allowance",
    }
    assert "$" not in manager.prompt() and tier in manager.prompt()


@pytest.fixture
async def running_heavy(chat):
    chat.decision.return_value = conversation.Decision(
        route="compute",
        reply="Training.",
        worker=tiers.WorkerChoice(
            expected_hours=3, tier="large", reason="Train Amici on a full sample."
        ),
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


async def test_on_demand_cancel_keeps_slot_until_deletion(chat, running_heavy, monkeypatch):
    event, record = running_heavy
    cancel = await chat.accept(payload("2.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    chat.test_client.delete_job.assert_not_called()
    cancelled = await chat.repository.get_connector_state("claude-event:" + event)
    from mica.agents.analysis import batch

    monkeypatch.setattr(batch.time, "time", lambda: cancelled["cancel_requested_at"] + 56)
    await chat.process(event, 1)
    chat.test_client.delete_job.assert_called_once_with(name=record["job"])
    assert f"claude-large-slot:{event}:0" in chat.repository.event_claims
    del chat.test_states[record["job"]]
    assert await chat.process(event, 2) == "failed"
    assert await chat.process(cancel, 1) == "complete"
    assert f"claude-large-slot:{event}:0" not in chat.repository.event_claims
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
    assert f"claude-large-slot:{event}:0" in chat.repository.event_claims
    chat.test_states[record["job"]] = "DELETION_IN_PROGRESS"
    assert await chat.process(event, 2) == "running"
    assert f"claude-large-slot:{event}:0" in chat.repository.event_claims
    del chat.test_states[record["job"]]
    assert await chat.process(event, 3) == "failed"
    assert f"claude-large-slot:{event}:0" not in chat.repository.event_claims
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
    assert f"claude-large-slot:{event}:0" not in chat.repository.event_claims
    chat.test_client.create_job.assert_called_once()


@pytest.mark.parametrize("tier", ["large", "xlarge"])
async def test_heavy_runs_launch_without_shared_cap(chat, monkeypatch, tier):
    # A legacy setting/lease cannot keep new independent heavy requests queued.
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_LARGE_RUNS", "1")
    assert "max_concurrent_large_runs" not in Settings(_env_file=None).model_dump()
    await chat.repository.begin_event("claude-large-slot:0", lease_seconds=7200)
    chat.decision.return_value = conversation.Decision(
        route="compute",
        reply="Training.",
        worker=tiers.WorkerChoice(expected_hours=3, tier=tier, reason="Full sample training"),
    )
    events = []
    for i in range(24):
        event = await chat.accept(payload(f"{i + 1}.0", text="Train this sample"))
        assert await chat.process(event, 0) == "running"
        events.append(event)
    assert chat.test_client.create_job.call_count == 24
    assert all(f"claude-large-slot:{event}:0" in chat.repository.event_claims for event in events)
    # Redelivery is idempotent, and the same conversation still runs in order.
    assert await chat.process(events[0], 1) == "running"
    followup = await chat.accept(payload("25.0", "1.0", "Then plot the result"))
    assert await chat.process(followup, 0) == "pending"
    assert chat.test_client.create_job.call_count == 24


async def test_existing_shared_heavy_lease_is_released_on_completion(chat, running_heavy):
    event, record = running_heavy
    await chat.repository.release_event(f"{record['slot_pool']}:0", record["slot_token"])
    legacy = await chat.repository.begin_event("claude-large-slot:0", lease_seconds=7200)
    record.update(slot_pool="claude-large-slot", slot_token=legacy.token)
    await chat.repository.set_connector_state(f"claude-event:{event}", record)
    chat.store.put(
        f"{recovery.event_prefix(event)}/attempt-a0.json",
        json.dumps(
            {"event_id": event, "attempt": 0, "model_cost": 0.2, "outcome": "complete"}
        ).encode(),
    )
    chat.test_states[record["job"]] = "SUCCEEDED"
    assert await chat.process(event, 1) == "complete"
    assert "claude-large-slot:0" not in chat.repository.event_claims
    chat.test_client.create_job.assert_called_once()


async def test_standard_pool_retains_its_configured_limit(chat):
    chat.settings.max_concurrent_runs = 1
    chat.decision.return_value = conversation.Decision(route="compute", reply="Plotting.")
    first = await chat.accept(payload(text="Plot sample one"))
    second = await chat.accept(payload("2.0", text="Plot sample two"))
    assert await chat.process(first, 0) == "running"
    assert await chat.process(second, 0) == "pending"
    chat.test_client.create_job.assert_called_once()
