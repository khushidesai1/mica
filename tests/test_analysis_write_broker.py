"""Server-side rewrite authority, resumability, and verified-source cleanup."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed
from test_analysis_write_access import STAMP, grants, run  # noqa: F401

from mica.agents.analysis import write_access, write_approval, write_broker


class Blob:
    def __init__(self, name, data=None):
        self.name, self.data = name, data
        self.generation = 7
        self.metadata = None
        self.time_created = datetime.now(UTC) - timedelta(minutes=1)
        self.writes, self.calls, self.deletes = 0, [], []
        self.iterations = 1
        self.on_rewrite = lambda: None
        self.bucket = SimpleNamespace(name="artifacts", blob=lambda name: self)
        self.refresh()

    def refresh(self):
        self.size = len(self.data) if self.data is not None else None
        self.crc32c = hashlib.sha256(self.data).hexdigest() if self.data is not None else None

    def reload(self, **kwargs):
        if self.data is None:
            raise NotFound("missing")
        self.refresh()

    def rewrite(self, source, **kwargs):
        self.calls.append(kwargs)
        expected = kwargs["if_generation_match"]
        assert kwargs["if_source_generation_match"] == source.generation
        self.on_rewrite()
        if (self.data is None and expected != 0) or (
            self.data is not None and expected != self.generation
        ):
            raise PreconditionFailed("target changed")
        n = int(kwargs["token"] or 0) + 1
        if n < self.iterations:
            return str(n), n, source.size
        self.data = source.data
        self.generation += 1
        self.time_created = datetime.now(UTC)
        self.writes += 1
        self.refresh()
        return None, self.size, self.size

    def delete(self, **kwargs):
        assert kwargs["if_generation_match"] == self.generation
        self.deletes.append(self.generation)
        self.data = None


@pytest.fixture
def transfer(grants, monkeypatch):  # noqa: F811
    store, pid, settings = grants
    run(grants, "write grant liver gs://lab-data/liver/output/")
    event = "a" * 32
    grant = write_access.active(store, pid, settings)
    source = Blob(write_broker.staged_key(event, "nested/result.csv"), b"some-result-data")
    dest = Blob("liver/output/nested/result.csv")
    store.bucket = SimpleNamespace(name="artifacts", blob=Mock(return_value=source))
    factory = Mock(return_value=dest)
    monkeypatch.setattr(write_broker, "destination_blob", factory)
    authorize = Mock()

    def upload(**kwargs):
        saved = write_approval.load(store, event)
        return write_broker.upload(
            store,
            settings,
            pid,
            event,
            "nested/result.csv",
            grant.id,
            authorize,
            approval=saved[0] if saved else None,
            **kwargs,
        )

    return SimpleNamespace(
        upload=upload,
        store=store,
        pid=pid,
        grant=grant,
        source=source,
        dest=dest,
        factory=factory,
        authorize=authorize,
        settings=settings,
        grants=grants,
    )


def receipts(transfer):
    return [
        p for p in transfer.store.keys("write-access/uploads") if not p.endswith(".rewrite.json")
    ]


def approve_existing(transfer):
    import time

    approval = write_approval.Approval(
        event_id="a" * 32,
        project_id=transfer.pid,
        grant_id=transfer.grant.id,
        principal=transfer.grant.actor,
        access_signature="test",
        paths=[transfer.grant.uri + "nested/"],
        inspected_at=time.time(),
        expires_at=time.time() + 3600,
        status="approved",
        approved_at=time.time(),
    )
    transfer.store.put(write_approval.key(approval.event_id), approval.model_dump_json().encode())
    transfer.dest.data = b"old approved result"
    return approval


def test_copy_preserves_subpaths_and_cleans_source_after_receipt(transfer):
    data = transfer.source.data
    delete = transfer.source.delete

    def cleanup(**kwargs):
        assert len(receipts(transfer)) == 1
        assert transfer.dest.data == data
        delete(**kwargs)

    transfer.source.delete = cleanup
    result = transfer.upload()
    assert result == {"uri": "gs://lab-data/liver/output/nested/result.csv", "status": "uploaded"}
    assert transfer.source.data is None
    assert transfer.upload() == result and transfer.dest.writes == 1
    assert len(receipts(transfer)) == 1


def test_rewrite_progress_is_visible_with_default_api_logging(transfer, monkeypatch, caplog):
    # Uvicorn leaves the application root at WARNING; successful copies still
    # need auditable progress without enabling verbose HTTP/SDK logging.
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    transfer.dest.iterations = 2
    assert transfer.upload()["status"] == "uploaded"
    messages = [r.getMessage() for r in caplog.records if r.name == write_broker.__name__]
    assert len(messages) == 2
    assert all("gcs_rewrite event=" + "a" * 32 in m for m in messages)
    assert "pending=True" in messages[0] and "pending=False" in messages[1]
    assert "total=16" in messages[1]
    assert all("some-result-data" not in m and "result.csv" not in m for m in messages)


def test_unapproved_existing_object_is_never_replaced(transfer):
    transfer.dest.data = b"important-original"
    with pytest.raises(FileExistsError, match="new filename"):
        transfer.upload()
    assert transfer.dest.data == b"important-original" and transfer.dest.writes == 0
    assert not transfer.source.deletes


@pytest.mark.parametrize("change", ["revoke", "replace", "expire", "membership"])
def test_rechecks_authority_between_rewrite_iterations(transfer, change):
    transfer.dest.iterations = 3

    def update():
        if change == "revoke":
            run(transfer.grants, "write revoke liver", ts=STAMP + 1)
        elif change == "replace":
            run(transfer.grants, "write grant liver gs://lab-data/liver/output/new/", ts=STAMP + 1)
        elif change == "expire":
            state = write_access.load(transfer.store, transfer.pid)
            state.grant.expires_at = write_access.now()
            transfer.store.put(write_access.key(transfer.pid), state.model_dump_json().encode())
        else:
            transfer.authorize.side_effect = PermissionError("membership changed")

    transfer.dest.on_rewrite = update
    with pytest.raises(PermissionError):
        transfer.upload()
    assert transfer.dest.data is None and len(transfer.dest.calls) == 1
    assert not receipts(transfer) and not transfer.source.deletes


def test_rejected_before_credentials_are_minted(transfer):
    run(transfer.grants, "write revoke liver", ts=STAMP + 1)
    with pytest.raises(PermissionError):
        transfer.upload()
    transfer.factory.assert_not_called()


def test_only_real_gcs_object_size_bound_remains(transfer, monkeypatch):
    monkeypatch.setattr(transfer.source, "refresh", lambda: None)
    transfer.source.size = 5 * 1024**4 + 1
    with pytest.raises(ValueError, match="5 TiB"):
        transfer.upload()
    assert not transfer.dest.calls


@pytest.mark.parametrize(
    "path",
    [
        "../other",
        "/absolute",
        "a/../../other",
        "a//b",
        "a/./b",
        "gs://other/key",
        "a%2fb",
        "a\\b",
        "a\x00b",
        "",
    ],
)
def test_rejects_untrusted_relative_paths(path):
    with pytest.raises(ValueError):
        write_broker.staged_key("a" * 32, path)


def test_cloud_credential_has_exact_destination_and_read_only_source_rules():
    rules = write_broker.boundary(
        "gs://data/output/result.csv",
        source_uri="gs://artifacts/staged/result.csv",
        overwrite=True,
    ).to_json()["accessBoundary"]["accessBoundaryRules"]
    assert len(rules) == 2
    assert rules[0]["availablePermissions"] == ["inRole:roles/storage.objectAdmin"]
    assert rules[1]["availablePermissions"] == ["inRole:roles/storage.objectViewer"]
    assert rules[0]["availabilityCondition"]["expression"] == (
        'resource.name == "projects/_/buckets/data/objects/output/result.csv"'
    )
    assert rules[1]["availabilityCondition"]["expression"] == (
        'resource.name == "projects/_/buckets/artifacts/objects/staged/result.csv"'
    )
    create = write_broker.boundary("gs://data/new").to_json()["accessBoundary"][
        "accessBoundaryRules"
    ]
    assert "inRole:roles/storage.objectAdmin" not in create[0]["availablePermissions"]


def test_deadline_resumes_saved_token_and_writes_one_receipt(transfer, monkeypatch):
    elapsed = [0]
    monkeypatch.setattr(write_broker.time, "monotonic", lambda: elapsed[0])
    transfer.settings.write_upload_seconds = 100
    transfer.dest.iterations = 3
    transfer.dest.on_rewrite = lambda: elapsed.__setitem__(0, elapsed[0] + 95)
    assert transfer.upload()["status"] == "in_progress"
    assert not receipts(transfer) and transfer.source.data
    assert transfer.upload()["status"] == "in_progress"
    assert transfer.upload()["status"] == "uploaded"
    assert [call["token"] for call in transfer.dest.calls] == [None, "1", "2"]
    assert transfer.dest.writes == 1 and len(receipts(transfer)) == 1


def test_approved_replacement_pins_generation(transfer):
    approve_existing(transfer)
    result = transfer.upload()
    assert transfer.dest.generation == 8
    assert transfer.dest.calls[0]["if_generation_match"] == 7
    assert transfer.factory.call_args.kwargs["overwrite"] is True
    assert transfer.upload() == result and transfer.dest.writes == 1
    assert json.loads(transfer.store.get(receipts(transfer)[0])[0])["replaced_generation"] == 7


@pytest.mark.parametrize("change", ["new-version", "wrong-grant", "not-approved", "outside"])
def test_approval_does_not_authorize_new_versions_or_other_paths(transfer, change):
    approval = approve_existing(transfer)
    if change == "new-version":
        transfer.dest.generation = 8
        transfer.dest.time_created = datetime.now(UTC) + timedelta(seconds=1)
    elif change == "wrong-grant":
        approval.grant_id = "b" * 32
    elif change == "outside":
        approval.paths = [transfer.grant.uri + "other/"]
    else:
        approval.status = "pending"
    transfer.store.put(write_approval.key(approval.event_id), approval.model_dump_json().encode())
    before = transfer.dest.data
    with pytest.raises(FileExistsError, match="changed since approval"):
        transfer.upload()
    assert transfer.dest.data == before and not receipts(transfer)


def test_concurrent_writer_is_preserved(transfer):
    approve_existing(transfer)

    def change():
        transfer.dest.generation = 8
        transfer.dest.data = b"newer external content"
        transfer.dest.metadata = {"external": "writer"}

    transfer.dest.on_rewrite = change
    with pytest.raises(FileExistsError):
        transfer.upload()
    assert transfer.dest.data == b"newer external content" and not receipts(transfer)


def test_lost_rewrite_response_reconciles_without_a_second_copy(transfer):
    approve_existing(transfer)
    copy = transfer.dest.rewrite

    def lost(source, **kwargs):
        copy(source, **kwargs)
        raise PreconditionFailed("lost acknowledgement")

    transfer.dest.rewrite = lost
    assert transfer.upload()["status"] == "uploaded"
    assert transfer.upload()["status"] == "uploaded"
    assert transfer.dest.writes == 1 and len(receipts(transfer)) == 1


def test_cleanup_failure_retries_after_durable_receipt(transfer):
    cleanup = transfer.source.delete
    transfer.source.delete = Mock(side_effect=OSError("temporary cleanup failure"))
    with pytest.raises(OSError):
        transfer.upload()
    assert transfer.dest.writes == 1 and len(receipts(transfer)) == 1
    transfer.source.delete = cleanup
    assert transfer.upload()["status"] == "uploaded" and transfer.source.data is None
    assert transfer.dest.writes == 1


def test_retry_restage_is_cleaned_without_publishing_twice(transfer):
    data = transfer.source.data
    transfer.upload()
    transfer.source.data, transfer.source.generation = data, 9
    assert transfer.upload()["status"] == "uploaded"
    assert transfer.source.deletes == [7, 9] and transfer.dest.writes == 1


def test_real_storage_sdk_serializes_rewrite_preconditions_and_recovers_lost_response(
    transfer, monkeypatch
):
    from urllib.parse import parse_qs, unquote, urlsplit

    import requests
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    approve_existing(transfer)
    source = {
        "name": transfer.source.name,
        "generation": "7",
        "size": "16",
        "crc32c": "new-crc",
        "timeCreated": "2026-10-01T00:00:00Z",
    }
    current = {
        "name": transfer.dest.name,
        "generation": "7",
        "size": "10",
        "crc32c": "old-crc",
        "timeCreated": "2026-10-01T00:00:00Z",
        "metadata": {},
    }
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        if method == "GET":
            assert "generation" not in query, "Always inspect the current destination"
            body = source if "/b/artifacts/" in parsed.path else current
        elif method == "POST":
            assert "/rewriteTo/" in unquote(parsed.path)
            assert query["ifGenerationMatch"] == ["7"]
            assert query["ifSourceGenerationMatch"] == ["7"]
            metadata = json.loads(kwargs["data"])
            assert "crc32c" not in metadata, "Never reuse a loaded destination checksum"
            current.update(
                generation="8",
                size=source["size"],
                crc32c=source["crc32c"],
                metadata=metadata["metadata"],
            )
            raise PreconditionFailed("copy completed before acknowledgement")
        else:
            assert method == "DELETE" and "/b/artifacts/" in parsed.path
            assert query["ifGenerationMatch"] == ["7"]
            assert receipts(transfer), "Record publication before deleting the staged copy"
            body = {}
        response._content = json.dumps(body).encode()
        return response

    session = requests.Session()
    monkeypatch.setattr(session, "is_mtls", False, raising=False)
    monkeypatch.setattr(session, "request", request)
    client = storage.Client(project="test", credentials=AnonymousCredentials(), _http=session)
    transfer.store.bucket = client.bucket("artifacts")
    monkeypatch.setattr(
        write_broker,
        "destination_blob",
        lambda *args, **kwargs: client.bucket("lab-data").blob(transfer.dest.name),
    )
    assert transfer.upload()["status"] == "uploaded"
    assert calls == ["GET", "GET", "POST", "GET", "DELETE"]
    assert transfer.upload()["status"] == "uploaded"
    assert calls.count("POST") == 1
    session.close()


def test_long_copy_refreshes_impersonated_and_downscoped_credentials(monkeypatch):
    import google.auth
    import requests
    from google.auth import _helpers, downscoped
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import storage
    from google.oauth2.credentials import Credentials

    current = [datetime(2026, 10, 13)]
    monkeypatch.setattr(_helpers, "utcnow", lambda: current[0])
    source = Credentials("test-source", expiry=current[0] + timedelta(hours=1))
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (source, "test-project"))
    minted, headers = [], []

    def mint(url, **kwargs):
        if "iamcredentials" in url:
            minted.append("impersonated")
            data = {
                "accessToken": "test-writer",
                "expireTime": (current[0] + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        else:
            assert "sts.googleapis.com" in url
            minted.append("downscoped")
            data = {"access_token": f"test-scoped-{len(minted)}", "expires_in": 300}
        return SimpleNamespace(status=200, data=json.dumps(data).encode(), headers={})

    credentials = []

    def client(**kwargs):
        credentials.append(kwargs["credentials"])
        return SimpleNamespace(bucket=lambda name: SimpleNamespace(blob=lambda path: None))

    def transport(self, method, url, **kwargs):
        headers.append(kwargs["headers"]["authorization"])
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(storage, "Client", client)
    monkeypatch.setattr(requests.Session, "request", transport)
    write_broker.destination_blob(
        SimpleNamespace(write_service_account="writer@test"),
        "gs://data/target",
        source_uri="gs://artifacts/staged",
    )
    assert isinstance(credentials[0], downscoped.Credentials)
    with AuthorizedSession(credentials[0], auth_request=mint) as session:
        for _ in range(5):
            session.request("POST", "https://storage.googleapis.com/rewrite-test")
            current[0] += timedelta(seconds=120)
    assert minted.count("impersonated") > 1 and minted.count("downscoped") > 1
    assert len(set(headers)) > 1
