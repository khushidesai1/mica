"""Bounded, operator-configured background intake through ordinary private threads."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import access, identity, project_memory, write_access
from .ambient_config import AmbientPerson, AmbientTask, sample_name
from .config import Settings
from .registry import Registry, RegistryDoc
from .storage import Conflict

if TYPE_CHECKING:
    from .batch import BatchBridge

log = logging.getLogger(__name__)
SCAN_SECONDS = 45
SAMPLE_LIMIT = 500


class Bucket:
    """Metadata only; list one level and never download a sample during discovery."""

    def __init__(self, name: str) -> None:
        from google.cloud import storage

        self.bucket = storage.Client().bucket(name)

    def samples(self, template: str, cursor: str = "") -> tuple[list[str], str]:
        prefix = template.split("{sample}")[0]
        listing = self.bucket.list_blobs(
            prefix=prefix,
            delimiter="/",
            max_results=50,
            page_size=50,
            page_token=cursor or None,
            timeout=5,
            retry=None,
        )
        folders: list[str] = []
        page = next(iter(listing.pages), None)
        if page is not None:
            list(page)
            folders.extend(page.prefixes)
        folders = sorted(folders)[:SAMPLE_LIMIT]
        samples = []
        for folder in folders:
            try:
                samples.append(sample_name(folder[len(prefix) :].rstrip("/")))
            except ValueError:
                continue
        # Move beyond the last folder; reset after reaching the end, avoiding starvation.
        return samples, str(listing.next_page_token or "")

    def marker(self, path: str) -> dict[str, Any] | None:
        from google.api_core.exceptions import NotFound

        blob = self.bucket.blob(path)
        try:
            blob.reload(timeout=5, retry=None)
        except NotFound:
            return None
        if blob.time_created is None:
            raise ValueError("Marker has no creation time")
        return {
            "path": path,
            "generation": str(blob.generation),
            "created": blob.time_created.timestamp(),
        }

    def exists(self, prefix: str) -> bool:
        return (
            next(
                iter(self.bucket.list_blobs(prefix=prefix, max_results=1, timeout=5, retry=None)),
                None,
            )
            is not None
        )


def issuer_permitted(
    settings: Settings, doc: RegistryDoc, grant: write_access.EventWriteGrant
) -> bool:
    task = next((t for t in settings.ambient_tasks if f"ambient:{t.id}" == grant.issuer), None)
    return bool(
        task
        and task.enabled
        and not task.dry_run
        and task.project == grant.project_id
        and task.authorized_by.email == grant.owner
        and task.output_uri(doc.projects[grant.project_id].data_uri, grant.sample) == grant.uri
    )


def validate_scope(bridge: BatchBridge, task: AmbientTask, sample: str) -> RegistryDoc:
    doc = Registry(bridge.storage).load()
    if not doc.owns(task.project, task.authorized_by.email):
        raise ValueError("The configured owner no longer owns this project")
    uri = task.output_uri(doc.projects[task.project].data_uri, sample)
    write_access.validate_scope(uri, bridge.settings)
    write_access.validate_project(uri, doc, task.project, task.authorized_by.email)
    for person in task.recipients:
        if not doc.can_use(task.project, person.email):
            raise ValueError("A configured recipient is no longer a project member")
    return doc


def person_permitted(
    bridge: BatchBridge, person: AmbientPerson, project: str, team: str, *, owner: bool = False
) -> bool:
    doc = Registry(bridge.storage).load()
    return bool(
        (doc.owns(project, person.email) if owner else doc.can_use(project, person.email))
        and identity.resolve_email(
            bridge.app, bridge.settings, bridge.storage, team, person.slack_user
        )
        == person.email
    )


async def validate_start(
    bridge: BatchBridge, task: AmbientTask, sample: str, team: str
) -> RegistryDoc:
    doc = await asyncio.to_thread(validate_scope, bridge, task, sample)
    for person in [task.authorized_by, *task.recipients]:
        if not await asyncio.to_thread(
            person_permitted,
            bridge,
            person,
            task.project,
            team,
            owner=person == task.authorized_by,
        ):
            raise ValueError("A configured Slack identity or project permission has changed")
    try:
        recipe, _ = await project_memory.GitHubMemory(bridge.settings).read_bytes(
            task.project, task.recipe
        )
    except FileNotFoundError as exc:
        raise ValueError("The configured project recipe is missing") from exc
    if not recipe.strip():
        raise ValueError("The configured project recipe is empty")
    return doc


def context(
    bridge: BatchBridge,
    doc: RegistryDoc,
    project: str,
    person: AmbientPerson,
    channel: str,
    ts: str,
    team: str,
) -> tuple[str, dict[str, Any]]:
    from .batch import digest

    thread_key = digest(f"{team}:{channel}:{ts}")
    uri = urlsplit(doc.projects[project].data_uri)
    state: dict[str, Any] = {
        "context": {
            "project_slug": project,
            "project_md": project_memory.NO_GUIDE,
            "data_path": str(bridge.settings.data_mount_root / uri.netloc / uri.path.lstrip("/")),
            "workdir": access.private_workdir(bridge.settings, person.email, project, thread_key),
            "thread_ts": ts,
            "session_id": None,
            "survey_only": False,
        },
        "turn": 0,
        "is_init": False,
        "messages": [],
        "private_owner": person.email,
        "thread_memory_origin": bridge.settings.thread_memory,
    }
    return thread_key, state


async def seed(
    bridge: BatchBridge,
    doc: RegistryDoc,
    project: str,
    person: AmbientPerson,
    channel: str,
    ts: str,
    team: str,
    summary: str = "",
) -> str:
    thread_key, state = context(bridge, doc, project, person, channel, ts, team)
    if summary:
        state["messages"] = [
            {
                "id": "ambient-result",
                "user": "Background run result",
                "assistant": summary[:6000],
                "success": True,
            }
        ]
    with suppress(Conflict):
        await asyncio.to_thread(
            bridge.storage.put_if,
            access.checkpoint_key(thread_key),
            json.dumps(state).encode(),
            None,
        )
    if summary:
        # The start notice already seeded the thread. Append the posted result without
        # replacing a collaborator's concurrent reply or restored workspace.
        for _ in range(3):
            raw, generation = await asyncio.to_thread(
                bridge.storage.get, access.checkpoint_key(thread_key)
            )
            saved = json.loads(raw)
            messages = saved.setdefault("messages", [])
            if any(m.get("id") == "ambient-result" for m in messages):
                break
            messages.append(state["messages"][0])
            try:
                await asyncio.to_thread(
                    bridge.storage.put_if,
                    access.checkpoint_key(thread_key),
                    json.dumps(saved).encode(),
                    generation,
                )
                break
            except Conflict:
                continue
        else:
            raise RuntimeError("Recipient conversation changed during result delivery")
    return thread_key


async def start(
    bridge: BatchBridge, task: AmbientTask, state: dict[str, Any], team: str, queued: int
) -> None:
    from .ambient_delivery import post_once
    from .batch import digest

    sample, key = state["sample"], state["key"]
    if state.get("definition"):
        frozen = AmbientTask.model_validate(state["definition"])
        if (frozen.project, frozen.authorized_by, frozen.output) != (
            task.project,
            task.authorized_by,
            task.output,
        ):
            raise ValueError("Task authority changed during startup; use a new task ID")
        task = frozen  # Recipients and routing cannot move after any notices were posted.
    doc = await validate_start(bridge, task, sample, team)
    if not state.get("root"):
        bucket = Bucket(urlsplit(doc.projects[task.project].data_uri).netloc)
        for marker in state["markers"]:
            current = await asyncio.to_thread(bucket.marker, marker["path"])
            if current is None or current["generation"] != marker["generation"]:
                raise ValueError("Transfer markers changed while queued; inspect the sample")
        if await asyncio.to_thread(bucket.exists, task.render(task.skip_if_exists, sample)):
            state.update(status="skipped", reason="Output appeared while queued")
            state.pop("ambient_due", None)
            await bridge.repository.set_connector_state(key, state)
            return
    state.setdefault("definition", task.model_dump(mode="json"))
    state.setdefault("start", time.time())
    state.setdefault(
        "deadline_at",
        state["start"] + min(task.worker.max_hours * 3600, bridge.settings.max_run_seconds),
    )
    state.setdefault("preset", task.worker.model_dump())
    state.setdefault("recipients", [p.model_dump() for p in task.recipients])
    state.setdefault("owner", task.authorized_by.model_dump())
    state.update(status="starting", team=team)
    await bridge.repository.set_connector_state(key, state)
    root = await post_once(
        bridge,
        key + ":root",
        task.authorized_by.slack_user,
        f"Ambient task set up by {task.authorized_by.email}: {sample} finished transferring. "
        f"Checking files, then following the project recipe and building the report. "
        f"I'll deliver it here and to the configured collaborators. {queued} more queued.",
    )
    state["root"] = root
    await bridge.repository.set_connector_state(key, state)
    notices = state.setdefault("notices", {})
    for person in task.recipients:
        if person.email == task.authorized_by.email:
            continue
        notices[person.email] = await post_once(
            bridge,
            key + ":start:" + person.slack_user,
            person.slack_user,
            f"Ambient task set up by {task.authorized_by.email}: checking and processing "
            f"{sample}. I'll post the final summary and report in this thread.",
        )
        await seed(
            bridge,
            doc,
            task.project,
            person,
            notices[person.email]["channel"],
            notices[person.email]["ts"],
            team,
        )
        await bridge.repository.set_connector_state(key, state)
    await seed(bridge, doc, task.project, task.authorized_by, root["channel"], root["ts"], team)
    event_id = digest(f"{team}:{root['channel']}:{root['ts']}")
    grant = write_access.EventWriteGrant(
        id=write_access.event_grant_id(event_id),
        event_id=event_id,
        project_id=task.project,
        issuer=f"ambient:{task.id}",
        admin_id=f"ambient:{task.id}",
        owner=task.authorized_by.email,
        actor=task.authorized_by.email,
        sample=sample,
        uri=task.output_uri(doc.projects[task.project].data_uri, sample),
        created_at=datetime.fromtimestamp(state["start"], UTC),
        expires_at=datetime.fromtimestamp(state["deadline_at"], UTC)
        + timedelta(seconds=bridge.settings.write_upload_seconds),
    )
    await asyncio.to_thread(write_access.issue_event, bridge.storage, grant)
    prompt = task.task.format(
        sample=sample, markers=json.dumps(state["markers"]), output=grant.uri, recipe=task.recipe
    )
    prompt = (
        f"Unattended ambient run set up by {task.authorized_by.email}. "
        "Follow the project recipe and skills. Check completeness first; stop and report any "
        "failure. If a consequential choice is unspecified, stop after the checks and ask here. "
        f"Publish only the requested new data objects under {grant.uri}. "
        "Follow the task and recipe's report format and storage instructions. "
        "Deliver the requested PDF and a short summary to the configured recipients; "
        "a data write grant does not require publishing reports or review intermediates.\n" + prompt
    )
    origin = {
        "task": task.id,
        "sample": sample,
        "markers": state["markers"],
        "preset": state["preset"],
        "deadline_at": state["deadline_at"],
        "key": key,
        "return_scripts": task.return_scripts,
    }
    # Save event identity before accept: a lost enqueue response cannot allocate another run.
    state["event_id"] = event_id
    await bridge.repository.set_connector_state(key, state)
    await bridge.accept(
        {
            "type": "event_callback",
            "team_id": team,
            "event": {
                "type": "message",
                "channel_type": "im",
                "channel": root["channel"],
                "ts": root["ts"],
                "thread_ts": root["ts"],
                "user": task.authorized_by.slack_user,
                "text": prompt,
            },
        },
        principal=task.authorized_by.email,
        ambient=origin,
    )
    state.update(status="running", ambient_due=time.time() + 300)
    await bridge.repository.set_connector_state(key, state)
    log.info("ambient_started task=%s sample=%s event=%s", task.id, sample, event_id)


async def _scan(bridge: BatchBridge, *, team: str = "") -> dict[str, int]:
    from slack_sdk.web.async_client import AsyncWebClient

    from .ambient_delivery import post_once, terminal

    result = {"ambient_queued": 0, "ambient_started": 0, "ambient_blocked": 0, "ambient_dry_run": 0}
    tasks = {t.id: t for t in bridge.settings.ambient_tasks if t.enabled}
    if not tasks:
        return result
    metas: dict[str, dict[str, Any]] = {}
    now = time.time()
    day = datetime.fromtimestamp(now, UTC).date().isoformat()
    # Reconcile active reservations independently of the bounded queue query.
    for task in tasks.values():
        meta_key = f"ambient-task:{task.id}"
        meta = await bridge.repository.get_connector_state(meta_key) or {}
        metas[task.id] = meta
        if not task.dry_run:
            active = []
            for key in meta.get("active", []):
                state = await bridge.repository.get_connector_state(key) or {}
                receipt = await bridge.repository.get_connector_state(
                    f"claude-event:{state.get('event_id', '')}"
                )
                if receipt and receipt["status"] in {"complete", "failed"}:
                    await terminal(bridge, receipt["event_id"], receipt)
                elif state.get("status") not in {"done", "blocked", "skipped"}:
                    active.append(key)
            meta["active"] = active
            if meta.get("day") != day:
                meta.update(day=day, starts=[])
            await bridge.repository.set_connector_state(meta_key, meta)
        doc = await asyncio.to_thread(Registry(bridge.storage).load)
        if task.project not in doc.projects:
            log.warning("ambient_project_unavailable task=%s", task.id)
            continue
        bucket = Bucket(urlsplit(doc.projects[task.project].data_uri).netloc)
        samples, cursor = await asyncio.to_thread(
            bucket.samples, task.markers[0], meta.get("cursor", "")
        )
        for sample in samples:
            key = f"ambient:{task.id}:{sample}"
            if not task.dry_run and await bridge.repository.get_connector_state(key):
                continue
            markers = [
                await asyncio.to_thread(bucket.marker, task.render(p, sample)) for p in task.markers
            ]
            if any(m is None for m in markers):
                continue
            present = [m for m in markers if m is not None]
            status = "queued"
            if any(m["created"] < task.enabled_at.timestamp() for m in present):
                status = "ignored"
            elif await asyncio.to_thread(bucket.exists, task.render(task.skip_if_exists, sample)):
                status = "skipped"
            if task.dry_run:
                result["ambient_dry_run"] += 1
                log.info("ambient_dry_run task=%s sample=%s status=%s", task.id, sample, status)
                continue
            state = {
                "key": key,
                "task": task.id,
                "project": task.project,
                "sample": sample,
                "status": status,
                "markers": present,
                "marker_at": max(m["created"] for m in present),
            }
            if status == "queued":
                state["ambient_due"] = state["marker_at"]
            if await bridge.repository.create_connector_state(key, state) and status == "queued":
                result["ambient_queued"] += 1
        if not task.dry_run:
            meta["cursor"] = cursor
            await bridge.repository.set_connector_state(meta_key, meta)
    pending = await bridge.repository.due_connector_states("ambient_due", now, limit=SAMPLE_LIMIT)
    pending.sort(key=lambda row: (row[1].get("marker_at", 0), row[0]))
    for key, state in pending:
        selected_task = tasks.get(state["task"])
        if not selected_task or selected_task.dry_run or state["status"] == "running":
            continue
        task = selected_task
        meta = metas[task.id]
        reserved = key in meta.get("active", [])
        if not reserved and (
            len(meta.get("active", [])) >= task.max_concurrent
            or len(meta.get("starts", [])) >= task.max_starts_per_day
            or sum(len(m.get("active", [])) for m in metas.values())
            >= bridge.settings.max_concurrent_runs
        ):
            continue
        if not reserved:
            available = False
            for slot in range(bridge.settings.max_concurrent_runs):
                slot_key = f"claude-slot:{slot}"
                probe = await bridge.repository.begin_event(slot_key, lease_seconds=5)
                if probe.status == "claimed":
                    await bridge.repository.release_event(slot_key, probe.token)
                    available = True
                    break
            if not available:
                continue
        if not team:
            auth = await AsyncWebClient(token=bridge.app.slack_bot_token, timeout=5).auth_test()
            team = str(auth["team_id"])
        try:
            # Validate before consuming daily quota. Revalidation also unblocks fixed recipes.
            await validate_start(bridge, task, state["sample"], team)
            if not reserved:
                meta.setdefault("active", []).append(key)
                if key not in meta.setdefault("starts", []):
                    meta["starts"].append(key)
                await bridge.repository.set_connector_state(f"ambient-task:{task.id}", meta)
            await start(bridge, task, state, team, max(0, len(pending) - 1))
            result["ambient_started"] += int(state["status"] == "running")
        except ValueError as exc:
            state.update(status="blocked", reason=str(exc), ambient_due=now + 300)
            await bridge.repository.set_connector_state(key, state)
            result["ambient_blocked"] += 1
            log.warning(
                "ambient_blocked task=%s sample=%s reason=%s", task.id, state["sample"], exc
            )
            # Do not disclose project details to an identity that no longer matches.
            email = await asyncio.to_thread(
                identity.resolve_email,
                bridge.app,
                bridge.settings,
                bridge.storage,
                team,
                task.authorized_by.slack_user,
            )
            if email == task.authorized_by.email:
                await post_once(
                    bridge,
                    key + ":blocked",
                    task.authorized_by.slack_user,
                    f"Ambient task {task.id}, set up by {task.authorized_by.email}, "
                    f"is blocked: {exc}. No new computation was started.",
                )
    return result


async def scan(bridge: BatchBridge, *, team: str = "") -> dict[str, int]:
    if not bridge.settings.ambient_tasks:
        return {}
    # Dry runs must not write even a lease. All real scans share one bounded admission lease.
    dry = all(not t.enabled or t.dry_run for t in bridge.settings.ambient_tasks)
    claim = None if dry else await bridge.repository.begin_event("ambient-scan", lease_seconds=90)
    if claim and claim.status != "claimed":
        return {"ambient_busy": 1}
    try:
        async with asyncio.timeout(SCAN_SECONDS):
            return await _scan(bridge, team=team)
    except Exception:
        log.exception("ambient_scan_failed")
        return {"ambient_errors": 1}
    finally:
        if claim:
            await bridge.repository.release_event("ambient-scan", claim.token)
