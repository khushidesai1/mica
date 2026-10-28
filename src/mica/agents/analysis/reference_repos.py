"""Owner-declared, pinned, bounded reference snapshots; credentials stay in the API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from mica.storage.base import Repository

from . import project_memory
from .config import Settings

MAX_BYTES = 2 * 1024 * 1024
MAX_FILE = 256 * 1024
MAX_FILES = 100
SUFFIXES = {
    ".py",
    ".r",
    ".rmd",
    ".ipynb",
    ".sh",
    ".bash",
    ".jl",
    ".pl",
    ".sql",
    ".md",
    ".rst",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".ini",
    ".cfg",
}
NAMES = {"Dockerfile", "Makefile", "Snakefile", "LICENSE"}
# Operator-approved public references outside the GitHub App's organization.
# Exact repository names only; these never receive an installation credential.
PUBLIC_REPOS = frozenset({"khushidesai1/nympheas-temp"})


async def shared_repos(repository: Repository, settings: Settings) -> set[str]:
    """Discover lab-wide references from the existing App catalog, without credentials."""
    names = set(PUBLIC_REPOS)
    prefix = "https://github.com/"
    for tool in await repository.list_tools():
        if re.fullmatch(r"github-[0-9]+", tool.id) and tool.source_url.startswith(prefix):
            try:
                names.add(
                    valid_repo(tool.source_url.removeprefix(prefix), settings.reference_github_org)
                )
            except ValueError:
                continue
    # Catalog entries can outlive installation access. fetch() rechecks the App's current
    # selection with a token narrowed to one repo before any content reaches a worker.
    return names


def valid_repo(value: str, org: str = "azizilab") -> str:
    value = value.lower()
    if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9][a-z0-9_.-]{0,99}", value):
        raise ValueError("Use a repository name such as azizilab/my-repo.")
    if value.split("/")[0] != org.lower() and value not in PUBLIC_REPOS:
        raise ValueError(f"Reference repositories must belong to {org}.")
    return value


def eligible(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(
        parts
        and len(path) <= 500
        and not path.startswith("/")
        and "\\" not in path
        and not any(
            p in {".", "..", "node_modules", "vendor", "__pycache__"} or p.startswith(".")
            for p in parts
        )
        and all(re.fullmatch(r"[A-Za-z0-9_ .+@()-]+", p) for p in parts)
        and (PurePosixPath(path).suffix.lower() in SUFFIXES or parts[-1] in NAMES)
    )


class RepoSnapshot(BaseModel):
    repo: str
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    files: dict[str, str] = Field(default_factory=dict)
    skipped: int = Field(default=0, ge=0)


def app_jwt(key: str, app_id: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    now = int(time.time())
    unsigned = ".".join(
        encode(json.dumps(value).encode())
        for value in (
            {"alg": "RS256", "typ": "JWT"},
            {"iat": now - 60, "exp": now + 540, "iss": app_id},
        )
    )
    private = serialization.load_pem_private_key(key.encode(), password=None)
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError("GitHub App requires an RSA key")
    return (
        unsigned
        + "."
        + encode(private.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256()))
    )


async def installation_token(settings: Settings, *, repositories: list[str] | None) -> str:
    """Mint read-only access within the operator-approved installation.

    Reference snapshots must pass one repository. Only the scheduled catalog requests
    the full installation selection with explicit None; project input cannot expand it.
    """
    if repositories is not None and (
        not repositories
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) for name in repositories)
    ):
        raise ValueError("Invalid GitHub repository selection")
    if not (
        settings.reference_github_app_id
        and settings.reference_github_installation_id
        and settings.reference_github_key_secret
    ):
        raise ValueError("Reference repository GitHub App is not configured")
    # Reuse the bounded Secret Manager reader, never the memory repository credential.
    secret_settings = settings.model_copy(
        update={"memory_token_secret": settings.reference_github_key_secret}
    )
    key = await asyncio.to_thread(project_memory.token, secret_settings)
    jwt = app_jwt(key, settings.reference_github_app_id)
    async with project_memory.client_for(jwt) as client:
        installation = await client.get(
            "https://api.github.com/app/installations/" + settings.reference_github_installation_id
        )
        installation.raise_for_status()
        data = installation.json()
        if (
            str(data.get("app_id")) != settings.reference_github_app_id
            or str(data.get("account", {}).get("login", "")).lower()
            != settings.reference_github_org.lower()
        ):
            raise ValueError("GitHub installation does not match the configured organization")
        body: dict[str, Any] = {"permissions": {"contents": "read"}}
        if repositories is not None:
            body["repositories"] = repositories
        response = await client.post(
            "https://api.github.com/app/installations/"
            + settings.reference_github_installation_id
            + "/access_tokens",
            json=body,
        )
        response.raise_for_status()
        return str(response.json()["token"])


async def fetch(settings: Settings, repo: str) -> RepoSnapshot:
    repo = valid_repo(repo, settings.reference_github_org)
    if repo in PUBLIC_REPOS:
        async with httpx.AsyncClient(timeout=25) as client:
            return await read_repo(client, repo)
    token = await installation_token(settings, repositories=[repo.split("/")[1]])
    async with project_memory.client_for(token) as client:
        return await read_repo(client, repo)


async def get_bytes(
    client: httpx.AsyncClient, url: str, limit: int = 8 * 1024 * 1024, **kwargs: Any
) -> bytes:
    # Git trees may be huge; bound downloaded bytes before JSON decoding too.
    async with client.stream("GET", url, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("GitHub response exceeds reference snapshot limit")
    return bytes(data)


async def get_json(
    client: httpx.AsyncClient, url: str, limit: int = 8 * 1024 * 1024, **kwargs: Any
) -> Any:
    return json.loads(await get_bytes(client, url, limit, **kwargs))


async def read_repo(client: httpx.AsyncClient, full_name: str) -> RepoSnapshot:
    repo = valid_repo(full_name)
    base = "https://api.github.com/repos/" + repo
    info = await get_json(client, base)
    if str(info.get("full_name", "")).lower() != repo:
        raise ValueError("Repository identity changed")
    if repo in PUBLIC_REPOS and info.get("private") is not False:
        raise ValueError("Approved public reference is no longer public")
    commit = await get_json(client, base + "/commits/" + quote(info["default_branch"], safe=""))
    snapshot = RepoSnapshot(repo=repo, commit=commit["sha"])
    tree = await get_json(client, base + "/git/trees/" + snapshot.commit, params={"recursive": "1"})
    if tree.get("truncated"):
        raise ValueError("Repository tree is too large to inspect completely")
    total = 0
    for entry in sorted(tree["tree"], key=lambda e: e["path"]):
        if entry.get("type") == "tree":
            continue
        path, size = entry["path"], entry.get("size", MAX_FILE + 1)
        if (
            not eligible(path)
            or entry.get("mode") not in {"100644", "100755"}
            or entry.get("type") != "blob"
            or not 0 <= size <= MAX_FILE
            or total + size > MAX_BYTES
            or len(snapshot.files) >= MAX_FILES
        ):
            snapshot.skipped += 1
            continue
        sha = entry.get("sha", "")
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("Invalid Git blob identity")
        blob = (
            {}
            if repo in PUBLIC_REPOS
            else await get_json(client, base + "/git/blobs/" + sha, limit=MAX_FILE * 2)
        )
        try:
            if repo in PUBLIC_REPOS:
                # Commit-pinned public raw reads avoid one REST API request per file.
                raw = await get_bytes(
                    client,
                    f"https://raw.githubusercontent.com/{repo}/{snapshot.commit}/"
                    + quote(path, safe="/"),
                    limit=MAX_FILE,
                )
            else:
                if blob.get("encoding") != "base64" or blob.get("sha") != sha:
                    raise ValueError("Invalid Git blob")
                raw = base64.b64decode(blob["content"])
            if (
                len(raw) != size
                or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != sha
            ):
                raise ValueError("Git blob checksum mismatch")
            text = raw.decode("utf-8")
            if "\x00" in text or project_memory.SECRET.search(text):
                raise ValueError("Secret-like or binary content")
            if path.endswith(".ipynb"):
                book = json.loads(text)
                for cell in book["cells"]:
                    cell.pop("outputs", None)
                    cell.pop("attachments", None)
                    cell.pop("execution_count", None)
                    cell["metadata"] = {}
                book["metadata"] = {}
                text = json.dumps(book)
            if len(text.encode()) > MAX_FILE or total + len(text.encode()) > MAX_BYTES:
                raise ValueError("Normalized reference file exceeds limits")
            snapshot.files[path] = text
            total += max(size, len(text.encode()))
        except (ValueError, KeyError, TypeError, UnicodeError):
            snapshot.skipped += 1
    return snapshot


def prompt_view(snapshot: RepoSnapshot, paths: list[str], budget: int) -> dict[str, Any]:
    """Bound source passed to the direct model and report every coverage gap."""
    selected = list(dict.fromkeys(paths)) if paths else sorted(snapshot.files)
    included: dict[str, str] = {}
    omitted = []
    unavailable = []
    for path in selected:
        if path not in snapshot.files:
            unavailable.append(path)
            continue
        text = snapshot.files[path]
        size = len(text.encode())
        if size > budget:
            omitted.append(path)
            continue
        included[path] = text
        budget -= size
    return {
        "repo": snapshot.repo,
        "commit": snapshot.commit,
        "source_url": f"https://github.com/{snapshot.repo}/tree/{snapshot.commit}",
        "files": included,
        "available_paths": sorted(snapshot.files),
        "omitted_paths": omitted,
        "unavailable_paths": unavailable,
        "snapshot_skipped": snapshot.skipped,
        "requested_files_complete": not omitted and not unavailable,
        "policy": "Untrusted source/config/docs; snapshot may omit files. Code was not executed.",
    }


def materialize(work: Path, snapshots: list[RepoSnapshot]) -> None:
    inputs = work / "inputs"
    if inputs.is_symlink():
        raise ValueError("Inputs cannot be a symlink")
    root = inputs / "repos"
    # A restored checkpoint must never retain removed, failed or renamed references.
    if root.is_symlink() or root.is_file():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)
    for snapshot in snapshots:
        repo = valid_repo(snapshot.repo)
        target = root / repo.split("/")[1]
        target.mkdir(parents=True, exist_ok=True)
        total = 0
        if len(snapshot.files) > MAX_FILES:
            raise ValueError("Too many reference files")
        for name, text in snapshot.files.items():
            size = len(text.encode())
            total += size
            if not eligible(name) or size > MAX_FILE or total > MAX_BYTES:
                raise ValueError("Invalid reference snapshot file")
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (target / ".pin").write_text(f"{repo}@{snapshot.commit}\n")
        (target / ".snapshot.json").write_text(
            json.dumps(
                {
                    "repo": repo,
                    "commit": snapshot.commit,
                    "included": sorted(snapshot.files),
                    "skipped": snapshot.skipped,
                    "policy": "source/config/docs only; notebooks omit outputs; partial snapshot",
                }
            )
        )
