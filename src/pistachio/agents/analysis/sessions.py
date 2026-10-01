from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from .runner import Context


class Sessions:
    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                channel TEXT, thread TEXT, context TEXT, turn INTEGER DEFAULT 0,
                is_init INTEGER DEFAULT 0, updated REAL DEFAULT (unixepoch()),
                PRIMARY KEY(channel, thread));
            CREATE TABLE IF NOT EXISTS processed_events (id TEXT PRIMARY KEY);
        """)
        self.recent: OrderedDict[str, bool] = OrderedDict()

    def claim(self, event: str) -> bool:
        if event in self.recent:
            return False
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO processed_events VALUES (?)", (event,)
            )
        self.recent[event] = True
        if len(self.recent) > 4096:
            self.recent.popitem(last=False)
        return inserted.rowcount == 1

    def get(self, channel: str, thread: str) -> tuple[Context, int, bool] | None:
        row = self.db.execute(
            "SELECT context,turn,is_init FROM sessions WHERE channel=? AND thread=?",
            (channel, thread),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["workdir"], data["data_path"] = Path(data["workdir"]), Path(data["data_path"])
        return Context(**data), row[1], bool(row[2])

    def save(self, channel: str, ctx: Context, turn: int = 0, is_init: bool = False) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,unixepoch())",
                (channel, ctx.thread_ts, json.dumps(asdict(ctx), default=str), turn, is_init),
            )

    def close(self) -> None:
        self.db.close()
