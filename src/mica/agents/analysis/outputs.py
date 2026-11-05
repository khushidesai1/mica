from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .access import run_root
from .storage import Storage

if TYPE_CHECKING:
    from .runner import Context, TurnResult

import json
import re
from pathlib import Path

DATA_SUFFIXES = {
    ".zarr",
    ".parquet",
    ".h5ad",
    ".h5",
    ".hdf5",
    ".tif",
    ".tiff",
    ".npy",
    ".npz",
    ".zip",
    ".gz",
    ".tar",
    ".bz2",
    ".xz",
    ".bin",
    ".loom",
}
TEXT_SUFFIXES = {
    ".py",
    ".r",
    ".sh",
    ".jl",
    ".sql",
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".log",
    ".csv",
    ".tsv",
}
REPORT_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".csv", ".tsv", ".md", ".txt"}
ATTACHMENT_SUFFIXES = REPORT_SUFFIXES | TEXT_SUFFIXES | {".zip"}
MAX_ATTACHMENT_BYTES = 200_000_000
ARTIFACT_BUDGET = 1024**3


def retain_tool_outputs(work: Path, session_id: str | None) -> None:
    """Copy the pinned CLI's full Bash result files into bounded, durable logs."""
    if not session_id or not re.fullmatch(r"[a-f0-9-]{36}", session_id):
        return
    logs = work / "logs/bash"
    if not logs.resolve().is_relative_to(work.resolve()) or any(
        p.is_symlink() for p in (logs, logs.parent)
    ):
        return
    for root in (Path.home() / ".claude/projects").glob(f"*/{session_id}/tool-results"):
        for source in files_under(root):
            if source.suffix != ".txt":
                continue
            logs.mkdir(parents=True, exist_ok=True)
            target = logs / f"{session_id}-{source.name}"
            if target.is_symlink():
                continue
            with source.open("rb") as stream:
                data = stream.read(8 * 1024**2)
            target.write_bytes(data)


def retention_reason(relative: Path, size: int, *, recovery: bool = False) -> str:
    """One policy for workspace archives and provenance; paths are workspace-relative."""
    if (
        relative.parts
        and relative.parts[0] == "outputs"
        and relative.parts[:2] != ("outputs", "gcs")
        and relative.suffix.lower() == ".zip"
        and not any(Path(p).suffix.lower() in DATA_SUFFIXES for p in relative.parts[:-1])
    ):
        return "" if size <= MAX_ATTACHMENT_BYTES else "large-or-unsupported-output"
    if any(Path(p).suffix.lower() in DATA_SUFFIXES for p in relative.parts):
        return "dataset"
    if relative.parts[:2] == ("outputs", "gcs"):
        return "bucket-staging"
    if recovery and relative.parts[:2] in {(".mica", "scripts"), (".pistachio", "scripts")}:
        return "" if size <= 1_000_000 else "oversized-script"
    if not relative.parts or relative.parts[0] not in {"code", "notes", "logs", "outputs"}:
        return "temporary-or-unrecognized"
    if relative.parts[0] == "outputs":
        return (
            ""
            if relative.suffix.lower() in ATTACHMENT_SUFFIXES and size <= MAX_ATTACHMENT_BYTES
            else "large-or-unsupported-output"
        )
    if relative.suffix.lower() not in TEXT_SUFFIXES:
        return "non-text"
    return (
        ""
        if size <= (8 * 1024**2 if relative.parts[0] == "logs" else 2 * 1024**2)
        else "large-text"
    )


def select_artifacts(
    paths: Sequence[Path], work: Path, *, budget: int = ARTIFACT_BUDGET, recovery: bool = False
) -> tuple[list[Path], dict[str, Any]]:
    kept: list[Path] = []
    sizes: dict[Path, int] = {}
    groups: dict[str, dict[str, Any]] = {}

    def exclude(path: Path, reason: str) -> None:
        relative = path.relative_to(work)
        # Aggregate array chunks and staged folders; never retain one row per chunk.
        folder = "/".join(
            relative.parts[:3] if relative.parts[:2] == ("outputs", "gcs") else relative.parts[:1]
        )
        key = folder + ":" + reason
        item = groups.setdefault(
            key, {"path": folder, "reason": reason, "bytes": 0, "files": 0, "examples": []}
        )
        item["bytes"] += sizes[path]
        item["files"] += 1
        if len(item["examples"]) < 3:
            item["examples"].append(str(relative))

    for path in paths:
        sizes[path] = path.stat().st_size
        reason = retention_reason(path.relative_to(work), sizes[path], recovery=recovery)
        if reason:
            exclude(path, reason)
        else:
            kept.append(path)
    total = sum(sizes[p] for p in kept)
    # Code/notes and trusted code snapshot copies take priority over optional reports/logs.
    for path in sorted(kept, key=lambda p: sizes[p], reverse=True):
        if total <= budget:
            break
        if path.relative_to(work).parts[0] in {"outputs", "logs"}:
            kept.remove(path)
            total -= sizes[path]
            exclude(path, "workspace-budget")
    return kept, {
        "kept_bytes": total,
        "excluded_bytes": sum(g["bytes"] for g in groups.values()),
        "excluded_paths": sum(g["files"] for g in groups.values()),
        "groups": list(groups.values()),
    }


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
    *,
    attachments: Sequence[dict[str, Any]] = (),
    keep_scripts: bool = True,
) -> dict[Path, str]:
    root = run_prefix or run_root(False, "", ctx.project_slug, "", ctx.thread_ts)
    prefix = f"{root}/turn-{turn}"
    published = {}
    retained, excluded = select_artifacts(
        sorted(set(result.files) | set(files_under(ctx.workdir / "code"))), ctx.workdir
    )
    for path in retained:
        if not keep_scripts and path.relative_to(ctx.workdir).parts[0] == "code":
            continue
        key = f"{prefix}/{path.relative_to(ctx.workdir)}"
        storage.upload(key, path)
        published[path] = key
    storage.put(f"{prefix}/excluded.json", json.dumps(excluded).encode())
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
        attachments=[{k: f[k] for k in ("id", "name", "sha256", "key")} for f in attachments],
    )
    if result.error in {"error_max_turns", "error_max_budget_usd"}:
        meta["closing_summary"] = result.summary_generated
    if result.runtimes:
        meta["runtimes"] = [asdict(event) for event in result.runtimes]
    storage.put(f"{prefix}/meta.json", json.dumps(meta, indent=2).encode())
    return published
