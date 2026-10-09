"""Offline worker-selection and continuation tests; never start cloud jobs."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response
from pydantic import ValidationError
from starlette.requests import Request

from pistachio.agents.analysis import compute_batch, compute_control, compute_interactions, pricing
from pistachio.agents.analysis.compute_control import ComputeCall, operate
from pistachio.agents.analysis.compute_quotes import Estimate, affordable_seconds
from pistachio.agents.analysis.compute_tools import ComputeManager
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import LocalStorage
from pistachio.api import slack_analysis

NOW = 1_800_000_000.0
EVENT = "a" * 32


@pytest.fixture
def compute(monkeypatch):
    monkeypatch.setattr(compute_control.time, "time", lambda: NOW)
    path = Path(__file__).parent / "fixtures/compute/n2-us-central1-skus.json"
    skus = json.loads(path.read_text())["skus"]
    rates = {
        mode: pricing.from_skus(skus, "us-central1", NOW, mode) for mode in ("SPOT", "STANDARD")
    }
    settings = Settings(
        _env_file=None, tiers_enabled=True, compute_quote_only=False, on_demand_enabled=True
    )
    record = {"project_id": "project", "principal": "alice@example.com"}
    compute_control.initialize(record, settings, rates["SPOT"])
    record.update(started=NOW, max_run_seconds=7200, compute_hourly=0.4)
    return SimpleNamespace(
        settings=settings, record=record, rates=rates, lookup=Mock(side_effect=rates.__getitem__)
    )


def estimate_call(hours=0.5, tier="standard"):
    return ComputeCall(
        attempt=0,
        action="estimate",
        estimate=Estimate(
            tier=tier,
            hours_low=0.1,
            hours_high=hours,
            peak_gib=20,
            task_peak_gib=20,
            disk_gb=1,
            basis="Representative subset measured for an offline test.",
        ),
    )


def estimate_and_request(compute, hours=0.5, tier="standard"):
    q = operate(
        EVENT, compute.record, estimate_call(hours, tier), compute.settings, compute.lookup, NOW
    )
    result = operate(
        EVENT,
        compute.record,
        ComputeCall(
            attempt=0, action="request", quote_id=q["id"], handoff="Prepared inputs saved."
        ),
        compute.settings,
        compute.lookup,
        NOW,
    )
    return q, result


def test_on_demand_setting_defaults_bounds_and_environment(monkeypatch):
    defaults = Settings(_env_file=None)
    assert defaults.on_demand_enabled is False
    assert defaults.on_demand_step_minutes == 30
    for value in (9, 1441):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, on_demand_step_minutes=value)
    monkeypatch.setenv("AGENT_ON_DEMAND_ENABLED", "true")
    monkeypatch.setenv("AGENT_ON_DEMAND_STEP_MINUTES", "60")
    assert Settings(_env_file=None).on_demand_enabled
    assert Settings(_env_file=None).on_demand_step_minutes == 60


@pytest.mark.parametrize(
    "enabled,minutes,hours,mode,status",
    [
        (False, 30, 1, "SPOT", "continue"),
        (True, 30, 0.499, "SPOT", "continue"),
        (True, 30, 0.5, "STANDARD", "checkpoint"),
        (True, 30, 0.501, "STANDARD", "checkpoint"),
        (True, 60, 0.5, "SPOT", "continue"),
        (True, 60, 1, "STANDARD", "checkpoint"),
    ],
)
def test_threshold_is_controller_owned(compute, enabled, minutes, hours, mode, status):
    compute.settings.on_demand_enabled = enabled
    compute.settings.on_demand_step_minutes = minutes
    q, result = estimate_and_request(compute, hours)
    assert q["provisioning"] == mode
    assert result["status"] == status
    assert compute.lookup.call_args_list[0].args == (mode,)
    assert compute.lookup.call_args_list[1].args == (mode,)
    # Quoting/requesting does not pretend the running worker already switched.
    assert compute.record.get("provisioning", "SPOT") == "SPOT"
    assert compute.record["escalations"] == 0


def test_legacy_record_without_mode_still_uses_spot(compute):
    compute.record.pop("provisioning", None)
    q, result = estimate_and_request(compute, 0.2)
    assert q["provisioning"] == "SPOT"
    assert result["status"] == "continue"


def test_on_demand_request_never_downgrades_for_a_shorter_estimate(compute):
    compute.record["provisioning"] = "STANDARD"
    q, result = estimate_and_request(compute, 0.2)
    assert q["provisioning"] == "STANDARD"
    assert result["status"] == "continue"


def test_switch_allowed_after_the_one_size_change(compute):
    compute.record.update(tier="large", escalations=1)
    q, result = estimate_and_request(compute, tier="large")
    assert q["provisioning"] == "STANDARD"
    assert result["status"] == "checkpoint"
    assert compute.record["escalations"] == 1


def test_size_and_mode_change_share_one_handoff_and_duplicate_request(compute):
    q, result = estimate_and_request(compute, tier="large")
    assert result["status"] == "checkpoint"
    pending = compute.record["compute_pending"]
    assert pending["quote"]["estimate"]["tier"] == "large"
    assert pending["quote"]["provisioning"] == "STANDARD"
    duplicate = operate(
        EVENT,
        compute.record,
        ComputeCall(
            attempt=0, action="request", quote_id=q["id"], handoff="Prepared inputs saved."
        ),
        compute.settings,
        compute.lookup,
        NOW,
    )
    assert duplicate == result
    assert compute.record["compute_pending"] == pending
    assert compute.lookup.call_count == 2


def test_size_change_limit_still_applies_on_demand(compute):
    compute.record.update(tier="large", escalations=1, provisioning="STANDARD")
    with pytest.raises(ValueError, match="already resized"):
        estimate_and_request(compute, tier="xlarge")
    compute.lookup.assert_not_called()


def test_quote_only_never_starts_a_mode_handoff(compute):
    compute.record["quote_only"] = True
    q, result = estimate_and_request(compute)
    assert q["provisioning"] == "STANDARD"
    assert result["status"] == "quote_only"
    assert "compute_pending" not in compute.record


def test_mismatched_price_mode_is_rejected(compute):
    compute.lookup.side_effect = None
    compute.lookup.return_value = compute.rates["SPOT"]
    with pytest.raises(ValueError, match="provisioning"):
        estimate_and_request(compute)


def test_price_refresh_uses_time_after_lookup(compute, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(compute_control.time, "time", lambda: clock[0])

    def refresh(mode):
        clock[0] += 1
        return compute.rates[mode].model_copy(
            update={"fetched_at": clock[0], "expires_at": clock[0] + 86400}
        )

    compute.lookup.side_effect = refresh
    q, result = estimate_and_request(compute)
    assert q["provisioning"] == "STANDARD"
    assert result["status"] == "checkpoint"
    assert compute.record["compute_quote"]["spent_usd"] > 0


def test_agent_cannot_choose_mode():
    body = estimate_call().model_dump()
    body["estimate"]["provisioning"] = "STANDARD"
    with pytest.raises(ValidationError):
        ComputeCall.model_validate(body)


async def test_compute_api_fetches_the_selected_prices(compute, repository, tmp_path, monkeypatch):
    bridge = SimpleNamespace(
        settings=compute.settings,
        repository=repository,
        app=SimpleNamespace(gcp_region="us-central1"),
    )
    store = LocalStorage(tmp_path)
    monkeypatch.setattr(
        slack_analysis, "worker_context", AsyncMock(return_value=(bridge, store, compute.record))
    )
    fetch = Mock(side_effect=lambda store, region, mode: compute.rates[mode])
    monkeypatch.setattr(pricing, "current", fetch)

    async def receive():
        return {"type": "http.request", "body": estimate_call().model_dump_json().encode()}

    request = Request({"type": "http", "headers": []}, receive=receive)
    result = await slack_analysis.compute_operation(EVENT, request, Response())
    assert result["provisioning"] == "STANDARD"
    fetch.assert_called_once_with(store, "us-central1", "STANDARD")
    saved = await repository.get_connector_state(f"claude-event:{EVENT}")
    assert saved["compute_quote"]["provisioning"] == "STANDARD"


@pytest.fixture
def compute_bridge(compute, repository, tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    monkeypatch.setattr(compute_batch, "GCSStorage", lambda bucket: store)
    monkeypatch.setattr(
        pricing, "current", Mock(side_effect=lambda store, region, mode="SPOT": compute.rates[mode])
    )
    return SimpleNamespace(
        settings=compute.settings,
        repository=repository,
        store=store,
        notify=AsyncMock(),
        app=SimpleNamespace(
            gcp_project="test-project",
            gcp_region="us-central1",
            gcs_bucket="test-artifacts",
            slack_bot_token="test",
        ),
    )


async def test_prepare_attempt_uses_mode_prices_and_budget(compute, compute_bridge):
    limits = {}
    for mode in ("SPOT", "STANDARD"):
        record = {**compute.record, "provisioning": mode, "compute_limit": 5.0}
        _, _, limits[mode] = await compute_batch.prepare_attempt(compute_bridge, EVENT, record)
        pricing.current.assert_called_with(compute_bridge.store, "us-central1", mode)
        assert record["rates"]["provisioning"] == mode
        assert limits[mode] == affordable_seconds(
            compute.rates[mode],
            "standard",
            3.0,
            ceiling=compute.settings.max_run_seconds,
        )
        assert record["model_reserved"] == compute.record["model_limit"]
    assert 600 <= limits["STANDARD"] < limits["SPOT"]


@pytest.mark.parametrize(
    "old_tier,escalations,new_tier,expected",
    [
        ("standard", 0, "standard", 0),
        ("large", 1, "large", 1),
        ("standard", 0, "large", 1),
    ],
)
async def test_terminal_carries_mode_without_extra_size_change(
    compute, compute_bridge, old_tier, escalations, new_tier, expected
):
    record = compute.record
    record.update(
        tier=old_tier,
        escalations=escalations,
        slot=0,
        slot_pool="claude-slot",
        slot_token="test-token",
    )
    _, result = estimate_and_request(compute, tier=new_tier)
    assert result["status"] == "checkpoint"
    pending_quote = record["compute_pending"]["quote"]
    prefix = compute_batch.event_prefix(EVENT)
    archive = f"{prefix}/handoff-a0-{'b' * 32}.tar.gz"
    compute_bridge.store.put(
        f"{prefix}/attempt-a0.json",
        json.dumps(
            {
                "event_id": EVENT,
                "attempt": 0,
                "model_cost": 0.2,
                "outcome": "handoff",
                "archive": archive,
            }
        ).encode(),
    )
    assert await compute_batch.terminal(compute_bridge, EVENT, record, Mock(), "SUCCEEDED")
    assert record["provisioning"] == "STANDARD"
    assert record["tier"] == new_tier
    assert record["attempt"] == 1
    assert record["escalations"] == expected
    assert record["status"] == "pending"
    assert record["resume"]["archive"] == archive
    assert record["compute_quote"] == pending_quote
    assert record["model_spent"] == 0.2
    assert "compute_pending" not in record
    assert "slot_token" not in record
    notice = compute_bridge.notify.call_args.args[-1]
    assert "Moving to an on-demand worker" in notice
    assert "(on-demand)" in notice
    await compute_batch.prepare_attempt(compute_bridge, EVENT, record)
    assert record["compute_quote"]["provisioning"] == "STANDARD"
    assert record["rates"]["provisioning"] == "STANDARD"
    assert record["compute_quote"]["reason"] == pending_quote["reason"]
    assert pending_quote["reason"] in notice


@pytest.mark.parametrize("enabled,mode", [(False, "SPOT"), (True, "SPOT"), (True, "STANDARD")])
def test_worker_prompt_uses_controller_manifest(compute, enabled, mode):
    compute.settings.on_demand_enabled = enabled
    compute.settings.on_demand_step_minutes = 45
    compute.settings.on_demand_total_hours = 6
    compute.record["provisioning"] = mode
    compute.record["launch_reason"] = "Compares existing transcript counts."
    manifest = compute_batch.worker_manifest(compute.record, compute.settings)["compute"]
    assert manifest["provisioning"] == mode
    # No new environment variables: the trusted manifest supplies the policy.
    worker_settings = Settings(_env_file=None, tiers_enabled=True)
    sampler = SimpleNamespace(cpus=8, memory_gib=64, sample=lambda: SimpleNamespace(free=50))
    prompt = ComputeManager(worker_settings, manifest, sampler, AsyncMock()).prompt()
    assert f"n2-highmem-8, {'Spot' if mode == 'SPOT' else 'on-demand'};" in prompt
    expected_mode = "Spot" if mode == "SPOT" else "on-demand"
    for line in (
        f"This worker was chosen at launch: {expected_mode}, standard "
        "(Compares existing transcript counts).",
        "on the same tier and mode, request_worker continues here without moving",
        "any single command of 45+ minutes",
        "unless the total exceeds 6 hours",
        "Save each unit's result in the workspace as it finishes",
        "Set longest_step_minutes to the longest single command, not the total",
    ):
        assert (line in prompt) is enabled
    assert "Estimates of 45 minutes or more" not in prompt


async def test_approval_refreshes_on_demand_prices(compute, compute_bridge, monkeypatch):
    compute.record["compute_limit"] = 1.0
    estimate_and_request(compute)
    record = compute.record
    record.update(
        provisioning="STANDARD",
        status="awaiting_approval",
        approval_expires=NOW + 3600,
        payload={"team_id": "T1", "event": {"channel": "D1", "user": "U1"}},
        thread_key="thread",
        access_signature="signature",
    )
    monkeypatch.setattr(
        compute_interactions, "resolve_email", Mock(return_value=record["principal"])
    )
    monkeypatch.setattr(
        compute_interactions.access,
        "resolve",
        Mock(return_value=SimpleNamespace(allowed=True, signature="signature")),
    )
    monkeypatch.setattr(
        compute_interactions.conversation_memory, "is_current", Mock(return_value=True)
    )
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: AsyncMock())
    await compute_bridge.repository.set_connector_state(f"claude-event:{EVENT}", record)
    await compute_interactions.handle(
        compute_bridge,
        compute_bridge.store,
        {
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "channel": {"id": "D1"},
            "actions": [
                {
                    "action_id": "pistachio_compute_approve",
                    "value": f"{EVENT}:{record['compute_quote']['id']}",
                }
            ],
        },
    )
    pricing.current.assert_called_once_with(compute_bridge.store, "us-central1", "STANDARD")
    saved = await compute_bridge.repository.get_connector_state(f"claude-event:{EVENT}")
    assert saved["status"] == "pending"
    assert saved["compute_quote"]["provisioning"] == "STANDARD"
    assert saved["compute_quote"]["reason"] == record["compute_quote"]["reason"]
    assert saved["compute_limit"] >= saved["compute_quote"]["request_high_usd"]


@pytest.mark.parametrize(
    "hours,step,initial,mode,reason",
    [
        (3, 15, "SPOT", "SPOT", "Each step is short (about 15 min)"),
        (3, 60, "SPOT", "STANDARD", "longest uninterrupted step is about 60 min"),
        (3, 30, "SPOT", "STANDARD", "longest uninterrupted step is about 30 min"),
        (4, 15, "SPOT", "STANDARD", "Total work is about 4 h"),
        (0.2, 5, "STANDARD", "STANDARD", "Already on an on-demand worker."),
        (3, None, "SPOT", "STANDARD", "longest uninterrupted step is about 180 min"),
    ],
)
def test_fallback_uses_single_step_or_total_hours(compute, hours, step, initial, mode, reason):
    compute.record.update(provisioning=initial, max_run_seconds=86400)
    data = estimate_call(hours).model_dump()
    if step is not None:
        data["estimate"]["longest_step_minutes"] = step
    q = operate(
        EVENT,
        compute.record,
        ComputeCall.model_validate(data),
        compute.settings,
        compute.lookup,
        NOW,
    )
    assert q["provisioning"] == mode
    assert reason in q["reason"] and q["notice"].startswith(q["reason"])
    result = operate(
        EVENT,
        compute.record,
        ComputeCall(attempt=0, action="request", quote_id=q["id"], handoff="Saved the inputs."),
        compute.settings,
        compute.lookup,
        NOW,
    )
    assert result["status"] == ("continue" if initial == mode else "checkpoint")
    assert result["notice"].startswith(q["reason"])


def test_estimate_validates_step_and_supports_training():
    data = estimate_call(1).estimate.model_dump()
    with pytest.raises(ValidationError, match="longest"):
        Estimate.model_validate({**data, "longest_step_minutes": 61})
    assert Estimate.model_validate(data).longest_step_minutes is None
    assert Estimate.model_validate({**data, "longest_step_minutes": 60, "label": "training"})


def test_agent_tool_requires_numeric_longest_step(compute):
    manager = ComputeManager(compute.settings, {}, Mock(), AsyncMock())
    schema = manager.tools()[0].input_schema
    assert "longest_step_minutes" in schema["required"]
    assert schema["properties"]["longest_step_minutes"]["type"] == "number"


def test_fallback_settings_bounds_and_environment(monkeypatch):
    settings = Settings(_env_file=None)
    assert settings.on_demand_total_hours == 4
    assert settings.launch_heavy_tier == "large"
    for hours in (0.9, 169):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, on_demand_total_hours=hours)
    monkeypatch.setenv("AGENT_ON_DEMAND_TOTAL_HOURS", "6")
    monkeypatch.setenv("AGENT_LAUNCH_HEAVY_TIER", "xlarge")
    assert Settings(_env_file=None).on_demand_total_hours == 6
    assert Settings(_env_file=None).launch_heavy_tier == "xlarge"
