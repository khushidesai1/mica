"""Adapt the single-session agent to Pistachio's existing Cloud Tasks and Spot Batch setup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import suppress
from html import escape
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from google.api_core.exceptions import AlreadyExists, GoogleAPICallError, NotFound
from google.cloud import batch_v1

from pistachio.config import Settings as AppSettings
from pistachio.storage.base import Repository

from . import access, memory_tasks, project_memory, write_access, write_approval, write_broker
from .attachments import AttachmentError
from .config import CHECKPOINT_PREFIX as PREFIX
from .config import Settings
from .data_links import init_args
from .slack_format import status_blocks
from .storage import GCSStorage, Storage
from .tiers import Provisioning

log = logging.getLogger(__name__)
STATUS_PHASES = {"received", "waiting", "starting", "replying", "cancelling"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def job_spec(
    app: AppSettings,
    settings: Settings,
    event_id: str,
    mounts: set[str] | None = None,
    *,
    tier: str = "standard",
    attempt: int = 0,
    budget: float | None = None,
    duration: int | None = None,
    provisioning: Provisioning = "SPOT",
) -> batch_v1.Job:
    import math

    from .tiers import resolve

    shape = resolve(tier, settings)
    if provisioning not in {"SPOT", "STANDARD"}:
        raise ValueError("Invalid worker provisioning mode.")
    seconds = shape.max_seconds if duration is None else duration
    if not 1 <= seconds <= shape.max_seconds or not 0 <= attempt <= 20:
        raise ValueError("Invalid worker duration or attempt.")
    if budget is not None and (not math.isfinite(budget) or not 0 < budget <= 10000):
        raise ValueError("Invalid reserved model budget.")
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
        "AGENT_MAX_BUDGET_USD_PER_TURN": str(
            settings.max_budget_usd_per_turn if budget is None else budget
        ),
        "AGENT_LONG_RUN_THRESHOLD_SECONDS": str(settings.long_run_threshold_seconds),
        "AGENT_LONG_RUN_UPDATE_SECONDS": str(settings.long_run_update_seconds),
        "AGENT_MAX_RUN_SECONDS": str(seconds),
    }
    environment.update(
        {
            "AGENT_MEMORY_INDEX_CHARS": str(settings.memory_index_chars),
            "AGENT_MEMORY_FILE_KB": str(settings.memory_file_kb),
            "AGENT_MEMORY_MAX_FILES": str(settings.memory_max_files),
            "AGENT_MEMORY_TOTAL_KB": str(settings.memory_total_kb),
        }
    )
    environment.update(
        {
            "AGENT_MAX_TIER": settings.max_tier,
            "AGENT_PARTIAL_CHECKPOINT_SECONDS": str(settings.partial_checkpoint_seconds),
            "AGENT_COMPUTE_CHECKPOINT_MAX_GB": str(settings.compute_checkpoint_max_gb),
        }
    )
    if settings.runtimes_enabled:
        environment.update(
            {
                "AGENT_RUNTIMES_ENABLED": "true",
                "AGENT_RUNTIME_ROOT": str(settings.runtime_root),
                "AGENT_RUNTIME_MIN_AGE_DAYS": str(settings.runtime_min_age_days),
                "AGENT_RUNTIME_MAX_GB": str(settings.runtime_max_gb),
                "AGENT_RUNTIME_MAX_NEW_PER_TURN": str(settings.runtime_max_new_per_turn),
                "AGENT_RUNTIME_TIMEOUT_SECONDS": str(settings.runtime_timeout_seconds),
                "AGENT_RUNTIME_MAX_LOCAL_GB": str(settings.runtime_max_local_gb),
                "AGENT_RUNTIME_DENYLIST": settings.runtime_denylist,
            }
        )
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
        max_run_duration=f"{seconds}s",
        compute_resource=batch_v1.ComputeResource(
            cpu_milli=shape.vcpus * 1000, memory_mib=shape.memory_gib * 1024
        ),
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
        machine_type=shape.machine,
        # Proto-plus enums support name lookup; their stubs omit EnumMeta.__getitem__.
        provisioning_model=(
            batch_v1.AllocationPolicy.ProvisioningModel[provisioning]  # type: ignore[misc]
        ),
        boot_disk=batch_v1.AllocationPolicy.Disk(type_=shape.disk_type, size_gb=shape.disk_gb),
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

    @property
    def storage(self) -> Storage:
        return GCSStorage(self.app.gcs_bucket)

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
        """Named polling tasks survive retries without duplicate jobs."""
        from .job_lifecycle import enqueue

        await enqueue(
            self.app,
            f"claude-{event_id}-{tick}",
            f"/api/v1/internal/slack-analysis/process/{event_id}/{tick}",
            30 if tick else 0,
        )

    async def accept(self, payload: dict[str, Any], *, principal: str | None = None) -> str:
        from .transfers import scrub_password_paste

        payload = await scrub_password_paste(self, payload)
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
                await memory_tasks.enqueue(self, event_id, record)

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
            await memory_tasks.enqueue(self, event_id, record)

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
        name = record.get("job", name)
        if record["status"] == "awaiting_approval":
            if record.get("overwrite_approval"):
                try:
                    if not await write_approval.ready(self, event_id, record, self.storage):
                        return
                except (ValueError, PermissionError) as exc:
                    record["status"] = "failed"
                    await self.notify(event_id, record, "failed", str(exc))
                    return
            else:
                record["status"] = "failed"
                await self.notify(
                    event_id,
                    record,
                    "failed",
                    "This request expired during an update; please send it again.",
                )
                return
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
        if record["status"] == "submitting" and (
            record.get("cancel_requested")
            or (record.get("recovery_version") and time.time() >= record["request_deadline"])
        ):
            # A submission may have failed before creating the job. Reconcile its identity
            # without creating late work after cancellation or the original deadline.
            record.update(status="running", started=record.get("started", time.time()))
        if record["status"] == "pending":
            if "write_grant" not in record:
                writable = await asyncio.to_thread(
                    write_access.active, self.storage, record["project_id"], self.settings
                )
                record["write_grant"] = writable.model_dump(mode="json") if writable else None
            try:
                if not await write_approval.ready(self, event_id, record, self.storage):
                    return
            except (ValueError, PermissionError, GoogleAPICallError) as exc:
                record["status"] = "failed"
                text = (
                    str(exc)
                    if isinstance(exc, (ValueError, PermissionError))
                    else ("Couldn't check existing outputs. Please retry before overwriting.")
                )
                await self.notify(event_id, record, "failed", text)
                return
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
            from .recovery import prepare_attempt

            try:
                pool, slots, seconds = await prepare_attempt(self, event_id, record)
            except ValueError as exc:
                record["status"] = "failed"
                await self.notify(event_id, record, "failed", str(exc))
                return
            name = record["job"]
            for slot in range(slots):
                claim = await self.repository.begin_event(
                    f"{pool}:{slot}", lease_seconds=seconds + 7200
                )
                if claim.status == "claimed":
                    record.update(
                        slot=slot,
                        slot_token=claim.token,
                        status="submitting",
                        max_run_seconds=seconds,
                    )
                    record.update(slot_pool=pool, submitted=time.time())
                    # Persist before submission: an HTTP retry uses the same job and slot.
                    await self.repository.set_connector_state(f"claude-event:{event_id}", record)
                    break
            else:
                await self.notify(
                    event_id,
                    record,
                    "waiting",
                    "Waiting for this worker attempt to finish…"
                    if pool != "claude-slot"
                    else "All workers are busy right now. You're in the queue…",
                )
                return
        if record["status"] == "submitting":
            starting = "Spinning up a compute worker (usually a few minutes)…"
            if record.get("provisioning") == "STANDARD":
                from .tiers import TIERS

                shape = TIERS[record["tier"]]
                reason = (
                    record.get("launch_reason") or "on-demand selected for this request"
                ).rstrip(".")
                starting = (
                    f"Starting an on-demand worker ({shape.name}: {shape.vcpus} CPUs, "
                    f"{shape.memory_gib} GiB): {reason}."
                )
            await self.notify(
                event_id,
                record,
                "starting",
                starting,
            )
            storage = GCSStorage(self.app.gcs_bucket)
            if "write_grant" not in record:
                writable = await asyncio.to_thread(
                    write_access.active, storage, record["project_id"], self.settings
                )
                record["write_grant"] = writable.model_dump(mode="json") if writable else None
                await self.repository.set_connector_state(f"claude-event:{event_id}", record)
            if "return_scripts" not in record:
                from . import user_settings

                record["return_scripts"] = await asyncio.to_thread(
                    user_settings.effective,
                    storage,
                    record["principal"],
                    self.settings.return_scripts_default,
                )
                await self.repository.set_connector_state(f"claude-event:{event_id}", record)
            memory_key = f"{PREFIX}/events/{event_id}/memory-snapshot.json"
            try:
                memory = project_memory.Snapshot.model_validate_json(
                    (await asyncio.to_thread(storage.get, memory_key))[0]
                )
            except FileNotFoundError:
                loaded_memory = await project_memory.GitHubMemory(self.settings).read(
                    grant.project_id, event_id=event_id
                )
                memory = loaded_memory or project_memory.Snapshot(head_oid="")
                await asyncio.to_thread(storage.put, memory_key, memory.model_dump_json().encode())
            manifest = {
                "event_id": event_id,
                "payload": record["payload"],
                "thread_key": record["thread_key"],
                "notice_ts": record.get("notice_ts"),
                "protocol": 2,
                "write_grant": record["write_grant"],
                "overwrite_paths": record.get("overwrite_paths", []),
                "write_max_mb": self.settings.write_max_mb,
                "write_max_files": self.settings.write_max_files,
                "write_max_total_gb": self.settings.write_max_total_gb,
                "write_upload_seconds": self.settings.write_upload_seconds,
                "attachments": [f["id"] for f in record.get("attachments", [])],
                "principal": record["principal"],
                "project_id": record["project_id"],
                "project_name": record["project_name"],
                "private": record["private"],
                "access_signature": record["access_signature"],
                "created": record["created"],
                "memory": memory.model_dump(include={"head_oid", "files"}),
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
            if record["return_scripts"]:
                manifest["return_scripts"] = True
            if record.get("recovery_version"):
                from .recovery import worker_manifest

                manifest.update(worker_manifest(record, self.settings))
            await asyncio.to_thread(
                storage.put,
                f"{PREFIX}/events/{event_id}/request.json",
                json.dumps(manifest).encode(),
            )
            with suppress(AlreadyExists):
                await asyncio.to_thread(
                    client.create_job,
                    parent=parent,
                    job_id=name.rsplit("/", 1)[-1],
                    job=job_spec(
                        self.app,
                        self.settings.model_copy(
                            update={
                                "data_buckets": record.get(
                                    "data_buckets", self.settings.data_buckets
                                ),
                                "max_run_seconds": record.get("max_run_seconds", 3600),
                            }
                        ),
                        event_id,
                        set(record["mount_buckets"]) if "mount_buckets" in record else None,
                        **(
                            {
                                "tier": record["tier"],
                                "attempt": record["attempt"],
                                "budget": record["model_reserved"],
                                "duration": record["max_run_seconds"],
                                "provisioning": record.get("provisioning", "SPOT"),
                            }
                            if record.get("recovery_version")
                            else {}
                        ),
                    ),
                )
            record.update(status="running", started=record.get("submitted", time.time()))
            await self.repository.set_connector_state(f"claude-event:{event_id}", record)
        try:
            job = await asyncio.to_thread(client.get_job, name=name)
            state = job.status.state.name
        except NotFound:
            state = "FAILED"
        if record.get("write_grant"):
            await asyncio.to_thread(
                write_broker.archive_manifests,
                GCSStorage(self.app.gcs_bucket),
                event_id,
                record["write_grant"]["uri"],
            )
        if (
            record.get("recovery_version")
            and time.time() >= record["request_deadline"]
            and state not in {"SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"}
        ):
            record["compute_failure"] = "This request reached its original time allowance."
            await asyncio.to_thread(client.delete_job, name=name)
            return  # Retain the slot until Batch confirms that this attempt stopped.
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
            and time.time() - record["started"] > record.get("max_run_seconds", 3600) + 3600
        ):
            await asyncio.to_thread(client.delete_job, name=name)
            return  # Keep the slot until a later poll observes termination/deletion.
        if state in {"SUCCEEDED", "FAILED"}:
            if record.get("recovery_version"):
                from .recovery import terminal

                if await terminal(self, event_id, record, client, state):
                    return
            if record.get("compute_version") and not record.get("recovery_version"):
                from .recovery import read_json

                result = await asyncio.to_thread(
                    read_json,
                    GCSStorage(self.app.gcs_bucket),
                    f"{PREFIX}/events/{event_id}/attempt-a{record.get('attempt', 0)}.json",
                )
                if result and result.get("outcome") != "complete":
                    state = "FAILED"
                    record["compute_failure"] = (
                        "This request expired during an update; please send it again."
                    )
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
                    record.get("compute_failure")
                    or "The worker stopped before finishing. Your earlier progress is safe, "
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
                    record["memory_reply"] = exchange["assistant"]
            await self.repository.release_event(
                f"{record.get('slot_pool', 'claude-slot')}:{record['slot']}", record["slot_token"]
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
                        if prior["status"] in {"pending", "awaiting_approval"}:
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
                            name = prior.get("job", name)
                            if not prior.get("cancel_requested"):
                                await asyncio.to_thread(
                                    GCSStorage(self.app.gcs_bucket).put,
                                    f"{PREFIX}/events/{cursor}/memory-cancelled.json",
                                    b"true",
                                )
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
