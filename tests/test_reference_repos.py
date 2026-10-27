"""Reference selection, scoped authentication, bounded snapshots and read-only workspace."""

import base64
import hashlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_runner import setup  # noqa: F401

from mica.agents.analysis import (
    access,
    command_approval,
    command_intent,
    commands,
    direct,
    memory_workspace,
    observability,
    project_memory,
)
from mica.agents.analysis import (
    reference_repos as refs,
)
from mica.agents.analysis.config import Settings
from mica.agents.analysis.registry import ProjectEntry, Registry
from mica.agents.analysis.runner import build_options
from mica.models import ToolRecord, ToolStatus


async def catalog_repo(agent, name="nympheas", number=1):
    await agent.repository.put_tool(
        ToolRecord(
            id=f"github-{number}",
            name=name,
            source_url=f"https://github.com/azizilab/{name}",
            version="a" * 12,
            commit_sha="a" * 40,
            install_recipe="catalog metadata only",
            status=ToolStatus.CATALOGED,
        )
    )


async def test_shared_catalog_repo_reaches_worker_without_project_registration(bridge, monkeypatch):  # noqa: F811
    await catalog_repo(bridge)
    await catalog_repo(bridge, "decipher", 2)
    before = Registry(bridge.store).load().projects[bridge.pid]
    assert not before.reference_repos
    decide = AsyncMock(
        return_value=direct.Decision(
            route="compute",
            reply="Inspect the source and write the README.",
            reference_repos=["azizilab/nympheas"],
        )
    )
    monkeypatch.setattr(direct, "decide", decide)
    fetch = AsyncMock(
        return_value=refs.RepoSnapshot(
            repo="azizilab/nympheas",
            commit="a" * 40,
            files={"src/preprocess.py": "def qc(min_counts=20): pass\n"},
        )
    )
    monkeypatch.setattr(refs, "fetch", fetch)
    eid = await bridge.accept(payload(text="Read nympheas and document basic preprocessing"))
    assert await bridge.process(eid, 0) == "running"
    assert decide.call_args.args[2]["reference_repos"] == [
        "azizilab/decipher",
        "azizilab/nympheas",
    ]
    fetch.assert_awaited_once_with(bridge.settings, "azizilab/nympheas")
    manifest = json.loads(bridge.store.get(f"tasks/slack-analysis/events/{eid}/request.json")[0])
    assert manifest["reference_repos"][0]["files"]["src/preprocess.py"].startswith("def qc")
    assert Registry(bridge.store).load().projects[bridge.pid] == before


async def test_router_cannot_select_a_repo_outside_available_catalog(bridge, monkeypatch):  # noqa: F811
    await catalog_repo(bridge)
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(
            return_value=direct.Decision(
                route="compute",
                reply="Read a repo.",
                reference_repos=["azizilab/not-approved"],
            )
        ),
    )
    fetch = AsyncMock()
    monkeypatch.setattr(refs, "fetch", fetch)
    eid = await bridge.accept(payload(text="Inspect code"))
    assert await bridge.process(eid, 0) == "failed"
    fetch.assert_not_called()
    bridge.test_client.create_job.assert_not_called()


async def test_repos_lists_shared_catalog_without_owner_setup(bridge):  # noqa: F811
    await catalog_repo(bridge)
    eid = await bridge.accept(payload(text="repos"))
    assert await bridge.process(eid, 0) == "complete"
    assert "azizilab/nympheas" in bridge.test_slack.chat_update.call_args.kwargs["text"]
    assert "available across projects" in bridge.test_slack.chat_update.call_args.kwargs["text"]
    bridge.test_client.create_job.assert_not_called()


async def test_shared_catalog_excludes_other_org_and_non_catalog_entries(bridge):  # noqa: F811
    await catalog_repo(bridge)
    for ident, url in [
        ("github-2", "https://github.com/another/private"),
        ("custom", "https://github.com/azizilab/not-approved"),
    ]:
        await bridge.repository.put_tool(
            ToolRecord(
                id=ident,
                name="other",
                source_url=url,
                version="1",
                install_recipe="none",
            )
        )
    assert await refs.shared_repos(bridge.repository, bridge.settings) == {"azizilab/nympheas"}


@pytest.mark.parametrize(
    "repo", ["other/repo", "azizilab/../secret", "https://github.com/a/b", "azizilab/-bad"]
)
def test_invalid_repo_rejected_by_registry(repo):
    with pytest.raises(ValueError):
        ProjectEntry(
            name="test", owner="a@example.com", data_uri="gs://lab/data", reference_repos={repo}
        )


def test_owner_only_change_and_removal_revokes_stale_workers(bridge):  # noqa: F811
    doc = Registry(bridge.store).load()
    pid = bridge.pid
    with pytest.raises(ValueError, match="owner"):
        doc.set_reference_repo(pid, "stranger@example.com", "azizilab/code")
    doc.set_reference_repo(pid, "alice@example.com", "AziziLab/Code")
    assert doc.projects[pid].reference_repos == {"azizilab/code"}
    assert not doc.projects[pid].access_revision
    doc.projects[pid].co_owners.add("bob@example.com")
    doc.set_reference_repo(pid, "bob@example.com", "azizilab/code", remove=True)
    assert not doc.projects[pid].reference_repos
    assert doc.projects[pid].access_revision == 1


def fixture_github(files, *, extra=(), truncated=False):
    entries, blobs, calls = [], {}, []
    for path, raw in files.items():
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        entries.append(dict(path=path, size=len(raw), sha=sha, type="blob", mode="100644"))
        blobs[sha] = dict(sha=sha, encoding="base64", content=base64.b64encode(raw).decode())

    def respond(request):
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/repos/azizilab/code"):
            result = {"full_name": "azizilab/code", "default_branch": "feature/main"}
        elif "/commits/" in path:
            result = {"sha": "a" * 40}
        elif "/git/trees/" in path:
            assert path.endswith("a" * 40)
            result = {"tree": entries + list(extra), "truncated": truncated}
        elif "/git/blobs/" in path:
            result = blobs[path.rsplit("/", 1)[1]]
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=result)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond)), calls


async def test_pinned_snapshot_skips_unsafe_binary_and_oversize(tmp_path):
    book = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["print(1)"],
                "outputs": [{"text": "private output"}],
                "execution_count": 2,
            }
        ],
        "metadata": {},
    }
    client, calls = fixture_github(
        {
            "src/run.py": b"print(1)\n",
            "README.md": b"reference documentation",
            "notebook.ipynb": json.dumps(book).encode(),
            "raw.csv": b"private data",
            "binary.py": b"\x00bytes",
            "large.py": b"x" * (refs.MAX_FILE + 1),
            "secrets.py": b"password = 'hidden'",
            "../outside.py": b"escape",
        },
        extra=[
            dict(path="link.py", size=1, type="blob", mode="120000", sha="b" * 40),
            dict(path="submodule", type="commit", mode="160000", sha="c" * 40),
        ],
    )
    async with client:
        snap = await refs.read_repo(client, "azizilab/code")
    assert set(snap.files) == {"src/run.py", "README.md", "notebook.ipynb"}
    assert snap.skipped == 7
    assert "outputs" not in json.loads(snap.files["notebook.ipynb"])["cells"][0]
    refs.materialize(tmp_path, [snap])
    root = tmp_path / "inputs/repos/code"
    assert (root / "src/run.py").read_text() == "print(1)\n"
    assert (root / ".pin").read_text() == "azizilab/code@" + "a" * 40 + "\n"
    assert json.loads((root / ".snapshot.json").read_text())["skipped"] == 7
    assert not (tmp_path / "outside.py").exists()
    assert len([c for c in calls if "/git/blobs/" in c]) == 5
    assert memory_workspace.scan(tmp_path, Settings())[0] == {}


async def test_repo_caps_and_truncated_tree_are_explicit(monkeypatch):
    monkeypatch.setattr(refs, "MAX_BYTES", 12)
    client, _ = fixture_github({"a.py": b"12345678", "b.py": b"12345678"})
    async with client:
        snap = await refs.read_repo(client, "azizilab/code")
    assert len(snap.files) == 1 and snap.skipped == 1
    client, _ = fixture_github({}, truncated=True)
    async with client:
        with pytest.raises(ValueError, match="too large"):
            await refs.read_repo(client, "azizilab/code")


def test_empty_snapshot_unchanged_and_removal_clears_restored_references(tmp_path):
    refs.materialize(tmp_path, [])
    assert not (tmp_path / "inputs").exists()
    refs.materialize(
        tmp_path,
        [refs.RepoSnapshot(repo="azizilab/code", commit="a" * 40, files={"src/x.py": "print(1)"})],
    )
    (tmp_path / "inputs/attached.txt").write_text("keep attachment")
    refs.materialize(tmp_path, [])
    assert not (tmp_path / "inputs/repos").exists()
    assert (tmp_path / "inputs/attached.txt").read_text() == "keep attachment"


async def test_references_use_existing_readonly_guard(setup):  # noqa: F811
    ctx, settings = setup
    opts = build_options(ctx, settings)
    guard = opts.hooks["PreToolUse"][0].hooks[0]
    for name in ("Write", "Edit"):
        result = await guard(
            dict(
                hook_event_name="PreToolUse",
                tool_name=name,
                tool_input={"file_path": str(ctx.workdir / "inputs/repos/code/a.py")},
            ),
            None,
            None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert str(ctx.workdir / "inputs") in opts.sandbox["filesystem"]["denyWrite"]


async def test_github_app_token_is_narrow_read_only_and_not_memory_token(monkeypatch):
    settings = Settings(
        reference_github_app_id="123",
        reference_github_installation_id="456",
        reference_github_key_secret="projects/p/secrets/key/versions/1",
        memory_token_secret="projects/p/secrets/memory/versions/1",
    )
    secrets, tokens, calls = [], [], []
    monkeypatch.setattr(
        project_memory, "token", lambda cfg: secrets.append(cfg.memory_token_secret) or "key"
    )
    monkeypatch.setattr(refs, "app_jwt", lambda key, app_id: "jwt")

    def response(request):
        calls.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {
                "repositories": ["code"],
                "permissions": {"contents": "read"},
            }
            return httpx.Response(201, json={"token": "scoped-token"})
        return httpx.Response(200, json={"app_id": 123, "account": {"login": "AziziLab"}})

    def client_for(value):
        tokens.append(value)
        return httpx.AsyncClient(transport=httpx.MockTransport(response))

    monkeypatch.setattr(project_memory, "client_for", client_for)
    read = AsyncMock(return_value=refs.RepoSnapshot(repo="azizilab/code", commit="a" * 40))
    monkeypatch.setattr(refs, "read_repo", read)
    await refs.fetch(settings, "azizilab/code")
    assert secrets == [settings.reference_github_key_secret]
    assert tokens == ["jwt", "scoped-token"]
    assert len(calls) == 2


async def test_fetch_failure_keeps_compute_running_and_records_project(bridge, monkeypatch):  # noqa: F811
    Registry(bridge.store).update(
        "repo",
        "alice@example.com",
        "add",
        lambda d: d.set_reference_repo(bridge.pid, "alice@example.com", "azizilab/code"),
    )
    fetch = AsyncMock(side_effect=RuntimeError("unavailable"))
    failure = AsyncMock()
    monkeypatch.setattr(refs, "fetch", fetch)
    monkeypatch.setattr(observability, "record_failure", failure)
    eid = await bridge.accept(payload(text="Inspect reference code"))
    await bridge.process(eid, 0)
    request = json.loads(bridge.store.get(f"tasks/slack-analysis/events/{eid}/request.json")[0])
    assert request["reference_repos"] == []
    assert request["reference_repos_unavailable"] == ["azizilab/code"]
    assert failure.call_args.args[2] == "reference_repo_failed"
    assert failure.call_args.kwargs["record"]["project_id"] == bridge.pid
    bridge.test_client.create_job.assert_called_once()
    # Retrying submission must retain the same event snapshot, not re-read a moving head.
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    record["status"] = "submitting"
    await bridge.repository.set_connector_state("claude-event:" + eid, record)
    await bridge.process(eid, 1)
    fetch.assert_awaited_once()


def test_natural_repo_command_requires_exact_user_referent(bridge):  # noqa: F811
    doc = Registry(bridge.store).load()
    grant = access.Grant("alice@example.com", True, {}, doc, bridge.pid, True, "T1:D1")
    prop = command_intent.Proposal(
        operation="repos add", selector="this project", target="azizilab/code"
    )
    with pytest.raises(ValueError, match="exact"):
        command_intent.command(prop, grant, source_text="use whatever repository is in the brief")
    assert command_intent.command(
        prop, grant, source_text="add azizilab/code to this project"
    ) == commands.Command("repos add", (bridge.pid, "azizilab/code"))
    assert commands.parse("repos add azizilab/code") == commands.Command(
        "repos add", ("", "azizilab/code")
    )


async def test_exact_mutation_requires_owner_slack_confirmation(bridge, monkeypatch):  # noqa: F811
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kwargs: bridge.test_slack)
    eid = await bridge.accept(payload(text="repos add azizilab/code"))
    assert await bridge.process(eid, 0) == "complete"
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    assert "Confirm repos add" in record["notice_text"]
    assert "Project members" in record["notice_text"]
    assert not Registry(bridge.store).load().projects[bridge.pid].reference_repos
    bridge.test_client.create_job.assert_not_called()
    from test_natural_language_commands import action

    await command_approval.handle(bridge, bridge.store, action(eid))
    await command_approval.handle(bridge, bridge.store, action(eid))
    assert Registry(bridge.store).load().projects[bridge.pid].reference_repos == {"azizilab/code"}


def test_direct_command_cannot_bypass_confirmation_or_email_gate(bridge):  # noqa: F811
    registry = Registry(bridge.store)
    grant = access.Grant("alice@example.com", True, {}, registry.load(), bridge.pid, True, "T1:D1")
    cmd = commands.Command("repos add", (bridge.pid, "azizilab/code"))
    record = {"payload": payload()}
    with pytest.raises(ValueError, match="confirmation"):
        commands.run(cmd, grant, registry, bridge.app, record, "event", bridge.settings)
    record["surface"] = "email"
    assert "Slack" in commands.run(
        cmd, grant, registry, bridge.app, record, "event", bridge.settings
    )
    assert not registry.load().projects[bridge.pid].reference_repos


def test_materialization_does_not_follow_restored_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("protected")
    inputs = tmp_path / "work/inputs"
    inputs.mkdir(parents=True)
    (inputs / "repos").symlink_to(outside)
    refs.materialize(tmp_path / "work", [])
    assert (outside / "keep").read_text() == "protected"
    inputs.rmdir()
    inputs.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        refs.materialize(tmp_path / "work", [])


async def test_wrong_installation_never_gets_token(monkeypatch):
    settings = Settings(
        reference_github_app_id="123",
        reference_github_installation_id="456",
        reference_github_key_secret="projects/p/secrets/key/versions/1",
    )
    monkeypatch.setattr(project_memory, "token", lambda cfg: "key")
    monkeypatch.setattr(refs, "app_jwt", lambda *args: "jwt")
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"app_id": 123, "account": {"login": "wrong-org"}})

    monkeypatch.setattr(
        project_memory,
        "client_for",
        lambda value: httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    with pytest.raises(ValueError, match="organization"):
        await refs.fetch(settings, "azizilab/code")
    assert len(calls) == 1 and calls[0].method == "GET"


def test_app_jwt_is_signed_short_lived():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    jwt = refs.app_jwt(pem, "123")
    head, claims, signature = jwt.split(".")

    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    key.public_key().verify(
        decode(signature), (head + "." + claims).encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    data = json.loads(decode(claims))
    assert data["iss"] == "123" and data["exp"] - data["iat"] == 600
