"""Renaming must preserve deployed settings, existing scripts and pending approvals."""

import importlib
import subprocess
import sys

import pytest

from mica.agents.analysis import transfer_interactions, write_approval
from mica.agents.analysis.config import Settings as AgentSettings
from mica.compat import getenv, legacy_environment
from mica.config import Settings
from mica.execution.backends import parse_progress_line


def test_deployed_settings_accept_old_names_and_prefer_mica(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text('PISTACHIO_GCP_PROJECT=old-project\nPISTACHIO_ALLOWED_EMAILS=["a@lab.org"]\n')
    assert Settings(_env_file=dotenv).gcp_project == "old-project"
    assert Settings(_env_file=dotenv).allowed_emails == ("a@lab.org",)
    monkeypatch.setenv("PISTACHIO_GCP_PROJECT", "deployed-project")
    assert Settings(_env_file=dotenv).gcp_project == "deployed-project"
    monkeypatch.setenv("MICA_GCP_PROJECT", "mica-project")
    assert Settings(_env_file=dotenv).gcp_project == "mica-project"
    assert Settings(_env_file=dotenv, gcp_project="explicit").gcp_project == "explicit"
    monkeypatch.setenv("PISTACHIO_GCS_BUCKET", "existing-bucket")
    assert AgentSettings(_env_file=None).bucket == "existing-bucket"
    monkeypatch.setenv("MICA_GCS_BUCKET", "new-bucket")
    assert AgentSettings(_env_file=None).bucket == "new-bucket"


def test_saved_scripts_keep_environment_and_progress_protocol(monkeypatch):
    monkeypatch.setenv("PISTACHIO_GCS_BUCKET", "existing-bucket")
    assert getenv("MICA_GCS_BUCKET") == "existing-bucket"
    monkeypatch.setenv("MICA_GCS_BUCKET", "")
    assert getenv("MICA_GCS_BUCKET") == ""
    env = legacy_environment({"MICA_SEED": "7", "PATH": "/bin"})
    assert env == {"MICA_SEED": "7", "PISTACHIO_SEED": "7", "PATH": "/bin"}
    value = '{"status":"info","message":"working","item":"analysis"}'
    current = parse_progress_line("MICA_PROGRESS " + value, 2)
    previous = parse_progress_line("PISTACHIO_PROGRESS " + value, 2)
    assert current is not None and previous is not None
    assert current.model_dump(exclude={"id", "created_at"}) == previous.model_dump(
        exclude={"id", "created_at"}
    )


@pytest.mark.parametrize("prefix", ["mica", "pistachio"])
def test_existing_and_new_slack_actions_are_recognized(prefix):
    assert write_approval.is_action(
        {"type": "block_actions", "actions": [{"action_id": prefix + "_overwrite_approve"}]}
    )
    assert transfer_interactions.is_transfer(
        {"type": "block_actions", "actions": [{"action_id": prefix + "_transfer_login"}]}
    )
    assert transfer_interactions.is_transfer(
        {"type": "view_submission", "view": {"callback_id": prefix + "_transfer_login_submit"}}
    )


def test_legacy_imports_share_model_identity_and_cli_still_runs():
    for name in ("config", "models", "agents.analysis.config", "worker.main"):
        assert importlib.import_module("pistachio." + name) is importlib.import_module(
            "mica." + name
        )
    result = subprocess.run(
        [sys.executable, "-m", "pistachio.agents.analysis.cli", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Local Mica agent" in result.stdout
