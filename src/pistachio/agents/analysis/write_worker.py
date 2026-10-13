"""Stage explicitly selected outputs; only the controller can publish to granted GCS paths."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from claude_agent_sdk import SdkMcpTool, tool
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from .config import CHECKPOINT_PREFIX, Settings
from .outputs import files_under
from .storage import Conflict, Storage
from .write_broker import relative_path, staged_key

if TYPE_CHECKING:
    from .runner import Context, TurnResult

log = logging.getLogger(__name__)
CONCURRENCY = 8
TOOL_NAME = "mcp__pistachio__publish_gcs"


def file_sizes(paths: dict[str, Path]) -> dict[str, int]:
    return {name: path.stat().st_size for name, path in paths.items()}


@dataclass
class Publication:
    uploaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Publisher:
    def __init__(
        self, storage: Storage, request: dict[str, Any], event_id: str, control_url: str
    ) -> None:
        self.storage, self.request = storage, request
        self.event_id, self.url = event_id, control_url.rstrip("/")
        self.grant = request.get("write_grant")
        self.limits = Settings(
            _env_file=None,
            **{
                k: request[k]
                for k in (
                    "write_max_mb",
                    "write_max_files",
                    "write_max_total_gb",
                    "write_upload_seconds",
                )
                if k in request
            },
        )
        self.lock = asyncio.Lock()
        self.sizes: dict[str, int] = {}
        self.uploaded: dict[str, str] = {}
        self.loaded = False
        self.denied = False
        # Transport state, not authority. The broker still checks the grant on every chunk
        # and writes the canonical per-object receipt before acknowledging publication.
        self.key = f"{CHECKPOINT_PREFIX}/events/{event_id}/write-publication.json"

    async def save(self) -> None:
        await asyncio.to_thread(
            self.storage.put,
            self.key,
            json.dumps({"sizes": self.sizes, "uploaded": self.uploaded}).encode(),
        )

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
        # Parallel tool calls share one preflight transaction; individual objects transfer
        # concurrently, retaining the controller's per-object mutex.
        async with self.lock:
            outcome = Publication()
            try:
                if not self.loaded:
                    try:
                        state = json.loads((await asyncio.to_thread(self.storage.get, self.key))[0])
                        self.sizes, self.uploaded = state["sizes"], state["uploaded"]
                    except FileNotFoundError:
                        pass
                    self.loaded = True
                paths = self.select(ctx, result, folder)
                root = ctx.workdir / "outputs/gcs"
                selected = {relative_path(p.relative_to(root).as_posix()): p for p in paths}
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
                    if any(n > self.limits.write_max_mb * 1024**2 for n in sizes.values()):
                        raise ValueError("GCS files exceed the per-file size limit; none uploaded.")
                    if any(p in self.sizes and self.sizes[p] != n for p, n in sizes.items()):
                        raise ValueError("A staged output changed. Choose a new destination path.")
                    reserved = self.sizes | sizes
                    if len(reserved) > self.limits.write_max_files:
                        raise ValueError(
                            f"GCS publishing supports {self.limits.write_max_files} files per run; "
                            "none uploaded."
                        )
                    if sum(reserved.values()) > self.limits.write_max_total_gb * 1024**3:
                        raise ValueError(
                            "GCS files exceed the total per-run size limit; none uploaded."
                        )
                    self.sizes = reserved
                    await self.save()  # Count unfinished attempts too, including after restart.
                    await self.transfer(pending, outcome)
                    await self.save()
                outcome.uploaded = [
                    uri
                    for p, uri in sorted(self.uploaded.items())
                    if folder is None or p in selected
                ]
            except (ValueError, OSError) as exc:
                outcome.errors.append(str(exc))
            except Exception:
                log.exception("GCS output publication failed for %s", self.event_id)
                outcome.errors.append(
                    "GCS delivery could not be confirmed; staged files are preserved."
                )
            if folder is None:
                # A later failure cannot undo a successful mid-turn publish.
                outcome.uploaded = sorted(self.uploaded.values())
            return outcome

    async def transfer(self, paths: dict[str, Path], outcome: Publication) -> None:
        pending = iter(paths.items())
        async with httpx.AsyncClient(timeout=self.limits.write_upload_seconds + 30) as client:

            async def upload_next() -> None:
                while not self.denied:
                    item = next(pending, None)
                    if item is None:
                        return
                    relative, path = item
                    try:
                        # Never restage on retry: a new source generation would invalidate the
                        # destination's receipt after a lost response from the broker.
                        with suppress(Conflict):
                            await asyncio.to_thread(
                                self.storage.upload_new, staged_key(self.event_id, relative), path
                            )
                        if self.denied:
                            return
                        auth_request = GoogleRequest()  # type: ignore[no-untyped-call]
                        token = await asyncio.to_thread(
                            id_token.fetch_id_token, auth_request, self.url
                        )
                        assert self.grant is not None
                        response = await client.post(
                            f"{self.url}/api/v1/internal/slack-analysis/write/{self.event_id}",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"relative_path": relative, "grant_id": self.grant["id"]},
                        )
                        if response.status_code in {403, 409, 413, 422}:
                            detail = response.json().get("detail", "Upload was refused.")
                            outcome.errors.append(f"{relative}: {detail}")
                            if response.status_code == 403:
                                self.denied = True
                            continue
                        response.raise_for_status()
                        self.uploaded[relative] = str(response.json()["uri"])
                    except Exception:
                        log.exception(
                            "GCS output publication failed for %s/%s", self.event_id, relative
                        )
                        outcome.errors.append(
                            f"{relative}: delivery unconfirmed; staged copy preserved."
                        )

            await asyncio.gather(*(upload_next() for _ in range(min(CONCURRENCY, len(paths)))))

    def tool(self, ctx: Context) -> SdkMcpTool[Any]:
        from .runner import TurnResult

        @tool(
            "publish_gcs",
            "Publish a completed, checked folder under outputs/gcs/ to this turn's GCS grant. "
            "Preserves paths and creates new objects only. Returns confirmed URIs and errors.",
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
                "content": [{"type": "text", "text": json.dumps(asdict(result))}],
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
