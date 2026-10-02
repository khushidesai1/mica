from __future__ import annotations

from typing import TYPE_CHECKING

from .storage import Storage

if TYPE_CHECKING:
    from .runner import Context, TurnResult

import json
import re
from pathlib import Path


def files_under(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not any(a.is_symlink() for a in p.parents if a != root)
    )


def snapshot_outputs(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in files_under(root)}


def changed_files(root: Path, before: dict[str, tuple[int, int]]) -> list[Path]:
    return [Path(p) for p, stat in snapshot_outputs(root).items() if before.get(p) != stat]


def publish(
    storage: Storage,
    ctx: Context,
    result: TurnResult,
    turn: int,
    user: str,
    prompt: str,
    model: str,
    run_prefix: str = "",
) -> None:
    root = run_prefix or f"projects/{ctx.project_slug}/runs/{ctx.thread_ts}"
    prefix = f"{root}/turn-{turn}"
    for path in [*result.files, *files_under(ctx.workdir / "code")]:
        storage.upload(f"{prefix}/{path.relative_to(ctx.workdir)}", path)
    storage.put(
        f"{prefix}/run_log.jsonl", "\n".join(json.dumps(x) for x in result.run_log).encode()
    )
    # Best-effort paths visible in tool inputs; script-internal reads are not a syscall trace.
    pattern = re.escape(str(ctx.data_path)) + r"""(?:/[^\s"'`;|<>]*)?"""
    touched = sorted(
        {
            path
            for item in result.run_log
            if item["tool"] in {"Read", "Bash"}
            for value in item["input"].values()
            if isinstance(value, str)
            for path in re.findall(pattern, value)
        }
    )
    meta = dict(
        user_id=user,
        prompt=prompt,
        session_id=result.session_id,
        model=model,
        cost=result.cost,
        duration=result.duration,
        error=result.error,
        brief_generation=ctx.brief_generation,
        datasets_touched=touched,
    )
    storage.put(f"{prefix}/meta.json", json.dumps(meta, indent=2).encode())
