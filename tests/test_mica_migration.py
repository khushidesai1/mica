from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest


@pytest.fixture
def migration():
    return runpy.run_path(str(Path(__file__).parents[1] / "scripts/mica_migration.py"))


@pytest.fixture
def source():
    project = "lab-project"
    env = {
        "PISTACHIO_GCP_PROJECT": project,
        "PISTACHIO_GCP_REGION": "us-central1",
        "PISTACHIO_GCS_BUCKET": "pistachio-staging-artifacts-old",
        "PISTACHIO_CONTROL_PLANE_URL": "https://old.example",
        "PISTACHIO_BATCH_IMAGE": "registry/science@sha256:" + "0" * 64,
        "MICA_BATCH_IMAGE": "registry/science@sha256:" + "1" * 64,
        "MICA_SLACK_AGENT_IMAGE": "registry/claude@sha256:" + "2" * 64,
        "AGENT_MEMORY_TOKEN_SECRET": (
            f"projects/{project}/secrets/pistachio-memory-token/versions/3"
        ),
        "AGENT_WRITE_ALLOWED_PREFIXES": "gs://science-data/raw/",
        "AGENT_TRANSFERS_ENABLED": "true",
        "AGENT_FAILURE_CHANNEL": "C123",
    }
    for identity, key in {
        "control": "MICA_CLOUD_TASKS_SERVICE_ACCOUNT",
        "worker": "MICA_BATCH_SERVICE_ACCOUNT",
        "writer": "AGENT_WRITE_SERVICE_ACCOUNT",
    }.items():
        env[key] = f"pistachio-staging-{identity}@{project}.iam.gserviceaccount.com"
    entries = [{"name": name, "value": value} for name, value in env.items()]
    entries.extend(
        [
            {
                "name": "OPENAI_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "pistachio-staging-openai-api-key",
                        "key": "2",
                    }
                },
            },
            {
                "name": "MICA_GMAIL_REFRESH_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "mica-gmail-refresh-token",
                        "key": "4",
                    }
                },
            },
        ]
    )
    return {
        "metadata": {"name": "pistachio-staging", "namespace": "123456789"},
        "status": {"latestReadyRevisionName": "pistachio-staging-current"},
        "spec": {
            "template": {
                "metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "3"}},
                "spec": {
                    "timeoutSeconds": 900,
                    "containers": [
                        {
                            "image": "registry/api@sha256:" + "3" * 64,
                            "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                            "env": entries,
                        }
                    ],
                },
            }
        },
    }


def inputs(migration, source):
    return migration["capture"](source, {"bindings": []}, "mica-staging-artifacts-new")[0]


def plan_for(config, resource=None):
    return {
        "applyable": True,
        "complete": True,
        "variables": {key: {"value": value} for key, value in config.items()},
        "resource_changes": [] if resource is None else [resource],
    }


def resource(kind="google_cloud_tasks_queue", tf_name="tasks", actions=None, **after):
    return {
        "address": f"{kind}.{tf_name}",
        "type": kind,
        "mode": "managed",
        "change": {
            "actions": actions or ["create"],
            "before": None,
            "after": after or {"name": "mica-staging"},
        },
    }


def test_capture_preserves_effective_settings_and_existing_mica_secrets(migration, source):
    before = copy.deepcopy(source)
    config, evidence = migration["capture"](source, {"bindings": []}, "mica-staging-artifacts-new")
    assert source == before
    env = config["runtime_environment"]
    assert env["MICA_BATCH_IMAGE"].endswith("1" * 64)
    assert not any(key.startswith("PISTACHIO_") for key in env)
    assert env["AGENT_TRANSFERS_ENABLED"] == "true"
    assert config["runtime_secrets"]["OPENAI_API_KEY"] == {
        "secret": "mica-staging-openai-api-key",
        "version": "1",
    }
    assert config["runtime_secrets"]["MICA_GMAIL_REFRESH_TOKEN"]["version"] == "4"
    assert evidence["operator_secret_copies"][0]["source_version"] == "2"


def test_capture_rejects_plaintext_credentials(migration, source):
    source["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "ANTHROPIC_API_KEY", "value": "do-not-capture"}
    )
    with pytest.raises(ValueError, match="Plaintext credential"):
        inputs(migration, source)


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
def test_gate_rejects_all_destruction_orders(migration, source, actions):
    config = inputs(migration, source)
    with pytest.raises(ValueError, match="Deletion/replacement"):
        migration["check_plan"](plan_for(config, resource(actions=actions)), config)


def test_gate_accepts_only_new_creates_without_update_exception(migration, source):
    config = inputs(migration, source)
    assert len(migration["check_plan"](plan_for(config, resource()), config)) == 1
    with pytest.raises(ValueError, match="Unreviewed"):
        migration["check_plan"](plan_for(config, resource(actions=["update"])), config)


def test_gate_rejects_legacy_rename_even_with_update_exception(migration, source):
    config = inputs(migration, source)
    change = resource(actions=["update"], name="pistachio-staging")
    # Resource's Terraform address remains a valid new-stack address.
    change["address"] = "google_cloud_tasks_queue.tasks"
    change["change"]["after"] = {"name": "pistachio-staging"}
    with pytest.raises(ValueError, match="Legacy"):
        migration["check_plan"](plan_for(config, change), config, {change["address"]})


def test_gate_rejects_import_and_unrelated_singleton(migration, source):
    config = inputs(migration, source)
    change = resource()
    change["change"]["importing"] = {"id": "mica-staging"}
    with pytest.raises(ValueError, match="ownership/import"):
        migration["check_plan"](plan_for(config, change), config)
    with pytest.raises(ValueError, match="ownership/import"):
        migration["check_plan"](
            plan_for(config, resource("google_firestore_database", "state")), config
        )


def test_gate_rejects_different_saved_inputs(migration, source):
    config = inputs(migration, source)
    plan = plan_for(copy.deepcopy(config))
    plan["variables"]["runtime_environment"]["value"]["AGENT_TRANSFERS_ENABLED"] = "false"
    with pytest.raises(ValueError, match="reviewed inputs"):
        migration["check_plan"](plan, config)


@pytest.mark.parametrize(
    "kind,after",
    [
        ("google_cloud_scheduler_job", {"name": "mica-staging-watch", "paused": False}),
        ("google_pubsub_subscription", {"name": "mica-staging-gmail-push"}),
    ],
)
def test_gate_rejects_early_consumers(migration, source, kind, after):
    config = inputs(migration, source)
    with pytest.raises(ValueError, match="cutover|paused"):
        migration["check_plan"](plan_for(config, resource(kind, "runtime", **after)), config)


def test_gate_rejects_external_bucket_iam_change(migration, source):
    config = inputs(migration, source)
    change = resource("google_storage_bucket_iam_member", "writer", bucket="someone-elses-data")
    with pytest.raises(ValueError, match="IAM bucket"):
        migration["check_plan"](plan_for(config, change), config)


def test_private_capture_does_not_replace_existing_file(migration, tmp_path):
    path = tmp_path / "snapshot.json"
    migration["save_private"](path, {"metadata": True})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        migration["save_private"](path, {})


@pytest.mark.parametrize("key", ["AGENT_BUCKET", "AGENT_TRANSFER_SERVICE_ACCOUNT"])
def test_capture_rejects_hidden_legacy_resource_overrides(migration, source, key):
    source["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": key, "value": "pistachio-staging-old-resource"}
    )
    with pytest.raises(ValueError, match="override requires migration review"):
        inputs(migration, source)


@pytest.mark.parametrize("key", ["MICA_MIGRATION_DEFAULT_MODE", "AGENT_TRANSFERS_ENABLED"])
def test_planned_runtime_must_preserve_guard_and_features(migration, source, key):
    config = inputs(migration, source)
    expected = migration["expected_environment"](config)
    entries = [{"name": name, "value": value} for name, value in expected.items()]
    entries += [
        {"name": name, "value_source": [{"secret_key_ref": [ref]}]}
        for name, ref in config["runtime_secrets"].items()
    ]
    after = {
        "template": [
            {
                "service_account": expected["MICA_CLOUD_TASKS_SERVICE_ACCOUNT"],
                "containers": [{"image": config["api_image"], "env": entries}],
            }
        ]
    }
    migration["check_runtime"](after, config)
    next(entry for entry in entries if entry["name"] == key)["value"] = "incorrect"
    with pytest.raises(ValueError, match="runtime setting changed"):
        migration["check_runtime"](after, config)
    entries[:] = [entry for entry in entries if entry["name"] != key]
    with pytest.raises(ValueError, match="added or dropped"):
        migration["check_runtime"](after, config)
