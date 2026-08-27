"""SPEC-v1.9.md §4 Rules 12-17 (module `pause`, M3) -- Luna's own test suite
for `core/pause.py` + `core/commands.py`'s `_match_pause`/`_match_resume`
region + `storage/db.py`'s `insert_pause`/`clear_pauses` write region.
Owned ACs (SPEC-v1.9.md §11): AC19 (`/pause`/`/resume` write+confirm+audit,
idempotent resume), AC21 (a pause holds the streak across a gap), AC22
(`/dashboard`/`/habits` marker is integration's own render -- this module's
own contribution is `/pause` bare's status reply, plus proving a voluntary
log during a pause still logs/confirms), AC23 (a voluntary log during a
pause still qualifies for a reactive milestone -- proven at the
`classify_day`/`day_qualifies` level, since that IS what a reactive
milestone check consults; the full `main.py` wiring is integration's job,
out of this module's scope), AC24 (over-cap duration + past/unparseable
`until` date rejection, no row written).

AC20 (the actual SUPPRESSION of reminders/check-ins/nudges/weekly-review/
daily-summary while paused) is integration's own wiring into those other
modules (SPEC-v1.9.md §6/§11) -- this module only EXPOSES the
`is_paused(...)` helper integration will call; this suite proves that
helper's own correctness (habit-scoped vs all-habits coverage, natural
expiry with no `/resume` needed) exhaustively, since that is the entire
contract integration depends on.

Conventions mirror `tests/test_grace.py`/`tests/test_v19_shared_surface.py`
(real on-disk SQLite via `tmp_path`, no DB mocks). `db.set_cadence` (module
`cadence`, M1) is used as-is (already landed) for the one engine-level
ISO-week-boundary test below -- read-only reliance on a sibling module's
already-shipped write accessor, not a duplicate/private copy.

Anchor dates (verified via `date.isocalendar()`): 2026-08-17 is a Monday,
ISO week 34; 2026-08-24 is the next Monday, ISO week 35; 2026-08-10 is the
Monday before that, ISO week 33; 2026-08-26 (this suite's fixed "today" for
every `execute_pause` call) is a Wednesday."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from habit_assistant.config import Config
from habit_assistant.core import audit, audit_view, commands, pause, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
TODAY = date(2026, 8, 26)  # a Wednesday


def _fixed_clock() -> datetime:
    return datetime.combine(TODAY, datetime.min.time()).replace(hour=10)


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _habit(id_: str, label_th: str | None = None, type_: str = "boolean") -> Habit:
    """A goal-less boolean habit so `day_qualifies` reduces to a plain
    any-entry check (`count_true`), keeping the streak arithmetic in every
    test below simple and legible -- mirrors `tests/test_grace.py`'s own
    `_habit` helper. Deliberately never "water" (SPEC-v1.1.md's own legacy
    2500 ml special-case in `targets.effective_goal`)."""
    return Habit(
        id=id_,
        type=type_,
        label_en=id_,
        label_th=label_th or id_,
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "pause.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def registry() -> HabitRegistry:
    return HabitRegistry([_habit("gym", "ยิม"), _habit("diary", "ไดอารี่")])


async def _pause(db, config, registry, text, lang="en", user_id=OWNER, clock=_fixed_clock):
    command = commands.dispatch(text, registry)
    assert command is not None, f"{text!r} should dispatch as a command"
    return await pause.execute_pause(command, db=db, config=config, registry=registry, lang=lang, user_id=user_id, clock=clock)


async def _resume(db, config, registry, text, lang="en", user_id=OWNER, clock=_fixed_clock):
    command = commands.dispatch(text, registry)
    assert command is not None, f"{text!r} should dispatch as a command"
    return await pause.execute_resume(
        command, db=db, config=config, registry=registry, lang=lang, user_id=user_id, clock=clock
    )


# ===========================================================================
# AC19 -- /pause writes a pauses row (habit-scoped / all-habits), confirms,
# audits pause_set; /resume deletes it, audits pause_clear; resuming when
# not paused returns pause_none_active.
# ===========================================================================


class TestAC19PauseResumeBasics:
    async def test_pause_habit_until_date_writes_row_and_confirms(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym until 2026-09-01")
        assert "gym" in reply and "2026-09-01" in reply
        rows = db.active_pauses(OWNER)
        assert len(rows) == 1
        assert rows[0]["habit_id"] == "gym"
        assert rows[0]["start_date"] == TODAY.isoformat()
        assert rows[0]["end_date"] == "2026-09-01"

    async def test_pause_days_form_writes_row(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause diary 5d")
        assert "diary" in reply
        rows = db.active_pauses(OWNER)
        assert rows[0]["habit_id"] == "diary"
        # today (26th) + 4 more days = 30th (5 calendar days inclusive).
        assert rows[0]["end_date"] == "2026-08-30"

    async def test_pause_no_habit_token_pauses_all(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause 5d")
        assert "everything" in reply.lower() or "/resume" in reply
        rows = db.active_pauses(OWNER)
        assert len(rows) == 1
        assert rows[0]["habit_id"] is None

    async def test_pause_set_writes_audit_row(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        rows = db._conn.execute(
            "SELECT * FROM audit_log WHERE action = 'pause_set' AND entity = 'gym'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] == OWNER
        # Renders with a bilingual label via /audit (R-27/AC18-style check,
        # reused here for pause_set's own vocabulary).
        rendered = audit_view.render_recent(db, config, "en", limit=5, owner_chat_id=OWNER)
        assert "paused" in rendered.lower() or "gym" in rendered

    async def test_resume_habit_deletes_row_and_confirms(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        reply = await _resume(db, config, registry, "/resume gym")
        assert "gym" in reply
        assert db.active_pauses(OWNER) == []

    async def test_resume_writes_pause_clear_audit_row(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        await _resume(db, config, registry, "/resume gym")
        rows = db._conn.execute(
            "SELECT * FROM audit_log WHERE action = 'pause_clear' AND entity = 'gym'"
        ).fetchall()
        assert len(rows) == 1

    async def test_resume_all_clears_every_row(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        await _pause(db, config, registry, "/pause diary until 2026-09-05")
        reply = await _resume(db, config, registry, "/resume")
        assert reply
        assert db.active_pauses(OWNER) == []

    async def test_resume_when_not_paused_is_idempotent_and_friendly(self, db, config, registry):
        reply = await _resume(db, config, registry, "/resume gym")
        assert "gym" in reply
        reply_all = await _resume(db, config, registry, "/resume")
        assert reply_all
        # Neither call wrote a pause_clear row (nothing to clear).
        rows = db._conn.execute("SELECT * FROM audit_log WHERE action = 'pause_clear'").fetchall()
        assert rows == []

    async def test_pause_invalid_habit_writes_nothing(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause nosuchhabit 5d")
        assert "gym" in reply and "diary" in reply  # lists valid ids
        assert db.active_pauses(OWNER) == []

    async def test_resume_invalid_habit_is_friendly(self, db, config, registry):
        reply = await _resume(db, config, registry, "/resume nosuchhabit")
        assert "gym" in reply and "diary" in reply


# ===========================================================================
# AC24 -- over-cap duration -> pause_too_long, no write; past/unparseable
# `until` date -> pause_invalid_date, no write. Includes the exact 30-day
# cap boundary (default `[pause] max_days`).
# ===========================================================================


class TestAC24Validation:
    async def test_over_cap_days_rejected_no_write(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 60d")
        assert "30" in reply
        assert db.active_pauses(OWNER) == []

    async def test_exactly_max_days_is_accepted(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 30d")
        assert db.active_pauses(OWNER) != []
        # today (08-26) + 29 more days = 09-24 (30 calendar days inclusive).
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-09-24"
        assert reply

    async def test_one_day_over_max_days_is_rejected(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 31d")
        assert db.active_pauses(OWNER) == []

    async def test_until_date_exactly_at_cap_boundary_is_accepted(self, db, config, registry):
        # 2026-09-24 is exactly 30 days from 2026-08-26 inclusive.
        reply = await _pause(db, config, registry, "/pause gym until 2026-09-24")
        assert db.active_pauses(OWNER) != []
        assert reply

    async def test_until_date_one_day_past_cap_boundary_is_rejected(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym until 2026-09-25")
        assert "30" in reply
        assert db.active_pauses(OWNER) == []

    async def test_past_until_date_rejected_no_write(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym until 2020-01-01")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_unparseable_until_token_rejected_no_write(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym until banana")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_until_today_is_accepted_not_treated_as_past(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym until 2026-08-26")
        assert db.active_pauses(OWNER) != []
        assert reply

    async def test_until_weekday_resolves_to_next_future_occurrence(self, db, config, registry):
        # TODAY (2026-08-26) is a Wednesday; "until monday" must resolve to
        # the NEXT Monday (2026-08-31), never today or a past Monday.
        await _pause(db, config, registry, "/pause gym until monday")
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-08-31"

    async def test_until_wednesday_today_rolls_to_next_week_not_today(self, db, config, registry):
        # "until <today's own weekday>" must NOT mean "until today" (a
        # zero-length pause) -- it means the NEXT occurrence, one week out.
        await _pause(db, config, registry, "/pause gym until wednesday")
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-09-02"


# ===========================================================================
# Adversarial edges (Archi's dispatch brief): overlapping pauses extend
# rather than reject/stack; pausing an already-paused habit; pausing all
# then resuming one specific habit (the literal-reading limitation).
# ===========================================================================


class TestAdversarialOverlapAndScope:
    async def test_re_pausing_the_same_habit_extends_replaces_not_stacks(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        first_end = db.active_pauses(OWNER)[0]["end_date"]
        await _pause(db, config, registry, "/pause gym until 2026-09-20")  # within the 30-day cap from 08-26
        rows = db.active_pauses(OWNER)
        assert len(rows) == 1, "a second /pause for the same habit must replace, not stack, the row"
        assert rows[0]["end_date"] == "2026-09-20"
        assert rows[0]["end_date"] != first_end

    async def test_re_pausing_records_the_previous_end_as_audit_old_value(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        await _pause(db, config, registry, "/pause gym 10d")
        row = db._conn.execute(
            "SELECT * FROM audit_log WHERE action = 'pause_set' AND entity = 'gym' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["old_value"] == "2026-08-30"  # the first pause's own end date
        assert row["new_value"] == "2026-09-04"

    async def test_pausing_all_then_a_different_habit_specifically_coexists(self, db, config, registry):
        await _pause(db, config, registry, "/pause 5d")  # all habits, expires 2026-08-30
        await _pause(db, config, registry, "/pause gym until 2026-09-20")  # gym specifically, longer, still <=30d cap
        rows = db.active_pauses(OWNER)
        assert len(rows) == 2
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 9, 10))  # only gym's own row covers this far out
        assert not pause.is_paused(db, config, OWNER, "diary", date(2026, 9, 10))  # the all-habits row already expired

    async def test_pause_all_then_resume_one_leaves_it_covered_by_the_all_row(self, db, config, registry):
        """Documented literal-reading behavior (IMPL-v1.9-pause.md's own
        "Known limitations"): `/resume <habit>` deletes only a
        HABIT-SCOPED row. With only an all-habits pause active, there is
        no habit-scoped row for `/resume gym` to delete, so it reports
        `pause_none_active`-style AND gym remains paused via the
        untouched all-habits row -- this is deliberate, not a bug."""
        await _pause(db, config, registry, "/pause 5d")  # all habits
        reply = await _resume(db, config, registry, "/resume gym")
        assert "gym" in reply
        # The all-habits row was NOT touched -- gym (and every other
        # habit) is still covered by it.
        assert pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert pause.is_paused(db, config, OWNER, "diary", TODAY)
        assert len(db.active_pauses(OWNER)) == 1

    async def test_pausing_an_already_paused_habit_a_second_time_still_confirms(self, db, config, registry):
        reply1 = await _pause(db, config, registry, "/pause gym 5d")
        reply2 = await _pause(db, config, registry, "/pause gym 3d")
        assert reply1 and reply2
        assert len(db.active_pauses(OWNER)) == 1


# ===========================================================================
# is_paused() -- habit-scoped vs all-habits coverage, natural expiry with
# no /resume needed (integration's own gating depends entirely on this).
# ===========================================================================


class TestIsPaused:
    def test_habit_scoped_pause_covers_only_that_habit(self, db, config):
        db.insert_pause(OWNER, "gym", "2026-08-26", "2026-08-30")
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 28))
        assert not pause.is_paused(db, config, OWNER, "diary", date(2026, 8, 28))

    def test_all_habits_pause_covers_every_habit(self, db, config):
        db.insert_pause(OWNER, None, "2026-08-26", "2026-08-30")
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 28))
        assert pause.is_paused(db, config, OWNER, "diary", date(2026, 8, 28))

    def test_date_outside_the_window_is_not_paused(self, db, config):
        db.insert_pause(OWNER, "gym", "2026-08-26", "2026-08-30")
        assert not pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 25))
        assert not pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 31))

    def test_pause_expires_naturally_with_no_resume_needed(self, db, config, registry):
        """R14's own "held, not broken" contract relies on the WINDOW
        itself, not an explicit /resume -- a pause that has simply run out
        must stop covering dates past its end, with the row still present
        (no /resume call in this test at all)."""
        db.insert_pause(OWNER, "gym", "2026-08-01", "2026-08-10")
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 5))
        assert not pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 11))
        # The row is still there (nobody /resumed) -- only the DATE is
        # what determines coverage.
        assert len(db.active_pauses(OWNER)) == 1


# ===========================================================================
# AC21 -- a pause holds a daily streak across the gap (engine-level,
# through THIS module's own insert_pause write path, not raw SQL).
# ===========================================================================


class TestAC21EngineHoldsStreakAcrossPause:
    def test_daily_streak_held_across_a_paused_gap(self, db, config, registry):
        habit = registry.get("gym")
        # Logged Mon-Wed, paused Thu-Fri (via execute_pause's own write
        # path), logged again Sat-Sun -- an unprotected gap would break the
        # streak at Thu; a NEUTRAL (held) gap must not.
        for day in ("2026-08-17", "2026-08-18", "2026-08-19"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-21")
        for day in ("2026-08-22", "2026-08-23"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)

        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 23), OWNER)
        assert streak == 5, "3 logged + 2 held-not-broken + 2 logged = 5"

    def test_a_genuine_unprotected_gap_still_breaks_the_streak(self, db, config, registry):
        """Control case for the test above -- proves the held result isn't
        an accident of the arithmetic (same shape, no pause row)."""
        habit = registry.get("gym")
        for day in ("2026-08-17", "2026-08-18", "2026-08-19"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        # No pause over 08-20/08-21 this time.
        for day in ("2026-08-22", "2026-08-23"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)

        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 23), OWNER)
        assert streak == 2, "the unprotected 08-20/08-21 gap breaks the walk; only the trailing 2 days count"


# ===========================================================================
# AC22/AC23 -- a voluntary log during a pause still logs/confirms/qualifies
# (a real entry beats the NEUTRAL default, R16) -- this is what lets a
# reactive milestone still fire; `day_qualifies`/`classify_day` are exactly
# what `crossed_milestone` consults, so proving QUALIFIED wins here IS the
# load-bearing proof for AC23 at this module's own layer (main.py's actual
# milestone-dispatch wiring is integration's, out of scope).
# ===========================================================================


class TestAC22AC23VoluntaryLogDuringPauseStillQualifies:
    def test_a_real_log_on_a_paused_day_is_qualified_not_neutral(self, db, config, registry):
        habit = registry.get("gym")
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-21")
        _seed(db, "2026-08-20T09:00:00", "gym", 1.0)  # logged DURING the pause

        state = streaks.classify_day(
            db, config, habit, "2026-08-20", OWNER, goal=None,
            paused_dates={"2026-08-20", "2026-08-21"}, grace_dates=set(),
        )
        assert state == "qualified"

    def test_a_paused_day_with_no_log_is_neutral(self, db, config, registry):
        habit = registry.get("gym")
        state = streaks.classify_day(
            db, config, habit, "2026-08-20", OWNER, goal=None,
            paused_dates={"2026-08-20"}, grace_dates=set(),
        )
        assert state == "neutral"

    def test_logging_during_a_pause_still_extends_the_streak(self, db, config, registry):
        """AC22's own "a voluntary log during the pause still logs and
        confirms" -- the streak itself must reflect the real entry (count
        it), not silently hide it behind the pause."""
        habit = registry.get("gym")
        _seed(db, "2026-08-18T09:00:00", "gym", 1.0)
        _seed(db, "2026-08-19T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-21")
        _seed(db, "2026-08-20T09:00:00", "gym", 1.0)  # logged DURING the pause -- should still count

        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 20), OWNER)
        assert streak == 3


# ===========================================================================
# /pause bare status reply -- not AC-mandated, but explicitly requested
# ("what /pause bare shows"). Proves it never errors and reflects reality.
# ===========================================================================


class TestPauseStatusReply:
    async def test_bare_pause_with_nothing_active_says_so(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause")
        assert reply

    async def test_bare_pause_lists_active_pauses(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        reply = await _pause(db, config, registry, "/pause")
        assert "gym" in reply
        assert "2026-08-30" in reply

    async def test_habit_only_no_duration_shows_that_habits_status(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        reply = await _pause(db, config, registry, "/pause diary")  # diary not paused
        assert "diary" in reply
        assert db.active_pauses(OWNER) != [] and len(db.active_pauses(OWNER)) == 1  # unchanged, no write


# ===========================================================================
# ISO-week-boundary NEUTRAL for a CADENCE habit -- a pause spanning the
# actual week boundary (some paused days fall in the earlier ISO week,
# some in the later one) must make the earlier week NEUTRAL (held, not
# broken) when it makes that week's target unreachable, letting the walk
# continue past it to an even-older MET week. Written through THIS
# module's own `insert_pause`; `db.set_cadence` (module `cadence`, already
# landed) sets up the cadence row this test depends on.
# ===========================================================================


class TestEngineIsoWeekBoundaryNeutral:
    def test_pause_spanning_the_week_boundary_holds_the_earlier_week(self, db, config, registry):
        db.set_cadence(OWNER, "gym", 3)
        habit = registry.get("gym")

        # Week Z (Mon 2026-08-10 - Sun 2026-08-16, ISO week 33): MET, 3 logs.
        for day in ("2026-08-11", "2026-08-12", "2026-08-13"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)

        # Week A (Mon 2026-08-17 - Sun 2026-08-23, ISO week 34): 0 logs.
        # Pause spans 2026-08-19 (Wed, week A) through 2026-08-25 (Tue,
        # week B) -- straddles the real Sun(08-23)->Mon(08-24) boundary.
        # Week A's own paused-day count is 5 (Wed-Sun), leaving only 2
        # non-paused days -- fewer than per_week=3, so week A is
        # UNREACHABLE (NEUTRAL/held), not MISSED.
        db.insert_pause(OWNER, "gym", "2026-08-19", "2026-08-25")

        # Week B (Mon 2026-08-24 - Sun 2026-08-30, ISO week 35, the
        # CURRENT week for this test's end_date): MET, 3 logs on the days
        # after the pause ends.
        for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)

        # Week before Z (Mon 2026-08-03 - Sun 2026-08-09): genuinely
        # unpaused and unlogged -- MUST break the walk here, so the total
        # is deterministic (proves week A's NEUTRAL isn't just "everything
        # before this point is also skipped").
        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 30), OWNER)
        assert streak == 2, (
            "current week B MET (1) -> week A held across the boundary, not broken "
            "(skip) -> week Z MET (2) -> the week before Z is a genuine miss, breaks"
        )

    def test_control_unpaused_missed_week_breaks_instead_of_holding(self, db, config, registry):
        """Same shape as the test above with NO pause over week A -- proves
        week A's own 0-log/unreachable-only-because-of-the-pause status is
        what makes the difference, not some other arithmetic quirk."""
        db.set_cadence(OWNER, "gym", 3)
        habit = registry.get("gym")
        for day in ("2026-08-11", "2026-08-12", "2026-08-13"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        # No pause this time -- week A (08-17..08-23) is a genuine miss
        # (0 logs, all 7 days reachable).
        for day in ("2026-08-27", "2026-08-28", "2026-08-29"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)

        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 30), OWNER)
        assert streak == 1, "week A is a genuine MISS without the pause -- the walk breaks right after week B"


# ===========================================================================
# commands.py dispatch -- shape recognition + the Thai-alias adversarial
# corpus (bare common words must never dispatch; ordinary glued prose must
# never dispatch).
# ===========================================================================


class TestDispatchShapes:
    def test_slash_pause_habit_until_date(self, registry):
        cmd = commands.dispatch("/pause gym until 2026-09-01", registry)
        assert cmd is not None and cmd.kind == "pause"
        assert cmd.category == "gym"
        assert cmd.pref_value == "until 2026-09-01"

    def test_slash_pause_days_no_habit(self, registry):
        cmd = commands.dispatch("/pause 5d", registry)
        assert cmd.category is None
        assert cmd.pref_value == "5d"

    def test_slash_pause_bare(self, registry):
        cmd = commands.dispatch("/pause", registry)
        assert cmd.kind == "pause"
        assert cmd.category is None and cmd.pref_value is None

    def test_slash_resume_bare_means_all(self, registry):
        cmd = commands.dispatch("/resume", registry)
        assert cmd.kind == "resume" and cmd.category is None

    def test_thai_pause_with_label_and_days(self, registry):
        cmd = commands.dispatch("พัก ยิม 3d", registry)
        assert cmd is not None and cmd.kind == "pause"
        assert cmd.category == "gym"
        assert cmd.pref_value == "3d"

    def test_thai_resume_with_label(self, registry):
        cmd = commands.dispatch("กลับมา ยิม", registry)
        assert cmd is not None and cmd.kind == "resume" and cmd.category == "gym"

    @pytest.mark.parametrize("word", ["พัก", "หยุดพัก", "กลับมา", "ต่อ"])
    def test_bare_thai_trigger_words_never_dispatch(self, registry, word):
        assert commands.dispatch(word, registry) is None

    @pytest.mark.parametrize(
        "text",
        [
            "พัก ก่อนนะ",  # "take a break first" -- ordinary prose, no real habit/duration shape
            "ต่อไปเลยนะ",  # glued continuation -- never even reaches the trigger boundary
            "ต่อ ไปอีกหน่อย",  # "continue a bit more" -- not a single habit token
            "กลับมาแล้วนะ",  # glued "[I'm] back now" -- never reaches the trigger boundary
            "หยุดพักหน้าจอหน่อย",  # glued "take a screen break" -- never reaches the trigger boundary
        ],
    )
    def test_ordinary_thai_prose_never_dispatches(self, registry, text):
        assert commands.dispatch(text, registry) is None

    def test_unresolved_habit_token_in_thai_falls_through(self, registry):
        # "พัก" + a token that names no configured habit -- must NOT
        # dispatch (mirrors _match_remind's own registry-anchored gate).
        assert commands.dispatch("พัก รถยนต์ 3d", registry) is None

    def test_malformed_duration_in_thai_falls_through(self, registry):
        # A real habit label, but the tail after it isn't duration-shaped.
        assert commands.dispatch("พัก ยิม พรุ่งนี้", registry) is None

    def test_reserved_trigger_words_include_pause_stems(self):
        words = commands.reserved_trigger_words()
        for stem in ("pause", "พัก", "หยุดพัก", "resume", "กลับมา", "ต่อ"):
            assert stem in words
