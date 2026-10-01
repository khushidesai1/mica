from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from google.api_core.exceptions import PreconditionFailed

from pistachio.execution.publication import verify_cloud_commit
from pistachio.models import (
    ArtifactRef,
    ExecutionBundle,
    ResourcePlan,
    TaskContract,
    TaskResult,
    ToolPlan,
)
from pistachio.worker.main import _publish_protocol2


class MemoryBucket:
    name = "test-bucket"

    def __init__(self):
        self.data = {}
        self.metadata = {}
        self.fail_key = ""

    def blob(self, key):
        bucket = self

        class Blob:
            def __init__(self):
                self.metadata = None

            def exists(self):
                return key in bucket.data

            def reload(self):
                self.metadata = bucket.metadata.get(key, {})

            def upload_from_string(self, data, **kwargs):
                if key == bucket.fail_key:
                    bucket.fail_key = ""
                    raise OSError("simulated interrupted upload")
                if key in bucket.data:
                    raise PreconditionFailed("object exists")
                bucket.data[key] = data.encode() if isinstance(data, str) else data
                bucket.metadata[key] = self.metadata

            def upload_from_filename(self, path, **kwargs):
                self.upload_from_string(Path(path).read_bytes(), **kwargs)

            def download_to_filename(self, path):
                Path(path).write_bytes(bucket.data[key])

            def download_as_text(self):
                return bucket.data[key].decode()

            def download_as_bytes(self):
                return bucket.data[key]

        return Blob()


def test_publication_recovers_snapshot_and_commits_last(monkeypatch, tmp_path):
    bucket = MemoryBucket()

    class Client:
        def __init__(self, **kwargs):
            pass

        def bucket(self, name):
            assert name == bucket.name
            return bucket

    monkeypatch.setattr("pistachio.worker.main.StorageClient", Client)
    monkeypatch.setenv("PISTACHIO_GCS_BUCKET", bucket.name)
    monkeypatch.delenv("PISTACHIO_COMPLETION_TOPIC", raising=False)
    bundle = ExecutionBundle(
        builder_protocol_version=2,
        task_id="task_test",
        operation_id="op-1",
        source_hash="source",
        script="",
        contract=TaskContract(objective="x"),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
    )
    prefix = "tasks/task_test/attempt-1/"
    output = tmp_path / "outputs/group/summary.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"answer": 42}')
    artifact = ArtifactRef(
        name="group/summary.json",
        uri=f"gs://test-bucket/{prefix}outputs/group/summary.json",
        checksum_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
    )
    result = TaskResult(
        summary="validated",
        operation_id="op-1",
        source_hash="source",
        exit_code=0,
        artifacts=[artifact],
        publication_committed=True,
    )
    (tmp_path / "worker-result.json").write_text(result.model_dump_json(indent=2))
    bucket.fail_key = prefix + "outputs/group/summary.json"
    with pytest.raises(OSError, match="interrupted"):
        _publish_protocol2(bundle, tmp_path)
    assert prefix + "publication-snapshot.zip" in bucket.data
    assert prefix + "publication-commit.json" not in bucket.data
    # No rerun is needed and a new worker has no prior files.
    resumed = tmp_path / "fresh-worker"
    resumed.mkdir()
    recovered = _publish_protocol2(bundle, resumed)
    assert recovered == result
    verify_cloud_commit(bucket, bundle.task_id, 1, recovered)
    assert _publish_protocol2(bundle, resumed) == result
    assert json.loads(bucket.data[prefix + "outputs/group/summary.json"]) == {"answer": 42}


def test_missing_commit_or_wrong_artifact_blocks_completion():
    bucket = MemoryBucket()
    result = TaskResult(summary="x", operation_id="op", exit_code=0)
    with pytest.raises(KeyError):
        verify_cloud_commit(bucket, "task", 1, result)
