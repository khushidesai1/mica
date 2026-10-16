"""Prepare task artifacts for consistent web and channel delivery."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from mica.config import Settings
from mica.models import ArtifactRef, TaskRecord

INDIVIDUAL_ARTIFACT_LIMIT = 4


class ArtifactUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryFile:
    path: Path
    name: str
    media_type: str
    artifact_count: int = 1


@dataclass
class PreparedArtifactDelivery:
    root: Path
    files: list[DeliveryFile]

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def artifact_media_type(artifact: ArtifactRef) -> str:
    if artifact.media_type != "application/octet-stream":
        return artifact.media_type
    return mimetypes.guess_type(artifact.name)[0] or artifact.media_type


def is_figure_artifact(artifact: ArtifactRef) -> bool:
    media_type = artifact_media_type(artifact)
    return media_type.startswith("image/") or Path(artifact.name).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
    }


def should_bundle_artifacts(artifacts: list[ArtifactRef]) -> bool:
    """Bundle only when the task produced more than four figures."""
    return sum(is_figure_artifact(artifact) for artifact in artifacts) > INDIVIDUAL_ARTIFACT_LIMIT


def delivery_manifest(task: TaskRecord) -> list[dict[str, object]]:
    artifacts = task.result.artifacts if task.result else []
    if should_bundle_artifacts(artifacts):
        return [
            {
                "name": f"{task.public_name}-artifacts.zip",
                "media_type": "application/zip",
                "artifact_count": len(artifacts),
                "bundled": True,
                "download_url": f"/api/v1/tasks/{task.id}/artifacts/archive",
            }
        ]
    return [
        {
            "name": artifact.name,
            "media_type": artifact_media_type(artifact),
            "artifact_count": 1,
            "bundled": False,
            "download_url": f"/api/v1/tasks/{task.id}/artifacts/{index}/download",
        }
        for index, artifact in enumerate(artifacts)
    ]


async def prepare_artifact_delivery(
    task: TaskRecord, settings: Settings
) -> PreparedArtifactDelivery:
    return await asyncio.to_thread(_prepare_artifact_delivery, task, settings)


async def prepare_individual_artifact(
    artifact: ArtifactRef, settings: Settings
) -> PreparedArtifactDelivery:
    return await asyncio.to_thread(_prepare_individual_artifact, artifact, settings)


def _prepare_artifact_delivery(
    task: TaskRecord, settings: Settings
) -> PreparedArtifactDelivery:
    artifacts = task.result.artifacts if task.result else []
    root = Path(tempfile.mkdtemp(prefix="mica-artifacts-"))
    try:
        if should_bundle_artifacts(artifacts):
            archive_name = f"{task.public_name}-artifacts.zip"
            archive_path = root / archive_name
            used_names: set[str] = set()
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, artifact in enumerate(artifacts):
                    name = _unique_name(_safe_name(artifact.name, index), used_names)
                    staged = root / f"source-{index}"
                    _materialize(artifact, staged, settings)
                    archive.write(staged, arcname=name)
            return PreparedArtifactDelivery(
                root=root,
                files=[
                    DeliveryFile(
                        path=archive_path,
                        name=archive_name,
                        media_type="application/zip",
                        artifact_count=len(artifacts),
                    )
                ],
            )

        files: list[DeliveryFile] = []
        for index, artifact in enumerate(artifacts):
            name = _safe_name(artifact.name, index)
            destination = root / f"{index}-{name}"
            _materialize(artifact, destination, settings)
            files.append(
                DeliveryFile(
                    path=destination,
                    name=name,
                    media_type=artifact_media_type(artifact),
                )
            )
        return PreparedArtifactDelivery(root=root, files=files)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _prepare_individual_artifact(
    artifact: ArtifactRef, settings: Settings
) -> PreparedArtifactDelivery:
    root = Path(tempfile.mkdtemp(prefix="mica-artifact-"))
    try:
        name = _safe_name(artifact.name, 0)
        destination = root / name
        _materialize(artifact, destination, settings)
        return PreparedArtifactDelivery(
            root=root,
            files=[
                DeliveryFile(
                    path=destination,
                    name=name,
                    media_type=artifact_media_type(artifact),
                )
            ],
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _materialize(artifact: ArtifactRef, destination: Path, settings: Settings) -> None:
    parsed = urlparse(artifact.uri)
    if parsed.scheme == "file":
        source = Path(unquote(parsed.path))
        if not source.is_file():
            raise ArtifactUnavailable(f"artifact is unavailable: {artifact.name}")
        shutil.copyfile(source, destination)
    elif parsed.scheme == "gs":
        if not parsed.netloc or not parsed.path.lstrip("/"):
            raise ArtifactUnavailable(f"artifact has an invalid storage URI: {artifact.name}")
        if parsed.netloc != settings.gcs_bucket:
            raise ArtifactUnavailable(
                f"artifact is outside the configured task-output bucket: {artifact.name}"
            )
        if not parsed.path.lstrip("/").startswith("tasks/"):
            raise ArtifactUnavailable(
                f"artifact is outside the managed task-output prefix: {artifact.name}"
            )
        from google.cloud.storage import Client

        client = Client(project=settings.gcp_project or None)
        blob = client.bucket(parsed.netloc).blob(parsed.path.lstrip("/"))
        blob.download_to_filename(destination)
    else:
        raise ArtifactUnavailable(f"artifact uses an unsupported storage URI: {artifact.name}")

    if artifact.checksum_sha256:
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest != artifact.checksum_sha256:
            raise ArtifactUnavailable(f"artifact checksum failed: {artifact.name}")


def _safe_name(name: str, index: int) -> str:
    candidate = Path(name).name.strip()
    return candidate if candidate not in {"", ".", ".."} else f"artifact-{index + 1}"


def _unique_name(name: str, used_names: set[str]) -> str:
    if name not in used_names:
        used_names.add(name)
        return name
    path = Path(name)
    counter = 2
    while True:
        candidate = f"{path.stem}-{counter}{path.suffix}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1
