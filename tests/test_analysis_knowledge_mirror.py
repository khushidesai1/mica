"""A fake Git database tests mirrors without contacting GitHub or GCP."""

# ruff: noqa: F811
import base64
import hashlib
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from analysis_helpers import seed_project
from test_analysis_batch import bridge  # noqa: F401

from pistachio.agents.analysis import knowledge, knowledge_mirror
from pistachio.agents.analysis.batch import job_spec
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.registry import Registry
from pistachio.agents.analysis.storage import LocalStorage

NOTE = b"type: fact\nsource: script, 2026-10-09, thread\nstatus: current\nTMA26 failed."


class GitHub:
    def __init__(self):
        self.head = "0" * 40
        self.trees = {"initial": {}}
        self.commits = {self.head: {"sha": self.head, "tree": {"sha": "initial"}, "parents": []}}
        self.blobs = {}
        self.messages = []
        self.moves = 0
        self.private = True

    def store(self, table, data):
        sha = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
        table[sha] = data
        return sha

    def handle(self, request):
        path = request.url.path.split("/repos/lab/knowledge", 1)[1]
        body = json.loads(request.content) if request.content else {}
        if not path:
            return httpx.Response(200, json={"private": self.private})
        if path.startswith("/git/ref/"):
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path.startswith("/git/refs/"):
            if self.moves:
                self.moves -= 1
                other = {
                    "message": "other project",
                    "tree": self.commits[self.head]["tree"],
                    "parents": [{"sha": self.head}],
                }
                self.head = self.store(self.commits, other)
                return httpx.Response(422, json={"message": "not fast forward"})
            if body["force"] or self.commits[body["sha"]]["parents"][0]["sha"] != self.head:
                return httpx.Response(422)
            self.head = body["sha"]
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path == "/git/blobs":
            data = (
                base64.b64decode(body["content"])
                if body["encoding"] == "base64"
                else body["content"].encode()
            )
            sha = hashlib.sha1(data).hexdigest()
            self.blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if path.startswith("/git/blobs/"):
            data = self.blobs[path.rsplit("/", 1)[1]]
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                    "size": len(data),
                },
            )
        if path == "/git/trees":
            tree = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry["sha"] is None:
                    tree.pop(entry["path"], None)
                else:
                    tree[entry["path"]] = entry["sha"]
            return httpx.Response(201, json={"sha": self.store(self.trees, tree)})
        if path.startswith("/git/trees/"):
            tree = self.trees[path.rsplit("/", 1)[1]]
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {
                            "path": p,
                            "sha": s,
                            "type": "blob",
                            "mode": "100644",
                            "size": len(self.blobs[s]),
                        }
                        for p, s in tree.items()
                    ],
                },
            )
        if path == "/git/commits":
            self.messages.append(body["message"])
            commit = {
                **body,
                "tree": {"sha": body["tree"]},
                "parents": [{"sha": s} for s in body["parents"]],
            }
            return httpx.Response(201, json={"sha": self.store(self.commits, commit)})
        if path.startswith("/git/commits/"):
            return httpx.Response(200, json=self.commits[path.rsplit("/", 1)[1]])
        if path.startswith("/compare/"):
            ancestor, descendant = path.rsplit("/", 1)[1].split("...")
            found = descendant == ancestor
            while not found and self.commits[descendant]["parents"]:
                descendant = self.commits[descendant]["parents"][0]["sha"]
                found = descendant == ancestor
            return httpx.Response(200, json={"status": "ahead" if found else "diverged"})
        pytest.fail(f"Unexpected GitHub request: {request.method} {path}")

    def files(self):
        return {
            p: self.blobs[s] for p, s in self.trees[self.commits[self.head]["tree"]["sha"]].items()
        }


@pytest.fixture
def mirror(tmp_path):
    store = LocalStorage(tmp_path / "store")
    pid = seed_project(store)
    settings = Settings(
        _env_file=None,
        knowledge_github_repo="lab/knowledge",
        knowledge_github_token_secret="projects/test/secrets/knowledge/versions/latest",
    )
    registry = Registry(store)

    def enable(doc):
        doc.projects[pid].knowledge_github = True
        return pid

    registry.update("enable", "alice@example.com", "enable", enable)
    github = GitHub()
    return store, pid, settings, github


def save(mirror, event, data=NOTE):
    store, pid, settings, _ = mirror
    base = knowledge.load(store, pid, settings)
    return knowledge.commit(
        store,
        pid,
        {"data/samples.md": data},
        base,
        settings,
        event_id=event,
        user_key="PRIVATE-USER-TEXT",
    )


async def run(mirror, key):
    store, pid, settings, github = mirror
    async with httpx.AsyncClient(transport=httpx.MockTransport(github.handle)) as client:
        return await knowledge_mirror.mirror_through(store, settings, pid, key, client)


async def test_one_commit_retry_and_immutable_saved_versions(mirror):
    first = save(mirror, "first")
    second = save(mirror, "second", NOTE + b" updated")
    await run(mirror, first.changes_key)
    store, pid, _, github = mirror
    assert github.files()[f"projects/{pid}/data/samples.md"] == NOTE
    assert set(github.files()) == {
        f"projects/{pid}/data/samples.md",
        f"projects/{pid}/README.md",
        f"projects/{pid}/PROJECT.md",
    }
    assert len(github.messages) == 2 and "PRIVATE-USER-TEXT" not in github.messages[0]
    await run(mirror, first.changes_key)
    assert len(github.messages) == 2
    await run(mirror, second.changes_key)
    assert github.files()[f"projects/{pid}/data/samples.md"] == NOTE + b" updated"
    assert json.loads(store.get(first.changes_key)[0])["mirror_sha"]


async def test_out_of_order_branch_moves_and_deletion(mirror):
    first = save(mirror, "first")
    second = save(mirror, "second", NOTE + b" updated")
    mirror[3].moves = 1
    await run(mirror, second.changes_key)
    assert json.loads(mirror[0].get(first.changes_key)[0])["mirrored"]
    deletion = save(mirror, "delete", None)
    await run(mirror, deletion.changes_key)
    assert f"projects/{mirror[1]}/data/samples.md" not in mirror[3].files()


async def test_private_project_requires_opt_in_and_public_repo_is_refused(mirror):
    saved = save(mirror, "first")
    store, pid, _, github = mirror

    def disable(doc):
        doc.projects[pid].knowledge_github = None
        return pid

    Registry(store).update("private", "alice@example.com", "private", disable)
    await run(mirror, saved.changes_key)
    assert not github.messages

    def share(doc):
        return doc.share(pid, "alice@example.com", "bob@example.com")

    Registry(store).update("share", "alice@example.com", "share", share)
    github.private = False
    with pytest.raises(ValueError, match="private"):
        await run(mirror, saved.changes_key)


async def test_secret_scan_again_before_mirroring(mirror):
    saved = save(mirror, "first")
    mirror[0].put(
        saved.changes_key.removesuffix("CHANGES.json") + "after/data/samples.md",
        NOTE + b" password=unsafe",
    )
    await run(mirror, saved.changes_key)
    assert f"projects/{mirror[1]}/data/samples.md" not in mirror[3].files()
    assert json.loads(mirror[0].get(saved.changes_key)[0])["mirror_rejected"] == ["data/samples.md"]


async def test_maintenance_reenqueues_old_saves(bridge, mirror, monkeypatch):
    store, pid, settings, _ = mirror
    bridge.settings = settings
    saved = save(mirror, "first")
    journal = json.loads(store.get(saved.changes_key)[0])
    journal["timestamp"] = "2020-01-01T00:00:00+00:00"
    store.put(saved.changes_key, json.dumps(journal).encode())
    enqueue = AsyncMock()
    monkeypatch.setattr("pistachio.agents.analysis.job_lifecycle.enqueue", enqueue)
    assert await knowledge_mirror.reconcile(bridge, store, now=time.time()) == 1
    assert f"/knowledge-mirror/{pid}/" in enqueue.call_args.args[2]


def test_token_never_reaches_worker_environment(bridge):

    bridge.settings.knowledge_github_repo = "lab/knowledge"
    bridge.settings.knowledge_github_token_secret = "sensitive-resource-name"
    job = job_spec(bridge.app, bridge.settings, "a" * 32)
    env = dict(job.task_groups[0].task_spec.runnables[0].environment.variables)
    assert "AGENT_KNOWLEDGE_ENABLED" not in env
    assert "sensitive-resource-name" not in str(env) and not any("GITHUB" in key for key in env)


async def test_restore_commit_round_trip(mirror):
    saved = save(mirror, "first")
    await run(mirror, saved.changes_key)
    store, pid, settings, github = mirror
    sha = github.head
    save(mirror, "changed", NOTE + b" bad edit")
    async with httpx.AsyncClient(transport=httpx.MockTransport(github.handle)) as client:
        restored = await knowledge_mirror.restore_commit(
            store, settings, pid, sha, client, actor="admin@example.com"
        )
    assert store.get(knowledge.prefix(pid) + "data/samples.md")[0] == NOTE
    assert restored.changed == ["data/samples.md"]


async def test_lost_ref_acknowledgement_does_not_duplicate_commit(mirror):
    saved = save(mirror, "first")
    github = mirror[3]
    failed = False

    def lose_ack(request):
        nonlocal failed
        response = github.handle(request)
        if request.method == "PATCH" and not failed:
            failed = True
            raise httpx.ReadTimeout("lost acknowledgement")
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(lose_ack)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await knowledge_mirror.mirror_through(
                mirror[0], mirror[2], mirror[1], saved.changes_key, client
            )
    assert len(github.messages) == 1
    await run(mirror, saved.changes_key)
    assert len(github.messages) == 2
    await run(mirror, saved.changes_key)
    assert len(github.messages) == 2
    assert json.loads(mirror[0].get(saved.changes_key)[0])["mirrored"]


async def test_undo_is_mirrored_as_a_restore(mirror):
    from pistachio.agents.analysis.access import Grant
    from pistachio.agents.analysis.commands import Command
    from pistachio.agents.analysis.knowledge_commands import run as command

    saved = save(mirror, "first")
    await run(mirror, saved.changes_key)
    deletion = save(mirror, "delete", None)
    await run(mirror, deletion.changes_key)
    store, pid, settings, github = mirror
    doc = Registry(store).load()
    grant = Grant("alice@example.com", True, {}, doc, pid, True, "T1:D1")
    _, restored = command(Command("undo knowledge"), grant, store, "undo", settings)
    await run(mirror, restored.changes_key)
    assert github.files()[f"projects/{pid}/data/samples.md"] == NOTE


async def test_turn_completion_queues_saved_attempt(bridge, mirror, monkeypatch):
    saved = save(mirror, "first")
    store, pid, settings, _ = mirror
    bridge.settings = settings
    store.put(
        "tasks/slack-analysis/events/first/attempt-a0.json",
        json.dumps({"knowledge": saved.model_dump()}).encode(),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr("pistachio.agents.analysis.job_lifecycle.enqueue", enqueue)
    await knowledge_mirror.after_event(bridge, store, "first", {"project_id": pid})
    assert enqueue.await_count == 1


async def test_restore_dry_run_does_not_write(mirror):
    saved = save(mirror, "first")
    await run(mirror, saved.changes_key)
    store, pid, settings, github = mirror
    save(mirror, "changed", NOTE + b" changed")
    before = store.get(knowledge.prefix(pid) + "data/samples.md")
    async with httpx.AsyncClient(transport=httpx.MockTransport(github.handle)) as client:
        restored = await knowledge_mirror.restore_commit(
            store, settings, pid, github.head, client, actor="admin@example.com", apply=False
        )
    assert restored.changed == ["data/samples.md"]
    assert store.get(knowledge.prefix(pid) + "data/samples.md") == before


async def test_save_notice_is_retryable_and_independent_of_mirror(bridge, monkeypatch):
    from test_analysis_batch import payload

    event = await bridge.accept(payload(text="Plot data"))
    await bridge.process(event, 0)
    record = await bridge.repository.get_connector_state("claude-event:" + event)
    saved = knowledge.Saved(added=["data/samples.md"])
    bridge.store.put(
        f"tasks/slack-analysis/events/{event}/attempt-a0.json",
        json.dumps({"attempt": 0, "knowledge": saved.model_dump()}).encode(),
    )
    post = AsyncMock(side_effect=[OSError("Slack unavailable"), None])
    monkeypatch.setattr(knowledge_mirror, "post", post)
    with pytest.raises(OSError):
        await knowledge_mirror.after_event(bridge, bridge.store, event, record)
    await knowledge_mirror.after_event(bridge, bridge.store, event, record)
    await knowledge_mirror.after_event(bridge, bridge.store, event, record)
    assert post.await_count == 2
    assert "1 data note" in post.call_args.args[3]


def test_path_secrets_rejected():
    with pytest.raises(ValueError, match="Secret"):
        knowledge.validate("data/sk-abcdefghijk.md", NOTE, Settings(_env_file=None))


async def test_first_mirror_includes_older_unchanged_files_without_future_results(mirror):
    store, pid, settings, github = mirror
    store.put(knowledge.prefix(pid) + "data/old.md", NOTE + b" older unchanged fact")
    first = save(mirror, "first")
    knowledge.commit(
        store,
        pid,
        {"results/future.md": NOTE + b" future result"},
        knowledge.load(store, pid, settings),
        settings,
        event_id="future",
        user_key="u",
    )
    await run(mirror, first.changes_key)
    assert f"projects/{pid}/data/old.md" in github.files()
    assert f"projects/{pid}/results/future.md" not in github.files()


async def test_completed_task_retry_does_not_fetch_credentials(bridge, mirror, monkeypatch):
    saved = save(mirror, "first")
    await run(mirror, saved.changes_key)
    store, pid, settings, _ = mirror
    bridge.settings = settings
    monkeypatch.setattr(knowledge_mirror, "token", lambda *args: pytest.fail("fetched credential"))
    stamp = saved.changes_key.split("/")[-2]
    assert await knowledge_mirror.process(bridge, store, pid, stamp) == 0
