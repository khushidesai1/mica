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

from mica.agents.analysis.config import Settings
from mica.agents.analysis.project_memory import GitHubMemory, client_for, token


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
        try:
            text = f"# Memory smoke {event}\n"
            first = await backend.commit(
                "_smoke", base, {"INDEX.md": text}, event_id=event, source="editor"
            )
            readback = await backend.read("_smoke", event_id=event)
            assert readback is not None and readback.head_oid == first.commit_oid
            assert readback.files["INDEX.md"].text == text
            assert readback.files["INDEX.md"].oid
            second = await backend.commit(
                "_smoke",
                base,
                {"INDEX.md": text + "Conflicting edit\n"},
                event_id=event + "-conflict",
                source="worker",
            )
            conflict = await backend.read("_smoke", event_id=event)
            assert conflict is not None and len(second.unmerged) == 1
            assert conflict.files[second.unmerged[0]].text == text + "Conflicting edit\n"
            assert second.unmerged[0] in conflict.files["INDEX.md"].text
            print(f"write={first.commit_oid} conflict={second.commit_oid} readback=matched")
            print(f"unmerged={second.unmerged[0]}")
        finally:
            latest = await backend.read("_smoke", event_id=event)
            if latest is None:
                raise RuntimeError("Smoke cleanup unavailable; inspect projects/_smoke/")
            saved = await backend.commit(
                "_smoke",
                latest,
                {p: None for p in latest.files},
                event_id=event + "-cleanup",
                source="compaction",
            )
            empty = await backend.read("_smoke", event_id=event)
            assert empty is not None and not empty.files
            print(f"cleanup={saved.commit_oid} folder=empty")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-cli", action="store_true")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(smoke(parser.parse_args().github_cli))
