"""Remove inactive workspaces while preserving thread/brief mappings and run numbering."""

import json
import shutil
import sqlite3
from pathlib import Path

from mica.agents.analysis.config import Settings


def cleanup(settings):
    if not settings.state_db.exists():
        return 0
    count = 0
    with sqlite3.connect(settings.state_db) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT channel,thread,context FROM sessions WHERE updated < unixepoch()-2592000"
        )
        for channel, thread, context in rows.fetchall():
            data = json.loads(context)
            path = Path(data["workdir"])
            root = settings.work_root.resolve()
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(root)
                or len(path.resolve().relative_to(root).parts) != 2
            ):
                raise ValueError("Refusing cleanup outside a thread workspace")
            if path.exists():
                shutil.rmtree(path)
                count += 1
            data["session_id"] = None
            db.execute(
                "UPDATE sessions SET context=? WHERE channel=? AND thread=?",
                (json.dumps(data), channel, thread),
            )
    return count


if __name__ == "__main__":
    print(f"Removed {cleanup(Settings())} inactive workspaces")
