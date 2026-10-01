from __future__ import annotations

import dataclasses
import logging
import platform
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
    ToolUseBlock,
    query,
)
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput, SandboxSettings

from . import prompts
from .config import Settings
from .outputs import changed_files, snapshot_outputs

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


def build_options(ctx: Context, settings: Settings, *, sandbox: bool = True) -> ClaudeAgentOptions:
    if not sandbox and platform.system() != "Darwin":
        raise ValueError("--no-sandbox is available only for local macOS development")

    tools = ["Read", "Write", "Edit"] if ctx.survey_only else TOOLS

    async def guard(
        event: HookInput, _tool_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if event["hook_event_name"] != "PreToolUse":
            return {}
        name, args = event["tool_name"], event["tool_input"]
        reason = ""
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
            elif name == "Read" and path.suffix.lower() in VISUAL_EXTENSIONS:
                reason = (
                    "Image previews are disabled. Check the script's exit status and output "
                    "file sizes using Bash. Leave figures in outputs/ for Slack delivery; "
                    "do not read or encode image contents into the conversation."
                )
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
            "append": prompts.build_system_append(ctx, settings),
        },
        setting_sources=[],
        tools=tools,
        allowed_tools=tools,
        disallowed_tools=["WebFetch", "WebSearch", "Agent", "AskUserQuestion", "NotebookEdit"]
        + (["Bash", "Glob", "Grep"] if ctx.survey_only else []),
        permission_mode="dontAsk",
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
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(ctx.workdir / ".mpl"),
            "TMPDIR": str(ctx.workdir / ".tmp"),
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
                    "denyWrite": [str(ctx.data_path)],
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
    if block.name == "Bash":
        return str(args.get("description") or args.get("command", "Running code"))[:120]
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


class Runner:
    def __init__(self, settings: Settings, *, sandbox: bool = True) -> None:
        self.settings, self.sandbox = settings, sandbox

    async def run_turn(
        self, ctx: Context, prompt: str, on_status: Callable[[str], Awaitable[None]]
    ) -> TurnResult:
        for name in ("code", "outputs", ".tmp", ".mpl"):
            (ctx.workdir / name).mkdir(parents=True, exist_ok=True)
        before, start = snapshot_outputs(ctx.workdir / "outputs"), time.monotonic()
        result = TurnResult(session_id=ctx.session_id)
        for attempt in range(2):
            details, got_result = "", False
            try:
                async for msg in query(
                    prompt=prompt, options=build_options(ctx, self.settings, sandbox=self.sandbox)
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
                                await on_status(describe_tool(block))
                            elif isinstance(block, TextBlock):
                                result.text = block.text
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
        result.files = changed_files(ctx.workdir / "outputs", before)
        result.duration = time.monotonic() - start
        return result
