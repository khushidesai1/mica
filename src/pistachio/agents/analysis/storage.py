from __future__ import annotations

import fcntl
import hashlib
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from pydantic import BaseModel

_LOCAL_LOCK = threading.RLock()


class Storage(Protocol):
    def get(self, key: str) -> tuple[bytes, str]: ...
    def put(self, key: str, data: bytes) -> None: ...
    def put_if(self, key: str, data: bytes, generation: str | None) -> str: ...
    def keys(self, prefix: str) -> list[str]: ...
    def upload(self, key: str, path: Path) -> None: ...
    def open_write(self, key: str) -> AbstractContextManager[BinaryIO]: ...
    def download(self, key: str, path: Path) -> None: ...
    def read_prefix(self, key: str, limit: int) -> bytes: ...


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

    def put_if(self, key: str, data: bytes, generation: str | None) -> str:
        with _LOCAL_LOCK, (self.root / ".cas.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = self.get(key)[1]
            except FileNotFoundError:
                current = None
            if current != generation:
                raise Conflict("The object changed since it was read.")
            self.put(key, data)
            return hashlib.sha256(data).hexdigest()

    def keys(self, prefix: str) -> list[str]:
        return [str(p.relative_to(self.root)) for p in self.path(prefix).rglob("*") if p.is_file()]

    def upload(self, key: str, path: Path) -> None:
        destination = self.path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    @contextmanager
    def open_write(self, key: str) -> Iterator[BinaryIO]:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                yield cast(BinaryIO, stream)
                stream.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def download(self, key: str, path: Path) -> None:
        shutil.copyfile(self.path(key), path)

    def read_prefix(self, key: str, limit: int) -> bytes:
        with self.path(key).open("rb") as stream:
            return stream.read(limit)


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

    def put_if(self, key: str, data: bytes, generation: str | None) -> str:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(data, if_generation_match=int(generation) if generation else 0)
        except PreconditionFailed as exc:
            raise Conflict("The object changed since it was read.") from exc
        return str(blob.generation)

    def keys(self, prefix: str) -> list[str]:
        return [blob.name for blob in self.bucket.list_blobs(prefix=prefix)]

    def upload(self, key: str, path: Path) -> None:
        self.bucket.blob(key).upload_from_filename(str(path))

    def open_write(self, key: str) -> AbstractContextManager[BinaryIO]:
        # BlobWriter terminates (rather than finalizing) the upload on exceptional exit.
        return cast(
            AbstractContextManager[BinaryIO],
            self.bucket.blob(key).open("wb", chunk_size=8 * 1024 * 1024),
        )

    def download(self, key: str, path: Path) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self.bucket.blob(key).download_to_filename(str(path))
        except NotFound as exc:
            raise FileNotFoundError(key) from exc

    def read_prefix(self, key: str, limit: int) -> bytes:
        from google.api_core.exceptions import NotFound

        try:
            return bytes(self.bucket.blob(key).download_as_bytes(start=0, end=limit - 1))
        except NotFound as exc:
            raise FileNotFoundError(key) from exc


class Conflict(ValueError):
    """The object changed after the caller read it."""


def update_model[T: BaseModel](
    storage: Storage, key: str, model: type[T], change: Callable[[T], None], attempts: int = 5
) -> T:
    for _ in range(attempts):
        try:
            raw, generation = storage.get(key)
            doc = model.model_validate_json(raw)
        except FileNotFoundError:
            doc, generation = model(), None
        before = doc.model_dump_json()
        change(doc)
        data = doc.model_dump_json()
        if data == before:
            return doc
        try:
            storage.put_if(key, data.encode(), generation)
            return doc
        except Conflict:
            continue
    raise Conflict("This record changed concurrently. Please retry.")
