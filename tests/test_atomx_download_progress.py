"""Observe partial local files without reporting them as verified GCS objects."""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.atomx_transfer import worker
from tools.atomx_transfer.core import File


@pytest.mark.parametrize("fail", [False, True])
def test_script_reports_partial_downloads_and_checks_only_after_success(
    tmp_path, monkeypatch, fail,
):
    remote = object.__new__(worker.ScriptSFTP)
    remote.config = {"host": "fixture", "host_key": "ssh-rsa fixture"}
    remote.login = {"username": "fixture", "password": "synthetic"}
    remote.temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    remote.local = {}
    remote.size = lambda _: 5
    files = [File("/source", "raw_outputs/raw/sample/file", 5)]
    updates = []
    waits = 0
    finished = False

    def wait(**kwargs):
        nonlocal waits, finished
        if finished:
            return 1 if fail else 0
        waits += 1
        target = Path(remote.temporary.name) / "0"
        if waits == 1:
            target.write_bytes(b"ab")
            raise subprocess.TimeoutExpired("fixture", 15)
        finished = True
        if not fail:
            target.write_bytes(b"abcde")
        return 1 if fail else 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        wait=wait, poll=lambda: (1 if fail else 0) if finished else None,
    ))
    try:
        if fail:
            with pytest.raises(ValueError, match="script download failed"):
                remote.stage(files, lambda: None, lambda *p: updates.append(p))
            assert all(p[0] == "downloading" for p in updates)
        else:
            remote.stage(files, lambda: None, lambda *p: updates.append(p))
            assert updates[-1] == ("checking_downloads", 1, 5)
        assert updates[:2] == [("downloading", 0, 0), ("downloading", 0, 2)]
    finally:
        remote.temporary.cleanup()
