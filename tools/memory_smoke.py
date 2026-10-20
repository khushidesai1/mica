"""Exercise real GitHub CAS writes; only touches the reserved projects/_smoke/ folder.

AGENT_MEMORY_REPO and AGENT_MEMORY_TOKEN_SECRET use the deployed settings. For local
setup, --github-cli uses the existing gh login in memory without printing the token.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
from uuid import uuid4

from mica.agents.analysis import memory_archive
from mica.agents.analysis.config import Settings
from mica.agents.analysis.project_memory import COMMIT, GitHubMemory, HeadMoved, client_for, token


async def smoke(use_cli: bool) -> None:
    settings = Settings()
    credential = (
        subprocess.run(
            ["gh", "auth", "token"], check=True, capture_output=True, text=True
        ).stdout.strip()
        if use_cli
        else await asyncio.to_thread(token, settings)
    )
    async with client_for(credential) as client:
        backend = GitHubMemory(settings, client=client)
        event = uuid4().hex
        base = await backend.read("_smoke", event_id=event)
        if base is None:
            raise RuntimeError("Could not read the real memory branch")
        if base.files or base.invalid:
            raise RuntimeError("Reserved smoke folder is not empty; inspect it before retrying")
        if base._root != "projects/_smoke":
            raise RuntimeError("Reserved smoke project is mapped elsewhere; refusing writes")
        try:
            text = (
                f"type: fact\nsource: synthetic smoke {event}\nstatus: current\n\nOriginal fact.\n"
            )
            entries = {f"data/note-{n:03d}.md": text + str(n) for n in range(200)}
            entries = {"data/fact.md": text} | entries
            first = await backend.commit("_smoke", base, entries, event_id=event, source="editor")
            readback = await backend.read("_smoke", event_id=event)
            assert readback is not None and readback.head_oid == first.commit_oid
            assert readback.files["data/fact.md"].text == text
            assert len([p for p in readback.files if p.startswith("archive/index-")]) > 1
            for path, original in entries.items():
                retained = (
                    readback.files.get(path)
                    or readback.files[memory_archive.archive_path(path, original)]
                )
                assert retained.text == original
            second = await backend.commit(
                "_smoke",
                base,
                {"data/fact.md": text + "Conflicting fact\n"},
                event_id=event + "-conflict",
                source="worker",
            )
            conflict = await backend.read("_smoke", event_id=event)
            assert conflict is not None and len(second.unmerged) == 1
            assert conflict.files[second.unmerged[0]].text == text + "Conflicting fact\n"
            assert second.unmerged[0] in conflict.files["INDEX.md"].text
            third = await backend.commit(
                "_smoke",
                conflict,
                {"data/fact.md": text + "New fact\n", second.unmerged[0]: None},
                event_id=event + "-rewrite",
                source="compaction",
            )
            archived = await backend.read("_smoke", event_id=event)
            assert archived is not None and not archived.invalid and not third.rejected
            for path in ("data/fact.md", second.unmerged[0]):
                original = conflict.files[path].text
                assert archived.files[memory_archive.archive_path(path, original)].text == original
            print(
                f"write={first.commit_oid} conflict={second.commit_oid} rewrite={third.commit_oid}"
            )
            print("readback=matched archive_pages=verified original_bytes=preserved")
        finally:
            # Production writers cannot erase archives. This explicit operator-only cleanup
            # removes exactly the isolated synthetic folder, fenced by its current Git head.
            for attempt in range(3):
                latest = await backend.read("_smoke", event_id=event)
                if latest is None or latest._root != "projects/_smoke" or latest.invalid:
                    raise RuntimeError("Smoke cleanup unsafe; inspect projects/_smoke/")
                if not latest.files:
                    break
                params = {
                    "branch": {
                        "repositoryNameWithOwner": settings.memory_repo,
                        "branchName": settings.memory_branch,
                    },
                    "expectedHeadOid": latest.head_oid,
                    "message": {"headline": f"memory(_smoke): cleanup {event}"},
                    "fileChanges": {
                        "deletions": [{"path": "projects/_smoke/" + p} for p in latest.files]
                    },
                }
                try:
                    result = await backend.graphql(client, COMMIT, {"input": params})
                    print("cleanup=" + result["createCommitOnBranch"]["commit"]["oid"])
                    break
                except HeadMoved:
                    if attempt == 2:
                        raise
            empty = await backend.read("_smoke", event_id=event)
            assert empty is not None and not empty.files and not empty.invalid
            print("folder=projects/_smoke/ cleanup=confirmed_empty")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-cli", action="store_true")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(smoke(parser.parse_args().github_cli))
