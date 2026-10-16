"""Batch worker entrypoint for immutable execution bundles."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from google.cloud.pubsub_v1 import PublisherClient
from google.cloud.storage import Client as StorageClient

from mica.compat import getenv
from mica.config import Settings
from mica.execution.backends import LocalExecutor
from mica.models import ExecutionBundle, TaskProgress, TaskResult


def download_bundle(uri: str) -> ExecutionBundle:
    if uri.startswith("gs://"):
        bucket_name, _, blob_name = uri.removeprefix("gs://").partition("/")
        payload = StorageClient().bucket(bucket_name).blob(blob_name).download_as_text()
    else:
        payload = Path(uri).read_text(encoding="utf-8")
    return ExecutionBundle.model_validate(json.loads(payload))


async def execute_bundle(uri: str, *, cache_root: Path | None = None) -> int:
    bundle = download_bundle(uri)
    with tempfile.TemporaryDirectory(prefix="mica-worker-") as workdir:
        settings = Settings(
            env="test",
            execution_backend="local",
            artifact_root=Path(workdir),
            batch_image=getenv('MICA_BATCH_IMAGE', ''),
            worker_sandbox="bubblewrap",
            worker_input_cache=cache_root,
        )

        async def publish_progress(progress: TaskProgress) -> None:
            await asyncio.to_thread(_publish_progress, bundle, progress)

        attempt_dir = Path(workdir) / bundle.task_id / f"attempt-{bundle.attempt}"
        bucket_name = getenv('MICA_GCS_BUCKET', '')
        if bundle.builder_protocol_version == 2 and bucket_name:
            snapshot = (
                StorageClient()
                .bucket(bucket_name)
                .blob(f"tasks/{bundle.task_id}/attempt-{bundle.attempt}/publication-snapshot.zip")
            )
            if await asyncio.to_thread(snapshot.exists):
                attempt_dir.mkdir(parents=True, exist_ok=True)
                recovered = await asyncio.to_thread(_publish_protocol2, bundle, attempt_dir)
                return recovered.exit_code or 0
        if bundle.workflow_version == 2:
            await publish_progress(
                TaskProgress(
                    attempt=bundle.attempt,
                    operation_id=bundle.operation_id,
                    trusted=True,
                    status="started",
                    message="Worker ready",
                    item="worker_start",
                )
            )
        result = await LocalExecutor(settings).execute(
            bundle,
            progress_callback=publish_progress,
        )
        bucket_name = getenv('MICA_GCS_BUCKET', '')
        if bucket_name and bundle.builder_protocol_version == 2:
            from urllib.parse import unquote, urlparse

            for artifact in result.artifacts + result.audit_artifacts:
                relative = Path(unquote(urlparse(artifact.uri).path)).relative_to(attempt_dir)
                artifact.uri = (
                    f"gs://{bucket_name}/tasks/{bundle.task_id}/attempt-{bundle.attempt}/{relative}"
                )
        elif bucket_name:
            prefix = f"tasks/{bundle.task_id}/attempt-{bundle.attempt}/outputs"
            for artifact in result.artifacts:
                artifact.uri = f"gs://{bucket_name}/{prefix}/{artifact.name}"
        output = Path(workdir) / bundle.task_id / f"attempt-{bundle.attempt}" / "worker-result.json"
        output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if bundle.builder_protocol_version == 2 and bucket_name:
            result = await asyncio.to_thread(_publish_protocol2, bundle, output.parent)
        else:
            await asyncio.to_thread(_publish_outputs, bundle, output.parent, result.exit_code)
        print(result.model_dump_json())
        return result.exit_code or 0


def _publish_outputs(bundle: ExecutionBundle, attempt_dir: Path, exit_code: int | None) -> None:
    bucket_name = getenv('MICA_GCS_BUCKET', '')
    project = getenv('MICA_GCP_PROJECT', '')
    topic = getenv('MICA_COMPLETION_TOPIC', '')
    if bucket_name:
        bucket = StorageClient(project=project or None).bucket(bucket_name)
        publishable = [attempt_dir / "worker-result.json"]
        publishable.extend(
            sorted(path for path in (attempt_dir / "outputs").rglob("*") if path.is_file())
        )
        for path in publishable:
            relative = path.relative_to(attempt_dir)
            bucket.blob(
                f"tasks/{bundle.task_id}/attempt-{bundle.attempt}/{relative}"
            ).upload_from_filename(path, if_generation_match=0)
    if topic:
        topic_path = (
            topic if topic.startswith("projects/") else f"projects/{project}/topics/{topic}"
        )
        PublisherClient().publish(
            topic_path,
            json.dumps(
                {
                    "kind": "completion",
                    "task_id": bundle.task_id,
                    "attempt": bundle.attempt,
                    "exit_code": exit_code,
                }
            ).encode(),
        ).result(timeout=30)


def _publish_progress(bundle: ExecutionBundle, progress: TaskProgress) -> None:
    progress.worker_session_id = getenv('MICA_WORKER_SESSION_ID', '')
    project = getenv('MICA_GCP_PROJECT', '')
    topic = getenv('MICA_COMPLETION_TOPIC', '')
    if not topic:
        return
    topic_path = topic if topic.startswith("projects/") else f"projects/{project}/topics/{topic}"
    PublisherClient().publish(
        topic_path,
        json.dumps(
            {
                "kind": "progress",
                "task_id": bundle.task_id,
                "attempt": bundle.attempt,
                "progress": progress.model_dump(mode="json"),
            }
        ).encode(),
    ).result(timeout=30)


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle")
    parser.add_argument("--session-task")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    if args.session_task and args.session_id:
        from mica.worker.session import supervise

        asyncio.run(
            supervise(
                args.session_task,
                args.session_id,
                int(getenv('MICA_SESSION_IDLE_SECONDS', '600')),
            )
        )
    elif args.bundle:
        raise SystemExit(asyncio.run(execute_bundle(args.bundle)))
    else:
        parser.error("bundle or session task/id is required")


def _publish_protocol2(bundle: ExecutionBundle, attempt_dir: Path) -> TaskResult:
    """Recover publication from one atomically uploaded snapshot, then commit files last."""
    import hashlib
    import zipfile

    from google.api_core.exceptions import PreconditionFailed

    from mica.execution.workspace import digest, safe_relative

    publication_started = time.monotonic()
    bucket_name = getenv('MICA_GCS_BUCKET')
    bucket = StorageClient(project=getenv('MICA_GCP_PROJECT', '') or None).bucket(bucket_name)
    prefix = f"tasks/{bundle.task_id}/attempt-{bundle.attempt}/"
    snapshot_blob = bucket.blob(prefix + "publication-snapshot.zip")
    snapshot_path = attempt_dir / "publication-snapshot.zip"
    bundle_hash = digest(bundle.model_dump(mode="json"))
    if not snapshot_blob.exists():
        from urllib.parse import urlparse

        result_to_publish = TaskResult.model_validate_json(
            (attempt_dir / "worker-result.json").read_text()
        )
        paths = [attempt_dir / "worker-result.json"]
        for artifact in result_to_publish.artifacts + result_to_publish.audit_artifacts:
            key = urlparse(artifact.uri).path.lstrip("/")
            if not key.startswith(prefix):
                raise ValueError("artifact is outside its operation prefix")
            paths.append(attempt_dir / safe_relative(key.removeprefix(prefix)))
        with zipfile.ZipFile(snapshot_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                if path.is_symlink():
                    raise ValueError("publication snapshot cannot contain symlinks")
                archive.write(path, path.relative_to(attempt_dir).as_posix())
        snapshot_blob.metadata = {"bundle_hash": bundle_hash}
        # Another delivery may already have established the authoritative snapshot.
        with suppress(PreconditionFailed):
            snapshot_blob.upload_from_filename(snapshot_path, if_generation_match=0)
    snapshot_blob.reload()
    if (snapshot_blob.metadata or {}).get("bundle_hash") != bundle_hash:
        raise ValueError("publication snapshot belongs to a different operation")
    snapshot_blob.download_to_filename(snapshot_path)
    checksums: dict[str, str] = {}
    with zipfile.ZipFile(snapshot_path) as archive:
        for item in archive.infolist():
            relative = safe_relative(item.filename)
            if not (
                relative == "worker-result.json"
                or relative.split("/")[0] in {"outputs", "evidence", "delivery", "checkpoints"}
            ):
                raise ValueError("unexpected publication snapshot member")
            data = archive.read(item)
            checksum = hashlib.sha256(data).hexdigest()
            checksums[relative] = checksum
            blob = bucket.blob(prefix + relative)
            blob.metadata = {"sha256": checksum, "operation_id": bundle.operation_id}
            try:
                blob.upload_from_string(data, if_generation_match=0)
            except PreconditionFailed:
                if hashlib.sha256(blob.download_as_bytes()).hexdigest() != checksum:
                    raise ValueError("immutable publication object differs") from None
        result = TaskResult.model_validate_json(archive.read("worker-result.json"))
    commit = json.dumps(
        {
            "operation_id": bundle.operation_id,
            "bundle_hash": bundle_hash,
            "checksums": checksums,
            "publication_seconds": time.monotonic() - publication_started,
        },
        sort_keys=True,
    )
    commit_blob = bucket.blob(prefix + "publication-commit.json")
    try:
        commit_blob.upload_from_string(
            commit, content_type="application/json", if_generation_match=0
        )
    except PreconditionFailed:
        existing_commit = json.loads(commit_blob.download_as_text())
        expected_commit = json.loads(commit)
        for item in (existing_commit, expected_commit):
            item.pop("publication_seconds", None)
        if existing_commit != expected_commit:
            raise ValueError("publication commit differs") from None
    topic = getenv('MICA_COMPLETION_TOPIC', '')
    if topic:
        project = getenv('MICA_GCP_PROJECT', '')
        topic_path = (
            topic if topic.startswith("projects/") else f"projects/{project}/topics/{topic}"
        )
        PublisherClient().publish(
            topic_path,
            json.dumps(
                {
                    "kind": "completion",
                    "task_id": bundle.task_id,
                    "attempt": bundle.attempt,
                    "operation_id": bundle.operation_id,
                    "exit_code": result.exit_code,
                }
            ).encode(),
        ).result(timeout=30)
    return result


if __name__ == "__main__":
    run()
