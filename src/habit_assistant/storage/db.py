"""SQLite access layer. Stdlib sqlite3 only, WAL mode, schema per SPEC.md §5.

No channel imports here (SPEC.md §8) — this module only knows about logs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from habit_assistant.storage.models import LogEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  category    TEXT NOT NULL,
  value_num   REAL,
  value_text  TEXT,
  raw_message TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'reply',
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_logs_ts_cat ON logs(ts, category);
"""


class Database:
    """Thin wrapper around one sqlite3 connection. Not thread-safe by
    design — the app is a single asyncio process; all DB calls happen on
    the event-loop thread (sqlite3 calls are fast/local so this is fine
    without wrapping in a threadpool)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_log(self, entry: LogEntry) -> int:
        cur = self._conn.execute(
            "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry.ts, entry.category, entry.value_num, entry.value_text, entry.raw_message, entry.source),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def water_total_ml(self, day: str) -> float:
        """day: 'YYYY-MM-DD'. Sums value_num for water logs whose ts starts with that date."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(value_num), 0) AS total FROM logs "
            "WHERE category = 'water' AND ts LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return float(row["total"])

    def stretch_count(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE category = 'stretch' AND ts LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return int(row["n"])

    def diary_count(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE category = 'diary' AND ts LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return int(row["n"])

    def logs_between(self, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM logs WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start_ts, end_ts),
        ).fetchall()
