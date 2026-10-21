"""Offline preparation and saved-plan checks for the isolated Mica stack.

This tool never applies Terraform, reads secret versions, or changes cloud resources.
Keep its metadata inputs/outputs in an ignored, access-restricted directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PIN = re.compile(r"@sha256:[a-f0-9]{64}$")
NAME = "mica-staging"
RESOURCE_TYPES = {
    "google_service_account.runtime",
    "google_storage_bucket.artifacts",
    "google_artifact_registry_repository.images",
    "google_secret_manager_secret.runtime",
    "google_cloud_tasks_queue.tasks",
    "google_pubsub_topic.runtime",
    "google_project_iam_member.runtime",
    "google_service_account_iam_member.service_agents",
    "google_service_account_iam_member.build_deployer",
    "google_service_account_iam_member.writer_controller",
    "google_pubsub_topic_iam_member.gmail_publisher",
    "google_storage_bucket_iam_member.artifacts",
    "google_project_iam_custom_role.writer",
    "google_project_iam_custom_role.export_reader",
    "google_storage_bucket_iam_member.writer",
    "google_storage_bucket_iam_member.export_reader",
    "google_cloud_run_v2_service.api",
    "google_cloud_run_v2_service_iam_member.invoker",
    "google_cloud_scheduler_job.runtime",
    "google_pubsub_subscription.runtime",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not overwrite a reviewed snapshot or follow a pre-existing symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def effective_environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = container.get("env", [])
    raw: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry["name"]
        if name in raw:
            raise ValueError(f"Duplicate runtime setting: {name}")
        raw[name] = entry
    result = {key: value for key, value in raw.items() if not key.startswith("PISTACHIO_")}
    for key, value in raw.items():
        if key.startswith("PISTACHIO_"):
            result.setdefault("MICA_" + key.removeprefix("PISTACHIO_"), value)
    return result


def capture(
    service: dict[str, Any], policy: dict[str, Any], artifact_bucket: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert the actual source service metadata into reviewed private inputs."""
    if service["metadata"]["name"] != "pistachio-staging":
        raise ValueError("Capture must use the existing pistachio-staging service")
    if not re.fullmatch(r"mica-staging-artifacts-[a-z0-9-]+", artifact_bucket):
        raise ValueError("Choose an explicit new Mica artifacts bucket")
    template = service["spec"]["template"]
    spec = template["spec"]
    if len(spec["containers"]) != 1 or spec.get("volumes") or spec.get("vpcAccess"):
        raise ValueError("Multi-container, volume or network changes require manifest review")
    container = spec["containers"][0]
    if container.get("command") or container.get("args"):
        raise ValueError("Custom container commands require explicit runtime migration support")
    annotations = template.get("metadata", {}).get("annotations", {})
    unsupported = {
        key
        for key in annotations
        if any(word in key for word in ["vpc-access", "network-interfaces", "cloudsql"])
    }
    if unsupported:
        raise ValueError("Network/Cloud SQL annotations require explicit runtime migration support")
    env = effective_environment(container)
    plain: dict[str, str] = {}
    secrets: dict[str, dict[str, str]] = {}
    copies: dict[str, dict[str, str]] = {}
    for key, entry in env.items():
        if "valueFrom" in entry:
            ref = entry["valueFrom"].get("secretKeyRef")
            if not ref or set(entry["valueFrom"]) != {"secretKeyRef"}:
                raise ValueError(f"Unsupported environment reference: {key}")
            source = ref["name"]
            target = source.replace("pistachio-staging-", f"{NAME}-", 1)
            version = str(ref.get("key", "latest"))
            if source != target:
                copies[target] = {"source": source, "source_version": version, "target": target}
                version = "1"  # One operator copy creates the initial version, not the old number.
            secrets[key] = {"secret": target, "version": version}
        else:
            value = str(entry.get("value", ""))
            credential = key.endswith(
                ("_API_KEY", "_BOT_TOKEN", "_SIGNING_SECRET", "_REFRESH_TOKEN")
            )
            if credential and value:
                raise ValueError(f"Plaintext credential must be moved to a secret reference: {key}")
            plain[key] = value
    for key in ["MICA_BATCH_IMAGE", "MICA_SLACK_AGENT_IMAGE"]:
        if not PIN.search(plain.get(key, "")):
            raise ValueError(f"Missing immutable worker pin: {key}")
    if not PIN.search(container["image"]):
        raise ValueError("Source API image must be pinned")
    project = plain["MICA_GCP_PROJECT"]
    for key in ["AGENT_BUCKET", "AGENT_TRANSFER_SERVICE_ACCOUNT"]:
        if plain.get(key):
            raise ValueError(f"Explicit legacy override requires migration review: {key}")
    for identity in ["control", "worker", "writer"]:
        expected = f"pistachio-staging-{identity}@{project}.iam.gserviceaccount.com"
        key = {
            "control": "MICA_CLOUD_TASKS_SERVICE_ACCOUNT",
            "worker": "MICA_BATCH_SERVICE_ACCOUNT",
            "writer": "AGENT_WRITE_SERVICE_ACCOUNT",
        }[identity]
        if plain[key] != expected:
            raise ValueError(f"Unexpected source {identity} identity")
    roles: dict[str, list[str]] = {}
    for identity in ["control", "worker", "writer"]:
        member = f"serviceAccount:pistachio-staging-{identity}@{project}.iam.gserviceaccount.com"
        selected = [
            binding for binding in policy.get("bindings", []) if member in binding["members"]
        ]
        if any(binding.get("condition") for binding in selected):
            raise ValueError("Conditional project grants require explicit migration review")
        roles[identity] = sorted(binding["role"] for binding in selected)
    memory = plain["AGENT_MEMORY_TOKEN_SECRET"].split("/")
    if len(memory) != 6 or memory[:1] != ["projects"] or memory[2] != "secrets":
        raise ValueError("Expected a versioned memory Secret Manager reference")
    copies[f"{NAME}-memory-token"] = {
        "source": memory[3],
        "source_version": memory[5],
        "target": f"{NAME}-memory-token",
    }
    limits = container["resources"]["limits"]
    inputs = {
        "project_id": project,
        "project_number": str(service["metadata"]["namespace"]),
        "region": plain["MICA_GCP_REGION"],
        "name": NAME,
        "stage": "foundation",
        "artifact_bucket": artifact_bucket,
        "source_artifact_bucket": plain["MICA_GCS_BUCKET"],
        "api_image": container["image"],
        "control_plane_url": plain["MICA_CONTROL_PLANE_URL"],
        "runtime_environment": plain,
        "runtime_secrets": secrets,
        "runtime_limits": {
            "cpu": limits["cpu"],
            "memory": limits["memory"],
            "concurrency": spec.get("containerConcurrency", 80),
            "timeout": spec["timeoutSeconds"],
            "min_instances": int(annotations.get("autoscaling.knative.dev/minScale", 0)),
            "max_instances": int(annotations["autoscaling.knative.dev/maxScale"]),
            "cpu_idle": annotations.get("run.googleapis.com/cpu-throttling", "true") != "false",
        },
        "project_roles": roles,
        "write_prefixes": sorted(filter(None, plain["AGENT_WRITE_ALLOWED_PREFIXES"].split(","))),
        "cutover_verified": False,
    }
    evidence = {
        "source_service": service["metadata"]["name"],
        "source_revision": service["status"]["latestReadyRevisionName"],
        "source_spec_sha256": digest(service["spec"]),
        "inputs_sha256": digest(inputs),
        "operator_secret_copies": list(copies.values()),
        "note": "Metadata only. No secret version was read. No cloud mutation performed.",
    }
    return inputs, evidence


def expected_environment(inputs: dict[str, Any]) -> dict[str, str]:
    project, bucket = inputs["project_id"], inputs["artifact_bucket"]

    def account(identity: str) -> str:
        return f"{NAME}-{identity}@{project}.iam.gserviceaccount.com"

    repository = f"{inputs['region']}-docker.pkg.dev/{project}/{NAME}"
    memory_secret = f"projects/{project}/secrets/{NAME}-memory-token/versions/1"
    expected = dict(inputs["runtime_environment"])
    expected.update(
        {
            "MICA_GCS_BUCKET": bucket,
            "MICA_GCS_QUARANTINE_BUCKET": bucket,
            "MICA_BATCH_SERVICE_ACCOUNT": account("worker"),
            "MICA_CLOUD_TASKS_SERVICE_ACCOUNT": account("control"),
            "MICA_CLOUD_TASKS_QUEUE": NAME,
            "MICA_COMPLETION_TOPIC": NAME + "-completion",
            "MICA_GMAIL_PUBSUB_TOPIC": f"projects/{project}/topics/{NAME}-gmail",
            "MICA_CONTROL_PLANE_URL": inputs["control_plane_url"],
            "MICA_OIDC_AUDIENCE": inputs["control_plane_url"],
            "MICA_TOOL_IMAGE_REPOSITORY": repository + "/tools",
            "AGENT_DATA_BUCKETS": ",".join(
                bucket if name == inputs["source_artifact_bucket"] else name
                for name in expected.get("AGENT_DATA_BUCKETS", "").split(",")
            ),
            "AGENT_WRITE_SERVICE_ACCOUNT": account("writer"),
            "AGENT_MEMORY_TOKEN_SECRET": memory_secret,
            "MICA_MIGRATION_SERVICE": NAME,
            "MICA_MIGRATION_DEFAULT_MODE": "frozen",
        }
    )
    return expected


def check_runtime(after: dict[str, Any], inputs: dict[str, Any]) -> None:
    """Check the planned container, not merely the variables passed to Terraform."""
    expected = expected_environment(inputs)
    template = after.get("template", [{}])[0]
    containers = template.get("containers", [])
    if len(containers) != 1 or containers[0].get("image") != inputs["api_image"]:
        raise ValueError("Planned API image/container differs from approved release")
    if template.get("service_account") != expected["MICA_CLOUD_TASKS_SERVICE_ACCOUNT"]:
        raise ValueError("Unexpected planned runtime service account")
    env = containers[0].get("env", [])
    names = [entry["name"] for entry in env]
    if len(names) != len(set(names)) or set(names) != set(expected) | set(
        inputs["runtime_secrets"]
    ):
        raise ValueError("Planned runtime settings were added or dropped")
    for entry in env:
        key = entry["name"]
        if key in expected:
            if entry.get("value") != expected[key] or entry.get("value_source"):
                raise ValueError(f"Planned runtime setting changed or is unknown: {key}")
        else:
            references = entry.get("value_source", [])
            actual = references[0].get("secret_key_ref", []) if len(references) == 1 else []
            if actual != [inputs["runtime_secrets"][key]] or entry.get("value"):
                raise ValueError(f"Planned secret reference changed: {key}")


def check_plan(
    plan: dict[str, Any], inputs: dict[str, Any], allow_updates: set[str] | None = None
) -> list[dict[str, Any]]:
    """Reject deletes/replacements/imports and resources outside this new stack."""
    if plan.get("errored") or not plan.get("applyable", True):
        raise ValueError("Plan is not applicable")
    if plan.get("complete") is False:
        raise ValueError("Incomplete/deferred plan is not accepted")
    variables = {key: entry["value"] for key, entry in plan.get("variables", {}).items()}
    for key, expected in inputs.items():
        if variables.get(key) != expected:
            raise ValueError(f"Saved plan differs from reviewed inputs: {key}")
    if inputs.get("name") != NAME or inputs.get("stage") not in {
        "foundation",
        "preview",
        "cutover",
    }:
        raise ValueError("Wrong migration name/stage")
    updates = allow_updates or set()
    if inputs["stage"] == "cutover" and inputs.get("cutover_verified") is not True:
        raise ValueError("Cutover has not been verified")
    result = []
    for item in plan.get("resource_changes", []):
        if item.get("mode") == "data":
            continue
        address, change = item["address"], item["change"]
        base = address.split("[", 1)[0]
        actions = change["actions"]
        if base not in RESOURCE_TYPES or item.get("previous_address") or change.get("importing"):
            raise ValueError(f"Unexpected resource ownership/import: {address}")
        if "delete" in actions or change.get("replace_paths"):
            raise ValueError(f"Deletion/replacement is forbidden: {address}")
        if item["type"] == "google_cloud_run_v2_service":
            check_runtime(change.get("after") or {}, inputs)
        if actions == ["no-op"]:
            continue
        if actions != ["create"] and not (actions == ["update"] and address in updates):
            raise ValueError(f"Unreviewed existing-resource change: {address}")
        after = change.get("after") or {}
        before = change.get("before") or {}
        for field in ["name", "account_id", "secret_id", "repository_id", "role_id", "member"]:
            if "pistachio" in str(before.get(field, "")):
                raise ValueError(f"Legacy existing resource cannot be changed: {address}")
        if "_iam_" in item["type"] and not item["type"].endswith(
            ("_iam_member", "_iam_custom_role")
        ):
            raise ValueError(f"Authoritative IAM is forbidden: {address}")
        member = after.get("member", "")
        if "pistachio-" in member:
            raise ValueError(f"Legacy identity cannot be modified: {address}")
        if after.get("role") in {"roles/owner", "roles/editor", "roles/viewer"}:
            raise ValueError(f"Primitive IAM role is forbidden: {address}")
        names = [
            after.get(field)
            for field in ["name", "account_id", "secret_id", "repository_id", "role_id"]
        ]
        if any(isinstance(name, str) and "pistachio" in name for name in names):
            raise ValueError(f"Legacy remote resource cannot be changed: {address}")
        if inputs["stage"] != "cutover":
            if item["type"] == "google_pubsub_subscription":
                raise ValueError("Push consumers cannot exist before cutover")
            if item["type"] == "google_cloud_scheduler_job" and after.get("paused") is not True:
                raise ValueError("Preview schedulers must be paused")
        if (
            item["type"] == "google_storage_bucket"
            and after.get("name") != inputs["artifact_bucket"]
        ):
            raise ValueError("Unexpected bucket")
        identities = {f"{NAME}-{identity}" for identity in ["control", "worker", "writer"]}
        required_names = {
            "google_service_account": ("account_id", identities),
            "google_artifact_registry_repository": ("repository_id", {NAME}),
            "google_cloud_tasks_queue": ("name", {NAME}),
            "google_cloud_run_v2_service": ("name", {NAME}),
            "google_pubsub_topic": ("name", {f"{NAME}-gmail", f"{NAME}-completion"}),
            "google_project_iam_custom_role": (
                "role_id",
                {
                    "mica_staging_output_writer",
                    "mica_staging_export_reader",
                },
            ),
        }
        if item["type"] in required_names:
            field, allowed = required_names[item["type"]]
            if after.get(field) not in allowed:
                raise ValueError(f"Unexpected new resource name: {address}")
        if item["type"] == "google_storage_bucket_iam_member":
            allowed_buckets = {inputs["artifact_bucket"]} | {
                uri.split("/")[2] for uri in inputs.get("write_prefixes", [])
            }
            if after.get("bucket") not in allowed_buckets:
                raise ValueError(f"Unexpected IAM bucket: {address}")
        if after.get("force_destroy") is True:
            raise ValueError("force_destroy is forbidden")
        result.append({"address": address, "actions": actions})
    if updates - {item["address"] for item in result}:
        raise ValueError("Update exception did not match an actual reviewed change")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture")
    cap.add_argument("--service", type=Path, required=True)
    cap.add_argument("--project-iam", type=Path, required=True)
    cap.add_argument("--artifact-bucket", required=True)
    cap.add_argument("--output", type=Path, required=True)
    gate = commands.add_parser("check-plan")
    gate.add_argument("--plan-json", type=Path, required=True)
    gate.add_argument("--inputs", type=Path, required=True)
    gate.add_argument("--allow-update", action="append", default=[])
    args = parser.parse_args()
    if args.command == "capture":
        inputs, evidence = capture(
            json.loads(args.service.read_text()),
            json.loads(args.project_iam.read_text()),
            args.artifact_bucket,
        )
        save_private(args.output, inputs)
        save_private(args.output.with_suffix(".evidence.json"), evidence)
        print(
            f"Captured {len(inputs['runtime_environment'])} settings and "
            f"{len(inputs['runtime_secrets'])} secret references; no secret values accessed."
        )
    else:
        result = check_plan(
            json.loads(args.plan_json.read_text()),
            json.loads(args.inputs.read_text()),
            set(args.allow_update),
        )
        print(
            json.dumps(
                {
                    "checked_changes": result,
                    "plan_sha256": hashlib.sha256(args.plan_json.read_bytes()).hexdigest(),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
