"""Vera (tester) supplemental coverage for module M3 (Review),
SPEC-v0.7.md §11, AC15/AC16. Written independently of Luna's
`tests/test_review.py` to probe behavior her own test file's assertions
don't directly exercise:

- soft-deleted rows excluded from `compute_weekly_stats` (the same
  scenario as the boundary failure `test_commands.py::
  test_soft_deleted_rows_excluded_from_weekly_review_stats`, which is
  M1-owned and currently broken on the old 3-arg call signature -- this
  file covers the identical behavior directly against M3's own
  `compute_weekly_stats(db, config, registry, end_date, user_id="owner")` so the scenario
  has *a* passing, signature-correct test while integration fixes the
  M1-owned boundary file).
- duration streak arithmetic across several hand-computed week patterns
  (not just the single seeded week Luna's file exercises), to confirm
  parity with the pre-v0.7 stretch-streak algorithm's semantics
  (consecutive days counting backward from end_date, break on first gap).
- boolean done-day counting across a full week with multiple logs on
  several different days (Luna's test only exercises one day with 2 logs).
- two non-built-in habit types (boolean + duration) configured alongside
  the three built-ins simultaneously, to confirm the built-in/generic
  branch dispatch in `format_stats_summary` composes cleanly for more
  than one generic habit at once.

Per the task's file-ownership rule (M3 = `core/review.py` +
`tests/test_review.py`, plus NEW `tests/test_v07_m3_*.py` files), this is
a NEW file and does not modify `tests/test_review.py`, `test_commands.py`,
or any other module's files.
"""

from __future__ import annotations

from datetime import date, timedelta

from habit_assistant.config import Config, HabitConfig, HabitLabel
from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.review import compute_weekly_stats, format_stats_summary
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


def _seed(db: Database, ts: str, category: str, value_num: float | None, raw: str = "x") -> int:
    entry = LogEntry(None, "owner", ts, category, value_num, None, raw, "reply")
    return db.insert_log(entry)


def _synthetic_habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_,
        label_th=id_,
        unit_en="u" if type_ in ("numeric", "duration") else None,
        unit_th="ห" if type_ in ("numeric", "duration") else None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


# ---------------------------------------------------------------------------
# Soft-deleted rows excluded (AC15 built-in math correctness; mirrors the
# M1-owned boundary test's scenario, but against M3's own signature).
# ---------------------------------------------------------------------------


def test_soft_deleted_rows_excluded_from_weekly_review_stats(tmp_path):
    db = Database(tmp_path / "habits.db")
    config = Config()
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)

    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    deleted_id = _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")
    stretch_id = _seed(db, "2026-08-19T11:00:00", "stretch", 10.0, raw="10 min")
    db.soft_delete(deleted_id)
    db.soft_delete(stretch_id)

    stats = compute_weekly_stats(db, config, registry, end_date, user_id="owner")

    assert stats.get("water").total == 500.0
    assert stats.get("stretch").total == 0
    assert stats.get("stretch").streak == 0
    db.close()


def test_soft_deleted_rows_excluded_for_a_generic_habit(tmp_path):
    """Same soft-delete exclusion, but for a non-built-in (config-added)
    habit going through the generic `sum_value`-based path, not the
    built-in water branch."""
    db = Database(tmp_path / "habits.db")
    sleep = _synthetic_habit("sleep", "numeric", goal=8, unit_en="h", unit_th="ชม.")
    registry = HabitRegistry([sleep])
    end_date = date(2026, 8, 19)

    kept_id = _seed(db, "2026-08-19T22:00:00", "sleep", 7.0)
    deleted_id = _seed(db, "2026-08-19T23:00:00", "sleep", 5.0)
    db.soft_delete(deleted_id)

    stats = compute_weekly_stats(db, config=Config(), registry=registry, end_date=end_date, user_id="owner")

    assert stats.get("sleep").total == 7.0
    assert kept_id != deleted_id
    db.close()


# ---------------------------------------------------------------------------
# Duration streak arithmetic -- several hand-computed week patterns beyond
# the single seeded week in tests/test_review.py.
# ---------------------------------------------------------------------------


def _insert_duration_days(db: Database, habit_id: str, end_date: date, active_offsets: set[int]) -> None:
    """`active_offsets` are day-offsets-before-end_date (0 = end_date
    itself) on which one session log is inserted."""
    for offset in active_offsets:
        d = end_date - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T11:00:00", habit_id, 10.0)


def test_duration_streak_all_seven_days_active(tmp_path):
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])
    end_date = date(2026, 8, 19)
    _insert_duration_days(db, "yoga", end_date, {0, 1, 2, 3, 4, 5, 6})

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("yoga").streak == 7
    assert stats.get("yoga").total == 7
    db.close()


def test_duration_streak_breaks_on_first_gap_from_end_date(tmp_path):
    """Active on end_date and end_date-1, but a gap at end_date-2: streak
    should stop at 2, even though end_date-3/-4 are also active (streak
    counts consecutively backward from end_date and stops at the FIRST
    gap, matching the pre-v0.7 stretch algorithm's `break` semantics)."""
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])
    end_date = date(2026, 8, 19)
    _insert_duration_days(db, "yoga", end_date, {0, 1, 3, 4})  # gap at offset 2

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("yoga").streak == 2
    assert stats.get("yoga").total == 4  # total session count unaffected by the gap
    db.close()


def test_duration_streak_zero_when_end_date_itself_has_no_session(tmp_path):
    """A session earlier in the week does not count if end_date itself is
    inactive -- the streak must count backward FROM end_date, not from
    the most recent active day."""
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])
    end_date = date(2026, 8, 19)
    _insert_duration_days(db, "yoga", end_date, {1, 2, 3})  # nothing at offset 0

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("yoga").streak == 0
    assert stats.get("yoga").total == 3
    db.close()


def test_duration_streak_multiple_sessions_same_day_counts_as_one_streak_day(tmp_path):
    """Two sessions logged on the same day must not double-count the
    streak (streak counts active DAYS, not session rows) -- covered via
    direct DB math since it's a one-day check."""
    db = Database(tmp_path / "habits.db")
    end_date = date(2026, 8, 19)
    _seed(db, f"{end_date.isoformat()}T09:00:00", "yoga", 10.0)
    _seed(db, f"{end_date.isoformat()}T18:00:00", "yoga", 15.0)
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("yoga").streak == 1
    assert stats.get("yoga").total == 2  # 2 sessions, but only 1 streak-day
    db.close()


# ---------------------------------------------------------------------------
# Boolean done-days across a full week with several distinct active days.
# ---------------------------------------------------------------------------


def test_boolean_done_days_across_week_with_multiple_logs_on_several_days(tmp_path):
    db = Database(tmp_path / "habits.db")
    meds = _synthetic_habit("meds", "boolean")
    registry = HabitRegistry([meds])
    end_date = date(2026, 8, 19)

    # Day 0 (end_date): 2 truthy logs -> 1 done day.
    _seed(db, f"{end_date.isoformat()}T09:00:00", "meds", 1.0)
    _seed(db, f"{end_date.isoformat()}T20:00:00", "meds", 1.0)
    # Day -1: 1 truthy + 1 falsy -> still 1 done day (any truthy log counts).
    d1 = end_date - timedelta(days=1)
    _seed(db, f"{d1.isoformat()}T09:00:00", "meds", 1.0)
    _seed(db, f"{d1.isoformat()}T20:00:00", "meds", 0.0)
    # Day -2: only falsy -> 0 done days.
    d2 = end_date - timedelta(days=2)
    _seed(db, f"{d2.isoformat()}T09:00:00", "meds", 0.0)
    # Day -3: 1 truthy -> 1 done day.
    d3 = end_date - timedelta(days=3)
    _seed(db, f"{d3.isoformat()}T09:00:00", "meds", 1.0)

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("meds").total == 3  # days 0, -1, -3 are done-days; -2 is not
    db.close()


# ---------------------------------------------------------------------------
# Multiple non-built-in habit types configured simultaneously alongside the
# three built-ins (AC16 "zero code change" at higher fan-out than a single
# added habit).
# ---------------------------------------------------------------------------


def test_two_generic_habits_alongside_all_three_builtins_render_independently(tmp_path):
    db = Database(tmp_path / "habits.db")
    config = Config()
    config.habits = [
        *config.habits,
        HabitConfig(
            id="meds",
            type="boolean",
            label=HabitLabel(en="meds", th="ยา"),
        ),
        HabitConfig(
            id="yoga",
            type="duration",
            unit=HabitLabel(en="min", th="นาที"),
            label=HabitLabel(en="yoga", th="โยคะ"),
        ),
    ]
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)

    _seed(db, f"{end_date.isoformat()}T09:00:00", "water", 500.0)
    _seed(db, f"{end_date.isoformat()}T11:00:00", "stretch", 10.0)
    _seed(db, f"{end_date.isoformat()}T21:30:00", "diary", None, raw="today was good")
    _seed(db, f"{end_date.isoformat()}T08:00:00", "meds", 1.0)
    _seed(db, f"{end_date.isoformat()}T17:00:00", "yoga", 20.0)

    stats = compute_weekly_stats(db, config, registry, end_date, user_id="owner")
    summary_en = format_stats_summary(stats, registry, "en")
    summary_th = format_stats_summary(stats, registry, "th")

    # Built-ins still hit their own catalog entries (byte-identical branch).
    assert i18n.t("stats_water_header", "en") in summary_en
    assert i18n.t("stats_stretch_summary", "en", stretch_total=1, stretch_streak=1) in summary_en
    assert i18n.t("stats_diary_summary", "en", diary_count=1) in summary_en

    # Both new habits render via the generic templates, independently.
    assert i18n.t("stats_generic_count_summary", "en", label="meds", count=1) in summary_en
    assert i18n.t("stats_generic_duration_summary", "en", label="yoga", total=1, streak=1) in summary_en
    assert i18n.t("stats_generic_count_summary", "th", label="ยา", count=1) in summary_th
    assert i18n.t("stats_generic_duration_summary", "th", label="โยคะ", total=1, streak=1) in summary_th
    db.close()
