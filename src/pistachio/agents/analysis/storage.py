from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def get(self, key: str) -> tuple[bytes, str]: ...
    def put(self, key: str, data: bytes) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...
    def upload(self, key: str, path: Path) -> None: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid storage key")
        return path

    def get(self, key: str) -> tuple[bytes, str]:
        data = self.path(key).read_bytes()
        return data, hashlib.sha256(data).hexdigest()

    def put(self, key: str, data: bytes) -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def keys(self, prefix: str) -> list[str]:
        return [str(p.relative_to(self.root)) for p in self.path(prefix).rglob("*") if p.is_file()]

    def upload(self, key: str, path: Path) -> None:
        import shutil

        destination = self.path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


class GCSStorage:
    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self.bucket = storage.Client().bucket(bucket)

    def get(self, key: str) -> tuple[bytes, str]:
        from google.api_core.exceptions import NotFound

        blob = self.bucket.blob(key)
        try:
            blob.reload()
            return bytes(blob.download_as_bytes(if_generation_match=blob.generation)), str(
                blob.generation
            )
        except NotFound as exc:
            raise FileNotFoundError(key) from exc

    def put(self, key: str, data: bytes) -> None:
        self.bucket.blob(key).upload_from_string(data)

    def keys(self, prefix: str) -> list[str]:
        return [blob.name for blob in self.bucket.list_blobs(prefix=prefix)]

    def upload(self, key: str, path: Path) -> None:
        self.bucket.blob(key).upload_from_filename(str(path))
