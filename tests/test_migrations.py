"""Migration runner tests (ROADMAP.md v0.4.0 "Migrations & Backup/Restore",
shipped as v0.3.0, AC4.1-AC4.2).

All against real on-disk SQLite files (tmp_path) -- no mocks, since sqlite3
is cheap, reliable, local state. Two layers:

- Behavior through the public seam a caller actually uses (`Database`,
  which calls `run_migrations` internally) -- AC4.1, AC4.2.
- Behavior of `run_migrations`/`current_version` directly against a bare
  connection with synthetic migration lists, so the runner's contract
  (idempotent, only-pending-applied, atomic-per-migration) is verified
  independent of whatever `MIGRATIONS` happens to contain today.
"""

from __future__ import annotations

import sqlite3

import pytest

from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS, current_version, run_migrations
from habit_assistant.storage.models import LogEntry

# Exact v0.1.0/v0.2.0 baseline schema (pre-migrations), reproduced here so
# AC4.2 can hand-build a DB that looks like it was created by that version --
# NOT imported from migrations.py, so this test doesn't validate against a
# moving target if the baseline migration's SQL text is refactored.
V010_SCHEMA = """
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


# ---------------------------------------------------------------------------
# AC4.1: fresh DB -> version N; second init applies nothing (idempotent)
# ---------------------------------------------------------------------------


def test_fresh_db_migrates_to_latest_version(tmp_path):
    db_path = tmp_path / "fresh.db"

    db = Database(db_path)

    assert db.schema_version_before == 0
    assert db.schema_version == len(MIGRATIONS)
    assert db.schema_version > 0
    db.close()


def test_second_open_of_migrated_db_applies_nothing(tmp_path):
    db_path = tmp_path / "fresh.db"

    db1 = Database(db_path)
    first_version = db1.schema_version
    db1.insert_log(LogEntry(None, "2026-08-19T10:00:00", "water", 250.0, None, "1 glass", "reply"))
    db1.close()

    db2 = Database(db_path)

    assert db2.schema_version_before == first_version
    assert db2.schema_version == first_version
    rows = db2.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    db2.close()


def test_current_version_reports_pragma_user_version(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "raw.db"))
    try:
        assert current_version(conn) == 0
        conn.execute("PRAGMA user_version = 3")
        assert current_version(conn) == 3
    finally:
        conn.close()


def test_run_migrations_on_bare_connection_returns_from_and_to_version(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "raw2.db"))
    try:
        from_version, to_version = run_migrations(conn)
        assert from_version == 0
        assert to_version == len(MIGRATIONS)

        # Idempotent: calling again on the now-migrated connection applies nothing.
        from_version2, to_version2 = run_migrations(conn)
        assert from_version2 == to_version
        assert to_version2 == to_version
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Runner contract, verified with synthetic migrations so it doesn't depend
# on MIGRATIONS' current length or content.
# ---------------------------------------------------------------------------


def test_run_migrations_applies_only_pending_migrations(tmp_path):
    applied = []

    def mig_a(conn):
        applied.append("a")
        conn.execute("CREATE TABLE a (x INTEGER)")

    def mig_b(conn):
        applied.append("b")
        conn.execute("CREATE TABLE b (x INTEGER)")

    conn = sqlite3.connect(str(tmp_path / "pending.db"))
    try:
        conn.execute("PRAGMA user_version = 1")  # simulate "mig_a already applied"

        from_version, to_version = run_migrations(conn, migrations=[mig_a, mig_b])

        assert from_version == 1
        assert to_version == 2
        assert applied == ["b"]  # mig_a's index (0) is below from_version=1, skipped
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "b" in tables
        assert "a" not in tables
    finally:
        conn.close()


def test_run_migrations_is_noop_when_already_at_target_version(tmp_path):
    calls = []

    def mig(conn):
        calls.append(1)

    conn = sqlite3.connect(str(tmp_path / "atlatest.db"))
    try:
        run_migrations(conn, migrations=[mig])
        assert calls == [1]

        from_version, to_version = run_migrations(conn, migrations=[mig])
        assert from_version == to_version == 1
        assert calls == [1]  # not called a second time
    finally:
        conn.close()


def test_run_migrations_rolls_back_failed_migration_and_reraises(tmp_path):
    def good(conn):
        conn.execute("CREATE TABLE good (x INTEGER)")

    def bad(conn):
        conn.execute("CREATE TABLE bad (x INTEGER)")
        raise RuntimeError("boom")

    conn = sqlite3.connect(str(tmp_path / "failing.db"))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            run_migrations(conn, migrations=[good, bad])

        # good's migration (index 0) committed and stamped version 1; bad's
        # (index 1) rolled back -- DB left at version 1, not 2, and the
        # `bad` table must not exist (whole-migration atomicity).
        assert current_version(conn) == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "good" in tables
        assert "bad" not in tables

        # Retrying from where it left off only re-runs the failed migration
        # onward (per IMPL.md's documented per-migration rollback design).
        def bad_fixed(conn):
            conn.execute("CREATE TABLE bad (x INTEGER)")

        from_version, to_version = run_migrations(conn, migrations=[good, bad_fixed])
        assert from_version == 1
        assert to_version == 2
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "bad" in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC4.2: a v0.1.0-shaped DB migrates forward with all existing rows intact
# (row count and values unchanged).
# ---------------------------------------------------------------------------


def test_v010_shaped_db_migrates_forward_preserving_rows(tmp_path):
    db_path = tmp_path / "legacy.db"

    # Hand-build a v0.1.0/v0.2.0 DB: raw sqlite3, old inline SCHEMA,
    # user_version left at SQLite's implicit default of 0.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(V010_SCHEMA)
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-08-10T09:00:00", "water", 500.0, None, "500ml please", "reply"),
    )
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-08-10T11:00:00", "stretch", 10.0, None, "10 min stretch", "reply"),
    )
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-08-10T21:30:00", "diary", None, "a good day", "a good day", "reply"),
    )
    conn.commit()

    before_rows = [tuple(r) for r in conn.execute("SELECT id, ts, category, value_num, value_text, raw_message, source FROM logs ORDER BY id")]
    before_count = len(before_rows)
    assert before_count == 3
    assert current_version(conn) == 0
    conn.close()

    # Open through the real Database class -- this is where migrations run.
    db = Database(db_path)

    assert db.schema_version_before == 0
    assert db.schema_version == len(MIGRATIONS)

    after_rows = [
        tuple(r)
        for r in db._conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source FROM logs ORDER BY id"
        )
    ]
    assert len(after_rows) == before_count
    assert after_rows == before_rows  # values byte-identical, not just counts
    db.close()


def test_v010_shaped_db_index_and_wal_still_correct_after_migration(tmp_path):
    db_path = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(V010_SCHEMA)
    conn.commit()
    conn.close()

    db = Database(db_path)

    row = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_logs_ts_cat'"
    ).fetchone()
    assert row is not None
    mode = db._conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()
