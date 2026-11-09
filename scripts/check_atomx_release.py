"""Offline release gate; synthetic credentials, no provider or data-bucket connection."""

import asyncio
import hashlib
import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
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


def check_worker_ssh():
    """Exercise OpenSSH initialization under the actual image user without connecting."""
    assert os.getuid() == os.getgid() == 65532, "Unexpected worker UID/GID"
    entry = pwd.getpwuid(os.getuid())
    assert entry.pw_gid == 65532 and entry.pw_dir == "/srv/pistachio"
    assert entry.pw_shell == "/usr/sbin/nologin"
    assert shutil.which("sshpass") and shutil.which("sftp")
    result = subprocess.run(
        ["ssh", "-G", "-F", "/dev/null", "-o", "User=synthetic", "localhost"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    assert "user synthetic" in result.stdout.splitlines()
    print("ATOMX_WORKER_ACCOUNT_AND_OPENSSH_VERIFIED (offline)")


def check_sftp_password_mode(scripts: Path):
    """Inspect the real scripts' effective OpenSSH options without opening a connection."""
    ssh = shutil.which("ssh")
    assert ssh and shutil.which("sftp"), "OpenSSH is required"
    with tempfile.TemporaryDirectory(prefix="mica-sftp-check-") as directory:
        root = Path(directory)
        config = root / "ssh-config"
        probe = root / "ssh-probe"
        probe.write_text(
            "#!/bin/sh\n"
            f"{shlex.quote(ssh)} -G \"$@\" > {shlex.quote(str(config))} || exit 2\n"
            # sftp expects its binary protocol; stop after obtaining configuration.
            "exit 1\n"
        )
        helper = root / "sshpass"
        helper.write_text(
            "#!/bin/sh\n"
            # Intercept only the password helper; run the actual sftp and ssh binaries.
            "test \"$1\" = -d || exit 2\nshift 2\n"
            "client=$1\nshift\n"
            f"exec \"$client\" -S {shlex.quote(str(probe))} \"$@\"\n"
        )
        probe.chmod(0o700)
        helper.chmod(0o700)
        batch = root / "download.batch"
        batch.write_text("bye\n")
        known = root / "known_hosts"
        known.write_text("")
        env = {
            "PATH": str(root) + os.pathsep + os.environ["PATH"],
            "ATOMX_PASSWORD_FD": "0",
            "ATOMX_KNOWN_HOSTS": str(known),
            "ATOMX_SFTP_HOST": "fixture.invalid",
            "ATOMX_SFTP_PORT": "22",
            "ATOMX_SFTP_USER": "synthetic",
        }
        for script in ("get_tma.sh", "get_tma_addfiles.sh"):
            config.unlink(missing_ok=True)
            result = subprocess.run(
                ["bash", str(scripts / script), "--download-only", str(batch)],
                env=env, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode != 0, "Configuration probe must not download files"
            options = dict(line.split(" ", 1) for line in config.read_text().splitlines())
            assert options["batchmode"] == "no", "Password authentication disabled by batch mode"
            assert options["user"] == "synthetic"
            assert options["hostname"] == "fixture.invalid"
            assert options["preferredauthentications"] == "password,keyboard-interactive"
            assert options["pubkeyauthentication"] in {"no", "false"}
            assert options["stricthostkeychecking"] in {"yes", "true"}
            assert options["userknownhostsfile"] == str(known)
            assert options["globalknownhostsfile"] == "/dev/null"
    print("ATOMX_SFTP_PASSWORD_MODE_AND_HOST_CHECKING_VERIFIED (offline)")


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
    folders = {
        "/Export": ["DecodedFiles", "flatFiles"],
        "/Export/DecodedFiles": ["sample"],
        "/Export/DecodedFiles/sample": ["batch"],
        "/Export/DecodedFiles/sample/batch": ["20260101_120000_S1"],
    }
    remote = SimpleNamespace(
        dirs=lambda path: folders[path],
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
        item = result["inventory"]["samples"][0]
        assert item["n_files"] == 1
        assert (item["sample"], item["as_sample"]) == ("sample", "SAMPLE")
        assert item["run_folder"] == "batch/20260101_120000_S1"
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
        from mica.agents.analysis import transfer_cards, transfer_destinations
        from mica.agents.analysis.registry import RegistryDoc
        from mica.agents.analysis.transfer_config import TransferRequest
        from mica.api.atomx import PAGE

        assert page.read_text() == PAGE
        pending = {
            "id": "a" * 32, "state": "preview", "expires": 2000000000,
            "request": {"sample": "sample", "as_sample": ""},
            "preview": {"sample": "sample", "export_root": "/Export", "run_folder": "run",
                        "n_files": 12, "total_bytes": 1200, "plan_fingerprint": "fixture"},
            "config": {"bucket": "fixture-bucket", "raw_prefix": "raw/",
                       "additional_prefix": "additional/"},
        }
        blocks = transfer_cards.blocks(pending, "Review transfer", ("confirm", "cancel"))
        assert "gs://fixture-bucket/raw/sample/" in str(blocks)
        assert "gs://fixture-bucket/additional/sample/" in str(blocks)
        assert [b["text"]["text"] for b in blocks[-1]["elements"]] == [
            "Approve transfer", "Cancel transfer",
        ]
        assert all("url" not in b for b in blocks[-1]["elements"])
        before = transfer_cards.token(pending)
        pending["config"]["raw_prefix"] = "different/"
        assert transfer_cards.token(pending) != before
        pending["state"] = "running"
        assert transfer_cards.blocks(pending, "Copying files", ("cancel",))[0]["type"] == "plan"
        print("ATOMX_SLACK_APPROVAL_AND_ACTIVITY_VERIFIED (offline)")
        doc = RegistryDoc()
        owner = "fixture@example.com"
        pid = doc.create(owner, "fixture", "gs://fixture-bucket/project/")
        proposal = TransferRequest(
            sample="sample", destination="gs://fixture-bucket/project/raw/sample/",
            additional_destination="gs://fixture-bucket/project/additional_files/sample/",
        )
        config = transfer_destinations.resolve(proposal, "", "Start that transfer", doc, pid, owner)
        assert config.raw_prefix == "project/raw/"
        assert config.additional_prefix == "project/additional_files/"
        guide = (
            "Data: gs://fixture-bucket/project/\nRaw: `raw/`\nAdditional: `additional_files/`\n"
            "Images were rebuilt from raw Morphology2D."
        )
        assert transfer_destinations.resolve(
            TransferRequest(sample="sample"), guide, "Start", doc, pid, owner,
        ).raw_prefix == "project/raw/"
        pending.update(state="preview", config=config.model_dump())
        blocks = transfer_cards.blocks(pending, "Review transfer", ("confirm", "cancel"))
        assert "gs://fixture-bucket/project/raw/sample/" in str(blocks)
        assert "sample/sample/" not in str(blocks)
        print("ATOMX_CONTEXT_PROPOSAL_AND_SAMPLE_PATHS_VERIFIED (offline)")
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
        from check_atomx_download import check_download

        check_worker_ssh()
        check_sftp_password_mode(installed / "scripts")
        check_download()
    print("ATOMX_PRIVATE_CONNECTION_AND_WORKER_TOOL_VERIFIED (offline)")


if __name__ == "__main__":
    asyncio.run(check())
