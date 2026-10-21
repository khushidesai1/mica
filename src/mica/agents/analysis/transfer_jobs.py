"""Trusted transfer job and saved-login operations; no model or analysis-worker access."""

from __future__ import annotations

import math
import re

from google.cloud import batch_v1

from mica.config import Settings as AppSettings

from .config import Settings


def job_spec(
    app: AppSettings,
    settings: Settings,
    run_id: str,
    kind: str,
    *,
    grant_token: str = "",
    source_bytes: int = 0,
) -> batch_v1.Job:
    image = settings.transfer_image or app.slack_agent_image
    account = settings.transfer_service_account or app.batch_service_account
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Configure a digest-pinned transfer image")
    if not account:
        raise ValueError("Configure a worker service account")
    disk = max(50, math.ceil(source_bytes * 1.15 / 1_000_000_000) + 40)
    if disk > settings.transfer_max_disk_gb:
        raise ValueError(
            "This transfer exceeds the configured worker disk limit; split the request."
        )
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
                                image_uri=image,
                                entrypoint="python",
                                commands=[
                                    "-m",
                                    "atomx_transfer.cli",
                                    "job",
                                    "--request",
                                    f"gs://{app.gcs_bucket}/tasks/transfers/{run_id}/{kind}-request.json",
                                ],
                            ),
                            environment=batch_v1.Environment(
                                variables={
                                    "MICA_ATOMX_GRANT": grant_token,
                                    "MICA_ATOMX_CONTROL_URL": app.control_plane_url,
                                }
                            ),
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
                        boot_disk=batch_v1.AllocationPolicy.Disk(type_="pd-balanced", size_gb=disk),
                    )
                )
            ],
            service_account=batch_v1.ServiceAccount(email=account),
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
        labels={"mica-agent": "atomx", "mica-transfer": run_id},
        logs_policy=batch_v1.LogsPolicy(destination=batch_v1.LogsPolicy.Destination.CLOUD_LOGGING),
    )
