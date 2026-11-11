"""Exact operation approval, immutable inventories and copy-before-delete recovery."""

import copy
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed, ServiceUnavailable
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_write_approval import click
from test_analysis_write_flow import allow, writable  # noqa: F401

from mica.agents.analysis import conversation, write_access, write_approval
from mica.agents.analysis import gcs_operations as ops
from mica.agents.analysis.config import Settings
from mica.agents.analysis.storage import LocalStorage

ROOT = "gs://lab-data/liver/output/"
EVENT = "a" * 32
SNAPSHOT = "b" * 32


class Blob:
    def __init__(self, cloud, bucket, name):
        self.cloud, self.name = cloud, name
        self.bucket = SimpleNamespace(name=bucket, blob=lambda name: Blob(cloud, bucket, name))
        self.generation = None
        self.metageneration = None
        self.metadata = None
        self.size = self.crc32c = None

    @property
    def uri(self):
        return f"gs://{self.bucket.name}/{self.name}"

    def reload(self, **kwargs):
        if self.uri not in self.cloud.objects:
            raise NotFound("absent")
        self.__dict__.update(copy.deepcopy(self.cloud.objects[self.uri]))

    def rewrite(self, source, **kwargs):
        self.cloud.before_copy()
        item = self.cloud.objects[source.uri]
        if (
            kwargs["if_source_generation_match"] != item["generation"]
            or kwargs["if_source_metageneration_match"] != item["metageneration"]
            or self.uri in self.cloud.objects
        ):
            raise PreconditionFailed("changed")
        assert kwargs["if_generation_match"] == 0
        assert source.generation is None  # Never pin the source URL to a historical version.
        self.cloud.tokens.append(kwargs["token"])
        if self.cloud.chunked and kwargs["token"] is None:
            self.cloud.after_chunk()
            return "rewrite-token", 1, item["size"]
        self.cloud.seed(self.uri, item["data"], metadata=self.metadata)
        self.cloud.copies.append((source.uri, self.uri))
        self.cloud.after_copy()
        return None, item["size"], item["size"]

    def delete(self, **kwargs):
        self.cloud.before_delete()
        item = self.cloud.objects.get(self.uri)
        if not item:
            raise NotFound("absent")
        if (
            kwargs["if_generation_match"] != item["generation"]
            or kwargs["if_metageneration_match"] != item["metageneration"]
        ):
            raise PreconditionFailed("changed")
        assert self.generation is None  # Preserve soft delete / versioning semantics.
        self.cloud.deletes.append((self.uri, kwargs.copy()))
        del self.cloud.objects[self.uri]
        self.cloud.after_delete()


class Cloud:
    def __init__(self):
        self.objects, self.copies, self.deletes, self.tokens = {}, [], [], []
        self.credentials, self.list_calls = [], []
        self.generation, self.chunked = 0, False
        self.before_copy = self.after_copy = self.after_chunk = lambda: None
        self.before_delete = self.after_delete = lambda: None

    def seed(self, uri, data=b"sample", metadata=None):
        self.generation += 1
        self.objects[uri] = dict(
            data=data,
            generation=self.generation,
            metageneration=1,
            size=len(data),
            crc32c=hashlib.sha256(data).hexdigest(),
            metadata=metadata or {"scientific-source": "preserved"},
        )

    def bucket(self, name):
        return SimpleNamespace(name=name, blob=lambda key: Blob(self, name, key))

    def writer(self, settings, uri, **kwargs):
        self.credentials.append((uri, kwargs))
        target = urlsplit(uri)
        return self.bucket(target.netloc).blob(target.path.lstrip("/"))

    def list_blobs(self, bucket, **kwargs):
        self.list_calls.append(kwargs)
        prefix = f"gs://{bucket}/{kwargs['prefix']}"
        selected = sorted(uri for uri in self.objects if uri.startswith(prefix))
        start, limit = int(kwargs.get("page_token") or 0), kwargs["max_results"]
        blobs = []
        for uri in selected[start : start + limit]:
            target = urlsplit(uri)
            blob = self.bucket(bucket).blob(target.path.lstrip("/"))
            blob.reload()
            blobs.append(blob)
        token = str(start + limit) if start + limit < len(selected) else None

        class Listing:
            pages = iter([blobs])
            next_page_token = token

            def __iter__(self):
                return iter(blobs)

        return Listing()


@pytest.fixture
def cloud(monkeypatch):
    fake = Cloud()
    monkeypatch.setattr("google.cloud.storage.Client", lambda **kwargs: fake)
    monkeypatch.setattr(ops.write_broker, "destination_blob", fake.writer)
    return fake


@pytest.fixture
def operation_store(tmp_path):
    return LocalStorage(tmp_path)


def move(source="TMA1/", destination="TMA1_old/"):
    return ops.Operation(action="move", source=ROOT + source, destination=ROOT + destination)


def prepare(store, operations):
    manifest = ops.prepare(
        store, EVENT, SNAPSHOT, operations, Mock(), deadline=time.monotonic() + 120
    )
    return write_approval.Approval(
        event_id=EVENT,
        project_id="project",
        grant_id="c" * 32,
        principal="user@example.com",
        access_signature="access",
        paths=[],
        inspected_at=time.time(),
        expires_at=time.time() + 3600,
        approved_at=time.time(),
        status="approved",
        operations=operations,
        snapshot_id=SNAPSHOT,
        plan_ready=True,
        operation_counts=manifest["counts"],
    )


def execute(store, approval, authorized=None):
    return ops.execute(
        store, Settings(), approval, authorized or Mock(), deadline=time.monotonic() + 120
    )


def test_folder_move_preserves_layout_metadata_and_siblings(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/")  # Directory marker is included.
    cloud.seed(ROOT + "TMA1/data/part.csv", b"results")
    cloud.seed(ROOT + "TMA1_backup/keep.csv", b"untouched")
    approval = prepare(operation_store, [move()])
    assert not cloud.copies and not cloud.deletes
    text, _ = write_approval.blocks(approval, ROOT)
    assert "Move folder" in text and "TMA1/" in text and "TMA1_old/" in text
    assert "part.csv" not in text and "2 inspected objects" in text
    result = execute(operation_store, approval)
    assert result["done"] and result["completed"] == 2 and result["remaining"] == []
    assert set(cloud.objects) == {
        ROOT + "TMA1_old/",
        ROOT + "TMA1_old/data/part.csv",
        ROOT + "TMA1_backup/keep.csv",
    }
    assert (
        cloud.objects[ROOT + "TMA1_old/data/part.csv"]["metadata"]["scientific-source"]
        == "preserved"
    )
    assert all(args["if_generation_match"] > 0 for _, args in cloud.deletes)
    assert execute(operation_store, approval) == result
    assert len(cloud.copies) == len(cloud.deletes) == 2
    assert all(kwargs.get("overwrite") for uri, kwargs in cloud.credentials if "/TMA1/" in uri)
    assert all(
        not kwargs.get("overwrite") for uri, kwargs in cloud.credentials if "TMA1_old" in uri
    )


@pytest.mark.parametrize("collision", ["TMA1_old", "TMA1_old/", "TMA1_old/existing.csv"])
@pytest.mark.parametrize("timing", ["inspection", "execution"])
def test_occupied_move_destination_refused_before_any_mutation(
    operation_store, cloud, collision, timing
):
    cloud.seed(ROOT + "TMA1/a")
    if timing == "execution":
        approval = prepare(operation_store, [move()])
    cloud.seed(ROOT + collision)
    with pytest.raises(ValueError, match="destination already exists"):
        if timing == "inspection":
            prepare(operation_store, [move()])
        else:
            execute(operation_store, approval)
    assert not cloud.copies and not cloud.deletes


@pytest.mark.parametrize("changed", ["generation", "metageneration"])
def test_changed_source_is_preserved(operation_store, cloud, changed):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    cloud.objects[ROOT + "TMA1/a"][changed] += 100
    with pytest.raises(ValueError, match="Source changed"):
        execute(operation_store, approval)
    assert not cloud.copies and not cloud.deletes


def test_new_source_objects_are_not_deleted(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [ops.Operation(action="delete", source=ROOT + "TMA1/")])
    cloud.seed(ROOT + "TMA1/new")
    result = execute(operation_store, approval)
    assert result["remaining"] == [ROOT + "TMA1/"]
    assert list(cloud.objects) == [ROOT + "TMA1/new"]
    assert not cloud.copies


@pytest.mark.parametrize("where", ["after_copy", "after_delete"])
def test_lost_response_recovers_without_duplicate_copy_or_delete(operation_store, cloud, where):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    setattr(cloud, where, Mock(side_effect=ServiceUnavailable("lost response")))
    with pytest.raises(ServiceUnavailable):
        execute(operation_store, approval)
    setattr(cloud, where, lambda: None)
    assert execute(operation_store, approval)["done"]
    assert len(cloud.copies) == len(cloud.deletes) == 1


def test_copy_verification_failure_never_deletes_source(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    cloud.after_copy = lambda: cloud.objects[ROOT + "TMA1_old/a"].update(crc32c="bad")
    with pytest.raises(ValueError, match="could not be verified"):
        execute(operation_store, approval)
    assert ROOT + "TMA1/a" in cloud.objects and not cloud.deletes


def test_source_change_between_copy_and_delete_preserves_source(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    cloud.before_delete = lambda: cloud.seed(ROOT + "TMA1/a", b"new generation")
    with pytest.raises(ValueError, match="Source changed before deletion"):
        execute(operation_store, approval)
    assert cloud.objects[ROOT + "TMA1/a"]["data"] == b"new generation"
    assert not cloud.deletes


def test_revoked_authority_between_copy_and_delete_preserves_source(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    authorized = Mock()
    cloud.after_copy = lambda: setattr(authorized, "side_effect", PermissionError("revoked"))
    with pytest.raises(PermissionError, match="revoked"):
        execute(operation_store, approval, authorized)
    assert len(cloud.copies) == 1 and not cloud.deletes


def test_resumable_rewrite_persists_token(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    cloud.chunked = True
    cloud.after_chunk = lambda: None
    assert execute(operation_store, approval)["done"]
    assert cloud.tokens == [None, "rewrite-token"]


def test_inventory_pages_are_frozen_and_bounded(operation_store, cloud, monkeypatch):
    monkeypatch.setattr(ops, "PAGE_SIZE", 2)
    for n in range(5):
        cloud.seed(ROOT + f"TMA1/{n}")
    approval = prepare(operation_store, [move()])
    manifest, _ = ops.read(operation_store, ops.root(EVENT, SNAPSHOT) + "manifest.json")
    assert manifest["shards"] == 3 and approval.operation_counts == [5]
    assert all(call["max_results"] <= 2 for call in cloud.list_calls)
    assert execute(operation_store, approval)["completed"] == 5


@pytest.mark.parametrize(
    "source,destination",
    [
        (ROOT, ROOT + "new/"),
        (ROOT + "TMA1/", "gs://other/TMA1/"),
        (ROOT + "TMA1/", ROOT + "TMA1/inside/"),
        (ROOT + "TMA1/", ROOT + "TMA1/"),
        (ROOT + "TMA1/../", ROOT + "new/"),
        (ROOT + "TMA*/", ROOT + "new/"),
        (ROOT + "TMA1/", ROOT + "../new/"),
    ],
)
def test_invalid_scopes_and_overlaps_rejected(source, destination):
    grant = write_access.WriteGrant(
        id="c" * 32, uri=ROOT, actor="owner", admin_id="T:U", created_at=write_access.now()
    )
    with pytest.raises(ValueError):
        ops.normalized(
            [ops.Operation(action="move", source=source, destination=destination)], grant
        )


@pytest.fixture
def moving(writable, cloud, monkeypatch):  # noqa: F811
    cloud.seed(ROOT + "TMA1/a")
    monkeypatch.setattr(write_approval, "AsyncWebClient", lambda **kw: writable.test_slack)
    monkeypatch.setattr(
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
                route="storage",
                reply="Rename TMA1",
                write_operations=[move()],
            )
        ),
    )
    return writable


async def start(agent):
    await allow(agent)
    event = await agent.accept(payload("3.0", text="Rename TMA1 to TMA1_old"))
    assert await agent.process(event, 0) == "awaiting_approval"
    return event, await agent.repository.get_connector_state("claude-event:" + event)


async def test_slack_confirmation_executes_exact_move_without_worker(moving, cloud):
    event, record = await start(moving)
    assert not cloud.copies and not cloud.deletes
    card = moving.test_slack.chat_update.call_args.kwargs["text"]
    assert "Move folder" in card and ROOT + "TMA1_old/" in card
    wrong = click(event, record)
    wrong["user"]["id"] = "U2"
    with pytest.raises(ValueError, match="requester"):
        await write_approval.handle(moving, moving.store, wrong)
    await write_approval.handle(moving, moving.store, click(event, record))
    assert await moving.process(event, record["tick"]) == "complete"
    assert ROOT + "TMA1_old/a" in cloud.objects and ROOT + "TMA1/a" not in cloud.objects
    moving.test_client.create_job.assert_not_called()
    await write_approval.handle(moving, moving.store, click(event, record))
    assert len(cloud.copies) == 1


@pytest.mark.parametrize("change", ["revoke", "cancel", "operations", "membership"])
async def test_changed_authorization_or_operations_never_runs(moving, cloud, change):
    event, record = await start(moving)
    if change == "cancel":
        await write_approval.handle(moving, moving.store, click(event, record, "cancel"))
    else:
        await write_approval.handle(moving, moving.store, click(event, record))
        if change == "revoke":
            state = write_access.load(moving.store, moving.pid)
            state.grant = None
            moving.store.put(write_access.key(moving.pid), state.model_dump_json().encode())
        elif change == "membership":
            moving.app.slack_user_map["T1:U1"] = "other@example.com"
        else:
            saved = await moving.repository.get_connector_state("claude-event:" + event)
            saved["direct_decision"]["write_operations"][0]["destination"] = ROOT + "different/"
            await moving.repository.set_connector_state("claude-event:" + event, saved)
    assert await moving.process(event, record["tick"]) == "failed"
    assert not cloud.copies and not cloud.deletes


async def test_expiry_renewal_builds_fresh_inventory_and_requires_fresh_click(moving, cloud):
    event, record = await start(moving)
    approval, generation = write_approval.load(moving.store, event)
    old_snapshot = approval.snapshot_id
    approval.expires_at = 1
    ops.approvals.save(moving.store, write_approval.key(event), approval, generation)
    await write_approval.KIND.expire(moving, moving.store, event)
    cloud.seed(ROOT + "TMA1/new")
    await write_approval.handle(moving, moving.store, click(event, record, "again"))
    renewed = write_approval.load(moving.store, event)[0]
    assert renewed.status == "pending" and renewed.snapshot_id != old_snapshot
    assert renewed.operation_counts == [2]
    assert not cloud.copies and not cloud.deletes


def test_single_file_replacement_card_names_the_exact_file():
    approval = write_approval.Approval(
        event_id=EVENT,
        project_id="p",
        grant_id="c" * 32,
        principal="user",
        access_signature="signature",
        paths=[ROOT + "results.csv"],
        inspected_at=1,
        expires_at=100,
    )
    text, _ = write_approval.blocks(approval, ROOT)
    assert f"Replace file `{ROOT}results.csv`" in text
    assert "folder" not in text


@pytest.mark.parametrize("action", ["move", "delete"])
def test_single_file_operations_preserve_other_files(operation_store, cloud, action):
    cloud.seed(ROOT + "result.csv")
    cloud.seed(ROOT + "result.csv.bak", b"keep")
    operation = (
        move("result.csv", "result_old.csv")
        if action == "move"
        else ops.Operation(action="delete", source=ROOT + "result.csv")
    )
    approval = prepare(operation_store, [operation])
    text, _ = write_approval.blocks(approval, ROOT)
    assert f"{action.title()} file" in text and "folder `" not in text
    assert execute(operation_store, approval)["done"]
    assert ROOT + "result.csv" not in cloud.objects
    assert cloud.objects[ROOT + "result.csv.bak"]["data"] == b"keep"


def test_rewrite_token_survives_controller_restart(operation_store, cloud, monkeypatch):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    clock = [0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    cloud.chunked = True
    cloud.after_chunk = lambda: clock.__setitem__(0, 100)
    first = ops.execute(operation_store, Settings(), approval, Mock(), deadline=120)
    assert not first["done"] and not cloud.copies and not cloud.deletes
    clock[0] = 0
    cloud.after_chunk = lambda: None
    second = ops.execute(operation_store, Settings(), approval, Mock(), deadline=120)
    assert second["done"] and cloud.tokens == [None, "rewrite-token"]


def test_mutation_cursor_resumes_after_time_slice(operation_store, cloud, monkeypatch):
    cloud.seed(ROOT + "TMA1/a")
    cloud.seed(ROOT + "TMA1/b")
    approval = prepare(operation_store, [move()])
    clock = [0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    cloud.after_delete = lambda: clock.__setitem__(0, 100)
    first = ops.execute(operation_store, Settings(), approval, Mock(), deadline=120)
    assert first["completed"] == 1 and not first["done"]
    clock[0] = 0
    cloud.after_delete = lambda: None
    assert ops.execute(operation_store, Settings(), approval, Mock(), deadline=120)["done"]
    assert len(cloud.copies) == len(cloud.deletes) == 2


def test_destination_change_after_copy_blocks_retry_delete(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    cloud.before_delete = Mock(side_effect=ServiceUnavailable("interrupted"))
    with pytest.raises(ServiceUnavailable):
        execute(operation_store, approval)
    cloud.before_delete = lambda: None
    cloud.seed(ROOT + "TMA1_old/a", b"someone else's data")
    with pytest.raises(ValueError, match="destination exists or changed"):
        execute(operation_store, approval)
    assert not cloud.deletes and ROOT + "TMA1/a" in cloud.objects


def test_late_copy_collision_never_overwrites_or_deletes(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    approval = prepare(operation_store, [move()])
    cloud.before_copy = lambda: cloud.seed(ROOT + "TMA1_old/a", b"someone else's data")
    with pytest.raises(ValueError, match="changed during the move"):
        execute(operation_store, approval)
    assert cloud.objects[ROOT + "TMA1_old/a"]["data"] == b"someone else's data"
    assert not cloud.deletes


@pytest.mark.parametrize("source", ["TMA1", "TMA1/"])
def test_file_folder_ambiguity_refused(operation_store, cloud, source):
    cloud.seed(ROOT + "TMA1")
    cloud.seed(ROOT + "TMA1/a")
    with pytest.raises(ValueError, match="both a file and a folder"):
        prepare(operation_store, [ops.Operation(action="delete", source=ROOT + source)])
    assert not cloud.deletes


async def test_no_write_grant_cannot_create_confirmation_or_mutate(moving, cloud):
    event = await moving.accept(payload("3.0", text="Rename TMA1 to TMA1_old"))
    assert await moving.process(event, 0) == "failed"
    assert "active project write grant" in moving.test_slack.chat_update.call_args.kwargs["text"]
    assert not write_approval.load(moving.store, event)
    assert not cloud.copies and not cloud.deletes


async def test_cancellation_record_is_rechecked_after_copy(moving, cloud):
    from mica.agents.analysis.config import CHECKPOINT_PREFIX

    event, record = await start(moving)
    await write_approval.handle(moving, moving.store, click(event, record))
    cloud.after_copy = lambda: moving.store.put(
        f"{CHECKPOINT_PREFIX}/events/{event}/memory-cancelled.json", b"true"
    )
    assert await moving.process(event, record["tick"]) == "failed"
    assert len(cloud.copies) == 1 and not cloud.deletes


async def test_email_and_background_mutations_are_denied(moving, cloud):
    moving.notify = AsyncMock()
    for flags in [{"surface": "email"}, {"ambient": {"kind": "analysis"}}]:
        record = {"route": "storage", **flags}
        await ops.advance(moving, EVENT, record, moving.store)
        assert record["status"] == "failed"
    assert not cloud.copies and not cloud.deletes


def test_inventory_cannot_expand_confirmed_scope(operation_store, cloud):
    cloud.seed(ROOT + "TMA1/a")
    cloud.seed(ROOT + "private/a")
    approval = prepare(operation_store, [move()])
    shard_path = ops.root(EVENT, SNAPSHOT) + "shards/00000000.json"
    shard, revision = ops.read(operation_store, shard_path)
    shard["entries"][0]["source"] = ROOT + "private/a"
    ops.approvals.save(operation_store, shard_path, shard, revision)
    with pytest.raises(ValueError, match="does not match the confirmed operation"):
        execute(operation_store, approval)
    assert not cloud.credentials and not cloud.copies and not cloud.deletes


def test_real_sdk_uses_live_source_and_conditional_delete(operation_store, monkeypatch):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.storage import Client

    client = Client(project="test", credentials=AnonymousCredentials())
    source = dict(
        name="source.csv",
        bucket="bucket",
        generation="5",
        metageneration="1",
        size="3",
        crc32c="checksum",
        metadata={"scientific-source": "keep"},
    )
    destination = None
    calls = []

    def api_request(**kwargs):
        nonlocal source, destination
        calls.append(kwargs)
        method, path = kwargs["method"], kwargs["path"]
        if method == "POST":
            assert "/rewriteTo/" in path
            assert kwargs["query_params"]["ifGenerationMatch"] == 0
            assert kwargs["query_params"]["ifSourceGenerationMatch"] == 5
            assert kwargs["query_params"]["ifSourceMetagenerationMatch"] == 1
            assert "sourceGeneration" not in kwargs["query_params"]
            assert "crc32c" not in kwargs["data"]
            destination = {**source, **kwargs["data"], "name": "destination.csv", "generation": "6"}
            return {
                "done": True,
                "resource": destination,
                "totalBytesRewritten": "3",
                "objectSize": "3",
            }
        if method == "DELETE":
            assert path.endswith("/source.csv")
            assert "generation" not in kwargs["query_params"]
            assert kwargs["query_params"]["ifGenerationMatch"] == 5
            assert kwargs["query_params"]["ifMetagenerationMatch"] == 1
            source = None
            return None
        value = source if path.endswith("/source.csv") else destination
        if value is None:
            raise NotFound("absent")
        return copy.deepcopy(value)

    monkeypatch.setattr(client._connection, "api_request", api_request)
    monkeypatch.setattr(
        ops.write_broker, "destination_blob", lambda settings, uri, **kw: ops.blob_at(client, uri)
    )
    entry = dict(
        source="gs://bucket/source.csv",
        destination="gs://bucket/destination.csv",
        generation=5,
        metageneration=1,
        size=3,
        crc32c="checksum",
    )
    assert ops.mutate(
        operation_store,
        Settings(),
        ops.root(EVENT, SNAPSHOT),
        "item",
        entry,
        Mock(),
        deadline=time.monotonic() + 120,
    )
    assert source is None and destination["metadata"]["scientific-source"] == "keep"
    assert [c["method"] for c in calls].count("POST") == 1
    assert [c["method"] for c in calls].count("DELETE") == 1
