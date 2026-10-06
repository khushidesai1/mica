from __future__ import annotations

import dataclasses
import logging
import platform
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput, SandboxSettings

from . import prompts
from .attachments import classify
from .config import Settings
from .limits import MAX_VISUAL_READS, PREVIEW_MAX_BYTES, SDK_MAX_BUFFER_BYTES
from .outputs import changed_files, snapshot_outputs
from .progress import ProgressUpdate, TaskProgress

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
    project_md: str = "No brief yet. Rely on listing and inspecting the data folder."
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


def build_options(
    ctx: Context,
    settings: Settings,
    *,
    sandbox: bool = True,
    user_memory: str = "",
    conversation_summary: str = "",
    project_name: str = "",
    write_access: str = "No GCS write grant for this run.",
    runtime_tmp: Path = Path("/tmp"),
) -> ClaudeAgentOptions:
    if not sandbox and platform.system() != "Darwin":
        raise ValueError("--no-sandbox is available only for local macOS development")

    tools = ["Read", "Write", "Edit"] if ctx.survey_only else TOOLS

    visual_reads = 0

    async def guard(
        event: HookInput, _tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        nonlocal visual_reads
        if event["hook_event_name"] != "PreToolUse":
            return {}
        name, args = event["tool_name"], event["tool_input"]
        reason = ""
        visual_read = False
        if name not in tools or args.get("dangerouslyDisableSandbox"):
            reason = "This tool or sandbox bypass is unavailable."
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
                ]
            )
            if not any(path.is_relative_to(p.resolve()) for p in roots):
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
        return {}

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
                ctx, settings, user_memory, project_name, conversation_summary
            ) + "\nCurrent GCS write access (application-authorized):\n" + write_access,
        },
        setting_sources=[],
        tools=tools,
        allowed_tools=tools,
        disallowed_tools=["WebFetch", "WebSearch", "Agent", "AskUserQuestion", "NotebookEdit"]
        + (["Bash", "Glob", "Grep"] if ctx.survey_only else []),
        permission_mode="dontAsk",
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        max_turns=settings.max_turns,
        max_budget_usd=settings.max_budget_usd_per_turn,
        env={
            "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
            "SLACK_BOT_TOKEN": "",
            "SLACK_APP_TOKEN": "",
            "SLACK_SIGNING_SECRET": "",
            "PISTACHIO_SLACK_BOT_TOKEN": "",
            "PISTACHIO_SLACK_SIGNING_SECRET": "",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            # A persisted `cd` into the read-only data mount makes the next sandbox
            # fail while creating .claude metadata, before any recovery command runs.
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(ctx.workdir / ".mpl"),
            "TMPDIR": str(runtime_tmp),
        },
        hooks={"PreToolUse": [HookMatcher(hooks=[guard])]},
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
                    "denyWrite": [str(ctx.data_path), str(ctx.workdir / "inputs")],
                    "denyRead": [
                        "/etc/pistachio",
                        str(Path.home() / ".claude"),
                        str(settings.state_db),
                        str(settings.work_root),
                        str(settings.data_mount_root),
                    ],
                    "allowRead": [str(ctx.workdir), str(ctx.data_path)],
                },
            },
        ),
    )


def describe_tool(block: ToolUseBlock) -> str:
    args = block.input
    if block.name == "Read" and ".previews" in Path(args.get("file_path", "")).parts:
        return "Looking at a figure preview"
    if block.name == "Bash":
        return str(args.get("description") or "Running code")[:80]
    if block.name in {"Read", "Write", "Edit"}:
        verb = {"Read": "Reading", "Write": "Writing", "Edit": "Editing"}[block.name]
        return f"{verb} {Path(args.get('file_path', '')).name}"
    return "Searching files"


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
        user_memory: str = "",
        conversation_summary: str = "",
        project_name: str = "",
        write_access: str = "No GCS write grant for this run.",
    ) -> None:
        self.settings, self.sandbox = settings, sandbox
        self.user_memory, self.project_name = user_memory, project_name
        self.conversation_summary = conversation_summary
        self.write_access = write_access

    async def run_turn(
        self, ctx: Context, prompt: str, on_status: Callable[[ProgressUpdate], Awaitable[None]]
    ) -> TurnResult:
        # The SDK puts Unix bridge sockets beneath TMPDIR. Private workspace paths can
        # already exceed the socket path limit before a socket filename is appended.
        # Each invocation gets short, private scratch storage, cleaned on every exit.
        with tempfile.TemporaryDirectory(prefix="pista-", dir="/tmp") as scratch:
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
        before, start = snapshot_outputs(ctx.workdir / "outputs"), time.monotonic()
        result = TurnResult(session_id=ctx.session_id)
        tasks: dict[str, TaskProgress] = {}
        previews = ctx.workdir / ".previews"
        clear_previews(previews)
        previews.mkdir()
        try:
            for attempt in range(2):
                details, got_result = "", False
                try:
                    async for msg in query(
                        prompt=prompt,
                        options=build_options(
                            ctx,
                            self.settings,
                            sandbox=self.sandbox,
                            user_memory=self.user_memory,
                            conversation_summary=self.conversation_summary,
                            project_name=self.project_name,
                            write_access=self.write_access,
                            runtime_tmp=runtime_tmp,
                        ),
                    ):
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, ToolUseBlock):
                                    result.run_log.append(
                                        {
                                            "tool": block.name,
                                            "input": truncate(block.input),
                                            "timestamp": datetime.now(UTC).isoformat(),
                                        }
                                    )
                                    task = tool_progress(block)
                                    tasks[block.id] = task
                                    await on_status(task)
                                elif isinstance(block, TextBlock):
                                    result.text = block.text
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
                                    await on_status(task)
                        elif isinstance(msg, ResultMessage):
                            got_result = True
                            result.session_id = msg.session_id
                            result.cost = (result.cost or 0) + (msg.total_cost_usd or 0)
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
        finally:
            clear_previews(previews)
        result.files = changed_files(ctx.workdir / "outputs", before)
        result.duration = time.monotonic() - start
        return result
