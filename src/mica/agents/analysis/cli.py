from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .progress import ActivityState, ProgressNote, ProgressUpdate, TaskProgress
from .runner import Context, Runner


async def main(args: argparse.Namespace) -> None:
    settings = Settings()
    settings.require_credentials()
    work = args.workdir.resolve()
    state = work / ".session.json"
    ctx = Context(workdir=work, data_path=args.data.resolve(), project_md=args.brief.read_text())
    if args.resume:
        saved = json.loads(state.read_text())
        if saved["data_path"] != str(ctx.data_path):
            raise ValueError("A resumed workspace must use the original data directory")
        ctx.session_id, ctx.project_md = saved["session_id"], saved["brief"]

    async def status(update: ProgressUpdate) -> None:
        text = (
            f"{update.title} ({update.status})"
            if isinstance(update, TaskProgress)
            else update.label
            if isinstance(update, ActivityState)
            else update.text
            if isinstance(update, ProgressNote)
            else update
        )
        print(text, flush=True)

    result = await Runner(settings, sandbox=not args.no_sandbox).run_turn(ctx, args.prompt, status)
    state.write_text(
        json.dumps(
            {
                "session_id": result.session_id,
                "brief": ctx.project_md,
                "data_path": str(ctx.data_path),
            }
        )
    )
    print(result.text)
    for path in result.files:
        print(path)
    if result.error:
        raise SystemExit(f"Stopped: {result.error}")


def run() -> None:
    parser = argparse.ArgumentParser(description="Local Mica agent (uses Claude API credits)")
    for name in ("data", "brief", "workdir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-sandbox", action="store_true", help="macOS local development only")
    parser.add_argument("prompt")
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    run()
