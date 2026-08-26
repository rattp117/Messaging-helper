"""SPEC-v1.9.md §4 Rules 12-17 (module `pause`, M3) -- Vera's adversarial
gap probe on top of Luna's own `tests/test_pause.py` (57 tests, all
passing at baseline). This file does NOT re-prove what Luna's suite
already proves cleanly (basic AC19/AC24 shapes, the cap boundary, the
cadence ISO-week-boundary NEUTRAL case) -- it targets edges Archi's
dispatch brief called out specifically: duration-parsing corners, Thai
numerals, month/year-end date math, per-user isolation (NOT covered by
Luna's suite -- her fixtures use a single `OWNER` throughout), the
truthfulness of the `/resume <habit>` reply under an active all-habits
pause (Archi's ruling 2), and a wider Thai-trigger-word false-positive
corpus (`ต่อ` especially).

Conventions mirror `tests/test_pause.py` exactly (real on-disk SQLite via
`tmp_path`, no DB mocks, `TODAY = date(2026, 8, 26)`, a Wednesday, fixed
via a `clock=` override threaded through `execute_pause`).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from habit_assistant.config import Config
from habit_assistant.core import audit_view, commands, pause, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
OTHER = "other-user"
TODAY = date(2026, 8, 26)  # a Wednesday


def _fixed_clock() -> datetime:
    return datetime.combine(TODAY, datetime.min.time()).replace(hour=10)


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER) -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, "x", "reply"))


def _habit(id_: str, label_th: str | None = None, type_: str = "boolean") -> Habit:
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
    database = Database(tmp_path / "pause_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(OTHER, role="member", status="active")
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
    # ROUND-2: `execute_resume` gained a `clock` param (needed by its own
    # `_resume_scope`/truncate logic to compute "today"/"yesterday"). This
    # helper now pins it to `_fixed_clock` explicitly, same as `_pause`
    # above -- relying on the default `datetime.now` would make every
    # elapsed-day/truncate assertion in this file dependent on the real
    # wall-clock date matching TODAY, which is fragile (it happened to be
    # true when this file was first written, by coincidence).
    command = commands.dispatch(text, registry)
    assert command is not None, f"{text!r} should dispatch as a command"
    return await pause.execute_resume(command, db=db, config=config, registry=registry, lang=lang, user_id=user_id, clock=clock)


# ===========================================================================
# Parsing -- garbage durations, mixed-case, Thai numerals, month/year-end
# date math, weekday forms in both languages.
# ===========================================================================


class TestGarbageDurations:
    async def test_zero_days_rejected_as_usage_not_written(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 0d")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_bare_digits_no_unit_suffix_is_usage_error(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 5")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_decimal_days_rejected_no_write(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 5.5d")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_double_unit_suffix_rejected_no_write(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 5dd")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_bare_until_with_no_token_is_usage_error_not_a_crash(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym until")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_negative_days_token_treated_as_unresolved_habit_not_duration(self, db, config, registry):
        # "-5d" doesn't start with a digit, so the shape-splitter treats it
        # as a HABIT token, not a duration -- it then fails habit
        # resolution. Documenting the actual (reasonable) behavior rather
        # than assuming a dedicated negative-number error path exists.
        reply = await _pause(db, config, registry, "/pause -5d")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_uppercase_day_unit_is_accepted(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym 5D")
        assert db.active_pauses(OWNER) != []
        assert reply

    async def test_thai_numerals_in_days_duration_are_parsed_correctly(self, db, config, registry):
        # Python's `\d`/`int()` are Unicode-digit-aware by default, so
        # "๕d" (Thai numeral 5) should resolve identically to "5d" with NO
        # special-case normalization code required in pause.py.
        reply = await _pause(db, config, registry, "/pause gym ๕d")
        rows = db.active_pauses(OWNER)
        assert rows != [], f"Thai-numeral duration was rejected: {reply!r}"
        # today (08-26) + 4 more days = 08-30, exactly like "5d".
        assert rows[0]["end_date"] == "2026-08-30"

    async def test_fullwidth_numerals_in_days_duration(self, db, config, registry):
        reply = await _pause(db, config, registry, "/pause gym ５d")
        rows = db.active_pauses(OWNER)
        assert rows != [], f"fullwidth-numeral duration was rejected: {reply!r}"
        assert rows[0]["end_date"] == "2026-08-30"


class TestMonthAndYearEndDateMath:
    async def test_days_duration_crosses_month_boundary(self, db, config, registry):
        # today override: 2026-08-28 (still a valid Wednesday->Friday
        # shift within the same fixture set, using a custom clock).
        async def _clock():
            return datetime(2026, 8, 28, 10, 0, 0)

        command = commands.dispatch("/pause gym 5d", registry)
        reply = await pause.execute_pause(
            command, db=db, config=config, registry=registry, lang="en", user_id=OWNER,
            clock=lambda: datetime(2026, 8, 28, 10, 0, 0),
        )
        rows = db.active_pauses(OWNER)
        assert rows != [], reply
        # 08-28 + 4 more days = 09-01.
        assert rows[0]["end_date"] == "2026-09-01"

    async def test_days_duration_crosses_year_boundary(self, db, config, registry):
        command = commands.dispatch("/pause gym 5d", registry)
        reply = await pause.execute_pause(
            command, db=db, config=config, registry=registry, lang="en", user_id=OWNER,
            clock=lambda: datetime(2026, 12, 29, 10, 0, 0),
        )
        rows = db.active_pauses(OWNER)
        assert rows != [], reply
        # 2026-12-29 + 4 more days = 2027-01-02.
        assert rows[0]["end_date"] == "2027-01-02"

    async def test_until_date_feb_29_in_a_non_leap_year_is_rejected(self, db, config, registry):
        # 2026 is not a leap year -- "2026-02-29" doesn't exist.
        reply = await _pause(db, config, registry, "/pause gym until 2026-02-29")
        assert db.active_pauses(OWNER) == []
        assert reply

    async def test_until_date_feb_29_in_a_leap_year_is_a_valid_date_shape(self, db, config, registry):
        # Not reachable within the 30-day cap from 2026-08-26, so this
        # exercises the DATE-PARSING branch only (must fail for being past
        # the cap, never for being an unparseable date) -- confirms
        # `pause_too_long`, not `pause_invalid_date`, fires.
        reply = await _pause(db, config, registry, "/pause gym until 2028-02-29")
        assert db.active_pauses(OWNER) == []
        assert "30" in reply, f"expected the cap message, got: {reply!r}"


class TestWeekdayForms:
    async def test_until_weekday_case_insensitive(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym until MONDAY")
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-08-31"

    async def test_until_bare_thai_weekday_resolves(self, db, config, registry):
        # TODAY is Wednesday 2026-08-26; "จันทร์" (Monday) with no "วัน"
        # prefix must resolve to the next Monday, 2026-08-31.
        await _pause(db, config, registry, "/pause gym until จันทร์")
        rows = db.active_pauses(OWNER)
        assert rows != []
        assert rows[0]["end_date"] == "2026-08-31"

    async def test_until_thai_weekday_with_wan_prefix_resolves(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym until วันจันทร์")
        rows = db.active_pauses(OWNER)
        assert rows != []
        assert rows[0]["end_date"] == "2026-08-31"

    async def test_thai_command_form_with_english_until_weekday(self, db, config, registry):
        # The Thai TRIGGER word (พัก) paired with the "until <token>"
        # duration grammar (the duration grammar itself is not
        # translated) -- must dispatch and resolve identically.
        reply = await _pause(db, config, registry, "พัก ยิม until จันทร์")
        rows = db.active_pauses(OWNER)
        assert rows != [], reply
        assert rows[0]["end_date"] == "2026-08-31"


# ===========================================================================
# Semantics -- shrinking re-pause, natural expiry vs. early resume,
# pause-all-then-resume-one truthfulness (Archi's ruling 2), ISO-week
# boundary for a DAILY (non-cadence) habit, expired rows, per-user
# isolation.
# ===========================================================================


class TestSemanticEdges:
    async def test_re_pausing_with_a_shorter_window_contracts_not_merges(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym until 2026-09-20")
        await _pause(db, config, registry, "/pause gym 2d")
        rows = db.active_pauses(OWNER)
        assert len(rows) == 1, "a second, SHORTER /pause must still replace, not merge with, the longer window"
        assert rows[0]["end_date"] == "2026-08-27"

    async def test_early_resume_before_natural_expiry_matches_natural_expiry_result(self, db, config, registry):
        """A streak held by an early /resume must compute identically to
        one left to expire naturally -- the streak arithmetic only ever
        consults the DATE window, never whether /resume was called.

        ROUND-2: this was Vera's finding 3 (round-1 FAIL, streak==2 --
        `execute_resume` used to hard-delete the row, retroactively
        un-protecting the already-elapsed 08-20/08-21 gap). Luna's fix:
        `_resume_scope` now TRUNCATES a row that already started
        accumulating protected days to end yesterday instead of deleting
        it outright (`db.truncate_pause`). Now PASSES."""
        habit = registry.get("gym")
        for day in ("2026-08-17", "2026-08-18", "2026-08-19"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-21")
        for day in ("2026-08-22", "2026-08-23"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        # Resume early (round-2: truncated to end yesterday, not deleted)
        # -- the PAST dates it used to cover must remain NEUTRAL for a
        # walk that already crossed them.
        await _resume(db, config, registry, "/resume gym")
        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 23), OWNER)
        assert streak == 5

    async def test_resume_habit_reply_is_not_misleading_when_all_habits_pause_still_covers_it(
        self, db, config, registry
    ):
        """Archi's ruling 2: '/resume <habit>' against an all-habits pause
        may report "nothing to resume" (no smart split, ACCEPTED for
        v1.9), BUT the reply text must not actively claim the habit is
        now unpaused/active while the all-pause still covers it.

        ROUND-2: this was Vera's finding 2 (round-1 FAIL -- the old
        `pause_none_active_habit` wording, "{label} isn't paused, so
        there's nothing to resume," is an affirmative false claim while
        gym is still covered by the all-habits row). Luna's fix: a new
        key `pause_covered_by_all` (states the real covering end date)
        is used whenever `is_paused(...)` is still true for the habit;
        `pause_none_active_habit` is now reserved for the genuinely-not-
        paused-at-all case. Now PASSES."""
        await _pause(db, config, registry, "/pause 5d")  # all habits
        reply = await _resume(db, config, registry, "/resume gym")
        assert pause.is_paused(db, config, OWNER, "gym", TODAY), "sanity: gym is still actually paused"
        lowered = reply.lower()
        assert "isn't paused" not in lowered and "is not paused" not in lowered, (
            f"reply asserts gym is unpaused while an all-habits pause still covers it: {reply!r}"
        )
        # The new reply must be truthful AND actionable -- state the real
        # covering end date and point at the way to actually end it.
        assert "2026-08-30" in reply, f"reply should state the all-habits pause's real end date: {reply!r}"

    async def test_resume_habit_reply_th_is_not_misleading_when_all_habits_pause_still_covers_it(
        self, db, config, registry
    ):
        """Same check against the Thai wording of `pause_none_active_habit`
        ("ไม่ได้ถูกพักอยู่" = "is not paused") -- the mirror-image bug in
        the other language. ROUND-2: now PASSES (see EN sibling test)."""
        await _pause(db, config, registry, "/pause 5d", lang="th")
        reply = await _resume(db, config, registry, "/resume gym", lang="th")
        assert pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert "ไม่ได้ถูกพัก" not in reply, f"Thai reply also falsely claims gym is unpaused: {reply!r}"
        assert "2026-08-30" in reply

    def test_pause_spanning_sunday_to_monday_iso_week_boundary_holds_a_daily_habit(self, db, config, registry):
        """A DAILY (non-cadence) habit's walk is a pure date sequence, but
        the ISO-week boundary (Sun 2026-08-23 -> Mon 2026-08-24) is still
        worth an explicit regression check -- a NEUTRAL day either side of
        that boundary must hold identically to any other NEUTRAL day."""
        habit = registry.get("gym")
        for day in ("2026-08-20", "2026-08-21", "2026-08-22"):  # Thu-Sat
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, "gym", "2026-08-23", "2026-08-24")  # Sun -> Mon, spans the boundary
        for day in ("2026-08-25", "2026-08-26"):  # Tue-Wed
            _seed(db, f"{day}T09:00:00", "gym", 1.0)

        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 26), OWNER)
        assert streak == 5, "3 logged + 2 held across the week boundary + 2 logged = 5"

    def test_expired_pause_row_does_not_linger_in_a_later_compute_streak_walk(self, db, config, registry):
        """A pause row that has naturally expired (never /resumed) must
        stop holding dates it does not literally cover, even when
        `compute_streak` is evaluated long after it expired -- the row's
        mere EXISTENCE must never leak protection past its own end_date."""
        habit = registry.get("gym")
        db.insert_pause(OWNER, "gym", "2026-07-01", "2026-07-05")  # long expired, never resumed
        for day in ("2026-08-24", "2026-08-25", "2026-08-26"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        # 2026-08-23 (Sunday) has no log and is NOT covered by the expired
        # July pause -- it must be a genuine MISSED break.
        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 26), OWNER)
        assert streak == 3, "the expired July pause must not protect August dates it never covered"

    async def test_pause_is_per_user_isolated_same_habit_id(self, db, config, registry):
        """A's pause on 'gym' must be completely invisible to B's read of
        the same habit id -- NOT exercised anywhere in Luna's own suite
        (single OWNER fixture throughout)."""
        await _pause(db, config, registry, "/pause gym 5d", user_id=OWNER)
        assert pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert not pause.is_paused(db, config, OTHER, "gym", TODAY)
        assert db.active_pauses(OTHER) == []
        assert len(db.active_pauses(OWNER)) == 1

    async def test_all_habits_pause_is_per_user_isolated(self, db, config, registry):
        await _pause(db, config, registry, "/pause 5d", user_id=OWNER)
        assert pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert pause.is_paused(db, config, OWNER, "diary", TODAY)
        assert not pause.is_paused(db, config, OTHER, "gym", TODAY)
        assert not pause.is_paused(db, config, OTHER, "diary", TODAY)

    async def test_resume_by_one_user_does_not_affect_another_users_identical_pause(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d", user_id=OWNER)
        await _pause(db, config, registry, "/pause gym 5d", user_id=OTHER)
        await _resume(db, config, registry, "/resume gym", user_id=OWNER)
        assert not pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert pause.is_paused(db, config, OTHER, "gym", TODAY), "B's identical pause must survive A's own /resume"


# ===========================================================================
# ROUND-2 (Archi's re-verification brief): the truncate-not-delete fix for
# the early-resume gap (finding 3) and the truthful pause_covered_by_all
# reply (finding 2). Probing the new `_resume_scope`/`db.truncate_pause`
# machinery directly, plus every edge Archi named: never-extends, expired
# rows excluded from is_paused/status listings, the all-habits bare
# /resume path, natural expiry unchanged, re-pause-after-truncate, an
# "until DATE" pause truncated, per-user isolation of the truncate.
# ===========================================================================


class TestRound2TruncateSemantics:
    def test_truncate_pause_never_extends_a_shorter_row(self, db, config):
        """db.truncate_pause's own WHERE end_date > ? guard -- calling it
        with a LATER date than the row already has must be a no-op, never
        push the end date forward."""
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-22")
        cleared = db.truncate_pause(OWNER, "gym", "2026-09-01")  # later than 08-22
        assert cleared == 0, "truncate_pause must refuse to extend, even if asked to"
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-08-22"

    def test_truncate_pause_shrinks_when_new_end_is_earlier(self, db, config):
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-25")
        cleared = db.truncate_pause(OWNER, "gym", "2026-08-22")
        assert cleared == 1
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-08-22"

    def test_truncate_pause_is_a_noop_on_an_already_shorter_row(self, db, config):
        db.insert_pause(OWNER, "gym", "2026-08-20", "2026-08-21")
        cleared = db.truncate_pause(OWNER, "gym", "2026-08-25")  # later -- no-op
        assert cleared == 0
        cleared2 = db.truncate_pause(OWNER, "gym", "2026-08-21")  # equal -- also a no-op (`>`, not `>=`)
        assert cleared2 == 0
        assert db.active_pauses(OWNER)[0]["end_date"] == "2026-08-21"

    async def test_early_resume_truncated_row_no_longer_covers_today_or_future(self, db, config, registry):
        """The truncated row must stop protecting TODAY and every future
        date -- only the already-elapsed portion stays protected."""
        for day in ("2026-08-20", "2026-08-21"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, "gym", "2026-08-22", "2026-08-30")  # covers today (08-26) and beyond
        await _resume(db, config, registry, "/resume gym")
        # Elapsed days (08-22..08-25, before today) must still read paused.
        for day in ("2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25"):
            assert pause.is_paused(db, config, OWNER, "gym", date.fromisoformat(day)), f"{day} should stay protected"
        # Today and every future date must now read NOT paused.
        assert not pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert not pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 30))

    async def test_early_resume_truncated_row_excluded_from_bare_status_once_expired(self, db, config, registry):
        """Archi's brief: expired-truncated rows must be excluded from
        active listings. `/pause` bare status is this module's own
        listing surface (`_render_status`) -- once a truncated row's
        (new, shrunk) end_date is in the past relative to `today`, it
        must not still be reported as an "active" pause."""
        db.insert_pause(OWNER, "gym", "2026-08-22", "2026-08-30")
        await _resume(db, config, registry, "/resume gym")  # truncates end_date to 2026-08-25 (yesterday)
        row = db.active_pauses(OWNER)[0]
        assert row["end_date"] == "2026-08-25"
        status = await _pause(db, config, registry, "/pause")
        assert "gym" not in status, (
            f"a pause row whose (truncated) end_date has already passed must not appear as 'active' in "
            f"/pause's own bare status listing, but it did: {status!r}"
        )

    async def test_early_resume_truncated_row_excluded_from_habit_status_once_expired(self, db, config, registry):
        db.insert_pause(OWNER, "gym", "2026-08-22", "2026-08-30")
        await _resume(db, config, registry, "/resume gym")
        status = await _pause(db, config, registry, "/pause gym")
        # Expect the "not paused" reading now that the only row covering
        # gym has an end_date in the past.
        assert "2026-08-25" not in status, f"a stale truncated end_date leaked into the status reply: {status!r}"

    async def test_bare_resume_all_also_truncates_not_deletes(self, db, config, registry):
        """The all-habits bare /resume path must route through the same
        truncate-not-delete protection -- not just the habit-scoped path."""
        for day in ("2026-08-20", "2026-08-21"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, None, "2026-08-22", "2026-08-30")  # all-habits, covers today+future
        await _resume(db, config, registry, "/resume")  # bare -- resumes everything
        # Elapsed days under the all-habits window must still read paused.
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 23))
        assert pause.is_paused(db, config, OWNER, "diary", date(2026, 8, 24))
        # Today/future must now read unpaused.
        assert not pause.is_paused(db, config, OWNER, "gym", TODAY)
        assert not pause.is_paused(db, config, OWNER, "diary", date(2026, 8, 30))

    async def test_natural_expiry_with_no_resume_call_still_unaffected_by_truncate_logic(self, db, config, registry):
        """A pause nobody ever /resumed (naturally expired) must behave
        exactly as before the round-2 fix -- `_resume_scope`/
        `truncate_pause` are only reachable through `execute_resume`."""
        db.insert_pause(OWNER, "gym", "2026-08-01", "2026-08-10")
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 5))
        assert not pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 11))
        row = db.active_pauses(OWNER)[0]
        assert row["end_date"] == "2026-08-10", "an un-resumed row's end_date must never be touched"

    async def test_re_pause_after_an_early_resume_truncate_starts_a_fresh_window(self, db, config, registry):
        """Pausing again after an early-resume-truncate must start a
        brand-new window from TODAY, not be confused by the leftover
        truncated (past) row for the same scope."""
        db.insert_pause(OWNER, "gym", "2026-08-22", "2026-08-30")
        await _resume(db, config, registry, "/resume gym")  # truncates to 2026-08-25
        reply = await _pause(db, config, registry, "/pause gym 3d")
        rows = db.active_pauses(OWNER)
        assert len(rows) == 1, f"re-pausing must replace the stale truncated row, not add a second one: {rows}"
        assert rows[0]["start_date"] == TODAY.isoformat()
        assert rows[0]["end_date"] == "2026-08-28"
        assert reply

    async def test_truncate_applies_identically_to_an_until_date_pause(self, db, config, registry):
        """The truncate fix must not be special-cased to the `<N>d` form
        -- an `until DATE`-shaped pause must be held/truncated
        identically. `/pause ... until DATE` always writes `start_date=
        today` (execute_pause has no way to backdate a start), so to get
        an ELAPSED window to truncate this simulates "the until-DATE
        pause was issued a few days ago" via `db.insert_pause` directly
        (same convention as this file's other elapsed-day tests) --
        what's under test is `_resume_scope`'s handling of the resulting
        ROW SHAPE (an Nd-form and an until-DATE-form pause produce an
        identical row once written), not the parsing path a second time."""
        for day in ("2026-08-20", "2026-08-21"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        db.insert_pause(OWNER, "gym", "2026-08-22", "2026-09-05")  # as if "until 2026-09-05" was set on 08-22
        await _resume(db, config, registry, "/resume gym")
        row = db.active_pauses(OWNER)[0]
        assert row["end_date"] == "2026-08-25", "an until-DATE-shaped pause resumed early must truncate to yesterday too"
        assert pause.is_paused(db, config, OWNER, "gym", date(2026, 8, 22))
        assert not pause.is_paused(db, config, OWNER, "gym", TODAY)

    async def test_truncate_is_per_user_isolated(self, db, config, registry):
        """A's early-resume truncate must never touch B's identical,
        independently-owned pause row for the same habit."""
        db.insert_pause(OWNER, "gym", "2026-08-22", "2026-09-05")
        db.insert_pause(OTHER, "gym", "2026-08-22", "2026-09-05")
        await _resume(db, config, registry, "/resume gym", user_id=OWNER)
        owner_row = db.active_pauses(OWNER)[0]
        other_row = db.active_pauses(OTHER)[0]
        assert owner_row["end_date"] == "2026-08-25"
        assert other_row["end_date"] == "2026-09-05", "B's row must be untouched by A's truncate"

    async def test_zero_elapsed_day_resume_still_deletes_outright_per_archis_ruling(self, db, config, registry):
        """A pause that started TODAY (zero elapsed days) must still be
        fully deleted on resume, not left as a same-day truncated row --
        Archi's explicit round-2 ruling."""
        await _pause(db, config, registry, "/pause gym 5d")  # starts TODAY (2026-08-26)
        await _resume(db, config, registry, "/resume gym")
        assert db.active_pauses(OWNER) == [], "a same-day pause must be deleted outright on resume, not truncated"


# ===========================================================================
# AC3-style gate at this module's own level: with zero pause rows ever
# written for a user, compute_streak's daily walk is exactly the
# pre-v1.9 "consecutive qualifying days, break on first gap" arithmetic
# -- this module contributes zero rows unless /pause is actually invoked.
# ===========================================================================


class TestNoPauseRowsEngineUnchanged:
    def test_zero_pause_rows_daily_walk_is_plain_consecutive_count(self, db, config, registry):
        habit = registry.get("gym")
        for day in ("2026-08-24", "2026-08-25", "2026-08-26"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        # No pause rows for this user AT ALL.
        assert db.active_pauses(OWNER) == []
        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 26), OWNER)
        assert streak == 3

    def test_zero_pause_rows_a_real_gap_still_breaks_normally(self, db, config, registry):
        habit = registry.get("gym")
        _seed(db, "2026-08-20T09:00:00", "gym", 1.0)
        # 08-21/08-22 unlogged, no pause -- genuine miss.
        for day in ("2026-08-23", "2026-08-24"):
            _seed(db, f"{day}T09:00:00", "gym", 1.0)
        streak = streaks.compute_streak(db, config, habit, date(2026, 8, 24), OWNER)
        assert streak == 2


# ===========================================================================
# Audit rows -- sane old/new values, both directions, rendered correctly.
# ===========================================================================


class TestAuditRowSanity:
    async def test_first_ever_pause_set_has_no_previous_end_as_old_value(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        row = db._conn.execute(
            "SELECT * FROM audit_log WHERE action = 'pause_set' AND entity = 'gym'"
        ).fetchone()
        assert row["old_value"] is None
        assert row["new_value"] == "2026-08-30"

    async def test_pause_clear_audit_row_has_a_sane_positive_old_value(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        await _resume(db, config, registry, "/resume gym")
        row = db._conn.execute(
            "SELECT * FROM audit_log WHERE action = 'pause_clear' AND entity = 'gym'"
        ).fetchone()
        assert row is not None
        assert int(row["old_value"]) >= 1

    async def test_pause_clear_renders_with_a_bilingual_label_in_audit_view(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        await _resume(db, config, registry, "/resume gym")
        rendered_en = audit_view.render_recent(db, config, "en", limit=5, owner_chat_id=OWNER)
        rendered_th = audit_view.render_recent(db, config, "th", limit=5, owner_chat_id=OWNER)
        assert rendered_en and rendered_th
        assert "gym" in rendered_en

    async def test_resume_all_audit_row_entity_is_all_not_a_specific_habit(self, db, config, registry):
        await _pause(db, config, registry, "/pause gym 5d")
        await _pause(db, config, registry, "/pause diary 5d")
        await _resume(db, config, registry, "/resume")
        rows = db._conn.execute("SELECT * FROM audit_log WHERE action = 'pause_clear'").fetchall()
        assert len(rows) == 1
        assert rows[0]["entity"] == "all"


# ===========================================================================
# Thai matcher zero-FP -- wider adversarial corpus, especially around
# `ต่อ` (an extremely common two-character Thai word for
# "continue/next/versus"), plus confirmation that the valid forms
# (including `ต่อ <habit>`, which Luna's own suite never exercises) DO
# dispatch.
# ===========================================================================


class TestThaiMatcherAdversarialCorpus:
    @pytest.mark.parametrize(
        "text",
        [
            "ต่อรองราคาหน่อย",  # "negotiate the price" -- glued, no space after ต่อ
            "การประชุมต่อจากนี้จะเริ่มบ่ายโมง",  # "ต่อ" mid-sentence, not at the trigger anchor
            "ต่อไปนี้คือขั้นตอน",  # "the following are the steps" -- glued
            "อย่าลืมกินข้าวต่อด้วยนะ",  # "ต่อ" mid-word/mid-sentence
            "พักผ่อนก่อนนะ",  # "go rest" -- glued, no space after พัก
            "พัก 5 นาที",  # "rest 5 minutes" -- has a space, but no real duration/habit shape
            "หยุดพัก 5 นาที",  # same, with the longer trigger word
            "ต่อ รถยนต์",  # "continue car" -- not a registered habit
            "กลับมาแล้วค่ะ ดีใจจัง",  # "[I'm] back, so happy" -- glued to แล้ว
            "ต่อ ไป อีกหน่อย",  # multi-token tail, not a single habit name
        ],
    )
    def test_ordinary_thai_prose_never_dispatches(self, registry, text):
        assert commands.dispatch(text, registry) is None, f"{text!r} should NOT dispatch but did"

    def test_tor_plus_gym_dispatches_as_resume(self, registry):
        # The single highest-risk valid form: the bare two-character
        # trigger `ต่อ` immediately followed by a real habit label.
        cmd = commands.dispatch("ต่อ ยิม", registry)
        assert cmd is not None and cmd.kind == "resume" and cmd.category == "gym"

    def test_tor_plus_diary_dispatches_as_resume(self, registry):
        cmd = commands.dispatch("ต่อ ไดอารี่", registry)
        assert cmd is not None and cmd.kind == "resume" and cmd.category == "diary"

    def test_yudpak_plus_gym_and_duration_dispatches_as_pause(self, registry):
        cmd = commands.dispatch("หยุดพัก ยิม 5d", registry)
        assert cmd is not None and cmd.kind == "pause" and cmd.category == "gym" and cmd.pref_value == "5d"

    def test_bare_tor_alone_never_dispatches(self, registry):
        assert commands.dispatch("ต่อ", registry) is None

    def test_tor_with_trailing_punctuation_only_never_dispatches(self, registry):
        assert commands.dispatch("ต่อ...", registry) is None
