"""Exercise the actual job entrypoint with the layout-free controller artifact contract."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from paramiko import AuthenticationException
from test_atomx_engine import MemoryBucket, MemoryRemote, run_cli

from mica.agents.analysis.transfer_config import TransferProject
from mica.agents.analysis.transfers import failure_notice
from tools.atomx_transfer import cli, core, errors, worker

RUN = "c99e8801b5e3bca304030f6b3bcab5d7"
DESTINATIONS = {
    "bucket": "fixture-bucket",
    "raw_prefix": "raw_outputs/raw/",
    "additional_prefix": "raw_outputs/raw/.additional_files/",
    "status_prefix": "raw_outputs/raw/.mica-transfers/",
}
# Same field shape as the failing TMA56 artifact; owner, provider and bucket are synthetic.
REQUEST = {
    "additional_destination": "",
    "as_sample": "TMA56",
    "bucket": "fixture-bucket",
    "connection_revision": "fixture-revision",
    "control_url": "https://fixture.invalid",
    "destination": "gs://fixture-bucket/raw_outputs/raw/",
    "destinations": DESTINATIONS,
    "export_root": "/MyExport",
    "login_saved_by": "fixture@example.com",
    "project": "fixture-project",
    "requested_by": "fixture@example.com",
    "run_id": RUN,
    "sample": "tma37",
}


@pytest.fixture
def job(monkeypatch):
    artifact, data, remote = MemoryBucket(), MemoryBucket(), MemoryRemote()
    remote.close = lambda: None
    run, files = core.plan(remote, REQUEST["export_root"], REQUEST["sample"], REQUEST["as_sample"])
    bootstrap = {
        **deepcopy(REQUEST),
        "kind": "inventory",
        "plan_fingerprint": core.fingerprint(run, files),
        "login": {"username": "fixture", "password": "never-publish-this-password"},
        "sftp": {},
    }
    monkeypatch.setattr(cli, "GCS", lambda _: artifact)
    monkeypatch.setattr(worker, "GCS", lambda *args: data)
    monkeypatch.setattr(
        worker, "Control", lambda _: SimpleNamespace(call=lambda _: deepcopy(bootstrap))
    )
    monkeypatch.setattr(cli, "SFTP", lambda *_: remote)
    monkeypatch.setattr(worker, "ScriptSFTP", lambda *_: remote)

    def execute(kind="inventory", request=None):
        bootstrap["kind"] = kind
        prefix = f"tasks/transfers/{RUN}/{kind}"
        artifact.put(prefix + "-request.json", json.dumps(request or REQUEST).encode())
        code = cli.run_job("gs://artifacts/" + prefix + "-request.json")
        result = artifact.read(prefix + "-result.json")[0]
        return code, result

    return SimpleNamespace(
        execute=execute, artifact=artifact, data=data, remote=remote, bootstrap=bootstrap
    )


def test_layout_free_request_uses_bootstrap_scope_for_inventory_and_pull(job):
    poisoned = {**REQUEST, "destinations": {"bucket": "untrusted"}, "sample": "wrong"}
    code, result = job.execute(request=poisoned)
    assert code == 0 and result["inventory"]["samples"][0]["n_files"] == 4
    assert job.data.data == {}  # Inventory cannot write to the data bucket.
    code, result = job.execute("pull", poisoned)
    assert code == 0 and result["marker"]["n_files"] == 4
    record = DESTINATIONS["status_prefix"] + "TMA56.done.json"
    assert job.data.read(record)[0] == result["marker"]
    for prefix in (DESTINATIONS["raw_prefix"], DESTINATIONS["additional_prefix"]):
        text = job.data.data[prefix + "TMA56/done.txt"].decode()
        assert f"record: gs://fixture-bucket/{record}\n" in text
    assert all(name.startswith("raw_outputs/raw/") for name in job.data.data)


@pytest.mark.parametrize(
    "field,value,step",
    [
        ("bucket", "other-bucket", "config"),
        ("run_id", "a" * 32, "config"),
    ],
)
def test_request_identity_mismatch_fails_before_sftp(job, monkeypatch, field, value, step):
    monkeypatch.setattr(cli, "SFTP", lambda *_: pytest.fail("SFTP must not open"))
    code, result = job.execute(request={**REQUEST, field: value})
    assert code == 1 and result["step"] == step and result["error_class"] == "ValueError"
    assert not job.data.data


def test_bootstrap_identity_mismatch_fails_closed(job, monkeypatch):
    job.bootstrap["run_id"] = "a" * 32
    monkeypatch.setattr(cli, "SFTP", lambda *_: pytest.fail("SFTP must not open"))
    code, result = job.execute()
    assert code == 1 and result["step"] == "bootstrap"


def test_legacy_config_validates_without_layout_field():
    model = TransferProject.model_validate({**DESTINATIONS, "layout": ""})
    assert "layout" not in model.model_dump()


@pytest.mark.parametrize("step", sorted(errors.STEPS))
def test_failure_reasons_survive_each_stage_without_secret_text(job, monkeypatch, capsys, step):
    def fail(*args, **kwargs):
        raise ValueError("never-publish-this-password or server response")

    kind = "inventory"
    if step == "config":
        monkeypatch.setattr(cli, "GCS", fail)
    elif step == "bootstrap":
        monkeypatch.setattr(worker, "Control", fail)
    elif step == "sftp_auth":
        monkeypatch.setattr(cli, "SFTP", fail)
    elif step == "sftp_listing":
        monkeypatch.setattr(job.remote, "dirs", fail)
    else:
        kind = "pull"
        if step == "staging":
            job.remote.stage = fail
        elif step == "upload":
            monkeypatch.setattr(job.data, "copy", fail)
        elif step == "verify":
            monkeypatch.setattr(core, "destination_objects", fail)
        elif step == "markers":
            original = job.data.write
            monkeypatch.setattr(
                job.data,
                "write",
                lambda name, *args: (
                    fail() if name.endswith(".done.json") else original(name, *args)
                ),
            )
    if step == "config":
        assert cli.run_job(f"gs://artifacts/tasks/transfers/{RUN}/inventory-request.json") == 1
        result = json.loads(capsys.readouterr().out)
    else:
        code, result = job.execute(kind)
        assert code == 1
    assert result["step"] == step and result["error_class"] == "ValueError"
    assert (
        "never-publish"
        not in json.dumps(result)
        + str(job.artifact.data)
        + str(job.data.data)
        + capsys.readouterr().out
    )
    assert f"during {step} (ValueError)" in failure_notice(result)


def test_authentication_failure_keeps_login_replacement_status(job, monkeypatch):
    def fail(*_):
        raise AuthenticationException("password-secret")

    monkeypatch.setattr(cli, "SFTP", fail)
    code, result = job.execute()
    assert code == 1 and result["status"] == "auth_failed"
    assert result["error_class"] == "AuthenticationException"
    assert "password-secret" not in str(result)


def test_failed_failure_marker_keeps_original_reason(job, monkeypatch):
    def fail(*_):
        raise TimeoutError("password-secret")

    job.remote.stage = fail
    original = job.data.write
    monkeypatch.setattr(
        job.data,
        "write",
        lambda name, *args: fail() if name.endswith(".failed.json") else original(name, *args),
    )
    code, result = job.execute("pull")
    assert code == 1 and result["step"] == "staging" and result["error_class"] == "TimeoutError"


def test_dynamic_exception_names_and_untrusted_artifacts_never_become_notices():
    secret_type = type("password-secret", (Exception,), {})
    result = errors.reason("password-secret", secret_type("password-secret"))
    assert result == {"step": "unknown", "error_class": "UnknownError"}
    for bad in ("password-secret", [], {}):
        assert "password-secret" not in failure_notice({"step": bad, "error_class": bad})
        assert "password-secret" not in failure_notice({"step": "upload", "error_class": bad})
    assert "markers" not in failure_notice({})


def test_mapped_operator_backfill_verify_and_nested_folders(job, monkeypatch, tmp_path, capsys):
    for prefix in (DESTINATIONS["raw_prefix"], DESTINATIONS["additional_prefix"]):
        job.data.put(prefix + "TMA56/data.csv", b"small")
    job.data.put(DESTINATIONS["status_prefix"] + "other.failed.json", b"{}")
    args = (monkeypatch, tmp_path, job.data)
    assert run_cli(*args, "backfill", "--yes", destinations=DESTINATIONS) == 0
    assert json.loads(capsys.readouterr().out)["marked"] == ["TMA56"]
    record = DESTINATIONS["status_prefix"] + "TMA56.done.json"
    assert job.data.read(record)[0]["destinations"] == DESTINATIONS
    assert (
        f"record: gs://fixture-bucket/{record}\n"
        in job.data.data[DESTINATIONS["raw_prefix"] + "TMA56/done.txt"].decode()
    )
    assert (
        run_cli(
            *args, "verify", "--sample", "TMA56", "--allow-backfilled", destinations=DESTINATIONS
        )
        == 0
    )
    assert "Listing-only" in capsys.readouterr().out


def test_mapped_transfer_verifies_with_manual_cli(job, monkeypatch, tmp_path):
    assert job.execute("pull")[0] == 0
    assert (
        run_cli(
            monkeypatch,
            tmp_path,
            job.data,
            "verify",
            "--sample",
            "TMA56",
            destinations=DESTINATIONS,
        )
        == 0
    )


def test_manual_config_requires_destinations(tmp_path, monkeypatch, capsys):
    config = tmp_path / "legacy.yaml"
    config.write_text("bucket: legacy\n")
    monkeypatch.setattr(
        "sys.argv", ["atomx-transfer", "verify", "--config", str(config), "--sample", "TMA56"]
    )
    assert cli.main() == 1
    assert "requires a destinations block" in capsys.readouterr().err


def test_mapping_refuses_outside_objects_and_inventory_writes(job):
    bucket = worker.MappedGCS(DESTINATIONS)
    with pytest.raises(ValueError, match="outside"):
        bucket.map("projects/another-project/data.csv")
    with pytest.raises(ValueError, match="read-only"):
        bucket.copy("raw_outputs/raw/TMA56/test", iter([b"test"]))


def test_wrong_status_prefix_fails_with_bounded_marker_reason(job, monkeypatch):
    job.bootstrap["destinations"]["status_prefix"] = "forbidden/"
    original = job.data.write

    def scoped_write(name, *args):
        if name.startswith("forbidden/"):
            raise PermissionError("synthetic-secret from scoped storage")
        return original(name, *args)

    monkeypatch.setattr(job.data, "write", scoped_write)
    code, result = job.execute("pull")
    assert code == 1 and result["step"] == "markers" and result["error_class"] == "PermissionError"
    assert not job.data.data and "synthetic-secret" not in str(result)


def test_kind_mismatch_rejected_before_sftp(job, monkeypatch):
    response = {**job.bootstrap, "kind": "pull"}
    monkeypatch.setattr(worker, "Control", lambda _: SimpleNamespace(call=lambda _: response))
    monkeypatch.setattr(cli, "SFTP", lambda *_: pytest.fail("SFTP must not open"))
    code, result = job.execute()
    assert code == 1 and result["step"] == "bootstrap"


def test_manual_repair_preserves_old_record_and_adds_configured_uri(job, monkeypatch, tmp_path):
    assert job.execute("pull")[0] == 0
    record = DESTINATIONS["status_prefix"] + "TMA56.done.json"
    body = job.data.read(record)[0]
    body["destinations"] = {}
    job.data.put(record, json.dumps(body).encode())
    original = job.data.data[record], job.data.generations[record]
    marker = DESTINATIONS["raw_prefix"] + "TMA56/done.txt"
    job.data.delete(marker, job.data.generations[marker])
    assert (
        run_cli(monkeypatch, tmp_path, job.data, "backfill", "--yes", destinations=DESTINATIONS)
        == 0
    )
    assert (job.data.data[record], job.data.generations[record]) == original
    assert f"record: gs://fixture-bucket/{record}\n" in job.data.data[marker].decode()
