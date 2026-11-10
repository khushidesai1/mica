"""A closed PyPI mirror: only trusted code fetches HTTPS, uv sees local wheel URLs.

The same listener is uv's mandatory HTTP(S) proxy. It refuses CONNECT and all
non-mirror URLs, including direct URL dependencies discovered in wheel metadata.
No installer subprocess inherits ambient proxy, index, git, or credential config.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from contextlib import AbstractContextManager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

from .runtime_policy import RuntimePolicyError
from .runtime_sdist import SourceDistribution
from .runtime_wheels import check_metadata, check_wheel


class WheelMirror(AbstractContextManager["WheelMirror"]):
    def __init__(
        self,
        root: Path,
        denied: set[str],
        max_bytes: int,
        *,
        min_age_days: int = 7,
    ) -> None:
        self.root, self.denied, self.max_bytes = root, denied, max_bytes
        self.denied = {canonicalize_name(name) for name in denied}
        self.cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
        self.indexes: dict[str, list[dict[str, Any]]] = {}
        self.simple: dict[str, bytes] = {}
        self.index_locks: dict[str, threading.Lock] = {}
        self.file_locks: dict[str, threading.Lock] = {}
        self.policy_errors: list[str] = []
        self.urls: dict[str, str] = {}
        self.local_wheels: dict[str, Path] = {}
        self.lock = threading.Lock()
        self.used = 0
        self.stopping = threading.Event()
        mirror = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(30)

            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def error(self, status: int, message: str) -> None:
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.send_error(status, message)

            def do_CONNECT(self) -> None:
                self.send_error(403, "External URLs are not permitted")

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_GET(self) -> None:
                try:
                    content, media = mirror.get(self.path)
                    self.send_response(200)
                    self.send_header("Content-Type", media)
                    length = content.stat().st_size if isinstance(content, Path) else len(content)
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    if self.command != "HEAD":
                        if isinstance(content, Path):
                            with content.open("rb") as stream:
                                shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
                        else:
                            self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The installer disconnected; do not turn this into a policy refusal.
                except RuntimePolicyError as exc:
                    mirror.policy_errors.append(str(exc))
                    self.error(403, "Package source refused by runtime policy")
                except httpx.HTTPStatusError as exc:
                    # A missing package is a resolver fact, not a transient registry outage.
                    missing = exc.response.status_code == 404
                    self.error(
                        404 if missing else 502,
                        "Package not found" if missing else "Package registry request failed",
                    )
                except httpx.HTTPError:
                    self.error(502, "Package registry request failed")
                except ValueError:
                    self.error(403, "Package source refused by runtime policy")
                except OSError:
                    self.error(503, "Package storage temporarily unavailable")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, False)
        self.server.request_queue_size = 64
        self.server.server_bind()
        self.server.server_activate()
        # Join request handlers before the enclosing temporary directory is removed.
        self.server.daemon_threads = False
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> WheelMirror:
        self.root.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        return self

    def __exit__(
        self,
        typ: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def fetch(self, url: str, *, metadata: bool = False, destination: Path | None = None) -> bytes:
        if self.stopping.is_set():
            raise httpx.ReadError("Package setup stopped")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"pypi.org", "files.pythonhosted.org"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Untrusted registry URL")
        # No redirects, netrc, proxy environment, auth, or package-controlled headers.
        with (
            httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client,
            client.stream(
                "GET",
                url,
                headers={"Accept": "application/vnd.pypi.simple.v1+json" if metadata else "*/*"},
            ) as response,
        ):
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Unexpected registry response")
            body = bytearray()
            with destination.open("wb") if destination else nullcontext(None) as target:
                for chunk in response.iter_bytes():
                    if self.stopping.is_set():
                        raise httpx.ReadError("Package setup stopped")
                    with self.lock:
                        self.used += len(chunk)
                        if self.used > self.max_bytes:
                            raise ValueError("Package download size limit exceeded")
                    if target:
                        target.write(chunk)
                    else:
                        body.extend(chunk)
                        if len(body) > 32 * 1024 * 1024:
                            raise ValueError("Package metadata too large")
            return bytes(body)

    def add_wheel(self, path: Path) -> None:
        name, _ = check_wheel(path, self.denied, self.max_bytes)
        self.local_wheels[name] = path

    def eligible(self, item: dict[str, Any], python_version: str) -> bool:
        if item.get("yanked"):
            return False
        try:
            uploaded = datetime.fromisoformat(item["upload-time"].replace("Z", "+00:00"))
            return uploaded <= self.cutoff and SpecifierSet(
                item.get("requires-python") or ""
            ).contains(python_version, prereleases=True)
        except (KeyError, TypeError, ValueError):
            return False  # Missing age or Python evidence never authorizes a source build.

    def source_candidates(
        self,
        error: str,
        specs: list[str],
        python_version: str,
    ) -> list[SourceDistribution]:
        """Only packages implicated by a failed wheel-only solve can run a build script.

        The final resolver still enforces all transitive constraints; selecting a source
        candidate does not authorize an incompatible install.
        """
        if "No solution found" not in error:
            return []
        candidates = []
        tags = set(sys_tags())
        requested = [Requirement(spec) for spec in specs]
        for name, files in self.indexes.items():
            pattern = re.escape(name).replace(r"\-", "[-_.]")
            if not re.search(r"(?<![\w.-])" + pattern + r"(?![\w.-])", error, re.I):
                continue
            constraints = [r.specifier for r in requested if canonicalize_name(r.name) == name]
            # Read positive requirements, not uv's descriptions of the unavailable versions.
            for match in re.finditer(
                r"(?:depends on|requires?|requiring)\s+"
                + pattern
                + r"((?:[<>=!~]{1,2}[A-Za-z0-9.*+!_-]+)(?:,[<>=!~]{1,2}[A-Za-z0-9.*+!_-]+)*)",
                error,
                re.I,
            ):
                constraints.append(SpecifierSet(match[1]))
            wheel_versions = set()
            matching_ages = []
            for item in files:
                if item["filename"].endswith(".whl"):
                    _, version, _, wheel_tags = parse_wheel_filename(item["filename"])
                    if all(c.contains(version) for c in constraints):
                        with suppress(KeyError, TypeError, ValueError):
                            matching_ages.append(
                                datetime.fromisoformat(item["upload-time"].replace("Z", "+00:00"))
                                > self.cutoff
                            )
                    if tags & wheel_tags and self.eligible(item, python_version):
                        wheel_versions.add(version)
            sources = []
            recent = matching_ages
            for item in files:
                if not item["filename"].endswith((".tar.gz", ".zip")):
                    continue
                source_name, version = parse_sdist_filename(item["filename"])
                if source_name == name and all(c.contains(version) for c in constraints):
                    with suppress(KeyError, TypeError, ValueError):
                        recent.append(
                            datetime.fromisoformat(item["upload-time"].replace("Z", "+00:00"))
                            > self.cutoff
                        )
                if (
                    source_name != name
                    or version in wheel_versions
                    or not all(c.contains(version) for c in constraints)
                    or not self.eligible(item, python_version)
                ):
                    continue
                url = str(item["url"])
                parsed = urlsplit(url)
                digest = item.get("hashes", {}).get("sha256", "")
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "files.pythonhosted.org"
                    or parsed.username
                    or parsed.password
                    or parsed.port not in (None, 443)
                    or parsed.fragment
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or Path(item["filename"]).name != item["filename"]
                ):
                    raise RuntimePolicyError("Untrusted or unpinned source archive")
                sources.append(
                    SourceDistribution(name, str(version), item["filename"], url, digest)
                )
            if sources:
                candidates.append(max(sources, key=lambda source: Version(source.version)))
            elif recent and all(recent) and not wheel_versions:
                raise RuntimePolicyError(f"Package {name} is newer than the minimum runtime age.")
        return sorted(candidates, key=lambda source: source.name)

    def fetch_source(self, source: SourceDistribution, destination: Path) -> None:
        # This path is available only to the trusted installer, never the uv HTTP proxy.
        if source.name in self.denied:
            raise RuntimePolicyError("Source package denied by runtime policy")
        if not any(
            item.get("url") == source.url
            and item.get("hashes", {}).get("sha256") == source.sha256
            and item.get("filename") == source.filename
            for item in self.indexes.get(source.name, [])
        ):
            raise RuntimePolicyError("Source archive is not pinned by the observed PyPI index")
        if urlsplit(source.url).hostname != "files.pythonhosted.org":
            raise RuntimePolicyError("Untrusted source artifact host")
        self.fetch(source.url, destination=destination)
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != source.sha256:
                raise RuntimePolicyError("Source archive SHA256 does not match the PyPI index")

    def get(self, raw: str) -> tuple[bytes | Path, str]:
        parsed = urlsplit(raw)
        if parsed.netloc and f"{parsed.scheme}://{parsed.netloc}" != self.url:
            raise ValueError("External URL dependency refused")
        if parsed.query or parsed.fragment:
            raise ValueError("Invalid registry path")
        path = parsed.path
        match = re.fullmatch(r"/simple/([a-z0-9][a-z0-9._-]*)/", path)
        if match:
            name = canonicalize_name(match[1])
            if name in self.denied:
                raise RuntimePolicyError("Package denied by runtime policy")
            if name in self.local_wheels:
                wheel = self.local_wheels[name]
                digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
                doc = {
                    "meta": {"api-version": "1.4"},
                    "name": name,
                    "files": [
                        {
                            "filename": wheel.name,
                            "url": f"{self.url}/built/{name}/{wheel.name}",
                            "hashes": {"sha256": digest},
                            "upload-time": "2000-01-01T00:00:00Z",
                        }
                    ],
                }
                return json.dumps(doc).encode(), "application/vnd.pypi.simple.v1+json"
            with self.lock:
                index_lock = self.index_locks.setdefault(name, threading.Lock())
            with index_lock:
                if name not in self.simple:
                    doc = json.loads(self.fetch(f"https://pypi.org/simple/{name}/", metadata=True))
                    self.indexes[name] = doc["files"]
                    wheels = []
                    for original in doc["files"]:
                        item = dict(original)
                        if not item["filename"].endswith(".whl"):
                            continue
                        url = item["url"]
                        if urlsplit(url).hostname != "files.pythonhosted.org":
                            raise ValueError("Untrusted artifact host")
                        key = hashlib.sha256(url.encode()).hexdigest() + "/" + item["filename"]
                        self.urls[key] = url
                        # uv may fetch PEP 658 wheel metadata alongside a wheel.
                        self.urls[key + ".metadata"] = url + ".metadata"
                        item["url"] = f"{self.url}/files/{key}"
                        wheels.append(item)
                    doc["files"] = wheels
                    self.simple[name] = json.dumps(doc).encode()
                return self.simple[name], "application/vnd.pypi.simple.v1+json"
        if path.startswith("/built/"):
            parts = path.split("/")
            built_wheel = self.local_wheels.get(parts[2]) if len(parts) == 4 else None
            if built_wheel and parts[3] == built_wheel.name:
                return built_wheel, "application/octet-stream"
            raise ValueError("Unknown built wheel")
        if path.startswith("/files/") and path[7:] in self.urls:
            key = path[7:]
            cache = self.root / hashlib.sha256(key.encode()).hexdigest()
            with self.lock:
                file_lock = self.file_locks.setdefault(key, threading.Lock())
            with file_lock:
                if self.stopping.is_set():
                    raise httpx.ReadError("Package setup stopped")
                if not cache.exists():
                    # Retried/concurrent clients share one verified download, even when the
                    # original client times out before a large CUDA wheel is ready.
                    temporary = cache.with_suffix(".tmp")
                    try:
                        self.fetch(self.urls[key], destination=temporary)
                        if key.endswith(".metadata"):
                            check_metadata(temporary.read_bytes(), self.denied)
                        else:
                            check_wheel(temporary, self.denied, self.max_bytes)
                        temporary.replace(cache)
                    finally:
                        temporary.unlink(missing_ok=True)
            return cache, "application/octet-stream"
        raise ValueError("Unrecognized package URL")
