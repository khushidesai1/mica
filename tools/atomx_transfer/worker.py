"""Trusted worker bootstrap, mapped destinations and reviewed script staging."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import google.auth.credentials
import requests
from google.auth.transport.requests import Request
from google.oauth2.id_token import fetch_id_token

from .adapters import GCS, SFTP
from .core import Object


class Control:
    def __init__(self, run):
        self.url = os.environ.pop("MICA_ATOMX_CONTROL_URL", "").rstrip("/")
        self.grant = os.environ.pop("MICA_ATOMX_GRANT", "")
        if urlsplit(self.url).scheme != "https" or not re.fullmatch(r"[a-f0-9]{64}", self.grant):
            raise ValueError("Transfer worker grant unavailable")
        self.run = run

    def call(self, action):
        identity = fetch_id_token(Request(), self.url)
        with requests.post(
            self.url + f"/integrations/atomx/worker/{self.run}/{action}",
            headers={"Authorization": "Bearer " + identity, "X-Mica-Transfer-Grant": self.grant},
            timeout=45,
            allow_redirects=False,
        ) as result:
            if result.status_code != 200:
                raise ValueError("Transfer authorization unavailable")
            return result.json()


class Writer(google.auth.credentials.Credentials):
    def __init__(self, control):
        super().__init__()
        self.control = control

    def refresh(self, request):
        value = self.control.call("gcs-token")
        self.token = value["token"]
        self.expiry = datetime.datetime.fromisoformat(value["expires"]).replace(tzinfo=None)


class MappedGCS:
    def __init__(self, config, control=None):
        self.mapping = [
            ("raw_outputs/raw/", config["raw_prefix"]),
            ("raw_outputs/additional_files/", config["additional_prefix"]),
            ("transfer_status/", config["status_prefix"]),
        ]
        self.reader = GCS(config["bucket"])
        self.writer = GCS(config["bucket"], Writer(control)) if control else None

    @classmethod
    def for_operator(cls, config, *, writable=False):
        bucket = cls(config)
        # Explicit operator commands use ADC; jobs never fall back from scoped credentials.
        if writable:
            bucket.writer = bucket.reader
        return bucket

    def map(self, name):
        for old, new in self.mapping:
            if name.startswith(old):
                return new + name[len(old) :]
        raise ValueError("Object outside transfer layout")

    def read(self, name):
        return self.reader.read(self.map(name))

    def stat(self, name):
        obj = self.reader.stat(self.map(name))
        return Object(name, obj.size, obj.crc32c) if obj else None

    def list(self, prefix):
        mapped = self.map(prefix)
        return [
            Object(prefix + o.name[len(mapped) :], o.size, o.crc32c)
            for o in self.reader.list(mapped)
        ]

    def folders(self, prefix):
        mapped = self.map(prefix)
        return [
            prefix + folder[len(mapped) :]
            for folder in self.reader.folders(mapped)
            # A raw root can contain the configured additional/status roots. They are not samples.
            if not any(new != mapped and new.startswith(folder) for _, new in self.mapping)
        ]

    def write(self, name, body, generation):
        if self.writer is None:
            raise ValueError("Inventory is read-only")
        return self.writer.write(self.map(name), body, generation)

    def delete(self, name, generation):
        if self.writer is None:
            raise ValueError("Inventory is read-only")
        return self.writer.delete(self.map(name), generation)

    def copy(self, name, chunks):
        if self.writer is None:
            raise ValueError("Inventory is read-only")
        return self.writer.copy(self.map(name), chunks)


class ScriptSFTP(SFTP):
    """Metadata through the existing adapter; payload downloads through upstream scripts."""

    def __init__(self, config, login):
        super().__init__(config, login)
        self.config, self.login = config, login
        self.temporary = tempfile.TemporaryDirectory(prefix="mica-atomx-")
        self.local = {}

    def close(self):
        super().close()
        if hasattr(self, "temporary"):
            self.temporary.cleanup()
        self.login = {}

    @staticmethod
    def quote(path):
        if any(c in path for c in '\r\n\x00"\\*?[]'):
            raise ValueError("Unsupported path in SFTP manifest")
        return '"' + path + '"'

    def stage(self, files, heartbeat):
        root = Path(self.temporary.name)
        if sum(f.size for f in files) * 1.15 + 5_000_000_000 > shutil.disk_usage(root).free:
            raise ValueError("Insufficient transfer staging space")
        host = (
            self.config["host"]
            if self.config.get("port", 22) == 22
            else f"[{self.config['host']}]:{self.config['port']}"
        )
        known = root / "known_hosts"
        known.write_text(host + " " + self.config["host_key"] + "\n")
        for scope, script in (
            ("raw_outputs/raw/", "get_tma.sh"),
            ("raw_outputs/additional_files/", "get_tma_addfiles.sh"),
        ):
            commands = []
            for index, file in enumerate(files):
                if not file.name.startswith(scope):
                    continue
                target = root / str(index)
                self.local[file.source] = target
                commands.append(f"get {self.quote(file.source)} {self.quote(str(target))}")
            if not commands:
                continue
            batch = root / "download.batch"
            batch.write_text("\n".join(commands) + "\nbye\n")
            read_fd, write_fd = os.pipe()
            try:
                # Password is sent through an inherited private pipe, never argv or environment.
                os.write(write_fd, (self.login["password"] + "\n").encode())
                os.close(write_fd)
                write_fd = -1
                env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "ATOMX_PASSWORD_FD": str(read_fd),
                    "ATOMX_KNOWN_HOSTS": str(known),
                    "ATOMX_SFTP_HOST": self.config["host"],
                    "ATOMX_SFTP_PORT": str(self.config.get("port", 22)),
                    "ATOMX_SFTP_USER": self.login["username"],
                }
                process = subprocess.Popen(
                    [
                        "bash",
                        str(Path(__file__).parent / "scripts" / script),
                        "--download-only",
                        str(batch),
                    ],
                    env=env,
                    pass_fds=(read_fd,),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    while True:
                        heartbeat()
                        try:
                            code = process.wait(timeout=15)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                    if code:
                        raise ValueError("SFTP script download failed")
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)
        for file in files:
            if (
                self.local[file.source].stat().st_size != file.size
                or self.size(file.source) != file.size
            ):
                raise ValueError("Source changed during staging")

    def open(self, path):
        return self.local[path].open("rb")
