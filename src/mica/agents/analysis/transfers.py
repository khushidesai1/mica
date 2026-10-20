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
from uuid import NAMESPACE_URL, uuid5

from google.api_core.exceptions import AlreadyExists, GoogleAPICallError, NotFound
from google.cloud import batch_v1
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from mica.tool_catalog import transfer_capability

from . import job_lifecycle
from .access import Grant
from .progress import TaskProgress, check_in_due, elapsed_label
from .registry import Registry
from .slack_format import activity_blocks, chunks
from .storage import GCSStorage, Storage
from .transfer_config import TransferProject, TransferRequest, age
from .transfer_jobs import LoginSecrets, job_spec

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
        self.secrets = LoginSecrets()

    def config(self, project: str) -> TransferProject:
        if not self.settings.transfers_enabled or project not in self.settings.transfer_projects:
            raise ValueError("AtoMx transfers are not enabled for this project.")
        job_spec(self.app, self.settings, "0" * 32, "inventory")
        return self.settings.transfer_projects[project]

    def authorize(self, project: str, email: str, pending: dict[str, Any] | None = None) -> None:
        doc = Registry(self.store).load()
        if not doc.can_use(project, email):
            raise ValueError("Project membership is required.")
        config = self.config(project)
        # Prevent a renamed/rebound project from silently writing to its former bucket.
        if not doc.projects[project].data_uri.startswith(f"gs://{config.bucket}/"):
            raise ValueError("The project's data bucket does not match its transfer configuration.")
        if (
            pending
            and not pending["channel"].startswith("D")
            and f"{pending['team']}:{pending['channel']}"
            not in doc.projects[project].published_channels
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

    async def login(self, project: str) -> dict[str, Any]:
        return await self.repo.get_connector_state(f"atomx-login:{project}") or {}

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
        actions = {
            "login": ("Enter AtoMx login", "mica_transfer_login"),
            "confirm": ("Confirm", "mica_transfer_confirm"),
            "cancel": ("Cancel", "mica_transfer_cancel"),
        }
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": part}}
            for part in chunks(text, limit=2900)
        ]
        if buttons:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": actions[b][0]},
                            "action_id": actions[b][1],
                            "value": p["id"],
                        }
                        for b in buttons
                    ],
                }
            )
        fallback = blocks
        native = (
            p["state"] in {"running", "inventory"}
            and buttons == ("cancel",)
            and p.get("native_progress", True)
        )
        if native:
            task = TaskProgress(
                p["job_run"],
                "Transfer " + (p["request"]["as_sample"] or p["request"]["sample"])
                if p["state"] == "running"
                else "Inspect AtoMx export",
                "AtoMx transfer" if p["state"] == "running" else "Checking AtoMx export",
            )
            blocks = activity_blocks([task], check_in=text) + blocks[-1:]
        if p.get("notice") == [text, list(buttons)]:
            return
        p["delivery"] = [text, list(buttons)]
        await self.save(p)
        if "login" in buttons:
            await self.repo.set_connector_state(
                f"atomx-login-prompt:{p['team']}:{p['channel']}:{p['thread']}",
                {"expires": p["expires"]},
            )
        client = AsyncWebClient(token=self.app.slack_bot_token, timeout=5)

        async def deliver(selected: list[dict[str, Any]]) -> Any:
            if p.get("notice_ts"):
                return await client.chat_update(
                    channel=p["channel"], ts=p["notice_ts"], text=text, blocks=selected
                )
            return await client.chat_postMessage(
                channel=p["channel"],
                thread_ts=p["thread"],
                text=text,
                blocks=selected,
                client_msg_id=str(uuid5(NAMESPACE_URL, "atomx:" + p["id"])),
            )

        try:
            response = await deliver(blocks)
        except SlackApiError as exc:
            if not native or exc.response.get("error") != "invalid_blocks":
                raise
            p["native_progress"] = False
            response = await deliver(fallback)
        if not p.get("notice_ts"):
            p["notice_ts"] = response["ts"]
        p["notice"] = [text, list(buttons)]
        await self.save(p)

    async def request(
        self,
        grant: Grant,
        record: dict[str, Any],
        event_id: str,
        request: TransferRequest | None,
    ) -> str:
        from . import surface

        if surface.is_email(record):
            raise PermissionError(surface.EMAIL_READ_ONLY)
        self.authorize(grant.project_id, grant.email)
        event = record["payload"]["event"]
        async with self.locked(grant.project_id):
            existing = await self.repo.get_connector_state(f"transfer-pending:{event_id}")
            if existing:
                if existing["state"] not in TERMINAL:
                    await self.schedule(existing)
                return "Your transfer request is recorded in this thread."
            login = await self.login(grant.project_id)
            if request is None and login:
                self.can_manage(grant.project_id, grant.email, login)
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
                "config": self.config(grant.project_id).model_dump(),
            }
            await self.transition(
                p, "prepare" if request and login.get("status") == "valid" else "login"
            )
        return (
            "Preparing your AtoMx transfer request."
            if request
            else "Opening the saved-login controls."
        )

    def can_manage(self, project: str, email: str, login: dict[str, Any]) -> None:
        if email != login.get("saved_by") and not Registry(self.store).load().owns(project, email):
            raise ValueError(
                "Only the project owner or the person who saved the login can change it."
            )

    async def forget(self, project: str, email: str) -> str:
        self.authorize(project, email)
        async with self.locked(project):
            login = await self.login(project)
            self.can_manage(project, email, login)
            if login.get("version"):
                await asyncio.to_thread(self.secrets.disable, login["version"])
            if login.get("previous_version"):
                await asyncio.to_thread(self.secrets.disable, login["previous_version"])
            login["status"] = "disabled"
            await self.repo.set_connector_state(f"atomx-login:{project}", login)
        return "Saved AtoMx login disabled. Running jobs may already have read it."

    async def save_login(
        self, pid: str, email: str, username: str, password: str, submission: str
    ) -> None:
        p = await self.pending(pid)
        async with self.locked(p["project"]):
            p = await self.pending(pid)
            self.authorize(p["project"], email, p)
            login = await self.login(p["project"])
            if login:
                self.can_manage(p["project"], email, login)
            elif email != p["requested_by"]:
                raise ValueError("Only the requester can save the first login for this request.")
            if login.get("submission") == submission:
                if login.get("previous_version"):
                    await asyncio.to_thread(self.secrets.disable, login["previous_version"])
                    login.pop("previous_version")
                    login["status"] = "valid"
                    await self.repo.set_connector_state(f"atomx-login:{p['project']}", login)
                if p["state"] == "login":
                    p.update(expires=time.time() + 1800, attempt=p["attempt"] + 1)
                    await self.transition(p, "prepare" if p["request"] else "done")
                await self.schedule(p)
                return
            if p["state"] != "login" or time.time() > p["expires"]:
                raise ValueError(
                    "This login form expired. Start a new transfer or replace atomx login."
                )
            if not username.strip() or not password or len(username) > 320 or len(password) > 4096:
                raise ValueError("Enter an AtoMx username and password in the form.")
            if login.get("previous_version"):
                await asyncio.to_thread(self.secrets.disable, login["previous_version"])
            version = await asyncio.to_thread(
                self.secrets.add, self.config(p["project"]).login_secret, username.strip(), password
            )
            # Persist the new version first; retries can finish disabling the old one.
            replacement = {
                "username": username.strip(),
                "saved_by": email,
                "saved_at": stamp(),
                "version": version,
                "status": "invalid" if login.get("version") else "valid",
                "submission": submission,
                "previous_version": login.get("version"),
            }
            await self.repo.set_connector_state(f"atomx-login:{p['project']}", replacement)
            if login.get("version"):
                await asyncio.to_thread(self.secrets.disable, login["version"])
                replacement.pop("previous_version", None)
                replacement["status"] = "valid"
                await self.repo.set_connector_state(f"atomx-login:{p['project']}", replacement)
            p.update(expires=time.time() + 1800, attempt=p["attempt"] + 1)
            await self.transition(p, "prepare" if p["request"] else "done")
            await self.notify(
                p,
                f"AtoMx login `{escape(username.strip())}` saved by {escape(email)}. "
                "Future transfers will use it.",
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
            login = await self.login(p["project"])
            if login.get("status") != "valid" or login.get("version") != p["login_version"]:
                raise ValueError("The saved login changed. Request a new preview.")
            if p["config"] != self.config(p["project"]).model_dump():
                raise ValueError("Transfer configuration changed. Request a new preview.")
            await self.available(p)
            p["confirmed_by"] = email
            await self.transition(p, "submitting")

    async def available(self, p: dict[str, Any]) -> dict[str, Any]:
        sample = p["request"]["as_sample"] or p["request"]["sample"]
        values = await asyncio.to_thread(markers, GCSStorage(p["config"]["bucket"]), sample)
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
        login = await self.login(p["project"])
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
            "layout": p["config"]["layout"],
            "bucket": p["config"]["bucket"],
            "requested_by": p["requested_by"],
            "login_saved_by": login["saved_by"],
            "login_secret_version": version,
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
                ),
            )
        login.update(last_used=stamp(), last_result="running")
        await self.repo.set_connector_state(f"atomx-login:{p['project']}", login)
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
        login = await self.login(p["project"])
        p.update(preview=item, expires=time.time() + 1800, login_version=login["version"])
        await self.transition(p, "preview", delay=300)
        text = (
            f"Transfer *{escape(wanted['sample'])}* as *{escape(target)}* from "
            f"`{escape(item['export_root'])}` / `{escape(item['run_folder'])}`?\n"
            f"{int(item['n_files']):,} files · {int(item['total_bytes']):,} bytes\n"
            f"To `gs://{p['config']['bucket']}/raw_outputs/raw/{target}/` and "
            f"`raw_outputs/additional_files/{target}/`.\n"
            f"Existing marker status: {marker_state(marker_values)}.\n"
            f"Using `{escape(login['username'])}`, saved by {escape(login['saved_by'])} "
            f"on {escape(login['saved_at'])}. Confirmation expires in 30 minutes."
        )
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
            return str(p["state"])

    async def advance(self, p: dict[str, Any]) -> None:
        if p["state"] in {"inventory", "running", "cancelling"}:
            await self.poll(p)
            return
        self.authorize(p["project"], p["requested_by"], p)
        if p["config"] != self.config(p["project"]).model_dump():
            raise ValueError("Transfer configuration changed. Start a new request.")
        if time.time() > p["expires"] and not (
            p["state"] == "submitting" and p.get("job_run") and not p.get("job_terminal")
        ):
            await self.release(p)
            await self.transition(p, "expired")
            await self.notify(p, "Transfer request expired. No transfer was started.")
            return
        if p["state"] == "login":
            login = await self.login(p["project"])
            if p["request"] and login.get("status") == "valid":
                await self.transition(p, "prepare")
                return
            if "login" not in p.get("notice", ["", []])[1]:
                await self.notify(
                    p,
                    "I need an AtoMx export login once. It's saved for future transfers. "
                    "Please use the button; never paste a password in chat.",
                    ("login", "cancel"),
                )
            await self.transition(p, "login", delay=300)
        elif p["state"] == "prepare":
            cached = await asyncio.to_thread(
                read, GCSStorage(p["config"]["bucket"]), "transfer_status/_inventory.json"
            )
            if (
                cached
                and 0 <= age(cached.get("generated_at")) < 3600
                and await self.preview(p, cached)
            ):
                return
            if await self.reserve(p):
                await self.submit(p, "inventory")
            else:
                await self.notify(p, "Waiting for this project's active transfer job to finish.")
                await self.transition(p, "prepare", delay=300)
        elif p["state"] == "preview":
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
        login = await self.login(p["project"])
        if login.get("version") == p.get("login_version"):
            login.update(last_result=result["status"])
            if result["status"] == "auth_failed":
                login["status"] = "invalid"
            await self.repo.set_connector_state(f"atomx-login:{p['project']}", login)
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
                "the button. You'll review and confirm the transfer again.",
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

    async def write_failure(self, p: dict[str, Any], error: str) -> None:
        if p.get("job_kind") != "pull":
            return
        bucket = GCSStorage(p["config"]["bucket"])
        target = p["request"]["as_sample"] or p["request"]["sample"]
        key = f"transfer_status/{target}.failed.json"
        existing = await asyncio.to_thread(markers, bucket, target)
        if existing.get("done", {}).get("run_id") == p["job_run"]:
            return  # Completion won a cancellation race.
        if existing.get("in_progress", {}).get("run_id") not in {None, p["job_run"]}:
            return  # A later run owns the marker; never replace its status.
        body = {
            **existing.get("in_progress", {}),
            "version": 1,
            "sample": target,
            "remote_sample": p["request"]["sample"],
            "export_root": p.get("preview", {}).get("export_root", p["request"]["export_root"]),
            "run_folder": p.get("preview", {}).get("run_folder", ""),
            "started": datetime.fromtimestamp(p["job_started"], UTC).isoformat(),
            "heartbeat": existing.get("in_progress", {}).get("heartbeat", stamp()),
            "tool_version": self.settings.transfer_image,
            "login_saved_by": p["login_saved_by"],
            "requested_by": p["requested_by"],
            "run_id": p["job_run"],
            "finished": stamp(),
            "step": "job",
            "error": error,
        }
        try:
            _, generation = await asyncio.to_thread(bucket.get, key)
        except FileNotFoundError:
            generation = None
        await asyncio.to_thread(bucket.put_if, key, json.dumps(body).encode(), generation)

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

    async def context(self, project: str) -> dict[str, Any]:
        try:
            return await self.status_context(project)
        except (GoogleAPICallError, ValueError, KeyError, TypeError):
            return {
                **transfer_capability(),
                "available": False,
                "reason": "Transfer status or configuration is unavailable. Please retry later.",
            }

    async def status_context(self, project: str) -> dict[str, Any]:
        capability = transfer_capability()
        if not self.settings.transfers_enabled or project not in self.settings.transfer_projects:
            return capability
        config = self.config(project)
        bucket = GCSStorage(config.bucket)
        keys = await asyncio.to_thread(bucket.keys, "transfer_status/")
        samples = sorted(
            {
                k.rsplit("/", 1)[-1].removesuffix("." + suffix + ".json")
                for k in keys
                for suffix in ("in_progress", "done", "failed")
                if k.endswith("." + suffix + ".json")
            }
        )[:100]
        rows = []
        for sample in samples:
            values = await asyncio.to_thread(markers, bucket, sample)
            current = values.get("in_progress", values.get("done", values.get("failed", {})))
            rows.append(
                {
                    "sample": sample,
                    "status": marker_state(values),
                    "age_seconds": age(current.get("finished") or current.get("heartbeat")),
                }
            )
        inventory = await asyncio.to_thread(read, bucket, "transfer_status/_inventory.json")
        login = await self.login(project)
        return {
            **capability,
            "available": True,
            "reason": "Confirmation required for every transfer.",
            "samples": rows,
            "coverage": "at most 100 marker samples",
            "inventory": {
                "age_seconds": age(inventory.get("generated_at")),
                "samples": inventory.get("samples", [])[:100],
            }
            if inventory
            else None,
            "login": {
                k: login.get(k)
                for k in ("username", "saved_by", "saved_at", "status", "last_used", "last_result")
            },
        }

    async def command(
        self, cmd: Command, grant: Grant, record: dict[str, Any], event_id: str
    ) -> str:
        from . import surface

        if surface.is_email(record):
            raise PermissionError(surface.EMAIL_READ_ONLY)
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
        if cmd.name == "replace atomx login":
            return await self.request(grant, record, event_id, None)
        if cmd.name == "forget atomx login":
            return await self.forget(grant.project_id, grant.email)
        if cmd.name == "atomx login status":
            login = await self.login(grant.project_id)
            if not login:
                return "No AtoMx login saved. Request a transfer to enter it through the form."
            return (
                f"AtoMx login `{escape(login['username'])}` · {login['status']}\n"
                f"Saved by {escape(login['saved_by'])} on {login['saved_at']}. "
                f"Last used: {login.get('last_used', 'never')}; "
                f"result: {login.get('last_result', 'none')}."
            )
        if cmd.name == "cancel transfer":
            slots = await self.repo.get_connector_state(f"transfer-slots:{grant.project_id}") or {
                "ids": []
            }
            for pid in slots["ids"]:
                p = await self.pending(pid)
                if cmd.args[0] == (p["request"]["as_sample"] or p["request"]["sample"]):
                    await self.cancel(pid, grant.email)
                    return "Transfer cancellation requested."
            return "No running transfer found. Use Cancel on the sample's preview if still pending."
        context = await self.context(grant.project_id)
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
    thread = event.get("thread_ts") or event["ts"]
    key = f"atomx-login-prompt:{payload.get('team_id')}:{event['channel']}:{thread}"
    prompt = await bridge.repository.get_connector_state(key)
    if not prompt or prompt.get("expires", 0) < time.time():
        return payload
    return {
        **payload,
        "event": {
            **event,
            "text": "[AtoMx password paste removed]",
            "atomx_password_removed": True,
        },
    }
