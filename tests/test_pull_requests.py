"""Approval ownership, immutable review, and idempotent GitHub publication."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_github_connections import KEY, OWNER, tokens
from test_pull_request_files import checkout, proposal

from mica.agents.analysis import (
    access,
    approvals,
    contribution_privacy,
    memory_artifacts,
    memory_tasks,
    project_memory,
    pull_requests,
    thread_import,
)
from mica.agents.analysis import pull_request_files as files
from mica.agents.analysis.github_connections import Connections, Reconnect


class GitHub:
    def __init__(self, pin):
        self.pin, self.head, self.commit, self.pulls = pin, "", None, []
        self.mutations = []
        self.lose = ""
        self.foreign_before_pr = False
        self.on_write = None

    def client(self, credential):
        assert credential == "access-private"
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        method, path = request.method, request.url.path
        data = json.loads(request.content) if request.content else {}
        if method == "GET":
            if path == "/user/installations":
                value = {"total_count": 1, "installations": [{"id": 1}]}
            elif path == "/user/installations/1/repositories":
                value = {
                    "total_count": 1,
                    "repositories": [{"full_name": self.pin.repo, "permissions": {"push": True}}],
                }
            elif path.endswith("/" + self.pin.repo):
                value = {"full_name": self.pin.repo, "default_branch": "main"}
            elif "/git/ref/heads/" in path:
                if not self.head:
                    return httpx.Response(404)
                value = {"object": {"sha": self.head}}
            elif "/git/commits/" in path:
                value = self.commit
            elif path.endswith("/pulls"):
                if self.foreign_before_pr:
                    self.head = "f" * 40
                value = self.pulls
            else:
                raise AssertionError(str(request.url))
            return httpx.Response(200, json=value)
        assert method == "POST", (method, path)
        self.mutations.append(path)
        if self.on_write:
            self.on_write(path)
        if path.endswith("/git/refs"):
            assert data == {"ref": "refs/heads/" + self.row.branch, "sha": self.pin.commit}
            self.head = self.pin.commit
            value = {"object": {"sha": self.head}}
            stage = "branch"
        elif path == "/graphql":
            value = data["variables"]["input"]
            assert value["expectedHeadOid"] == self.pin.commit
            assert value["branch"]["branchName"] == self.row.branch
            self.head = "d" * 40
            self.commit = {
                "tree": {"sha": self.row.tree},
                "parents": [{"sha": self.pin.commit}],
                "message": self.row.proposal.title + "\n\nMica-Proposal: " + self.row.proposal.id,
            }
            value = {"data": {"createCommitOnBranch": {"commit": {"oid": self.head}}}}
            stage = "commit"
        elif path.endswith("/pulls"):
            assert data["draft"] is True and data["base"] == "main"
            assert OWNER not in json.dumps(data)
            value = {
                "html_url": "https://github.com/other-owner/project/pull/1",
                "draft": True,
                "head": {"sha": self.head, "ref": self.row.branch},
                "base": {"ref": "main"},
            }
            self.pulls.append(value)
            stage = "pr"
        else:
            raise AssertionError(path)
        if self.lose == stage:
            self.lose = ""
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(201, json=value)


@pytest.fixture
async def prepared(bridge, monkeypatch):
    # Start a normal run, then enable personal-repository admission for this requester.
    eid = await bridge.accept(payload())
    await bridge.process(eid, 0)
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    bridge.app.signed_link_secret = KEY
    bridge.settings.pull_requests = True
    bridge.settings.github_pr_client_id = "client123"
    bridge.settings.github_pr_app_slug = "mica-contributor"
    connection = Connections(bridge.app, bridge.settings, bridge.repository)
    await connection.repo.set_connector_state(
        connection.record_key(OWNER),
        {
            "owner": OWNER,
            "provider": connection.provider(),
            "revision": "rev1",
            "github_id": "123",
            "login": "alice",
            "status": "active",
            **connection.seal(OWNER, "rev1", tokens()),
        },
    )
    contribution_privacy.protect(bridge.store, record, OWNER)
    pin, index, base = checkout(bridge.store)
    record.update(
        pull_request=pin.model_dump(),
        pull_request_repo=pin.repo,
        pull_request_identity={"principal": OWNER, "revision": "rev1", "github_id": "123"},
    )
    await bridge.repository.set_connector_state("claude-event:" + eid, record)
    row = proposal([files.Change(path="src/main.py", content="print('after')\n")])
    bridge.store.put(files.staged_key(eid, row.id), files.encoded(row))
    monkeypatch.setattr(pull_requests, "AsyncWebClient", lambda **kw: bridge.test_slack)
    bridge.test_slack.files_upload_v2.return_value = {
        "files": [{"permalink": "https://slack/patch"}]
    }
    bridge.test_slack.chat_postMessage.return_value = {"ts": "99.0"}
    await pull_requests.propose(bridge, bridge.store, eid, row.id, 0)
    frozen, _ = pull_requests.load(bridge.store, eid)
    gh = GitHub(pin)
    gh.row = frozen
    monkeypatch.setattr(project_memory, "client_for", gh.client)
    action = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "D1"},
        "message": {"ts": "99.0"},
        "actions": [{"action_id": pull_requests.ACTION + "approve", "value": eid + ":" + row.id}],
    }
    return bridge, eid, record, gh, action


@pytest.mark.parametrize("lose", ["", "branch", "commit", "pr"])
async def test_retries_reconcile_same_branch_commit_and_draft_without_duplicates(prepared, lose):
    bridge, eid, record, gh, action = prepared
    gh.lose = lose
    # Replacing worker-writable bytes after review cannot affect the frozen patch.
    bridge.store.put(files.staged_key(eid, gh.row.proposal.id), b"malicious replacement")
    await pull_requests.decide(bridge, bridge.store, action, None)
    if lose:
        assert pull_requests.load(bridge.store, eid)[0].status == "publishing"
    await pull_requests.decide(bridge, bridge.store, action, None)
    row, _ = pull_requests.load(bridge.store, eid)
    assert row.status == "opened" and row.pr_url.endswith("/pull/1")
    assert len(gh.mutations) == 3 and len(gh.pulls) == 1
    assert row.proposal.changes[0].content == "print('after')\n"
    assert not bridge.store.keys(files.prefix(eid) + "proposals/")
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert len(gh.mutations) == 3


@pytest.mark.parametrize("field", ["user", "team", "channel", "message", "proposal"])
async def test_wrong_requester_and_stale_buttons_cannot_write(prepared, field):
    bridge, eid, record, gh, action = prepared
    if field == "proposal":
        action["actions"][0]["value"] = eid + ":" + "f" * 32
    else:
        action[field] = {"ts" if field == "message" else "id": "wrong"}
    with pytest.raises(ValueError):
        await pull_requests.decide(bridge, bridge.store, action, None)
    assert gh.mutations == []


@pytest.mark.parametrize("change", ["cancel", "project", "flag", "pilot", "connection"])
async def test_authority_is_rechecked_after_patch_review(prepared, change):
    bridge, eid, record, gh, action = prepared
    if change == "cancel":
        record["cancel_requested"] = True
    elif change == "project":
        record["access_signature"] = "changed"
    elif change == "flag":
        bridge.settings.pull_requests = False
    elif change == "pilot":
        bridge.settings.pull_request_user_ids = "T1:U2"
    else:
        connection = Connections(bridge.app, bridge.settings, bridge.repository)
        await connection.repo.set_connector_state(
            connection.record_key(OWNER),
            {
                "owner": OWNER,
                "provider": connection.provider(),
                "revision": "new",
                "status": "forgotten",
            },
        )
    await bridge.repository.set_connector_state("claude-event:" + eid, record)
    if change == "connection":
        await pull_requests.decide(bridge, bridge.store, action, None)
        row = pull_requests.load(bridge.store, eid)[0]
        assert row.status == "blocked" and "fresh approval" in row.failure_detail
    else:
        with pytest.raises((PermissionError, ValueError)):
            await pull_requests.decide(bridge, bridge.store, action, None)
    assert gh.mutations == []


async def test_foreign_head_never_overwritten_or_used_for_new_pr(prepared):
    bridge, eid, record, gh, action = prepared
    gh.foreign_before_pr = True
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert len(gh.mutations) == 2 and not gh.pulls
    row = pull_requests.load(bridge.store, eid)[0]
    assert row.status == "publishing" and row.failure


async def test_cancel_between_branch_and_commit_prevents_commit(prepared):
    bridge, eid, record, gh, action = prepared

    def revoke(path):
        if path.endswith("/git/refs"):
            bridge.settings.pull_requests = False

    gh.on_write = revoke
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert len(gh.mutations) == 1 and gh.head == gh.pin.commit


@pytest.mark.parametrize("state", ["discard", "expired"])
async def test_discard_and_expiry_clean_staging_without_github_writes(prepared, state):
    bridge, eid, record, gh, action = prepared
    if state == "discard":
        action["actions"][0]["action_id"] = pull_requests.ACTION + "discard"
    else:
        row, generation = pull_requests.load(bridge.store, eid)
        row.expires_at = 1
        approvals.save(bridge.store, pull_requests.key(eid), row, generation)
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert pull_requests.load(bridge.store, eid)[0].status in {"discarded", "expired"}
    assert gh.mutations == []
    assert not bridge.store.keys(files.prefix(eid) + "proposals/")


async def test_pending_proposal_replacement_invalidates_old_card(prepared):
    bridge, eid, record, gh, action = prepared
    updated = gh.row.proposal.model_copy(update={"id": "f" * 32, "title": "Revised patch"})
    bridge.store.put(files.staged_key(eid, updated.id), files.encoded(updated))
    await pull_requests.propose(bridge, bridge.store, eid, updated.id, 0)
    with pytest.raises(ValueError, match="changed"):
        await pull_requests.decide(bridge, bridge.store, action, None)
    assert gh.mutations == []
    with pytest.raises(PermissionError, match="attempt"):
        await pull_requests.propose(bridge, bridge.store, eid, updated.id, 1)


async def test_private_thread_stays_private_across_followups_and_project_changes(
    prepared, monkeypatch
):
    bridge, eid, record, gh, action = prepared
    followup = {
        **record,
        "private_contribution": False,
        "backchannel_enabled": True,
        "write_grant": {"paths": ["shared"]},
        "project_id": "different-project",
    }
    contribution_privacy.inherit(bridge.store, followup)
    assert followup["private_contribution"]
    assert not followup["backchannel_enabled"] and followup["write_grant"] is None
    assert not memory_tasks.conversation_turn(followup)
    await memory_tasks.enqueue(bridge, eid, followup)
    assert not followup["memory_pending"]
    assert (await bridge.repository.get_connector_state("claude-event:" + eid))[
        "memory_privacy"
    ] == "requester_only"
    impostor = json.loads(json.dumps(record))
    impostor["payload"]["event"]["user"] = "U2"
    assert not access.resolve(bridge.app, bridge.settings, bridge.store, impostor).allowed
    # Import must skip before reading any private checkpoint/history/script body.
    checkpoint = access.checkpoint_key(record["thread_key"])
    bridge.store.put(checkpoint, b"private body must not be parsed")
    monkeypatch.setattr(memory_artifacts, "checkpoints", lambda storage: [checkpoint])
    assert (
        await thread_import.import_threads(
            bridge.store, project_memory.GitHubMemory(bridge.settings), apply=False
        )
        == []
    )


async def test_requester_without_connection_gets_private_link_and_no_worker(prepared, monkeypatch):
    bridge, eid, record, gh, action = prepared
    from mica.agents.analysis import connection_providers

    record.pop("pull_request_identity")
    connection = Connections(bridge.app, bridge.settings, bridge.repository)
    await connection.repo.set_connector_state(connection.record_key(OWNER), {})
    deliver = AsyncMock()
    monkeypatch.setattr(connection_providers, "command", deliver)
    with pytest.raises(ValueError, match="Connect, then resend"):
        await pull_requests.admission(bridge, bridge.store, eid, record, gh.pin.repo)
    deliver.assert_awaited_once()
    assert gh.mutations == []
    record["pull_request_identity"] = {"revision": "old", "github_id": "123"}
    with pytest.raises(Reconnect):
        await pull_requests.admission(bridge, bridge.store, eid, record, gh.pin.repo)
    assert deliver.await_count == 1


async def test_existing_branch_at_base_is_never_treated_as_a_retry(prepared):
    bridge, eid, record, gh, action = prepared
    gh.head = gh.pin.commit
    assert not gh.row.branch_attempted
    assert len(gh.row.branch.rsplit("-", 1)[1]) == 16
    await pull_requests.decide(bridge, bridge.store, action, None)
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert not gh.mutations and not gh.pulls
    assert not pull_requests.load(bridge.store, eid)[0].branch_attempted


async def test_deleted_approved_branch_is_not_recreated(prepared):
    bridge, eid, record, gh, action = prepared
    gh.lose = "pr"
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert pull_requests.load(bridge.store, eid)[0].commit
    gh.head = ""
    count = len(gh.mutations)
    await pull_requests.decide(bridge, bridge.store, action, None)
    assert len(gh.mutations) == count and gh.head == ""
