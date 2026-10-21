"""Durable, confirmation-gated AtoMx control plane. No password enters a task record."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import batch_v1

from mica.tool_catalog import transfer_capability

from . import (
    atomx_connections,
    atomx_delivery,
    job_lifecycle,
    observability,
    project_memory,
    transfer_destinations,
)
from .access import Grant
from .progress import check_in_due, elapsed_label
from .registry import Registry
from .storage import GCSStorage, Storage
from .transfer_config import TransferProject, TransferRequest, age
from .transfer_jobs import job_spec

if TYPE_CHECKING:
    from .batch import BatchBridge
    from .commands import Command

TERMINAL = {"done", "failed", "cancelled", "expired"}
ACTIVE = {"inventory", "submitting", "running", "cancelling"}


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def read(storage: Storage, key: str) -> dict[str, Any] | None:
    try:
        return dict(json.loads(storage.get(key)[0]))
    except FileNotFoundError:
        return None


def markers(storage: Storage, sample: str) -> dict[str, Any]:
    return {
        state: value
        for state in ("in_progress", "done", "failed")
        if (value := read(storage, f"transfer_status/{sample}.{state}.json"))
    }


def marker_state(values: dict[str, Any]) -> str:
    if "in_progress" in values:
        return "stuck" if age(values["in_progress"].get("heartbeat")) > 3600 else "in_progress"
    if "done" in values:
        if values["done"].get("provenance") == "backfill":
            return "done (backfilled; listing-only, not verified)"
        return "done (marker; manifest not rechecked here)"
    return "failed" if "failed" in values else "on_atomx_only"


class Transfers:
    def __init__(self, bridge: BatchBridge, storage: Storage) -> None:
        self.bridge, self.repo, self.store = bridge, bridge.repository, storage
        self.settings, self.app = bridge.settings, bridge.app
        self.connections = atomx_connections.Connections(self.app, self.settings, self.repo)

    def config(self, project: str) -> TransferProject:
        if not self.settings.transfers_enabled:
            raise ValueError("AtoMx transfers are not enabled yet.")
        self.connections.key("credentials")
        if not self.settings.transfer_host_key or "REPLACE" in self.settings.transfer_host_key:
            raise ValueError(
                "AtoMx server identity is not configured. Ask the operator to verify its host key."
            )
        job_spec(self.app, self.settings, "0" * 32, "inventory")
        doc = Registry(self.store).load()
        if project not in doc.projects:
            raise ValueError("Choose a project for this transfer.")
        from urllib.parse import urlsplit

        return TransferProject(bucket=urlsplit(doc.projects[project].data_uri).netloc)

    def authorize(self, project: str, email: str, pending: dict[str, Any] | None = None) -> None:
        from urllib.parse import urlsplit

        from .write_access import scope, validate_scope

        doc = Registry(self.store).load()
        if not doc.can_use(project, email):
            raise ValueError("Project membership is required.")
        self.config(project)
        if pending:
            config = TransferProject.model_validate(pending["config"])
            entry = doc.projects[project]
            if (
                pending.get("data_uri", entry.data_uri) != entry.data_uri
                or pending.get("access_revision", entry.access_revision) != entry.access_revision
            ):
                raise ValueError(
                    "Project access or storage changed; request a new transfer preview."
                )
            if config.bucket != urlsplit(entry.data_uri).netloc:
                raise ValueError("The import bucket no longer matches the project.")
            for prefix in (config.raw_prefix, config.additional_prefix, config.status_prefix):
                uri = f"gs://{config.bucket}/{prefix}"
                validate_scope(uri, self.settings)
                if not doc.owns(project, email) and not uri.startswith(scope(entry.data_uri)):
                    raise ValueError(
                        "An owner must approve imports outside this project's linked prefix."
                    )
            if (
                pending.get("surface") != "email"
                and not pending["channel"].startswith("D")
                and f"{pending['team']}:{pending['channel']}" not in entry.published_channels
            ):
                raise ValueError("This channel is no longer authorized for the project.")

    @asynccontextmanager
    async def locked(self, project: str) -> AsyncIterator[None]:
        key = f"transfer-lock:{project}"
        claim = await self.repo.begin_event(key, lease_seconds=240)
        if claim.status != "claimed":
            raise ValueError("A transfer operation is in progress; please retry.")
        try:
            yield
        finally:
            await self.repo.release_event(key, claim.token)

    async def save(self, p: dict[str, Any]) -> None:
        await self.repo.set_connector_state(f"transfer-pending:{p['id']}", p)

    async def login(self, owner: str) -> dict[str, Any]:
        status = await self.connections.status(owner)
        if not status.get("revision"):
            return {}
        return {
            **status,
            "version": status["revision"],
            "status": "valid" if status.get("status") == "saved" else status.get("status"),
            "saved_by": owner,
            "saved_at": status.get("updated_at"),
            "username": "your personal account",
        }

    async def connection_link(
        self, owner: str, record: dict[str, Any], event_id: str, pending: str = ""
    ) -> str:
        if not self.settings.transfers_enabled:
            return "Secure AtoMx connections are not available in this release."
        url = await self.connections.issue(owner, "connection", event_id, pending)
        await atomx_delivery.deliver(
            self.bridge,
            owner,
            record,
            event_id,
            "Manage your private AtoMx connection (expires in 15 minutes): " + url,
            "connection:" + url.rsplit("#", 1)[-1].split(".")[0],
            private_link=True,
        )
        return (
            "I sent a private link to manage your personal AtoMx connection. "
            "Please use that form; do not send credentials in chat or email."
        )

    async def pending(self, pid: str) -> dict[str, Any]:
        p = await self.repo.get_connector_state(f"transfer-pending:{pid}")
        if not p:
            raise ValueError("Unknown transfer request.")
        return p

    async def schedule(self, p: dict[str, Any], delay: int = 0) -> None:
        # Each state change advances the tick; old scheduled deliveries become harmless.
        await job_lifecycle.enqueue(
            self.app,
            f"atomx-{p['id']}-{p['tick']}",
            f"/api/v1/internal/slack-analysis/transfers/{p['id']}/{p['tick']}",
            delay,
        )

    async def transition(self, p: dict[str, Any], state: str, *, delay: int = 0) -> None:
        p.update(state=state, tick=p.get("tick", -1) + 1)
        await self.save(p)
        if state not in TERMINAL:
            await self.schedule(p, delay)

    async def notify(self, p: dict[str, Any], text: str, buttons: tuple[str, ...] = ()) -> None:
        p["delivery"] = [text, list(buttons)]
        await self.save(p)
        record = await self.repo.get_connector_state(f"claude-event:{p['id']}")
        if not record:
            raise ValueError("The requesting conversation is unavailable.")
        if "login" in buttons or "confirm" in buttons:
            purpose = "connection" if "login" in buttons else "approval"
            url = await self.connections.issue(
                p["requested_by"],
                purpose,
                f"{p['id']}:{p['attempt']}:{p.get('login_version', '')}",
                p["id"],
            )
            await atomx_delivery.deliver(
                self.bridge,
                p["requested_by"],
                record,
                p["id"],
                text + "\n\nOpen your private form (expires in 15 minutes): " + url,
                purpose + ":" + url.rsplit("#", 1)[-1].split(".")[0],
                private_link=True,
            )
        else:
            await atomx_delivery.deliver(
                self.bridge,
                p["requested_by"],
                record,
                p["id"],
                text,
                f"status:{p['state']}:{hashlib.sha256(text.encode()).hexdigest()}",
            )
        # Store only redacted notice metadata; bearer links never enter pending/history records.
        p["notice"] = [text, list(buttons)]
        p.pop("delivery", None)
        await self.save(p)

    async def request(
        self,
        grant: Grant,
        record: dict[str, Any],
        event_id: str,
        request: TransferRequest | None,
    ) -> str:
        from . import surface

        if request is None:
            return await self.connection_link(grant.email, record, event_id)
        self.authorize(grant.project_id, grant.email)
        event = record["payload"]["event"]
        async with self.locked(grant.project_id):
            existing = await self.repo.get_connector_state(f"transfer-pending:{event_id}")
            if existing:
                if existing["state"] not in TERMINAL:
                    await self.schedule(existing)
                return "Your transfer request is recorded in this thread."
            login = await self.login(grant.email)
            snapshot = await project_memory.GitHubMemory(self.settings).read(grant.project_id)
            guide = (
                snapshot.files["PROJECT.md"].text
                if snapshot and "PROJECT.md" in snapshot.files
                else ""
            )
            config = transfer_destinations.resolve(
                request, guide, str(event.get("text", "")), grant.doc, grant.project_id, grant.email
            )
            p = {
                "id": event_id,
                "project": grant.project_id,
                "requested_by": grant.email,
                "slack_user": event["user"],
                "team": record["payload"]["team_id"],
                "channel": event["channel"],
                "thread": event.get("thread_ts") or event["ts"],
                "request": request.model_dump() if request else None,
                "created": time.time(),
                "attempt": 0,
                "expires": time.time() + 1800,
                "config": config.model_dump(),
                "surface": "email" if surface.is_email(record) else "slack",
                "data_uri": grant.project.data_uri,
                "access_revision": grant.project.access_revision,
            }
            self.authorize(grant.project_id, grant.email, p)
            index_key = "atomx-user-runs:" + atomx_connections.digest(grant.email)
            async with self.connections.locked(index_key):
                index = await self.repo.get_connector_state(index_key) or {"ids": []}
                index["ids"] = list(dict.fromkeys(index["ids"] + [event_id]))
                if len(index["ids"]) > 1000:
                    raise ValueError("Your transfer history needs operator maintenance.")
                await self.repo.set_connector_state(index_key, index)
            await self.transition(
                p, "prepare" if request and login.get("status") == "valid" else "login"
            )
        return (
            "Preparing your AtoMx transfer request."
            if request
            else "Opening the saved-login controls."
        )

    async def confirm(self, pid: str, email: str) -> None:
        p = await self.pending(pid)
        async with self.locked(p["project"]):
            p = await self.pending(pid)
            self.authorize(p["project"], email, p)
            if p["requested_by"] != email or p["state"] != "preview":
                raise ValueError("Only the requester can confirm a pending preview, once.")
            if time.time() > p["expires"]:
                raise ValueError("This confirmation expired. Request a new preview.")
            login = await self.login(p["requested_by"])
            if login.get("status") != "valid" or login.get("version") != p["login_version"]:
                raise ValueError("The saved login changed. Request a new preview.")
            await self.available(p)
            p["confirmed_by"] = email
            await self.transition(p, "submitting")

    def destination_markers(self, p: dict[str, Any], sample: str) -> dict[str, Any]:
        store = GCSStorage(p["config"]["bucket"])
        return {
            state: value
            for state in ("in_progress", "done", "failed")
            if (value := read(store, p["config"]["status_prefix"] + sample + "." + state + ".json"))
        }

    async def available(self, p: dict[str, Any]) -> dict[str, Any]:
        sample = p["request"]["as_sample"] or p["request"]["sample"]
        values = await asyncio.to_thread(self.destination_markers, p, sample)
        if "done" in values:
            raise ValueError(
                "This sample already has a done marker. Verify it before any new transfer."
            )
        if "in_progress" in values and age(values["in_progress"].get("heartbeat")) <= 3600:
            raise ValueError("This sample has a live transfer already.")
        return values

    async def reserve(self, p: dict[str, Any]) -> bool:
        key = f"transfer-slots:{p['project']}"
        slots = await self.repo.get_connector_state(key) or {"ids": []}
        if p["id"] in slots["ids"]:
            return True
        # Slots never expire on a timer: a live job cannot lose its slot to a second transfer.
        for other in slots["ids"]:
            pending = await self.pending(other)
            a = p["request"]["as_sample"] or p["request"]["sample"]
            b = pending["request"]["as_sample"] or pending["request"]["sample"]
            if a == b:
                raise ValueError("A transfer for this sample is already active.")
        if len(slots["ids"]) >= p["config"]["max_concurrent"]:
            return False
        slots["ids"].append(p["id"])
        await self.repo.set_connector_state(key, slots)
        return True

    async def release(self, p: dict[str, Any]) -> None:
        key = f"transfer-slots:{p['project']}"
        slots = await self.repo.get_connector_state(key) or {"ids": []}
        slots["ids"] = [i for i in slots["ids"] if i != p["id"]]
        await self.repo.set_connector_state(key, slots)

    def job_name(self, run_id: str) -> str:
        return (
            f"projects/{self.app.gcp_project}/locations/{self.app.gcp_region}/jobs/atomx-{run_id}"
        )

    async def submit(self, p: dict[str, Any], kind: str) -> None:
        if kind == "pull" and p.get("confirmed_by") != p["requested_by"]:
            raise ValueError("A transfer requires the requester's confirmation.")
        run = hashlib.sha256(f"{p['id']}:{p['attempt']}:{kind}".encode()).hexdigest()[:32]
        if p.get("job_run") == run:
            try:
                await asyncio.to_thread(
                    batch_v1.BatchServiceClient().get_job, name=self.job_name(run)
                )
            except NotFound:
                pass
            else:
                await self.transition(
                    p, "inventory" if kind == "inventory" else "running", delay=300
                )
                return
        if kind == "pull":
            await self.available(p)
        login = await self.login(p["requested_by"])
        if login.get("status") != "valid":
            await self.release(p)
            await self.transition(p, "login")
            return
        version = p.get("login_version") if kind == "pull" else login["version"]
        if version != login["version"]:
            raise ValueError("Saved login changed after confirmation; request a new preview.")
        request = {
            **p["request"],
            "run_id": run,
            "project": p["project"],
            "destinations": p["config"],
            "bucket": p["config"]["bucket"],
            "requested_by": p["requested_by"],
            "login_saved_by": login["saved_by"],
            "connection_revision": version,
            "control_url": self.app.control_plane_url,
        }
        if kind == "pull":
            request["export_root"] = p["preview"]["export_root"]
            request["plan_fingerprint"] = p["preview"]["plan_fingerprint"]
        if p.get("job_run") != run:
            p.update(
                job_started=time.time(),
                last_check_in=None,
                max_run_seconds=self.settings.max_run_seconds if kind == "pull" else 3600,
            )
        p.update(
            job_run=run,
            job_kind=kind,
            login_version=version,
            job_terminal=False,
            login_saved_by=login["saved_by"],
        )
        grant_token = self.connections.mac("worker-grant", f"{run}:{p['requested_by']}:{version}")
        await self.repo.set_connector_state(
            "atomx-run:" + run,
            {
                "pending": p["id"],
                "owner": p["requested_by"],
                "revision": version,
                "kind": kind,
                "token_hash": atomx_connections.digest(grant_token),
                "expires": p["job_started"] + p["max_run_seconds"] + 3600,
            },
        )
        await self.save(p)  # Same job name and immutable request survive uncertain submissions.
        key = f"tasks/transfers/{run}/{kind}-request.json"
        previous = await asyncio.to_thread(read, self.store, key)
        if previous is not None and previous != request:
            raise ValueError("Transfer request changed after submission.")
        if previous is None:
            await asyncio.to_thread(self.store.put_if, key, json.dumps(request).encode(), None)
        with suppress(AlreadyExists):
            await asyncio.to_thread(
                batch_v1.BatchServiceClient().create_job,
                parent=self.job_name(run).rsplit("/jobs/", 1)[0],
                job_id="atomx-" + run,
                job=job_spec(
                    self.app,
                    self.settings.model_copy(
                        update={
                            "max_run_seconds": p.get("max_run_seconds", 86400),
                        }
                    ),
                    run,
                    kind,
                    grant_token=grant_token,
                    source_bytes=int(p.get("preview", {}).get("total_bytes", 0)),
                ),
            )
        await self.transition(p, "inventory" if kind == "inventory" else "running", delay=300)
        await self.notify(
            p, "Checking AtoMx…" if kind == "inventory" else "AtoMx transfer started.", ("cancel",)
        )

    async def preview(self, p: dict[str, Any], inventory: dict[str, Any]) -> bool:
        wanted = p["request"]
        target = wanted["as_sample"] or wanted["sample"]
        entries = [
            x
            for x in inventory.get("samples", [])
            if x["sample"] == wanted["sample"]
            and x.get("as_sample", x["sample"]) == target
            and (not wanted["export_root"] or x["export_root"] == wanted["export_root"])
        ]
        if not entries:
            return False
        if len(entries) != 1:
            raise ValueError(
                "Sample appears in several exports; use transfer <sample> from <export_root>."
            )
        item = entries[0]
        TransferRequest(sample=wanted["sample"], export_root=item["export_root"], as_sample=target)
        if not item.get("plan_fingerprint"):
            return False
        marker_values = await self.available(p)
        login = await self.login(p["requested_by"])
        p.update(preview=item, expires=time.time() + 1800, login_version=login["version"])
        await self.transition(p, "preview", delay=300)
        text = (
            f"Transfer *{escape(wanted['sample'])}* as *{escape(target)}* from "
            f"`{escape(item['export_root'])}` / `{escape(item['run_folder'])}`?\n"
            f"{int(item['n_files']):,} files · {int(item['total_bytes']):,} bytes\n"
            f"Raw: gs://{p['config']['bucket']}/{p['config']['raw_prefix']}{target}/\n"
            "Additional: "
            f"gs://{p['config']['bucket']}/{p['config']['additional_prefix']}{target}/\n"
            f"Existing marker status: {marker_state(marker_values)}.\n"
            f"Using `{escape(login['username'])}`, saved by {escape(login['saved_by'])} "
            f"on {escape(str(login['saved_at']))}. "
            "This private confirmation link expires in 15 minutes."
        )
        p["preview_text"] = text
        await self.save(p)
        await self.notify(p, text, ("confirm", "cancel"))
        return True

    async def process(self, pid: str, tick: int) -> str:
        p = await self.pending(pid)
        async with self.locked(p["project"]):
            p = await self.pending(pid)
            if p.get("delivery") and p.get("notice") != p["delivery"]:
                await self.notify(p, p["delivery"][0], tuple(p["delivery"][1]))
            if p["state"] in TERMINAL:
                return str(p["state"])
            if tick != p["tick"]:
                await self.schedule(p, 300)
                return str(p["state"])
            try:
                await self.advance(p)
            except ValueError as exc:
                if p.get("job_run") and p["state"] in ACTIVE and not p.get("job_terminal"):
                    # Keep the slot until Batch confirms termination.
                    await self.cancel_locked(p)
                else:
                    await self.release(p)
                    await self.transition(p, "failed")
                await self.notify(p, escape(str(exc))[:2500])
            except Exception:
                await self.report_failure(p, "atomx_control_failed", unit=str(tick))
                raise
            return str(p["state"])

    async def advance(self, p: dict[str, Any]) -> None:
        if p["state"] in {"inventory", "running", "cancelling"}:
            await self.poll(p)
            return
        self.authorize(p["project"], p["requested_by"], p)
        current = await self.login(p["requested_by"])
        if p.get("login_version") and current.get("status") == "forgotten":
            await self.release(p)
            await self.transition(p, "cancelled")
            await self.notify(
                p, "Transfer cancelled because your personal connection was forgotten."
            )
            return
        if time.time() > p["expires"] and not (
            p["state"] == "submitting" and p.get("job_run") and not p.get("job_terminal")
        ):
            await self.release(p)
            await self.transition(p, "expired")
            await self.notify(p, "Transfer request expired. No transfer was started.")
            return
        if p["state"] == "login":
            login = await self.login(p["requested_by"])
            if p["request"] and login.get("status") == "valid":
                await self.transition(p, "prepare")
                return
            if "login" not in p.get("notice", ["", []])[1]:
                await self.notify(
                    p,
                    "I need an AtoMx export login once. It's saved for future transfers. "
                    "Please use the private link; never paste a password in chat or email.",
                    ("login", "cancel"),
                )
            await self.transition(p, "login", delay=300)
        elif p["state"] == "prepare":
            if await self.reserve(p):
                await self.submit(p, "inventory")
            else:
                await self.notify(p, "Waiting for this project's active transfer job to finish.")
                await self.transition(p, "prepare", delay=300)
        elif p["state"] == "preview":
            current = await self.login(p["requested_by"])
            if current.get("version") != p["login_version"] or current.get("status") != "valid":
                p.update(attempt=p["attempt"] + 1, expires=time.time() + 1800)
                p.pop("job_run", None)
                p.pop("confirmed_by", None)
                await self.transition(p, "prepare" if current.get("status") == "valid" else "login")
            else:
                await self.transition(p, "preview", delay=300)
        elif p["state"] == "submitting":
            if await self.reserve(p):
                await self.submit(p, "pull")
            else:
                await self.notify(p, "Confirmed. Waiting for a transfer slot.")
                await self.transition(p, "submitting", delay=300)

    async def poll(self, p: dict[str, Any]) -> None:
        client = batch_v1.BatchServiceClient()
        try:
            job = await asyncio.to_thread(client.get_job, name=self.job_name(p["job_run"]))
            state = job.status.state.name
        except NotFound:
            state = "MISSING"
        if state not in {"SUCCEEDED", "FAILED", "MISSING"}:
            deadline = p.get("max_run_seconds", 86400 if p["job_kind"] == "pull" else 3600) + 3600
            if time.time() - p["job_started"] > deadline:
                p["cancelled_by"] = "runtime deadline"
                await self.cancel_locked(p)
                return
            try:
                self.authorize(p["project"], p["requested_by"], p)
                current_login = await self.login(p["requested_by"])
                if (
                    current_login.get("version") != p.get("login_version")
                    or current_login.get("status") != "valid"
                ):
                    raise ValueError("Personal connection changed")
            except ValueError:
                await self.cancel_locked(p)
                return
            if p["state"] == "cancelling":
                await self.cancel_locked(p)
                return
            now = time.time()
            elapsed = now - p["job_started"]
            due = check_in_due(
                p["job_started"],
                p.get("last_check_in"),
                now,
                self.settings.long_run_threshold_seconds,
                self.settings.long_run_update_seconds,
            )
            if elapsed < self.settings.long_run_threshold_seconds or due:
                progress = await asyncio.to_thread(
                    read,
                    self.store,
                    f"tasks/transfers/{p['job_run']}/{p['job_kind']}-progress.json",
                )
                text = f"AtoMx job: {state.lower()}."
                if progress and progress.get("run_id") == p["job_run"]:
                    text += (
                        f" Last reported: {int(progress.get('files_done', 0)):,} files, "
                        f"{int(progress.get('bytes_done', 0)):,} bytes transferred."
                    )
                    freshness = age(progress.get("heartbeat"))
                    if freshness != float("inf"):
                        text += f" Last transfer heartbeat {elapsed_label(freshness)} ago."
                    if freshness > 300:
                        text += " Progress telemetry is stale; forward progress is not confirmed."
                else:
                    text += " No transfer progress has been reported yet."
                if due:
                    text += f" Job elapsed: {elapsed_label(elapsed)}."
                await self.notify(p, text, ("cancel",))
                if due:
                    p["last_check_in"] = now
            await self.transition(p, p["state"], delay=300)
            return
        result = await asyncio.to_thread(
            read, self.store, f"tasks/transfers/{p['job_run']}/{p['job_kind']}-result.json"
        ) or {"status": "failed"}
        await self.release(p)
        p["job_terminal"] = True
        try:
            self.authorize(p["project"], p["requested_by"], p)
        except ValueError:
            await self.transition(p, "failed")
            await self.notify(
                p,
                "Project access or transfer configuration changed. "
                "Current project members can check the transfer markers.",
            )
            return
        login = await self.login(p["requested_by"])
        if login.get("version") == p.get("login_version"):
            await self.connections.result(p["requested_by"], p["login_version"], result["status"])
        completed_marker = result.get("marker", {})
        if (
            p["state"] == "cancelling"
            and result["status"] == "done"
            and p["job_kind"] == "pull"
            and completed_marker.get("run_id") == p["job_run"]
        ):
            await self.transition(p, "done")
            await self.notify(
                p, "The transfer finished and verified before cancellation took effect."
            )
        elif p["state"] == "cancelling":
            await self.write_failure(p, "cancelled by " + p.get("cancelled_by", p["requested_by"]))
            await self.transition(p, "cancelled")
            await self.notify(p, "Transfer cancelled. Existing objects were kept.")
        elif result["status"] == "auth_failed":
            await self.write_failure(p, "auth_failed")
            p.update(expires=time.time() + 1800, confirmed_by=None, attempt=p["attempt"] + 1)
            p.pop("job_run", None)
            await self.transition(p, "login")
            await self.notify(
                p,
                "The saved AtoMx login no longer works. Its owner can replace it using "
                "the private link. You'll review and confirm the transfer again.",
                ("login", "cancel"),
            )
        elif state == "SUCCEEDED" and result["status"] == "done":
            if p["job_kind"] == "inventory":
                if not await self.preview(p, result["inventory"]):
                    raise ValueError("Sample was not found in the selected AtoMx export.")
            else:
                marker = result["marker"]
                if marker.get("run_id") != p["job_run"]:
                    raise ValueError("Transfer result does not match this job.")
                await self.transition(p, "done")
                await self.notify(
                    p,
                    f"{escape(marker['sample'])} transfer verified: "
                    f"{int(marker['n_files']):,} files, {int(marker['total_bytes']):,} bytes.",
                )
        else:
            await self.write_failure(p, "job_failed")
            await self.transition(p, "failed")
            await self.notify(p, "AtoMx job failed. Check its status markers before retrying.")

    async def report_failure(
        self, p: dict[str, Any], cause: observability.Cause, *, unit: str = ""
    ) -> None:
        # Only already-verified identity metadata enters the incident record.
        await observability.report(
            self.bridge,
            cause,
            p["id"],
            {
                "project_id": p["project"],
                "principal": p["requested_by"],
                "surface": p.get("surface", ""),
            },
            unit=unit or p.get("job_run", str(p.get("attempt", 0))),
        )

    async def write_failure(self, p: dict[str, Any], error: str) -> None:
        if error in {"auth_failed", "job_failed"}:
            await self.report_failure(
                p,
                "atomx_auth_failed"
                if error == "auth_failed"
                else (
                    "atomx_inventory_failed"
                    if p.get("job_kind") == "inventory"
                    else "atomx_pull_failed"
                ),
            )
        if p.get("job_kind") != "pull":
            return
        # Controller audit remains available even when revoked writer access prevents a
        # worker from updating destination markers. Never mint credentials after revocation.
        await self.repo.set_connector_state(
            f"atomx-failure:{p['job_run']}",
            {
                "run_id": p["job_run"],
                "owner": p["requested_by"],
                "error": error,
                "finished": stamp(),
                "destinations": p["config"],
            },
        )

    async def cancel_locked(self, p: dict[str, Any]) -> None:
        if p.get("job_run") and p["state"] in ACTIVE:
            with suppress(NotFound):
                await asyncio.to_thread(
                    batch_v1.BatchServiceClient().delete_job, name=self.job_name(p["job_run"])
                )
            await self.transition(p, "cancelling", delay=300)
        else:
            await self.release(p)
            await self.transition(p, "cancelled")
        await self.notify(
            p, "Cancellation requested." if p["state"] == "cancelling" else "Cancelled."
        )

    async def cancel(self, pid: str, email: str) -> None:
        p = await self.pending(pid)
        async with self.locked(p["project"]):
            p = await self.pending(pid)
            self.authorize(p["project"], email, p)
            if email != p["requested_by"] and not Registry(self.store).load().owns(
                p["project"], email
            ):
                raise ValueError("Only the requester or project owner can cancel a transfer.")
            if p["state"] in TERMINAL:
                raise ValueError("This transfer request is already finished.")
            p["cancelled_by"] = email
            await self.cancel_locked(p)

    async def context(self, project: str, owner: str = "") -> dict[str, Any]:
        capability = transfer_capability()
        try:
            self.config(project)
        except ValueError as exc:
            return {**capability, "available": False, "reason": str(exc)}
        rows = []
        if owner:
            self.authorize(project, owner)
            index = await self.repo.get_connector_state(
                "atomx-user-runs:" + atomx_connections.digest(owner)
            ) or {"ids": []}
            for pid in reversed(index["ids"][-100:]):
                pending = await self.pending(pid)
                if pending["project"] == project and pending["requested_by"] == owner:
                    rows.append(
                        {
                            "sample": pending["request"]["as_sample"]
                            or pending["request"]["sample"],
                            "status": pending["state"],
                            "pending_id": pid,
                        }
                    )
        return {
            **capability,
            "available": True,
            "reason": (
                "Uses your personal connection and a destination from your request"
                " or project guide. Preview and confirmation required."
            ),
            "connection": await self.connections.status(owner) if owner else {},
            "samples": rows,
            "inventory": None,
        }

    async def command(
        self, cmd: Command, grant: Grant, record: dict[str, Any], event_id: str
    ) -> str:

        if cmd.name in {"connect atomx", "replace atomx login", "forget atomx login"}:
            return await self.connection_link(grant.email, record, event_id)
        if cmd.name == "atomx login status":
            value = await self.connections.status(grant.email)
            state = value.get("status") or "not connected"
            return (
                f"Your personal AtoMx connection: {state}. Ask to connect, replace "
                f"or forget it using a private form."
            )
        self.authorize(grant.project_id, grant.email)
        if cmd.name == "transfer":
            return await self.request(
                grant,
                record,
                event_id,
                TransferRequest(
                    sample=cmd.args[0],
                    export_root=cmd.args[1],
                    as_sample=cmd.args[2],
                ),
            )
        if cmd.name == "cancel transfer":
            slots = await self.repo.get_connector_state(f"transfer-slots:{grant.project_id}") or {
                "ids": []
            }
            personal = await self.repo.get_connector_state(
                "atomx-user-runs:" + atomx_connections.digest(grant.email)
            ) or {"ids": []}
            for pid in reversed(list(dict.fromkeys(slots["ids"] + personal["ids"]))):
                p = await self.pending(pid)
                if (
                    p["project"] == grant.project_id
                    and p["state"] not in TERMINAL
                    and cmd.args[0] == (p["request"]["as_sample"] or p["request"]["sample"])
                ):
                    await self.cancel(pid, grant.email)
                    return "Transfer cancellation requested."
            return "No running transfer found. Use Cancel on the sample's preview if still pending."
        context = await self.context(grant.project_id, grant.email)
        if not context["available"]:
            return str(context["reason"])
        rows = [f"• {escape(r['sample'])}: {r['status']}" for r in context.get("samples", [])]
        inv = context.get("inventory")
        if inv:
            known = {r["sample"] for r in context.get("samples", [])}
            rows.extend(
                f"• {escape(r['sample'])}: in last AtoMx inventory"
                for r in inv["samples"]
                if r["sample"] not in known
            )
            rows.append(
                f"Inventory age: {int(inv['age_seconds']) // 60} min; at most 100 samples shown."
            )
        return "\n".join(rows)[:2900] or "No transfer markers or published AtoMx inventory yet."


async def scrub_password_paste(bridge: BatchBridge, payload: dict[str, Any]) -> dict[str, Any]:
    """Discard recognizable password pastes before receipts, request files, or model context."""
    event = payload["event"]
    if not re.search(r"(?i)\b(?:password|passwd|pwd)\s*[:=]", event.get("text", "")):
        return payload
    return {
        **payload,
        "event": {
            **event,
            "text": "[AtoMx password paste removed]",
            "atomx_password_removed": True,
        },
    }
