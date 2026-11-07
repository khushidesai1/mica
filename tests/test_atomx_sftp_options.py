"""Exercise the download scripts through real OpenSSH option parsing, without networking."""

import runpy
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def script_check(monkeypatch):
    if not shutil.which("ssh") or not shutil.which("sftp"):
        pytest.skip("OpenSSH is required; the installed worker gate requires it unconditionally")
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / "tools"))
    check = runpy.run_path(str(root / "scripts/check_atomx_release.py"))[
        "check_sftp_password_mode"
    ]
    return check, root / "tools/atomx_transfer/scripts"


def test_download_scripts_allow_passwords_and_require_the_pinned_host(script_check):
    check, scripts = script_check
    check(scripts)


def test_gate_rejects_the_original_option_order(script_check, tmp_path):
    check, scripts = script_check
    shutil.copytree(scripts, tmp_path / "scripts")
    manifest = tmp_path / "scripts/download_manifest.sh"
    manifest.write_text(
        manifest.read_text().replace('-o BatchMode=no -b "$1"', '-b "$1" -o BatchMode=no')
    )
    with pytest.raises(AssertionError, match="Password authentication disabled"):
        check(manifest.parent)
