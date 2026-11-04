"""In-process MCP provisioning tool and per-turn runtime provenance."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from .config import CHECKPOINT_PREFIX, Settings
from .runtime_install import Installer, RuntimeFailure, RuntimeRecord
from .runtime_policy import RuntimePolicyError, normalize
from .runtime_store import RuntimeStore, encode
from .storage import Conflict, Storage
from .toolkit_catalog import CATALOG

log = logging.getLogger(__name__)
TOOL_NAME = "mcp__pistachio__ensure_packages"


@dataclass
class RuntimeEvent:
    runtime_id: str
    kind: str
    python: str
    installed: dict[str, str]
    reused: bool
    size_bytes: int
    package_count: int
    lock_sha256: str
    reason: str
    note: str = ""
    saved: bool = False
    source_built: list[dict[str, str]] = field(default_factory=list)

    def notice(self) -> str:
        if self.reused or not self.saved:
            return ""
        packages = ", ".join(f"{name} {version}" for name, version in self.installed.items())
        built = ", ".join(f"{p['name']} {p['version']}" for p in self.source_built)
        return (
            f"Set up {packages} ({self.kind} runtime, {self.package_count} packages, "
            f"{self.size_bytes / 1024**3:.2f} GB). It's saved for future runs."
            + (f" Built from pure-Python source: {built}." if built else "")
        )


class RuntimeManager:
    def __init__(
        self,
        settings: Settings,
        *,
        installer: Installer | None = None,
        principal: str = "",
        storage: Storage | None = None,
        event_id: str = "",
        attempt: int = 0,
    ) -> None:
        self.settings = settings
        self.installer = installer or Installer(settings)
        self.principal = principal
        self.event_id, self.attempt = event_id, attempt
        self.store = RuntimeStore(storage, settings) if storage is not None else None
        self.pending_restores: list[RuntimeEvent] = []
        self.events: list[RuntimeEvent] = []
        self.thread_ids: set[str] = set()
        self.records: dict[str, RuntimeRecord] = {}
        self.catalog_status: dict[str, str] = {}
        self.failures: dict[tuple[tuple[str, ...], bool], tuple[float, str]] = {}
        self.restore_errors: list[str] = []
        self.new_count = 0
        self.lock = threading.Lock()

    def begin_turn(self) -> None:
        self.events = self.pending_restores
        self.pending_restores = []
        self.new_count = sum(not event.reused for event in self.events)

    def record(
        self, record: RuntimeRecord, reason: str, *, reused: bool, saved: bool
    ) -> RuntimeEvent:
        self.records[record.id] = record
        if record.kind != "image":
            self.thread_ids.add(record.id)
        event = RuntimeEvent(
            record.id,
            record.kind,
            record.python,
            record.top_level,
            reused,
            record.size_bytes,
            len(record.packages),
            record.lock_sha256,
            reason,
            record.note,
            saved,
            record.source_built,
        )
        self.events.append(event)
        return event

    def ensure(self, packages: list[str], reason: str, isolated: bool = False) -> RuntimeEvent:
        if not self.settings.runtimes_enabled:
            raise RuntimeFailure("Package runtimes are disabled.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise RuntimeFailure("Give a reason between 1 and 200 characters.")
        if not isinstance(isolated, bool):
            raise RuntimeFailure("isolated must be a boolean.")
        request = normalize(packages, denylist=self.settings.csv("runtime_denylist"))
        with self.lock:
            if any(entry.kind == "image" for entry in request.entries):
                record = self.installer.create(request, reason=reason, principal=self.principal)
                return self.record(record, reason, reused=True, saved=False)
            if request.sources:
                request = self.installer.pin_request(request)
            base = self.installer.identity() if self.store else None
            if self.store and base:
                cached = self.store.find(request, base, isolated=isolated)
                if cached:
                    self.store.restore(cached, self.installer)
                    return self.record(cached, reason, reused=True, saved=True)
                failure_key = self.store.failure_key(request, base, isolated)
                failure = self.store.failure(failure_key)
                if failure:
                    raise RuntimeFailure(
                        "Previous setup failed; retry after cache expiry or change the inputs. "
                        + failure
                    )
            else:
                failure_key = ""
            local_key = (request.specs, isolated)
            previous = self.failures.get(local_key)
            if (
                not self.store
                and previous
                and (time.time() - previous[0] < self.settings.runtime_failure_ttl_hours * 3600)
            ):
                raise RuntimeFailure(previous[1])
            if self.new_count >= self.settings.runtime_max_new_per_turn:
                raise RuntimeFailure("This turn's new-runtime limit has been reached.")
            self.new_count += 1
            try:
                record = self.installer.create(
                    request, reason=reason, isolated=isolated, principal=self.principal
                )
            except RuntimePolicyError:
                raise
            except (ValueError, OSError) as exc:
                self.failures[local_key] = (time.time(), str(exc)[-3000:])
                if self.store:
                    self.store.save_failure(failure_key, str(exc))
                raise RuntimeFailure(str(exc)[-3000:]) from exc
            reused = False
            if self.store:
                created = record
                try:
                    record = self.store.save(record)
                except Exception:
                    # A transient publication failure must not leave an unindexed local path
                    # that blocks every later retry of the deterministic runtime ID.
                    shutil.rmtree(self.store.path(record.id), ignore_errors=True)
                    raise
                if (
                    record.created_at != created.created_at
                    or record.lock_sha256 != created.lock_sha256
                ):
                    self.store.restore(record, self.installer)
                    reused = True
                else:
                    (self.store.path(record.id) / "runtime.json").write_bytes(encode(record))
            return self.record(record, reason, reused=reused, saved=self.store is not None)

    async def report_failure(self) -> None:
        """Persist a bounded, idempotent receipt for the controller's existing polling loop."""
        if not self.store or not re.fullmatch(r"[a-f0-9]{32}", self.event_id):
            return
        receipt = {
            "event_id": self.event_id,
            "attempt": self.attempt,
            "cause": "runtime_install_failed",
        }
        key = f"{CHECKPOINT_PREFIX}/events/{self.event_id}/runtime-failure.json"
        try:
            await asyncio.to_thread(
                self.store.storage.put_if, key, json.dumps(receipt).encode(), None
            )
        except Conflict:
            pass  # One incident observation per request, including retries and worker handoffs.
        except Exception:
            log.warning("Runtime install failure receipt could not be saved")

    def restore_thread(self, ids: list[str]) -> None:
        if not self.store or not ids:
            return
        base = self.installer.identity()

        def restore_one(record: RuntimeRecord) -> RuntimeRecord:
            assert self.store is not None
            installer = Installer(self.settings, wheelhouse=self.installer.wheelhouse)
            self.store.restore(record, installer)
            return record

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = []
            for id in dict.fromkeys(ids):
                try:
                    record = self.store.load(id)
                    if record.python_version != base[0] or (
                        record.kind == "overlay" and record.base_fingerprint != base[1]
                    ):
                        # A new ID is intentional; scripts must use the returned interpreter.
                        packages = [e.name for e in CATALOG if e.name == record.catalog]
                        if not packages and record.sources:
                            raise RuntimePolicyError(
                                "Refreshing this source runtime needs a reviewed catalog entry."
                            )
                        packages = packages or record.request
                        event = self.ensure(
                            packages,
                            "Refresh stale thread runtime",
                            isolated=record.kind == "isolated",
                        )
                        event.note = f"Replaces stale {id}; update scripts to use {event.python}."
                    else:
                        futures.append(pool.submit(restore_one, record))
                except (ValueError, OSError) as exc:
                    self.restore_errors.append(f"{id}: {str(exc)[-300:]}")
            for future in futures:
                try:
                    record = future.result()
                    self.record(record, "Restored for thread continuity", reused=True, saved=True)
                except (ValueError, OSError) as exc:
                    self.restore_errors.append(str(exc)[-300:])
        self.pending_restores = list(self.events)
        self.events = []

    def prepare(self) -> None:
        if not self.store:
            return
        base = self.installer.identity()
        for entry in CATALOG:
            if entry.kind == "image":
                continue
            try:
                request = normalize([entry.name], denylist=self.settings.csv("runtime_denylist"))
            except ValueError:
                self.catalog_status[entry.name] = "failed — denied by runtime policy"
                continue
            ready = self.store.find(request, base)
            if ready:
                self.catalog_status[entry.name] = "ready (saved; call ensure_packages to restore)"
            else:
                failure = self.store.failure(self.store.failure_key(request, base, False))
                if failure:
                    self.catalog_status[entry.name] = "failed — " + failure.splitlines()[-1][:150]

    def toolkit(self) -> str:
        lines = ["## Toolkit", "Preinstalled: NumPy, SciPy, scanpy, scvi-tools, squidpy."]
        for entry in CATALOG:
            ready = next((r for r in self.records.values() if r.catalog == entry.name), None)
            status = (
                f"ready — {ready.python}"
                if ready
                else "needs admin build (validation required)"
                if entry.kind == "image"
                else "known, needs setup"
            )
            if not ready and entry.name in self.catalog_status:
                status = self.catalog_status[entry.name]
            lines.append(f"- {entry.name}: {status}")
        for record in list(self.records.values())[:5]:
            if not record.catalog:
                names = ", ".join(record.top_level)
                lines.append(f"- {names}: ready — {record.python}")
        lines.extend("- Restore failed: " + error for error in self.restore_errors[:3])
        return "\n".join(lines[:15])


def ensure_tool(manager: RuntimeManager) -> SdkMcpTool[Any]:
    @tool(
        "ensure_packages",
        "Set up public Python packages in a separate offline analysis runtime. "
        "Dependencies without wheels are built from source only if they are pure Python.",
        {
            "type": "object",
            "properties": {
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 200},
                "isolated": {"type": "boolean", "default": False},
            },
            "required": ["packages", "reason"],
            "additionalProperties": False,
        },
    )
    async def ensure(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(args) - {"packages", "reason", "isolated"}:
                raise RuntimeFailure("Unexpected package setup option.")
            event = await asyncio.to_thread(
                manager.ensure,
                args.get("packages", []),
                args.get("reason", ""),
                args.get("isolated", False),
            )
            result = {**asdict(event), "status": "ready", "size_mb": event.size_bytes / 1024**2}
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except (ValueError, OSError) as exc:
            if isinstance(exc, RuntimeFailure):
                await manager.report_failure()
            message = "\n".join(str(exc).splitlines()[-30:])[-3000:]
            return {"content": [{"type": "text", "text": message}], "isError": True}
        except Exception as exc:
            await manager.report_failure()
            log.exception("Package runtime service failed")
            message = (
                f"Package setup could not finish ({type(exc).__name__}); check the worker logs."
            )
            return {"content": [{"type": "text", "text": message}], "isError": True}

    return ensure


def create_server(
    manager: RuntimeManager | None,
    extra_tools: list[SdkMcpTool[Any]] | None = None,
) -> McpSdkServerConfig:
    tools = ([ensure_tool(manager)] if manager else []) + (extra_tools or [])
    return create_sdk_mcp_server("pistachio", "1.0.0", tools)
