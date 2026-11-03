"""Adapt the single-session agent to Mica's existing Cloud Tasks and Spot Batch setup."""

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
from pydantic import ValidationError

from mica.config import Settings as AppSettings
from mica.storage.base import Repository

from . import (
    access,
    amendments,
    compute_approval,
    gpu_capacity,
    memory_tasks,
    observability,
    project_memory,
    surface,
    thread_history,
    write_access,
    write_approval,
    write_broker,
)
from .attachments import AttachmentError
from .config import CHECKPOINT_PREFIX as PREFIX
from .config import Settings
from .data_links import init_args
from .identity import IdentityUnavailable
from .slack_format import chunks, status_blocks
from .storage import GCSStorage, Storage
from .tiers import Provisioning, selected_gpu_choice

log = logging.getLogger(__name__)
STATUS_PHASES = {"received", "waiting", "starting", "replying", "cancelling"}
GPU_BOOT_IMAGE = (
    "projects/batch-custom-image/global/images/batch-debian-12-official-20261016-00-p00"
)


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
    gpu_type: str = "l4",
) -> batch_v1.Job:
    import math

    from . import gpu_runtime
    from .tiers import resolve

    shape = resolve(tier, settings, gpu_type)
    gpu = tier == "gpu"
    if provisioning not in {"SPOT", "STANDARD"}:
        raise ValueError("Invalid worker provisioning mode.")
    if shape.spot_only and provisioning != "SPOT":
        raise ValueError("This GPU configuration requires Spot provisioning.")
    seconds = shape.max_seconds if duration is None else duration
    if not 1 <= seconds <= shape.max_seconds or not 0 <= attempt <= 64:
        raise ValueError("Invalid worker duration or attempt.")
    if budget is not None and (not math.isfinite(budget) or not 0 < budget <= 10000):
        raise ValueError("Invalid reserved model budget.")
    if not app.slack_agent_image or "@sha256:" not in app.slack_agent_image:
        raise ValueError("Set MICA_SLACK_AGENT_IMAGE to a pinned Claude worker image")
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
    if gpu:
        options += gpu_runtime.OPTIONS
    environment = {
        "HOME": "/srv/pistachio",
        "MICA_GCS_BUCKET": app.gcs_bucket,
        "MICA_CONTROL_PLANE_URL": app.control_plane_url,
        "AGENT_DATA_BUCKETS": ",".join(sorted(buckets)),
        "AGENT_ACCESS_MODE": settings.access_mode,
        "AGENT_ACCESS_CACHE_SECONDS": str(settings.access_cache_seconds),
        "AGENT_ACCESS_UNAVAILABLE_GRACE_SECONDS": str(settings.access_unavailable_grace_seconds),
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
        "AGENT_GPU_WORKER": "true" if gpu else "false",
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
            image_uri=settings.gpu_image if gpu else app.slack_agent_image,
            commands=["python", "-m", "mica.agents.analysis.batch_worker", event_id],
            options=options,
            volumes=[f"/mnt/stateful_partition/gcs/{b}:/mnt/gcs/{b}:ro" for b in sorted(buckets)],
        ),
        environment=batch_v1.Environment(variables=environment),
    )
    task = batch_v1.TaskSpec(
        runnables=(
            [
                batch_v1.Runnable(
                    script=batch_v1.Runnable.Script(text=gpu_runtime.SETUP), timeout="120s"
                )
            ]
            if gpu
            else []
        )
        + [runnable],
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
                    f"--cache-dir=/mnt/stateful_partition/mica-cache/{b}",
                    f"--file-cache-max-size-mb={400_000 // len(buckets)}",
                    "--file-cache-cache-file-for-range-read=true",
                ],
            )
            for b in sorted(buckets)
        ],
    )
    policy = batch_v1.AllocationPolicy.InstancePolicy(
        machine_type=shape.machine,
        accelerators=(
            [batch_v1.AllocationPolicy.Accelerator(
                type_=shape.accelerator_type, count=shape.gpu_count
            )]
            if shape.accelerator_type
            else []
        ),
        # Proto-plus enums support name lookup; their stubs omit EnumMeta.__getitem__.
        provisioning_model=(
            batch_v1.AllocationPolicy.ProvisioningModel[provisioning]  # type: ignore[misc]
        ),
        boot_disk=batch_v1.AllocationPolicy.Disk(
            type_=shape.disk_type,
            size_gb=shape.disk_gb,
            # Batch owns GPU/container integration. COS currently injects a privileged
            # launch that conflicts with its CDI hook; use the supported Debian host.
            **({"image": GPU_BOOT_IMAGE} if gpu else {}),
        ),
    )
    return batch_v1.Job(
        task_groups=[batch_v1.TaskGroup(task_count=1, task_spec=task)],
        allocation_policy=batch_v1.AllocationPolicy(
            instances=[
                batch_v1.AllocationPolicy.InstancePolicyOrTemplate(
                    policy=policy, install_gpu_drivers=gpu
                )
            ],
            service_account=batch_v1.ServiceAccount(email=app.batch_service_account),
            network=batch_v1.AllocationPolicy.NetworkPolicy(
                network_interfaces=[
                    batch_v1.AllocationPolicy.NetworkInterface(
                        network=network, subnetwork=subnet, no_external_ip_address=True
                    )
                ]
            ),
        ),
        labels={"mica-agent": "claude", "pistachio-event": event_id},
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

        from .email_delivery import deliver
        from .transport import SlackTransport

        if phase == "failed" and not record.get("cancel_requested"):
            cause = record.get("failure_cause")
            if record.get("stop_reason") == "access_unverifiable":
                cause = "access_unverifiable"
            elif record.get("stop_reason") == "compute_time_allowance":
                cause = "compute_deadline"
            elif record.get("worker_stopped") and not record.get("stop_reason"):
                cause = "compute_failed"
            if cause in observability.CATALOG:
                await observability.report(self, cause, event_id, record)
        if surface.is_email(record):
            terminal = phase in {"complete", "failed", "denied", "cancelled"}
            if terminal:
                record["terminal_reply"] = text
                if phase == "complete" and record.get("reply_handoff") == "atomx":
                    # The transfer controller sends action links and its actual outcome.
                    # Finishing this routing turn is not the completion of the transfer.
                    record.update(notice_phase=phase, notice_text=text)
                    return
                handle = await deliver(self, event_id, record, text, kind="final")
                record.update(notice_ts=handle, notice_phase=phase, notice_text=text)
            return

        if phase in {"complete", "failed", "denied", "cancelled"}:
            record["terminal_reply"] = text
        if record.get("notice_text") == text:
            return
        event = record["payload"]["event"]
        client = SlackTransport(AsyncWebClient(token=self.app.slack_bot_token, timeout=5))
        status: dict[str, Any] = (
            {"blocks": status_blocks(self.settings.loading_emoji, text)}
            if phase in STATUS_PHASES
            else {}
        )
        if phase == "waiting" and text == amendments.WAITING:
            status = {"blocks": amendments.buttons(event_id)}
        elif record.get("amendable") and not status:
            status = {"blocks": []}  # Remove old buttons without bypassing text chunk limits.
        # Use the same bounded messages as guide display for every response, including
        # built-in commands. Save each confirmed part so a retry resumes the delivery.
        displayed = f"🚩 {text}" if phase in {"failed", "cancelled"} else text
        parts = list(chunks(displayed)) or [""]
        delivery_id = digest(f"{phase}:{displayed}")
        progress = record.get("notice_chunks", {})
        sent = int(progress.get("sent", 0)) if progress.get("id") == delivery_id else 0
        try:
            for index, part in enumerate(parts):
                if index < sent:
                    continue
                if index == 0 and record.get("notice_ts"):
                    await client.update(
                        channel=event["channel"], ts=record["notice_ts"], text=part, **status
                    )
                else:
                    message_id = (
                        f"pistachio-receipt:{event_id}"
                        if index == 0
                        else f"mica-reply:{event_id}:{delivery_id}:{index}"
                    )
                    response = await client.post(
                        channel=event["channel"],
                        thread_ts=event.get("thread_ts") or event["ts"],
                        text=part,
                        unfurl_links=False,
                        unfurl_media=False,
                        client_msg_id=str(uuid5(NAMESPACE_URL, message_id)),
                        **status,
                    )
                    if index == 0:
                        record["notice_ts"] = response["ts"]
                if len(parts) > 1:
                    record["notice_chunks"] = {"id": delivery_id, "sent": index + 1}
                    await self.repository.set_connector_state(f"claude-event:{event_id}", record)
            record.update(notice_phase=phase, notice_text=text)
            record.pop("notice_chunks", None)
            if record.get("status") not in {"complete", "failed"}:
                await self.repository.set_connector_state(f"claude-event:{event_id}", record)
        except Exception:
            await observability.report(self, "slack_delivery_failed", event_id, record, unit=phase)
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

    async def accept(
        self,
        payload: dict[str, Any],
        *,
        principal: str | None = None,
        surface_name: str = "slack",
    ) -> str:
        from .transfers import scrub_password_paste

        if surface_name not in {"slack", "email"}:
            raise ValueError("Unknown intake surface")
        payload = await scrub_password_paste(self, payload)
        event = payload["event"]
        thread = event.get("thread_ts") or event["ts"]
        thread_key = digest(f"{payload.get('team_id')}:{event['channel']}:{thread}")
        # message and app_mention can describe the same message with different event IDs.
        event_id = digest(f"{payload.get('team_id')}:{event['channel']}:{event['ts']}")
        if surface_name == "email":
            event_id, thread_key = surface.email_keys(payload, principal or "")
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
                    "surface": surface_name,
                    "payload": payload,
                    "intake_principal": principal,
                    "thread_key": thread_key,
                    "previous": tail.get("tail"),
                    "status": "pending",
                    "tick": 0,
                    "created": time.time(),
                    "write_control": surface_name == "slack"
                    and write_access.is_command(event.get("text", "")),
                }
                if self.settings.amendments and receipt.get("previous"):
                    previous = await self.repository.get_connector_state(
                        f"claude-event:{receipt['previous']}"
                    )
                    if previous and amendments.eligible(self, receipt, previous):
                        receipt["amendable"] = receipt["previous"]
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
                if record["status"] == "failed" and (
                    record.get("stop_reason") or record.get("cancel_requested")
                ):
                    await self.notify_stop(event_id, record)
                await self.cleanup_terminal(event_id, record)
                await self.record_terminal(event_id, record)
                await self.repository.set_connector_state(key, record)
                await memory_tasks.enqueue(self, event_id, record)

                return str(record["status"])
            if record.get("amendable"):
                amendment = await amendments.reconcile(self, event_id, record)
                if amendment != "follow":
                    if amendment == "wait":
                        await self.schedule(event_id, tick + 1)
                        record["tick"] = tick + 1
                    await self.repository.set_connector_state(key, record)
                    return str(record["status"])
            if not record.get("failure_project_id") and not record.get("project_id"):
                grant = await asyncio.to_thread(
                    access.resolve, self.app, self.settings, self.storage, record
                )
                if observability.capture_project(record, grant):
                    await self.repository.set_connector_state(key, record)
            if not record.get("notice_ts"):
                await self.notify(event_id, record, "received", "Mica is working")
            from . import commands, conversation

            if record["status"] == "pending":
                conversation.start(record)
            from .direct import RoutingUnavailable, finish

            previous = (
                await self.repository.get_connector_state(f"claude-event:{record['previous']}")
                if record.get("previous")
                else None
            )
            for _ in range(100):
                if not previous or not previous.get("control_bypass"):
                    break
                previous = (
                    await self.repository.get_connector_state(
                        f"claude-event:{previous['previous']}"
                    )
                    if previous.get("previous")
                    else None
                )
            else:
                raise RuntimeError("Control predecessor chain exceeds the safe limit")
            cmd = commands.Command("cancel") if record.get("cancel_control") else None
            if record["status"] == "pending" and record.get("route") != "compute":
                try:
                    cmd = commands.parse(record["payload"]["event"].get("text", ""))
                    grant = await asyncio.to_thread(
                        access.resolve, self.app, self.settings, self.storage, record
                    )
                    if not cmd and previous and previous["status"] not in {"complete", "failed"}:
                        operation = await conversation.control(self, event_id, record, grant)
                        if operation == "cancel":
                            cmd = commands.Command("cancel")
                        elif operation == "write revoke" and not surface.is_email(record):
                            record["control_bypass"] = True
                            await self.repository.set_connector_state(key, record)
                except RoutingUnavailable:
                    record["routing_failures"] = int(record.get("routing_failures", 0)) + 1
                    if record["routing_failures"] >= 3:
                        record.update(status="failed", failure_cause="routing_exhausted")
                        await self.notify(
                            event_id,
                            record,
                            "failed",
                            "I couldn't determine this request after several attempts. "
                            "Please try again.",
                        )
                    else:
                        await self.notify(
                            event_id,
                            record,
                            "replying",
                            "I'm retrying a temporary routing issue. No need to resend.",
                        )
                    record["tick"] = tick + 1
                    await self.repository.set_connector_state(key, record)
                    if record["status"] == "pending":
                        await self.schedule(event_id, tick + 1)
                    return str(record["status"])
                except ValueError as exc:
                    record["status"] = "failed"
                    await self.notify(event_id, record, "failed", str(exc).splitlines()[0][:250])
                    await self.repository.set_connector_state(key, record)
                    return "failed"
            from .data_links import message_text

            if cmd and cmd.name == "cancel":
                record["cancel_control"] = True
                grant = await asyncio.to_thread(
                    access.resolve,
                    self.app,
                    self.settings,
                    GCSStorage(self.app.gcs_bucket),
                    record,
                )
                observability.capture_project(record, grant)
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
                or record.get("control_bypass")
            ):
                await self.advance(event_id, record)
            else:
                await self.notify(
                    event_id,
                    record,
                    "waiting",
                    amendments.WAITING
                    if self.settings.amendments
                    and record.get("amendable")
                    and not record.get("amendment_choice")
                    and record.get("conversation_amendable") is True
                    else "Got it, I'll start once the earlier request here finishes…",
                )
            if record["status"] not in {"complete", "failed"}:
                await self.schedule(event_id, tick + 1)
                record["tick"] = tick + 1
            record.pop("identity_unavailable_since", None)
            await self.cleanup_terminal(event_id, record)
            await self.record_terminal(event_id, record)
            await self.repository.set_connector_state(key, record)
            await memory_tasks.enqueue(self, event_id, record)

            return str(record["status"])
        except IdentityUnavailable:
            # Preserve routing/submission progress and let Cloud Tasks retry admission.
            if record:
                since = record.setdefault("identity_unavailable_since", time.time())
                if (
                    record["status"] not in {"complete", "failed"}
                    and time.time() - since >= self.settings.access_unavailable_grace_seconds
                ):
                    record.update(status="failed", stop_reason="access_unverifiable")
                    await self.notify_stop(event_id, record)
                await self.repository.set_connector_state(key, record)
            raise
        except GoogleAPICallError as exc:
            await observability.report(
                self, "compute_control_failed", event_id, record or {}, unit=claim.token,
                api_status=int(exc.code) if exc.code is not None else 0,
            )
            raise
        finally:
            await self.repository.release_event(mutex, claim.token)

    async def cleanup_terminal(self, event_id: str, record: dict[str, Any]) -> None:
        if (
            record.get("retention_version") != 1
            or record.get("temporary_cleaned")
            or record["status"] not in {"complete", "failed"}
            or not record.get("worker_stopped")
        ):
            return
        from .checkpoints import cleanup_temporary

        # A failure propagates to the existing Cloud Tasks retry, without repeating computation.
        await asyncio.to_thread(cleanup_temporary, self.storage, event_id)
        record["temporary_cleaned"] = True

    async def record_terminal(self, event_id: str, record: dict[str, Any]) -> None:
        """Fill missing history before a terminal event releases the next turn."""
        from . import recovery
        from .direct import record_exchange

        if record["status"] not in {"complete", "failed"} or record.get("terminal_recorded"):
            return
        if memory_tasks.conversation_turn(record):
            storage = self.storage
            grant = await asyncio.to_thread(
                access.resolve, self.app, self.settings, storage, record
            )
            if (
                grant.allowed
                and grant.has_context
                and record.get("project_id", grant.project_id) == grant.project_id
                and record.get("principal", grant.email) == grant.email
            ):
                state = grant.state
                if record["status"] == "failed" and record.get("recovery_version"):
                    partial = await asyncio.to_thread(
                        recovery.verified_resume, storage, event_id, record["attempt"]
                    )
                    if partial:
                        # Preserve the code/notes handoff too. Large results live in separately
                        # scoped durable stages, never in the ordinary workspace archive.
                        state.update(archive=partial["archive"], retention_version=1)
                        if "script_manifest" in partial:
                            state["script_manifest"] = partial["script_manifest"]
                exchange = next(
                    (m for m in state.get("messages", []) if m.get("id") == event_id), None
                )
                reply = record.get("terminal_reply") or record.get("notice_text", "")
                if exchange is None:
                    record_exchange(
                        state,
                        event_id,
                        record["payload"]["event"].get("text", ""),
                        reply,
                        success=record["status"] == "complete",
                        history=thread_history.History(
                            storage, record["thread_key"], grant.project_id
                        ),
                    )
                    await asyncio.to_thread(
                        storage.put,
                        access.checkpoint_key(record["thread_key"]),
                        json.dumps(state).encode(),
                    )
                if record["status"] == "failed" and record.get("direct_decision"):
                    record["memory_reply"] = reply
                elif record["status"] == "complete" and exchange and exchange.get("success", True):
                    record["memory_reply"] = exchange["assistant"]
        record["terminal_recorded"] = True

    async def advance(self, event_id: str, record: dict[str, Any]) -> None:
        if surface.is_email(record):
            record["write_grant"] = None
            record["overwrite_paths"] = []
            if record.get("overwrite_approval") or record.get("write_paths"):
                record["status"] = "failed"
                await self.notify(event_id, record, "denied", surface.EMAIL_READ_ONLY)
                return
        if record["status"] == "pending" and record.get("route") != "compute":
            from .direct import RoutingUnavailable, handle

            if time.time() < record.get("routing_retry_at", 0):
                return
            try:
                if await handle(self, event_id, record, GCSStorage(self.app.gcs_bucket)):
                    return
            except RoutingUnavailable:
                attempts = int(record.get("routing_failures", 0)) + 1
                record["routing_failures"] = attempts
                log.warning("routing_recovery event=%s attempt=%s", event_id, attempts)
                if attempts < 3:
                    # The original receipt remains pending; normal polling survives restarts,
                    # deduplicates deliveries, and rechecks access before each routing attempt.
                    record["routing_retry_at"] = time.time() + 30 * attempts
                    await self.notify(
                        event_id,
                        record,
                        "replying",
                        "I'm recovering from a temporary issue and will retry automatically. "
                        "No need to resend your message.",
                    )
                else:
                    record.update(status="failed", failure_cause="routing_exhausted")
                    await self.notify(
                        event_id,
                        record,
                        "failed",
                        "I couldn't safely start this request after several automatic recovery "
                        "attempts. Your request and conversation are saved.",
                    )
                return
            except ValidationError:
                record.update(status="failed", failure_cause="conversation_failed")
                log.warning("conversation_validation_failed event=%s", event_id)
                await self.notify(
                    event_id,
                    record,
                    "failed",
                    "I couldn't safely complete this request because of an internal issue. "
                    "Your request and conversation are saved.",
                )
                return
            except (ValueError, httpx.HTTPError, GoogleAPICallError) as exc:
                record["status"] = "failed"
                if not isinstance(exc, ValueError):
                    record["failure_cause"] = "conversation_failed"
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
            if record.get("gpu_approval"):
                try:
                    if not await compute_approval.ready(self, event_id, record, self.storage):
                        return
                except (ValueError, PermissionError) as exc:
                    record["status"] = "failed"
                    await self.notify(event_id, record, "failed", str(exc))
                    return
            elif record.get("overwrite_approval"):
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
            try:
                if not await compute_approval.ready(self, event_id, record, self.storage):
                    return
            except (ValueError, PermissionError) as exc:
                record["status"] = "failed"
                await self.notify(event_id, record, "failed", str(exc))
                return
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
                record.update(status="failed", failure_cause="bucket_unavailable")
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
                    record.setdefault(
                        "amendments_enabled",
                        self.settings.amendments and not surface.is_email(record),
                    )
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
                from .tiers import resolve

                shape = (
                    resolve(record["tier"], self.settings, selected_gpu_choice(record))
                )
                reason = (
                    record.get("launch_reason") or "on-demand selected for this request"
                ).rstrip(".")
                starting = (
                    f"Starting an on-demand worker ({shape.name}: {shape.vcpus} CPUs, "
                    f"{shape.memory_gib} GiB"
                    + (f", NVIDIA {str(shape.gpu_type).upper()}" if shape.gpu_type else "")
                    + f"): {reason}."
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
            from . import reference_repos

            references_key = f"{PREFIX}/events/{event_id}/reference-repos.json"
            try:
                references = json.loads((await asyncio.to_thread(storage.get, references_key))[0])
            except FileNotFoundError:
                references = {"snapshots": [], "unavailable": []}
                references_started = time.monotonic()
                available = grant.project.reference_repos | await reference_repos.shared_repos(
                    self.repository, self.settings
                )
                selected = record.get("reference_repos", sorted(grant.project.reference_repos))
                for repo in selected:
                    try:
                        if repo not in available:
                            raise ValueError("Reference repository is no longer available")
                        async with asyncio.timeout(
                            max(0.01, 60 - (time.monotonic() - references_started))
                        ):
                            snapshot = await reference_repos.fetch(self.settings, repo)
                        references["snapshots"].append(snapshot.model_dump())
                    except Exception:
                        references["unavailable"].append(repo)
                        await observability.record_failure(
                            self.repository,
                            self.app,
                            "reference_repo_failed",
                            occurrence=f"{event_id}:{repo}",
                            event_id=event_id,
                            record=record,
                        )
                await asyncio.to_thread(
                    storage.put, references_key, json.dumps(references).encode()
                )
            manifest = {
                "event_id": event_id,
                "payload": record["payload"],
                "thread_key": record["thread_key"],
                "notice_ts": record.get("notice_ts"),
                "protocol": 2,
                "cooperative_stop": True,
                "retention_version": 1,
                "write_grant": record["write_grant"],
                "overwrite_paths": record.get("overwrite_paths", []),
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
            if record.get("amendments_enabled"):
                manifest["amendments_enabled"] = True
            if references["snapshots"] or references["unavailable"]:
                manifest["reference_repos"] = references["snapshots"]
                manifest["reference_repos_unavailable"] = references["unavailable"]
            if surface.is_email(record):
                manifest["surface"] = "email"
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
            record["cooperative_stop"] = True
            record["retention_version"] = 1
            record.pop("worker_stopped", None)
            # Revalidate after snapshot preparation, immediately before creating a GPU job.
            try:
                if not await compute_approval.ready(self, event_id, record, self.storage):
                    return
            except (ValueError, PermissionError) as exc:
                # A prior submission may have reached Batch before its response was lost.
                # Reconcile/delete it and retain the slot until deletion is observed.
                try:
                    await asyncio.to_thread(client.delete_job, name=name)
                except NotFound:
                    await self.repository.release_event(
                        f"{record.get('slot_pool', 'claude-slot')}:{record['slot']}",
                        record["slot_token"],
                    )
                    record.update(status="failed", worker_stopped=True)
                else:
                    record.update(status="running", started=record.get("started", time.time()))
                record["cancel_requested"] = True
                await self.notify(event_id, record, "failed", str(exc))
                return
            await self.repository.set_connector_state(f"claude-event:{event_id}", record)
            try:
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
                        gpu_type=selected_gpu_choice(record),
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
            except AlreadyExists:
                pass
            except GoogleAPICallError as exc:
                if not gpu_capacity.capacity_error(str(exc)):
                    raise
                try:
                    existing = await asyncio.to_thread(client.get_job, name=name)
                except NotFound:
                    existing = None
                if await gpu_capacity.fallback(
                    self,
                    event_id,
                    record,
                    client,
                    existing,
                    existing.status.state.name if existing else "MISSING",
                    submission_error=str(exc),
                ):
                    return
                raise
            record.update(status="running", started=record.get("submitted", time.time()))
            await self.repository.set_connector_state(f"claude-event:{event_id}", record)
        try:
            job = await asyncio.to_thread(client.get_job, name=name)
            state = job.status.state.name
        except NotFound:
            job = None
            state = "FAILED"
        if await gpu_capacity.fallback(
            self, event_id, record, client, job, state if job else "MISSING"
        ):
            return
        if (
            record.get("cancel_requested")
            and record.get("cooperative_stop")
            and time.time() - record.get("cancel_requested_at", 0) >= 55
            and state not in {"SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"}
        ):
            await asyncio.to_thread(client.delete_job, name=name)
            return
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
            record["worker_stopped"] = True
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
                if state == "SUCCEEDED"
                and not record.get("cancel_requested")
                and record.get("attempt_outcome", "complete") == "complete"
                else "failed"
            )
            if record.get("cancel_requested") or record.get("stop_reason"):
                await self.notify_stop(event_id, record)
            elif record["status"] == "failed":
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

    async def notify_stop(self, event_id: str, record: dict[str, Any]) -> None:
        reason = (
            "cancelled" if record.get("cancel_requested") else str(record.get("stop_reason", ""))
        )
        text = {
            "cancelled": "Cancelled this computation.",
            "access_revoked": "Stopped: your project access was revoked or changed.",
            "access_unverifiable": (
                "Stopped: couldn't confirm your project access with Slack for "
                f"{self.settings.access_unavailable_grace_seconds / 60:g} min."
            ),
            "compute_time_allowance": (
                "This request reached its approved time allowance. Saved stages remain available. "
                "Reply to continue with an additional allowance; "
                "completed saved work can be reused."
            ),
        }.get(reason, "Stopped before finishing.")
        # A generic stop notice is safe; project paths require a verified current grant.
        try:
            grant = await asyncio.to_thread(
                access.resolve, self.app, self.settings, self.storage, record
            )
            if (
                grant.allowed
                and grant.project_id == record.get("project_id")
                and record.get("write_grant")
            ):
                summary = await asyncio.to_thread(
                    write_broker.stopped_summary,
                    self.storage,
                    event_id,
                    record["write_grant"]["uri"],
                )
                if summary:
                    text += "\n" + summary
        except IdentityUnavailable:
            pass
        text += "\nSend another message to retry unfinished work."
        await self.notify(
            event_id, record, "cancelled" if reason == "cancelled" else "failed", text
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
            if prior.get("thread_key") != record["thread_key"] or (
                surface.is_email(record)
                and (
                    not surface.is_email(prior)
                    or prior.get("intake_principal") != record.get("intake_principal")
                )
            ):
                raise PermissionError("Cancellation crossed a conversation boundary")
            next_cursor = prior.get("previous")
            if prior.get("control_bypass"):
                cursor = next_cursor
                continue
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
                                if not prior.get("cooperative_stop"):
                                    with suppress(NotFound):
                                        await asyncio.to_thread(client.delete_job, name=name)
                                prior["cancel_requested"] = True
                                prior["cancel_requested_at"] = time.time()
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
