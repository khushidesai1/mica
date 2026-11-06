from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import platform
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    HookJSONOutput,
    SandboxSettings,
    StreamEvent,
)

from . import memory_checks, project_memory, prompts, script_capture, toolkit_catalog, write_worker
from .attachments import classify
from .config import Settings
from .deliverables import send_tool
from .limits import MAX_VISUAL_READS, PREVIEW_MAX_BYTES, SDK_MAX_BUFFER_BYTES
from .outputs import MAX_ATTACHMENT_BYTES, changed_files, retain_tool_outputs, snapshot_outputs
from .progress import ActivityState, ProgressNote, ProgressUpdate, TaskProgress, public_note
from .recovery_tools import RecoveryManager, restored_cost
from .recovery_tools import run_query as recovery_query
from .runtime_tools import TOOL_NAME, RuntimeEvent, RuntimeManager, create_server
from .storage import GCSStorage, LocalStorage
from .thread_history import History

log = logging.getLogger(__name__)
TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
VISUAL_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".svg",
    ".avif",
    ".heic",
    ".ico",
    ".pdf",
}


def is_visual(path: Path) -> bool:
    if path.suffix.lower() in VISUAL_EXTENSIONS:
        return True
    if path.is_file():
        with path.open("rb") as stream:
            return classify("", stream.read(12))[0] in {"image", "pdf"}
    return False


@dataclass
class Context:
    workdir: Path
    data_path: Path
    project_slug: str = "local"
    project_md: str = "No guide yet."
    brief_generation: str | None = None
    session_id: str | None = None
    thread_ts: str = "local"
    survey_only: bool = False


@dataclass
class TurnResult:
    session_id: str | None = None
    text: str = ""
    files: list[Path] = field(default_factory=list)
    run_log: list[dict[str, Any]] = field(default_factory=list)
    cost: float | None = None
    error: str | None = None
    duration: float = 0
    summary_generated: bool = False
    runtimes: list[RuntimeEvent] = field(default_factory=list)
    delivery_packages: dict[Path, dict[Path, str]] = field(default_factory=dict)


def build_options(
    ctx: Context,
    settings: Settings,
    *,
    sandbox: bool = True,
    memory_context: str = "",
    return_scripts: bool = False,
    capture: script_capture.Capture | None = None,
    project_name: str = "",
    write_access: str = "No GCS write grant for this run.",
    runtime_tmp: Path = Path("/tmp"),
    runtime_manager: RuntimeManager | None = None,
    compute_manager: RecoveryManager | None = None,
    gcs_publisher: write_worker.Publisher | None = None,
    history: History | None = None,
    thread_key: str = "",
    thread_context: str = "",
    memory_base: project_memory.Snapshot | None = None,
    import_highlights: list[str] | None = None,
    delivery_packages: dict[Path, dict[Path, str]] | None = None,
    attachment_limit: int = MAX_ATTACHMENT_BYTES,
    named_outputs: set[Path] | None = None,
    checks: memory_checks.Checks | None = None,
) -> ClaudeAgentOptions:
    if not sandbox and platform.system() != "Darwin":
        raise ValueError("--no-sandbox is available only for local macOS development")

    tools = ["Read", "Write", "Edit"] if ctx.survey_only else list(TOOLS)
    runtimes = settings.runtimes_enabled and not ctx.survey_only
    manager = (runtime_manager or RuntimeManager(settings)) if runtimes else None
    if manager:
        tools.append(TOOL_NAME)
    compute = compute_manager if not ctx.survey_only else None
    compute_tools = compute.tools() if compute else []
    if not ctx.survey_only:
        from . import gpu_tools

        compute_tools.extend(
            gpu_tools.tools(
                ctx.workdir,
                ctx.data_path,
                settings,
                save_result=compute.save_result if compute else None,
                records=lambda: manager.records.values() if manager else (),
            )
        )
        compute_tools.append(
            send_tool(
                ctx.workdir,
                named_outputs if named_outputs is not None else set(),
                manifest=compute.manifest if compute else None,
                prepared=delivery_packages,
                limit=attachment_limit,
            )
        )
    if gcs_publisher and gcs_publisher.grant and not ctx.survey_only:
        compute_tools.append(gcs_publisher.tool(ctx))
    if history and not ctx.survey_only:
        compute_tools.append(history.tool())
    tools.extend(f"mcp__pistachio__{t.name}" for t in compute_tools)

    from . import thread_memory

    visual_reads = 0
    capture = (
        (capture or script_capture.Capture(ctx.workdir))
        if return_scripts and not ctx.survey_only
        else None
    )

    async def guard(
        event: HookInput, _tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        nonlocal visual_reads
        if event["hook_event_name"] != "PreToolUse":
            return {}
        name, args = event["tool_name"], event["tool_input"]
        reason = ""
        visual_read = False
        if (
            name == "Skill"
            and not ctx.survey_only
            and args.get("skill") in toolkit_catalog.BUNDLED_SKILLS
        ):
            return {}
        if name not in tools or args.get("dangerouslyDisableSandbox"):
            reason = "This tool or sandbox bypass is unavailable."
        elif compute and (denied := compute.deny(name, args)):
            reason = denied
        elif name.startswith("mcp__pistachio__") and name != TOOL_NAME or name == TOOL_NAME:
            return {}
        elif (
            runtimes
            and name == "Bash"
            and re.search(
                r"\b(?:pip[0-9.]*\b[^\n;&|]*\binstall|uv\s+pip|(?:conda|mamba)\s+install)\b",
                str(args.get("command", "")),
            )
        ):
            reason = "Use ensure_packages; shell commands have no network."
        elif name != "Bash":
            raw = args.get("file_path", args.get("path", str(ctx.workdir)))
            path = (ctx.workdir / raw).resolve()
            roots = (
                [ctx.workdir]
                if ctx.survey_only or name in {"Write", "Edit"}
                else [
                    ctx.workdir,
                    ctx.data_path,
                    settings.tools_dir,
                    *([settings.runtime_root] if runtimes else []),
                ]
            )
            if (
                runtimes
                and name in {"Write", "Edit"}
                and path.is_relative_to(settings.runtime_root.resolve())
            ):
                reason = "Package runtimes are read-only; use ensure_packages."
            elif not any(path.is_relative_to(p.resolve()) for p in roots):
                reason = "Access is restricted to this workspace and its project data."
            elif name in {"Write", "Edit"} and path.is_relative_to(
                (ctx.workdir / "inputs").resolve()
            ):
                reason = "Attached inputs are read-only. Write derived files to code/ or outputs/."
            elif name == "Read" and is_visual(path):
                reason = (
                    f"Look at images through a bounded preview: run {settings.sci_python} "
                    f"{settings.tools_dir}/preview.py <file> [--page N], then Read the preview "
                    "path it prints. Leave originals in place for Slack delivery."
                )
                preview_root = ctx.workdir / ".previews"
                if (
                    not preview_root.is_symlink()
                    and path.is_relative_to(preview_root.resolve())
                    and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
                    and path.is_file()
                    and path.stat().st_size <= PREVIEW_MAX_BYTES
                ):
                    with path.open("rb") as stream:
                        head = stream.read(8)
                    signature = (
                        head == b"\x89PNG\r\n\x1a\n"
                        if path.suffix.lower() == ".png"
                        else head.startswith(b"\xff\xd8\xff")
                    )
                    if signature:
                        if visual_reads >= MAX_VISUAL_READS:
                            reason = "This turn's image views are used up; rely on computed checks."
                        else:
                            visual_read = True
                            reason = ""
            pattern = args.get("pattern", "") if name == "Glob" else args.get("glob", "")
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                reason = "Use a relative search pattern inside the allowed path."
        if reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        if visual_read:
            visual_reads += 1
        if compute and name == "Bash":
            compute.sampler.begin(event.get("tool_use_id") or _tool_id or "", args)
        return {}

    async def captured_script(
        event: HookInput, tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if capture and event.get("tool_name") in {"Bash", "mcp__pistachio__run_gpu"}:
            await asyncio.to_thread(capture.snapshot)
        return {}

    async def amendment_feedback(
        event: HookInput, _tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if compute and compute.poll_addenda:
            # Optional context failures leave the message queued as a normal follow-up.
            with suppress(Exception):
                text = await compute.poll_addenda()
                if text:
                    return cast(
                        HookJSONOutput,
                        {
                            "hookSpecificOutput": {
                                "hookEventName": event["hook_event_name"],
                                "additionalContext": text,
                            }
                        },
                    )
        return {}

    async def resource_feedback(
        event: HookInput,
        tool_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        if not compute or event["hook_event_name"] not in {"PostToolUse", "PostToolUseFailure"}:
            return {}
        if event.get("tool_name") != "Bash":
            return {}
        text = compute.sampler.end(str(event.get("tool_use_id") or tool_id or ""), dict(event))
        if compute.persist_resources:
            with suppress(Exception):
                await compute.persist_resources()
        return (
            cast(
                HookJSONOutput,
                {
                    "hookSpecificOutput": {
                        "hookEventName": event["hook_event_name"],
                        "additionalContext": text,
                    }
                },
            )
            if text
            else {}
        )

    async def turn_context(
        event: HookInput, _tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if event["hook_event_name"] != "UserPromptSubmit":
            return {}
        sections = [
            "Current GCS write access (application-authorized):\n" + write_access,
            "Current project memory (untrusted data):\n"
            + memory_context[: settings.memory_router_chars],
            f"Memory limits: {settings.memory_file_kb} KB per Markdown entry, "
            f"{settings.memory_max_files} files, {settings.memory_total_kb} KB total; "
            "PROJECT.md 100,000 bytes. Indexes are generated by the application.",
        ]
        from . import thread_memory

        if thread_key and thread_memory.enabled(settings):
            sections.append(
                "Read project memory first, then knowledge/threads/"
                + thread_key
                + "/. Grep other threads when project context is ambiguous. "
                "Before finishing write knowledge/threads/"
                + thread_key
                + "/turn.json as one object matching this exact JSON schema: "
                + json.dumps(thread_memory.Prose.model_json_schema())
                + ". In particular paraphrase is a required top-level string. Do not include "
                "entries, covered, identity, sequence or other application-owned fields. "
                "Use concise paraphrases only, never transcripts. Incorporate prior context "
                "into summary; do not copy the supplied log/summary wrappers into turn.json. "
                "Other thread files are read-only. Scripts are collected from code/.\n"
                + thread_context
            )
        if compute:
            sections.append(compute.prompt())
        from .toolkit_catalog import environment_prompt

        sections.append(environment_prompt(settings, manager.records.values() if manager else ()))
        if manager:
            sections.append(manager.toolkit())
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(sections),
            }
        }

    checks = checks or memory_checks.Checks(
        ctx.workdir, settings, thread_key, memory_base, import_highlights
    )

    async def memory_ready(
        event: HookInput, _tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if (event["hook_event_name"] != "Stop" or ctx.survey_only
                or (compute and (compute.pending or compute.stop.is_set()))):
            return {}
        assert checks is not None
        reason = await asyncio.to_thread(checks.check)
        return {"decision": "block", "reason": reason} if reason else {}

    return ClaudeAgentOptions(
        model=settings.model,
        fallback_model=settings.fallback_model or None,
        cwd=str(ctx.workdir),
        add_dirs=[] if ctx.survey_only else [str(ctx.data_path)],
        resume=ctx.session_id,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": prompts.build_system_append(
                ctx,
                settings,
                project_name,
                return_scripts=return_scripts,
            ),
        },
        setting_sources=[],
        plugins=[{"type": "local", "path": str(settings.plugin_dir)}],
        skills=list(toolkit_catalog.BUNDLED_SKILLS) if not ctx.survey_only else [],
        tools=tools + (["Skill"] if not ctx.survey_only else []),
        allowed_tools=tools,
        mcp_servers={"pistachio": create_server(manager, compute_tools)}
        if manager or compute_tools
        else {},
        disallowed_tools=["WebFetch", "WebSearch", "Agent", "AskUserQuestion", "NotebookEdit"]
        + (["Bash", "Glob", "Grep"] if ctx.survey_only else []),
        permission_mode="dontAsk",
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        max_turns=settings.max_turns,
        max_budget_usd=settings.max_budget_usd_per_turn,
        include_partial_messages=True,
        settings=json.dumps({"bashOutputMaxChars": 12000}) if history else None,
        env={
            **({"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"} if history else {}),
            **(
                {
                    "MCP_TOOL_TIMEOUT": str(
                        1000
                        * max(
                            settings.max_run_seconds
                            if gcs_publisher or settings.gpu_worker
                            else 1200,
                            settings.runtime_timeout_seconds + 60 if runtimes else 0,
                        )
                    )
                }
                if runtimes or gcs_publisher or not ctx.survey_only
                else {}
            ),
            "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
            "SLACK_BOT_TOKEN": "",
            "SLACK_APP_TOKEN": "",
            "SLACK_SIGNING_SECRET": "",
            "MICA_SLACK_BOT_TOKEN": "",
            "MICA_SLACK_SIGNING_SECRET": "",
            "PISTACHIO_SLACK_BOT_TOKEN": "",
            "PISTACHIO_SLACK_SIGNING_SECRET": "",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            # Keep a long command attached until its real result; the app owns check-ins.
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "BASH_MAX_TIMEOUT_MS": str(settings.max_run_seconds * 1000),
            # A persisted `cd` into the read-only data mount makes the next sandbox
            # fail while creating .claude metadata, before any recovery command runs.
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(ctx.workdir / ".mpl"),
            "TMPDIR": str(runtime_tmp),
        },
        hooks={
            "PreToolUse": [HookMatcher(hooks=[guard])],
            "UserPromptSubmit": [HookMatcher(hooks=[turn_context])],
            **({"Stop": [HookMatcher(hooks=[memory_ready])]}
               if not ctx.survey_only and (memory_base is not None or import_highlights is not None
                   or (thread_key and thread_memory.enabled(settings))) else {}),
            **(
                {
                    "PostToolUse": [
                        HookMatcher(
                            matcher="Bash|mcp__pistachio__run_gpu" if capture else "Bash",
                            hooks=([resource_feedback] if compute else [])
                            + ([captured_script] if capture else []),
                        ),
                        *(
                            [HookMatcher(hooks=[amendment_feedback])]
                            if compute and compute.poll_addenda
                            else []
                        ),
                    ],
                    "PostToolUseFailure": [
                        HookMatcher(
                            matcher="Bash|mcp__pistachio__run_gpu" if capture else "Bash",
                            hooks=([resource_feedback] if compute else [])
                            + ([captured_script] if capture else []),
                        ),
                        *(
                            [HookMatcher(hooks=[amendment_feedback])]
                            if compute and compute.poll_addenda
                            else []
                        ),
                    ],
                }
                if compute or capture
                else {}
            ),
        },
        sandbox=cast(
            SandboxSettings,
            {
                "enabled": sandbox,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "credentials": {"envVars": [{"name": "ANTHROPIC_API_KEY", "mode": "deny"}]},
                "network": {
                    "allowedDomains": [],
                    "deniedDomains": ["*"],
                    "allowLocalBinding": False,
                    "allowAllUnixSockets": False,
                },
                "filesystem": {
                    "allowWrite": [str(runtime_tmp)],
                    "denyWrite": [
                        str(ctx.data_path),
                        str(ctx.workdir / "inputs"),
                        *([str(settings.runtime_root)] if runtimes else []),
                    ],
                    "denyRead": [
                        "/etc/pistachio",
                        str(Path.home() / ".claude"),
                        str(settings.state_db),
                        str(settings.work_root),
                        str(settings.data_mount_root),
                    ],
                    "allowRead": [
                        str(ctx.workdir),
                        str(ctx.data_path),
                        *([str(settings.runtime_root)] if runtimes else []),
                    ],
                },
            },
        ),
    )


def describe_tool(block: ToolUseBlock) -> str:
    args = block.input
    if block.name == "mcp__pistachio__run_gpu":
        script = Path(str(args.get("script", ""))).name
        return f"Running GPU script: {script[:60]}" if script else "Running GPU computation"
    if block.name == TOOL_NAME:
        packages = args.get("packages") or ["packages"]
        return f"Setting up {str(packages[0])[:60]}…"
    if block.name == write_worker.TOOL_NAME:
        return "Publishing completed results to GCS"
    operations = {
        "mcp__pistachio__checkpoint_stage": "Saving completed work",
        "mcp__pistachio__recover_worker": "Preparing to continue on another worker",
        "mcp__pistachio__recall_thread": "Reviewing earlier work in this thread",
    }
    if block.name in operations:
        return operations[block.name]
    if block.name == "Read" and ".previews" in Path(args.get("file_path", "")).parts:
        return "Looking at a figure preview"
    if block.name == "Bash":
        return str(args.get("description") or "Running code")[:80]
    if block.name in {"Read", "Write", "Edit"}:
        verb = {"Read": "Reading", "Write": "Writing", "Edit": "Editing"}[block.name]
        return f"{verb} {Path(args.get('file_path', '')).name}"
    return "Searching files" if block.name in {"Glob", "Grep"} else "Working on the request"


def truncate(value: Any) -> Any:
    if isinstance(value, str):
        return value[:10_240]
    if isinstance(value, dict):
        return {k: truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [truncate(v) for v in value]
    return value


def tool_progress(block: ToolUseBlock) -> TaskProgress:
    phase = {
        "Read": "Inspecting the data and context…",
        "Glob": "Finding relevant files…",
        "Grep": "Finding relevant information…",
        "Write": "Preparing the analysis and outputs…",
        "Edit": "Updating the analysis…",
        "Bash": "Running analysis and checks…",
        TOOL_NAME: "Setting up tools…",
    }.get(block.name, "Working on the request…")
    if describe_tool(block) == "Looking at a figure preview":
        phase = "Checking the figure…"
    return TaskProgress(block.id, describe_tool(block), phase)


def clear_previews(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


class Runner:
    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: bool = True,
        memory_context: str = "",
        return_scripts: bool = False,
        script_manifest: dict[str, Any] | None = None,
        project_name: str = "",
        write_access: str = "No GCS write grant for this run.",
        runtime_manager: RuntimeManager | None = None,
        compute_manager: RecoveryManager | None = None,
        gcs_publisher: write_worker.Publisher | None = None,
        history: History | None = None,
        thread_key: str = "",
        thread_context: str = "",
        memory_base: project_memory.Snapshot | None = None,
        import_highlights: list[str] | None = None,
        attachment_limit: int = MAX_ATTACHMENT_BYTES,
    ) -> None:
        self.settings, self.sandbox = settings, sandbox
        self.project_name = project_name
        self.return_scripts = return_scripts
        self.attachment_limit = attachment_limit
        self.script_manifest = script_manifest
        self.script_capture: script_capture.Capture | None = None
        self.memory_context = memory_context
        self.memory_reads = 0
        self.write_access = write_access
        self.compute = compute_manager
        self.gcs_publisher = gcs_publisher
        self.history = history
        self.thread_key = thread_key
        self.thread_context = thread_context
        self.memory_base, self.import_highlights = memory_base, import_highlights
        self.last_result: TurnResult | None = None
        self.runtime_manager = runtime_manager or (
            RuntimeManager(
                settings,
                storage=(
                    LocalStorage(settings.local_storage_root)
                    if settings.storage == "local"
                    else GCSStorage(settings.bucket)
                    if settings.bucket
                    else None
                ),
            )
            if settings.runtimes_enabled
            else None
        )

    async def run_turn(
        self, ctx: Context, prompt: str, on_status: Callable[[ProgressUpdate], Awaitable[None]]
    ) -> TurnResult:
        # The SDK puts Unix bridge sockets beneath TMPDIR. Private workspace paths can
        # already exceed the socket path limit before a socket filename is appended.
        # Each invocation gets short, private scratch storage, cleaned on every exit.
        with tempfile.TemporaryDirectory(prefix="mica-", dir="/tmp") as scratch:
            return await self._run_turn(ctx, prompt, on_status, Path(scratch))

    async def _run_turn(
        self,
        ctx: Context,
        prompt: str,
        on_status: Callable[[ProgressUpdate], Awaitable[None]],
        runtime_tmp: Path,
    ) -> TurnResult:
        for name in ("code", "outputs", ".tmp", ".mpl"):
            (ctx.workdir / name).mkdir(parents=True, exist_ok=True)
        if self.compute:
            for name in ("scratch", "checkpoints"):
                (ctx.workdir / name).mkdir(exist_ok=True)
        if self.return_scripts and not ctx.survey_only:
            self.script_capture = script_capture.Capture(ctx.workdir, self.script_manifest)
            self.script_manifest = None
        else:
            self.script_capture = None
        before, start = snapshot_outputs(ctx.workdir / "outputs"), time.monotonic()
        result = TurnResult(session_id=ctx.session_id)
        delivery_packages: dict[Path, dict[Path, str]] = {}
        named_outputs: set[Path] = set()
        manager = self.runtime_manager if not ctx.survey_only else None
        if manager:
            manager.begin_turn()
            try:
                await asyncio.to_thread(manager.prepare)
            except Exception:
                log.warning("Could not refresh the runtime catalog", exc_info=True)
                manager.restore_errors.append("Saved toolkit status could not be refreshed.")
        # Keep the closing call inside the existing model allowance, including on budget stops.
        summary_budget = min(0.50, self.settings.max_budget_usd_per_turn * 0.1)
        tasks: dict[str, TaskProgress] = {}
        activity: ActivityState | None = None
        message_id: str | None = None
        pending_note: tuple[str, str] | None = None

        async def before_tool(current_id: str | None) -> None:
            nonlocal pending_note
            if pending_note and pending_note[0] == current_id:
                note = pending_note[1]
                pending_note = None
                # Confirmed narration must not become a partial answer/history on failure.
                result.text = ""
                await on_status(ProgressNote(note))

        async def signal(state: Literal["working", "thinking"]) -> None:
            nonlocal activity
            next_state = ActivityState(state)
            if next_state != activity:
                activity = next_state
                await on_status(next_state)

        previews = ctx.workdir / ".previews"
        clear_previews(previews)
        previews.mkdir()
        checks = memory_checks.Checks(
            ctx.workdir, self.settings, self.thread_key, self.memory_base, self.import_highlights
        )
        try:
            for attempt in range(2):
                details, got_result = "", False
                message_id, pending_note = None, None
                try:
                    options = build_options(
                        ctx,
                        self.settings,
                        sandbox=self.sandbox,
                        memory_context=self.memory_context,
                        return_scripts=self.return_scripts,
                        capture=self.script_capture,
                        project_name=self.project_name,
                        write_access=self.write_access,
                        runtime_tmp=runtime_tmp,
                        runtime_manager=manager,
                        compute_manager=self.compute,
                        gcs_publisher=self.gcs_publisher,
                        history=self.history,
                        thread_key=self.thread_key,
                        thread_context=self.thread_context,
                        memory_base=self.memory_base,
                        import_highlights=self.import_highlights,
                        delivery_packages=delivery_packages,
                        attachment_limit=self.attachment_limit,
                        named_outputs=named_outputs,
                        checks=checks,
                    )
                    options.max_budget_usd = self.settings.max_budget_usd_per_turn - summary_budget
                    baseline = restored_cost(ctx.session_id) if self.compute else 0.0
                    if self.compute and baseline is None:
                        baseline = self.compute.manifest.get("sdk_cost_total")
                    stream = (
                        recovery_query(prompt, options, self.compute)
                        if self.compute
                        else query(prompt=prompt, options=options)
                    )
                    await signal("thinking")
                    async for msg in stream:
                        if isinstance(msg, StreamEvent):
                            if msg.parent_tool_use_id is not None:
                                continue
                            if msg.event.get("type") == "message_start":
                                message_id = msg.event.get("message", {}).get("id")
                                pending_note = None
                            if (
                                msg.event.get("type") == "content_block_start"
                                and msg.event.get("content_block", {}).get("type") == "tool_use"
                            ):
                                await before_tool(message_id)
                            # Observe thinking phase markers, never reasoning or text deltas.
                            if (
                                msg.parent_tool_use_id is None
                                and msg.event.get("type") == "content_block_start"
                                and msg.event.get("content_block", {}).get("type") == "thinking"
                                and not any(t.status == "in_progress" for t in tasks.values())
                            ):
                                await signal("thinking")
                        elif isinstance(msg, AssistantMessage):
                            top_level = msg.parent_tool_use_id is None
                            if top_level and msg.message_id and msg.message_id != message_id:
                                message_id, pending_note = msg.message_id, None
                            for block in msg.content:
                                if isinstance(block, ToolUseBlock):
                                    if top_level:
                                        await before_tool(msg.message_id or message_id)
                                    from .memory_workspace import is_read

                                    self.memory_reads += int(
                                        is_read(ctx.workdir, block.name, block.input)
                                    )
                                    result.run_log.append(
                                        {
                                            "tool": block.name,
                                            "input": truncate(block.input),
                                            "timestamp": datetime.now(UTC).isoformat(),
                                        }
                                    )
                                    task = tool_progress(block)
                                    tasks[block.id] = task
                                    await signal("working")
                                    await on_status(task)
                                elif isinstance(block, TextBlock) and top_level:
                                    result.text = block.text
                                    note = public_note(block.text)
                                    if message_id and note:
                                        pending_note = (message_id, note)
                        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                            for completed in msg.content:
                                if (
                                    isinstance(completed, ToolResultBlock)
                                    and completed.tool_use_id in tasks
                                ):
                                    task = dataclasses.replace(
                                        tasks[completed.tool_use_id],
                                        status="error" if completed.is_error else "complete",
                                    )
                                    tasks[completed.tool_use_id] = task
                                    if self.compute:
                                        usage = self.compute.sampler.commands.get(
                                            completed.tool_use_id
                                        )
                                        if usage:
                                            result.run_log.append(
                                                {
                                                    "tool": "resources",
                                                    "input": {},
                                                    "tool_use_id": completed.tool_use_id,
                                                    "resources": usage,
                                                }
                                            )
                                    await on_status(task)
                            if not any(t.status == "in_progress" for t in tasks.values()):
                                await signal("thinking")
                        elif isinstance(msg, SystemMessage) and self.compute:
                            session = msg.data.get("session_id")
                            if isinstance(session, str):
                                result.session_id = self.compute.session_id = session
                        elif isinstance(msg, ResultMessage):
                            message_id, pending_note = None, None
                            if self.history:
                                log.info(
                                    "worker_model_usage session=%s usage=%s",
                                    msg.session_id,
                                    json.dumps(
                                        {
                                            k: v
                                            for k, v in (msg.usage or {}).items()
                                            if k
                                            in {
                                                "input_tokens",
                                                "output_tokens",
                                                "cache_read_input_tokens",
                                                "cache_creation_input_tokens",
                                                "cache_creation",
                                            }
                                        }
                                    ),
                                )
                            got_result = True
                            result.session_id = msg.session_id
                            result.cost = (result.cost or 0) + (msg.total_cost_usd or 0)
                            if self.compute:
                                total = msg.total_cost_usd
                                self.compute.sdk_cost_total = total
                                cost = (
                                    total - baseline
                                    if total is not None
                                    and baseline is not None
                                    and total >= baseline
                                    else None
                                )
                                result.cost = self.compute.model_cost = cost
                                self.compute.session_id = msg.session_id
                            details = " ".join(msg.errors or [])
                            result.error = None if msg.subtype == "success" else msg.subtype
                            result.text = msg.result or result.text
                            if getattr(msg, "stop_reason", None) == "refusal":
                                log.warning("Model refusal in thread %s", ctx.thread_ts)
                    if not got_result:
                        result.error = "Agent stream ended without a result"
                except Exception as exc:
                    details += str(exc)
                    result.error = result.error or type(exc).__name__
                    log.exception("Agent turn failed in %s", ctx.thread_ts)
                missing = any(
                    s in details.lower()
                    for s in (
                        "no conversation found",
                        "session not found",
                        "could not load session",
                    )
                )
                if not (attempt == 0 and ctx.session_id and missing and not result.run_log):
                    break
                ctx = dataclasses.replace(ctx, session_id=None)
                result.session_id = None
                result.error, result.text = None, ""
                prompt = (
                    "(Earlier context is unavailable. Ask the user to restate anything needed.)\n"
                    + prompt
                )
            if result.error in {"error_max_turns", "error_max_budget_usd"} and result.session_id:
                remaining = self.settings.max_run_seconds - (time.monotonic() - start)
                if self.compute:
                    remaining = min(remaining, self.compute.deadline - time.time())
                available = (
                    max(0, self.settings.max_budget_usd_per_turn - result.cost)
                    if result.cost is not None
                    else 0
                )
                if remaining > 0 and available > 0:
                    await self._closing_summary(
                        result, options, min(60, remaining), min(summary_budget, available)
                    )
        finally:
            clear_previews(previews)
            if self.history:
                with suppress(OSError):
                    retain_tool_outputs(ctx.workdir, result.session_id)
        if (
            self.return_scripts
            and self.script_capture
            and not (self.compute and self.compute.pending)
        ):
            provided = {
                digest for members in delivery_packages.values() for digest in members.values()
            }
            try:
                snapshots = await asyncio.to_thread(self.script_capture.uploads, 0, provided)
                named_outputs.update(Path(upload["file"]) for upload in snapshots)
            except (ValueError, OSError):
                for path in list(self.script_capture.scripts):
                    self.script_capture.skip(path, "attachment copy could not be prepared")
        result.files = sorted(set(changed_files(ctx.workdir / "outputs", before)) | named_outputs)
        result.delivery_packages = delivery_packages
        if self.compute:
            # Tar restore can round mtimes. Carry only this request's pending outputs,
            # never compare restored file timestamps to the previous machine's baseline.
            from .outputs import files_under

            pending = set(self.compute.manifest.get("pending_outputs", []))
            result.files = sorted(
                set(result.files)
                | {
                    p
                    for p in files_under(ctx.workdir / "outputs")
                    if str(p.relative_to(ctx.workdir / "outputs")) in pending
                }
            )
            self.compute.manifest["pending_outputs"] = [
                str(p.relative_to(ctx.workdir / "outputs")) for p in result.files
            ]
        if manager:
            result.runtimes = list(manager.events)
            result.run_log.extend(
                {
                    "tool": TOOL_NAME,
                    "input": {"runtime": dataclasses.asdict(event)},
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                for event in result.runtimes
            )
        result.duration = time.monotonic() - start
        self.last_result = result
        return result

    async def _closing_summary(
        self, result: TurnResult, options: ClaudeAgentOptions, seconds: float, budget: float
    ) -> None:
        """Resume once for text only; preserve the stop status and existing text on failure."""

        async def deny_tools(
            _event: HookInput, _tool_id: str | None, _context: HookContext
        ) -> HookJSONOutput:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "This call only summarizes completed work.",
                }
            }

        closing = dataclasses.replace(
            options,
            resume=result.session_id,
            max_turns=1,
            max_budget_usd=budget,
            tools=[],
            allowed_tools=[],
            mcp_servers={},
            disallowed_tools=list(set(options.disallowed_tools + TOOLS)),
            hooks={"PreToolUse": [HookMatcher(hooks=[deny_tools])]},
        )
        prompt = (
            "Stop working. In a short reply, summarize what was completed, where results are "
            "saved, what was not finished, and the next step. Use only the session's existing "
            "evidence. Do not use tools or list individual GCS files."
        )
        previous_total = self.compute.sdk_cost_total if self.compute else result.cost
        text, succeeded = "", False
        try:
            async with asyncio.timeout(seconds):
                async for msg in query(prompt=prompt, options=closing):
                    if isinstance(msg, AssistantMessage):
                        text = "\n".join(b.text for b in msg.content if isinstance(b, TextBlock))
                    elif isinstance(msg, ResultMessage):
                        total = msg.total_cost_usd
                        if total is not None:
                            cost = (
                                total - previous_total
                                if previous_total is not None and total >= previous_total
                                else total
                            )
                            if result.cost is not None:
                                result.cost += cost
                            if self.compute:
                                self.compute.sdk_cost_total = total
                                self.compute.model_cost = result.cost
                        succeeded = msg.subtype == "success"
                        text = msg.result or text
            if succeeded and text.strip():
                result.text, result.summary_generated = text.strip(), True
        except Exception:
            log.warning(
                "Closing summary unavailable; retaining the stopped-run reply", exc_info=True
            )
