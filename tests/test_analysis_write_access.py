"""Write grants must fail closed across command retries, expiration and project changes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from analysis_helpers import seed_project

from pistachio.agents.analysis import commands, write_access
from pistachio.agents.analysis.access import Grant
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.registry import Registry
from pistachio.agents.analysis.storage import LocalStorage

STAMP = 2_000_000_000


@pytest.fixture
def grants(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    pid = seed_project(store)
    settings = Settings(
        _env_file=None,
        bucket="internal-artifacts",
        write_admin_ids="T1:U1",
        write_allowed_prefixes="gs://lab-data/liver/output/",
        write_service_account="writer@example.com",
    )
    monkeypatch.setattr(write_access, "now", lambda: datetime.fromtimestamp(STAMP, UTC))
    return store, pid, settings


def run(
    grants, text, *, ts=STAMP, event_id=None, user="U1", email="alice@example.com", private=True
):
    store, pid, settings = grants
    cmd = commands.parse(text)
    grant = Grant(email, private, {}, Registry(store).load(), pid, True, "T1:D1")
    record = {
        "payload": {"team_id": "T1", "event": {"user": user, "ts": f"{ts}.0", "channel": "D1"}}
    }
    return commands.run(cmd, grant, Registry(store), Mock(), record, event_id or str(ts), settings)


def test_omitted_duration_is_indefinite_and_status_says_so(grants, monkeypatch):
    store, pid, settings = grants
    reply = run(grants, "write grant liver gs://lab-data/liver/output")
    assert "no expiry" in reply and "until revoked" in reply
    grant = write_access.active(store, pid, settings)
    assert grant.uri == "gs://lab-data/liver/output/" and grant.expires_at is None
    monkeypatch.setattr(
        write_access, "now", lambda: datetime.fromtimestamp(STAMP, UTC) + timedelta(days=999)
    )
    assert write_access.active(store, pid, settings) == grant
    assert "no expiry" in run(grants, "write status liver")


def test_timed_grant_expires_at_boundary_and_replay_cannot_extend_it(grants, monkeypatch):
    store, pid, settings = grants
    command = "write grant liver gs://lab-data/liver/output/ 1h"
    run(grants, command)
    expected = datetime.fromtimestamp(STAMP + 3600, UTC)
    assert write_access.active(store, pid, settings).expires_at == expected
    monkeypatch.setattr(write_access, "now", lambda: expected)
    assert "expired" in run(grants, command)
    assert write_access.active(store, pid, settings) is None
    assert write_access.load(store, pid).grant.expires_at == expected


def test_replays_and_out_of_order_events_do_not_restore_or_remove_new_access(grants):
    store, pid, settings = grants
    command = "write grant liver gs://lab-data/liver/output/"
    run(grants, command)
    original = write_access.active(store, pid, settings)
    run(grants, "write revoke liver", ts=STAMP + 2)
    assert "No active" in run(grants, command)
    with pytest.raises(ValueError, match="newer"):
        run(grants, command, ts=STAMP + 1)
    assert write_access.active(store, pid, settings) is None
    run(grants, command, ts=STAMP + 3)
    current = write_access.active(store, pid, settings)
    assert current.id != original.id
    run(grants, "write revoke liver", ts=STAMP + 2)
    assert write_access.active(store, pid, settings) == current
    with pytest.raises(PermissionError):
        write_access.require_active(store, pid, original.id, settings)
    assert len(write_access.load(store, pid).events) == 3


@pytest.mark.parametrize(
    "text",
    [
        "write grant liver gs://lab-data/liver/output/ 0h",
        "write grant liver gs://lab-data/liver/output/ -1h",
        "write grant liver gs://lab-data/liver/output/ 1hour",
        "write grant liver gs://lab-data/liver/output/ 366d",
        "write grant liver gs://lab-data/liver/output/ 1h extra",
        "write revoke",
        "write status",
        "write grant liver",
        "write grant liver gs://lab-data/liver/output/ --overwrite",
    ],
)
def test_malformed_commands_never_reach_the_model(text):
    with pytest.raises(ValueError):
        commands.parse(text)


def test_link_markup_and_natural_language():
    cmd = commands.parse("<@U123> write grant liver <gs://lab-data/liver/output/|folder> 30m")
    assert cmd.args == ("liver", "gs://lab-data/liver/output/", "30m")
    assert commands.parse("write a script that plots cell counts") is None


@pytest.mark.parametrize(
    "uri",
    [
        "gs://lab-data/liver/output-else/",
        "gs://lab-data/liver/",
        "gs://lab-data/",
        "gs://other-bucket/liver/output/",
        "gs://lab-data/liver/output/../private/",
        "gs://lab-data/liver/output/%2e%2e/private/",
        "gs://lab-data/liver/output/*/",
    ],
)
def test_scope_cannot_escape_the_configured_ceiling(grants, uri):
    with pytest.raises(ValueError):
        run(grants, f"write grant liver {uri}")


def test_whole_bucket_grant_requires_whole_bucket_configuration(grants):
    store, pid, settings = grants
    settings.write_allowed_prefixes = "gs://lab-data/"
    run(grants, "write grant liver gs://lab-data/")
    assert write_access.active(store, pid, settings).uri == "gs://lab-data/"


def test_default_off_admin_membership_dm_and_runtime_storage_protection(grants):
    store, pid, settings = grants
    command = "write grant liver gs://lab-data/liver/output/"
    with pytest.raises(ValueError, match="administrators"):
        run(grants, command, user="U2")
    with pytest.raises(ValueError, match="DM"):
        run(grants, command, private=False)
    with pytest.raises(ValueError, match="unavailable"):
        run(grants, command, email="bob@example.com")
    settings.write_service_account = ""
    with pytest.raises(ValueError, match="not configured"):
        run(grants, command)
    settings.write_service_account = "writer@example.com"
    settings.write_allowed_prefixes = "gs://internal-artifacts/"
    with pytest.raises(ValueError, match="exports"):
        run(grants, "write grant liver gs://internal-artifacts/projects/")
    doc = Registry(store).load()
    doc.projects[pid].data_uri = "gs://internal-artifacts/input/liver/"
    store.put("projects/_registry/registry.json", doc.model_dump_json().encode())
    run(grants, "write grant liver gs://internal-artifacts/exports/liver/")
    assert write_access.active(store, pid, settings)
    settings.write_admin_ids = ""
    assert write_access.active(store, pid, settings) is None


def test_cannot_grant_another_projects_bucket_even_with_global_approval(grants):
    store, pid, settings = grants
    settings.write_allowed_prefixes += ",gs://other-data/"
    doc = Registry(store).load()
    doc.create("alice@example.com", "other", "gs://other-data/")
    store.put("projects/_registry/registry.json", doc.model_dump_json().encode())
    with pytest.raises(ValueError, match="linked data bucket"):
        run(grants, "write grant liver gs://other-data/outputs/")
    assert write_access.active(store, pid, settings) is None
    run(grants, "write grant other gs://other-data/outputs/", ts=STAMP + 1)


@pytest.mark.parametrize("change", ["bucket", "membership", "deleted"])
def test_existing_grant_fails_closed_when_project_association_changes(grants, change):
    store, pid, settings = grants
    run(grants, "write grant liver gs://lab-data/liver/output/")
    doc = Registry(store).load()
    if change == "bucket":
        doc.projects[pid].data_uri = "gs://other-data/"
    elif change == "membership":
        doc.projects[pid].owner = "bob@example.com"
    else:
        del doc.projects[pid]
    store.put("projects/_registry/registry.json", doc.model_dump_json().encode())
    assert write_access.active(store, pid, settings) is None


def test_project_members_can_read_status_but_cannot_grant(grants):
    store, pid, _ = grants
    registry = Registry(store)
    registry.update(
        "share",
        "alice@example.com",
        "share",
        lambda d: d.share(pid, "alice@example.com", "bob@example.com"),
    )
    run(grants, "write grant liver gs://lab-data/liver/output/")
    assert "no expiry" in run(grants, "write status liver", user="U2", email="bob@example.com")
    with pytest.raises(ValueError, match="administrators"):
        run(grants, "write revoke liver", user="U2", email="bob@example.com")


def test_configuration_changes_disable_existing_grants(grants):
    store, pid, settings = grants
    run(grants, "write grant liver gs://lab-data/liver/output/")
    settings.write_allowed_prefixes = "gs://lab-data/another-project/"
    assert write_access.active(store, pid, settings) is None


def test_command_replay_remains_bound_to_original_project_after_rename(grants):
    store, pid, settings = grants
    command = "write grant liver gs://lab-data/liver/output/"
    run(grants, command)
    registry = Registry(store)
    registry.update(
        "rename",
        "alice@example.com",
        "rename",
        lambda d: d.rename(pid, "alice@example.com", "renamed"),
    )
    other = seed_project(store, name="liver", channel="D2")
    assert other != pid
    run(grants, command)
    assert write_access.active(store, pid, settings)
    assert write_access.active(store, other, settings) is None


def test_reusing_command_id_with_different_scope_is_rejected(grants):
    run(grants, "write grant liver gs://lab-data/liver/output/", event_id="same")
    with pytest.raises(ValueError, match="already used"):
        run(grants, "write grant liver gs://lab-data/liver/output/nested/", event_id="same")


def test_help_alias_matches_the_single_command_list(grants):
    assert run(grants, "help") == run(grants, "help all")


@pytest.mark.parametrize("text", ["help", "help all", "help write", "write help"])
def test_help_explains_commands_optional_expiry_and_write_limits(grants, text):
    reply = run(grants, text)
    for required in (
        "write grant",
        "write status",
        "write revoke",
        "until revoked",
        "overwrites",
        "write admins",
        "project's linked bucket",
    ):
        assert required in reply
    if text in {"help write", "write help"}:
        assert "future runs" in reply and "Confirm overwrites" in reply


def test_write_access_log_identifies_grant_without_prompt_or_file_content(grants, caplog):
    store, pid, settings = grants
    run(grants, "write grant liver gs://lab-data/liver/output/")
    grant = write_access.active(store, pid, settings)
    with caplog.at_level("INFO"):
        write_access.log_turn("a" * 32, grant)
        write_access.log_turn("b" * 32, None)
    messages = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("write_access ")
    ]
    assert messages == [
        f"write_access event={'a' * 32} grant={grant.id[:8]} uri=gs://lab-data/liver/output/",
        f"write_access event={'b' * 32} grant=none uri=none",
    ]


def test_store_instructions_describe_layout_current_grant_and_confirmed_uris(grants):
    store, pid, settings = grants
    run(grants, "write grant liver gs://lab-data/liver/output/")
    text = write_access.instructions(write_access.active(store, pid, settings))
    for phrase in [
        "outputs/gcs/",
        "tables/*.zarr",
        "publish_gcs",
        "application-confirmed targets",
        "unchanged inputs",
        "only URIs returned",
        "earlier turns",
    ]:
        assert phrase in text
