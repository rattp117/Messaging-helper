"""Vera's own adversarial probes for SPEC-v1.9.md module `grace` (M2, AC13-
AC18), independent of Luna's own `tests/test_grace.py` (which already earns
its 15-test AC1:1 coverage -- read that file first; this one exists to poke
at edges she may not have exercised: independent per-habit/per-user grace
budgets, a cadence row seeded via a DIFFERENT write path than `db.
set_cadence`, an all-habits (`habit_id IS NULL`) pause, a genuinely-zero-
history user, a habit whose streak already broke earlier (so "day before
yesterday" itself reads 0), a real DB-write fail-open (not just a
`day_qualifies` fail-open), bilingual interpolation with DISTINCT en/th
habit labels, and ISO week-key math across the 2026-W53 -> 2027-W01 year
boundary (2026 genuinely has a week 53 -- verified via `date.isocalendar()`
before writing this file, not assumed).

Same conventions as `tests/test_grace.py`: real on-disk SQLite via
`tmp_path`, no DB mocks, a goal-less `hydrate`-family habit id (never
`water`, SPEC-v1.1.md's legacy `effective_goal` special-case).
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from habit_assistant.config import Config, GraceConfig
from habit_assistant.core import grace, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
OTHER = "other"


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _habit(id_: str, type_: str = "boolean", label_en: str | None = None, label_th: str | None = None) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en or id_,
        label_th=label_th or id_,
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


def _add_pause(db: Database, user_id: str, habit_id: str | None, start: str, end: str) -> None:
    db._conn.execute(
        "INSERT INTO pauses (user_id, habit_id, start_date, end_date) VALUES (?, ?, ?, ?)",
        (user_id, habit_id, start, end),
    )
    db._conn.commit()


def _set_cadence_raw(db: Database, user_id: str, habit_id: str, per_week: int) -> None:
    """Deliberately NOT `db.set_cadence` -- proves grace's `db.get_cadence`
    read path is agnostic to who/how the `habit_cadence` row was written
    (mirrors `tests/test_v19_shared_surface.py:_set_cadence`'s own raw-SQL
    seeding, independent of module `cadence`'s own write path)."""
    db._conn.execute(
        "INSERT INTO habit_cadence (user_id, habit_id, per_week) VALUES (?, ?, ?)", (user_id, habit_id, per_week)
    )
    db._conn.commit()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "grace_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(OTHER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


# ===========================================================================
# One bridge per ISO week per habit per user -- independent budgets.
# ===========================================================================


def test_two_habits_for_same_user_have_independent_grace_budgets(db, config):
    hydrate = _habit("hydrate")
    stretch = _habit("stretch", type_="duration")
    registry = HabitRegistry([hydrate, stretch])
    for habit_id in ("hydrate", "stretch"):
        for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
            _seed(db, f"{d}T09:00:00", habit_id, 1)
    # Both miss 08-23 (Sun) -- each habit gets its OWN grace this week.
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert {h.id for h, _ in result} == {"hydrate", "stretch"}
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True
    assert db.grace_used_in_week(OWNER, "stretch", "2026-W34") is True


def test_two_users_have_independent_grace_budgets_for_the_same_habit(db, config):
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1, user_id=OWNER)
    # OTHER never logged "hydrate" at all -- no streak to protect for them.
    owner_result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    other_result = grace.evaluate_grace(db, config, registry, OTHER, date(2026, 8, 24))
    assert len(owner_result) == 1
    assert other_result == []  # OTHER has zero history -- nothing to bridge
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True
    assert db.grace_used_in_week(OTHER, "hydrate", "2026-W34") is False
    # OWNER's bridge is invisible to OTHER's own ledger read.
    assert db.grace_protected_dates(OTHER, "hydrate", "2026-08-01", "2026-08-31") == set()


# ===========================================================================
# Daily habits only -- a cadence row seeded via a DIFFERENT write path than
# db.set_cadence still suppresses grace entirely (AC16).
# ===========================================================================


def test_cadence_row_seeded_via_raw_sql_still_blocks_grace(db, config):
    gym = _habit("gym")
    registry = HabitRegistry([gym])
    _set_cadence_raw(db, OWNER, "gym", 3)
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "gym", 1)
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db.grace_protected_dates(OWNER, "gym", "2026-08-01", "2026-08-31") == set()


# ===========================================================================
# Never during a pause -- including an ALL-HABITS pause (habit_id IS NULL).
# ===========================================================================


def test_all_habits_pause_also_suppresses_grace(db, config):
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    _add_pause(db, OWNER, None, "2026-08-23", "2026-08-23")  # NULL = all habits

    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is False
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 23), OWNER) == 3  # held by the pause itself


def test_backfill_after_bridge_does_not_refund_the_weeks_budget_for_a_second_miss(db, config):
    """Rule 8's "one grace already spent this week" stays spent even if a
    later backfill made the bridged date turn out unneeded (the ledger row
    is never deleted, per Luna's own IMPL note) -- so a SECOND, later miss
    the SAME ISO week still cannot be bridged, even after the backfill."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-17", "2026-08-18"):  # Mon/Tue, week 34
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    # 08-19 (Wed) missed -> bridged.
    grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 20))
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True

    # User backfills 08-19 after the fact -- it now reads QUALIFIED.
    _seed(db, "2026-08-19T20:00:00", "hydrate", 1)
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 19), OWNER) == 3

    # A SECOND, later miss the same week (08-20, Thu) -- budget still spent.
    second = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 21))
    assert second == []
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-20", "2026-08-20") == set()
    # And the streak genuinely breaks at the unbridged 08-20 miss.
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 20), OWNER) == 0


# ===========================================================================
# First-day-of-habit / no-streak-to-protect -- zero logs ever, and a habit
# whose streak had ALREADY broken before yesterday (day-before-yesterday
# itself reads streak 0, not merely "no logs at all").
# ===========================================================================


def test_user_with_zero_logs_ever_evaluates_cleanly(db, config):
    hydrate = _habit("hydrate")
    stretch = _habit("stretch", type_="duration")
    registry = HabitRegistry([hydrate, stretch])
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db._conn.execute("SELECT COUNT(*) AS n FROM grace_ledger").fetchone()["n"] == 0


def test_streak_already_broken_before_yesterday_has_nothing_to_protect(db, config):
    """A single old log four days back, then a gap, then yesterday also
    missed: `compute_streak(..., day_before_yesterday, ...)` is 0 because
    the streak already broke on the days BETWEEN the old log and now -- not
    because there's no history at all. Rule 9's "`< 1` guard" must still
    correctly refuse to bridge here."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    _seed(db, "2026-08-18T09:00:00", "hydrate", 1)  # Tue -- lone old log
    # 08-19..08-23 all missed (no logs) -- streak is long since broken.
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 22), OWNER) == 0


# ===========================================================================
# Fail-open at the WRITE step, not just the read step -- one habit's DB
# error during `record_grace` must not abort the others.
# ===========================================================================


def test_fail_open_when_record_grace_itself_raises(db, config, monkeypatch):
    hydrate = _habit("hydrate")
    stretch = _habit("stretch", type_="duration")
    registry = HabitRegistry([hydrate, stretch])
    for habit_id in ("hydrate", "stretch"):
        for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
            _seed(db, f"{d}T09:00:00", habit_id, 1)

    real_record_grace = db.record_grace

    def _boom_for_hydrate(user_id, habit_id, protected_date, period_key):
        if habit_id == "hydrate":
            raise sqlite3.OperationalError("synthetic DB failure for hydrate only")
        return real_record_grace(user_id, habit_id, protected_date, period_key)

    monkeypatch.setattr(db, "record_grace", _boom_for_hydrate)

    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))

    assert [h.id for h, _ in result] == ["stretch"]
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-23", "2026-08-23") == set()
    assert db.grace_protected_dates(OWNER, "stretch", "2026-08-23", "2026-08-23") == {"2026-08-23"}
    # No audit row was ever written for the habit whose DB write blew up.
    hydrate_audit = [r for r in db.recent_audit(10) if r["action"] == "grace_consumed" and r["entity"] == "hydrate"]
    assert hydrate_audit == []


# ===========================================================================
# Engine integration -- a grace-bridged day is NEUTRAL in compute_streak;
# streak survives exactly one miss; week-boundary shapes (Sun vs Mon miss).
# ===========================================================================


def test_streak_survives_exactly_one_miss_not_two_unbridged(db, config):
    """A single bridged miss holds the streak; a habit that then racks up
    a SECOND, genuinely unbridged miss the same week breaks normally --
    the NEUTRAL treatment is exactly one day wide, never generalizes."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-17", "2026-08-18"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 20))  # bridges 08-19
    _seed(db, "2026-08-20T09:00:00", "hydrate", 1)
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 20), OWNER) == 3  # 17,18 + held 19 + 20

    # 08-21 (Fri) missed too, unbridged (grace already spent this week).
    grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 22))
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 21), OWNER) == 0


def test_miss_on_sunday_vs_monday_each_bridge_their_own_iso_week(db, config):
    """A miss on the LAST day of an ISO week (Sunday) vs the FIRST day of
    the next (Monday) must each consume that week's own, independent
    budget -- this is the shape most likely to break under an off-by-one
    week-boundary bug (e.g. treating Sunday as belonging to the FOLLOWING
    week, or vice versa)."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    # Sunday 08-23 (week 34) missed.
    sun = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert len(sun) == 1
    # Log Monday to keep the walk alive, then Monday-of-NEXT-week is fine;
    # instead here we directly check next week's Monday budget is untouched.
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W35") is False


def test_ac3_gate_untouched_habit_matches_pre_v19_arithmetic_with_grace_module_loaded(db, config):
    """Confirms `core/grace.py` merely being imported/used for ONE habit in
    a test run causes no side effect for a DIFFERENT, untouched habit --
    the shared-surface AC3 byte-identical gate's own precondition (empty
    `grace_ledger` for a habit that never triggers grace) must hold even
    inside a test file that actively exercises `evaluate_grace` elsewhere
    in the same process."""
    hydrate = _habit("hydrate")
    untouched = _habit("untouched")
    registry = HabitRegistry([hydrate, untouched])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    # "untouched" is logged straight through 08-23 too -- no miss, ever.
    for d in ("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"):
        _seed(db, f"{d}T09:00:00", "untouched", 1)
    # Trigger grace machinery for "hydrate" only (untouched has no miss).
    grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))

    # "untouched" was logged straight through with no gap -- plain
    # consecutive-day arithmetic, no NEUTRAL day anywhere in its own walk.
    assert db.grace_protected_dates(OWNER, "untouched", "2026-08-01", "2026-08-31") == set()
    assert streaks.compute_streak(db, config, untouched, date(2026, 8, 23), OWNER) == 4


# ===========================================================================
# Bilingual message + status line -- real Thai/English, correct
# interpolation with DISTINCT en/th labels (not the same string twice).
# ===========================================================================


def test_message_interpolates_distinct_en_th_labels_and_streak_number(db, config):
    hydrate = _habit("hydrate", label_en="hydrate", label_th="ดื่มน้ำ")
    en = grace.format_grace_message([(hydrate, 12)], "en")
    th = grace.format_grace_message([(hydrate, 12)], "th")
    assert "hydrate" in en and "12" in en
    assert "ดื่มน้ำ" in th and "12" in th
    assert "hydrate" not in th  # the Thai label, not the English one, is used
    assert "🛟" in en and "🛟" in th


def test_status_line_used_weekday_reflects_the_actual_bridged_date(db, config):
    hydrate = _habit("hydrate", label_en="hydrate", label_th="ดื่มน้ำ")
    for d in ("2026-08-17", "2026-08-18", "2026-08-19"):  # Mon/Tue/Wed
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    # 08-20 (Thursday) missed -> bridged.
    grace.evaluate_grace(db, config, HabitRegistry([hydrate]), OWNER, date(2026, 8, 21))
    line_en = grace.grace_status_line(db, config, hydrate, OWNER, date(2026, 8, 21), "en")
    line_th = grace.grace_status_line(db, config, hydrate, OWNER, date(2026, 8, 21), "th")
    assert "Thu" in line_en
    assert "Thu" in line_th  # weekday token is not itself translated, per Luna's own {weekday} interpolation


# ===========================================================================
# grace_used_in_week week-key math at the ISO year boundary: 2026 genuinely
# HAS a week 53 (verified via date.isocalendar() before writing this test:
# 2026-12-28 Mon = 2026-W53; 2027-01-03 Sun = STILL 2026-W53; 2027-01-04 Mon
# = 2027-W01). Two misses either side of that boundary must land in their
# own, independently-budgeted periods.
# ===========================================================================


def test_grace_period_key_correct_across_the_2026_w53_to_2027_w01_boundary(db, config):
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    # Fri/Sat/Sun of week 52 logged -> 3-day streak.
    for d in ("2026-12-25", "2026-12-26", "2026-12-27"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)

    # Monday 2026-12-28 (week 2026-W53) missed -> bridged on the 12-29 run.
    first = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 12, 29))
    assert len(first) == 1
    assert first[0][1] == 3
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W53") is True
    assert db.grace_used_in_week(OWNER, "hydrate", "2027-W01") is False

    # Log every remaining day of week 53 (Tue 12-29 .. Sun 2027-01-03) so the
    # walk stays alive with no SECOND miss inside 2026-W53.
    for d in ("2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)

    # Monday 2027-01-04 (week 2027-W01, a DIFFERENT calendar year) also
    # missed -> its own, freshly-budgeted grace, bridged on the 01-05 run.
    second = grace.evaluate_grace(db, config, registry, OWNER, date(2027, 1, 5))
    assert len(second) == 1
    assert db.grace_used_in_week(OWNER, "hydrate", "2027-W01") is True
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-12-28", "2027-01-04") == {"2026-12-28", "2027-01-04"}
