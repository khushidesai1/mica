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
from contextlib import AbstractContextManager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit

import httpx
from packaging.utils import canonicalize_name

from .runtime_wheels import check_metadata, check_wheel


class WheelMirror(AbstractContextManager["WheelMirror"]):
    def __init__(self, root: Path, denied: set[str], max_bytes: int) -> None:
        self.root, self.denied, self.max_bytes = root, denied, max_bytes
        self.urls: dict[str, str] = {}
        self.local_wheels: dict[str, Path] = {}
        self.lock = threading.Lock()
        self.used = 0
        mirror = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

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
                except (ValueError, httpx.HTTPError, OSError):
                    self.send_error(403, "Package source refused by runtime policy")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
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
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def fetch(self, url: str, *, metadata: bool = False, destination: Path | None = None) -> bytes:
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
            if response.status_code != 200:
                raise ValueError("Registry fetch failed")
            body = bytearray()
            with destination.open("wb") if destination else nullcontext(None) as target:
                for chunk in response.iter_bytes():
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
                raise ValueError("Package denied")
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
            doc = json.loads(self.fetch(f"https://pypi.org/simple/{name}/", metadata=True))
            wheels = []
            for item in doc["files"]:
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
            return json.dumps(doc).encode(), "application/vnd.pypi.simple.v1+json"
        if path.startswith("/built/"):
            parts = path.split("/")
            built_wheel = self.local_wheels.get(parts[2]) if len(parts) == 4 else None
            if built_wheel and parts[3] == built_wheel.name:
                return built_wheel, "application/octet-stream"
            raise ValueError("Unknown built wheel")
        if path.startswith("/files/") and path[7:] in self.urls:
            key = path[7:]
            cache = self.root / hashlib.sha256(key.encode()).hexdigest()
            if not cache.exists():
                # Publish only a completely fetched, checked artifact to concurrent readers.
                temporary = cache.with_suffix(f".{threading.get_ident()}.tmp")
                self.fetch(self.urls[key], destination=temporary)
                if key.endswith(".metadata"):
                    check_metadata(temporary.read_bytes(), self.denied)
                else:
                    check_wheel(temporary, self.denied, self.max_bytes)
                temporary.replace(cache)
            return cache, "application/octet-stream"
        raise ValueError("Unrecognized package URL")
