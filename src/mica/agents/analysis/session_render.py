"""Streaming Claude Code JSONL adapter. Parses data; never executes imported code."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import shlex
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

FORMAT = "claude-code-jsonl"
MAX_LINE = 8_000_000
BLOCK_CHARS = 4000
START = "t000000000_000000"


def records(stream: TextIO, stats: dict[str, int]) -> Iterator[tuple[int, dict[str, Any]]]:
    number = 0
    while raw := stream.readline(MAX_LINE + 1):
        number += 1
        if len(raw) > MAX_LINE:
            raise ValueError("A session record exceeds the 8 MB parsing limit")
        try:
            row = json.loads(raw)
        except (ValueError, RecursionError):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        if not isinstance(row, dict):
            stats["malformed"] = stats.get("malformed", 0) + 1
            continue
        message = row.get("message")
        if (
            row.get("type") not in {"user", "assistant"}
            or not isinstance(message, dict)
            or not isinstance(message.get("content"), (list, str))
        ):
            stats["bookkeeping"] = stats.get("bookkeeping", 0) + 1
            continue
        stats["records"] = stats.get("records", 0) + 1
        if isinstance(row.get("sessionId"), str) and row["sessionId"]:
            stats["session_records"] = stats.get("session_records", 0) + 1
        yield number, row


def result_text(text: str) -> str:
    if len(text) <= 12000:
        return text
    lines = text.splitlines()
    keep = {0, len(lines) - 1}
    header = 0
    for index, line in enumerate(lines):
        if "|" in line and index + 1 < len(lines) and re.search(r"\|\s*:?-", lines[index + 1]):
            header = index
        if re.search(r"[0-9/\\]|\b(?:error|exception|failed|warning)\b", line, re.I):
            keep.update(range(max(0, index - 3), min(len(lines), index + 4)))
            if "|" in line:
                keep.update({header, min(header + 1, len(lines) - 1)})
    output, previous = [], -1
    for index in sorted(keep):
        if index > previous + 1:
            output.append("[… nonnumeric tool output omitted …]")
        output.append(lines[index])
        previous = index
    return "\n".join(output)


def canonical(path: str, cwd: str) -> str:
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(cwd, path))


class Versions:
    """Disk-backed indexes bound RAM independently of transcript length."""

    def __init__(self, database: str) -> None:
        self.db = sqlite3.connect(database)
        self.db.execute("CREATE TABLE scripts (path TEXT PRIMARY KEY, body TEXT, anchor TEXT)")
        self.db.execute("CREATE TABLE hashes (digest TEXT PRIMARY KEY, anchor TEXT)")
        self.db.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, anchor TEXT)")

    def save(self, path: str, body: str, anchor: str) -> str:
        digest = hashlib.sha256(body.encode()).hexdigest()
        prior = self.db.execute("SELECT anchor FROM hashes WHERE digest=?", (digest,)).fetchone()
        version = prior[0] if prior else anchor
        self.db.execute("INSERT OR IGNORE INTO hashes VALUES (?,?)", (digest, anchor))
        self.db.execute("INSERT OR REPLACE INTO scripts VALUES (?,?,?)", (path, body, version))
        return (
            f"Script {path}, sha256 {digest}, same contents as #{version}.\n"
            if prior
            else f"Script {path}, sha256 {digest}, version #{anchor}:\n```\n{body}\n```\n"
        )

    def tool(self, block: dict[str, Any], anchor: str, cwd: str) -> str:
        name, args = str(block.get("name", "")), block.get("input", {})
        if not isinstance(args, dict):
            args = {}
        self.db.execute(
            "INSERT OR REPLACE INTO calls VALUES (?,?)", (str(block.get("id", "")), anchor)
        )
        output = f"Tool {name} ({block.get('id', '')})\n"
        path = str(args.get("file_path", args.get("path", "")))
        if name == "Write" and isinstance(args.get("content"), str):
            return output + self.save(canonical(path, cwd), args["content"], anchor)
        if name == "Edit":
            path = canonical(path, cwd)
            before = self.db.execute("SELECT body FROM scripts WHERE path=?", (path,)).fetchone()
            old, new = args.get("old_string"), args.get("new_string")
            if (
                before
                and isinstance(old, str)
                and old
                and isinstance(new, str)
                and old in before[0]
            ):
                body = before[0].replace(old, new, -1 if args.get("replace_all") else 1)
                return output + self.save(path, body, anchor)
            return output + "Edit diff (earlier contents unavailable):\n" + json.dumps(args) + "\n"
        if name == "Bash":
            command = str(args.get("command", ""))
            output += f"```sh\n{command}\n```\n"
            # Deliberately recognize only literal cat heredocs. Other shell constructs
            # remain verbatim evidence and are never evaluated to reconstruct a file.
            pattern = r"cat\s*>\s*([^\s;]+)\s*<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\2(?:\n|$)"
            for match in re.finditer(pattern, command, re.S):
                output += self.save(canonical(match[1].strip("\"'"), cwd), match[3] + "\n", anchor)
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = []
            for token in dict.fromkeys(tokens):
                if token.endswith((".py", ".R", ".r", ".sh", ".jl")):
                    script = canonical(token, cwd)
                    version = self.db.execute(
                        "SELECT anchor FROM scripts WHERE path=?", (script,)
                    ).fetchone()
                    output += f"Referenced script {script}: " + (
                        f"version #{version[0]}\n" if version else "version not captured\n"
                    )
            return output
        return output + json.dumps(args, ensure_ascii=False) + "\n"

    def blocks(self, row: dict[str, Any], line: int) -> Iterator[str]:
        content = row["message"]["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        segment = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            anchor = f"t{line:09d}_{segment:06d}"
            kind = block.get("type")
            prefix = f"{row['type']} {row.get('timestamp', '')}\n"
            if kind == "text":
                body = prefix + str(block.get("text", "")) + "\n"
            elif kind == "tool_use":
                body = prefix + self.tool(block, anchor, str(row.get("cwd", "")))
            elif kind == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = "\n".join(
                        str(b.get("text", "")) for b in result if isinstance(b, dict)
                    )
                call = self.db.execute(
                    "SELECT anchor FROM calls WHERE id=?", (str(block.get("tool_use_id", "")),)
                ).fetchone()
                body = (
                    prefix
                    + f"Result for #{call[0] if call else 'unknown'}\n"
                    + result_text(str(result))
                    + "\n"
                )
            else:
                continue
            for start in range(0, len(body), BLOCK_CHARS):
                yield f"\n## t{line:09d}_{segment:06d}\n" + body[start : start + BLOCK_CHARS]
                segment += 1
        self.db.commit()


def render(path: Path, stats: dict[str, int]) -> Iterator[tuple[str, str]]:
    with tempfile.TemporaryDirectory(prefix="mica-render-") as folder:
        versions = Versions(str(Path(folder) / "versions.sqlite"))
        try:
            with path.open(encoding="utf-8") as stream:
                for line, row in records(stream, stats):
                    for block in versions.blocks(row, line):
                        anchor = block.split("\n", 2)[1].removeprefix("## ")
                        stats["characters"] = stats.get("characters", 0) + len(block)
                        yield anchor, block
        finally:
            versions.db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--from", dest="start", default=START)
    parser.add_argument("--max-chars", type=int, default=200000)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"t[0-9]{9}_[0-9]{6}", args.start) or args.max_chars < 5000:
        parser.error("Use a valid anchor and at least 5000 characters")
    stats: dict[str, int] = {}
    count, next_anchor = 0, None
    for anchor, block in render(args.session, stats):
        if args.stats or anchor < args.start:
            continue
        if count + len(block) > args.max_chars:
            next_anchor = anchor
            break
        sys.stdout.write(block)
        count += len(block)
    print(
        json.dumps({**stats, "next_anchor": next_anchor, "slice_characters": count}),
        file=sys.stdout if args.stats else sys.stderr,
    )


if __name__ == "__main__":
    main()
