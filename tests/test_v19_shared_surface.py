"""SPEC-v1.9.md "Life happens" (streak-engine rework) + Recap wrapped card
-- shared-surface tests, built ahead of the four parallel modules
(`cadence`, `grace`, `pause`, `wrapped`) that consume this surface
(SPEC-v1.9.md §11): migration 012 (AC1), the reworked `core/streaks.py`
engine's byte-identical gate (AC2/AC3, the release's own load-bearing
AC -- adversarial edge shapes: year boundary, empty logs, single-day
streak, deleted/undone rows), NEUTRAL classification for pause/grace
(AC4), config defaults (AC5), and the Thai font shared surface (AC6).

No mocks for the DB (real on-disk SQLite via tmp_path, mirroring
tests/test_v18_shared_surface.py's own convention). `habit_cadence`/
`grace_ledger`/`pauses` rows are seeded via raw SQL in this file -- their
own write accessors (`set_cadence`/`record_grace`/`insert_pause`, etc.)
are each owning module's (M1/M2/M3) own later, disjoint edit to
`storage/db.py`, not yet built at this shared-surface pass; only the
SHARED read accessors (`get_cadence`/`paused_dates`/
`grace_protected_dates`/`active_pauses`) exist here.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from habit_assistant.config import (
    DEFAULT_CONFIG_PATH,
    CadenceConfig,
    Config,
    GraceConfig,
    PauseConfig,
    WrappedConfig,
    load_config,
)
from habit_assistant.core import audit, audit_view, commands, targets
from habit_assistant.core import fonts as fonts_module
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.release_notes import RELEASE_NOTES, get_release_note
from habit_assistant.core import records, review, dashboard, streaks
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS
from habit_assistant.storage.models import LogEntry

OWNER = "owner"


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _synthetic_habit(id_: str, type_: str, **kw) -> Habit:
    """Mirrors tests/test_streaks.py's own helper verbatim -- each test
    file keeps its own copy per this codebase's convention. Deliberately
    NEVER "water" (SPEC-v1.1.md's own legacy special-case in `targets.
    effective_goal` forces `water`'s goal to 2500 regardless of `goal=`
    given here)."""
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


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def _set_cadence(db: Database, user_id: str, habit_id: str, per_week: int) -> None:
    db._conn.execute(
        "INSERT INTO habit_cadence (user_id, habit_id, per_week) VALUES (?, ?, ?)", (user_id, habit_id, per_week)
    )
    db._conn.commit()


def _add_pause(db: Database, user_id: str, habit_id: str | None, start: str, end: str) -> None:
    db._conn.execute(
        "INSERT INTO pauses (user_id, habit_id, start_date, end_date) VALUES (?, ?, ?, ?)",
        (user_id, habit_id, start, end),
    )
    db._conn.commit()


def _add_grace(db: Database, user_id: str, habit_id: str, protected_date: str, period_key: str = "2026-W01") -> None:
    db._conn.execute(
        "INSERT INTO grace_ledger (user_id, habit_id, protected_date, period_key) VALUES (?, ?, ?, ?)",
        (user_id, habit_id, protected_date, period_key),
    )
    db._conn.commit()


# ===========================================================================
# AC1 -- migration 012: habit_cadence/grace_ledger/pauses exist, idempotent
# re-run is a no-op, no existing table/column/row is touched.
# ===========================================================================


def test_migration_012_creates_all_three_tables_idempotently(tmp_path):
    db_ = Database(tmp_path / "mig012.db")
    assert db_.schema_version >= 12
    tables = {r[0] for r in db_._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "habit_cadence" in tables
    assert "grace_ledger" in tables
    assert "pauses" in tables
    assert db_.get_cadence("u1", "gym") is None
    assert db_.active_pauses("u1") == []
    db_.close()

    # Reopening (re-running migrations) applies nothing further.
    reopened = Database(tmp_path / "mig012.db")
    assert reopened.schema_version_before == reopened.schema_version
    reopened.close()


def test_migration_012_touches_no_existing_data(tmp_path):
    """Hand-build a v11-shaped DB (migrations 001-011 already applied)
    with a real user + log + routine row, then open it through the real
    `Database` (which runs every pending migration, including 012) --
    proves 012 is purely additive, mirrors tests/test_routines.py's
    identical migration-011 rehearsal."""
    db_path = tmp_path / "v11_copy.db"
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
        CREATE TABLE users (
          chat_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'member',
          status TEXT NOT NULL DEFAULT 'pending', display_name TEXT,
          language_pref TEXT NOT NULL DEFAULT 'auto', quiet_hours_json TEXT,
          snooze_default_minutes INTEGER, checkin_window TEXT NULL,
          last_announced_version TEXT NULL, dashboard_msg_id TEXT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE routines (
          user_id TEXT NOT NULL, name TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          PRIMARY KEY (user_id, name)
        );
        INSERT INTO users (chat_id, role, status) VALUES ('legacy-owner', 'owner', 'active');
        INSERT INTO logs (ts, category, value_num, raw_message, user_id)
          VALUES ('2026-01-01T09:00:00', 'water', 500.0, '500ml', 'legacy-owner');
        INSERT INTO routines (user_id, name) VALUES ('legacy-owner', 'morning');
        """
    )
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    conn.close()

    before = {
        "logs": [
            tuple(r)
            for r in sqlite3.connect(str(db_path)).execute(
                "SELECT id, ts, category, value_num, raw_message, user_id FROM logs ORDER BY id"
            )
        ],
        "users": [
            tuple(r) for r in sqlite3.connect(str(db_path)).execute("SELECT chat_id, role, status FROM users")
        ],
        "routines": [tuple(r) for r in sqlite3.connect(str(db_path)).execute("SELECT user_id, name FROM routines")],
    }

    db_ = Database(db_path)
    assert db_.schema_version_before == 11
    assert db_.schema_version == 13

    after = {
        "logs": [
            tuple(r)
            for r in db_._conn.execute("SELECT id, ts, category, value_num, raw_message, user_id FROM logs ORDER BY id")
        ],
        "users": [tuple(r) for r in db_._conn.execute("SELECT chat_id, role, status FROM users")],
        "routines": [tuple(r) for r in db_._conn.execute("SELECT user_id, name FROM routines")],
    }
    assert after == before

    tables = {r[0] for r in db_._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"habit_cadence", "grace_ledger", "pauses"} <= tables
    assert db_._conn.execute("SELECT COUNT(*) FROM habit_cadence").fetchone()[0] == 0
    assert db_._conn.execute("SELECT COUNT(*) FROM grace_ledger").fetchone()[0] == 0
    assert db_._conn.execute("SELECT COUNT(*) FROM pauses").fetchone()[0] == 0
    db_.close()


# ===========================================================================
# storage/db.py SHARED read accessors: get_cadence, paused_dates,
# grace_protected_dates, active_pauses.
# ===========================================================================


def test_get_cadence_none_when_no_row_and_the_value_when_one_exists(db):
    assert db.get_cadence(OWNER, "gym") is None
    _set_cadence(db, OWNER, "gym", 3)
    assert db.get_cadence(OWNER, "gym") == 3


def test_get_cadence_is_scoped_per_user_and_per_habit(db):
    _set_cadence(db, OWNER, "gym", 3)
    assert db.get_cadence("someone_else", "gym") is None
    assert db.get_cadence(OWNER, "yoga") is None


def test_paused_dates_empty_set_when_no_pause_rows(db):
    assert db.paused_dates(OWNER, "water", "2026-01-01", "2026-01-10") == set()


def test_paused_dates_expands_a_habit_scoped_pause_within_range(db):
    _add_pause(db, OWNER, "water", "2026-08-20", "2026-08-22")
    dates = db.paused_dates(OWNER, "water", "2026-08-18", "2026-08-25")
    assert dates == {"2026-08-20", "2026-08-21", "2026-08-22"}


def test_paused_dates_all_habits_null_row_applies_to_every_habit(db):
    _add_pause(db, OWNER, None, "2026-08-20", "2026-08-21")
    assert db.paused_dates(OWNER, "water", "2026-08-18", "2026-08-25") == {"2026-08-20", "2026-08-21"}
    assert db.paused_dates(OWNER, "gym", "2026-08-18", "2026-08-25") == {"2026-08-20", "2026-08-21"}


def test_paused_dates_a_different_habits_pause_row_does_not_leak(db):
    _add_pause(db, OWNER, "water", "2026-08-20", "2026-08-21")
    assert db.paused_dates(OWNER, "gym", "2026-08-18", "2026-08-25") == set()


def test_paused_dates_is_clamped_to_the_requested_range(db):
    _add_pause(db, OWNER, "water", "2026-08-01", "2026-08-31")
    assert db.paused_dates(OWNER, "water", "2026-08-20", "2026-08-22") == {"2026-08-20", "2026-08-21", "2026-08-22"}


def test_paused_dates_crosses_a_year_boundary_correctly(db):
    """ISO date strings sort lexicographically correctly across a year
    boundary (e.g. "2026-12-31" < "2027-01-01") -- confirms
    `paused_dates`' range-overlap SQL and date-expansion loop don't
    silently drop or misorder anything at the boundary."""
    _add_pause(db, OWNER, "water", "2026-12-30", "2027-01-02")
    dates = db.paused_dates(OWNER, "water", "2026-12-28", "2027-01-04")
    assert dates == {"2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"}


def test_paused_dates_scoped_per_user(db):
    _add_pause(db, OWNER, "water", "2026-08-20", "2026-08-21")
    assert db.paused_dates("someone_else", "water", "2026-08-18", "2026-08-25") == set()


def test_grace_protected_dates_empty_set_when_no_grace_rows(db):
    assert db.grace_protected_dates(OWNER, "water", "2026-01-01", "2026-01-10") == set()


def test_grace_protected_dates_returns_only_matching_habit_and_range(db):
    _add_grace(db, OWNER, "water", "2026-08-20")
    _add_grace(db, OWNER, "gym", "2026-08-21")  # different habit
    assert db.grace_protected_dates(OWNER, "water", "2026-08-18", "2026-08-25") == {"2026-08-20"}


def test_grace_protected_dates_scoped_per_user(db):
    _add_grace(db, OWNER, "water", "2026-08-20")
    assert db.grace_protected_dates("someone_else", "water", "2026-08-18", "2026-08-25") == set()


def test_active_pauses_returns_raw_rows_for_the_user_only(db):
    _add_pause(db, OWNER, "water", "2026-08-20", "2026-08-21")
    _add_pause(db, OWNER, None, "2026-09-01", "2026-09-05")
    rows = db.active_pauses(OWNER)
    assert len(rows) == 2
    assert {r["habit_id"] for r in rows} == {"water", None}
    assert db.active_pauses("someone_else") == []


# ===========================================================================
# AC2/AC3 -- the HARD byte-identical gate. Reference implementation below is
# a literal reimplementation of the pre-v1.9 `compute_streak`/`day_qualifies`
# (copied from this module's own pre-rework source, mirroring tests/
# test_streaks.py's own `_old_v09_duration_streak` precedent) -- pinning
# against a real independent reference, not merely re-deriving the new
# algorithm's own logic.
# ===========================================================================

_OLD_MAX_LOOKBACK_DAYS = 3650


def _old_day_qualifies(db: Database, config: Config, habit: Habit, day: str, user_id: str, goal: float | None) -> bool:
    if goal:
        return db.sum_value(user_id, habit.id, day) >= goal
    if habit.type == "boolean":
        return db.count_true(user_id, habit.id, day) > 0
    return db.count(user_id, habit.id, day) > 0


def _old_compute_streak(db: Database, config: Config, habit: Habit, end_date: date, user_id: str) -> int:
    goal = targets.effective_goal(db, habit, config, user_id)
    streak = 0
    day = end_date
    for _ in range(_OLD_MAX_LOOKBACK_DAYS):
        if not _old_day_qualifies(db, config, habit, day.isoformat(), user_id, goal):
            break
        streak += 1
        day -= timedelta(days=1)
    return streak


@pytest.mark.parametrize(
    ("type_", "goal", "seed_value"),
    [
        ("numeric", 1000, 1000.0),
        ("numeric", None, 5.0),
        ("duration", None, 10.0),
        ("boolean", None, 1.0),
        ("text", None, None),
    ],
    ids=["numeric-goal", "numeric-nogoal", "duration", "boolean", "text"],
)
def test_gate_empty_logs_reference_and_rework_agree(db, type_, goal, seed_value):
    """Adversarial edge shape: NO logs at all for this habit."""
    habit = _synthetic_habit("h", type_, goal=goal)
    config = Config()
    end_date = date(2026, 8, 19)
    assert streaks.compute_streak(db, config, habit, end_date, OWNER) == 0
    assert _old_compute_streak(db, config, habit, end_date, OWNER) == 0


@pytest.mark.parametrize(
    ("type_", "goal", "seed_value"),
    [
        ("numeric", 1000, 1000.0),
        ("boolean", None, 1.0),
        ("duration", None, 10.0),
    ],
)
def test_gate_single_day_streak_reference_and_rework_agree(db, type_, goal, seed_value):
    """Adversarial edge shape: a single qualifying day, nothing before or
    after it."""
    habit = _synthetic_habit("h", type_, goal=goal)
    config = Config()
    end_date = date(2026, 8, 19)
    _seed(db, f"{end_date.isoformat()}T09:00:00", "h", seed_value)

    expected = _old_compute_streak(db, config, habit, end_date, OWNER)
    actual = streaks.compute_streak(db, config, habit, end_date, OWNER)
    assert expected == 1
    assert actual == expected


def test_gate_streak_crossing_a_year_boundary_reference_and_rework_agree(db):
    """Adversarial edge shape: an unbroken run spanning Dec 30/31 into
    Jan 1/2 of the following year."""
    habit = _synthetic_habit("yoga", "duration")
    config = Config()
    for d_str in ("2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03"):
        _seed(db, f"{d_str}T09:00:00", "yoga", 10.0)
    end_date = date(2027, 1, 3)

    expected = _old_compute_streak(db, config, habit, end_date, OWNER)
    actual = streaks.compute_streak(db, config, habit, end_date, OWNER)
    assert expected == 7
    assert actual == expected


def test_gate_streak_with_a_gap_exactly_at_the_year_boundary_reference_and_rework_agree(db):
    """A gap ON the year-boundary day itself (Jan 1 missing) must still
    correctly reset the trailing run in both implementations."""
    habit = _synthetic_habit("yoga", "duration")
    config = Config()
    for d_str in ("2026-12-30", "2026-12-31"):  # gap at 2027-01-01
        _seed(db, f"{d_str}T09:00:00", "yoga", 10.0)
    for d_str in ("2027-01-02", "2027-01-03"):
        _seed(db, f"{d_str}T09:00:00", "yoga", 10.0)
    end_date = date(2027, 1, 3)

    expected = _old_compute_streak(db, config, habit, end_date, OWNER)
    actual = streaks.compute_streak(db, config, habit, end_date, OWNER)
    assert expected == 2
    assert actual == expected


def test_gate_deleted_undone_rows_are_excluded_in_both_implementations(db):
    """Adversarial edge shape: a soft-deleted (undone) row must not count
    toward qualification in either implementation -- proves the rework
    didn't accidentally start reading through `deleted_at`."""
    habit = _synthetic_habit("meds", "boolean")
    config = Config()
    _seed(db, "2026-08-17T09:00:00", "meds", 1.0)
    row_id = _seed(db, "2026-08-18T09:00:00", "meds", 1.0)
    _seed(db, "2026-08-19T09:00:00", "meds", 1.0)
    db.soft_delete(row_id)  # undo the middle day -> becomes a genuine gap
    end_date = date(2026, 8, 19)

    expected = _old_compute_streak(db, config, habit, end_date, OWNER)
    actual = streaks.compute_streak(db, config, habit, end_date, OWNER)
    assert expected == 1  # only 08-19 remains qualifying; 08-18 undone breaks the run back to 08-17
    assert actual == expected


def test_gate_multiple_habits_and_gaps_reference_and_rework_agree(db):
    """A broader randomized-shape sweep: several habits, several gap
    patterns, all compared against the reference implementation on the
    same seeded data."""
    config = Config()
    scenarios = [
        ("numeric", 1000, 1000.0, {0, 1, 2, 5, 6}),
        ("boolean", None, 1.0, {0, 1, 3, 4}),
        ("duration", None, 10.0, {0, 2, 3, 4, 5, 6, 7, 8, 9}),
        ("text", None, None, {1, 2}),
    ]
    end_date = date(2026, 8, 19)
    for idx, (type_, goal, seed_value, offsets) in enumerate(scenarios):
        habit = _synthetic_habit(f"h{idx}", type_, goal=goal)
        for offset in offsets:
            d = end_date - timedelta(days=offset)
            _seed(db, f"{d.isoformat()}T09:00:00", habit.id, seed_value)

        expected = _old_compute_streak(db, config, habit, end_date, OWNER)
        actual = streaks.compute_streak(db, config, habit, end_date, OWNER)
        assert actual == expected, f"mismatch for {habit.id}"


def test_gate_review_records_dashboard_call_sites_agree_across_a_year_boundary(db):
    """Ties AC3's gate directly to the three documented call sites
    (review.py:114, records.py:193, dashboard.py:237) for a year-boundary
    scenario -- with empty cadence/pause/grace stores, each must report
    the SAME streak number `compute_streak` itself does."""
    config = Config()
    registry = HabitRegistry([_synthetic_habit("yoga", "duration")])
    for d_str in ("2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"):
        _seed(db, f"{d_str}T09:00:00", "yoga", 10.0)
    end_date = date(2027, 1, 2)

    direct = streaks.compute_streak(db, config, registry.get("yoga"), end_date, OWNER)
    assert direct == 4

    review_streak = review.compute_weekly_stats(db, config, registry, end_date, OWNER).get("yoga").streak
    assert review_streak == direct

    records.update_on_log(db, config, registry, registry.get("yoga"), OWNER, clock=lambda: datetime(2027, 1, 2, 12, 0, 0))
    assert db.get_record(OWNER, "yoga", "longest_streak") == 4.0

    dashboard_text = dashboard.render(
        db, config, registry, "en", OWNER, clock=lambda: datetime(2027, 1, 2, 12, 0, 0)
    )
    assert "yoga" in dashboard_text


def test_streaks_module_stays_read_only_with_the_new_accessors_too(db):
    """Extends tests/test_streaks.py's own `_ReadOnlyGuardDatabase` proof
    (AC10.5) to the three new SHARED read accessors -- `classify_day`/
    `compute_streak`/`streak_unit` must never write, even now that they
    also consult `get_cadence`/`paused_dates`/`grace_protected_dates`."""

    class _ReadOnlyGuardDatabase(Database):
        def insert_log(self, *a, **k):
            raise AssertionError("must never write")

        def soft_delete(self, *a, **k):
            raise AssertionError("must never write")

    seed_db = db  # already seeded via the fixture's own real Database
    _seed(seed_db, "2026-08-19T09:00:00", "water", 2500.0)
    seed_db.close()

    # Re-derive the path the fixture used, mirroring test_streaks.py's own
    # "seed via a normal Database, then reopen guarded" pattern.
    guarded = _ReadOnlyGuardDatabase(seed_db.db_path)
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    today = date(2026, 8, 19)

    streaks.compute_streak(guarded, config, water, today, OWNER)
    streaks.streak_unit(guarded, water, OWNER)
    streaks.classify_day(guarded, config, water, today.isoformat(), OWNER, goal=2500, paused_dates=set(), grace_dates=set())
    guarded.close()  # reached only if nothing above raised


# ===========================================================================
# AC4 -- NEUTRAL classification: paused/grace-protected days are NEUTRAL
# (held, not broken); a MISSED day still breaks.
# ===========================================================================


def test_classify_day_qualified_neutral_missed(db):
    habit = _synthetic_habit("meds", "boolean")
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "meds", 1.0)

    assert streaks.classify_day(db, config, habit, "2026-08-19", OWNER, goal=None, paused_dates=set(), grace_dates=set()) == "qualified"
    assert streaks.classify_day(db, config, habit, "2026-08-20", OWNER, goal=None, paused_dates={"2026-08-20"}, grace_dates=set()) == "neutral"
    assert streaks.classify_day(db, config, habit, "2026-08-21", OWNER, goal=None, paused_dates=set(), grace_dates={"2026-08-21"}) == "neutral"
    assert streaks.classify_day(db, config, habit, "2026-08-22", OWNER, goal=None, paused_dates=set(), grace_dates=set()) == "missed"


def test_classify_day_a_real_entry_beats_the_neutral_default(db):
    """Rule 16: a voluntary/qualifying log on a paused (or grace-protected)
    day still counts as QUALIFIED, never NEUTRAL -- "a real entry beats
    the neutral default"."""
    habit = _synthetic_habit("meds", "boolean")
    config = Config()
    _seed(db, "2026-08-20T09:00:00", "meds", 1.0)  # a genuine log on the paused day

    assert streaks.classify_day(db, config, habit, "2026-08-20", OWNER, goal=None, paused_dates={"2026-08-20"}, grace_dates=set()) == "qualified"


def test_daily_walk_paused_gap_is_held_not_broken(db):
    habit = _synthetic_habit("meds", "boolean")
    config = Config()
    for d_str in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d_str}T09:00:00", "meds", 1.0)
    _add_pause(db, OWNER, "meds", "2026-08-20", "2026-08-20")

    streak = streaks.compute_streak(db, config, habit, date(2026, 8, 22), OWNER)
    assert streak == 5  # 5 qualifying days bridged across the one held/neutral gap


def test_daily_walk_grace_protected_gap_is_held_not_broken(db):
    habit = _synthetic_habit("meds", "boolean")
    config = Config()
    for d_str in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d_str}T09:00:00", "meds", 1.0)
    _add_grace(db, OWNER, "meds", "2026-08-20", "2026-W34")

    streak = streaks.compute_streak(db, config, habit, date(2026, 8, 22), OWNER)
    assert streak == 5


def test_daily_walk_a_genuine_gap_with_no_pause_or_grace_still_breaks(db):
    habit = _synthetic_habit("meds", "boolean")
    config = Config()
    for d_str in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d_str}T09:00:00", "meds", 1.0)
    # No pause, no grace for 2026-08-20.

    streak = streaks.compute_streak(db, config, habit, date(2026, 8, 22), OWNER)
    assert streak == 2  # only 08-21/08-22 -- the real gap ends the walk


def test_all_habits_pause_neutralizes_every_habit(db):
    meds = _synthetic_habit("meds", "boolean")
    water = _synthetic_habit("water2", "boolean")
    config = Config()
    for habit in (meds, water):
        for d_str in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-21", "2026-08-22"):
            _seed(db, f"{d_str}T09:00:00", habit.id, 1.0)
    _add_pause(db, OWNER, None, "2026-08-20", "2026-08-20")  # all-habits pause

    assert streaks.compute_streak(db, config, meds, date(2026, 8, 22), OWNER) == 5
    assert streaks.compute_streak(db, config, water, date(2026, 8, 22), OWNER) == 5


# ===========================================================================
# Weekly-cadence walk (Rules 1, 4, 5, 6, 7) -- direct engine coverage ahead
# of module `cadence`'s own AC7-AC12 (not this shared surface's ACs, but
# the engine mechanism itself must be trustworthy before four modules
# build on it).
# ===========================================================================


def test_streak_unit_is_day_without_cadence_and_week_with_it(db):
    habit = _synthetic_habit("gym", "boolean")
    assert streaks.streak_unit(db, habit, OWNER) == "day"
    _set_cadence(db, OWNER, "gym", 3)
    assert streaks.streak_unit(db, habit, OWNER) == "week"


def test_weekly_walk_rest_days_never_break_the_streak(db):
    """Rule 7: a 3x/week habit logged Mon/Wed/Fri, empty Tue/Thu/Sat/Sun,
    has a MET current week and an unbroken weekly streak."""
    gym = _synthetic_habit("gym", "boolean")
    config = Config()
    _set_cadence(db, OWNER, "gym", 3)
    # 2026-08-24 is a Monday.
    for d_str in ("2026-08-24", "2026-08-26", "2026-08-28"):  # Mon/Wed/Fri
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)

    # Sunday of the same week: still MET (3 of 3 already logged), rest
    # days (Tue/Thu/Sat/Sun) never touched.
    assert streaks.compute_streak(db, config, gym, date(2026, 8, 30), OWNER) == 1


def test_weekly_walk_accumulates_across_consecutive_met_weeks(db):
    gym = _synthetic_habit("gym", "boolean")
    config = Config()
    _set_cadence(db, OWNER, "gym", 3)
    for d_str in ("2026-08-17", "2026-08-19", "2026-08-21"):  # week 1 MET
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)
    for d_str in ("2026-08-24", "2026-08-26", "2026-08-28"):  # week 2 MET
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)

    assert streaks.compute_streak(db, config, gym, date(2026, 8, 28), OWNER) == 2


def test_weekly_walk_current_week_not_yet_met_is_not_over_reported(db):
    """Rule 4's own "never over-reported mid-week": a current partial
    week with fewer than N qualifying days so far contributes 0, even
    though it hasn't technically "failed" yet."""
    gym = _synthetic_habit("gym", "boolean")
    config = Config()
    _set_cadence(db, OWNER, "gym", 3)
    for d_str in ("2026-08-17", "2026-08-19", "2026-08-21"):  # prior week MET
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-24T09:00:00", "gym", 1.0)  # this week: only 1 of 3 so far (Monday)

    assert streaks.compute_streak(db, config, gym, date(2026, 8, 24), OWNER) == 1  # prior week only


def test_weekly_walk_a_fully_paused_week_is_neutral_held_not_broken(db):
    """Rule 4's "NEUTRAL if paused enough that fewer than N non-paused
    days remain" -- a week entirely covered by an all-habits pause can
    never reach N, so it's held, not a failure."""
    gym = _synthetic_habit("gym", "boolean")
    config = Config()
    _set_cadence(db, OWNER, "gym", 3)
    for d_str in ("2026-08-17", "2026-08-19", "2026-08-21"):  # week 1 MET
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)
    _add_pause(db, OWNER, None, "2026-08-24", "2026-08-30")  # week 2 fully paused
    for d_str in ("2026-08-31", "2026-09-02", "2026-09-04"):  # week 3 MET
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)

    # Current (partial) week is MET (3 of 3 by Friday) -> +1; the fully-
    # paused week before it is NEUTRAL (held, +0); the week before THAT
    # was also MET -> +1. Total 2 -- the held week bridges the two MET
    # weeks without itself contributing to the count (mirrors the daily
    # walk's own "neutral doesn't increment" rule).
    assert streaks.compute_streak(db, config, gym, date(2026, 9, 4), OWNER) == 2


def test_weekly_walk_a_genuinely_missed_week_breaks_it(db):
    gym = _synthetic_habit("gym", "boolean")
    config = Config()
    _set_cadence(db, OWNER, "gym", 3)
    for d_str in ("2026-08-17", "2026-08-19", "2026-08-21"):  # week 1 MET (would be lost by the break below)
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-24T09:00:00", "gym", 1.0)  # week 2: only 1 of 3, no pause -- genuinely missed
    for d_str in ("2026-08-31", "2026-09-02", "2026-09-04"):  # week 3 MET
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)

    assert streaks.compute_streak(db, config, gym, date(2026, 9, 4), OWNER) == 1  # only the current week; week 2 breaks the walk


def test_grace_never_applies_to_a_cadence_habit(db):
    """Rule 6: even if a `grace_ledger` row somehow exists for a cadence
    habit (shouldn't happen by construction, R9), the weekly walk must
    never consult it -- avoids double tolerance."""
    gym = _synthetic_habit("gym", "boolean")
    config = Config()
    _set_cadence(db, OWNER, "gym", 3)
    for d_str in ("2026-08-24", "2026-08-26"):  # only 2 of 3 this week
        _seed(db, f"{d_str}T09:00:00", "gym", 1.0)
    _add_grace(db, OWNER, "gym", "2026-08-28", "2026-W35")  # a stray grace row (shouldn't exist for a cadence habit)

    # Still only 1 MET week is never reached this week (2 of 3, no pause) --
    # the stray grace row changes nothing.
    assert streaks.compute_streak(db, config, gym, date(2026, 8, 30), OWNER) == 0


# ===========================================================================
# AC5 -- config defaults ([cadence]/[grace]/[pause]/[wrapped]).
# ===========================================================================


def test_config_defaults_match_spec_5():
    config = Config()
    assert config.cadence == CadenceConfig(max_per_week=7)
    assert config.grace == GraceConfig(enabled=True)
    assert config.pause == PauseConfig(max_days=30)
    assert config.wrapped == WrappedConfig(auto_send=False, celebrate_burst=True)


def test_config_toml_absent_sections_use_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[app]\ntimezone = "Asia/Bangkok"\n', encoding="utf-8")
    config = load_config(path)
    assert config.cadence.max_per_week == 7
    assert config.grace.enabled is True
    assert config.pause.max_days == 30
    assert config.wrapped.auto_send is False
    assert config.wrapped.celebrate_burst is True


def test_config_toml_sections_are_overridable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[cadence]\nmax_per_week = 5\n"
        "[grace]\nenabled = false\n"
        "[pause]\nmax_days = 14\n"
        "[wrapped]\nauto_send = true\ncelebrate_burst = false\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.cadence.max_per_week == 5
    assert config.grace.enabled is False
    assert config.pause.max_days == 14
    assert config.wrapped.auto_send is True
    assert config.wrapped.celebrate_burst is False


def test_repo_config_toml_loads_cleanly_with_v19_sections():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config.cadence.max_per_week == 7
    assert config.grace.enabled is True
    assert config.pause.max_days == 30
    assert config.wrapped.auto_send is False
    assert config.wrapped.celebrate_burst is True


@pytest.mark.parametrize("value", [0, 8, -1])
def test_cadence_max_per_week_must_be_1_to_7(value):
    with pytest.raises(Exception):
        CadenceConfig(max_per_week=value)


def test_pause_max_days_must_be_positive():
    with pytest.raises(Exception):
        PauseConfig(max_days=0)


# ===========================================================================
# AC6 -- Thai font shared surface. Empirically proves the byte-identical
# claim (not merely asserted): the SAME chart rendered before/after
# `register_thai_font()` produces identical PNG bytes for non-Thai text.
# ===========================================================================


@pytest.fixture(autouse=True)
def _restore_font_registration_state():
    """Every font test below manipulates module-global registration state
    (`fonts_module._registered`) and/or `matplotlib.rcParams` -- restore
    both afterward so this file's own tests can't leak state into any
    other test file that imports `core/charts.py`/`core/heatmap.py`
    (whose own import-time `register_thai_font()` call must keep working
    identically for every OTHER test module in the suite)."""
    import matplotlib

    saved_family = list(matplotlib.rcParams["font.family"])
    saved_registered = fonts_module._registered
    yield
    matplotlib.rcParams["font.family"] = saved_family
    fonts_module._registered = saved_registered


def test_register_thai_font_is_idempotent(monkeypatch):
    monkeypatch.setattr(fonts_module, "_registered", False)
    fonts_module.register_thai_font()
    assert fonts_module._registered is True
    family_after_first = list(__import__("matplotlib").rcParams["font.family"])

    fonts_module.register_thai_font()  # second call: no-op
    assert list(__import__("matplotlib").rcParams["font.family"]) == family_after_first


def test_register_thai_font_sets_dejavu_primary_noto_fallback(monkeypatch):
    monkeypatch.setattr(fonts_module, "_registered", False)
    fonts_module.register_thai_font()
    import matplotlib

    assert matplotlib.rcParams["font.family"] == ["DejaVu Sans", "Noto Sans Thai"]


def test_register_thai_font_adds_noto_to_the_font_manager(monkeypatch):
    monkeypatch.setattr(fonts_module, "_registered", False)
    fonts_module.register_thai_font()
    from matplotlib import font_manager

    names = {f.name for f in font_manager.fontManager.ttflist}
    assert "Noto Sans Thai" in names


def test_register_thai_font_missing_file_never_raises_and_leaves_state_unregistered(monkeypatch, caplog):
    monkeypatch.setattr(fonts_module, "_registered", False)
    monkeypatch.setattr(fonts_module, "_warned_missing", False)
    monkeypatch.setattr(fonts_module, "FONT_PATH", fonts_module.FONT_PATH.parent / "does-not-exist.ttf")

    fonts_module.register_thai_font()  # must not raise

    assert fonts_module._registered is False


def test_font_registration_does_not_change_non_thai_chart_bytes(monkeypatch):
    """AC6's own core claim, verified empirically: reset to matplotlib's
    stock defaults (simulating pre-v1.9, unregistered), render a chart,
    then register the Thai font and render the SAME chart again -- the
    PNG bytes must be identical for non-Thai text."""
    import matplotlib

    from habit_assistant.core.charts import _render_bar_chart

    matplotlib.rcParams["font.family"] = list(matplotlib.rcParamsDefault["font.family"])
    monkeypatch.setattr(fonts_module, "_registered", False)

    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    values = [500.0, 1000.0, 1500.0, 2000.0, 2500.0, 1800.0, 2200.0]
    before = _render_bar_chart(labels, values, "water", "ml", 2500)

    fonts_module.register_thai_font()
    after = _render_bar_chart(labels, values, "water", "ml", 2500)

    assert before[:8] == b"\x89PNG\r\n\x1a\n"
    assert before == after


def test_thai_title_renders_without_raising_and_produces_real_png_bytes(monkeypatch):
    from habit_assistant.core.charts import _render_bar_chart

    monkeypatch.setattr(fonts_module, "_registered", False)
    fonts_module.register_thai_font()

    png = _render_bar_chart(["จ", "อ", "พ", "พฤ", "ศ", "ส", "อา"], [1, 2, 3, 4, 5, 6, 7], "น้ำ", "มล.", 2500)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 500


# ===========================================================================
# Audit vocab (5 new actions) + audit_view labels.
# ===========================================================================


def test_audit_actions_include_the_five_new_v19_actions():
    for action in ("cadence_set", "cadence_clear", "pause_set", "pause_clear", "grace_consumed"):
        assert action in audit.ACTIONS


def test_audit_view_has_localized_labels_for_the_five_new_actions(db):
    config = Config()
    for action in ("cadence_set", "cadence_clear", "pause_set", "pause_clear", "grace_consumed"):
        audit.record(db, actor=OWNER, action=action, source="command", entity="gym")
        en_reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
        th_reply = audit_view.render_recent(db, config, "th", limit=None, owner_chat_id=OWNER)
        en_line = en_reply.splitlines()[1]
        th_line = th_reply.splitlines()[1]
        assert "_" not in en_line.split(" · ")[2]
        assert action not in th_line


# ===========================================================================
# Release notes 1.9.0.
# ===========================================================================


def test_release_notes_1_9_0_exists_in_both_languages():
    assert "1.9.0" in RELEASE_NOTES
    assert get_release_note("1.9.0", "en")
    assert get_release_note("1.9.0", "th")


def test_release_notes_1_9_0_mentions_every_shipped_feature():
    en = get_release_note("1.9.0", "en").lower()
    assert "cadence" in en
    assert "grace" in en
    assert "pause" in en
    assert "wrapped" in en or "recap" in en
    assert "thai" in en


# ===========================================================================
# CommandKind + reserved words (SPEC-v1.9.md §5 skeleton).
# ===========================================================================


@pytest.mark.parametrize("word", ["cadence", "ต่อสัปดาห์", "กี่ครั้งต่อสัปดาห์", "pause", "พัก", "หยุดพัก", "resume", "กลับมา", "ต่อ", "wrapped", "recap", "สรุปเดือน", "การ์ดสรุป"])
def test_reserved_trigger_words_contains_the_thirteen_v19_literals(word):
    assert word in commands.reserved_trigger_words()


def test_v19_kinds_present_in_command_kind_literal():
    for kind in ("cadence", "pause", "resume", "wrapped"):
        cmd = commands.Command(kind=kind)
        assert cmd.kind == kind


@pytest.mark.parametrize("word", ["cadence", "pause", "resume", "wrapped", "recap"])
def test_commandkind_reserved_words_do_not_yet_dispatch(word):
    base_registry = HabitRegistry.from_config(Config())
    assert commands.dispatch(word, base_registry) is None
