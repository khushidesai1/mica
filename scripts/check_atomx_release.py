"""Offline release gate; synthetic credentials, no provider or data-bucket connection."""

import asyncio
import hashlib
import json
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from atomx_transfer import cli, worker

import mica
from mica.agents.analysis.atomx_connections import Connections
from mica.agents.analysis.config import Settings
from mica.config import Settings as AppSettings
from mica.storage.memory import InMemoryRepository


def check_layout_free_job():
    """Run the installed CLI, mapping and inventory engine without network or a layout file."""
    run = "a" * 32
    destinations = {
        "bucket": "fixture-bucket",
        "raw_prefix": "project/raw/",
        "additional_prefix": "project/raw/.additional_files/",
        "status_prefix": "project/raw/.mica-transfers/",
    }
    request = {
        "run_id": run,
        "bucket": "fixture-bucket",
        "sample": "SAMPLE",
        "as_sample": "",
        "export_root": "/Export",
        "destinations": destinations,
        "requested_by": "fixture@example.com",
        "connection_revision": "fixture-revision",
    }
    bootstrap = {
        **request,
        "kind": "inventory",
        "plan_fingerprint": None,
        "sftp": {},
        "login": {"username": "fixture", "password": "never-publish-password"},
    }
    values = {f"tasks/transfers/{run}/inventory-request.json": request}

    def write(name, value, generation):
        values[name] = deepcopy(value)
        return "1"

    artifact = SimpleNamespace(
        read=lambda name: (deepcopy(values[name]), "1") if name in values else None, write=write
    )
    data = SimpleNamespace(read=lambda _: None, list=lambda _: [])
    remote = SimpleNamespace(
        dirs=lambda path: ["SAMPLE"] if path.endswith("DecodedFiles") else ["20260101_120000_S1"],
        files=lambda path: iter([("cells.csv", 5)]) if "/flatFiles/" in path else iter([]),
        close=lambda: None,
    )
    with (
        patch.object(cli, "GCS", return_value=artifact),
        patch.object(worker, "GCS", return_value=data),
        patch.object(
            worker, "Control", return_value=SimpleNamespace(call=lambda _: deepcopy(bootstrap))
        ),
        patch.object(cli, "SFTP", return_value=remote),
    ):
        uri = f"gs://fixture-artifacts/tasks/transfers/{run}/inventory-request.json"
        assert cli.run_job(uri) == 0
        result = values[f"tasks/transfers/{run}/inventory-result.json"]
        assert result["inventory"]["samples"][0]["n_files"] == 1
        assert worker.MappedGCS(destinations).map("transfer_status/SAMPLE.done.json") == (
            "project/raw/.mica-transfers/SAMPLE.done.json"
        )
        request["bucket"] = "wrong-bucket"
        assert cli.run_job(uri) == 1
        result = values[f"tasks/transfers/{run}/inventory-result.json"]
        assert result["step"] == "config" and result["error_class"] == "ValueError"
        assert "never-publish-password" not in json.dumps(values)
    print("ATOMX_LAYOUT_FREE_JOB_AND_BUCKET_FENCE_VERIFIED (offline)")


async def check():
    check_layout_free_job()
    service = Connections(
        AppSettings(
            _env_file=None,
            signed_link_secret="synthetic-image-gate-key-0123456789",
            control_plane_url="https://fixture.invalid",
        ),
        Settings(_env_file=None),
        InMemoryRepository(),
    )
    value = await service.save("fixture@example.com", "", "fixture-user", "fixture-password")
    assert (await service.decrypt("fixture@example.com", value["revision"]))[
        "password"
    ] == "fixture-password"
    url = await service.issue("fixture@example.com", "connection", "fixture")
    token = await service.exchange(url.split("#")[1])
    assert (await service.session(token))["owner"] == "fixture@example.com"
    await service.forget("fixture@example.com", value["revision"])
    try:
        await service.session(token)
    except ValueError:
        pass
    else:
        raise AssertionError("Forgotten connection retained a session")
    page = Path(mica.__file__).with_name("api") / "atomx_form.html"
    assert 'type="password"' in page.read_text() and "location.hash" in page.read_text()
    if sys.argv[-1] == "api":
        from mica.api.atomx import PAGE

        assert page.read_text() == PAGE
    source = Path("/workspace/tools/atomx_transfer")
    installed = Path(cli.__file__).parent
    for path in [*source.glob("*.py"), *source.glob("scripts/*.sh")]:
        assert (
            hashlib.sha256(path.read_bytes()).digest()
            == hashlib.sha256((installed / path.relative_to(source)).read_bytes()).digest()
        ), path
    script = (installed / "scripts/download_manifest.sh").read_text()
    assert "StrictHostKeyChecking=yes" in script and "sshpass -d" in script
    assert worker.ScriptSFTP.quote("/plain file") == '"/plain file"'
    if sys.argv[-1] == "worker":
        assert shutil.which("sshpass") and shutil.which("sftp")
    print("ATOMX_PRIVATE_CONNECTION_AND_WORKER_TOOL_VERIFIED (offline)")


if __name__ == "__main__":
    asyncio.run(check())
