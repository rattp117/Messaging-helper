"""Database + weekly-review aggregation tests (AC2, AC8's aggregation math,
AC11). All against a real on-disk SQLite file (tmp_path) -- no mocks, since
sqlite3 is cheap, reliable, local state.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from habit_assistant.config import Config
from habit_assistant.core.review import compute_weekly_stats, format_stats_summary
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "sub" / "habits.db")


# ---------------------------------------------------------------------------
# Schema / index / WAL (AC2)
# ---------------------------------------------------------------------------


def test_creates_db_file_and_parent_dir_on_first_run(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "habits.db"
    assert not db_path.exists()

    db = Database(db_path)

    assert db_path.exists()
    db.close()


def test_logs_table_created_with_expected_columns(tmp_path):
    db = make_db(tmp_path)

    cols = {row[1] for row in db._conn.execute("PRAGMA table_info(logs)").fetchall()}

    assert cols == {
        "id", "ts", "category", "value_num", "value_text", "raw_message", "source", "created_at",
        "deleted_at",  # ROADMAP.md v0.5.0 migration 003: soft-delete for undo
    }
    db.close()


def test_index_on_ts_category_created(tmp_path):
    db = make_db(tmp_path)

    row = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_logs_ts_cat'"
    ).fetchone()

    assert row is not None
    db.close()


def test_wal_mode_enabled(tmp_path):
    db = make_db(tmp_path)

    mode = db._conn.execute("PRAGMA journal_mode;").fetchone()[0]

    assert mode.lower() == "wal"
    db.close()


def test_schema_creation_is_idempotent_on_reopen(tmp_path):
    db_path = tmp_path / "habits.db"
    db1 = Database(db_path)
    db1.insert_log(LogEntry(None, "2026-08-19T10:00:00", "water", 250.0, None, "1 glass", "reply"))
    db1.close()

    # Reopening must not error (CREATE TABLE/INDEX IF NOT EXISTS) and must
    # preserve previously written rows.
    db2 = Database(db_path)
    rows = db2.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    db2.close()


# ---------------------------------------------------------------------------
# Insert / query round-trips (AC2)
# ---------------------------------------------------------------------------


def test_insert_log_roundtrip_all_fields(tmp_path):
    db = make_db(tmp_path)
    entry = LogEntry(
        id=None,
        ts="2026-08-19T14:30:00",
        category="water",
        value_num=500.0,
        value_text=None,
        raw_message="500ml please",
        source="reply",
    )

    row_id = db.insert_log(entry)
    assert isinstance(row_id, int)
    assert row_id > 0

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    row = rows[0]
    assert row["ts"] == "2026-08-19T14:30:00"
    assert row["category"] == "water"
    assert row["value_num"] == 500.0
    assert row["value_text"] is None
    assert row["raw_message"] == "500ml please"
    assert row["source"] == "reply"
    assert row["created_at"] is not None
    db.close()


def test_insert_log_diary_roundtrip(tmp_path):
    db = make_db(tmp_path)
    entry = LogEntry(None, "2026-08-19T21:30:00", "diary", None, "a good day", "a good day", "reply")

    db.insert_log(entry)

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows[0]["value_num"] is None
    assert rows[0]["value_text"] == "a good day"
    db.close()


def test_insert_log_default_source_is_reply(tmp_path):
    db = make_db(tmp_path)
    row_id = db._conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message) VALUES (?, ?, ?, ?, ?)",
        ("2026-08-19T09:00:00", "water", 250.0, None, "1 glass"),
    ).lastrowid
    db._conn.commit()

    row = db._conn.execute("SELECT source FROM logs WHERE id = ?", (row_id,)).fetchone()
    assert row["source"] == "reply"
    db.close()


def test_raw_message_not_null_constraint_enforced(tmp_path):
    db = make_db(tmp_path)
    try:
        db._conn.execute(
            "INSERT INTO logs (ts, category, value_num, value_text, raw_message) VALUES (?, ?, ?, ?, NULL)",
            ("2026-08-19T09:00:00", "water", 250.0, None),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "raw_message NOT NULL constraint (SPEC.md §5) should reject a NULL raw_message"
    db.close()


# ---------------------------------------------------------------------------
# Day-boundary correctness (AC2)
# ---------------------------------------------------------------------------


def test_water_total_ml_respects_day_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "2026-08-19T23:59:59", "water", 500.0, None, "late glass", "reply"))
    db.insert_log(LogEntry(None, "2026-08-20T00:00:00", "water", 300.0, None, "next day glass", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T08:00:00", "water", 250.0, None, "morning glass", "reply"))

    assert db.water_total_ml("2026-08-19") == 750.0
    assert db.water_total_ml("2026-08-20") == 300.0
    assert db.water_total_ml("2026-08-21") == 0.0
    db.close()


def test_water_total_ml_with_no_logs_returns_zero(tmp_path):
    db = make_db(tmp_path)
    assert db.water_total_ml("2026-08-19") == 0.0
    db.close()


def test_stretch_count_respects_day_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "2026-08-19T11:00:00", "stretch", 10.0, None, "stretch 1", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T16:00:00", "stretch", 5.0, None, "stretch 2", "reply"))
    db.insert_log(LogEntry(None, "2026-08-20T11:00:00", "stretch", 15.0, None, "stretch next day", "reply"))

    assert db.stretch_count("2026-08-19") == 2
    assert db.stretch_count("2026-08-20") == 1
    assert db.stretch_count("2026-08-21") == 0
    db.close()


def test_diary_count_respects_day_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "2026-08-19T21:30:00", "diary", None, "entry 1", "entry 1", "reply"))
    db.insert_log(LogEntry(None, "2026-08-20T21:30:00", "diary", None, "entry 2", "entry 2", "reply"))

    assert db.diary_count("2026-08-19") == 1
    assert db.diary_count("2026-08-20") == 1
    assert db.diary_count("2026-08-21") == 0
    db.close()


def test_water_total_ml_only_counts_water_category(tmp_path):
    """A stretch/diary log on the same day/timestamp prefix must not leak
    into the water total (category filter correctness)."""
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "2026-08-19T10:00:00", "water", 500.0, None, "500ml", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T10:05:00", "stretch", 10.0, None, "10 min", "reply"))

    assert db.water_total_ml("2026-08-19") == 500.0
    db.close()


def test_logs_between_inclusive_bounds(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "2026-08-19T00:00:00", "water", 250.0, None, "a", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T12:00:00", "water", 250.0, None, "b", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T23:59:59", "water", 250.0, None, "c", "reply"))
    db.insert_log(LogEntry(None, "2026-08-20T00:00:00", "water", 250.0, None, "d", "reply"))

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")

    assert [r["raw_message"] for r in rows] == ["a", "b", "c"]
    db.close()


def test_logs_between_orders_by_ts_ascending(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "2026-08-19T15:00:00", "water", 250.0, None, "later", "reply"))
    db.insert_log(LogEntry(None, "2026-08-19T08:00:00", "water", 250.0, None, "earlier", "reply"))

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")

    assert [r["raw_message"] for r in rows] == ["earlier", "later"]
    db.close()


# ---------------------------------------------------------------------------
# Weekly-review aggregation (AC8 math, computed on top of storage/db.py) --
# seeded data -> adherence %, totals, streak, diary count.
# ---------------------------------------------------------------------------


def _seed_known_week(db: Database, end_date: date) -> None:
    """Seed a deterministic week ending on end_date (inclusive), 7 days
    (offsets 6..0):
      day -6: water 1000ml, no stretch, no diary
      day -5: water 2500ml (exactly goal), 1 stretch, 1 diary
      day -4: water 0ml, 1 stretch, no diary
      day -3: water 1250ml, no stretch, no diary   <- breaks any streak so far
      day -2: water 2500ml, 1 stretch, 1 diary
      day -1: water 2500ml, 1 stretch, no diary
      day  0 (end_date): water 2500ml, 1 stretch, 1 diary
    Expected trailing stretch streak = 3 (days -2, -1, 0).
    """
    plan = {
        6: (1000, 0, 0),
        5: (2500, 1, 1),
        4: (0, 1, 0),
        3: (1250, 0, 0),
        2: (2500, 1, 1),
        1: (2500, 1, 0),
        0: (2500, 1, 1),
    }
    for offset, (water_ml, stretch_n, diary_n) in plan.items():
        day = end_date - timedelta(days=offset)
        day_str = day.isoformat()
        if water_ml:
            db.insert_log(LogEntry(None, f"{day_str}T09:00:00", "water", float(water_ml), None, "seed water", "reply"))
        for i in range(stretch_n):
            db.insert_log(LogEntry(None, f"{day_str}T11:{i:02d}:00", "stretch", 10.0, None, "seed stretch", "reply"))
        for i in range(diary_n):
            db.insert_log(LogEntry(None, f"{day_str}T21:30:00", "diary", None, "seed diary", "seed diary", "reply"))


def test_compute_weekly_stats_totals_and_adherence(tmp_path):
    db = make_db(tmp_path)
    config = Config()  # default goal_ml = 2500
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)

    stats = compute_weekly_stats(db, config, end_date)

    assert len(stats.days) == 7
    assert stats.days[0].day == (end_date - timedelta(days=6)).isoformat()
    assert stats.days[-1].day == end_date.isoformat()

    # Per-day adherence % (water_ml / goal_ml * 100)
    by_day = {d.day: d for d in stats.days}
    assert by_day[(end_date - timedelta(days=6)).isoformat()].water_pct == 40.0  # 1000/2500
    assert by_day[end_date.isoformat()].water_pct == 100.0  # 2500/2500

    assert stats.water_total_ml == 1000 + 2500 + 0 + 1250 + 2500 + 2500 + 2500
    assert stats.water_avg_ml == round(stats.water_total_ml / 7, 1)
    assert stats.stretch_total == 0 + 1 + 1 + 0 + 1 + 1 + 1
    assert stats.diary_count == 0 + 1 + 0 + 0 + 1 + 0 + 1
    db.close()


def test_compute_weekly_stats_current_streak(tmp_path):
    """Streak = consecutive stretch days counting backward from the last
    (most recent / end_date) day. Per the seeded plan, days -3 has no
    stretch, so the streak should only count days -2, -1, 0 = 3."""
    db = make_db(tmp_path)
    config = Config()
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)

    stats = compute_weekly_stats(db, config, end_date)

    assert stats.stretch_streak == 3
    db.close()


def test_compute_weekly_stats_streak_zero_when_last_day_has_no_stretch(tmp_path):
    db = make_db(tmp_path)
    config = Config()
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))
    # No stretch logs at all this week.

    stats = compute_weekly_stats(db, config, end_date)

    assert stats.stretch_streak == 0
    db.close()


def test_compute_weekly_stats_empty_week_is_all_zero(tmp_path):
    db = make_db(tmp_path)
    config = Config()
    end_date = date(2026, 8, 19)

    stats = compute_weekly_stats(db, config, end_date)

    assert stats.water_total_ml == 0
    assert stats.water_avg_ml == 0.0
    assert stats.stretch_total == 0
    assert stats.stretch_streak == 0
    assert stats.diary_count == 0
    assert all(d.water_pct == 0.0 for d in stats.days)
    db.close()


def test_format_stats_summary_contains_expected_figures(tmp_path):
    db = make_db(tmp_path)
    config = Config()
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    stats = compute_weekly_stats(db, config, end_date)

    summary = format_stats_summary(stats)

    assert f"Water total: {int(stats.water_total_ml)} ml" in summary
    assert f"Stretch sessions this week: {stats.stretch_total}" in summary
    assert f"current streak: {stats.stretch_streak} day(s)" in summary
    assert f"Diary entries this week: {stats.diary_count}" in summary
    db.close()


def test_compute_weekly_stats_respects_custom_goal(tmp_path):
    db = make_db(tmp_path)
    config = Config.model_validate({"reminders": {"water": {"goal_ml": 1000}}})
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, config, end_date)

    assert stats.days[-1].water_goal_ml == 1000
    assert stats.days[-1].water_pct == 50.0
    db.close()
