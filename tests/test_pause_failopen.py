"""SPEC-v1.10.md §4 R18/R-SS9 (module `riders`): fail-open unification at
the 5 pause-gating proactive sites -- `reminders.send_reminder` (already
fail-open, and already adopted `pause.is_paused_safe` in the shared-surface
pass, IMPL-v1.10-shared.md), `checkins.build_checkin_message`, `nudge.
build_nudge_message`, `streaks.compute_daily_summary`, and `review.
compute_weekly_stats` (+ its own `run_weekly_review` trends filter) now all
route their pauses-table read through `pause.is_paused_safe`/`pause.
active_pauses_safe` (R-SS9) instead of the raw `db.active_pauses`/`pause.
is_paused`.

AC16: a pauses-read error for one user (A) at each of the 5 sites must (a)
leave A treated as not-paused -- its content proceeds/the habit stays
eligible -- and (b) never abort the run for users B/C sharing the same
tick/job. Each site below is proven at two levels:

1. Directly -- the per-user builder/aggregator itself doesn't raise when
   its pauses read blows up, and treats the affected user/habit as
   not-paused (content proceeds, nothing is silently dropped).
2. Through the real fan-out loop -- `run_due_reminders`/`run_due_checkins`/
   `run_due_nudges` (all three owned by this module's file list, SPEC-
   v1.10.md §11) are called directly; `streaks.compute_daily_summary`/
   `review.compute_weekly_stats`/`review.run_weekly_review` are exercised
   through a LOCALLY MIRRORED loop with the exact uncaught
   `for user_id in ...: <single-user call>` shape `core/jobs.py:
   daily_summary_job`/`weekly_review_job` themselves use -- `core/jobs.py`
   is not an M3-owned file (§11 lists only `core/checkins.py`/`core/
   nudge.py`/`core/streaks.py`/`core/review.py`/`pyproject.toml`/this file),
   so this reproduces its iteration shape here rather than editing it. Since
   neither job wraps its per-user call in a try/except, the only way user
   B's/C's iteration is ever reached after A's is if A's own call doesn't
   raise -- exactly what these tests prove.

`_PartlyBrokenDb` is a thin `Database` subclass whose `active_pauses`
raises for exactly one configured user id; every other read/write
(including `active_pauses` for any OTHER user) passes straight through to
the real on-disk SQLite backing store, so these are genuine exercises of
the production code path, not fully-mocked unit tests."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import FakeOllamaClient
from conftest import RecordingChannel as FakeChannel

from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, nudge, review, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import run_due_reminders, send_reminder
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

USER_A = "user-a"  # the user whose pauses read is broken in every test below
USER_B = "user-b"
USER_C = "user-c"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class _PartlyBrokenDb(Database):
    """`Database`, except `active_pauses(user_id)` raises for exactly one
    configured user -- every other method, and `active_pauses` for any
    OTHER user, behaves exactly like the real backing store."""

    def __init__(self, path, raise_for_user: str) -> None:
        super().__init__(path)
        self.raise_for_user = raise_for_user

    def active_pauses(self, user_id: str):
        if user_id == self.raise_for_user:
            raise RuntimeError("simulated pauses-table read failure")
        return super().active_pauses(user_id)


@pytest.fixture
def db(tmp_path):
    database = _PartlyBrokenDb(tmp_path / "habits.db", raise_for_user=USER_A)
    for user_id in (USER_A, USER_B, USER_C):
        database.upsert_user(user_id, role="member", status="active")
    yield database
    database.close()


def _log(db_: Database, user_id: str, habit_id: str, value: float, ts: str = "2026-08-27T09:00:00") -> None:
    db_.insert_log(LogEntry(None, user_id, ts, habit_id, value, None, f"{value}", "reply"))


async def _enable_checkin(db_: Database, config: Config, user_id: str) -> None:
    await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db_, config=config, lang="en", user_id=user_id
    )


def _habit(id_: str, goal: float, label_en: str) -> Habit:
    return Habit(
        id=id_,
        type="numeric",
        label_en=label_en,
        label_th=label_en,
        unit_en="u",
        unit_th="u",
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


# Deliberately NOT id "water" -- `targets.config_goal` special-cases that
# exact id to the legacy `config.reminders.water.goal_ml` (mirrors tests/
# test_nudge.py's own documented reason for the same choice). Two habits so
# the nudge test below can prove a pauses-read failure doesn't drop the
# WHOLE user's nudge (every close habit, not just the ones the broken read
# happens not to touch).
TWO_CLOSE_HABIT_REGISTRY = HabitRegistry([_habit("juice", 1000.0, "juice"), _habit("protein", 100.0, "protein")])


# ===========================================================================
# Site 1 -- reminders.send_reminder (already adopted `is_paused_safe` in
# the shared-surface pass; re-verified here as the AC16 "reference posture"
# every other site is held to).
# ===========================================================================


async def test_send_reminder_pause_read_failure_treated_as_not_paused(db):
    config = Config()
    water = DEFAULT_REGISTRY.get("water")
    channel = FakeChannel()

    await send_reminder(channel, USER_A, water, "en", db=db, config=config)

    assert channel.sent_to(USER_A) != []


async def test_run_due_reminders_pause_read_failure_does_not_abort_the_tick_for_other_users(db):
    config = Config()
    water = DEFAULT_REGISTRY.get("water")
    hour, minute = (int(x) for x in water.reminder_times[0].split(":"))
    clock = lambda: datetime(2026, 8, 27, hour, minute, 0)  # noqa: E731
    channel = FakeChannel()

    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=clock)

    assert channel.sent_to(USER_A) != []  # fail-open: A's own reminder still sends
    assert channel.sent_to(USER_B) != []  # tick wasn't aborted before reaching B
    assert channel.sent_to(USER_C) != []  # ...or C


# ===========================================================================
# Site 2 -- checkins.build_checkin_message / run_due_checkins.
# ===========================================================================


def test_build_checkin_message_pause_read_failure_not_suppressed(db):
    config = Config()
    water = DEFAULT_REGISTRY.get("water")

    message = checkins.build_checkin_message(
        db, config, DEFAULT_REGISTRY, "en", USER_A, clock=lambda: datetime(2026, 8, 27, 9, 0, 0)
    )

    assert message is not None
    assert water.label("en") in message


async def test_run_due_checkins_pause_read_failure_does_not_abort_the_tick_for_other_users(db):
    config = Config()
    for user_id in (USER_A, USER_B, USER_C):
        await _enable_checkin(db, config, user_id)
    channel = FakeChannel()

    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=lambda: datetime(2026, 8, 27, 9, 0, 0))

    assert channel.sent_to(USER_A) != []
    assert channel.sent_to(USER_B) != []
    assert channel.sent_to(USER_C) != []


# ===========================================================================
# Site 3 -- nudge.build_nudge_message / run_due_nudges.
# ===========================================================================


def test_build_nudge_message_pause_read_failure_preserves_every_close_habit(db):
    """R18's own stated nuance for this site: a pauses-read failure must
    treat the affected habit as not-paused and still leave it a "close"
    candidate, rather than the exception blowing up the whole build and
    dropping the user's ENTIRE nudge (including close habits the failed
    read had nothing to do with) -- proven with two independently-close
    habits, both of which must survive."""
    config = Config()
    _log(db, USER_A, "juice", 900.0)  # 90% of 1000 -- close
    _log(db, USER_A, "protein", 90.0)  # 90% of 100 -- close

    message = nudge.build_nudge_message(
        db, config, TWO_CLOSE_HABIT_REGISTRY, "en", USER_A, clock=lambda: datetime(2026, 8, 27, 20, 0, 0)
    )

    assert message is not None
    assert "juice" in message.lower()
    assert "protein" in message.lower()


async def test_run_due_nudges_pause_read_failure_does_not_abort_the_tick_for_other_users(db):
    config = Config()
    for user_id in (USER_A, USER_B, USER_C):
        await _enable_checkin(db, config, user_id)  # nudge rides checkin enablement
        _log(db, user_id, "water", 2000.0)  # 2000/2500 = 80% -- close
    channel = FakeChannel()

    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=lambda: datetime(2026, 8, 27, 20, 0, 0))

    assert channel.sent_to(USER_A) != []
    assert channel.sent_to(USER_B) != []
    assert channel.sent_to(USER_C) != []


# ===========================================================================
# Site 4 -- streaks.compute_daily_summary (fan-out lives in
# core/jobs.py:daily_summary_job, not an M3-owned file -- see module
# docstring for why the loop below is a local mirror, not an import).
# ===========================================================================


def test_compute_daily_summary_pause_read_failure_treats_habit_as_not_paused(db):
    config = Config()

    lines = streaks.compute_daily_summary(db, config, DEFAULT_REGISTRY, date(2026, 8, 27), USER_A)

    assert "water" in [line.habit.id for line in lines]


def test_compute_daily_summary_pause_read_failure_does_not_abort_a_multi_user_fanout(db):
    config = Config()
    today = date(2026, 8, 27)

    # Mirrors core/jobs.py:daily_summary_job's own uncaught
    # `for user_id in db.active_user_ids(): streaks.run_daily_summary(...)`
    # loop shape: if USER_A's call raised, this `for` would never reach
    # USER_B/USER_C and the asserts below would never run.
    results = {}
    for user_id in (USER_A, USER_B, USER_C):
        results[user_id] = streaks.compute_daily_summary(db, config, DEFAULT_REGISTRY, today, user_id)

    assert set(results) == {USER_A, USER_B, USER_C}
    assert {line.habit.id for line in results[USER_B]} == {line.habit.id for line in results[USER_C]}


# ===========================================================================
# Site 5 -- review.compute_weekly_stats + run_weekly_review's own trends
# filter (fan-out lives in core/jobs.py:weekly_review_job, not an M3-owned
# file -- same rationale as site 4).
# ===========================================================================


def test_compute_weekly_stats_pause_read_failure_treats_habit_as_not_paused(db):
    config = Config()

    stats = review.compute_weekly_stats(db, config, DEFAULT_REGISTRY, date(2026, 8, 27), USER_A)

    assert "water" in [hs.habit.id for hs in stats.habits]


def test_compute_weekly_stats_pause_read_failure_does_not_abort_a_multi_user_fanout(db):
    config = Config()
    end_date = date(2026, 8, 27)

    # Mirrors core/jobs.py:weekly_review_job's own uncaught
    # `for user_id in db.active_user_ids(): ... run_weekly_review(...)`
    # loop shape (see site 4's identical rationale).
    results = {}
    for user_id in (USER_A, USER_B, USER_C):
        results[user_id] = review.compute_weekly_stats(db, config, DEFAULT_REGISTRY, end_date, user_id)

    assert set(results) == {USER_A, USER_B, USER_C}


async def test_run_weekly_review_trends_filter_pause_read_failure_does_not_raise(db):
    """The trends-block filter (`run_weekly_review`'s own `trends_registry`
    comprehension) is a second, independent `pause.is_paused_safe` call
    site in this module -- exercising the full async function (not just
    `compute_weekly_stats`) proves THAT call site is fixed too: if it still
    called the raw `pause.is_paused`, this would raise partway through and
    the function would never return text at all."""
    config = Config()
    _log(db, USER_A, "water", 2000.0, ts="2026-08-20T09:00:00")
    _log(db, USER_A, "water", 2100.0, ts="2026-08-27T09:00:00")

    text = await review.run_weekly_review(
        db, config, DEFAULT_REGISTRY, FakeOllamaClient(), "en", USER_A, today=date(2026, 8, 27)
    )

    assert text
    assert DEFAULT_REGISTRY.get("water").label("en") in text or "water" in text.lower()


async def test_run_weekly_review_pause_read_failure_does_not_abort_a_multi_user_fanout(db):
    config = Config()
    end_date = date(2026, 8, 27)
    llm = FakeOllamaClient()

    results = {}
    for user_id in (USER_A, USER_B, USER_C):
        results[user_id] = await review.run_weekly_review(db, config, DEFAULT_REGISTRY, llm, "en", user_id, today=end_date)

    assert set(results) == {USER_A, USER_B, USER_C}
    assert all(results.values())
