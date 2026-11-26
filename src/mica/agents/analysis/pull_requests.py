"""Requester-approved, immutable proposals; GitHub credentials stay in the controller."""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import re
import secrets
import time
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import BaseModel, Field
from slack_sdk.web.async_client import AsyncWebClient

from . import (
    access,
    approvals,
    memory_workspace,
    observability,
    recovery,
    reference_repos,
)
from . import pull_request_files as files
from .github_connections import Connections, Reconnect
from .storage import Storage
from .transport import SlackTransport

if TYPE_CHECKING:
    from .batch import BatchBridge

ACTION = "mica_pull_request_"


class PublicationConflict(ValueError):
    """A fresh branch and approval are required; preserve any prior GitHub writes."""


def key(event_id: str) -> str:
    files.prefix(event_id)
    # Workers have no access to write-access/. Never approve a worker-writable document.
    return f"write-access/pull-requests/{event_id}.json"


class Approval(BaseModel):
    event_id: str = Field(pattern=files.ID)
    proposal: files.Proposal
    pin: files.Pin
    tree: str = Field(pattern=files.SHA)
    branch: str
    branch_attempted: bool = False
    patch: str
    summary: list[str]
    expires_at: float
    status: Literal["pending", "publishing", "opened", "discarded", "expired", "blocked"] = (
        "pending"
    )
    notice_ts: str = ""
    patch_url: str = ""
    approved_at: float = 0
    commit: str = ""
    pr_url: str = ""
    principal: str
    connection_revision: str
    github_id: str
    failure_detail: str = ""
    failure: bool = False


def load(storage: Storage, event_id: str) -> tuple[Approval, str] | None:
    return approvals.load_model(storage, key(event_id), Approval)


async def admission(
    bridge: BatchBridge,
    storage: Storage,
    event_id: str,
    record: dict[str, Any],
    repo: str,
) -> str:
    from . import commands, connection_providers, contribution_privacy

    connection = Connections(bridge.app, bridge.settings, bridge.repository)
    connection.enabled(record)
    repo = repo.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}/[a-z0-9][a-z0-9_.-]{0,99}", repo):
        raise ValueError("Use a GitHub repository name such as owner/repo.")
    grant = await asyncio.to_thread(access.resolve, bridge.app, bridge.settings, storage, record)
    if not grant.allowed:
        raise PermissionError("Current project access is required")
    await asyncio.to_thread(contribution_privacy.protect, storage, record, grant.email)
    expected = record.get("pull_request_identity", {})
    try:
        user = await connection.current(
            grant.email, expected.get("revision", ""), expected.get("github_id", "")
        )
    except Reconnect:
        if not expected:
            await connection_providers.command(
                "github",
                bridge,
                storage,
                commands.Command("connect github"),
                grant,
                record,
                event_id,
            )
            raise ValueError(
                "I sent a private GitHub connection link. Connect, then resend the PR request."
            ) from None
        raise
    await connection.repositories(grant.email, user["revision"], repo)
    record["pull_request_identity"] = {
        "principal": grant.email,
        "revision": user["revision"],
        "github_id": user["github_id"],
    }
    return repo


async def stage(
    bridge: BatchBridge,
    storage: Storage,
    event_id: str,
    record: dict[str, Any],
) -> files.Pin:
    settings = bridge.settings
    repo = await admission(bridge, storage, event_id, record, record["pull_request_repo"])
    identity = record["pull_request_identity"]
    connection = Connections(bridge.app, settings, bridge.repository)
    if record.get("pull_request"):
        pin = files.Pin.model_validate(record["pull_request"])
        if pin.repo != repo:
            raise ValueError("Pull request target changed")
        await asyncio.to_thread(files.load_checkout, storage, pin)
        return pin
    limits = files.Limits(
        checkout_bytes=settings.pr_checkout_bytes,
        expanded_bytes=settings.pr_expanded_bytes,
        files=settings.pr_files,
        file_bytes=settings.pr_file_bytes,
        total_bytes=settings.pr_total_bytes,
    )
    base = "https://api.github.com/repos/" + repo
    async with connection.client(identity["principal"], identity["revision"]) as client:
        info = await reference_repos.get_json(client, base)
        if info["full_name"].lower() != repo or info.get("archived") or info.get("disabled"):
            raise ValueError("Repository is unavailable for contributions")
        branch = info["default_branch"]
        commit = await reference_repos.get_json(client, base + "/commits/" + quote(branch, safe=""))
        sha = commit["sha"]
        tree = await reference_repos.get_json(
            client, base + "/git/trees/" + sha, params={"recursive": "1"}, limit=16 * 1024**2
        )
        if tree.get("truncated"):
            raise ValueError("Repository tree is too large to inspect completely")
        index = files.Index(
            commit=sha,
            entries={
                item["path"]: files.Entry(sha=item["sha"], mode=item["mode"])
                for item in tree["tree"]
                if item["type"] != "tree"
            },
        )
        if files.tree_sha(index, []) != tree["sha"]:
            raise ValueError("Incomplete repository tree")
        # GitHub redirects private archives to codeload. Do not forward Authorization there.
        response = await client.get(base + "/tarball/" + sha)
        if response.status_code != 302:
            response.raise_for_status()
            raise ValueError("GitHub did not return an archive download")
        url = response.headers["location"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "codeload.github.com"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Unexpected GitHub archive host")
        async with httpx.AsyncClient(timeout=60) as downloader:
            raw = await reference_repos.get_bytes(downloader, url, limits.checkout_bytes)
    archive = await asyncio.to_thread(files.filtered_archive, raw, index, limits)
    index_raw = index.model_dump_json().encode()
    root = files.prefix(event_id)
    pin = files.Pin(
        repo=repo,
        commit=sha,
        branch=branch,
        checkout_key=root + "checkout.tar.gz",
        sha256=hashlib.sha256(archive).hexdigest(),
        index_key=root + "index.json",
        index_sha256=hashlib.sha256(index_raw).hexdigest(),
        limits=limits,
    )
    await asyncio.to_thread(storage.put, pin.checkout_key, archive)
    await asyncio.to_thread(storage.put, pin.index_key, index_raw)
    return pin


def authorize(bridge: BatchBridge, storage: Storage, event_id: str, record: dict[str, Any]) -> None:
    Connections(bridge.app, bridge.settings, bridge.repository).enabled(record)
    if (
        record.get("cancel_requested")
        or record.get("stop_reason")
        or memory_workspace.cancelled(storage, event_id)
    ):
        raise PermissionError("This run was cancelled or stopped")
    attempt = recovery.read_json(
        storage, f"{recovery.event_prefix(event_id)}/attempt-a{record.get('attempt', 0)}.json"
    )
    if attempt and attempt.get("outcome") == "stopped":
        raise PermissionError("This run was stopped")
    current = access.resolve(bridge.app, bridge.settings, storage, record)
    if (
        not current.allowed
        or current.project_id != record.get("project_id")
        or current.email != record.get("principal")
        or current.signature != record.get("access_signature")
    ):
        raise PermissionError("Project access changed. Send a new request.")


def patch_for(proposal: files.Proposal, base: dict[str, bytes]) -> tuple[str, list[str]]:
    patch, summary = [], []
    for change in sorted(proposal.changes, key=lambda c: c.path):
        before = base.get(change.path, b"").decode().splitlines(keepends=True)
        after = (change.content or "").splitlines(keepends=True)
        lines = list(
            difflib.unified_diff(
                before,
                after,
                fromfile="a/" + change.path if change.path in base else "/dev/null",
                tofile="b/" + change.path if change.content is not None else "/dev/null",
            )
        )
        plus = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
        minus = sum(line.startswith("-") and not line.startswith("---") for line in lines)
        summary.append(f"{change.path}: +{plus} / −{minus}")
        # Empty file creation/deletion still needs a visible, unambiguous review entry.
        patch.append(f"diff --git a/{change.path} b/{change.path}\n")
        if change.path not in base:
            patch.append("new file mode 100644\n")
        elif change.content is None:
            patch.append(f"deleted file mode {change.mode}\n")
        for line in lines:
            patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(patch), summary


def blocks(row: Approval) -> tuple[str, list[dict[str, Any]]]:
    if row.status == "blocked":
        text = (
            row.failure_detail
            + "\nAny partial branch is preserved: https://github.com/"
            + row.pin.repo
            + "/tree/"
            + row.branch
        )
    elif row.status == "opened":
        text = f"Draft PR opened: {row.pr_url}"
    elif row.status in {"discarded", "expired"}:
        text = f"Pull request proposal {row.status}. Nothing was pushed."
    else:
        text = (
            f"Open a draft PR in {row.pin.repo}\n{row.proposal.title}\n"
            f"Base: {row.pin.commit}\nBranch: {row.branch}\n"
            "Review the patch and proposed PR description below."
        )
        if row.failure:
            text += "\nGitHub delivery was interrupted. Retry to reconcile the same branch and PR."
    content: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "plain_text", "text": text[:2900]},
        }
    ]
    if row.status in {"pending", "publishing"}:
        description = (
            "PR description:\n"
            + row.proposal.body
            + "\n\nChecks reported by the worker (not verified):\n"
            + row.proposal.checks
        )
        for start in range(0, len(description), 2800):
            content.append(
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": description[start : start + 2800],
                    },
                }
            )
        for start in range(0, len(row.summary), 5):
            content.append(
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": "\n".join(row.summary[start : start + 5]),
                    },
                }
            )
        if row.patch_url:
            content.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<{escape(row.patch_url, quote=False)}|Review the exact patch> "
                        "before approving. "
                        "This branch can run the repository's CI with its secrets.",
                    },
                }
            )
            content.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "action_id": ACTION + action,
                            "value": row.event_id + ":" + row.proposal.id,
                            **({"style": "primary"} if action == "approve" else {}),
                        }
                        for action, label in (
                            [("approve", "Open draft PR"), ("discard", "Discard")]
                            if row.status == "pending"
                            else [("approve", "Retry draft PR")]
                        )
                    ],
                }
            )
    return text, content


async def card(bridge: BatchBridge, record: dict[str, Any], row: Approval) -> None:
    client = AsyncWebClient(token=bridge.app.slack_bot_token, timeout=20)
    event = record["payload"]["event"]
    thread = event.get("thread_ts") or event["ts"]
    if not row.patch_url and row.status == "pending":
        response = await SlackTransport(client).upload(
            content=row.patch,
            filename=f"mica-{row.proposal.id}.patch",
            title=f"Proposed change to {row.pin.repo}",
            channel=event["channel"],
            thread_ts=thread,
        )
        row.patch_url = str(response["files"][0]["permalink"])
    text, content = blocks(row)
    if row.notice_ts:
        await approvals.card(client, event["channel"], row.notice_ts, text, content)
    else:
        response = await SlackTransport(client).post(
            channel=event["channel"],
            thread_ts=thread,
            text=text,
            blocks=content,
            unfurl_links=False,
            unfurl_media=False,
            client_msg_id=str(uuid5(NAMESPACE_URL, "mica-pr:" + row.event_id)),
        )
        row.notice_ts = response["ts"]


def cleanup(storage: Storage, event_id: str, proposal_id: str) -> None:
    path = files.staged_key(event_id, proposal_id)
    try:
        _, generation = storage.get(path)
    except FileNotFoundError:
        return
    storage.delete_if(path, generation)


async def propose(
    bridge: BatchBridge,
    storage: Storage,
    event_id: str,
    proposal_id: str,
    attempt: int,
) -> dict[str, str]:
    async with approvals.lease(bridge, f"claude-poll:{event_id}", seconds=240):
        record = await bridge.repository.get_connector_state("claude-event:" + event_id)
        if not record or record.get("status") not in {"submitting", "running"}:
            raise PermissionError("This worker grant is inactive")
        if record.get("attempt", 0) != attempt:
            raise PermissionError("This worker attempt is no longer active")
        await asyncio.to_thread(authorize, bridge, storage, event_id, record)
        identity = record["pull_request_identity"]
        await Connections(bridge.app, bridge.settings, bridge.repository).current(
            identity["principal"], identity["revision"], identity["github_id"]
        )
        pin = files.Pin.model_validate(record.get("pull_request"))
        saved = await asyncio.to_thread(load, storage, event_id)
        if saved and saved[0].proposal.id == proposal_id:
            row, generation = saved
            await card(bridge, record, row)
            await asyncio.to_thread(approvals.save, storage, key(event_id), row, generation)
            await approvals.watch(bridge, KIND, event_id, row.expires_at)
            return {"status": row.status, "message": "Proposal already recorded; use its card."}
        if saved and saved[0].status != "pending":
            raise ValueError("This run already has a decided proposal. Start a new request.")
        raw = await asyncio.to_thread(
            files.read_bounded,
            storage,
            files.staged_key(event_id, proposal_id),
            pin.limits.total_bytes * 6 + 100_000,
        )
        proposal = files.Proposal.model_validate_json(raw)
        if proposal.id != proposal_id:
            raise ValueError("Proposal identity changed")
        index, base = await asyncio.to_thread(files.load_checkout, storage, pin)
        files.validate(proposal, pin, index, base)
        patch, summary = patch_for(proposal, base)
        slug = re.sub(r"[^a-z0-9]+", "-", proposal.title.lower()).strip("-")[:40] or "change"
        row = Approval(
            event_id=event_id,
            principal=record["pull_request_identity"]["principal"],
            connection_revision=record["pull_request_identity"]["revision"],
            github_id=record["pull_request_identity"]["github_id"],
            proposal=proposal,
            pin=pin,
            tree=files.tree_sha(index, proposal.changes),
            branch=f"mica/{slug}-{secrets.token_hex(8)}",
            patch=patch,
            summary=summary,
            expires_at=KIND.deadline(),
            notice_ts=saved[0].notice_ts if saved else "",
        )
        generation = await asyncio.to_thread(
            approvals.save, storage, key(event_id), row, saved[1] if saved else None
        )
        if saved:
            await asyncio.to_thread(cleanup, storage, event_id, saved[0].proposal.id)
        await approvals.watch(bridge, KIND, event_id, row.expires_at)
        await card(bridge, record, row)
        await asyncio.to_thread(approvals.save, storage, key(event_id), row, generation)
        return {
            "status": "pending",
            "message": "Patch posted. Only the requester can open the draft PR.",
        }


async def publish(
    bridge: BatchBridge,
    storage: Storage,
    record: dict[str, Any],
    row: Approval,
    generation: str,
) -> str:
    async def save() -> None:
        nonlocal generation
        generation = await asyncio.to_thread(
            approvals.save, storage, key(row.event_id), row, generation
        )

    connection = Connections(bridge.app, bridge.settings, bridge.repository)

    async def check() -> None:
        current = await bridge.repository.get_connector_state("claude-event:" + row.event_id)
        if not current or current.get("pull_request") != row.pin.model_dump():
            raise PermissionError("Pull request authorization changed")
        if current.get("pull_request_identity") != {
            "principal": row.principal,
            "revision": row.connection_revision,
            "github_id": row.github_id,
        }:
            raise Reconnect("GitHub identity changed. Submit a new request for fresh approval.")
        await asyncio.to_thread(authorize, bridge, storage, row.event_id, current)
        await connection.current(row.principal, row.connection_revision, row.github_id)
        await connection.repositories(row.principal, row.connection_revision, row.pin.repo)

    await check()
    if (
        not re.fullmatch(r"mica/[a-z0-9-]+-[a-f0-9]{16}", row.branch)
        or row.branch == row.pin.branch
    ):
        raise ValueError("Invalid contribution branch")
    api = "https://api.github.com/repos/" + row.pin.repo
    message = row.proposal.title + "\n\nMica-Proposal: " + row.proposal.id
    async with connection.client(row.principal, row.connection_revision) as client:
        info = await reference_repos.get_json(client, api)
        if info["full_name"].lower() != row.pin.repo or info["default_branch"] != row.pin.branch:
            raise ValueError("Repository or default branch changed; start a new request")
        ref_url = api + "/git/ref/heads/" + quote(row.branch, safe="/")
        response = await client.get(ref_url)
        if response.status_code == 404:
            if row.commit:
                raise ValueError(
                    "The approved contribution branch was deleted; start a new request"
                )
            await check()
            # Persist creation intent before the request so a lost response is reconcilable.
            row.branch_attempted = True
            await save()
            response = await client.post(
                api + "/git/refs",
                json={
                    "ref": "refs/heads/" + row.branch,
                    "sha": row.pin.commit,
                },
            )
            if response.status_code == 422:
                row.branch_attempted = False
                await save()
                raise PublicationConflict(
                    "Contribution branch creation was refused; start a new request"
                )
            response.raise_for_status()
            response = await client.get(ref_url)
        elif not row.branch_attempted:
            raise PublicationConflict("Contribution branch already exists; start a new request")
        response.raise_for_status()
        head = response.json()["object"]["sha"]
        if head == row.pin.commit:
            if row.commit:
                raise ValueError("Contribution branch was reset after the approved commit")
            await check()
            result = await client.post(
                "https://api.github.com/graphql",
                json={
                    "query": "mutation($input: CreateCommitOnBranchInput!) { "
                    "createCommitOnBranch(input: $input) { commit { oid } } }",
                    "variables": {
                        "input": {
                            "branch": {
                                "repositoryNameWithOwner": row.pin.repo,
                                "branchName": row.branch,
                            },
                            "expectedHeadOid": row.pin.commit,
                            "message": {
                                "headline": row.proposal.title,
                                "body": "Mica-Proposal: " + row.proposal.id,
                            },
                            "fileChanges": {
                                "additions": [
                                    {
                                        "path": c.path,
                                        "contents": base64.b64encode(c.content.encode()).decode(),
                                    }
                                    for c in row.proposal.changes
                                    if c.content is not None
                                ],
                                "deletions": [
                                    {"path": c.path}
                                    for c in row.proposal.changes
                                    if c.content is None
                                ],
                            },
                        }
                    },
                },
            )
            result.raise_for_status()
            if result.json().get("errors"):
                raise ValueError("GitHub could not confirm the approved commit; retry its card")
            head = result.json()["data"]["createCommitOnBranch"]["commit"]["oid"]
        # Reconcile a lost mutation response using the exact parent, message and complete tree.
        commit = await reference_repos.get_json(client, api + "/git/commits/" + head)
        if (
            commit["tree"]["sha"] != row.tree
            or commit["message"].rstrip("\n") != message
            or [p["sha"] for p in commit["parents"]] != [row.pin.commit]
            or (row.commit and row.commit != head)
        ):
            raise ValueError(
                "Contribution branch contains an unexpected commit; nothing was updated"
            )
        row.commit = head
        await save()
        pulls = await reference_repos.get_json(
            client,
            api + "/pulls",
            params={
                "head": row.pin.repo.split("/")[0] + ":" + row.branch,
                "state": "all",
            },
        )
        if pulls:
            if len(pulls) != 1:
                raise ValueError("Contribution branch has ambiguous pull requests")
            pr = pulls[0]
        else:
            await check()
            current_ref = await reference_repos.get_json(client, ref_url)
            if current_ref["object"]["sha"] != head:
                raise ValueError("Contribution branch changed; no pull request was opened")
            body = (
                row.proposal.body + f"\n\nBase commit: `{row.pin.commit}`\n\n"
                "Checks reported by the worker (not independently verified):\n"
                + row.proposal.checks
            )
            response = await client.post(
                api + "/pulls",
                json={
                    "title": row.proposal.title,
                    "body": body,
                    "head": row.branch,
                    "base": row.pin.branch,
                    "draft": True,
                },
            )
            response.raise_for_status()
            pr = response.json()
        if (
            pr["head"]["sha"] != head
            or pr["head"]["ref"] != row.branch
            or pr["base"]["ref"] != row.pin.branch
            or not pr.get("draft")
        ):
            raise ValueError("The existing pull request no longer matches the approved draft")
        row.pr_url, row.status, row.failure = pr["html_url"], "opened", False
        await save()
        await asyncio.to_thread(cleanup, storage, row.event_id, row.proposal.id)
    return generation


async def decide(
    bridge: BatchBridge,
    storage: Storage,
    payload: dict[str, Any],
    background: Any,
) -> dict[str, Any]:
    action = payload["actions"][0]
    verb = action["action_id"].removeprefix(ACTION)
    event_id, separator, proposal_id = str(action.get("value", "")).partition(":")
    if verb not in {"approve", "discard"} or not separator:
        raise ValueError("Invalid pull request action")
    key(event_id)
    async with approvals.lease(bridge, f"claude-poll:{event_id}", seconds=240):
        async with asyncio.timeout(200):
            record = await bridge.repository.get_connector_state("claude-event:" + event_id)
            saved = await asyncio.to_thread(load, storage, event_id)
            if not record or not saved:
                raise ValueError("Unknown pull request proposal")
            row, generation = saved
            approvals.requester(payload, {**record, "notice_ts": row.notice_ts})
            if proposal_id != row.proposal.id:
                raise ValueError("The proposal changed. Review the current patch and card.")
            if row.status in {"opened", "discarded", "expired", "blocked"}:
                await asyncio.to_thread(cleanup, storage, event_id, row.proposal.id)
                await card(bridge, record, row)
                return {}
            if row.status == "pending" and (verb == "discard" or approvals.expired(row.expires_at)):
                row.status = "discarded" if verb == "discard" else "expired"
                await asyncio.to_thread(approvals.save, storage, key(event_id), row, generation)
                await asyncio.to_thread(cleanup, storage, event_id, row.proposal.id)
                await card(bridge, record, row)
                return {}
            if verb != "approve":
                raise ValueError("This proposal was already approved; use its retry button.")
            await asyncio.to_thread(authorize, bridge, storage, event_id, record)
            if row.status == "pending":
                if approvals.expired(row.expires_at):
                    raise ValueError("Proposal expired while confirming. Send a new request.")
                row.status, row.approved_at = "publishing", time.time()
                generation = await asyncio.to_thread(
                    approvals.save, storage, key(event_id), row, generation
                )
            try:
                generation = await publish(bridge, storage, record, row, generation)
            except Exception as exc:
                # Reload the latest CAS generation: a successful earlier step may already be saved.
                saved = await asyncio.to_thread(load, storage, event_id)
                assert saved is not None
                row, generation = saved
                row.failure = True
                if isinstance(exc, Reconnect):
                    row.status = "blocked"
                    row.failure_detail = (
                        "GitHub authorization changed. Reconnect, then submit a new PR request "
                        "for fresh approval."
                    )
                if isinstance(exc, PublicationConflict):
                    row.status, row.failure_detail = "blocked", str(exc)
                await asyncio.to_thread(approvals.save, storage, key(event_id), row, generation)
                await observability.record_failure(
                    bridge.repository,
                    bridge.app,
                    "pull_request_failed",
                    occurrence=f"{event_id}:{proposal_id}",
                    event_id=event_id,
                    record=record,
                )
                await card(bridge, record, row)
                return {}
            await card(bridge, record, row)
            return {}


async def expire(bridge: BatchBridge, storage: Storage, event_id: str) -> None:
    async with approvals.lease(bridge, f"claude-poll:{event_id}", seconds=240):
        saved = await asyncio.to_thread(load, storage, event_id)
        record = await bridge.repository.get_connector_state("claude-event:" + event_id)
        if not saved or not record:
            return
        row, generation = saved
        if row.status not in {"pending", "expired"} or not approvals.expired(row.expires_at):
            return
        row.status = "expired"
        await asyncio.to_thread(approvals.save, storage, key(event_id), row, generation)
        await asyncio.to_thread(cleanup, storage, event_id, row.proposal.id)
        await card(bridge, record, row)


KIND = approvals.Kind("pull_request", (ACTION,), key, 3600, decide, expire)
