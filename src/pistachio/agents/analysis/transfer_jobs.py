"""Trusted transfer job and saved-login operations; no model or analysis-worker access."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from google.auth import default
from google.auth.transport.requests import AuthorizedSession
from google.cloud import batch_v1

from pistachio.config import Settings as AppSettings

from .config import Settings


class LoginSecrets:
    """Write-only to this process. Error messages never include provider response bodies."""

    def call(self, resource: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])  # type: ignore[no-untyped-call]
            with AuthorizedSession(credentials) as session:  # type: ignore[no-untyped-call]
                response = session.post(
                    f"https://secretmanager.googleapis.com/v1/{resource}:{method}",
                    json=body,
                    timeout=10,
                )
                if response.status_code != 200:
                    raise ValueError("Secret operation failed")
                return dict(response.json())
        except Exception:
            raise ValueError(
                "Could not save or disable the AtoMx login. Please retry the form."
            ) from None

    def add(self, secret: str, username: str, password: str) -> str:
        body = base64.b64encode(json.dumps({"username": username, "password": password}).encode())
        result = self.call(secret, "addVersion", {"payload": {"data": body.decode()}})
        version = str(result["name"])
        if not re.fullmatch(re.escape(secret) + r"/versions/[0-9]+", version):
            raise ValueError("Secret Manager returned an invalid version")
        return version

    def disable(self, version: str) -> None:
        self.call(version, "disable", {})


def job_spec(app: AppSettings, settings: Settings, run_id: str, kind: str) -> batch_v1.Job:
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", settings.transfer_image):
        raise ValueError("Configure a digest-pinned transfer image")
    if (
        not settings.transfer_service_account
        or settings.transfer_service_account == app.batch_service_account
    ):
        raise ValueError("Configure a dedicated transfer service account")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or kind not in {"inventory", "pull"}:
        raise ValueError("Invalid transfer job")
    network, subnet = app.batch_network, app.batch_subnetwork
    if not network.startswith("projects/"):
        network = f"projects/{app.gcp_project}/global/networks/{network}"
    if not subnet.startswith("projects/"):
        subnet = f"projects/{app.gcp_project}/regions/{app.gcp_region}/subnetworks/{subnet}"
    return batch_v1.Job(
        task_groups=[
            batch_v1.TaskGroup(
                task_count=1,
                task_spec=batch_v1.TaskSpec(
                    runnables=[
                        batch_v1.Runnable(
                            container=batch_v1.Runnable.Container(
                                image_uri=settings.transfer_image,
                                commands=[
                                    "job",
                                    "--request",
                                    f"gs://{app.gcs_bucket}/tasks/transfers/{run_id}/{kind}-request.json",
                                ],
                            )
                        )
                    ],
                    max_retry_count=0,
                    max_run_duration=f"{settings.max_run_seconds}s" if kind == "pull" else "3600s",
                    compute_resource=batch_v1.ComputeResource(cpu_milli=4000, memory_mib=16384),
                ),
            )
        ],
        allocation_policy=batch_v1.AllocationPolicy(
            instances=[
                batch_v1.AllocationPolicy.InstancePolicyOrTemplate(
                    policy=batch_v1.AllocationPolicy.InstancePolicy(
                        machine_type="n2-standard-4",
                        provisioning_model=batch_v1.AllocationPolicy.ProvisioningModel.STANDARD,
                        boot_disk=batch_v1.AllocationPolicy.Disk(type_="pd-balanced", size_gb=50),
                    )
                )
            ],
            service_account=batch_v1.ServiceAccount(email=settings.transfer_service_account),
            network=batch_v1.AllocationPolicy.NetworkPolicy(
                network_interfaces=[
                    batch_v1.AllocationPolicy.NetworkInterface(
                        network=network,
                        subnetwork=subnet,
                        no_external_ip_address=True,
                    )
                ]
            ),
        ),
        labels={"pistachio-agent": "atomx", "pistachio-transfer": run_id},
        logs_policy=batch_v1.LogsPolicy(destination=batch_v1.LogsPolicy.Destination.CLOUD_LOGGING),
    )
