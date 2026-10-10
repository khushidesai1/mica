"""Offline resource and request-wide model allowance contracts."""

import json

import pytest

from pistachio.agents.analysis.batch import job_spec
from pistachio.agents.analysis.config import Settings
from pistachio.config import Settings as AppSettings


def app_settings():
    return AppSettings(
        _env_file=None,
        slack_agent_image="us-docker.pkg.dev/p/i/slack@sha256:" + "a" * 64,
        gcp_project="fixture",
        gcs_bucket="artifacts",
        batch_service_account="worker@example.com",
        batch_network="default",
        batch_subnetwork="default",
    )


@pytest.mark.parametrize(
    "tier,cpus,ram,disk,seconds",
    [
        ("standard", 8, 64, 500, 604800),
        ("large", 32, 256, 1000, 604800),
        ("xlarge", 64, 512, 2000, 604800),
    ],
)
def test_tier_job_spec(tier, cpus, ram, disk, seconds):
    settings = Settings(_env_file=None, data_buckets="lab-data")
    job = job_spec(app_settings(), settings, "a" * 32, tier=tier, attempt=1)
    task = job.task_groups[0].task_spec
    policy = job.allocation_policy.instances[0].policy
    assert task.compute_resource.cpu_milli == cpus * 1000
    assert task.compute_resource.memory_mib == ram * 1024
    assert policy.machine_type == f"n2-highmem-{cpus}"
    assert policy.boot_disk.size_gb == disk
    assert task.max_run_duration.total_seconds() == seconds
    env = task.runnables[0].environment.variables
    assert "AGENT_TIERS_ENABLED" not in env
    assert env["AGENT_MAX_RUN_SECONDS"] == str(seconds)
    assert policy.provisioning_model.name == "SPOT"


def test_unknown_tier_is_rejected():
    settings = Settings(_env_file=None, data_buckets="lab-data")
    with pytest.raises(ValueError):
        job_spec(app_settings(), settings, "a" * 32, tier="unknown")


def test_attempt_uses_reserved_budget_and_shorter_deadline():
    settings = Settings(_env_file=None, data_buckets="lab-data")
    job = job_spec(
        app_settings(), settings, "a" * 32, tier="large", attempt=2, budget=1.25, duration=1800
    )
    task = job.task_groups[0].task_spec
    assert task.max_run_duration.total_seconds() == 1800
    assert task.runnables[0].environment.variables["AGENT_MAX_BUDGET_USD_PER_TURN"] == "1.25"
    assert task.runnables[0].environment.variables["AGENT_MAX_RUN_SECONDS"] == "1800"


def test_pinned_sdk_cumulative_cost_baseline(tmp_path):
    from pistachio.agents.analysis.recovery_tools import restored_cost

    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    path = tmp_path / ".claude/projects/synthetic" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "cost-state", "sessionId": sid, "totalCostUSD": 1.25})
        + "\n"
        + '{"partial":'
    )
    assert restored_cost(sid, tmp_path) == 1.25
    assert restored_cost(None, tmp_path) == 0
    path.write_text(
        json.dumps({"type": "cost-state", "sessionId": sid, "totalCostUSD": float("nan")})
    )
    assert restored_cost(sid, tmp_path) is None
