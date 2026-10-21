"""Offline release gate; synthetic credentials, no provider or data-bucket connection."""

import asyncio
import hashlib
import shutil
import sys
from pathlib import Path

from atomx_transfer import cli, worker
from mica.agents.analysis.atomx_connections import Connections
from mica.agents.analysis.config import Settings
from mica.api.atomx import PAGE
from mica.config import Settings as AppSettings
from mica.storage.memory import InMemoryRepository


async def check():
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
    assert 'type="password"' in PAGE and "location.hash" in PAGE
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


asyncio.run(check())
