#!/usr/bin/env python3
"""Preview or apply a trusted project knowledge restore from a private GitHub commit."""

from __future__ import annotations

import argparse
import asyncio

from pistachio.agents.analysis import knowledge_mirror
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import GCSStorage, LocalStorage


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--commit", required=True, help="Full 40-character Git commit SHA")
    parser.add_argument("--actor", required=True, help="Admin identity for the audit history")
    parser.add_argument("--apply", action="store_true", help="Write the restore; otherwise preview")
    args = parser.parse_args()
    settings = Settings()
    storage = (
        LocalStorage(settings.local_storage_root)
        if settings.storage == "local"
        else GCSStorage(settings.bucket)
    )
    value = await asyncio.to_thread(knowledge_mirror.token, settings)
    async with knowledge_mirror.client_for(value) as client:
        result = await knowledge_mirror.restore_commit(
            storage, settings, args.project, args.commit, client, actor=args.actor, apply=args.apply
        )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
