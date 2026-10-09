"""Frozen pre-change Batch specifications; no cloud calls."""

import json
from pathlib import Path

import pytest
from google.cloud import batch_v1

from pistachio.agents.analysis.batch import job_spec
from pistachio.agents.analysis.config import Settings
from pistachio.config import Settings as AppSettings

FIXTURE = Path(__file__).parent / "fixtures/compute/spot-job-specs.json"
CASES = {
    "legacy": (False, {}),
    "standard": (True, {"duration": 1800, "budget": 5.0}),
    "large-continuation": (True, {"tier": "large", "attempt": 1, "duration": 1234, "budget": 3.0}),
}


def job_inputs(case):
    app = AppSettings(
        _env_file=None,
        env="test",
        gcp_project="test-project",
        gcp_region="us-central1",
        gcs_bucket="test-artifacts",
        batch_service_account="worker@example.com",
        batch_network="default",
        batch_subnetwork="default",
        control_plane_url="https://control.example.com",
        slack_agent_image="us-docker.pkg.dev/test/images/worker@sha256:" + "a" * 64,
    )
    enabled, kwargs = CASES[case]
    settings = Settings(
        _env_file=None, tiers_enabled=enabled, data_buckets="science-data,reference-data"
    )
    return app, settings, kwargs


def serialized(job):
    return batch_v1.Job.pb(job).SerializeToString(deterministic=True)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("enabled", [False, True])
def test_spot_spec_matches_frozen_original_byte_for_byte(case, enabled):
    app, settings, kwargs = job_inputs(case)
    settings.on_demand_enabled = enabled
    expected = batch_v1.Job.from_json(json.dumps(json.loads(FIXTURE.read_text())["jobs"][case]))
    assert serialized(job_spec(app, settings, "a" * 32, **kwargs)) == serialized(expected)
    assert serialized(job_spec(app, settings, "a" * 32, provisioning="SPOT", **kwargs)) == (
        serialized(expected)
    )


@pytest.mark.parametrize("case", CASES)
def test_on_demand_changes_only_the_provisioning_model(case):
    app, settings, kwargs = job_inputs(case)
    settings.on_demand_enabled = True
    job = job_spec(app, settings, "a" * 32, provisioning="STANDARD", **kwargs)
    policy = job.allocation_policy.instances[0].policy
    assert policy.provisioning_model.name == "STANDARD"
    policy.provisioning_model = batch_v1.AllocationPolicy.ProvisioningModel.SPOT
    expected = batch_v1.Job.from_json(json.dumps(json.loads(FIXTURE.read_text())["jobs"][case]))
    assert serialized(job) == serialized(expected)
