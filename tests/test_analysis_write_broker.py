import hashlib
import io
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import NotFound
from test_analysis_write_access import STAMP, grants, run  # noqa: F401

from pistachio.agents.analysis import write_access, write_broker


class Blob:
    def __init__(self, name, data=None):
        self.name, self.data = name, data
        self.generation = 7
        self.metadata = None
        self.writes = 0
        self.aborts = 0
        self.on_chunk = lambda: None
        self.refresh()

    def refresh(self):
        self.size = len(self.data) if self.data is not None else None
        self.crc32c = hashlib.sha256(self.data).hexdigest() if self.data is not None else None

    def reload(self, **kwargs):
        if self.data is None:
            raise NotFound("missing")
        self.refresh()

    @contextmanager
    def open(self, mode, **kwargs):
        if mode == "rb":
            assert kwargs["if_generation_match"] == self.generation
            yield io.BytesIO(self.data)
            return
        assert kwargs["if_generation_match"] == 0
        self.writes += 1
        buffer = io.BytesIO()
        try:
            yield SimpleNamespace(write=lambda chunk: (buffer.write(chunk), self.on_chunk()))
        except Exception:
            self.aborts += 1
            raise
        else:
            assert self.data is None
            self.data = buffer.getvalue()
            self.refresh()


@pytest.fixture
def transfer(grants, monkeypatch):  # noqa: F811
    store, pid, settings = grants
    run(grants, "write grant liver gs://lab-data/liver/output/")
    event = "a" * 32
    grant = write_access.active(store, pid, settings)
    source = Blob(write_broker.staged_key(event, "nested/result.csv"), b"some-result-data")
    dest = Blob("liver/output/nested/result.csv")
    store.bucket = SimpleNamespace(blob=Mock(return_value=source))
    factory = Mock(return_value=dest)
    monkeypatch.setattr(write_broker, "destination_blob", factory)
    monkeypatch.setattr(write_broker, "CHUNK_SIZE", 4)
    authorize = Mock()

    def upload():
        return write_broker.upload(
            store, settings, pid, event, "nested/result.csv", grant.id, authorize
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


def test_publish_preserves_subpaths_is_create_only_and_retries_are_idempotent(transfer):
    result = transfer.upload()
    assert result["uri"] == "gs://lab-data/liver/output/nested/result.csv"
    assert transfer.dest.data == transfer.source.data
    assert transfer.upload() == result and transfer.dest.writes == 1
    assert transfer.authorize.call_count > 3
    assert transfer.store.keys("write-access/uploads")


def test_existing_unrelated_object_is_never_replaced(transfer):
    transfer.dest.data = b"important-original"
    with pytest.raises(FileExistsError, match="new filename"):
        transfer.upload()
    assert transfer.dest.data == b"important-original" and transfer.dest.writes == 0


@pytest.mark.parametrize("change", ["revoke", "replace", "expire", "membership"])
def test_rechecks_authority_during_streaming_and_aborts(transfer, monkeypatch, change):
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

    transfer.dest.on_chunk = update
    with pytest.raises(PermissionError):
        transfer.upload()
    assert transfer.dest.data is None and transfer.dest.aborts == 1
    assert not transfer.store.keys("write-access/uploads")


def test_rejected_before_credentials_are_minted(transfer):
    run(transfer.grants, "write revoke liver", ts=STAMP + 1)
    with pytest.raises(PermissionError):
        transfer.upload()
    transfer.factory.assert_not_called()


def test_size_limit_before_upload(transfer):
    transfer.settings.write_max_mb = 1
    transfer.source.data = b"x" * (1024 * 1024 + 1)
    with pytest.raises(ValueError, match="limited"):
        transfer.upload()
    transfer.factory.assert_not_called()


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


def test_cloud_credential_is_scoped_to_one_exact_object():
    cab = write_broker.boundary("gs://lab-data/liver/output/result.csv").to_json()
    rules = cab["accessBoundary"]["accessBoundaryRules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["availableResource"] == "//storage.googleapis.com/projects/_/buckets/lab-data"
    assert rule["availabilityCondition"]["expression"] == (
        'resource.name == "projects/_/buckets/lab-data/objects/liver/output/result.csv"'
    )
    assert rule["availablePermissions"] == [
        "inRole:roles/storage.objectCreator",
        "inRole:roles/storage.objectViewer",
    ]
