"""Trusted Git fetch, followed by a build backend inside an offline sandbox."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .toolkit_catalog import Source

GITHUB = re.compile(
    r"git\+https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]*)/"
    r"([A-Za-z0-9][A-Za-z0-9_.-]*)@([A-Za-z0-9][A-Za-z0-9_./-]*)"
)


def parse_source(value: str) -> Source:
    match = GITHUB.fullmatch(value)
    if not match or any(s in match[3] for s in ("..", "//")) or match[3].endswith(("/", ".")):
        raise ValueError("Use git+https://github.com/owner/repo@ref with a public ref or commit.")
    return Source(f"https://github.com/{match[1]}/{match[2].removesuffix('.git')}", match[3])


def git_command(*args: str) -> list[str]:
    return [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "http.followRedirects=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "submodule.recurse=false",
        *args,
    ]


def pin_source(source: Source, run: Callable[[list[str]], str]) -> Source:
    if re.fullmatch(r"[0-9a-f]{40}", source.commit):
        return source
    ref = source.commit
    output = run(
        git_command(
            "ls-remote",
            "--exit-code",
            source.url,
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
            f"refs/tags/{ref}^{{}}",
        )
    )
    matches = dict(line.split("\t", 1)[::-1] for line in output.splitlines() if "\t" in line)
    commit = matches.get(f"refs/tags/{ref}^{{}}") or matches.get(f"refs/tags/{ref}")
    branch = matches.get(f"refs/heads/{ref}")
    if commit and branch and commit != branch:
        raise ValueError("Git ref is ambiguous; use a full commit SHA.")
    commit = commit or branch or ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("GitHub ref did not resolve to a commit.")
    return Source(source.url, commit)


def checkout(source: Source, path: Path, run: Callable[[list[str]], str]) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source.commit):
        raise ValueError("Source builds require an immutable commit")
    run(git_command("init", str(path)))
    run(git_command("-C", str(path), "remote", "add", "origin", source.url))
    run(git_command("-C", str(path), "fetch", "--depth", "1", "origin", source.commit))
    run(git_command("-C", str(path), "checkout", "--detach", "FETCH_HEAD"))
    commit = run(git_command("-C", str(path), "rev-parse", "HEAD")).strip()
    if commit != source.commit:
        raise ValueError("Fetched source does not match the pinned commit")
    # Never let trusted metadata readers follow package-controlled links into host data.
    if any(p.is_symlink() for p in path.rglob("*")):
        raise ValueError("Source trees containing symlinks require an admin build")
    if (path / ".gitmodules").exists():
        raise ValueError("Source trees requiring submodules need an admin build")
