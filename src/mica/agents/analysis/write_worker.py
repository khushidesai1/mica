"""Stage explicitly selected outputs; only the controller can publish to granted GCS paths."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from claude_agent_sdk import SdkMcpTool, tool
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from .config import Settings
from .outputs import files_under
from .storage import Conflict, Storage
from .write_broker import BATCH_SIZE, manifest_stage, relative_path, staged_key, valid_shard

if TYPE_CHECKING:
    from .runner import Context, TurnResult

log = logging.getLogger(__name__)
CONCURRENCY = 8
TOOL_NAME = "mcp__pistachio__publish_gcs"


def file_sizes(paths: dict[str, Path]) -> dict[str, int]:
    return {name: path.stat().st_size for name, path in paths.items()}


@dataclass
class Destination:
    uri: str
    files: int = 0
    bytes: int = 0
    errors: int = 0


@dataclass
class Publication:
    uploaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    destinations: list[Destination] = field(default_factory=list)
    manifest: str = ""

    @property
    def failed_count(self) -> int:
        return max(len(self.errors), sum(item.errors for item in self.destinations))

    def tool_result(self) -> dict[str, Any]:
        return {
            "destinations": [asdict(item) for item in self.destinations],
            "files": len(self.uploaded),
            "bytes": sum(item.bytes for item in self.destinations),
            "failed": self.failed_count,
            "error_examples": [error[:500] for error in self.errors[:5]],
            "manifest": self.manifest,
        }

    def receipt(self) -> str:
        lines = [
            f"Saved to GCS: `{item.uri}` — {item.files:,} files, {format_bytes(item.bytes)}."
            for item in self.destinations
            if item.files
        ]
        if self.failed_count:
            examples = "; ".join(error[:300] for error in self.errors[:3])
            lines.append(f"GCS publishing: {self.failed_count:,} failed. " + examples)
        return "\n".join(lines)


def format_bytes(size: int) -> str:
    for suffix, unit in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if size >= unit:
            return f"{size / unit:.1f} {suffix}"
    return f"{size:,} bytes"


class Publisher:
    def __init__(
        self, storage: Storage, request: dict[str, Any], event_id: str, control_url: str
    ) -> None:
        self.storage, self.request = storage, request
        self.event_id, self.url = event_id, control_url.rstrip("/")
        self.grant = request.get("write_grant")
        self.limits = Settings(
            _env_file=None, write_upload_seconds=request.get("write_upload_seconds", 840)
        )
        self.lock = asyncio.Lock()
        self.sizes: dict[str, int] = {}
        self.uploaded: dict[str, str] = {}
        self.destinations: dict[str, str] = {}
        self.manifests: list[str] = []
        self.shards: list[str] = []
        self.state_lock = asyncio.Lock()
        self.loaded = False
        self.denied = False
        self.stopped = False
        self.active: set[asyncio.Task[Any]] = set()
        self.on_stop: Callable[[str], None] | None = None
        self.identity_unavailable_since: float | None = None
        self.key = manifest_stage(event_id)
        self.shard_prefix = self.key.removesuffix("manifest.json") + "shards/"

    def identity_retry(self) -> None:
        now = time.monotonic()
        if self.identity_unavailable_since is None:
            self.identity_unavailable_since = now
        if now - self.identity_unavailable_since >= self.limits.access_unavailable_grace_seconds:
            self.stopped = True
            if self.on_stop:
                self.on_stop("access_unverifiable")
            raise PermissionError("Slack access could not be verified; publication stopped.")

    async def save(self, rows: list[dict[str, Any]] | None = None) -> None:
        async with self.state_lock:
            if rows:
                key = self.shard_prefix + uuid.uuid4().hex + ".jsonl"
                data = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
                await asyncio.to_thread(self.storage.put_if, key, data, None)
                self.shards.append(key)
            index = {
                "version": 2,
                "event_id": self.event_id,
                "shards": self.shards,
                "publications": self.manifests,
                "destinations": [asdict(item) for item in self.summarize(set(self.sizes))],
            }
            await asyncio.to_thread(self.storage.put, self.key, json.dumps(index).encode())

    def load(self) -> None:
        try:
            index = json.loads(self.storage.get(self.key)[0])
        except FileNotFoundError:
            return
        self.shards = index.get("shards", [])
        self.manifests = index.get("publications", [])
        for key in self.shards:
            if not valid_shard(self.event_id, key):
                raise ValueError("Invalid publication state shard.")
            with self.storage.open_read(key) as stream:
                for line in stream:
                    row = json.loads(line)
                    path = relative_path(row["relative_path"])
                    self.sizes[path] = row["bytes"]
                    self.destinations[path] = row["folder"]
                    if row["status"] == "uploaded":
                        self.uploaded[path] = row["uri"]

    def rows(self, paths: list[str], errors: dict[str, str] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "relative_path": p,
                "bytes": self.sizes[p],
                "folder": self.destinations[p],
                "status": "uploaded" if p in self.uploaded else "pending",
                **({"uri": self.uploaded[p]} if p in self.uploaded else {}),
                **({"error": errors[p]} if errors and p in errors else {}),
            }
            for p in paths
        ]

    def published_paths(self, workdir: Path) -> set[Path]:
        return {workdir / "outputs/gcs" / relative for relative in self.uploaded}

    def select(self, ctx: Context, result: TurnResult, folder: str | None) -> list[Path]:
        root = ctx.workdir / "outputs/gcs"
        if folder is not None:
            relative_path(folder)
            if not folder.startswith("outputs/gcs/"):
                raise ValueError("Choose a complete folder under outputs/gcs/.")
            target = ctx.workdir / folder
            if (
                not target.is_dir()
                or target.is_symlink()
                or any(p.is_symlink() for p in target.parents)
                or not target.resolve().is_relative_to(root.resolve())
            ):
                raise ValueError("Choose an existing folder without symlinks under outputs/gcs/.")
            if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in target.rglob("*")):
                raise ValueError("Result folder contains a symlink or special file; none uploaded.")
            return files_under(target)
        selected = set(files_under(root))
        return sorted(set(result.files) & selected)

    async def publish(
        self, ctx: Context, result: TurnResult, *, folder: str | None = None
    ) -> Publication:
        if self.stopped:
            return Publication(errors=["This run has stopped; publication was cancelled."])
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            return await self._publish(ctx, result, folder=folder)
        finally:
            self.active.discard(task)

    async def cancel(self) -> None:
        """Cancel HTTP/tool waits without acquiring the lock held by publication."""
        self.stopped = True
        active = list(self.active)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await self.save()

    async def _publish(
        self, ctx: Context, result: TurnResult, *, folder: str | None = None
    ) -> Publication:
        # Parallel tool calls share one preflight transaction; individual objects transfer
        # concurrently, retaining the controller's per-object mutex.
        async with self.lock:
            outcome = Publication()
            selected: dict[str, Path] = {}
            try:
                if not self.loaded:
                    await asyncio.to_thread(self.load)
                    self.loaded = True
                paths = self.select(ctx, result, folder)
                root = ctx.workdir / "outputs/gcs"
                selected = {relative_path(p.relative_to(root).as_posix()): p for p in paths}
                for path in selected:
                    group = (
                        folder.removeprefix("outputs/gcs/").rstrip("/") + "/"
                        if folder
                        else (path.split("/", 1)[0] + "/" if "/" in path else "")
                    )
                    self.destinations.setdefault(path, group)
                pending = {p: file for p, file in selected.items() if p not in self.uploaded}
                if pending:
                    if result.error:
                        raise ValueError(
                            "GCS files were not uploaded because computation stopped early."
                        )
                    if not self.grant:
                        raise ValueError(
                            "GCS files were not uploaded: this run has no write grant."
                        )
                    if self.denied:
                        raise ValueError("GCS write access was revoked or changed during this run.")
                    sizes = file_sizes(pending)
                    if any(p in self.sizes and self.sizes[p] != n for p, n in sizes.items()):
                        raise ValueError("A staged output changed. Choose a new destination path.")
                    self.sizes.update(sizes)
                    # Reserve the whole folder so a stop reports the intended total, even
                    # when only the first batch reached the controller.
                    names = list(pending)
                    for offset in range(0, len(names), BATCH_SIZE):
                        await self.save(self.rows(names[offset : offset + BATCH_SIZE]))
                    await self.transfer(pending, outcome)
            except (ValueError, OSError) as exc:
                outcome.errors.append(str(exc))
            except Exception:
                log.exception("GCS output publication failed for %s", self.event_id)
                outcome.errors.append(
                    "GCS delivery could not be confirmed; staged files are preserved."
                )
            # A later failure cannot undo any controller-confirmed publication.
            outcome.uploaded = [
                uri
                for path, uri in sorted(self.uploaded.items())
                if folder is None or path in selected
            ]
            scope = set(selected) | (set(self.uploaded) if folder is None else set())
            outcome.destinations = self.summarize(scope)
            if scope or outcome.errors:
                try:
                    await self.write_manifest(ctx, outcome)
                except Exception:
                    log.exception("GCS manifest could not be saved for %s", self.event_id)
                    outcome.errors.append("Full GCS publication manifest could not be saved.")
            return outcome

    def summarize(self, scope: set[str]) -> list[Destination]:
        groups: dict[str, Destination] = {}
        for path in sorted(scope):
            prefix = self.destinations.get(path, path.split("/", 1)[0] + "/" if "/" in path else "")
            root = str((self.grant or {}).get("uri", ""))
            if not root and path in self.uploaded:
                root = self.uploaded[path].rsplit("/", len(Path(path).parts))[0] + "/"
            if not root:
                continue
            uri = root.rstrip("/") + "/" + prefix
            item = groups.setdefault(uri, Destination(uri))
            if path in self.uploaded:
                item.files += 1
                item.bytes += self.sizes.get(path, 0)
            else:
                item.errors += 1
        return list(groups.values())

    async def write_manifest(self, ctx: Context, outcome: Publication) -> None:
        identifier = uuid.uuid4().hex
        relative = f"logs/gcs-publish-{identifier}.json"
        logs = ctx.workdir / "logs"
        if logs.is_symlink() or not logs.resolve().is_relative_to(ctx.workdir.resolve()):
            raise ValueError("Publication logs must remain inside the workspace")
        logs.mkdir(exist_ok=True)
        outcome.manifest = relative
        data = json.dumps(
            {
                "event_id": self.event_id,
                **outcome.tool_result(),
                "shards": self.shards,
            }
        ).encode()
        # Unique manifests never replace a prior call or follow a pre-existing file symlink.
        with (ctx.workdir / relative).open("xb") as stream:
            stream.write(data)
        if self.grant:
            key = f"write-access/uploads/{self.event_id}/publications/{identifier}/manifest.json"
            # Workers can write their event transport area, never controller audit records.
            await asyncio.to_thread(
                self.storage.put_if, manifest_stage(self.event_id, identifier), data, None
            )
            self.manifests.append(key)
            await self.save()

    async def transfer(self, paths: dict[str, Path], outcome: Publication) -> None:
        pending = iter(paths.items())
        staging = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(timeout=self.limits.write_upload_seconds + 30) as client:

            async def stage(relative: str, path: Path) -> None:
                async with staging:
                    with suppress(Conflict):
                        await asyncio.to_thread(
                            self.storage.upload_new, staged_key(self.event_id, relative), path
                        )

            async def upload_next() -> None:
                while not self.denied:
                    batch = dict(islice(pending, BATCH_SIZE))
                    if not batch:
                        return
                    errors: dict[str, str] = {}
                    ready = []
                    staged = await asyncio.gather(
                        *(stage(p, f) for p, f in batch.items()), return_exceptions=True
                    )
                    for path, result in zip(batch, staged, strict=True):
                        if isinstance(result, BaseException):
                            errors[path] = "Could not stage output; retry publication."
                        else:
                            ready.append(path)
                    attempts = 0
                    while ready and not self.denied:
                        try:
                            auth_request = GoogleRequest()  # type: ignore[no-untyped-call]
                            token = await asyncio.to_thread(
                                id_token.fetch_id_token, auth_request, self.url
                            )
                            assert self.grant is not None
                            response = await client.post(
                                f"{self.url}/api/v1/internal/slack-analysis/write/{self.event_id}",
                                headers={"Authorization": f"Bearer {token}"},
                                json={"relative_paths": ready, "grant_id": self.grant["id"]},
                            )
                            if (
                                response.status_code == 503
                                and "identity verification" in response.text
                            ):
                                self.identity_retry()
                                await asyncio.sleep(2)
                                continue
                            if response.status_code in {403, 422}:
                                detail = response.json().get("detail", "Upload was refused.")
                                errors.update(dict.fromkeys(ready, detail))
                                self.denied = response.status_code == 403
                                break
                            response.raise_for_status()
                            results = response.json()["results"]
                            if any(
                                item.get("reason") == "identity_unavailable" for item in results
                            ):
                                self.identity_retry()
                            else:
                                self.identity_unavailable_since = None
                            if {r["relative_path"] for r in results} != set(ready):
                                raise ValueError("Incomplete broker batch response")
                            retry = []
                            for item in results:
                                path = item["relative_path"]
                                if item["status"] == "uploaded":
                                    uri = str(item["uri"])
                                    if uri != self.grant["uri"] + path:
                                        raise ValueError("Unexpected publication destination")
                                    self.uploaded[path] = uri
                                elif item["status"] == "in_progress":
                                    retry.append(path)
                                else:
                                    errors[path] = item.get("error", "Upload was refused.")
                                    if item["status"] == "denied":
                                        self.denied = True
                            await self.save(self.rows(ready, errors))
                            ready = retry
                            attempts = 0
                            if ready:
                                await asyncio.sleep(1)
                        except (httpx.HTTPError, ValueError, KeyError):
                            attempts += 1
                            if attempts >= 3:
                                errors.update(
                                    dict.fromkeys(
                                        ready, "Delivery unconfirmed; staged copy preserved."
                                    )
                                )
                                break
                            await asyncio.sleep(attempts)
                    if self.denied:
                        for p in ready:
                            if p not in self.uploaded:
                                errors.setdefault(p, "GCS write access changed.")
                    await self.save(self.rows(list(batch), errors))
                    # Details stay in shards; user-facing examples remain bounded.
                    outcome.errors.extend(f"{p}: {e}" for p, e in list(errors.items())[:5])

            await asyncio.gather(*(upload_next() for _ in range(min(CONCURRENCY, len(paths)))))

    def tool(self, ctx: Context) -> SdkMcpTool[Any]:
        from .runner import TurnResult

        @tool(
            "publish_gcs",
            "Publish a completed, checked folder under outputs/gcs/ to this turn's GCS grant. "
            "Preserves paths, creating files or replacing versions approved for this run. "
            "Returns folder totals and errors; "
            "the manifest links to per-batch publication details.",
            {
                "type": "object",
                "properties": {"folder": {"type": "string"}},
                "required": ["folder"],
                "additionalProperties": False,
            },
        )
        async def publish_folder(args: dict[str, Any]) -> dict[str, Any]:
            folder = args.get("folder")
            if not isinstance(folder, str) or set(args) != {"folder"}:
                result = Publication(errors=["Provide a folder under outputs/gcs/."])
            else:
                result = await self.publish(ctx, TurnResult(), folder=folder)
            return {
                "content": [{"type": "text", "text": json.dumps(result.tool_result())}],
                "isError": bool(result.errors),
            }

        return publish_folder


async def publish(
    storage: Storage,
    request: dict[str, Any],
    event_id: str,
    control_url: str,
    ctx: Context,
    result: TurnResult,
) -> Publication:
    return await Publisher(storage, request, event_id, control_url).publish(ctx, result)
