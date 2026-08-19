"""Adaptive reminders / quiet hours tests (ROADMAP.md v0.9.0 "Adaptive
Reminders, Snooze & Quiet Hours", AC9.1/AC9.2/AC9.4/AC9.5).

`core/reminders.py`'s `send_reminder` gained two opt-in checks, both no-ops
(byte-identical to v0.7.0/v0.8.0 behavior, see tests/test_reminders.py)
unless the caller passes `db`/`config`: quiet-hours suppression (AC9.2,
including a midnight-crossing window) and a goal-met skip for goal-bearing
habits (AC9.1), fail-open on a DB read error (AC9.5), and overridable per
habit via `skip_if_goal_met` (AC9.4)."""

from __future__ import annotations

from datetime import datetime, time
from typing import Awaitable, Callable

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config, QuietHoursConfig
from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import ReminderState, _in_quiet_hours, send_reminder
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError


def _water_habit(goal: float | None = 2500, skip_if_goal_met: bool = True) -> Habit:
    return HabitRegistry.from_config(
        Config.model_validate(
            {
                "habits": [
                    {
                        "id": "water",
                        "type": "numeric",
                        "goal": goal,
                        "label": {"en": "water", "th": "น้ำ"},
                        "unit": {"en": "ml", "th": "มล."},
                        "skip_if_goal_met": skip_if_goal_met,
                    }
                ]
            }
        )
    ).get("water")


class _RaisingDatabase:
    """Stands in for `Database` to prove AC9.5's fail-open contract without
    needing to actually corrupt a real SQLite file."""

    def sum_value(self, habit_id: str, day: str) -> float:
        raise sqlite3_error()


def sqlite3_error() -> Exception:
    import sqlite3

    return sqlite3.OperationalError("database is locked")


# ===========================================================================
# AC9.2 -- quiet-hours suppression, incl. a window crossing midnight.
# ===========================================================================


def test_in_quiet_hours_same_day_window():
    windows = [("13:00", "14:00")]
    assert _in_quiet_hours(time(13, 30), windows) is True
    assert _in_quiet_hours(time(12, 59), windows) is False
    assert _in_quiet_hours(time(14, 0), windows) is False  # end is exclusive


def test_in_quiet_hours_midnight_crossing_window():
    windows = [("23:00", "07:00")]
    assert _in_quiet_hours(time(23, 30), windows) is True  # late night
    assert _in_quiet_hours(time(3, 0), windows) is True  # early morning
    assert _in_quiet_hours(time(7, 0), windows) is False  # end is exclusive
    assert _in_quiet_hours(time(12, 0), windows) is False  # broad daylight


def test_in_quiet_hours_no_windows_never_suppresses():
    assert _in_quiet_hours(time(23, 30), []) is False


class _FixedDatetime(datetime):
    """A `datetime` subclass whose `.now(tz)` always returns a fixed wall-
    clock moment, so quiet-hours suppression can be tested deterministically
    regardless of when the suite actually runs."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.replace(tzinfo=tz) if tz is not None else cls._fixed


def _freeze_reminders_clock(monkeypatch, hour: int, minute: int) -> None:
    fixed = _FixedDatetime(2026, 8, 19, hour, minute, 0)
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": fixed})
    monkeypatch.setattr("habit_assistant.core.reminders.datetime", frozen)


async def test_send_reminder_suppressed_inside_quiet_hours_window(monkeypatch):
    _freeze_reminders_clock(monkeypatch, 23, 30)  # inside the window below
    habit = _water_habit()
    channel = FakeChannel()
    config = Config(quiet_hours=QuietHoursConfig(windows=[("23:00", "07:00")]))

    await send_reminder(channel, habit, "en", db=None, config=config)

    assert channel.sent == []


async def test_send_reminder_not_suppressed_outside_quiet_hours_window(monkeypatch):
    _freeze_reminders_clock(monkeypatch, 12, 0)  # outside the window below
    habit = _water_habit()
    channel = FakeChannel()
    config = Config(quiet_hours=QuietHoursConfig(windows=[("23:00", "07:00")]))

    await send_reminder(channel, habit, "en", db=None, config=config)

    assert channel.sent == [i18n.t("reminder_water", "en")]


# ===========================================================================
# AC9.1 / AC9.4 -- goal-met skip, per-habit overridable.
# ===========================================================================


def _seed_water(db: Database, ml: float, ts: str = "2026-08-19T09:00:00") -> None:
    db.insert_log(LogEntry(None, ts, "water", ml, None, f"{ml}ml", "reply"))


async def test_send_reminder_skipped_when_goal_already_met(tmp_path):
    db = Database(tmp_path / "habits.db")
    try:
        _seed_water(db, 3000.0)  # over the 2500ml goal
        habit = _water_habit(goal=2500)
        channel = FakeChannel()
        config = Config()

        await send_reminder(channel, habit, "en", db=db, config=config)

        assert channel.sent == []
    finally:
        db.close()


async def test_send_reminder_sent_when_goal_not_met(tmp_path):
    db = Database(tmp_path / "habits.db")
    try:
        _seed_water(db, 500.0)  # under the 2500ml goal
        habit = _water_habit(goal=2500)
        channel = FakeChannel()
        config = Config()

        await send_reminder(channel, habit, "en", db=db, config=config)

        assert channel.sent == [i18n.t("reminder_water", "en")]
    finally:
        db.close()


async def test_send_reminder_goal_exactly_met_is_skipped(tmp_path):
    db = Database(tmp_path / "habits.db")
    try:
        _seed_water(db, 2500.0)  # exactly the goal
        habit = _water_habit(goal=2500)
        channel = FakeChannel()

        await send_reminder(channel, habit, "en", db=db, config=Config())

        assert channel.sent == []
    finally:
        db.close()


async def test_send_reminder_skip_if_goal_met_false_disables_adaptive_skip(tmp_path):
    """AC9.4: `skip_if_goal_met = false` on the habit itself disables the
    skip, even though the goal is already met."""
    db = Database(tmp_path / "habits.db")
    try:
        _seed_water(db, 3000.0)
        habit = _water_habit(goal=2500, skip_if_goal_met=False)
        channel = FakeChannel()

        await send_reminder(channel, habit, "en", db=db, config=Config())

        assert channel.sent == [i18n.t("reminder_water", "en")]
    finally:
        db.close()


async def test_send_reminder_goal_check_skipped_for_habit_without_a_goal(tmp_path):
    """A goal-less habit (e.g. stretch) has nothing to compare -- the
    adaptive check is a no-op, reminder always sends regardless of today's
    activity."""
    db = Database(tmp_path / "habits.db")
    try:
        stretch = HabitRegistry.from_config(Config()).get("stretch")
        db.insert_log(LogEntry(None, "2026-08-19T09:00:00", "stretch", 30.0, None, "30 min stretch", "reply"))
        channel = FakeChannel()

        await send_reminder(channel, stretch, "en", db=db, config=Config())

        assert channel.sent == [i18n.t("reminder_stretch", "en")]
    finally:
        db.close()


# ===========================================================================
# AC9.5 -- fail-open: a DB read error never suppresses a reminder, and
# never crashes/raises out of send_reminder (a scheduler job must survive).
# ===========================================================================


async def test_send_reminder_fails_open_when_db_read_raises():
    habit = _water_habit(goal=2500)
    channel = FakeChannel()
    config = Config()
    raising_db = _RaisingDatabase()

    # Must not raise -- a scheduler job crashing is exactly what AC9.5 forbids.
    await send_reminder(channel, habit, "en", db=raising_db, config=config)

    assert channel.sent == [i18n.t("reminder_water", "en")]


# ===========================================================================
# Pre-v0.9 backward compatibility: send_reminder(channel, habit, lang) with
# no db/config is byte-identical to v0.7.0/v0.8.0 -- no adaptive checks run.
# ===========================================================================


async def test_send_reminder_without_db_or_config_is_unaffected_by_adaptive_checks(tmp_path):
    db = Database(tmp_path / "habits.db")
    try:
        _seed_water(db, 5000.0)  # would be "goal met" if db were passed
        habit = _water_habit(goal=2500)
        channel = FakeChannel()

        await send_reminder(channel, habit, "en")  # no db, no config

        assert channel.sent == [i18n.t("reminder_water", "en")]
    finally:
        db.close()


# ===========================================================================
# ReminderState -- tracks the habit whose reminder most recently fired
# (ROADMAP.md v0.9.0's own "target habit" rule for a bare snooze command).
# ===========================================================================


async def test_send_reminder_updates_state_only_when_actually_sent(tmp_path):
    db = Database(tmp_path / "habits.db")
    try:
        _seed_water(db, 3000.0)  # goal met -> suppressed
        habit = _water_habit(goal=2500)
        channel = FakeChannel()
        state = ReminderState()

        await send_reminder(channel, habit, "en", db=db, config=Config(), state=state)

        assert channel.sent == []
        assert state.last_habit_id is None  # a suppressed reminder must not update state
    finally:
        db.close()


async def test_send_reminder_updates_state_when_sent():
    habit = _water_habit(goal=2500)
    channel = FakeChannel()
    state = ReminderState()

    await send_reminder(channel, habit, "en", state=state)

    assert channel.sent == [i18n.t("reminder_water", "en")]
    assert state.last_habit_id == "water"
