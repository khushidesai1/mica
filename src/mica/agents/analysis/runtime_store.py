"""Immutable public runtime archives and a generation-checked lab-wide index."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .config import Settings
from .runtime_install import Installer, RuntimeFailure, RuntimeRecord, file_sha256, parse_lock
from .runtime_policy import POLICY_VERSION, PackageRequest, request_key, valid_runtime_id
from .storage import Conflict, Storage

PREFIX = "projects/_toolkit"


def encode(record: RuntimeRecord) -> bytes:
    return json.dumps(asdict(record), sort_keys=True).encode()


def read_record(raw: bytes) -> RuntimeRecord:
    record = RuntimeRecord(**json.loads(raw))
    if (
        not valid_runtime_id(record.id)
        or not record.validated.get("ok")
        or record.kind not in {"overlay", "isolated", "image"}
        or record.policy_version != POLICY_VERSION
    ):
        raise RuntimeFailure("Unvalidated or incompatible runtime record")
    return record


def inspect_archive(archive: Path, max_bytes: int) -> RuntimeRecord:
    with tarfile.open(archive, "r:gz") as tar:
        size = 0
        names = set()
        for member in tar:
            name = Path(member.name)
            # No links (including interpreter links), devices, traversal, or duplicate members.
            if (
                name.is_absolute()
                or ".." in name.parts
                or member.name in names
                or not (member.isfile() or member.isdir())
                or (name.parent == Path("bin") and re.fullmatch(r"python[0-9.]*", name.name))
            ):
                raise RuntimeFailure("Unsafe runtime archive member")
            names.add(member.name)
            size += member.size
            if size > max_bytes:
                raise RuntimeFailure("Runtime archive exceeds the size limit")
        info = tar.getmember("runtime.json")
        if info.size > 2 * 1024 * 1024:
            raise RuntimeFailure("Runtime manifest too large")
        stream = tar.extractfile(info)
        if stream is None:
            raise RuntimeFailure("Runtime manifest missing")
        return read_record(stream.read())


class RuntimeStore:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage, self.settings = storage, settings

    def path(self, id: str) -> Path:
        if not valid_runtime_id(id):
            raise RuntimeFailure("Invalid runtime ID")
        return self.settings.runtime_root / id

    def index(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.storage.get(f"{PREFIX}/index.json")[0]))
        except FileNotFoundError:
            return {}

    def load(self, id: str) -> RuntimeRecord:
        self.path(id)
        record = read_record(self.storage.get(f"{PREFIX}/runtimes/{id}/runtime.json")[0])
        if record.id != id:
            raise RuntimeFailure("Runtime ID mismatch")
        return record

    def matches(
        self,
        record: RuntimeRecord,
        request: PackageRequest,
        base: tuple[str, str, dict[str, str]],
        *,
        isolated: bool = False,
    ) -> bool:
        if (
            not record.validated.get("ok")
            or record.policy_version != POLICY_VERSION
            or record.python_version != base[0]
            or (record.kind == "overlay" and record.base_fingerprint != base[1])
            or ((isolated or request.isolated) and record.kind != "isolated")
        ):
            return False
        if {
            canonicalize_name(n) for n in self.settings.csv("runtime_denylist")
        } & record.packages.keys():
            return False
        wanted_sources = {(s.url, s.commit) for s in request.sources}
        if wanted_sources != {(s["url"], s["commit"]) for s in record.sources}:
            return False
        # An extra is only reusable if it was resolved originally; a top-level version alone
        # cannot establish that the optional dependencies are present.
        original = [Requirement(s) for s in record.request]
        for spec in request.specs:
            req = Requirement(spec)
            name = canonicalize_name(req.name)
            if name not in record.packages or not req.specifier.contains(
                record.packages[name], prereleases=True
            ):
                return False
            extras = {e for r in original if canonicalize_name(r.name) == name for e in r.extras}
            if not req.extras <= extras:
                return False
        return True

    def find(
        self,
        request: PackageRequest,
        base: tuple[str, str, dict[str, str]],
        *,
        isolated: bool = False,
    ) -> RuntimeRecord | None:
        candidates = []
        for id in self.index():
            try:
                record = self.load(id)
            except RuntimeFailure:
                continue  # Older policy records cannot block a fresh, validated setup.
            if self.matches(record, request, base, isolated=isolated):
                candidates.append(record)
        catalog = ", ".join(e.name for e in request.entries)
        return next(
            iter(
                sorted(candidates, key=lambda r: (r.catalog == catalog, r.created_at), reverse=True)
            ),
            None,
        )

    def save(self, record: RuntimeRecord) -> RuntimeRecord:
        if not record.validated.get("ok"):
            raise RuntimeFailure("Cannot save an unvalidated runtime")
        path = self.path(record.id)
        if file_sha256(path / "lock.txt") != record.lock_sha256:
            raise RuntimeFailure("Runtime lock checksum mismatch")
        with tempfile.TemporaryDirectory(
            prefix=".archive-", dir=self.settings.runtime_root
        ) as temp:
            archive = Path(temp) / "env.tar.gz"
            # Do not write runtime.json into the shared local directory while concurrent savers
            # run: embed each immutable manifest directly in that saver's archive.
            with tarfile.open(archive, "w:gz", dereference=True) as tar:
                for item in sorted(path.rglob("*")):
                    name = str(item.relative_to(path))
                    if item.is_symlink():
                        if item.name == "lib64" and item.readlink() == Path("lib"):
                            continue
                        if item.parent == path / "bin" and re.fullmatch(
                            r"python[0-9.]*", item.name
                        ):
                            continue
                        raise RuntimeFailure("Unsupported symlink in runtime")
                    if name not in {"runtime.json", ".lock"}:
                        tar.add(item, arcname=name, recursive=False)
                data = encode(record)
                member = tarfile.TarInfo("runtime.json")
                member.size = len(data)
                member.mode = 0o644
                tar.addfile(member, io.BytesIO(data))
            inspect_archive(archive, int(self.settings.runtime_max_gb * 1024**3))
            prefix = f"{PREFIX}/runtimes/{record.id}"
            try:
                self.storage.upload_new(f"{prefix}/env.tar.gz", archive)
            except Conflict:
                # A competing build (or a crashed publisher) already owns the immutable archive.
                # Its embedded manifest lets us finish publication without overwriting its bytes.
                self.storage.download(f"{prefix}/env.tar.gz", archive)
                record = inspect_archive(archive, int(self.settings.runtime_max_gb * 1024**3))
                if record.id != path.name:
                    raise RuntimeFailure("Conflicting runtime archive identity") from None
            record = replace(record, archive_sha256=file_sha256(archive))
            with tarfile.open(archive, "r:gz") as tar:
                stream = tar.extractfile("lock.txt")
                if stream is None:
                    raise RuntimeFailure("Runtime archive has no lock")
                lock = stream.read(16 * 1024 * 1024)
                if hashlib.sha256(lock).hexdigest() != record.lock_sha256:
                    raise RuntimeFailure("Runtime lock checksum mismatch")
            for name, content in (("lock.txt", lock), ("runtime.json", encode(record))):
                try:
                    self.storage.put_if(f"{prefix}/{name}", content, None)
                except Conflict:
                    if self.storage.get(f"{prefix}/{name}")[0] != content:
                        raise RuntimeFailure("Conflicting immutable runtime record") from None
            self.update_index(record)
            return record

    def update_index(self, record: RuntimeRecord) -> None:
        key = f"{PREFIX}/index.json"
        for _ in range(10):
            try:
                raw, generation = self.storage.get(key)
                index = json.loads(raw)
            except FileNotFoundError:
                index, generation = {}, None
            index[record.id] = {
                "kind": record.kind,
                "catalog": record.catalog,
                "request": record.request,
                "created_at": record.created_at,
                "size_bytes": record.size_bytes,
            }
            try:
                self.storage.put_if(key, json.dumps(index, sort_keys=True).encode(), generation)
                return
            except Conflict:
                continue
        raise RuntimeFailure("Runtime index changed concurrently; retry setup.")

    def local_ready(self, record: RuntimeRecord) -> bool:
        path = self.path(record.id)
        try:
            local = read_record((path / "runtime.json").read_bytes())
            return (
                local.archive_sha256 == record.archive_sha256
                and file_sha256(path / "lock.txt") == record.lock_sha256
                and (path / "bin/python").exists()
            )
        except (OSError, ValueError):
            return False

    def restore(self, record: RuntimeRecord, installer: Installer) -> None:
        path = self.path(record.id)
        denied = {canonicalize_name(n) for n in self.settings.csv("runtime_denylist")}
        if denied & record.packages.keys():
            raise RuntimeFailure("Saved runtime includes a denied package")
        if self.local_ready(record):
            return
        self.settings.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".restore-", dir=self.settings.runtime_root
        ) as temp:
            installer.scratch = Path(temp)
            installer.deadline = time.monotonic() + self.settings.runtime_timeout_seconds
            archive = Path(temp) / "env.tar.gz"
            self.storage.download(f"{PREFIX}/runtimes/{record.id}/env.tar.gz", archive)
            if file_sha256(archive) != record.archive_sha256:
                raise RuntimeFailure("Runtime archive checksum mismatch")
            embedded = inspect_archive(archive, int(self.settings.runtime_max_gb * 1024**3))
            if replace(embedded, archive_sha256=record.archive_sha256) != record:
                raise RuntimeFailure("Runtime manifest mismatch")
            if path.exists():
                shutil.rmtree(path)
            try:
                installer.skeleton(path, record.kind)
                with tarfile.open(archive, "r:gz") as tar:
                    tar.extractall(path, filter="data")
                if file_sha256(path / "lock.txt") != record.lock_sha256:
                    raise RuntimeFailure("Runtime lock checksum mismatch")
                packages = parse_lock((path / "lock.txt").read_text())
                if {k: p.version for k, p in packages.items()} != record.packages:
                    raise RuntimeFailure("Runtime package manifest mismatch")
                installer.validate(path, tuple(record.validated["imports"]))
                installer.check_size()
                (path / "runtime.json").write_bytes(encode(record))
            except BaseException:
                shutil.rmtree(path, ignore_errors=True)
                raise

    def failure_key(
        self, request: PackageRequest, base: tuple[str, str, dict[str, str]], isolated: bool
    ) -> str:
        key = request_key(
            request, "isolated" if isolated or request.isolated else "overlay", base[0], base[1]
        )
        policy = json.dumps(
            [
                POLICY_VERSION,
                self.settings.runtime_max_source_builds,
                self.settings.runtime_min_age_days,
                sorted(self.settings.csv("runtime_denylist")),
                self.settings.runtime_max_gb,
                self.settings.runtime_timeout_seconds,
            ]
        )
        return hashlib.sha256((key + policy).encode()).hexdigest()

    def failure(self, key: str) -> str | None:
        try:
            entry = json.loads(self.storage.get(f"{PREFIX}/failures/{key}.json")[0])
            created = datetime.fromisoformat(entry["created_at"])
            age = (datetime.now(UTC) - created).total_seconds()
            if entry["policy_version"] != POLICY_VERSION or not (
                0 <= age < self.settings.runtime_failure_ttl_hours * 3600
            ):
                return None
            return str(entry["error"])
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            return None

    def save_failure(self, key: str, message: str) -> None:
        self.storage.put(
            f"{PREFIX}/failures/{key}.json",
            json.dumps(
                {
                    "error": message[-3000:],
                    "created_at": datetime.now(UTC).isoformat(),
                    "policy_version": POLICY_VERSION,
                }
            ).encode(),
        )
