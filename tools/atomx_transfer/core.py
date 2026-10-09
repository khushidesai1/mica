"""Verified, create-only streaming transfers with a generation-checked marker lease."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Protocol

import google_crc32c


def now() -> str:
    return datetime.now(UTC).isoformat()


def age(stamp: str) -> float:
    return time.time() - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def sample_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Invalid sample name")
    return value


def export_path(value: str) -> str:
    if not value.startswith("/") or any(p in {".", "..", ""} for p in value[1:].split("/")):
        raise ValueError("Use an absolute export folder without traversal or trailing slash")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Invalid export folder")
    return value


@dataclass(frozen=True)
class Object:
    name: str
    size: int
    crc32c: str


@dataclass(frozen=True)
class File:
    source: str
    name: str
    size: int


class Remote(Protocol):
    def dirs(self, path: str) -> list[str]: ...
    def files(self, path: str) -> Iterator[tuple[str, int]]: ...
    def size(self, path: str) -> int: ...
    def open(self, path: str) -> BinaryIO: ...


class Bucket(Protocol):
    def read(self, name: str) -> tuple[dict[str, Any], str] | None: ...
    def write(self, name: str, body: dict[str, Any], generation: str | None) -> str: ...
    def delete(self, name: str, generation: str) -> None: ...
    def stat(self, name: str) -> Object | None: ...
    def list(self, prefix: str) -> list[Object]: ...
    def copy(self, name: str, chunks: Iterator[bytes]) -> None: ...


class Busy(ValueError):
    pass


def plan(
    remote: Remote,
    root: str,
    sample: str,
    target: str,
    heartbeat: Callable[[], None] = lambda: None,
) -> tuple[str, list[File]]:
    export_path(root)
    sample_name(sample)
    sample_name(target)
    base = f"{root}/DecodedFiles/{sample}"
    runs = sorted(x for x in remote.dirs(base) if re.fullmatch(r"\d{8}_\d{6}_S\d+", x))
    if not runs:
        raise ValueError("No decoded run folder found")
    run = f"{base}/{runs[-1]}"
    files = []
    for rel, size in remote.files(run):
        heartbeat()
        if rel.startswith("CellStatsDir/Morphology2D/"):
            destination = f"raw_outputs/raw/{target}/" + rel.removeprefix("CellStatsDir/")
        elif rel == "RunSummary/Morphology_ChannelID_Dictionary.txt":
            destination = f"raw_outputs/raw/{target}/Morphology_ChannelID_Dictionary.txt"
        else:
            destination = f"raw_outputs/additional_files/{target}/{rel}"
        files.append(File(f"{run}/{rel}", destination, size))
    for rel, size in remote.files(f"{root}/flatFiles/{sample}"):
        heartbeat()
        files.append(
            File(f"{root}/flatFiles/{sample}/{rel}", f"raw_outputs/raw/{target}/{rel}", size)
        )
    if not files or len({f.name for f in files}) != len(files):
        raise ValueError("Empty export or destination name collision")
    for file in files:
        if file.size < 0 or ".." in PurePosixPath(file.name).parts:
            raise ValueError("Invalid source file")
    return runs[-1], sorted(files, key=lambda f: f.name)


def fingerprint(run: str, files: list[File]) -> str:
    return hashlib.sha256(
        json.dumps([run, [asdict(f) for f in files]], sort_keys=True).encode()
    ).hexdigest()


class Transfer:
    def __init__(
        self,
        remote: Remote,
        bucket: Bucket,
        request: dict[str, Any],
        progress: Callable[[dict[str, Any]], None] = lambda _: None,
    ) -> None:
        self.remote, self.bucket, self.request, self.progress = remote, bucket, request, progress
        self.sample = sample_name(request.get("as_sample") or request["sample"])
        self.prefix = "transfer_status/" + self.sample
        self.generation = ""
        self.last_beat = 0.0
        self.marker: dict[str, Any] = {
            "version": 1,
            "sample": self.sample,
            "remote_sample": request["sample"],
            "export_root": request["export_root"],
            "run_folder": "",
            "started": now(),
            "heartbeat": now(),
            "finished": None,
            "tool_version": request["tool_version"],
            "requested_by": request["requested_by"],
            "login_saved_by": request["login_saved_by"],
            "run_id": request["run_id"],
            "files_done": 0,
            "bytes_done": 0,
            "plan_fingerprint": request.get("plan_fingerprint"),
        }

    def beat(self, *, force: bool = False) -> None:
        if force or time.monotonic() - self.last_beat >= 300:
            self.marker["heartbeat"] = now()
            self.generation = self.bucket.write(
                self.prefix + ".in_progress.json", self.marker, self.generation
            )
            self.last_beat = time.monotonic()
            self.progress({"event": "progress", **self.marker})

    def run(self) -> dict[str, Any]:
        if self.bucket.read(self.prefix + ".done.json"):
            raise Busy("Sample already has a done marker; verify it instead")
        prior = self.bucket.read(self.prefix + ".in_progress.json")
        if prior and age(prior[0]["heartbeat"]) <= 3600:
            raise Busy("Sample has an active transfer")
        self.generation = self.bucket.write(
            self.prefix + ".in_progress.json", self.marker, prior[1] if prior else None
        )
        # Recheck after claiming to close the race with a preceding transfer's completion.
        if self.bucket.read(self.prefix + ".done.json"):
            self.bucket.delete(self.prefix + ".in_progress.json", self.generation)
            raise Busy("Sample already has a done marker; verify it instead")
        step = "plan"
        try:
            run, files = plan(
                self.remote,
                self.request["export_root"],
                self.request["sample"],
                self.sample,
                self.beat,
            )
            self.marker.update(
                run_folder=run, n_files=len(files), total_bytes=sum(f.size for f in files)
            )
            if self.request.get("plan_fingerprint") != fingerprint(run, files):
                raise ValueError("Source changed since confirmation; request a new preview")
            self.beat(force=True)
            step = "copy"
            manifest = []
            for file in files:
                self.beat()
                checksum = google_crc32c.Checksum()
                read = 0

                def chunks(file: File = file, checksum: Any = checksum) -> Iterator[bytes]:
                    nonlocal read
                    with self.remote.open(file.source) as stream:
                        while block := stream.read(8 * 1024 * 1024):
                            read += len(block)
                            checksum.update(block)
                            self.beat()
                            yield block
                    if read != file.size or self.remote.size(file.source) != file.size:
                        raise ValueError("Source file changed during transfer")

                existing = self.bucket.stat(file.name)
                if existing is None:
                    self.bucket.copy(file.name, chunks())
                else:
                    for _ in chunks():
                        pass
                expected = Object(file.name, read, base64.b64encode(checksum.digest()).decode())
                if self.bucket.stat(file.name) != expected:
                    raise ValueError("Existing or uploaded object differs from source")
                manifest.append(expected)
                self.marker["files_done"] += 1
                self.marker["bytes_done"] += read
            step = "verify"
            if plan(
                self.remote,
                self.request["export_root"],
                self.request["sample"],
                self.sample,
                self.beat,
            ) != (run, files):
                raise ValueError("Source listing changed during transfer")
            actual = self.bucket.list(f"raw_outputs/raw/{self.sample}/") + self.bucket.list(
                f"raw_outputs/additional_files/{self.sample}/"
            )
            if sorted(actual, key=lambda f: f.name) != manifest:
                raise ValueError(
                    "Destination manifest mismatch (missing, changed, or extra objects)"
                )
            self.beat(force=True)
            done = {
                **self.marker,
                "finished": now(),
                "manifest": [asdict(f) for f in manifest],
                "source": {"n_files": len(files), "total_bytes": sum(f.size for f in files)},
            }
            self.bucket.write(self.prefix + ".done.json", done, None)
            self.bucket.delete(self.prefix + ".in_progress.json", self.generation)
            failed = self.bucket.read(self.prefix + ".failed.json")
            if failed:
                self.bucket.delete(self.prefix + ".failed.json", failed[1])
            self.progress({"event": "done", **done})
            return done
        except Exception:
            # Never serialize a library exception: it may contain credentials or server text.
            failed = self.bucket.read(self.prefix + ".failed.json")
            body = {**self.marker, "finished": now(), "step": step, "error": "transfer_failed"}
            # Confirm ownership before publishing failure or removing the lease.
            self.beat(force=True)
            self.bucket.write(self.prefix + ".failed.json", body, failed[1] if failed else None)
            self.bucket.delete(self.prefix + ".in_progress.json", self.generation)
            raise RuntimeError(f"Transfer failed during {step}") from None


def inventory(remote: Remote, bucket: Bucket, request: dict[str, Any]) -> dict[str, Any]:
    roots = (
        [request["export_root"]]
        if request.get("export_root")
        else ["/" + x for x in remote.dirs("/")]
    )
    entries = []
    for root in roots:
        try:
            samples = remote.dirs(root + "/DecodedFiles")
        except FileNotFoundError:
            continue
        for sample in samples:
            sample_name(sample)
            target = (
                (request.get("as_sample") or sample) if sample == request.get("sample") else sample
            )
            run, files = plan(remote, root, sample, target)
            status = "on_atomx_only"
            for suffix in ("failed", "done", "in_progress"):
                marker = bucket.read(f"transfer_status/{target}.{suffix}.json")
                if marker:
                    status = suffix
                    if suffix == "done" and marker[0].get("plan_fingerprint") != fingerprint(
                        run, files
                    ):
                        status = "changed_since_done"
            entries.append(
                {
                    "sample": sample,
                    "as_sample": target,
                    "export_root": root,
                    "run_folder": run,
                    "n_files": len(files),
                    "total_bytes": sum(f.size for f in files),
                    "status": status,
                    "plan_fingerprint": fingerprint(run, files),
                }
            )
    targets = {e["as_sample"] for e in entries}
    for obj in bucket.list("transfer_status/"):
        if not obj.name.endswith(".done.json"):
            continue
        marker = bucket.read(obj.name)
        if marker and marker[0]["sample"] not in targets:
            body = marker[0]
            entries.append(
                {
                    "sample": body["remote_sample"],
                    "as_sample": body["sample"],
                    "export_root": body["export_root"],
                    "run_folder": body["run_folder"],
                    "n_files": body["n_files"],
                    "total_bytes": body["total_bytes"],
                    "status": "bucket_only",
                }
            )
    return {
        "version": 1,
        "generated_at": now(),
        "requested_by": request["requested_by"],
        "login_saved_by": request["login_saved_by"],
        "samples": entries,
    }
