"""SQLite schema migration runner (ROADMAP.md v0.3.0 "Migrations &
Backup/Restore").

`PRAGMA user_version` drives an ordered list of migration functions. The
runner applies only the migrations above the DB's current user_version,
each inside its own transaction, and stamps the version forward as it
goes. Re-running on an already-migrated DB applies nothing (AC4.1). No
third-party migration framework -- Alembic is SQLAlchemy-bound, which this
project deliberately doesn't use; a small user_version runner is enough.

Migration 001 adopts the baseline v0.1.0/v0.2.0 schema via CREATE
TABLE/INDEX IF NOT EXISTS. That is deliberate: run against an existing
production DB that already has the `logs` table, it changes nothing --
it only stamps user_version=1 (AC4.2, "no-op-safe baseline adoption").
Every future migration must preserve that property: additive schema
changes only, never DROP/ALTER-destructive against existing columns/rows.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger("habit_assistant")

Migration = Callable[[sqlite3.Connection], None]


def _migration_001_baseline(conn: sqlite3.Connection) -> None:
    """Baseline schema: the `logs` table + its (ts, category) index, exactly
    as shipped in v0.1.0/v0.2.0. IF NOT EXISTS makes this safe to apply
    against a DB that already has them."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts_cat ON logs(ts, category)")


def _migration_002_category_index(conn: sqlite3.Connection) -> None:
    """ROADMAP.md v0.4.0 "Runtime Resilience & Self-Monitoring": deferred
    messages (received while the LLM was unavailable) are persisted as
    ordinary `logs` rows with category='unparsed'. That's a data-level
    change only -- `category` has never had a CHECK constraint restricting
    its values, so no column change is needed to store it. This migration
    is purely additive: an index so `Database.pending_unparsed()` (the
    startup/recovery re-parse scan) doesn't table-scan as `logs` grows."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)")


def _migration_003_soft_delete(conn: sqlite3.Connection) -> None:
    """ROADMAP.md v0.5.0 "Command Layer & Edit/Undo": undo is soft-delete
    (Sophia's default, confirmed by Archi) so a mistaken "undo"/"ยกเลิก" is
    reversible and every log stays auditable (AC5.1, AC5.4). Additive-only:
    a new nullable column, default NULL for every existing row (nothing is
    retroactively marked deleted), plus an index so the `deleted_at IS
    NULL` filter added to every aggregation query in storage/db.py doesn't
    table-scan as `logs` grows."""
    conn.execute("ALTER TABLE logs ADD COLUMN deleted_at TEXT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_deleted_at ON logs(deleted_at)")


def _migration_004_habit_type(conn: sqlite3.Connection) -> None:
    """ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (SPEC-v0.7.md §4 R11):
    additive-only, like every migration before it -- a new nullable
    column, then a one-time backfill of the three categories that existed
    before this version (`water`->'numeric', `stretch`->'duration',
    `diary`->'text'); any other category (notably 'unparsed', and any
    unrecognized historical category) is left NULL rather than guessed at.
    `value_num`/`value_text`/`category`/`ts` of every existing row are
    untouched -- they already sit in the right column/value (AC3/AC7.5)."""
    conn.execute("ALTER TABLE logs ADD COLUMN habit_type TEXT NULL")
    conn.execute(
        """
        UPDATE logs SET habit_type = CASE category
            WHEN 'water' THEN 'numeric'
            WHEN 'stretch' THEN 'duration'
            WHEN 'diary' THEN 'text'
            ELSE NULL
        END
        """
    )


def _migration_005_habit_targets(conn: sqlite3.Connection) -> None:
    """SPEC-v1.1.md "Undo menu + per-habit targets" (§4 R-T1): additive-only,
    like every migration before it -- a new, small override table, no
    ALTER/DROP on `logs`. `habit_id` is the primary key (one override row
    per habit, upserted by `Database.set_target`); `goal` is the daily
    target in the habit's base unit. `IF NOT EXISTS` makes re-running a
    no-op (AC12)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS habit_targets (
          habit_id   TEXT PRIMARY KEY,
          goal       REAL NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )


# Ordered list of migrations. Index 0 -> user_version 1, index 1 ->
# user_version 2, etc. Append-only: never reorder or remove an entry once
# it has shipped, or a DB stamped at that version will silently skip it.
MIGRATIONS: list[Migration] = [
    _migration_001_baseline,
    _migration_002_category_index,
    _migration_003_soft_delete,
    _migration_004_habit_type,
    _migration_005_habit_targets,
]


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def run_migrations(
    conn: sqlite3.Connection, migrations: list[Migration] | None = None
) -> tuple[int, int]:
    """Apply every migration above the DB's current user_version, in
    order, each inside its own transaction (schema change + version stamp
    commit/rollback together). Returns (from_version, to_version).

    Idempotent: a DB already at len(migrations) applies nothing and
    returns (N, N).
    """
    migrations = MIGRATIONS if migrations is None else migrations
    target_version = len(migrations)

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # explicit BEGIN/COMMIT/ROLLBACK control
    try:
        from_version = current_version(conn)

        if from_version >= target_version:
            logger.info(
                "DB schema up to date at version %d; no migrations applied", from_version
            )
            return from_version, from_version

        version = from_version
        for idx in range(from_version, target_version):
            migration = migrations[idx]
            name = getattr(migration, "__name__", str(migration))
            next_version = idx + 1
            conn.execute("BEGIN")
            try:
                migration(conn)
                conn.execute(f"PRAGMA user_version = {next_version}")
            except Exception:
                conn.rollback()
                logger.exception(
                    "Migration %d (%s) failed; rolled back, DB left at version %d",
                    next_version,
                    name,
                    version,
                )
                raise
            else:
                conn.commit()
                version = next_version
                logger.info("Applied migration %d/%d (%s)", version, target_version, name)

        logger.info("DB schema migrated %d -> %d", from_version, version)
        return from_version, version
    finally:
        conn.isolation_level = previous_isolation
