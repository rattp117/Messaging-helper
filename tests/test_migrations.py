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
    db1.insert_log(LogEntry(None, "owner", "2026-08-19T10:00:00", "water", 250.0, None, "1 glass", "reply"))
    db1.close()

    db2 = Database(db_path)

    assert db2.schema_version_before == first_version
    assert db2.schema_version == first_version
    rows = db2.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
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


# ---------------------------------------------------------------------------
# ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (AC3): migration 004
# (`habit_type`) on a COPY of a v3-shaped DB (never the live
# `data/habits.db` -- this test builds its own throwaway file under
# `tmp_path`). Verifies schema_version 3->4, the new column's backfilled
# values per category, and that every pre-existing row's
# value_num/value_text/category/ts is byte-for-byte unchanged.
# ---------------------------------------------------------------------------


def test_v3_shaped_db_migrates_to_v4_with_habit_type_backfilled(tmp_path):
    db_path = tmp_path / "v3_copy.db"

    # Hand-build a v3-shaped DB (migrations 001-003 already applied,
    # i.e. the schema shape shipped through v0.6.0): logs table +
    # deleted_at, no habit_type yet, user_version=3. Seeded with real rows
    # across every category the live DB could plausibly carry, including
    # 'unparsed' (SPEC-v0.7.md §2.3).
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        PRAGMA user_version = 3;
        """
    )
    rows_to_insert = [
        ("2026-08-10T09:00:00", "water", 500.0, None, "500ml", "reply"),
        ("2026-08-10T11:00:00", "stretch", 10.0, None, "10 min stretch", "reply"),
        ("2026-08-10T21:30:00", "diary", None, "a good day", "a good day", "reply"),
        ("2026-08-11T09:00:00", "unparsed", None, None, "garbled message", "reply"),
    ]
    for ts, category, value_num, value_text, raw_message, source in rows_to_insert:
        conn.execute(
            "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, category, value_num, value_text, raw_message, source),
        )
    conn.commit()
    before_rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source FROM logs ORDER BY id"
        )
    ]
    assert current_version(conn) == 3
    conn.close()

    # Open through the real Database class -- this also runs migrations 005
    # (SPEC-v1.1.md, additive `habit_targets`), 006 (SPEC-v1.2.md,
    # `users`/`logs.user_id`/habit_targets rebuild/`user_reminder_times`),
    # and 007 (SPEC-v1.3.md, additive `audit_log`) since all three are now
    # unconditionally part of MIGRATIONS; a v3-shaped DB opened today lands
    # on version 7, not 4, but everything asserted below about migration
    # 004's own effect (habit_type backfill, untouched logs rows) still
    # holds -- neither 006's `logs.user_id` column addition nor 007's new
    # `audit_log` table touches the columns selected below.
    db = Database(db_path)

    assert db.schema_version_before == 3
    assert db.schema_version == 7

    after_rows = [
        tuple(r)
        for r in db._conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source FROM logs ORDER BY id"
        )
    ]
    assert after_rows == before_rows  # untouched, byte-for-byte, row count identical

    habit_types = {
        row["category"]: row["habit_type"]
        for row in db._conn.execute("SELECT category, habit_type FROM logs ORDER BY id")
    }
    assert habit_types["water"] == "numeric"
    assert habit_types["stretch"] == "duration"
    assert habit_types["diary"] == "text"
    assert habit_types["unparsed"] is None
    db.close()

    # Re-running (reopen) migrates nothing further (idempotent).
    reopened = Database(db_path)
    assert reopened.schema_version_before == 7
    assert reopened.schema_version == 7
    reopened.close()


def test_fresh_db_has_habit_type_column(tmp_path):
    # SPEC-v1.1.md added migration 005 after this one (004); a fresh DB now
    # lands on the latest version (5, asserted separately below), not
    # hardcoded here -- this test only cares that habit_type exists.
    db = Database(tmp_path / "fresh_v4.db")
    cols = {row[1] for row in db._conn.execute("PRAGMA table_info(logs)").fetchall()}
    assert "habit_type" in cols
    db.close()


# ---------------------------------------------------------------------------
# SPEC-v1.1.md "Undo menu + per-habit targets" (AC12): migration 005
# (`habit_targets`) on a fresh DB and on a v4-shaped DB, both built on
# throwaway `tmp_path` files -- never the live `data/habits.db`.
# ---------------------------------------------------------------------------


def test_fresh_db_reports_schema_version_7_with_habit_targets_table(tmp_path):
    # SPEC-v1.2.md added migration 006 (this test's own focus, habit_
    # targets rebuilt with a surrogate `id` PK + `user_id`, R-M1) and
    # SPEC-v1.3.md then added migration 007 (audit_log, additive) after
    # it; a fresh DB now lands on version 7, not 6.
    db = Database(tmp_path / "fresh_v6.db")
    assert db.schema_version == 7
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "habit_targets" in tables
    cols = {row[1] for row in db._conn.execute("PRAGMA table_info(habit_targets)").fetchall()}
    assert cols == {"id", "user_id", "habit_id", "goal", "updated_at"}
    db.close()


def test_v4_shaped_db_migrates_to_v5_habit_targets_idempotent_and_logs_untouched(tmp_path):
    db_path = tmp_path / "v4_copy.db"

    # Hand-build a v4-shaped DB (migrations 001-004 already applied): logs
    # + habit_type, no habit_targets yet, user_version=4.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        PRAGMA user_version = 4;
        """
    )
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-08-10T09:00:00", "water", 500.0, None, "500ml", "reply", "numeric"),
    )
    conn.commit()
    before_rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source, habit_type FROM logs ORDER BY id"
        )
    ]
    assert current_version(conn) == 4
    conn.close()

    db = Database(db_path)

    # SPEC-v1.2.md/SPEC-v1.3.md: opening a v4-shaped DB today also applies
    # migration 006 (users/logs.user_id/habit_targets rebuild/
    # user_reminder_times) and migration 007 (audit_log), so it lands on
    # version 7, not 5.
    assert db.schema_version_before == 4
    assert db.schema_version == 7

    after_rows = [
        tuple(r)
        for r in db._conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source, habit_type FROM logs ORDER BY id"
        )
    ]
    assert after_rows == before_rows  # logs untouched, byte-for-byte (R-T1: no ALTER/DROP on logs)

    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "habit_targets" in tables
    db.close()

    # Re-running (reopen) applies nothing further (idempotent, AC12).
    reopened = Database(db_path)
    assert reopened.schema_version_before == 7
    assert reopened.schema_version == 7
    reopened.close()


def test_habit_targets_get_set_clear_all(tmp_path):
    db = Database(tmp_path / "targets.db")

    assert db.get_target("owner", "water") is None
    assert db.all_targets("owner") == {}

    db.set_target("owner", "water", 2000.0)
    assert db.get_target("owner", "water") == 2000.0
    assert db.all_targets("owner") == {"water": 2000.0}

    db.set_target("owner", "water", 1800.0)  # upsert -- replaces, doesn't stack
    assert db.get_target("owner", "water") == 1800.0
    assert db.all_targets("owner") == {"water": 1800.0}

    db.set_target("owner", "stretch", 20.0)
    assert db.all_targets("owner") == {"water": 1800.0, "stretch": 20.0}

    db.clear_target("owner", "water")
    assert db.get_target("owner", "water") is None
    assert db.all_targets("owner") == {"stretch": 20.0}

    db.clear_target("owner", "water")  # no-op, not an error, when absent
    assert db.get_target("owner", "water") is None
    db.close()


def test_habit_targets_are_scoped_per_user(tmp_path):
    """SPEC-v1.2.md R-D2 (AC-U1): the migration-006 rebuild makes
    `habit_targets` UNIQUE(user_id, habit_id), not just habit_id -- two
    users can each have their own override for the same habit id."""
    db = Database(tmp_path / "targets_multiuser.db")

    db.set_target("user-a", "water", 2000.0)
    db.set_target("user-b", "water", 3000.0)

    assert db.get_target("user-a", "water") == 2000.0
    assert db.get_target("user-b", "water") == 3000.0
    assert db.all_targets("user-a") == {"water": 2000.0}
    assert db.all_targets("user-b") == {"water": 3000.0}

    db.clear_target("user-a", "water")
    assert db.get_target("user-a", "water") is None
    assert db.get_target("user-b", "water") == 3000.0  # untouched by A's clear
    db.close()


def test_get_log_returns_row_regardless_of_deleted_at(tmp_path):
    db = Database(tmp_path / "getlog.db")
    from habit_assistant.storage.models import LogEntry

    row_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 500.0, None, "500ml", "reply"))

    live = db.get_log(row_id)
    assert live is not None
    assert live["id"] == row_id
    assert live["deleted_at"] is None

    db.soft_delete(row_id)
    still_returned = db.get_log(row_id)
    assert still_returned is not None
    assert still_returned["deleted_at"] is not None  # get_log does NOT filter deleted rows

    assert db.get_log(row_id + 999) is None  # genuinely missing id
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


# ---------------------------------------------------------------------------
# SPEC-v1.2.md "Multi-user support" (AC-M1): migration 006 -- `users`,
# `logs.user_id` + index, and the `habit_targets` rebuild (the ONE
# sanctioned break of the additive-only guarantee, R-M1). Built on a v5-shaped
# DB, never the live `data/habits.db`.
# ---------------------------------------------------------------------------


def test_v5_shaped_db_migrates_to_v6_multiuser(tmp_path):
    db_path = tmp_path / "v5_copy.db"

    # Hand-build a v5-shaped DB (migrations 001-005 already applied): logs +
    # habit_type + the OLD habit_targets (habit_id-only PK), user_version=5.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        CREATE TABLE habit_targets (
          habit_id   TEXT PRIMARY KEY,
          goal       REAL NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        PRAGMA user_version = 5;
        """
    )
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-08-10T09:00:00", "water", 500.0, None, "500ml", "reply", "numeric"),
    )
    conn.execute("INSERT INTO habit_targets (habit_id, goal) VALUES ('water', 2500.0)")
    conn.commit()
    before_logs = [
        tuple(r)
        for r in conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source, habit_type FROM logs ORDER BY id"
        )
    ]
    before_targets = [tuple(r) for r in conn.execute("SELECT habit_id, goal FROM habit_targets")]
    assert current_version(conn) == 5
    conn.close()

    db = Database(db_path)

    # SPEC-v1.3.md added migration 007 (audit_log, additive) after this
    # one; a v5-shaped DB opened today lands on version 7, not 6, but
    # everything asserted below about migration 006's own effect (users/
    # logs.user_id/habit_targets rebuild/user_reminder_times) still holds.
    assert db.schema_version_before == 5
    assert db.schema_version == 7

    # logs values preserved, byte-for-byte; new user_id column present and NULL.
    after_logs = [
        tuple(r)
        for r in db._conn.execute(
            "SELECT id, ts, category, value_num, value_text, raw_message, source, habit_type FROM logs ORDER BY id"
        )
    ]
    assert after_logs == before_logs
    log_cols = {row[1] for row in db._conn.execute("PRAGMA table_info(logs)").fetchall()}
    assert "user_id" in log_cols
    user_ids = [row["user_id"] for row in db._conn.execute("SELECT user_id FROM logs")]
    assert user_ids == [None]  # the migration itself cannot know the owner (R-M1) -- R-M2 fills it later

    # habit_targets rebuilt: same (habit_id, goal) preserved, user_id NULL,
    # new surrogate id PK + UNIQUE(user_id, habit_id).
    after_targets = [
        (row["habit_id"], row["goal"]) for row in db._conn.execute("SELECT habit_id, goal FROM habit_targets")
    ]
    assert after_targets == before_targets
    target_user_ids = [row["user_id"] for row in db._conn.execute("SELECT user_id FROM habit_targets")]
    assert target_user_ids == [None]
    target_cols = {row[1] for row in db._conn.execute("PRAGMA table_info(habit_targets)").fetchall()}
    assert target_cols == {"id", "user_id", "habit_id", "goal", "updated_at"}

    # users + user_reminder_times both created, users empty (nobody attributed yet).
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "user_reminder_times"} <= tables
    assert db._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    db.close()

    # Re-running (reopen) applies nothing further (idempotent, AC-M1).
    reopened = Database(db_path)
    assert reopened.schema_version_before == 7
    assert reopened.schema_version == 7
    reopened.close()


def test_fresh_db_has_users_and_user_reminder_times_tables(tmp_path):
    db = Database(tmp_path / "fresh_v6.db")
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "user_reminder_times"} <= tables
    user_cols = {row[1] for row in db._conn.execute("PRAGMA table_info(users)").fetchall()}
    assert user_cols == {
        "chat_id", "role", "status", "display_name", "language_pref",
        "quiet_hours_json", "snooze_default_minutes", "created_at",
    }
    reminder_cols = {row[1] for row in db._conn.execute("PRAGMA table_info(user_reminder_times)").fetchall()}
    assert reminder_cols == {"user_id", "habit_id", "time"}
    db.close()


# ---------------------------------------------------------------------------
# SPEC-v1.3.md "Audit log" (AC-A1): migration 007 -- `audit_log` + its two
# indexes, PURELY additive (unlike 006, the one sanctioned break -- this
# migration touches no existing table/column/row at all). Built on a
# v6-shaped DB, never the live `data/habits.db`.
# ---------------------------------------------------------------------------


def test_v6_shaped_db_migrates_to_v7_audit_log_touching_nothing_existing(tmp_path):
    db_path = tmp_path / "v6_copy.db"

    # Hand-build a v6-shaped DB (migrations 001-006 already applied): the
    # full pre-audit-log multiuser schema, user_version=6, with a real
    # user + log + target row so "touches no existing row" is actually
    # exercised, not just asserted against an empty DB.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL,
          user_id     TEXT NULL
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        CREATE INDEX idx_logs_user ON logs(user_id, category, ts);
        CREATE TABLE habit_targets (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    TEXT,
          habit_id   TEXT NOT NULL,
          goal       REAL NOT NULL,
          updated_at TEXT,
          UNIQUE(user_id, habit_id)
        );
        CREATE TABLE users (
          chat_id                TEXT PRIMARY KEY,
          role                   TEXT NOT NULL DEFAULT 'member',
          status                 TEXT NOT NULL DEFAULT 'pending',
          display_name           TEXT,
          language_pref          TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json       TEXT,
          snooze_default_minutes INTEGER,
          created_at             TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE user_reminder_times (
          user_id  TEXT NOT NULL,
          habit_id TEXT NOT NULL,
          time     TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, time)
        );
        PRAGMA user_version = 6;
        """
    )
    conn.execute(
        "INSERT INTO users (chat_id, role, status) VALUES ('owner-chat-id', 'owner', 'active')"
    )
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type, user_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-08-10T09:00:00", "water", 500.0, None, "500ml", "reply", "numeric", "owner-chat-id"),
    )
    conn.execute(
        "INSERT INTO habit_targets (user_id, habit_id, goal) VALUES ('owner-chat-id', 'water', 2500.0)"
    )
    conn.commit()
    before_users = [tuple(r) for r in conn.execute("SELECT chat_id, role, status FROM users")]
    before_logs = [
        tuple(r) for r in conn.execute("SELECT id, ts, category, value_num, user_id FROM logs ORDER BY id")
    ]
    before_targets = [tuple(r) for r in conn.execute("SELECT id, user_id, habit_id, goal FROM habit_targets")]
    assert current_version(conn) == 6
    conn.close()

    # Open through the real Database class -- migration 007 runs here.
    db = Database(db_path)

    assert db.schema_version_before == 6
    assert db.schema_version == 7

    # Every pre-existing table/row is untouched, byte-for-byte -- the
    # additive-only guarantee AC-A1 requires (unlike 006's own sanctioned
    # rebuild of habit_targets, this migration rebuilds nothing).
    after_users = [tuple(r) for r in db._conn.execute("SELECT chat_id, role, status FROM users")]
    after_logs = [
        tuple(r) for r in db._conn.execute("SELECT id, ts, category, value_num, user_id FROM logs ORDER BY id")
    ]
    after_targets = [tuple(r) for r in db._conn.execute("SELECT id, user_id, habit_id, goal FROM habit_targets")]
    assert after_users == before_users
    assert after_logs == before_logs
    assert after_targets == before_targets

    # The new table + both indexes exist, with exactly the columns R-M1 specifies.
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "audit_log" in tables
    cols = {row[1] for row in db._conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    assert cols == {
        "id", "ts", "user_id", "action", "entity", "old_value", "new_value",
        "source", "target_user_id", "created_at",
    }
    indexes = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_audit_ts", "idx_audit_user"} <= indexes
    assert db._conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 0
    db.close()

    # Re-running (reopen) applies nothing further (idempotent, AC-A1).
    reopened = Database(db_path)
    assert reopened.schema_version_before == 7
    assert reopened.schema_version == 7
    reopened.close()


def test_fresh_db_has_audit_log_table_with_expected_shape(tmp_path):
    db = Database(tmp_path / "fresh_v7.db")
    assert db.schema_version == 7
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "audit_log" in tables
    cols = {row[1] for row in db._conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    assert cols == {
        "id", "ts", "user_id", "action", "entity", "old_value", "new_value",
        "source", "target_user_id", "created_at",
    }
    db.close()


def test_insert_recent_and_prune_audit_round_trip(tmp_path):
    """`storage/db.py`'s three audit accessors, exercised directly against
    a real on-disk DB -- the actual round-trip `core/audit.py:record`
    (tested separately) builds on top of."""
    from datetime import datetime, timedelta

    from habit_assistant.storage.models import AuditEntry

    db = Database(tmp_path / "audit_accessors.db")

    row_id = db.insert_audit(
        AuditEntry(
            id=None,
            ts="2026-08-20T09:00:00",
            user_id="owner-chat-id",
            action="target_set",
            entity="water",
            old_value="2500",
            new_value="2000",
            source="command",
        )
    )
    assert isinstance(row_id, int) and row_id > 0

    old_ts = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    db.insert_audit(
        AuditEntry(None, old_ts, "owner-chat-id", "undo", "water", "500", None, "button")
    )

    recent = db.recent_audit(10)
    assert len(recent) == 2
    assert recent[0]["action"] == "undo"  # newest first (id DESC)
    assert recent[1]["action"] == "target_set"

    cutoff = (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds")
    pruned = db.prune_audit(cutoff)
    assert pruned == 1  # only the 400-day-old row

    remaining = db.recent_audit(10)
    assert len(remaining) == 1
    assert remaining[0]["action"] == "target_set"
    db.close()


# ---------------------------------------------------------------------------
# SPEC-v1.2.md R-M2 (AC-M2): startup attribution -- `attribute_legacy_to_
# owner`, called once in `async_main` right after `load_secrets`.
# ---------------------------------------------------------------------------


def test_attribute_legacy_to_owner_upserts_owner_row_and_backfills_null_user_ids(tmp_path):
    from habit_assistant.storage.models import LogEntry

    db = Database(tmp_path / "attribution.db")
    # Simulate pre-v1.2 legacy rows: user_id is NULL until attribution runs.
    db.insert_log(LogEntry(None, None, "2026-08-10T09:00:00", "water", 500.0, None, "500ml", "reply"))
    db.set_target(None, "water", 2500.0)
    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id IS NULL").fetchone()["n"] == 1
    assert db._conn.execute("SELECT COUNT(*) AS n FROM habit_targets WHERE user_id IS NULL").fetchone()["n"] == 1

    db.attribute_legacy_to_owner("owner-chat-id")

    owner_row = db.get_user("owner-chat-id")
    assert owner_row is not None
    assert owner_row["role"] == "owner"
    assert owner_row["status"] == "active"

    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id IS NULL").fetchone()["n"] == 0
    assert db._conn.execute("SELECT COUNT(*) AS n FROM habit_targets WHERE user_id IS NULL").fetchone()["n"] == 0
    assert db.last_log("owner-chat-id") is not None
    assert db.get_target("owner-chat-id", "water") == 2500.0
    db.close()


def test_attribute_legacy_to_owner_is_idempotent_and_never_downgrades(tmp_path):
    db = Database(tmp_path / "attribution2.db")

    db.attribute_legacy_to_owner("owner-chat-id")
    first = db.get_user("owner-chat-id")
    assert first["role"] == "owner"
    assert first["status"] == "active"

    # Running it again changes nothing -- no NULL rows left to backfill, and
    # the owner's own role/status stay exactly role=owner/status=active.
    db.attribute_legacy_to_owner("owner-chat-id")
    second = db.get_user("owner-chat-id")
    assert second["role"] == "owner"
    assert second["status"] == "active"
    assert db._conn.execute("SELECT COUNT(*) AS n FROM users WHERE chat_id = 'owner-chat-id'").fetchone()["n"] == 1
    db.close()


def test_attribute_legacy_to_owner_does_not_touch_already_attributed_rows(tmp_path):
    """A row already attributed to a DIFFERENT (non-owner) user must not be
    reassigned to the owner -- the backfill is `WHERE user_id IS NULL` only."""
    from habit_assistant.storage.models import LogEntry

    db = Database(tmp_path / "attribution3.db")
    db.insert_log(LogEntry(None, "member-chat-id", "2026-08-10T09:00:00", "water", 300.0, None, "300ml", "reply"))

    db.attribute_legacy_to_owner("owner-chat-id")

    assert db.last_log("member-chat-id") is not None
    assert db.last_log("owner-chat-id") is None  # nothing was reassigned to the owner
    db.close()
