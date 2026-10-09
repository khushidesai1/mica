"""Trusted launch selection and router compatibility, entirely offline."""
# ruff: noqa: F811

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_compute import compute, compute_bridge  # noqa: F401
from test_analysis_direct import chat, payload  # noqa: F401

from pistachio.agents.analysis import compute_batch, compute_control, direct, pricing
from pistachio.agents.analysis.compute_tools import ComputeManager
from pistachio.agents.analysis.config import Settings

BASELINE = Path(__file__).parent / "fixtures/compute/prompts-before-launch.json"


@pytest.mark.parametrize(
    "category,instruction,tier,expected_tier,mode",
    [
        ("analysis", None, "xlarge", "standard", "SPOT"),
        ("analysis", True, "large", "standard", "STANDARD"),
        ("analysis", False, "large", "standard", "SPOT"),
        ("training", None, "standard", "large", "STANDARD"),
        ("full_segmentation", None, "large", "large", "STANDARD"),
        ("full_ingest", None, "xlarge", "xlarge", "STANDARD"),
        ("training", False, "standard", "large", "SPOT"),
    ],
)
def test_trusted_launch_table(category, instruction, tier, expected_tier, mode):
    worker = direct.WorkerChoice(
        category=category,
        on_demand=instruction,
        tier=tier,
        reason="Measured input requires this worker.",
    )
    result = compute_control.launch_choice(worker, Settings(_env_file=None, on_demand_enabled=True))
    assert result == {
        "tier": expected_tier,
        "provisioning": mode,
        "category": category,
        "reason": worker.reason,
    }


def test_missing_disabled_and_capped_launch_choices():
    settings = Settings(
        _env_file=None, on_demand_enabled=True, max_tier="large", launch_heavy_tier="xlarge"
    )
    worker = direct.WorkerChoice(category="training", tier="xlarge", reason="Train a new model.")
    assert compute_control.launch_choice(worker, settings)["tier"] == "large"
    assert compute_control.launch_choice(None, settings) == {
        "tier": "standard",
        "provisioning": "SPOT",
        "category": "analysis",
        "reason": "",
    }
    settings.on_demand_enabled = False
    assert compute_control.launch_choice(worker, settings) == compute_control.launch_choice(
        None, settings
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_router_worker_output_and_flag_off_request_compatibility(monkeypatch, enabled):
    sent = []
    context = {"request": "Run Proseg on the full sample", "recent_messages": []}

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "route_request",
                        "input": {
                            "route": "compute",
                            "reply": "Running segmentation.",
                            "worker": {
                                "category": "full_segmentation",
                                "tier": "large",
                                "reason": "A new full sample segmentation.",
                            },
                        },
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    result = await direct.decide("fake", "model", context, on_demand_enabled=enabled)
    assert result.worker.category == "full_segmentation"
    assert len(sent) == 1
    assert json.loads(sent[0]["messages"][0]["content"][0]["text"]) == context
    baseline = json.loads(BASELINE.read_text())
    if not enabled:
        assert sent[0]["system"] == baseline["router_system"]
        assert sent[0]["tools"][0]["input_schema"] == baseline["router_schema"]
    else:
        assert "When route is compute, set worker" in sent[0]["system"]
        assert "worker" in sent[0]["tools"][0]["input_schema"]["properties"]


@pytest.fixture
def launch_prices(chat, monkeypatch):
    chat.settings.on_demand_enabled = True
    chat.settings.tiers_enabled = True
    chat.settings.compute_quote_only = False
    skus = json.loads(
        (Path(__file__).parent / "fixtures/compute/n2-us-central1-skus.json").read_text()
    )["skus"]
    monkeypatch.setattr(compute_batch, "GCSStorage", lambda bucket: chat.store)
    fetch = Mock(
        side_effect=lambda store, region, mode: pricing.from_skus(
            skus, region, compute_control.time.time(), mode
        )
    )
    monkeypatch.setattr(pricing, "current", fetch)
    return fetch


@pytest.mark.parametrize(
    "worker",
    [
        None,
        {
            "category": "full_segmentation",
            "tier": "large",
            "reason": "Runs Proseg over the full sample.",
        },
    ],
)
async def test_router_stores_validated_launch_and_starts_matching_job(chat, launch_prices, worker):
    chat.decision.return_value = direct.Decision(route="compute", reply="Working", worker=worker)
    event = await chat.accept(payload(text="Run Proseg over the full sample"))
    assert await chat.process(event, 0) == "running"
    record = await chat.repository.get_connector_state(f"claude-event:{event}")
    assert record["launch"] == compute_control.launch_choice(
        chat.decision.return_value.worker, chat.settings
    )
    mode, tier = ("STANDARD", "large") if worker else ("SPOT", "standard")
    assert record["provisioning"] == mode and record["tier"] == tier
    assert record["slot_pool"] == ("claude-large-slot" if worker else "claude-slot")
    policy = (
        chat.test_client.create_job.call_args.kwargs["job"].allocation_policy.instances[0].policy
    )
    assert policy.provisioning_model.name == mode
    assert policy.machine_type == ("n2-highmem-32" if worker else "n2-highmem-8")
    assert launch_prices.call_args.args[-1] == mode
    assert record["notice_text"] == (
        "Starting an on-demand worker (large: 32 CPUs, 256 GiB): Runs Proseg over the full sample."
        if worker
        else "Spinning up a compute worker (usually a few minutes)…"
    )


async def test_heavy_launch_has_full_probe_budget(compute, compute_bridge):
    record = {
        "launch": {
            "tier": "large",
            "provisioning": "STANDARD",
            "category": "training",
            "reason": "Fits a model on the entire sample.",
        }
    }
    pool, _, _ = await compute_batch.prepare_attempt(compute_bridge, "a" * 32, record)
    assert pool == "claude-large-slot" and record["tier"] == "large"
    assert record["provisioning"] == "STANDARD"
    pricing.current.assert_called_once_with(compute_bridge.store, "us-central1", "STANDARD")
    assert record["launch_reason"] == record["launch"]["reason"]
    assert record["launch_category"] == "training"
    manifest = compute_batch.worker_manifest(record, compute.settings)["compute"]
    assert manifest["probe_affordable_seconds"] == compute.settings.compute_probe_seconds
    assert record["compute_probe_budget"] > compute.settings.compute_probe_budget_usd


def test_legacy_start_and_disabled_worker_prompt_are_unchanged(compute):
    compute.settings.on_demand_enabled = False
    record = {}
    compute_control.initialize(record, compute.settings, compute.rates["SPOT"])
    assert record["tier"] == "standard" and record["provisioning"] == "SPOT"
    assert record["compute_probe_budget"] == compute.settings.compute_probe_budget_usd
    sampler = SimpleNamespace(cpus=8, memory_gib=64, sample=lambda: SimpleNamespace(free=50))
    manifest = compute_batch.worker_manifest(record, compute.settings)["compute"]
    assert (
        ComputeManager(compute.settings, manifest, sampler, AsyncMock()).prompt()
        == (json.loads(BASELINE.read_text())["worker_compute"])
    )


async def test_correct_heavy_launch_continues_without_relaunch(compute, compute_bridge):
    from pistachio.agents.analysis.compute_control import ComputeCall
    from pistachio.agents.analysis.compute_quotes import Estimate

    record = {
        "project_id": "project",
        "principal": "scientist",
        "launch": {
            "tier": "large",
            "provisioning": "STANDARD",
            "category": "full_segmentation",
            "reason": "Full sample Proseg is a long process.",
        },
    }
    _, _, seconds = await compute_batch.prepare_attempt(compute_bridge, "a" * 32, record)
    now = compute_control.time.time()
    record.update(started=now, max_run_seconds=seconds)
    quoted = compute_control.operate(
        "a" * 32,
        record,
        ComputeCall(
            attempt=0,
            action="estimate",
            estimate=Estimate(
                tier="large",
                hours_low=1,
                hours_high=3.5,
                peak_gib=180,
                task_peak_gib=180,
                disk_gb=50,
                basis="Dense subset measured on this worker.",
            ),
        ),
        compute.settings,
        compute.lookup,
        now,
    )
    result = compute_control.operate(
        "a" * 32,
        record,
        ComputeCall(
            attempt=0,
            action="request",
            quote_id=quoted["id"],
            handoff="Continue on this worker.",
        ),
        compute.settings,
        compute.lookup,
        now,
    )
    assert result["status"] == "continue"
    assert "compute_pending" not in record
    assert record["attempt"] == 0 and record["escalations"] == 0


def test_bounded_measure_accepts_training_label():
    script = Path(__file__).resolve().parents[1] / "tools/measure.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--label",
            "training",
            "--seconds",
            "1",
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stderr.split("PROBE_RESOURCES ", 1)[1])
    assert data["label"] == "training" and data["exit_code"] == 0
    assert not data["timed_out"]


@pytest.fixture
async def running_heavy(chat, launch_prices):
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
    assert 0 < task.max_run_duration.total_seconds() <= 172800
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
        compute_control.time, "time", lambda: record["started"] + record["max_run_seconds"] + 3601
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
        f"{compute_batch.event_prefix(event)}/attempt-a0.json",
        json.dumps(
            {"event_id": event, "attempt": 0, "model_cost": 0.2, "outcome": "complete"}
        ).encode(),
    )
    chat.test_states[record["job"]] = state
    assert await chat.process(event, 1) == status
    assert "claude-large-slot:0" not in chat.repository.event_claims
    chat.test_client.create_job.assert_called_once()
