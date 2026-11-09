"""Network adapters confined to the transfer container."""

from __future__ import annotations

import base64
import json
import posixpath
import stat
from collections.abc import Iterator
from typing import Any, Literal

from .core import Busy, Object


class GCS:
    def __init__(self, bucket: str, credentials=None) -> None:
        from google.cloud import storage

        self.bucket = storage.Client(credentials=credentials).bucket(bucket)

    def read(self, name: str) -> tuple[dict[str, Any], str] | None:
        from google.api_core.exceptions import NotFound

        blob = self.bucket.blob(name)
        try:
            blob.reload()
            return json.loads(blob.download_as_bytes(if_generation_match=blob.generation)), str(
                blob.generation
            )
        except NotFound:
            return None

    def write(self, name: str, body: dict[str, Any], generation: str | None) -> str:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(name)
        try:
            blob.upload_from_string(
                json.dumps(body),
                content_type="application/json",
                if_generation_match=int(generation) if generation else 0,
            )
        except PreconditionFailed:
            raise Busy("Marker generation changed") from None
        return str(blob.generation)

    def delete(self, name: str, generation: str) -> None:
        self.bucket.blob(name).delete(if_generation_match=int(generation))

    def stat(self, name: str) -> Object | None:
        blob = self.bucket.get_blob(name)
        return Object(blob.name, blob.size, blob.crc32c) if blob else None

    def list(self, prefix: str) -> list[Object]:
        return [Object(b.name, b.size, b.crc32c) for b in self.bucket.list_blobs(prefix=prefix)]

    def folders(self, prefix: str) -> list[str]:
        listing = self.bucket.list_blobs(prefix=prefix, delimiter="/")
        # Prefixes are populated lazily, including those on later pages.
        for _ in listing:
            pass
        return sorted(listing.prefixes)

    def copy(self, name: str, chunks: Iterator[bytes]) -> None:
        with self.bucket.blob(name).open(
            "wb", chunk_size=8 * 1024 * 1024, if_generation_match=0, checksum="crc32c"
        ) as stream:
            for block in chunks:
                stream.write(block)


class SFTP:
    def __init__(self, config: dict[str, Any], login: dict[str, str]) -> None:
        self._config, self._login = dict(config), dict(login)
        self._closed = False
        self._connect()

    def _connect(self) -> None:
        import paramiko

        if self._closed:
            raise ValueError("SFTP connection is closed")
        if hasattr(self, "ssh"):
            self.ssh.close()
        config, login = self._config, self._login
        host, port = config["host"], int(config.get("port", 22))
        kind, key = config["host_key"].split()[:2]
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.ssh.get_host_keys().add(
            host if port == 22 else f"[{host}]:{port}",
            kind,
            paramiko.PKey.from_type_string(kind, base64.b64decode(key, validate=True)),
        )
        try:
            self.ssh.connect(
                host,
                port=port,
                username=login["username"],
                password=login["password"],
                allow_agent=False,
                look_for_keys=False,
                timeout=30,
                auth_timeout=30,
                banner_timeout=30,
            )
            self.sftp = self.ssh.open_sftp()
            self.sftp.get_channel().settimeout(60)
            self.ssh.get_transport().set_keepalive(30)
        except Exception:
            self.ssh.close()
            raise

    def close(self) -> None:
        self.ssh.close()
        self._login.clear()
        self._closed = True

    def _connected(self) -> bool:
        transport = self.ssh.get_transport()
        return bool(transport and transport.is_active() and not self.sftp.get_channel().closed)

    def _request(self, operation: Literal["listdir_attr", "stat"], path: str) -> Any:
        """Retry one read after a dropped metadata connection; never retry remote file errors."""
        import paramiko

        if not self._connected():
            print(json.dumps({"event": "atomx_sftp_reconnect", "operation": operation}), flush=True)
            self._connect()
        try:
            return getattr(self.sftp, operation)(path)
        except (EOFError, OSError, paramiko.SSHException):
            if self._connected():
                raise
        # Downloads run in a separate OpenSSH process, leaving this channel idle for hours.
        # Reauthentication retains the same pinned host key and personal login.
        print(json.dumps({"event": "atomx_sftp_reconnect", "operation": operation}), flush=True)
        self._connect()
        return getattr(self.sftp, operation)(path)

    def entries(self, path: str) -> list[Any]:
        items = self._request("listdir_attr", path)
        for item in items:
            if item.filename in {".", ".."} or "/" in item.filename or not item.st_mode:
                raise ValueError("Invalid remote entry")
            if not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)):
                raise ValueError("Symlinks and special files are not supported")
        return items

    def dirs(self, path: str) -> list[str]:
        # Searching skips symlinks and odd entries; transferred files still use entries().
        return [
            a.filename
            for a in self._request("listdir_attr", path)
            if a.filename not in {".", ".."}
            and "/" not in a.filename
            and a.st_mode
            and stat.S_ISDIR(a.st_mode)
        ]

    def files(self, path: str) -> Iterator[tuple[str, int]]:
        for item in self.entries(path):
            if stat.S_ISDIR(item.st_mode):
                for rel, size in self.files(posixpath.join(path, item.filename)):
                    yield item.filename + "/" + rel, size
            else:
                yield item.filename, item.st_size

    def size(self, path: str) -> int:
        return int(self._request("stat", path).st_size)

    def open(self, path: str) -> Any:
        return self.sftp.open(path, "rb")


def saved_login(version: str) -> dict[str, str]:
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    with AuthorizedSession(credentials) as session:
        response = session.get(
            f"https://secretmanager.googleapis.com/v1/{version}:access", timeout=30
        )
        if response.status_code != 200:
            raise RuntimeError("Saved login unavailable")
        login = json.loads(base64.b64decode(response.json()["payload"]["data"], validate=True))
    if not all(isinstance(login.get(k), str) and login[k] for k in ("username", "password")):
        raise ValueError("Invalid saved login")
    return login
