"""Real password-authenticated SFTP downloads against a bounded loopback-only fixture."""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path

import paramiko


class Login(paramiko.ServerInterface):
    def __init__(self, fixture, transport):
        self.fixture, self.transport = fixture, transport

    def check_auth_password(self, username, password):
        if (username, password) == ("fixture", "fixture-password"):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, channel):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.server = server

    def path(self, name):
        root = self.server.fixture.root
        path = (root / name.lstrip("/")).resolve()
        if not path.is_relative_to(root):
            raise PermissionError(errno.EACCES, "Outside fixture")
        return path

    def stat(self, path):
        if self.server.fixture.stat_drops:
            self.server.fixture.stat_drops -= 1
            self.server.transport.close()
            return paramiko.SFTP_FAILURE
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def list_folder(self, path):
        try:
            entries = []
            for child in self.path(path).iterdir():
                item = paramiko.SFTPAttributes.from_stat(child.stat())
                item.filename = child.name
                entries.append(item)
            return entries
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attributes):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            return paramiko.SFTP_PERMISSION_DENIED
        fixture = self.server.fixture
        # The metadata connection can expire while the independent scripts download.
        if (fixture.drop_on_download and not fixture.dropped
                and self.server.transport is not fixture.transports[0]):
            fixture.transports[0].close()
            fixture.dropped = True
        try:
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = self.path(path).open("rb")
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class SFTPFixture:
    """A read-only server; synthetic login, ephemeral pinned key, no external listener."""

    def __init__(self, root: Path, *, drop_on_download: bool = False):
        self.root = root.resolve()
        self.drop_on_download, self.dropped = drop_on_download, False
        self.stat_drops = 0
        self.key = paramiko.RSAKey.generate(2048)
        self.transports = []
        self.handlers = []
        self.stopped = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.accept, daemon=True)

    @property
    def config(self):
        return {"host": "127.0.0.1", "port": self.port,
                "host_key": self.key.get_name() + " " + self.key.get_base64()}

    @property
    def login(self):
        return {"username": "fixture", "password": "fixture-password"}

    def accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            transport = paramiko.Transport(connection)
            self.transports.append(transport)
            handler = threading.Thread(target=self.serve, args=(transport,), daemon=True)
            self.handlers.append(handler)
            handler.start()

    def serve(self, transport):
        transport.add_server_key(self.key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
        try:
            transport.start_server(server=Login(self, transport))
            while transport.is_active() and not self.stopped.wait(0.1):
                pass
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=5)
        for transport in self.transports:
            transport.close()
        for handler in self.handlers:
            handler.join(timeout=5)
        assert not self.thread.is_alive() and not any(t.is_alive() for t in self.handlers)


def check_download(*, disconnect: bool = True):
    from atomx_transfer.core import File
    from atomx_transfer.worker import ScriptSFTP

    with tempfile.TemporaryDirectory(prefix="mica-sftp-fixture-") as directory:
        root = Path(directory)
        payloads = {"raw file.txt": b"raw fixture\n" * 1000,
                    "additional file.txt": b"additional fixture\n" * 1000}
        files = []
        for name, data in payloads.items():
            (root / name).write_bytes(data)
            scope = "raw" if name.startswith("raw") else "additional_files"
            files.append(File("/" + name, f"raw_outputs/{scope}/sample/{name}", len(data)))
        with SFTPFixture(root, drop_on_download=disconnect) as server:
            remote = ScriptSFTP(server.config, server.login)
            staging = Path(remote.temporary.name)
            try:
                remote.stage(files, lambda: None)
                for file in files:
                    with remote.open(file.source) as stream:
                        assert stream.read() == payloads[file.source.lstrip("/")]
                if disconnect:
                    assert server.dropped
                    remote.sftp.close()
                    assert remote.size(files[0].source) == files[0].size
                result = {"probe": "mica_atomx_real_sftp", "passed": True,
                          "files": len(files), "bytes": sum(f.size for f in files),
                          "metadata_disconnect": disconnect,
                          "connections": len(server.transports)}
            finally:
                remote.close()
            assert not staging.exists(), "Staged fixture files must be removed"
    print(json.dumps(result), flush=True)
    return result


if __name__ == "__main__":
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
    check_download()
