import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from mica.agents.analysis import atomx_worker, transfer_destinations
from mica.agents.analysis.atomx_connections import Connections, digest
from mica.agents.analysis.config import Settings
from mica.agents.analysis.registry import RegistryDoc
from mica.agents.analysis.transfer_config import TransferRequest
from mica.agents.analysis.transfer_jobs import job_spec
from mica.api import atomx
from mica.config import Settings as AppSettings
from mica.storage.memory import InMemoryRepository
from tools.atomx_transfer.worker import ScriptSFTP

A, B = "alice@example.com", "bob@example.com"
KEY = "synthetic-application-key-only-for-tests-0123456789"


@pytest.fixture
def connections():
    app = AppSettings(_env_file=None, signed_link_secret=KEY, control_plane_url="https://mica.test")
    return Connections(app, Settings(_env_file=None), InMemoryRepository())


async def test_encrypted_connections_are_owner_and_revision_bound(connections):
    first = await connections.save(A, "", "alice-login", "private-test-password")
    serialized = json.dumps(connections.repo.connector_state)
    assert "private-test-password" not in serialized and "alice-login" not in serialized
    assert await connections.status(B) == dict.fromkeys(
        ("revision", "status", "updated_at", "last_result")
    )
    assert (await connections.decrypt(A, first["revision"]))["username"] == "alice-login"
    with pytest.raises(ValueError):
        await connections.decrypt(B, first["revision"])
    second = await connections.save(A, first["revision"], "replacement", "replacement-secret")
    with pytest.raises(ValueError):
        await connections.decrypt(A, first["revision"])
    with pytest.raises(ValueError):
        await connections.save(A, first["revision"], "stale", "stale")
    await connections.result(A, first["revision"], "auth_failed")
    assert (await connections.status(A))["status"] == "saved"
    await connections.forget(A, second["revision"])
    assert "ciphertext" not in await connections.get(A)
    with pytest.raises(ValueError):
        await connections.decrypt(A, second["revision"])


async def test_envelope_tampering_and_key_rotation_fail_closed(connections):
    saved = await connections.save(A, "", "login", "password")
    copied = dict(saved, owner=B)
    await connections.repo.set_connector_state(connections.record_key(B), copied)
    with pytest.raises(ValueError, match="Reconnect"):
        await connections.decrypt(B, saved["revision"])
    link = await connections.issue(A, "connection", "rotation-test")
    connections.app.signed_link_secret = KEY + "rotated"
    with pytest.raises(ValueError, match="Reconnect"):
        await connections.decrypt(A, saved["revision"])
    with pytest.raises(ValueError):
        await connections.exchange(link.split("#")[1])


async def test_links_single_use_expiry_and_revision_revocation(connections):
    link = await connections.issue(A, "connection", "event")
    token = link.split("#")[1]
    assert token not in json.dumps(connections.repo.connector_state)
    results = await asyncio.gather(
        connections.exchange(token), connections.exchange(token), return_exceptions=True
    )
    assert sum(isinstance(value, str) for value in results) == 1
    session = next(value for value in results if isinstance(value, str))
    assert (await connections.session(session))["owner"] == A
    await connections.save(A, "", "username", "password")
    with pytest.raises(ValueError, match="changed"):
        await connections.session(session)
    expired = await connections.issue(A, "connection", "expired")
    key = "atomx-link:" + expired.split("#")[1].split(".")[0]
    value = await connections.repo.get_connector_state(key)
    value["expires"] = time.time() - 1
    await connections.repo.set_connector_state(key, value)
    with pytest.raises(ValueError, match="expired"):
        await connections.exchange(expired.split("#")[1])


async def test_browser_scanner_safe_csrf_and_no_password_echo(connections, monkeypatch):
    app = FastAPI()
    app.include_router(atomx.router)
    monkeypatch.setattr(atomx, "service", lambda request: (SimpleNamespace(), connections))
    link = await connections.issue(A, "connection", "browser")
    before = json.dumps(connections.repo.connector_state, sort_keys=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mica.test"
    ) as client:
        page = await client.get("/integrations/atomx")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert 'type="password"' in page.text and 'href="http' not in page.text
        assert json.dumps(connections.repo.connector_state, sort_keys=True) == before
        payload = {"token": link.split("#")[1]}
        assert (await client.post("/integrations/atomx/exchange", json=payload)).status_code == 400
        headers = {"Origin": "https://mica.test"}
        opened = await client.post("/integrations/atomx/exchange", json=payload, headers=headers)
        assert opened.status_code == 200
        cookie = opened.headers["set-cookie"].lower()
        assert all(part in cookie for part in ("secure", "httponly", "samesite=strict"))
        session = (await client.get("/integrations/atomx/session")).json()
        action = {"action": "save", "username": "test-login", "password": "secret-in-form"}
        assert (
            await client.post("/integrations/atomx/action", json=action, headers=headers)
        ).status_code == 400
        headers["X-CSRF-Token"] = session["csrf"]
        saved = await client.post("/integrations/atomx/action", json=action, headers=headers)
        assert saved.status_code == 200 and "secret-in-form" not in saved.text
        assert (
            await client.post("/integrations/atomx/action", json=action, headers=headers)
        ).status_code == 400
        assert "secret-in-form" not in json.dumps(connections.repo.connector_state)


async def test_worker_grant_requires_current_owner_revision_and_active_run(connections):
    saved = await connections.save(A, "", "login", "password")
    run, token = "a" * 32, "b" * 64
    pending = {
        "job_run": run,
        "state": "running",
        "requested_by": A,
        "login_version": saved["revision"],
        "job_kind": "pull",
        "confirmed_by": A,
        "project": "project",
    }
    service = SimpleNamespace(
        repo=connections.repo,
        connections=connections,
        pending=AsyncMock(return_value=pending),
        authorize=lambda *a: None,
    )
    await connections.repo.set_connector_state(
        "atomx-run:" + run,
        {
            "pending": "pending",
            "owner": A,
            "revision": saved["revision"],
            "kind": "pull",
            "expires": time.time() + 60,
            "token_hash": digest(token),
        },
    )
    assert await atomx_worker.authorize(service, run, token) == pending
    for other_run, other_token in (("c" * 32, token), (run, "c" * 64), (run, "")):
        with pytest.raises(ValueError):
            await atomx_worker.authorize(service, other_run, other_token)
    pending["state"] = "cancelling"
    with pytest.raises(ValueError):
        await atomx_worker.authorize(service, run, token)
    pending["state"] = "running"
    await connections.forget(A, saved["revision"])
    with pytest.raises(ValueError, match="changed"):
        await atomx_worker.authorize(service, run, token)


def test_destination_precedence_ambiguity_and_project_authorization():
    doc = RegistryDoc()
    pid = doc.create(A, "project", "gs://lab-data/project/processed")
    doc.share(pid, A, B)
    guide = "Raw: gs://lab-data/project/raw\nAdditional: gs://lab-data/project/additional_files"
    request = TransferRequest(sample="sample")
    result = transfer_destinations.resolve(request, guide, "transfer sample", doc, pid, A)
    assert result.raw_prefix == "project/raw/" and result.layout_source == "project_guide"
    result = transfer_destinations.resolve(
        request, guide, "to gs://lab-data/import/raw", doc, pid, A
    )
    assert result.raw_prefix == "import/raw/" and result.layout_source == "request"
    with pytest.raises(ValueError, match="unambiguous"):
        transfer_destinations.resolve(
            request, "Processed: gs://lab-data/project/processed", "transfer", doc, pid, A
        )
    with pytest.raises(ValueError, match="linked bucket"):
        transfer_destinations.resolve(request, guide, "to gs://other-data/raw", doc, pid, A)
    with pytest.raises(ValueError, match="owner"):
        transfer_destinations.resolve(request, guide, "transfer", doc, pid, B)
    with pytest.raises(ValueError, match="exact raw"):
        transfer_destinations.resolve(
            request.model_copy(update={"destination": "gs://lab-data/invented"}),
            guide,
            "transfer",
            doc,
            pid,
            A,
        )


def test_worker_job_uses_existing_worker_without_analysis_or_password(connections):
    app = connections.app.model_copy(
        update={
            "slack_agent_image": "worker@sha256:" + "a" * 64,
            "batch_service_account": "worker@example.com",
        }
    )
    job = job_spec(
        app,
        connections.settings,
        "a" * 32,
        "pull",
        grant_token="b" * 64,
        source_bytes=80_000_000_000,
    )
    runnable = job.task_groups[0].task_spec.runnables[0]
    assert list(runnable.container.commands)[:3] == ["-m", "atomx_transfer.cli", "job"]
    assert job.allocation_policy.service_account.email == "worker@example.com"
    assert job.allocation_policy.instances[0].policy.boot_disk.size_gb >= 132
    assert set(runnable.environment.variables) == {"MICA_ATOMX_GRANT", "MICA_ATOMX_CONTROL_URL"}
    with pytest.raises(ValueError, match="disk limit"):
        job_spec(app, connections.settings, "a" * 32, "pull", source_bytes=3_000_000_000_000)


@pytest.mark.parametrize("path", ["/file\n!evil", "/wild*", '/quote"', "/bad\\name"])
def test_script_manifest_rejects_sftp_command_injection(path):
    with pytest.raises(ValueError):
        ScriptSFTP.quote(path)


async def test_transfer_flow_keeps_login_out_of_artifacts_and_freezes_destinations(
    connections, tmp_path, monkeypatch
):
    from mica.agents.analysis import project_memory, transfers
    from mica.agents.analysis.access import Grant
    from mica.agents.analysis.registry import Registry
    from mica.agents.analysis.storage import LocalStorage

    store = LocalStorage(tmp_path)
    registry = Registry(store)
    pid = registry.update(
        "create", A, "create", lambda d: d.create(A, "test", "gs://lab-data/processed")
    )
    doc = registry.load()
    grant = Grant(A, True, {}, doc, pid, True, "EMAIL:thread")
    connections.app.slack_agent_image = "worker@sha256:" + "a" * 64
    connections.app.batch_service_account = "worker@example.com"
    settings = Settings(
        _env_file=None,
        transfers_enabled=True,
        transfer_host_key="ssh-rsa fake",
        write_service_account="writer@example.com",
        write_allowed_prefixes="gs://lab-data/",
    )
    bridge = SimpleNamespace(app=connections.app, settings=settings, repository=connections.repo)
    service = transfers.Transfers(bridge, store)
    service.schedule = AsyncMock()
    service.notify = AsyncMock()
    monkeypatch.setattr(transfers, "GCSStorage", lambda bucket: store)
    monkeypatch.setattr(project_memory.GitHubMemory, "read", AsyncMock(return_value=None))
    client = SimpleNamespace(create_job=lambda **kwargs: None)
    monkeypatch.setattr(transfers.batch_v1, "BatchServiceClient", lambda: client)
    saved = await service.connections.save(A, "", "private-login", "private-password")
    record = {
        "payload": {
            "team_id": "EMAIL",
            "event": {
                "user": "user",
                "channel": "D1",
                "ts": "1",
                "text": "Transfer sample to gs://lab-data/import/raw",
            },
        }
    }
    eid = "1" * 32
    await service.request(grant, record, eid, TransferRequest(sample="sample"))
    pending = await service.pending(eid)
    await service.advance(pending)
    pending = await service.pending(eid)
    assert pending["state"] == "inventory"
    request = json.loads(
        store.get(f"tasks/transfers/{pending['job_run']}/inventory-request.json")[0]
    )
    assert "private-login" not in json.dumps(request) and "private-password" not in json.dumps(
        request
    )
    assert "token_hash" not in request and "grant_token" not in request
    assert request["connection_revision"] == saved["revision"]
    # The real controller artifact and API bootstrap must be accepted by the installed job
    # entrypoint. A controller-only test previously missed the obsolete worker layout check.
    from copy import deepcopy

    from test_atomx_engine import MemoryBucket, MemoryRemote

    from tools.atomx_transfer import cli, worker

    monkeypatch.setattr(atomx, "worker", AsyncMock(return_value=(service, pending)))
    bootstrap = json.loads((await atomx.bootstrap(None, pending["job_run"])).body)
    artifact, data, remote = MemoryBucket(), MemoryBucket(), MemoryRemote()
    remote.data = {key.replace("tma37", "sample"): value for key, value in remote.data.items()}
    remote.close = lambda: None
    prefix = f"tasks/transfers/{pending['job_run']}/inventory"
    artifact.put(prefix + "-request.json", json.dumps(request).encode())
    monkeypatch.setattr(cli, "GCS", lambda _: artifact)
    monkeypatch.setattr(worker, "GCS", lambda *_: data)
    monkeypatch.setattr(cli, "SFTP", lambda *_: remote)
    monkeypatch.setattr(
        worker, "Control", lambda _: SimpleNamespace(call=lambda _: deepcopy(bootstrap))
    )
    assert "layout" not in request
    assert cli.run_job("gs://artifacts/" + prefix + "-request.json") == 0
    result = artifact.read(prefix + "-result.json")[0]
    item = result["inventory"]["samples"][0]
    assert item["n_files"] == 4
    await service.preview(pending, {"samples": [item]})
    preview = await service.pending(eid)
    assert "gs://lab-data/import/raw/sample/" in preview["preview_text"]
    with pytest.raises(ValueError, match="membership"):
        await service.confirm(eid, B)
    await service.confirm(eid, A)
    assert (await service.pending(eid))["state"] == "submitting"
    await service.connections.save(A, saved["revision"], "new", "new-password")
    with pytest.raises(ValueError, match="changed"):
        await service.submit(await service.pending(eid), "pull")


def test_script_staging_uses_private_fd_pinned_host_and_download_only(tmp_path, monkeypatch):
    import os
    import tempfile
    from pathlib import Path

    from tools.atomx_transfer import worker
    from tools.atomx_transfer.core import File

    remote = object.__new__(ScriptSFTP)
    remote.config = {"host": "example.com", "port": 22, "host_key": "ssh-rsa fixture"}
    remote.login = {"username": "fixture-login", "password": "fixture-password"}
    remote.temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    remote.local = {}
    remote.size = lambda path: 3
    files = [File("/source/file.txt", "raw_outputs/raw/sample/file.txt", 3)]
    calls = []

    def popen(args, **kwargs):
        assert args[-2] == "--download-only"
        assert "fixture-password" not in json.dumps([args, kwargs["env"]])
        assert os.read(kwargs["pass_fds"][0], 100) == b"fixture-password\n"
        assert (
            Path(kwargs["env"]["ATOMX_KNOWN_HOSTS"]).read_text() == "example.com ssh-rsa fixture\n"
        )
        manifest = Path(args[-1]).read_text()
        assert 'get "/source/file.txt"' in manifest
        (Path(remote.temporary.name) / "0").write_bytes(b"abc")
        calls.append(args)
        return SimpleNamespace(wait=lambda **kwargs: 0, poll=lambda: 0)

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    remote.stage(files, lambda: None)
    with remote.open("/source/file.txt") as stream:
        assert stream.read() == b"abc"
    assert len(calls) == 1
    remote.temporary.cleanup()
