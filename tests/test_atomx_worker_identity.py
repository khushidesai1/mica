"""The image gate must catch the runtime failure missed by executable-only checks."""

import os
import pwd
import runpy
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def gate(monkeypatch):
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / "tools"))
    check = runpy.run_path(str(root / "scripts/check_atomx_release.py"))[
        "check_worker_ssh"
    ]
    monkeypatch.setattr(os, "getuid", lambda: 65532)
    monkeypatch.setattr(os, "getgid", lambda: 65532)
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda _: SimpleNamespace(
            pw_gid=65532, pw_dir="/srv/pistachio", pw_shell="/usr/sbin/nologin"
        ),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    return check


def test_gate_rejects_missing_local_account_before_invoking_ssh(gate, monkeypatch):
    def missing(_):
        raise KeyError("65532")

    monkeypatch.setattr(pwd, "getpwuid", missing)
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(KeyError):
        gate()
    run.assert_not_called()


def test_gate_requires_successful_openssh_startup(gate, monkeypatch):
    failure = subprocess.CalledProcessError(255, "ssh", stderr="No user exists for uid 65532")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(subprocess.CalledProcessError):
        gate()


def test_gate_runs_config_only_and_checks_the_remote_user(gate, monkeypatch):
    run = Mock(return_value=SimpleNamespace(stdout="user synthetic\nhostname localhost\n"))
    monkeypatch.setattr(subprocess, "run", run)
    gate()
    assert run.call_args.args[0] == [
        "ssh", "-G", "-F", "/dev/null", "-o", "User=synthetic", "localhost"
    ]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["timeout"] == 10
    run.return_value.stdout = "user wrong-user\n"
    with pytest.raises(AssertionError):
        gate()
