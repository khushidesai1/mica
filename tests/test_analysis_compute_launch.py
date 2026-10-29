"""Batch launch shape and provisioning contracts; no cloud calls."""

import pytest
from google.cloud import batch_v1

from mica.agents.analysis.batch import job_spec
from mica.agents.analysis.config import Settings
from mica.config import Settings as AppSettings

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
    _, kwargs = CASES[case]
    settings = Settings(_env_file=None, data_buckets="science-data,reference-data")
    return app, settings, kwargs


def serialized(job):
    return batch_v1.Job.pb(job).SerializeToString(deterministic=True)


@pytest.mark.parametrize("case", CASES)
def test_cpu_boot_disk_omits_image_selector_on_the_wire(case):
    app, settings, kwargs = job_inputs(case)
    job = batch_v1.Job.deserialize(serialized(job_spec(app, settings, "a" * 32, **kwargs)))
    disk = job.allocation_policy.instances[0].policy.boot_disk
    # Empty string still selects this oneof; only omission lets Batch choose its host.
    assert batch_v1.AllocationPolicy.Disk.pb(disk).WhichOneof("data_source") is None
    assert disk.type_ == "pd-balanced" and disk.size_gb >= 500


@pytest.mark.parametrize("case", CASES)
def test_on_demand_changes_only_the_provisioning_model(case):
    app, settings, kwargs = job_inputs(case)
    job = job_spec(app, settings, "a" * 32, provisioning="STANDARD", **kwargs)
    policy = job.allocation_policy.instances[0].policy
    assert policy.provisioning_model.name == "STANDARD"
    policy.provisioning_model = batch_v1.AllocationPolicy.ProvisioningModel.SPOT
    expected = job_spec(app, settings, "a" * 32, provisioning="SPOT", **kwargs)
    assert serialized(job) == serialized(expected)
