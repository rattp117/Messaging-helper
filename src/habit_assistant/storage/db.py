"""SQLite access layer. Stdlib sqlite3 only, WAL mode, schema per SPEC.md §5.

No channel imports here (SPEC.md §8) — this module only knows about logs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from habit_assistant.storage.migrations import run_migrations
from habit_assistant.storage.models import LogEntry

_UNSET = object()  # sentinel: "field not given" vs. "field explicitly set to None"


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
        # ROADMAP v0.3.0: schema now evolves through storage/migrations.py's
        # user_version-based runner instead of a single inline executescript.
        self.schema_version_before, self.schema_version = run_migrations(self._conn)

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
        """day: 'YYYY-MM-DD'. Sums value_num for water logs whose ts starts
        with that date. Excludes soft-deleted rows (ROADMAP.md v0.5.0
        AC5.4) -- an undone/edited-away entry must not count toward today's
        total."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(value_num), 0) AS total FROM logs "
            "WHERE category = 'water' AND deleted_at IS NULL AND ts LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return float(row["total"])

    def stretch_count(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE category = 'stretch' AND deleted_at IS NULL AND ts LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return int(row["n"])

    def diary_count(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM logs WHERE category = 'diary' AND deleted_at IS NULL AND ts LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return int(row["n"])

    def logs_between(self, start_ts: str, end_ts: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM logs WHERE ts >= ? AND ts <= ? AND deleted_at IS NULL ORDER BY ts",
            (start_ts, end_ts),
        ).fetchall()

    def pending_unparsed(self) -> list[sqlite3.Row]:
        """ROADMAP.md v0.4.0 AC3.3: rows deferred while the LLM was
        unavailable, still waiting to be re-parsed. A plain query against
        persisted state, not an in-memory queue -- so this also finds rows
        deferred by a *previous* process run (survives a restart). Excludes
        soft-deleted rows (ROADMAP.md v0.5.0 AC5.4): an undone deferred
        message should not be recovered/re-parsed."""
        return self._conn.execute(
            "SELECT * FROM logs WHERE category = 'unparsed' AND deleted_at IS NULL ORDER BY ts"
        ).fetchall()

    def reclassify_log(
        self, log_id: int, category: str, value_num: float | None, value_text: str | None
    ) -> None:
        """Convert a deferred 'unparsed' row to its real category once it
        has been successfully re-parsed (ROADMAP.md v0.4.0 AC3.3).
        `ts`/`raw_message`/`source` are left untouched -- only the
        parsed-out fields change."""
        self._conn.execute(
            "UPDATE logs SET category = ?, value_num = ?, value_text = ? WHERE id = ?",
            (category, value_num, value_text, log_id),
        )
        self._conn.commit()

    def last_log(self, category: str | None = None) -> sqlite3.Row | None:
        """Most recent non-deleted log (ROADMAP.md v0.5.0 AC5.1/AC5.3),
        optionally restricted to one category. Ties on `ts` (e.g. seeded/
        backdated data) break on `id DESC` so "most recent" is always
        well-defined as "most recently inserted"."""
        if category is None:
            return self._conn.execute(
                "SELECT * FROM logs WHERE deleted_at IS NULL ORDER BY ts DESC, id DESC LIMIT 1"
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM logs WHERE deleted_at IS NULL AND category = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (category,),
        ).fetchone()

    def soft_delete(self, log_id: int) -> None:
        """Undo (ROADMAP.md v0.5.0 AC5.1): mark a row deleted without
        removing it, so it stays out of every aggregation (AC5.4) but
        remains in the table for audit/recovery. Reversible in principle
        (a future version could add "redo" by clearing this column) --
        this version doesn't need that, but soft-delete keeps the door
        open, per Archi's confirmed default."""
        self._conn.execute(
            "UPDATE logs SET deleted_at = datetime('now','localtime') WHERE id = ?", (log_id,)
        )
        self._conn.commit()

    def update_value(self, log_id: int, value_num=_UNSET, value_text=_UNSET) -> None:
        """Edit (ROADMAP.md v0.5.0 AC5.3): update only the field(s) given.
        `_UNSET` (not `None`) is the "leave alone" default so a numeric-only
        edit ("make that 300ml") never clobbers an existing `value_text`
        (and vice versa) -- `None` is itself a valid value to set."""
        fields: list[str] = []
        params: list = []
        if value_num is not _UNSET:
            fields.append("value_num = ?")
            params.append(value_num)
        if value_text is not _UNSET:
            fields.append("value_text = ?")
            params.append(value_text)
        if not fields:
            return
        params.append(log_id)
        self._conn.execute(f"UPDATE logs SET {', '.join(fields)} WHERE id = ?", params)
        self._conn.commit()
