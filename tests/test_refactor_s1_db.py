"""SPEC-REFACTOR.md Stage 1, DB track (AC2, AC3): pragma setup + the
`ts LIKE '{day}%'` -> range-bound rewrite of `sum_value`/`count`/
`count_true`. All against real on-disk SQLite files (tmp_path) -- no mocks.

Two kinds of proof here:
- Byte-identical output across every boundary rule 3 names (midnight,
  23:59:59, next-day exclusion, soft-delete interplay, month/year
  rollover) -- built by comparing the CURRENT (range-bound) implementation
  against a hand-rolled reference `ts LIKE '{day}%'` query run directly
  against the same rows, so this test keeps proving byte-identity even
  after `sum_value`/`count`/`count_true` themselves no longer contain a
  LIKE clause to compare against.
- `_day_bounds` unit tests for the pure date-math helper.
- The pragma settings (`synchronous=NORMAL`, `busy_timeout=5000`) are
  actually applied on every new connection.
- `all_reminder_times()` (rule 1(a)'s cross-track dependency, added
  after the parallel S1-B/tick track flagged the exact shape it needs in
  `IMPL-refactor-s1-tick.md`): one whole-table bulk read, consumed by
  `core/reminders.py` via `getattr` feature detection -- this file only
  proves the accessor's own shape/ordering/content; byte-identical
  parity against the per-(user, habit) fallback is the tick track's own
  `tests/test_refactor_stage1_tick.py` (its `all_reminder_times`-simulating
  parity test).
"""

from __future__ import annotations

from habit_assistant.storage.db import Database, _day_bounds
from habit_assistant.storage.models import LogEntry


def make_db(tmp_path) -> Database:
    return Database(tmp_path / "sub" / "habits.db")


def _like_sum(db: Database, user_id: str, habit_id: str, day: str) -> float:
    row = db._conn.execute(
        "SELECT COALESCE(SUM(value_num), 0) AS total FROM logs "
        "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ?",
        (user_id, habit_id, f"{day}%"),
    ).fetchone()
    return float(row["total"])


def _like_count(db: Database, user_id: str, habit_id: str, day: str) -> int:
    row = db._conn.execute(
        "SELECT COUNT(*) AS n FROM logs WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ?",
        (user_id, habit_id, f"{day}%"),
    ).fetchone()
    return int(row["n"])


def _like_count_true(db: Database, user_id: str, habit_id: str, day: str) -> int:
    row = db._conn.execute(
        "SELECT COUNT(*) AS n FROM logs "
        "WHERE user_id = ? AND category = ? AND deleted_at IS NULL AND ts LIKE ? AND value_num != 0",
        (user_id, habit_id, f"{day}%"),
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# PRAGMA setup (rule 4 / AC3's own precondition)
# ---------------------------------------------------------------------------


def test_synchronous_is_normal_on_new_connection(tmp_path):
    db = make_db(tmp_path)
    mode = db._conn.execute("PRAGMA synchronous;").fetchone()[0]
    assert mode == 1  # SQLite reports NORMAL as 1 (OFF=0, NORMAL=1, FULL=2, EXTRA=3)
    db.close()


def test_busy_timeout_is_set_on_new_connection(tmp_path):
    db = make_db(tmp_path)
    timeout_ms = db._conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    assert timeout_ms == 5000
    db.close()


def test_wal_mode_still_enabled_alongside_new_pragmas(tmp_path):
    """Stage 1 must not disturb the existing WAL setup (AC-G1)."""
    db = make_db(tmp_path)
    mode = db._conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()


# ---------------------------------------------------------------------------
# _day_bounds -- pure helper unit tests
# ---------------------------------------------------------------------------


def test_day_bounds_ordinary_day():
    assert _day_bounds("2026-08-19") == ("2026-08-19", "2026-08-20")


def test_day_bounds_month_rollover():
    assert _day_bounds("2026-08-31") == ("2026-08-31", "2026-09-01")


def test_day_bounds_year_rollover():
    assert _day_bounds("2026-12-31") == ("2026-12-31", "2027-01-01")


def test_day_bounds_leap_day():
    # 2028 is a leap year -- Feb 29 exists and rolls to Mar 1.
    assert _day_bounds("2028-02-29") == ("2028-02-29", "2028-03-01")


# ---------------------------------------------------------------------------
# Byte-identical boundary proofs (AC2): sum_value/count/count_true vs a
# hand-rolled LIKE reference, across midnight / 23:59:59 / next-day /
# soft-delete / month+year rollover.
# ---------------------------------------------------------------------------


def test_sum_value_matches_like_reference_at_midnight_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T00:00:00", "water", 100.0, None, "midnight exact", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-18T23:59:59", "water", 200.0, None, "just before", "reply"))

    for day in ("2026-08-18", "2026-08-19", "2026-08-20"):
        assert db.sum_value("owner", "water", day) == _like_sum(db, "owner", "water", day)
    db.close()


def test_sum_value_matches_like_reference_at_235959_boundary(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T23:59:59", "water", 500.0, None, "last second", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T00:00:00", "water", 300.0, None, "first second next day", "reply"))

    assert db.sum_value("owner", "water", "2026-08-19") == _like_sum(db, "owner", "water", "2026-08-19") == 500.0
    assert db.sum_value("owner", "water", "2026-08-20") == _like_sum(db, "owner", "water", "2026-08-20") == 300.0
    db.close()


def test_sum_value_excludes_next_day_like_the_like_reference(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T12:00:00", "water", 500.0, None, "today", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T12:00:00", "water", 999.0, None, "tomorrow", "reply"))

    assert db.sum_value("owner", "water", "2026-08-19") == _like_sum(db, "owner", "water", "2026-08-19") == 500.0
    db.close()


def test_sum_value_matches_like_reference_with_soft_deleted_rows(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T08:00:00", "water", 500.0, None, "kept", "reply"))
    deleted_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 300.0, None, "undone", "reply"))
    db.soft_delete(deleted_id)

    assert db.sum_value("owner", "water", "2026-08-19") == _like_sum(db, "owner", "water", "2026-08-19") == 500.0
    db.close()


def test_sum_value_matches_like_reference_across_month_rollover(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-31T23:59:59", "water", 400.0, None, "last day of aug", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-09-01T00:00:00", "water", 600.0, None, "first day of sep", "reply"))

    assert db.sum_value("owner", "water", "2026-08-31") == _like_sum(db, "owner", "water", "2026-08-31") == 400.0
    assert db.sum_value("owner", "water", "2026-09-01") == _like_sum(db, "owner", "water", "2026-09-01") == 600.0
    db.close()


def test_sum_value_matches_like_reference_across_year_rollover(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-12-31T23:59:59", "water", 150.0, None, "last day of year", "reply"))
    db.insert_log(LogEntry(None, "owner", "2027-01-01T00:00:00", "water", 250.0, None, "first day of new year", "reply"))

    assert db.sum_value("owner", "water", "2026-12-31") == _like_sum(db, "owner", "water", "2026-12-31") == 150.0
    assert db.sum_value("owner", "water", "2027-01-01") == _like_sum(db, "owner", "water", "2027-01-01") == 250.0
    db.close()


def test_count_matches_like_reference_across_all_boundaries(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T00:00:00", "stretch", 10.0, None, "midnight", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T23:59:59", "stretch", 10.0, None, "last second", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T00:00:00", "stretch", 10.0, None, "next day", "reply"))
    deleted_id = db.insert_log(LogEntry(None, "owner", "2026-08-19T12:00:00", "stretch", 10.0, None, "undone", "reply"))
    db.soft_delete(deleted_id)

    for day in ("2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"):
        assert db.count("owner", "stretch", day) == _like_count(db, "owner", "stretch", day)
    assert db.count("owner", "stretch", "2026-08-19") == 2  # midnight + 23:59:59, not the soft-deleted one
    db.close()


def test_count_true_matches_like_reference_across_all_boundaries(tmp_path):
    db = make_db(tmp_path)
    db.insert_log(LogEntry(None, "owner", "2026-08-19T00:00:00", "meds", 1.0, None, "midnight true", "reply", habit_type="boolean"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T23:59:59", "meds", 0.0, None, "last second false", "reply", habit_type="boolean"))
    db.insert_log(LogEntry(None, "owner", "2026-08-20T00:00:00", "meds", 1.0, None, "next day true", "reply", habit_type="boolean"))
    deleted_id = db.insert_log(
        LogEntry(None, "owner", "2026-08-19T12:00:00", "meds", 1.0, None, "undone true", "reply", habit_type="boolean")
    )
    db.soft_delete(deleted_id)

    for day in ("2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"):
        assert db.count_true("owner", "meds", day) == _like_count_true(db, "owner", "meds", day)
    assert db.count_true("owner", "meds", "2026-08-19") == 1  # only the midnight-true row
    db.close()


def test_sum_value_count_count_true_agree_with_like_reference_over_a_full_year(tmp_path):
    """A denser fuzz-style proof: seed timestamps at every hour boundary
    across a handful of days spanning a month rollover, then compare the
    live implementation against the LIKE reference for every day touched."""
    db = make_db(tmp_path)
    days = ["2026-01-31", "2026-02-01", "2026-02-28", "2026-03-01", "2026-06-15"]
    for day in days:
        for hh in ("00:00:00", "00:00:01", "11:59:59", "12:00:00", "23:59:58", "23:59:59"):
            db.insert_log(LogEntry(None, "owner", f"{day}T{hh}", "water", 100.0, None, f"{day} {hh}", "reply"))
            db.insert_log(LogEntry(None, "owner", f"{day}T{hh}", "meds", 1.0, None, f"{day} {hh}", "reply", habit_type="boolean"))

    for day in days:
        assert db.sum_value("owner", "water", day) == _like_sum(db, "owner", "water", day)
        assert db.count("owner", "water", day) == _like_count(db, "owner", "water", day)
        assert db.count_true("owner", "meds", day) == _like_count_true(db, "owner", "meds", day)
    db.close()


# ---------------------------------------------------------------------------
# all_reminder_times (SPEC-REFACTOR.md Stage 1 rule 1(a) -- the parallel
# S1-B/tick track's flagged cross-track dependency, IMPL-refactor-s1-tick.md
# "Known limitations").
# ---------------------------------------------------------------------------


def test_all_reminder_times_empty_when_no_overrides_stored(tmp_path):
    db = make_db(tmp_path)
    assert db.all_reminder_times() == []
    db.close()


def test_all_reminder_times_returns_every_row_across_users_and_habits(tmp_path):
    db = make_db(tmp_path)
    db.set_reminder_times("owner", "water", ["08:00", "20:00"])
    db.set_reminder_times("owner", "stretch", ["12:00"])
    db.set_reminder_times("member-a", "water", ["09:00"])

    rows = db.all_reminder_times()

    got = {(r["user_id"], r["habit_id"], r["time"]) for r in rows}
    assert got == {
        ("owner", "water", "08:00"),
        ("owner", "water", "20:00"),
        ("owner", "stretch", "12:00"),
        ("member-a", "water", "09:00"),
    }
    assert len(rows) == 4
    db.close()


def test_all_reminder_times_is_sorted_by_user_then_habit_then_time(tmp_path):
    db = make_db(tmp_path)
    db.set_reminder_times("owner", "water", ["20:00", "08:00"])
    db.set_reminder_times("member-a", "diary", ["07:00"])
    db.set_reminder_times("owner", "diary", ["06:00"])

    rows = db.all_reminder_times()

    assert [(r["user_id"], r["habit_id"], r["time"]) for r in rows] == [
        ("member-a", "diary", "07:00"),
        ("owner", "diary", "06:00"),
        ("owner", "water", "08:00"),
        ("owner", "water", "20:00"),
    ]
    db.close()


def test_all_reminder_times_reflects_the_off_sentinel_verbatim(tmp_path):
    """Storage-only: `all_reminder_times` returns the raw stored rows,
    including the `["off"]` sentinel -- interpreting it (into "no
    reminders") is the caller's job (`core/reminders.py`), mirroring
    `get_reminder_times`'s own "raw value, owning module interprets"
    convention."""
    db = make_db(tmp_path)
    db.set_reminder_times("owner", "water", ["off"])

    rows = db.all_reminder_times()

    assert [(r["user_id"], r["habit_id"], r["time"]) for r in rows] == [("owner", "water", "off")]
    db.close()


def test_all_reminder_times_excludes_cleared_rows(tmp_path):
    db = make_db(tmp_path)
    db.set_reminder_times("owner", "water", ["08:00"])
    db.set_reminder_times("owner", "stretch", ["12:00"])
    db.clear_reminder_times("owner", "water")

    rows = db.all_reminder_times()

    assert [(r["user_id"], r["habit_id"], r["time"]) for r in rows] == [("owner", "stretch", "12:00")]
    db.close()


def test_all_reminder_times_matches_get_reminder_times_per_key(tmp_path):
    """Cross-check against the existing per-key accessor: grouping
    `all_reminder_times()`'s rows by `(user_id, habit_id)` must equal
    calling `get_reminder_times` for each of those same keys -- the bulk
    read is a pure re-shape of the same underlying data, not a different
    view of it."""
    db = make_db(tmp_path)
    db.set_reminder_times("owner", "water", ["08:00", "20:00"])
    db.set_reminder_times("owner", "stretch", ["12:00"])
    db.set_reminder_times("member-a", "water", ["09:00"])

    bulk = db.all_reminder_times()
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in bulk:
        grouped.setdefault((row["user_id"], row["habit_id"]), []).append(row["time"])

    for (user_id, habit_id), times in grouped.items():
        assert sorted(times) == db.get_reminder_times(user_id, habit_id)
    assert set(grouped) == {("owner", "water"), ("owner", "stretch"), ("member-a", "water")}
    db.close()
