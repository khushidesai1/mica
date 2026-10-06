"""Adapt the single-session agent to Pistachio's existing Cloud Tasks and Spot Batch setup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from google.api_core.exceptions import AlreadyExists, GoogleAPICallError, NotFound
from google.cloud import batch_v1

from pistachio.config import Settings as AppSettings
from pistachio.storage.base import Repository

from . import access, conversation_memory, user_memory, write_access
from .attachments import AttachmentError
from .config import CHECKPOINT_PREFIX as PREFIX
from .config import Settings
from .data_links import init_args
from .registry import Registry
from .slack_format import status_blocks
from .storage import GCSStorage

log = logging.getLogger(__name__)
STATUS_PHASES = {"received", "waiting", "starting", "replying", "cancelling"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def job_spec(
    app: AppSettings, settings: Settings, event_id: str, mounts: set[str] | None = None
) -> batch_v1.Job:
    if not app.slack_agent_image or "@sha256:" not in app.slack_agent_image:
        raise ValueError("Set PISTACHIO_SLACK_AGENT_IMAGE to a pinned Claude worker image")
    if not settings.data_buckets and not settings.data_project:
        raise ValueError("Set AGENT_DATA_BUCKETS to the project's allowed data buckets")
    if any(
        not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", b)
        for b in settings.csv("data_buckets")
    ):
        raise ValueError("AGENT_DATA_BUCKETS must contain bucket names, without prefixes")
    buckets = settings.csv("data_buckets") if mounts is None else mounts
    if not buckets <= settings.csv("data_buckets"):
        raise ValueError("Cannot mount an unverified data bucket")
    network = app.batch_network
    subnet = app.batch_subnetwork
    if not network.startswith("projects/"):
        network = f"projects/{app.gcp_project}/global/networks/{network}"
    if not subnet.startswith("projects/"):
        subnet = f"projects/{app.gcp_project}/regions/{app.gcp_region}/subnetworks/{subnet}"
    nested = app.batch_nested_sandbox_opt_in and app.env in {"staging", "test"}
    options = (
        "--security-opt seccomp=unconfined --security-opt apparmor=unconfined" if nested else ""
    )
    if settings.batch_proc_mount_opt_in:
        if not nested:
            raise ValueError(
                "The proc-mount exception requires the existing staging sandbox opt-in"
            )
        options += " --security-opt systempaths=unconfined"
    environment = {
        "HOME": "/srv/pistachio",
        "PISTACHIO_GCS_BUCKET": app.gcs_bucket,
        "PISTACHIO_CONTROL_PLANE_URL": app.control_plane_url,
        "AGENT_DATA_BUCKETS": ",".join(sorted(buckets)),
        "AGENT_ACCESS_MODE": settings.access_mode,
        "AGENT_ACCESS_CACHE_SECONDS": str(settings.access_cache_seconds),
        "AGENT_MODEL": settings.model,
        "AGENT_LOADING_EMOJI": settings.loading_emoji,
        "AGENT_FALLBACK_MODEL": settings.fallback_model,
        "AGENT_MAX_TURNS": str(settings.max_turns),
        "AGENT_MAX_BUDGET_USD_PER_TURN": str(settings.max_budget_usd_per_turn),
    }
    runnable = batch_v1.Runnable(
        container=batch_v1.Runnable.Container(
            image_uri=app.slack_agent_image,
            commands=["python", "-m", "pistachio.agents.analysis.batch_worker", event_id],
            options=options,
            volumes=[f"/mnt/stateful_partition/gcs/{b}:/mnt/gcs/{b}:ro" for b in sorted(buckets)],
        ),
        environment=batch_v1.Environment(variables=environment),
    )
    task = batch_v1.TaskSpec(
        runnables=[runnable],
        max_retry_count=0,
        max_run_duration="3600s",
        compute_resource=batch_v1.ComputeResource(cpu_milli=8000, memory_mib=65536),
        volumes=[
            batch_v1.Volume(
                gcs=batch_v1.GCS(remote_path=b),
                # Batch's Container-Optimized OS keeps /mnt itself read-only.
                mount_path=f"/mnt/stateful_partition/gcs/{b}",
                mount_options=[
                    "-o",
                    "ro,allow_other",
                    "--implicit-dirs",
                    f"--cache-dir=/mnt/stateful_partition/pistachio-cache/{b}",
                    f"--file-cache-max-size-mb={400_000 // len(buckets)}",
                    "--file-cache-cache-file-for-range-read=true",
                ],
            )
            for b in sorted(buckets)
        ],
    )
    policy = batch_v1.AllocationPolicy.InstancePolicy(
        machine_type="n2-highmem-8",
        provisioning_model=batch_v1.AllocationPolicy.ProvisioningModel.SPOT,
        boot_disk=batch_v1.AllocationPolicy.Disk(type_="pd-balanced", size_gb=500),
    )
    return batch_v1.Job(
        task_groups=[batch_v1.TaskGroup(task_count=1, task_spec=task)],
        allocation_policy=batch_v1.AllocationPolicy(
            instances=[batch_v1.AllocationPolicy.InstancePolicyOrTemplate(policy=policy)],
            service_account=batch_v1.ServiceAccount(email=app.batch_service_account),
            network=batch_v1.AllocationPolicy.NetworkPolicy(
                network_interfaces=[
                    batch_v1.AllocationPolicy.NetworkInterface(
                        network=network, subnetwork=subnet, no_external_ip_address=True
                    )
                ]
            ),
        ),
        labels={"pistachio-agent": "claude", "pistachio-event": event_id},
        logs_policy=batch_v1.LogsPolicy(destination=batch_v1.LogsPolicy.Destination.CLOUD_LOGGING),
    )


class BatchBridge:
    def __init__(self, app: AppSettings, repository: Repository) -> None:
        self.app, self.repository = app, repository
        self.settings = Settings()
        self.settings.bucket = app.gcs_bucket

    async def notify(self, event_id: str, record: dict[str, Any], phase: str, text: str) -> None:
        """One durable status message per input, independent of slow Spot startup."""
        from slack_sdk.web.async_client import AsyncWebClient

        if record.get("notice_text") == text:
            return
        event = record["payload"]["event"]
        client = AsyncWebClient(token=self.app.slack_bot_token, timeout=5)
        status: dict[str, Any] = (
            {"blocks": status_blocks(self.settings.loading_emoji, text)}
            if phase in STATUS_PHASES
            else {}
        )
        try:
            if record.get("notice_ts"):
                await client.chat_update(
                    channel=event["channel"], ts=record["notice_ts"], text=text, **status
                )
            else:
                response = await client.chat_postMessage(
                    channel=event["channel"],
                    thread_ts=event.get("thread_ts") or event["ts"],
                    text=text,
                    unfurl_links=False,
                    unfurl_media=False,
                    client_msg_id=str(uuid5(NAMESPACE_URL, f"pistachio-receipt:{event_id}")),
                    **status,
                )
                record["notice_ts"] = response["ts"]
            record.update(notice_phase=phase, notice_text=text)
            await self.repository.set_connector_state(f"claude-event:{event_id}", record)
        except Exception:
            log.warning("Slack status delivery failed for %s", event_id, exc_info=True)

    def resolve_data(
        self, record: dict[str, Any], *, uri: str | None = None
    ) -> tuple[Settings, set[str]]:
        """Verify bucket admission, optionally for a trusted, project-scoped metadata URI."""
        from google.cloud import storage

        settings = self.settings.model_copy()
        if settings.data_project:
            settings.data_buckets = ",".join(
                sorted(b.name for b in storage.Client().list_buckets(project=settings.data_project))
            )
        allowed = settings.csv("data_buckets")
        event = record["payload"]["event"]
        args = init_args(event.get("text", ""))
        if uri is None and args:
            uri = args[1]
        if uri is None:
            store = GCSStorage(self.app.gcs_bucket)
            try:
                saved = json.loads(
                    store.get(f"projects/_threads/{record['thread_key']}/checkpoint.json")[0]
                )
            except FileNotFoundError:
                saved = {}
            path = saved.get("context", {}).get("data_path")
            if path:
                from pathlib import Path

                relative = Path(path).relative_to(settings.data_mount_root)
                uri = f"gs://{relative.parts[0]}/"
            else:
                raise ValueError("This thread has no saved data context.")
        bucket = urlsplit(uri).netloc
        if bucket and bucket not in allowed:
            project_name = settings.data_project or "the configured data buckets"
            raise ValueError(
                f"Bucket `{bucket}` is not available in `{project_name}`. "
                "Check the link and the bucket's Google Cloud project."
            )
        return settings, {bucket} if bucket else set()

    async def schedule(self, event_id: str, tick: int = 0) -> None:
        """Use the existing queue; named polling tasks survive retries without duplicate jobs."""
        from google.auth import default
        from google.auth.transport.requests import AuthorizedSession

        app = self.app
        parent = (
            f"projects/{app.gcp_project}/locations/{app.gcp_region}/queues/{app.cloud_tasks_queue}"
        )
        task: dict[str, Any] = {
            "name": f"{parent}/tasks/claude-{event_id}-{tick}",
            "dispatchDeadline": "180s",
            "httpRequest": {
                "httpMethod": "POST",
                "url": (
                    f"{app.control_plane_url}/api/v1/internal/slack-analysis/"
                    f"process/{event_id}/{tick}"
                ),
                "oidcToken": {
                    "serviceAccountEmail": app.cloud_tasks_service_account,
                    "audience": app.control_plane_url,
                },
            },
        }
        if tick:
            task["scheduleTime"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()

        def enqueue() -> None:
            credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])  # type: ignore[no-untyped-call]
            response = AuthorizedSession(credentials).post(  # type: ignore[no-untyped-call]
                f"https://cloudtasks.googleapis.com/v2/{parent}/tasks",
                json={"task": task},
                timeout=20,
            )
            if response.status_code != 409:
                response.raise_for_status()

        await asyncio.to_thread(enqueue)

    async def accept(self, payload: dict[str, Any], *, principal: str | None = None) -> str:
        event = payload["event"]
        thread = event.get("thread_ts") or event["ts"]
        thread_key = digest(f"{payload.get('team_id')}:{event['channel']}:{thread}")
        # message and app_mention can describe the same message with different event IDs.
        event_id = digest(f"{payload.get('team_id')}:{event['channel']}:{event['ts']}")
        receipt_key = f"claude-event:{event_id}"
        mutex = f"claude-intake:{thread_key}"
        claim = await self.repository.begin_event(mutex, lease_seconds=60)
        if claim.status != "claimed":
            raise RuntimeError("Slack intake busy; retry this event")
        try:
            tail_key = f"claude-thread:{thread_key}"
            tail = await self.repository.get_connector_state(tail_key) or {}
            # Write-ahead receipt repairs a crash between the two document writes.
            if tail and not await self.repository.get_connector_state(
                f"claude-event:{tail['tail']}"
            ):
                await self.repository.set_connector_state(
                    f"claude-event:{tail['tail']}", tail["receipt"]
                )
                await self.schedule(tail["tail"], 0)
            receipt = await self.repository.get_connector_state(receipt_key)
            if not receipt:
                receipt = {
                    "payload": payload,
                    "intake_principal": principal,
                    "thread_key": thread_key,
                    "previous": tail.get("tail"),
                    "status": "pending",
                    "tick": 0,
                    "created": time.time(),
                    "write_control": write_access.is_command(event.get("text", "")),
                }
                # Write controls bypass running work without replacing its queue tail.
                # A subsequent analysis must still wait for that active computation.
                if not receipt["write_control"]:
                    await self.repository.set_connector_state(
                        tail_key, {"tail": event_id, "receipt": receipt}
                    )
                await self.repository.set_connector_state(receipt_key, receipt)
            if receipt["status"] not in {"complete", "failed"}:
                await self.schedule(event_id, receipt["tick"])
            return event_id
        finally:
            await self.repository.release_event(mutex, claim.token)

    async def process(self, event_id: str, tick: int) -> str:
        mutex = f"claude-poll:{event_id}"
        claim = await self.repository.begin_event(mutex, lease_seconds=240)
        if claim.status != "claimed":
            raise RuntimeError("Poll already in progress")
        key = f"claude-event:{event_id}"
        try:
            record = await self.repository.get_connector_state(key)
            if not record:
                raise LookupError("Unknown Slack event")
            if record["status"] in {"complete", "failed"} or tick < record["tick"]:
                return str(record["status"])
            if not record.get("notice_ts"):
                await self.notify(event_id, record, "received", "On it…")
            previous = (
                await self.repository.get_connector_state(f"claude-event:{record['previous']}")
                if record.get("previous")
                else None
            )
            from .data_links import message_text

            if message_text(record["payload"]["event"].get("text", "")).lower() == "cancel":
                grant = await asyncio.to_thread(
                    access.resolve,
                    self.app,
                    self.settings,
                    GCSStorage(self.app.gcs_bucket),
                    record,
                )
                if grant.email and (grant.private or grant.allowed):
                    await self.cancel(event_id, record)
                else:
                    from .direct import finish

                    await finish(self, event_id, record, access.deny_text(grant))
            elif (
                not previous
                or previous["status"] in {"complete", "failed"}
                or message_text(record["payload"]["event"].get("text", "")).lower()
                in {"forget context", "forget all"}
                or record.get("write_control")
            ):
                await self.advance(event_id, record)
            else:
                await self.notify(
                    event_id,
                    record,
                    "waiting",
                    "Got it, I'll start once the earlier request here finishes…",
                )
            if record["status"] not in {"complete", "failed"}:
                await self.schedule(event_id, tick + 1)
                record["tick"] = tick + 1
            await self.repository.set_connector_state(key, record)
            return str(record["status"])
        finally:
            await self.repository.release_event(mutex, claim.token)

    async def advance(self, event_id: str, record: dict[str, Any]) -> None:
        if record["status"] == "pending" and record.get("route") != "compute":
            from .direct import handle

            try:
                if await handle(self, event_id, record, GCSStorage(self.app.gcs_bucket)):
                    return
            except (ValueError, httpx.HTTPError, GoogleAPICallError) as exc:
                record["status"] = "failed"
                # Never fall back to a costly worker because conversation handling failed.
                detail = (
                    str(exc).splitlines()[0][:250]
                    if isinstance(exc, ValueError)
                    else "Conversation service unavailable. Please retry."
                )
                if isinstance(exc, AttachmentError):
                    detail = escape(str(exc), quote=False)[:12_000]
                await self.notify(event_id, record, "failed", f"Couldn’t complete: {detail}")
                return
        client = batch_v1.BatchServiceClient()
        parent = f"projects/{self.app.gcp_project}/locations/{self.app.gcp_region}"
        name = f"{parent}/jobs/claude-{event_id}"
        if record["status"] in {"pending", "submitting"} and not record.get("cancel_requested"):
            grant = await asyncio.to_thread(
                access.resolve,
                self.app,
                self.settings,
                GCSStorage(self.app.gcs_bucket),
                record,
            )
            if (
                not grant.allowed
                or record.get("memory_epoch", 0)
                != await asyncio.to_thread(
                    access.memory_epoch, GCSStorage(self.app.gcs_bucket), grant
                )
                or not await asyncio.to_thread(
                    conversation_memory.is_current,
                    GCSStorage(self.app.gcs_bucket),
                    grant,
                    record["thread_key"],
                    record,
                )
                or not grant.has_context
                or (record.get("decision_access") and record["decision_access"] != grant.signature)
            ):
                await self.notify(
                    event_id,
                    record,
                    "denied",
                    access.deny_text(grant)
                    if not grant.allowed
                    else "Project access changed. Send a new request.",
                )
                if record.get("slot_token"):
                    with suppress(NotFound):
                        await asyncio.to_thread(client.delete_job, name=name)
                    record.update(
                        cancel_requested=True,
                        status="running",
                        started=record.get("started", time.time()),
                    )
                else:
                    record["status"] = "failed"
                    return
            else:
                record.update(
                    principal=grant.email,
                    project_id=grant.project_id,
                    project_name=grant.project.name,
                    private=grant.private,
                    access_signature=grant.signature,
                    protocol=2,
                )
        if record.get("cancel_requested") and record["status"] == "submitting":
            # A submission may have failed before creating the job. Never create it now.
            record.update(status="running", started=record.get("started", time.time()))
        if record["status"] == "pending":
            try:
                resolved, mounts = await asyncio.to_thread(self.resolve_data, record)
            except ValueError as exc:
                record["status"] = "failed"
                await self.notify(event_id, record, "failed", f"Couldn’t start: {exc}")
                return
            except GoogleAPICallError:
                log.exception("Could not verify data bucket for %s", event_id)
                record["status"] = "failed"
                await self.notify(
                    event_id,
                    record,
                    "failed",
                    "Couldn’t verify Google Cloud bucket access. Please retry your request; "
                    "if this persists, the service’s bucket-list permission needs checking.",
                )
                return
            record["data_buckets"] = resolved.data_buckets
            record["mount_buckets"] = sorted(mounts)
            for slot in range(self.settings.max_concurrent_runs):
                claim = await self.repository.begin_event(
                    f"claude-slot:{slot}", lease_seconds=10800
                )
                if claim.status == "claimed":
                    record.update(slot=slot, slot_token=claim.token, status="submitting")
                    # Persist before submission: an HTTP retry uses the same job and slot.
                    await self.repository.set_connector_state(f"claude-event:{event_id}", record)
                    break
            else:
                await self.notify(
                    event_id,
                    record,
                    "waiting",
                    "All workers are busy right now. You're in the queue…",
                )
                return
        if record["status"] == "submitting":
            await self.notify(
                event_id,
                record,
                "starting",
                "Spinning up a compute worker (usually a few minutes)…",
            )
            storage = GCSStorage(self.app.gcs_bucket)
            if "write_grant" not in record:
                writable = await asyncio.to_thread(
                    write_access.active, storage, record["project_id"], self.settings
                )
                record["write_grant"] = writable.model_dump(mode="json") if writable else None
                await self.repository.set_connector_state(f"claude-event:{event_id}", record)
            manifest = {
                "payload": record["payload"],
                "thread_key": record["thread_key"],
                "notice_ts": record.get("notice_ts"),
                "protocol": 2,
                "write_grant": record["write_grant"],
                "write_max_mb": self.settings.write_max_mb,
                "attachments": [f["id"] for f in record.get("attachments", [])],
                "principal": record["principal"],
                "project_id": record["project_id"],
                "project_name": record["project_name"],
                "private": record["private"],
                "access_signature": record["access_signature"],
                "memory_epoch": record.get("memory_epoch", 0),
                "created": record["created"],
                "conversation_epoch": record.get(
                    "conversation_epoch", {"epoch": 0, "owner_epoch": 0}
                ),
                "conversation_memory": (
                    await asyncio.to_thread(
                        conversation_memory.load, storage, grant, record["thread_key"]
                    )
                )[0].summary,
                "user_memory": (
                    user_memory.render(
                        await asyncio.to_thread(user_memory.load, storage, record["principal"]),
                        await asyncio.to_thread(Registry(storage).load),
                        record["principal"],
                        record["payload"]["event"].get("text", ""),
                    )
                    if record["private"]
                    else ""
                ),
                "run_prefix": access.run_root(
                    record["private"],
                    record["principal"],
                    record["project_id"],
                    record["thread_key"],
                    record["payload"]["event"].get("thread_ts") or record["payload"]["event"]["ts"],
                )
                if record["private"]
                else "",
            }
            await asyncio.to_thread(
                storage.put,
                f"{PREFIX}/events/{event_id}/request.json",
                json.dumps(manifest).encode(),
            )
            with suppress(AlreadyExists):
                await asyncio.to_thread(
                    client.create_job,
                    parent=parent,
                    job_id=f"claude-{event_id}",
                    job=job_spec(
                        self.app,
                        self.settings.model_copy(
                            update={
                                "data_buckets": record.get(
                                    "data_buckets", self.settings.data_buckets
                                )
                            }
                        ),
                        event_id,
                        set(record["mount_buckets"]) if "mount_buckets" in record else None,
                    ),
                )
            record.update(status="running", started=time.time())
            await self.repository.set_connector_state(f"claude-event:{event_id}", record)
        try:
            job = await asyncio.to_thread(client.get_job, name=name)
            state = job.status.state.name
        except NotFound:
            state = "FAILED"
        if state in {"QUEUED", "SCHEDULED"}:
            minutes = int((time.time() - record["created"]) // 60)
            if minutes:
                await self.notify(
                    event_id,
                    record,
                    "starting",
                    f"Still waiting on a worker ({minutes} min so far). No need to resend…",
                )
        if (
            state not in {"SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"}
            and time.time() - record["started"] > 7200
        ):
            await asyncio.to_thread(client.delete_job, name=name)
            return  # Keep the slot until a later poll observes termination/deletion.
        if state in {"SUCCEEDED", "FAILED"}:
            record["status"] = (
                "complete"
                if state == "SUCCEEDED" and not record.get("cancel_requested")
                else "failed"
            )
            if record.get("cancel_requested"):
                await self.notify(event_id, record, "cancelled", "Cancelled this computation.")
            elif state != "SUCCEEDED":
                await self.notify(
                    event_id,
                    record,
                    "failed",
                    "The worker stopped before finishing. Your earlier progress is safe, "
                    "so send another message to retry.",
                )
            if record["status"] == "complete":
                storage = GCSStorage(self.app.gcs_bucket)
                grant = await asyncio.to_thread(
                    access.resolve, self.app, self.settings, storage, record
                )
                exchange = next(
                    (m for m in grant.state.get("messages", []) if m.get("id") == event_id), None
                )
                if exchange and exchange.get("success", True):
                    await user_memory.after_turn(
                        self, event_id, record, storage, exchange["assistant"]
                    )
            await self.repository.release_event(
                f"claude-slot:{record['slot']}", record["slot_token"]
            )

    async def cancel(self, event_id: str, record: dict[str, Any]) -> None:
        """Cancel earlier queued/running work without submitting a job for the command."""
        cursor = record.get("previous")
        waiting = False
        for _ in range(100):
            if not cursor:
                break
            key = f"claude-event:{cursor}"
            prior = await self.repository.get_connector_state(key)
            if not prior:
                break
            next_cursor = prior.get("previous")
            if prior["status"] in {"complete", "failed"} and prior.get("cancelled_by") != event_id:
                cursor = None
                break
            if prior["status"] not in {"complete", "failed"}:
                claim = await self.repository.begin_event(
                    f"claude-poll:{cursor}", lease_seconds=240
                )
                if claim.status != "claimed":
                    waiting = True
                else:
                    try:
                        prior = await self.repository.get_connector_state(key) or prior
                        prior["cancelled_by"] = event_id
                        if prior["status"] == "pending":
                            prior["status"] = "failed"
                            await self.notify(
                                cursor, prior, "cancelled", "Cancelled before starting."
                            )
                        elif prior["status"] not in {"complete", "failed"}:
                            client = batch_v1.BatchServiceClient()
                            name = (
                                f"projects/{self.app.gcp_project}/locations/"
                                f"{self.app.gcp_region}/jobs/claude-{cursor}"
                            )
                            if not prior.get("cancel_requested"):
                                with suppress(NotFound):
                                    await asyncio.to_thread(client.delete_job, name=name)
                                prior["cancel_requested"] = True
                            waiting = True  # Normal polling releases the slot after termination.
                        await self.repository.set_connector_state(key, prior)
                    finally:
                        await self.repository.release_event(f"claude-poll:{cursor}", claim.token)
            cursor = next_cursor
        if cursor:
            waiting = True
        await self.notify(
            event_id,
            record,
            "cancelling" if waiting else "complete",
            "Cancellation requested. Waiting for active work to stop."
            if waiting
            else "Cancelled. No new worker was started.",
        )
        if not waiting:
            record["status"] = "complete"
