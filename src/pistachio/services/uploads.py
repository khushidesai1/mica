"""Quarantine, validate, checksum, and promote small channel uploads."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import zipfile
from pathlib import Path

from fastapi import UploadFile

from pistachio.config import Settings
from pistachio.models import AttachmentRef


class UploadRejected(ValueError):
    pass


def attachment_tag(
    settings: Settings, principal_id: str, uri: str, checksum: str, generation: str
) -> str:
    secret = settings.signed_link_secret or "local-development-only"
    message = f"{principal_id.lower()}\n{uri}\n{checksum}\n{generation}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class UploadService:
    ARCHIVE_MEMBER_LIMIT = 10_000
    ARCHIVE_EXPANSION_LIMIT = 2 * 1024 * 1024 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def accept(self, upload: UploadFile, principal_id: str) -> AttachmentRef:
        safe_name = Path(upload.filename or "upload.bin").name
        if safe_name in {"", ".", ".."}:
            raise UploadRejected("invalid filename")
        quarantine = self.settings.artifact_root / "quarantine"
        promoted = self.settings.artifact_root / "uploads"
        quarantine.mkdir(parents=True, exist_ok=True)
        promoted.mkdir(parents=True, exist_ok=True)
        temporary = quarantine / f"{hashlib.sha256(safe_name.encode()).hexdigest()}-{safe_name}"
        digest = hashlib.sha256()
        size = 0
        with temporary.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.upload_limit_bytes:
                    temporary.unlink(missing_ok=True)
                    raise UploadRejected("upload exceeds the 100 MB channel limit")
                digest.update(chunk)
                handle.write(chunk)
        self._validate_archive(temporary)
        checksum = digest.hexdigest()
        if self.settings.gcs_bucket:
            uri, generation = await asyncio.to_thread(
                self._promote_to_gcs, temporary, checksum, safe_name
            )
            temporary.unlink(missing_ok=True)
            return AttachmentRef(
                filename=safe_name,
                media_type=upload.content_type or "application/octet-stream",
                size_bytes=size,
                uri=uri,
                checksum_sha256=checksum,
                generation=generation,
                quarantined=False,
                authorization_tag=attachment_tag(
                    self.settings, principal_id, uri, checksum, generation
                ),
            )
        target = promoted / f"{checksum}-{safe_name}"
        temporary.replace(target)
        uri = target.resolve().as_uri()
        return AttachmentRef(
            filename=safe_name,
            media_type=upload.content_type or "application/octet-stream",
            size_bytes=size,
            uri=uri,
            checksum_sha256=checksum,
            quarantined=False,
            authorization_tag=attachment_tag(self.settings, principal_id, uri, checksum, ""),
        )

    def _promote_to_gcs(self, path: Path, checksum: str, safe_name: str) -> tuple[str, str]:
        from google.api_core.exceptions import PreconditionFailed
        from google.cloud.storage import Client

        client = Client(project=self.settings.gcp_project or None)
        quarantine_bucket = self.settings.gcs_quarantine_bucket or self.settings.gcs_bucket
        quarantine_blob = client.bucket(quarantine_bucket).blob(f"incoming/{checksum}-{safe_name}")
        try:
            quarantine_blob.upload_from_filename(path, if_generation_match=0)
        except PreconditionFailed:
            quarantine_blob.reload()
        destination = client.bucket(self.settings.gcs_bucket)
        promoted_name = f"inputs/{checksum}/{safe_name}"
        try:
            promoted = destination.copy_blob(
                quarantine_blob,
                destination,
                new_name=promoted_name,
                if_generation_match=0,
            )
        except PreconditionFailed:
            promoted = destination.blob(promoted_name)
            promoted.reload()
        quarantine_blob.delete(if_generation_match=quarantine_blob.generation)
        return f"gs://{self.settings.gcs_bucket}/{promoted_name}", str(promoted.generation)

    def _validate_archive(self, path: Path) -> None:
        if not zipfile.is_zipfile(path):
            return
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > self.ARCHIVE_MEMBER_LIMIT:
                path.unlink(missing_ok=True)
                raise UploadRejected("archive contains too many members")
            total = sum(info.file_size for info in infos)
            if total > self.ARCHIVE_EXPANSION_LIMIT:
                path.unlink(missing_ok=True)
                raise UploadRejected("archive expansion exceeds the validation limit")
            for info in infos:
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    path.unlink(missing_ok=True)
                    raise UploadRejected("archive contains an unsafe path")
