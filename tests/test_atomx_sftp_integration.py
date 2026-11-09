"""Read real SFTP data, expire connections, and exercise the verified transfer engine."""

import runpy
import shutil
from pathlib import Path

import paramiko
import pytest
from test_atomx_engine import MemoryBucket, MemoryRemote

from tools.atomx_transfer import core
from tools.atomx_transfer.adapters import SFTP
from tools.atomx_transfer.worker import ScriptSFTP

FIXTURE = runpy.run_path(str(Path(__file__).parents[1] / "scripts/check_atomx_download.py"))[
    "SFTPFixture"
]


@pytest.fixture
def server(tmp_path):
    for name, data in MemoryRemote().data.items():
        path = tmp_path / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    with FIXTURE(tmp_path) as fixture:
        yield fixture


def test_metadata_reconnects_after_channel_and_transport_close(server):
    remote = SFTP(server.config, server.login)
    try:
        path = next(iter(MemoryRemote().data))
        expected = remote.size(path)
        remote.sftp.close()
        assert remote.size(path) == expected
        remote.ssh.close()
        assert "MyExport" in remote.dirs("/")
        assert len(server.transports) == 3
    finally:
        remote.close()
    assert remote._login == {}
    with pytest.raises(ValueError, match="closed"):
        remote.size(path)


def test_missing_source_is_not_retried_as_a_connection_failure(server):
    remote = SFTP(server.config, server.login)
    try:
        with pytest.raises(FileNotFoundError):
            remote.size("/missing")
        assert len(server.transports) == 1
    finally:
        remote.close()


@pytest.mark.parametrize("drops", [1, 2])
def test_disconnect_during_stat_is_retried_only_once(server, drops):
    remote = SFTP(server.config, server.login)
    server.stat_drops = drops
    path, data = next(iter(MemoryRemote().data.items()))
    try:
        if drops == 1:
            assert remote.size(path) == len(data)
        else:
            with pytest.raises((EOFError, OSError, paramiko.SSHException)):
                remote.size(path)
        assert len(server.transports) == 2
    finally:
        remote.close()


def test_reconnect_rejects_a_changed_host_key(server):
    remote = SFTP(server.config, server.login)
    remote.sftp.close()
    server.key = paramiko.RSAKey.generate(2048)
    try:
        with pytest.raises(paramiko.BadHostKeyException):
            remote.size(next(iter(MemoryRemote().data)))
    finally:
        remote.close()


def test_real_scripts_transfer_and_verify_after_metadata_disconnect(server):
    if not shutil.which("sshpass"):
        pytest.skip("sshpass is required; CI and the installed-worker gate provide it")
    server.drop_on_download = True
    remote = ScriptSFTP(server.config, server.login)
    staging = Path(remote.temporary.name)
    bucket = MemoryBucket()
    try:
        run, files = core.plan(remote, "/MyExport", "tma37", "TMA37")
        request = {
            "sample": "tma37", "as_sample": "TMA37", "export_root": "/MyExport",
            "plan_fingerprint": core.fingerprint(run, files), "run_id": "sftp-integration",
            "tool_version": "fixture", "requested_by": "fixture@example.com",
            "login_saved_by": "fixture@example.com",
        }
        result = core.Transfer(remote, bucket, request).run()
        assert server.dropped
        assert result["files_done"] == len(files) == 4
        assert result["bytes_done"] == sum(f.size for f in files)
        for file in files:
            assert bucket.data[file.name] == MemoryRemote().data[file.source]
        assert bucket.read("transfer_status/TMA37.done.json")
        assert bucket.read("transfer_status/TMA37.in_progress.json") is None
        assert bucket.read("transfer_status/TMA37.failed.json") is None
        assert bucket.data["raw_outputs/raw/TMA37/done.txt"]
        assert bucket.data["raw_outputs/additional_files/TMA37/done.txt"]
    finally:
        remote.close()
    assert not staging.exists()
