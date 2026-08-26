"""SPEC-v1.9.md §4 Rules 8-11 (module `grace`, M2) -- Luna's own test suite
for `core/grace.py`. Owned ACs (SPEC-v1.9.md §11): AC13 (bridges a genuine
single miss with an active streak), AC14 (kind message sent once, never
repeated), AC15 (a second miss the same ISO week is NOT bridged), AC16
(never fires for a cadence habit), AC17 (`/habits` balance line +
`[grace] enabled=false` byte-identical to a graceless world), AC18 (exactly
one bilingual audit row per `(user, habit, date)`).

Conventions mirror `tests/test_v19_shared_surface.py` (real on-disk SQLite
via `tmp_path`, no DB mocks; `pauses`/`habit_cadence` rows seeded via raw
SQL or the now-landed `db.set_cadence` where available -- `insert_pause`
(module `pause`, M3) has not landed yet at the time this file was written,
so a paused date is seeded via the same raw-SQL `_add_pause` helper the
shared-surface tests already established) and `tests/test_nudge.py`
(`FakeChannel`/`_habit` shape, even though `core/grace.py` never imports a
channel itself -- `evaluate_grace` only writes the ledger + audit and
returns what WOULD be sent; `main.py`'s integration step is what actually
sends `format_grace_message`'s result, per SPEC-v1.9.md §6, out of this
module's scope).

Anchor dates (verified via `date.isocalendar()`, see this file's own
docstring comment on each test that relies on it): 2026-08-17 is a Monday,
ISO week 34; 2026-08-23 is the following Sunday, still week 34; 2026-08-24
is the next Monday, ISO week 35 -- the Sun/Mon ISO-week-boundary pair this
suite deliberately exercises."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from habit_assistant.config import Config, GraceConfig
from habit_assistant.core import audit, audit_view, grace, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _habit(id_: str, type_: str = "boolean") -> Habit:
    """A goal-less habit so `day_qualifies` reduces to a plain any-entry
    check (`count_true`/`count`), keeping the streak arithmetic in every
    test below simple and legible -- mirrors `tests/test_streaks.py`'s/
    `tests/test_v19_shared_surface.py`'s own `_synthetic_habit` helper,
    trimmed to just the shapes this file needs."""
    return Habit(
        id=id_,
        type=type_,
        label_en=id_,
        label_th=id_,
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


def _add_pause(db: Database, user_id: str, habit_id: str | None, start: str, end: str) -> None:
    """Mirrors `tests/test_v19_shared_surface.py:_add_pause` exactly --
    `core/pause.py:insert_pause` (M3) had not landed at the time this file
    was written, so a paused date is seeded directly against the shared
    `pauses` table this suite's own `grace.py` never writes to."""
    db._conn.execute(
        "INSERT INTO pauses (user_id, habit_id, start_date, end_date) VALUES (?, ?, ?, ?)",
        (user_id, habit_id, start, end),
    )
    db._conn.commit()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "grace.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


# ===========================================================================
# AC13 -- a genuine single miss with an active streak gets bridged; "no
# streak to protect" (a miss with zero prior qualifying history) does not.
# ===========================================================================


def test_bridges_a_single_miss_and_the_held_streak_reads_as_preserved(db, config):
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])

    # 2026-08-20/21/22 (Thu/Fri/Sat) logged -> a 3-day streak ending 08-22.
    # 08-23 (Sun) is a genuine miss -- no log, no pause, no prior grace.
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)

    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))

    assert len(result) == 1
    bridged_habit, protected_streak = result[0]
    assert bridged_habit.id == "hydrate"
    assert protected_streak == 3  # the streak that existed right up to the miss

    # The ledger row + audit row both exist for exactly the miss date.
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-23", "2026-08-23") == {"2026-08-23"}
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True

    # "the streak reads as preserved everywhere afterward" (AC13): the day
    # itself is HELD (neutral), not incrementing but not resetting either --
    # compute_streak ending exactly on the bridged date still reads 3.
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 23), OWNER) == 3

    # ...and the walk continues seamlessly PAST the held gap once the user
    # logs again: 08-24 qualifies -> 4, not a reset to 1.
    _seed(db, "2026-08-24T09:00:00", "hydrate", 1)
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 24), OWNER) == 4


def test_no_prior_streak_means_nothing_to_protect(db, config):
    """A miss on a habit with zero qualifying history before it (e.g. the
    very first day a habit exists) has no streak to protect -- Rule 9's
    own "active streak >= 1 ending the day before yesterday" precondition
    naturally excludes it, no habit-age special-casing needed."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    # No logs at all -- 08-23 is technically "missed", but there is nothing
    # behind it to break.
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-01", "2026-08-31") == set()


def test_yesterday_already_qualified_is_not_a_miss_at_all(db, config):
    """A day the user DID log for isn't a "miss" in the first place --
    nothing to bridge, and calling evaluate_grace must not treat an
    already-qualifying yesterday as anything special."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []


# ===========================================================================
# AC14/AC15 -- sent exactly once, never repeated; a SECOND miss the same
# ISO week is not bridged and the streak breaks normally.
# ===========================================================================


def test_second_miss_same_week_not_bridged_streak_breaks_normally(db, config):
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])

    # 08-17/18 (Mon/Tue, week 34) logged -> streak 2. 08-19 (Wed) missed.
    _seed(db, "2026-08-17T09:00:00", "hydrate", 1)
    _seed(db, "2026-08-18T09:00:00", "hydrate", 1)

    first = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 20))
    assert len(first) == 1
    assert first[0][1] == 2  # protected the 2-day streak
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True

    # User logs 08-20 (Thu) -- the streak continues seamlessly across the
    # held 08-19 gap: 08-20 + 08-18 + 08-17 = 3 (not a reset to 1).
    _seed(db, "2026-08-20T09:00:00", "hydrate", 1)
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 20), OWNER) == 3

    # A SECOND miss, still ISO week 34: 08-21 (Fri) has no log.
    second = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 22))
    assert second == []  # AC15: not bridged -- grace already spent this week
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-21", "2026-08-21") == set()

    # The streak genuinely breaks at the unbridged 08-21 miss.
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 21), OWNER) == 0


def test_re_running_evaluate_grace_for_the_same_night_is_idempotent(db, config):
    """AC14: "sent exactly once ... never repeated" -- a second call for
    the SAME `today` (e.g. a process restart between 00:05 and the next
    day) must not bridge the same date twice or write a second audit row."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)

    first = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert len(first) == 1

    second = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert second == []  # already bridged -- nothing new to report/send

    ledger_rows = db._conn.execute(
        "SELECT COUNT(*) AS n FROM grace_ledger WHERE user_id = ? AND habit_id = ?", (OWNER, "hydrate")
    ).fetchone()["n"]
    assert ledger_rows == 1

    audit_rows = db._conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE user_id = ? AND action = 'grace_consumed'", (OWNER,)
    ).fetchone()["n"]
    assert audit_rows == 1


# ===========================================================================
# AC16 -- grace never fires for a cadence habit.
# ===========================================================================


def test_cadence_habit_never_bridged(db, config):
    gym = _habit("gym")
    registry = HabitRegistry([gym])
    db.set_cadence(OWNER, "gym", 3)

    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "gym", 1)
    # 08-23 missed -- would ordinarily protect a 3-day streak if this were
    # a daily habit, but "gym" has a cadence row.
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db.grace_protected_dates(OWNER, "gym", "2026-08-01", "2026-08-31") == set()


# ===========================================================================
# AC17 -- `[grace] enabled=false` disables the entire mechanism (no
# bridging, no writes); the `/habits` balance line reflects both states,
# and itself goes silent (not "always available") when disabled.
# ===========================================================================


def test_disabled_config_bridges_nothing_writes_nothing(db):
    disabled = Config(grace=GraceConfig(enabled=False))
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)

    result = grace.evaluate_grace(db, disabled, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db._conn.execute("SELECT COUNT(*) AS n FROM grace_ledger").fetchone()["n"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'grace_consumed'"
    ).fetchone()["n"] == 0


def test_grace_status_line_available_then_used(db, config):
    hydrate = _habit("hydrate")
    for lang in ("en", "th"):
        assert grace.grace_status_line(db, config, hydrate, OWNER, date(2026, 8, 20), lang) == (
            "🛟 grace: available this week" if lang == "en" else "🛟 สิทธิ์ผ่อนผัน: ยังใช้ได้สัปดาห์นี้"
        )

    # 08-19 (Wednesday, ISO week 34) gets bridged.
    _seed(db, "2026-08-17T09:00:00", "hydrate", 1)
    _seed(db, "2026-08-18T09:00:00", "hydrate", 1)
    grace.evaluate_grace(db, config, HabitRegistry([hydrate]), OWNER, date(2026, 8, 20))

    line_en = grace.grace_status_line(db, config, hydrate, OWNER, date(2026, 8, 21), "en")
    assert line_en == "🛟 grace: used Wed (streak protected)"
    line_th = grace.grace_status_line(db, config, hydrate, OWNER, date(2026, 8, 21), "th")
    assert "ใช้ไปแล้วเมื่อ Wed" in line_th

    # A DIFFERENT ISO week (e.g. the following Monday) has its own fresh
    # balance -- "used" is scoped per-week, not forever.
    assert grace.grace_status_line(db, config, hydrate, OWNER, date(2026, 8, 24), "en") == (
        "🛟 grace: available this week"
    )


def test_grace_status_line_empty_when_disabled(db):
    disabled = Config(grace=GraceConfig(enabled=False))
    hydrate = _habit("hydrate")
    assert grace.grace_status_line(db, disabled, hydrate, OWNER, date(2026, 8, 20), "en") == ""
    assert grace.grace_status_line(db, disabled, hydrate, OWNER, date(2026, 8, 20), "th") == ""


# ===========================================================================
# AC18 -- exactly one audit `grace_consumed` row per (user, habit, date),
# bilingual via /audit.
# ===========================================================================


def test_audit_row_recorded_once_and_renders_bilingually(db, config):
    hydrate = _habit("hydrate")
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    grace.evaluate_grace(
        db, config, HabitRegistry([hydrate]), OWNER, date(2026, 8, 24), clock=lambda: datetime(2026, 8, 24, 0, 5, 0)
    )

    rows = db.recent_audit(10)
    grace_rows = [r for r in rows if r["action"] == "grace_consumed"]
    assert len(grace_rows) == 1
    row = grace_rows[0]
    assert row["user_id"] == OWNER
    assert row["entity"] == "hydrate"
    # Integration ruling (Archi, v1.9 integration pass): grace_consumed now
    # records source="system" (a dedicated SOURCES value added at
    # integration) instead of Luna's own "admin" placeholder.
    assert row["source"] == "system"
    assert row["ts"] == "2026-08-24T00:05:00"

    en = audit_view.render_recent(db, config, "en", limit=5, owner_chat_id=OWNER)
    th = audit_view.render_recent(db, config, "th", limit=5, owner_chat_id=OWNER)
    assert "grace used" in en
    assert "ใช้สิทธิ์ผ่อนผัน" in th


# ===========================================================================
# Adversarial: ISO week Sunday/Monday boundary, pause interaction, a
# backfilled log beating the neutral default, and fail-open per-habit.
# ===========================================================================


def test_week_boundary_sunday_then_monday_each_get_their_own_grace(db, config):
    """2026-08-23 is a Sunday (still ISO week 34); 2026-08-24 is the
    following Monday (ISO week 35). Two CONSECUTIVE calendar-day misses
    straddling that boundary each fall in their OWN ISO week, so each may
    independently be bridged -- `period_key` is derived from the missed
    date's own `isocalendar()`, not a naive 7-day counter from habit
    creation, which is exactly what this test would catch if it were
    wrong (an off-by-one week grouping would either double-bridge inside
    one week, violating AC15, or wrongly refuse the second, distinct-week
    miss)."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)

    # 08-23 (Sun, week 34) missed -> bridged on the 08-24 run.
    first = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert len(first) == 1
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W35") is False

    # 08-24 (Mon, week 35) ALSO missed (still no log that day) -> a
    # DIFFERENT ISO week's own grace, bridged on the 08-25 run.
    second = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 25))
    assert len(second) == 1
    assert second[0][1] == 3  # the same held-at-3 streak, still protected
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W35") is True
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-23", "2026-08-24") == {"2026-08-23", "2026-08-24"}


def test_paused_yesterday_is_never_treated_as_a_miss(db, config):
    """"Grace never fires during a pause": a paused date is NEUTRAL, not
    MISSED (Rule 2) -- `evaluate_grace` must not spend the week's one
    grace on a day that was already held for free by the pause."""
    hydrate = _habit("hydrate")
    registry = HabitRegistry([hydrate])
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    _add_pause(db, OWNER, "hydrate", "2026-08-23", "2026-08-23")

    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-23", "2026-08-23") == set()
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is False

    # The pause itself still holds the streak (independently of grace).
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 23), OWNER) == 3


def test_a_backfilled_log_on_an_already_bridged_date_counts_as_qualified_not_neutral(db, config):
    """Rule 16's general principle ("a real entry beats the neutral
    default"), applied to a grace-protected date specifically: once the
    user backfills a genuine log for a date grace already bridged, that
    day is QUALIFIED on every subsequent read -- the earlier grace spend
    is not "refunded" (the `grace_ledger` row is never deleted), but it
    also no longer matters for THIS date's own classification."""
    hydrate = _habit("hydrate")
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d}T09:00:00", "hydrate", 1)
    grace.evaluate_grace(db, config, HabitRegistry([hydrate]), OWNER, date(2026, 8, 24))
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 23), OWNER) == 3  # held, not incremented

    # The user backfills 08-23 after the fact.
    _seed(db, "2026-08-23T20:00:00", "hydrate", 1)
    assert streaks.compute_streak(db, config, hydrate, date(2026, 8, 23), OWNER) == 4  # now genuinely counted

    # The week's grace budget stays spent -- the ledger row is untouched.
    assert db.grace_used_in_week(OWNER, "hydrate", "2026-W34") is True


def test_fail_open_one_bad_habit_does_not_abort_the_others(db, config, monkeypatch):
    hydrate = _habit("hydrate")
    stretch = _habit("stretch", type_="duration")
    registry = HabitRegistry([hydrate, stretch])
    for habit_id in ("hydrate", "stretch"):
        for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
            _seed(db, f"{d}T09:00:00", habit_id, 1)

    real_day_qualifies = streaks.day_qualifies

    def _boom_for_hydrate(db_, config_, habit, day, user_id, goal=streaks._GOAL_UNSET):  # type: ignore[assignment]
        if habit.id == "hydrate":
            raise RuntimeError("synthetic failure for hydrate only")
        return real_day_qualifies(db_, config_, habit, day, user_id, goal=goal)

    monkeypatch.setattr(streaks, "day_qualifies", _boom_for_hydrate)

    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))

    # hydrate blew up and was skipped; stretch was still evaluated and bridged.
    assert [h.id for h, _ in result] == ["stretch"]
    assert db.grace_protected_dates(OWNER, "hydrate", "2026-08-23", "2026-08-23") == set()
    assert db.grace_protected_dates(OWNER, "stretch", "2026-08-23", "2026-08-23") == {"2026-08-23"}


# ===========================================================================
# format_grace_message -- pure formatter, folds every bridged habit into
# ONE message (mirrors core/nudge.py:build_nudge_message's own discipline).
# ===========================================================================


def test_format_grace_message_single_and_multiple_and_empty():
    hydrate = _habit("hydrate")
    stretch = _habit("stretch", type_="duration")

    assert grace.format_grace_message([], "en") == ""

    single = grace.format_grace_message([(hydrate, 20)], "en")
    assert single == (
        "🛟 No worries — I used your grace day for hydrate, so your 20-day streak is safe. (one grace per week)"
    )

    multi = grace.format_grace_message([(hydrate, 20), (stretch, 5)], "en")
    assert "hydrate" in multi and "stretch" in multi
    assert multi.count("🛟") == 2  # one full line per bridged habit
    assert "\n\n" in multi  # joined, not concatenated

    multi_th = grace.format_grace_message([(hydrate, 20)], "th")
    assert "ไม่ต้องห่วง" in multi_th
