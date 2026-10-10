"""One-way private GitHub history, dispatched only by the control plane.

Uses GitHub's Git database API; no git subprocess and no credential in worker jobs.
The immutable history after-images are the saved generations, even after live GCS edits.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from . import access, job_lifecycle, knowledge
from .config import CHECKPOINT_PREFIX, Settings
from .identity import post, user_key
from .knowledge_commands import mirror_project
from .registry import Registry, project_key
from .storage import Conflict, Storage
from .user_memory import SECRET

if TYPE_CHECKING:
    from .batch import BatchBridge

STAMP = re.compile(r"[0-9]{8}T[0-9]{12}-[a-f0-9]{16}")
BOT = {"name": "Pistachio Knowledge", "email": "pistachio-knowledge@users.noreply.github.com"}


def configured(settings: Settings) -> bool:
    return bool(settings.knowledge_github_repo and settings.knowledge_github_token_secret)


def api_root(settings: Settings) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", settings.knowledge_github_repo):
        raise ValueError("Set the private knowledge repository as owner/repository.")
    return "https://api.github.com/repos/" + settings.knowledge_github_repo


async def api(
    client: httpx.AsyncClient, settings: Settings, method: str, path: str, **kwargs: Any
) -> Any:
    response = await client.request(method, api_root(settings) + path, **kwargs)
    response.raise_for_status()
    return response.json()


def token(settings: Settings) -> str:
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    resource = settings.knowledge_github_token_secret
    if not re.fullmatch(
        r"projects/[^/]+/secrets/[A-Za-z0-9_-]+/versions/(?:latest|[0-9]+)", resource
    ):
        raise ValueError("Set a Secret Manager version for the knowledge mirror token.")
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])  # type: ignore[no-untyped-call]
    with AuthorizedSession(credentials) as session:  # type: ignore[no-untyped-call]
        response = session.get(
            f"https://secretmanager.googleapis.com/v1/{resource}:access", timeout=20
        )
        response.raise_for_status()
        return base64.b64decode(response.json()["payload"]["data"]).decode().strip()


def client_for(value: str) -> httpx.AsyncClient:
    if not value:
        raise ValueError("The knowledge mirror credential is empty.")
    return httpx.AsyncClient(
        timeout=25,
        headers={
            "Authorization": f"Bearer {value}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )


def changes_key(pid: str, stamp: str) -> str:
    if not STAMP.fullmatch(stamp):
        raise ValueError("Invalid knowledge commit stamp.")
    return project_key(pid) + f"/knowledge-history/{stamp}/CHANGES.json"


def _stamp(pid: str, key: str) -> str:
    root = project_key(pid) + "/knowledge-history/"
    stamp = key.removeprefix(root).removesuffix("/CHANGES.json")
    if changes_key(pid, stamp) != key:
        raise ValueError("Knowledge history belongs to a different project.")
    return stamp


def _save_journal(storage: Storage, key: str, doc: dict[str, Any]) -> None:
    # Worker saves are complete before mirroring; preserve their immutable payload metadata.
    raw, generation = storage.get(key)
    latest = json.loads(raw)
    latest.update({k: v for k, v in doc.items() if k.startswith("mirror")})
    storage.put_if(key, json.dumps(latest, sort_keys=True).encode(), generation)


async def _tree(
    client: httpx.AsyncClient, settings: Settings, head: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    commit = await api(client, settings, "GET", "/git/commits/" + head)
    tree_sha = commit["tree"]["sha"]
    tree = await api(client, settings, "GET", "/git/trees/" + tree_sha, params={"recursive": "1"})
    if tree.get("truncated"):
        raise ValueError("The GitHub tree is truncated; cannot safely mirror or restore it.")
    return tree_sha, {entry["path"]: entry for entry in tree["tree"]}


def _bootstrap_snapshot(
    storage: Storage, settings: Settings, pid: str, key: str
) -> dict[str, bytes]:
    """Include older unchanged files on first mirror, even after history pruning.

    Rewind the live snapshot through later retained saves to reconstruct this stamp,
    so a delayed first mirror does not accidentally include future results.
    """
    with knowledge.project_lock(storage, pid):
        files = {p: v.data for p, v in knowledge.load(storage, pid, settings).items()}
        for later in reversed(knowledge.history_keys(storage, pid)):
            if later <= key:
                break
            journal = json.loads(storage.get(later)[0])
            if journal.get("status") != "complete":
                raise Conflict("A knowledge save is pending; retry bootstrap.")
            root = later.removesuffix("CHANGES.json")
            for change in journal["changes"]:
                if change.get("skipped"):
                    continue
                path = knowledge.valid_path(change["path"])
                if change["operation"] == "added":
                    files.pop(path, None)
                else:
                    files[path] = storage.get(root + "before/" + path)[0]
        return files


async def _mirror_one(
    storage: Storage, settings: Settings, pid: str, key: str, client: httpx.AsyncClient
) -> None:
    journal = json.loads(storage.get(key)[0])
    if journal.get("mirror_sha"):
        return
    project = Registry(storage).load().projects[pid]
    if SECRET.search(project.name):
        raise ValueError("The project name resembles a secret; rename it before mirroring.")
    if not mirror_project(project):
        return
    if journal.get("status") != "complete":
        raise Conflict("An earlier knowledge save is still pending.")
    if journal.get("project_id") != pid:
        raise ValueError("Knowledge history project mismatch.")
    root = key.removesuffix("CHANGES.json")
    payload: dict[str, bytes | None] = {}
    rejected = []
    for change in journal["changes"]:
        if change.get("skipped"):
            continue
        path = knowledge.valid_path(change["path"])
        if change["operation"] == "removed":
            payload[path] = None
            continue
        data = storage.get(root + "after/" + path)[0]
        try:
            knowledge.validate(path, data, settings)
        except ValueError:
            rejected.append(path)
            continue
        payload[path] = data
    journal["mirror_rejected"] = rejected
    ref = "heads/" + quote(settings.knowledge_github_branch, safe="/")
    for _ in range(4):
        # Recheck opt-out before each outbound mutation sequence.
        project = Registry(storage).load().projects[pid]
        if not mirror_project(project):
            return
        head = (await api(client, settings, "GET", "/git/ref/" + ref))["object"]["sha"]
        candidate = journal.get("mirror_candidate")
        if candidate:
            comparison = await api(client, settings, "GET", f"/compare/{candidate}...{head}")
            if comparison["status"] in {"ahead", "identical"}:
                journal.update(mirror_sha=candidate, mirrored=True)
                _save_journal(storage, key, journal)
                return
        tree_sha, tree = await _tree(client, settings, head)
        project_root = f"projects/{pid}/"
        if not any(path.startswith(project_root) for path in tree):
            initial = await asyncio.to_thread(_bootstrap_snapshot, storage, settings, pid, key)
            for path, initial_content in initial.items():
                if path in payload or path in rejected:
                    continue
                try:
                    knowledge.validate(path, initial_content, settings)
                except ValueError:
                    rejected.append(path)
                    continue
                payload[path] = initial_content
            journal["mirror_rejected"] = rejected
        # A trusted README is the only non-knowledge file written by the mirror.
        readme = project_root + "README.md"
        data_by_path = {project_root + path: data for path, data in payload.items()}
        if readme not in tree:
            data_by_path[readme] = (
                f"# {project.name}\n\nProject knowledge mirrored from Pistachio. "
                "Edit through Slack; GitHub edits do not sync back.\n"
            ).encode()
        entries = []
        for path, content in data_by_path.items():
            if content is None:
                if path in tree:
                    entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            else:
                blob = await api(
                    client,
                    settings,
                    "POST",
                    "/git/blobs",
                    json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
                )
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if not entries:
            journal.update(mirror_sha=head, mirrored=True)
            _save_journal(storage, key, journal)
            return
        tree_result = await api(
            client, settings, "POST", "/git/trees", json={"base_tree": tree_sha, "tree": entries}
        )
        summary = knowledge.Saved.model_validate(journal["summary"])
        date = (
            datetime.fromisoformat(journal["timestamp"])
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
        event = re.sub(r"[^a-zA-Z0-9_-]", "", str(journal["event_id"]))[:8]
        message = (
            f"{project.name}: {len(summary.added)}/{len(summary.changed)}/{len(summary.removed)} "
            f"via Slack ({date[:10]}, event {event})"
        )
        commit = await api(
            client,
            settings,
            "POST",
            "/git/commits",
            json={
                "message": message,
                "tree": tree_result["sha"],
                "parents": [head],
                "author": {**BOT, "date": date},
                "committer": {**BOT, "date": date},
            },
        )
        # Save the candidate before advancing the branch. A lost PATCH acknowledgement
        # is recovered through ancestry on retry, without another reachable commit.
        journal["mirror_candidate"] = commit["sha"]
        _save_journal(storage, key, journal)
        if not mirror_project(Registry(storage).load().projects[pid]):
            return
        try:
            await api(
                client,
                settings,
                "PATCH",
                "/git/refs/" + ref,
                json={"sha": commit["sha"], "force": False},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {409, 422}:
                raise
            new_head = (await api(client, settings, "GET", "/git/ref/" + ref))["object"]["sha"]
            if new_head == head:
                raise
            continue
        journal.update(mirror_sha=commit["sha"], mirrored=True)
        _save_journal(storage, key, journal)
        return
    raise Conflict("The GitHub branch kept moving; retry this mirror.")


async def mirror_through(
    storage: Storage, settings: Settings, pid: str, key: str, client: httpx.AsyncClient
) -> int:
    _stamp(pid, key)
    project = Registry(storage).load().projects.get(pid)
    if not settings.knowledge_github_repo or not project or not mirror_project(project):
        return 0
    await asyncio.to_thread(knowledge.recover, storage, pid, settings)
    # Check a recorded receipt before making any GitHub call.
    keys = [
        k
        for k in knowledge.history_keys(storage, pid)
        if k <= key and not json.loads(storage.get(k)[0]).get("mirror_sha")
    ]
    if not keys:
        return 0
    if not (await api(client, settings, "GET", ""))["private"]:
        raise ValueError("The knowledge mirror requires a private GitHub repository.")
    for pending in keys:
        await _mirror_one(storage, settings, pid, pending, client)
    return len(keys)


async def enqueue_save(
    bridge: BatchBridge, storage: Storage, pid: str, key: str, *, retry: str = ""
) -> bool:
    if not configured(bridge.settings):
        return False
    stamp = _stamp(pid, key)
    project = Registry(storage).load().projects.get(pid)
    if not project or not mirror_project(project):
        return False
    try:
        doc = json.loads(storage.get(key)[0])
    except FileNotFoundError:
        return False
    if doc.get("mirror_sha") or doc.get("status") != "complete":
        return False
    task_id = "knowledge-mirror-" + hashlib.sha256(key.encode()).hexdigest()[:40] + retry
    await job_lifecycle.enqueue(
        bridge.app, task_id, f"/api/v1/internal/slack-analysis/knowledge-mirror/{pid}/{stamp}"
    )
    return True


async def after_event(
    bridge: BatchBridge, storage: Storage, event_id: str, record: dict[str, Any]
) -> None:
    pid = record.get("project_id")
    keys: set[str] = set()
    if record.get("knowledge_changes_key"):
        key = record["knowledge_changes_key"]
        pid = key.split("/")[1]
        keys.add(key)
    if pid:
        for key in storage.keys(f"{CHECKPOINT_PREFIX}/events/{event_id}/"):
            if re.search(r"/attempt-a[0-9]+\.json$", key):
                attempt = json.loads(storage.get(key)[0])
                saved = knowledge.Saved.model_validate(attempt.get("knowledge", {}))
                if saved.changes_key:
                    keys.add(saved.changes_key)
                notice = knowledge.notice(saved)
                if notice and record.get("payload"):
                    receipt = f"knowledge-notice:{event_id}:a{attempt['attempt']}"
                    if not await bridge.repository.get_connector_state(receipt):
                        grant = await asyncio.to_thread(
                            access.resolve, bridge.app, bridge.settings, storage, record
                        )
                        if grant.allowed and grant.signature == record.get("access_signature"):
                            event = record["payload"]["event"]
                            await post(
                                bridge.app.slack_bot_token,
                                event["channel"],
                                event.get("thread_ts") or event["ts"],
                                notice,
                                event_id=receipt,
                            )
                            await bridge.repository.set_connector_state(
                                receipt, {"delivered": True}
                            )
        for key in sorted(keys):
            await enqueue_save(bridge, storage, pid, key)


async def process(bridge: BatchBridge, storage: Storage, pid: str, stamp: str) -> int:
    if not configured(bridge.settings):
        return 0
    key = changes_key(pid, stamp)
    mutex = f"knowledge-mirror:{pid}"
    claim = await bridge.repository.begin_event(mutex, lease_seconds=170)
    if claim.status != "claimed":
        raise Conflict("A mirror for this project is already running.")
    try:
        project = Registry(storage).load().projects.get(pid)
        if not project or not mirror_project(project):
            return 0
        pending = any(
            k <= key and not json.loads(storage.get(k)[0]).get("mirror_sha")
            for k in knowledge.history_keys(storage, pid)
        )
        if not pending:
            return 0
        async with asyncio.timeout(150):
            value = await asyncio.to_thread(token, bridge.settings)
            async with client_for(value) as client:
                return await mirror_through(storage, bridge.settings, pid, key, client)
    finally:
        await bridge.repository.release_event(mutex, claim.token)


async def reconcile(bridge: BatchBridge, storage: Storage, *, now: float | None = None) -> int:
    if not configured(bridge.settings):
        return 0
    now = time.time() if now is None else now
    count = 0
    for pid, project in Registry(storage).load().projects.items():
        if not mirror_project(project):
            continue
        for key in knowledge.history_keys(storage, pid):
            doc = json.loads(storage.get(key)[0])
            if now - datetime.fromisoformat(doc["timestamp"]).timestamp() >= 600:
                count += await enqueue_save(bridge, storage, pid, key, retry=f"-r{int(now // 600)}")
    return count


async def restore_commit(
    storage: Storage,
    settings: Settings,
    pid: str,
    sha: str,
    client: httpx.AsyncClient,
    *,
    actor: str,
    apply: bool = True,
) -> knowledge.Saved:
    project_key(pid)
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Use a full Git commit SHA for restoration.")
    if not (await api(client, settings, "GET", ""))["private"]:
        raise ValueError("The knowledge mirror requires a private GitHub repository.")
    _, tree = await _tree(client, settings, sha)
    root = f"projects/{pid}/"
    files: dict[str, bytes] = {}
    for path, entry in tree.items():
        if not path.startswith(root) or path == root + "README.md" or entry["type"] == "tree":
            continue
        relative = knowledge.valid_path(path.removeprefix(root))
        if entry["type"] != "blob" or entry["mode"] != "100644" or entry.get("size", 0) > 64 * 1024:
            raise ValueError("Unsafe file in the repository snapshot.")
        blob = await api(client, settings, "GET", "/git/blobs/" + entry["sha"])
        if blob.get("encoding") != "base64":
            raise ValueError("Unsupported GitHub blob encoding.")
        data = base64.b64decode(blob["content"])
        knowledge.validate(relative, data, settings)
        files[relative] = data
        if (
            len(files) > settings.knowledge_max_files
            or sum(map(len, files.values())) > settings.knowledge_total_kb * 1024
        ):
            raise ValueError("Repository snapshot exceeds project knowledge limits.")
    if not files:
        raise ValueError("The commit contains no knowledge for this project.")
    base = knowledge.load(storage, pid, settings)
    updates: dict[str, bytes | None] = {
        p: v for p, v in files.items() if p not in base or base[p].data != v
    }
    updates.update({p: None for p in base.keys() - files.keys()})
    if not apply:
        return knowledge.Saved(
            added=[p for p in updates if p not in base],
            changed=[p for p, v in updates.items() if p in base and v is not None],
            removed=[p for p, v in updates.items() if v is None],
        )
    return knowledge.commit(
        storage,
        pid,
        updates,
        base,
        settings,
        event_id="restore-" + uuid4().hex,
        user_key=user_key(actor),
    )
