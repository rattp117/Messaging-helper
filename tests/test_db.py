"""Database + weekly-review aggregation tests (AC2, AC8's aggregation math,
AC11). All against a real on-disk SQLite file (tmp_path) -- no mocks, since
sqlite3 is cheap, reliable, local state.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from habit_assistant.config import Config
from habit_assistant.core.habits import HabitRegistry
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
        "habit_type",  # ROADMAP.md v0.7.0 migration 004: multi-habit extensibility (AC3)
        "user_id",  # SPEC-v1.2.md migration 006: multi-user support
        "unparsed_state",  # SPEC-v1.10.md migration 013: unparsed-state machine (R-SS1)
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
    db1.insert_log(LogEntry(None, "owner", "2026-08-19T10:00:00", "water", 250.0, None, "1 glass", "reply"))
    db1.close()

    # Reopening must not error (CREATE TABLE/INDEX IF NOT EXISTS) and must
    # preserve previously written rows.
    db2 = Database(db_path)
    rows = db2.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    db2.close()


# ---------------------------------------------------------------------------
# Insert / query round-trips (AC2)
# ---------------------------------------------------------------------------


def test_insert_log_roundtrip_all_fields(tmp_path):
    db = make_db(tmp_path)
    entry = LogEntry(
        id=None,
        user_id="owner",
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

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
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
    entry = LogEntry(None, "owner", "2026-08-19T21:30:00", "diary", None, "a good day", "a good day", "reply")

    db.insert_log(entry)

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
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
    db.insert_log(LogEntry(None, "owner", "2026-08-19T23:59:59", "water", 500.0, None, "late glass", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T00:00:00", "water", 300.0, None, "next day glass", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T08:00:00", "water", 250.0, None, "morning glass", "reply"))

    assert db.water_total_ml("owner", "2026-08-19") == 750.0
    assert db.water_total_ml("owner", "2026-08-20") == 300.0
    assert db.water_total_ml("owner", "2026-08-21") == 0.0
    db.close()


def test_water_total_ml_with_no_logs_returns_zero(tmp_path):
    db = make_db(tmp_path)
    assert db.water_total_ml("owner", "2026-08-19") == 0.0
    db.close()


def test_stretch_count_respects_day_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T11:00:00", "stretch", 10.0, None, "stretch 1", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T16:00:00", "stretch", 5.0, None, "stretch 2", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T11:00:00", "stretch", 15.0, None, "stretch next day", "reply"))

    assert db.stretch_count("owner", "2026-08-19") == 2
    assert db.stretch_count("owner", "2026-08-20") == 1
    assert db.stretch_count("owner", "2026-08-21") == 0
    db.close()


def test_diary_count_respects_day_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T21:30:00", "diary", None, "entry 1", "entry 1", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T21:30:00", "diary", None, "entry 2", "entry 2", "reply"))

    assert db.diary_count("owner", "2026-08-19") == 1
    assert db.diary_count("owner", "2026-08-20") == 1
    assert db.diary_count("owner", "2026-08-21") == 0
    db.close()


# ---------------------------------------------------------------------------
# ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (AC4): generic
# sum_value/count/count_true, soft-delete-aware, and the v0.6 wrappers
# (water_total_ml/stretch_count/diary_count) returning identical numbers.
# ---------------------------------------------------------------------------


def test_sum_value_matches_water_total_ml_and_excludes_soft_deleted(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T08:00:00", "water", 500.0, None, "a", "reply"))
    row_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 300.0, None, "b", "reply"))
    db.soft_delete(row_id)

    assert db.sum_value("owner", "water", "2026-08-19") == 500.0
    assert db.sum_value("owner", "water", "2026-08-19") == db.water_total_ml("owner", "2026-08-19")
    db.close()


def test_count_matches_stretch_count_and_diary_count(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T11:00:00", "stretch", 10.0, None, "a", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T16:00:00", "stretch", 5.0, None, "b", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T21:30:00", "diary", None, "c", "c", "reply"))

    assert db.count("owner", "stretch", "2026-08-19") == 2 == db.stretch_count("owner", "2026-08-19")
    assert db.count("owner", "diary", "2026-08-19") == 1 == db.diary_count("owner", "2026-08-19")
    db.close()


def test_count_excludes_soft_deleted_rows(tmp_path):
    db = make_db(tmp_path)
    row_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T11:00:00", "stretch", 10.0, None, "a", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T16:00:00", "stretch", 5.0, None, "b", "reply"))
    db.soft_delete(row_id)

    assert db.count("owner", "stretch", "2026-08-19") == 1
    db.close()


def test_count_true_counts_only_truthy_boolean_rows(tmp_path):
    """log_entry_from_result encodes a boolean habit as value_num 1.0/0.0
    (SPEC-v0.7.md §4 R10) -- count_true counts only the 1.0 rows, and
    excludes a soft-deleted truthy row."""
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "meds", 1.0, None, "took meds", "reply", habit_type="boolean"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T10:00:00", "meds", 0.0, None, "no meds", "reply", habit_type="boolean"))
    deleted_id = db.insert_log(
        LogEntry(None, "owner", "2026-08-19T11:00:00", "meds", 1.0, None, "took meds again", "reply", habit_type="boolean")
    )
    db.soft_delete(deleted_id)

    assert db.count_true("owner", "meds", "2026-08-19") == 1
    db.close()


def test_insert_log_writes_habit_type_column(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 500.0, None, "500ml", "reply", habit_type="numeric"))

    row = db._conn.execute("SELECT habit_type FROM logs WHERE category = 'water'").fetchone()
    assert row["habit_type"] == "numeric"
    db.close()


def test_reclassify_log_stamps_habit_type(tmp_path):
    db = make_db(tmp_path)
    row_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "unparsed", None, None, "500ml", "reply"))

    db.reclassify_log(row_id, "water", 500.0, None, habit_type="numeric")

    row = db._conn.execute("SELECT category, value_num, habit_type FROM logs WHERE id = ?", (row_id,)).fetchone()
    assert row["category"] == "water"
    assert row["value_num"] == 500.0
    assert row["habit_type"] == "numeric"
    db.close()


def test_reclassify_log_habit_type_defaults_to_none_for_pre_v070_callers(tmp_path):
    """Backward compatibility: a caller that doesn't pass habit_type (the
    v0.6.0 call shape) still works, leaving the column NULL."""
    db = make_db(tmp_path)
    row_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "unparsed", None, None, "500ml", "reply"))

    db.reclassify_log(row_id, "water", 500.0, None)

    row = db._conn.execute("SELECT habit_type FROM logs WHERE id = ?", (row_id,)).fetchone()
    assert row["habit_type"] is None
    db.close()


def test_water_total_ml_only_counts_water_category(tmp_path):
    """A stretch/diary log on the same day/timestamp prefix must not leak
    into the water total (category filter correctness)."""
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T10:00:00", "water", 500.0, None, "500ml", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T10:05:00", "stretch", 10.0, None, "10 min", "reply"))

    assert db.water_total_ml("owner", "2026-08-19") == 500.0
    db.close()


def test_logs_between_inclusive_bounds(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T00:00:00", "water", 250.0, None, "a", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T12:00:00", "water", 250.0, None, "b", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T23:59:59", "water", 250.0, None, "c", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T00:00:00", "water", 250.0, None, "d", "reply"))

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")

    assert [r["raw_message"] for r in rows] == ["a", "b", "c"]
    db.close()


def test_logs_between_orders_by_ts_ascending(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T15:00:00", "water", 250.0, None, "later", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T08:00:00", "water", 250.0, None, "earlier", "reply"))

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")

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
            db.insert_log(LogEntry(None, "owner", f"{day_str}T09:00:00", "water", float(water_ml), None, "seed water", "reply"))
        for i in range(stretch_n):
            db.insert_log(LogEntry(None, "owner", f"{day_str}T11:{i:02d}:00", "stretch", 10.0, None, "seed stretch", "reply"))
        for i in range(diary_n):
            db.insert_log(LogEntry(None, "owner", f"{day_str}T21:30:00", "diary", None, "seed diary", "seed diary", "reply"))


# ROADMAP.md v0.7.0 integration: `compute_weekly_stats`/`format_stats_summary`
# gained a required `registry` param (SPEC-v0.7.md §5, module M3), and
# `WeeklyStats` is now per-habit (`stats.get(habit_id)`) instead of a single
# water-shaped object -- registry-wiring/accessor-shape edits only, same
# underlying math/assertions as before (per `IMPL-v0.7-M3.md`'s own rename
# table for the equivalent tests it kept in `test_review.py`).


def test_compute_weekly_stats_totals_and_adherence(tmp_path):
    db = make_db(tmp_path)
    config = Config()  # default goal_ml = 2500
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)

    stats = compute_weekly_stats(db, config, registry, end_date, "owner")
    water = stats.get("water")

    assert len(water.days) == 7
    assert water.days[0].day == (end_date - timedelta(days=6)).isoformat()
    assert water.days[-1].day == end_date.isoformat()

    # Per-day adherence % (water_ml / goal_ml * 100)
    by_day = {d.day: d for d in water.days}
    assert by_day[(end_date - timedelta(days=6)).isoformat()].pct == 40.0  # 1000/2500
    assert by_day[end_date.isoformat()].pct == 100.0  # 2500/2500

    assert water.total == 1000 + 2500 + 0 + 1250 + 2500 + 2500 + 2500
    assert water.avg == round(water.total / 7, 1)
    assert stats.get("stretch").total == 0 + 1 + 1 + 0 + 1 + 1 + 1
    assert stats.get("diary").total == 0 + 1 + 0 + 0 + 1 + 0 + 1
    db.close()


def test_compute_weekly_stats_current_streak(tmp_path):
    """Streak = consecutive stretch days counting backward from the last
    (most recent / end_date) day. Per the seeded plan, days -3 has no
    stretch, so the streak should only count days -2, -1, 0 = 3."""
    db = make_db(tmp_path)
    config = Config()
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)

    stats = compute_weekly_stats(db, config, registry, end_date, "owner")

    assert stats.get("stretch").streak == 3
    db.close()


def test_compute_weekly_stats_streak_zero_when_last_day_has_no_stretch(tmp_path):
    db = make_db(tmp_path)
    config = Config()
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))
    # No stretch logs at all this week.

    stats = compute_weekly_stats(db, config, registry, end_date, "owner")

    assert stats.get("stretch").streak == 0
    db.close()


def test_compute_weekly_stats_empty_week_is_all_zero(tmp_path):
    db = make_db(tmp_path)
    config = Config()
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)

    stats = compute_weekly_stats(db, config, registry, end_date, "owner")

    assert stats.get("water").total == 0
    assert stats.get("water").avg == 0.0
    assert stats.get("stretch").total == 0
    assert stats.get("stretch").streak == 0
    assert stats.get("diary").total == 0
    assert all(d.pct == 0.0 for d in stats.get("water").days)
    db.close()


def test_format_stats_summary_contains_expected_figures(tmp_path):
    db = make_db(tmp_path)
    config = Config()
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    stats = compute_weekly_stats(db, config, registry, end_date, "owner")

    summary = format_stats_summary(stats, registry)

    assert f"Water total: {int(stats.get('water').total)} ml" in summary
    assert f"Stretch sessions this week: {stats.get('stretch').total}" in summary
    assert f"current streak: {stats.get('stretch').streak} day(s)" in summary
    assert f"Diary entries this week: {stats.get('diary').total}" in summary
    db.close()


def test_compute_weekly_stats_respects_custom_goal(tmp_path):
    db = make_db(tmp_path)
    config = Config.model_validate({"reminders": {"water": {"goal_ml": 1000}}})
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, config, registry, end_date, "owner")
    water = stats.get("water")

    assert water.days[-1].goal == 1000
    assert water.days[-1].pct == 50.0
    db.close()
