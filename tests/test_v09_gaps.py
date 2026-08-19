"""Vera's supplementary gap tests for ROADMAP.md v0.9.0 "Adaptive Reminders,
Snooze & Quiet Hours" (AC9.1-AC9.5), on top of Luna's own
`tests/test_adaptive_reminders.py` (14 tests) and `tests/test_commands.py`'s
v0.9.0 section (27 tests) -- see `IMPL.md`'s "v0.9.0" section for what Luna
already covered.

Luna's own suite proves the adaptive checks and snooze at the *unit* level
(`send_reminder(channel, habit, lang, db=..., config=...)` called directly,
and `_execute_snooze` exercised through a `_FakeSnoozeScheduler` that only
records `add_job` calls). This file targets specifically the gaps requested
by the test brief -- the REAL send/scheduling path, boundary combinations
Luna didn't hit, and an explicit false-positive/precedence/backward-compat
audit:

  1. AC9.1: goal-met skip through `schedule_reminders`'s real registered
     job (`scheduler.get_job(...)` then `await job.func(*job.args)`, not a
     direct `send_reminder(...)` call) -- skip is both silent AND logged;
     goal-unmet sends; exactly-at-goal is documented (IMPL.md / code:
     `total >= habit.goal`) as met, held to that.
  2. AC9.2: midnight-crossing window suppresses 23:30/06:30, not 12:00,
     through the real job path; multiple simultaneous windows; window
     boundary (`[start, end)` -- start inclusive, end exclusive, per
     `core/reminders.py:_in_quiet_hours`'s own docstring); the snoozed
     one-off ALSO suppressed if its fire moment lands in quiet hours.
  3. AC9.3: snooze targets the most recently *fired* reminder, not the most
     recently *logged* habit (a plain diary log for a different habit must
     not steal the snooze target); the scheduled job is a genuine one-shot
     -- fires once, then is gone from `scheduler.get_jobs()`.
  4. AC9.4: `skip_if_goal_met = false` disables the skip for that habit only
     while a second, still-`true` habit in the same registry keeps skipping.
  5. AC9.5: a DB read raising mid-check still sends (fail-open) and logs;
     no exception escapes a real scheduler job; adaptive checks perform
     zero DB writes; the scheduler keeps processing other jobs afterward.
  6. Audit: `send_reminder`'s pre-v0.9 3-positional-arg call is pinned
     against the same i18n catalog text v0.8.0 produced, across every habit
     shape (all 3 built-ins, a custom `reminder_text` habit, a type-generic
     fallback habit) -- even when the omitted `db` would show "goal met".
  7. False-positive sweep: "เลื่อนเวลานัดหมอ" (postpone a doctor's
     appointment) and "I snoozed my alarm today" as ordinary diary-shaped
     messages must NOT be classified as `snooze` and must still reach the
     parser exactly once.

Same fixture/fake conventions as `tests/test_adaptive_reminders.py` and
`tests/test_commands.py` (real on-disk sqlite `Database` in `tmp_path`,
`Config()` defaults for the water/stretch/diary registry, a real
`AsyncIOScheduler` where the brief specifically asks for the real send
path).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config, QuietHoursConfig
from habit_assistant.core import commands, i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import ReminderState, schedule_reminders, send_reminder
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import _execute_snooze, handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message) -> None:
        raise NotImplementedError("not exercised in these tests")


class _NeverCalledLLM:
    async def extract(self, *args, **kwargs):
        raise AssertionError("LLM must not be called")

    async def health_check(self):
        raise AssertionError("LLM must not be called")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def fixed_clock():
    # Wednesday 2026-08-19, 14:30 local -- well clear of any test's quiet
    # hours window unless the test itself configures one that covers it.
    def clock():
        return datetime(2026, 8, 19, 14, 30, 0)

    return clock


def _seed(db: Database, ts: str, category: str, value_num: float, habit_type: str = "numeric") -> None:
    db.insert_log(LogEntry(None, ts, category, value_num, None, ts, "reply", habit_type=habit_type))


def _raw_row_count(db: Database, table: str = "logs") -> int:
    return db._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


class _FixedDatetime(datetime):
    """Same technique as `tests/test_adaptive_reminders.py`'s
    `_FixedDatetime` -- a `datetime` subclass whose `.now(tz)` always
    returns a fixed wall-clock moment, monkeypatched onto
    `habit_assistant.core.reminders.datetime` so quiet-hours suppression is
    deterministic regardless of when the suite actually runs."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.replace(tzinfo=tz) if tz is not None else cls._fixed


def _freeze_reminders_clock(monkeypatch, hour: int, minute: int) -> None:
    fixed = _FixedDatetime(2026, 8, 19, hour, minute, 0)
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": fixed})
    monkeypatch.setattr("habit_assistant.core.reminders.datetime", frozen)


# ===========================================================================
# AC9.1 -- goal-met skip through the REAL scheduled-job path (not a direct
# send_reminder(...) call): schedule_reminders binds db/config/state into
# the job's args exactly as production main.py does; the job is fetched
# from the scheduler and invoked the way APScheduler itself would.
# ===========================================================================


async def test_goal_met_reminder_skipped_via_real_scheduled_job_and_logged(db, caplog):
    _seed(db, "2026-08-19T09:00:00", "water", 3000.0)  # over the 2500ml default goal
    config = Config()
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    state = ReminderState()

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, db, state=state)
    job = scheduler.get_job("reminder_water_08:00")
    assert job is not None

    with caplog.at_level(logging.INFO, logger="habit_assistant.core.reminders"):
        await job.func(*job.args)

    assert channel.sent == []
    assert state.last_habit_id is None  # a suppressed reminder is not a snooze target
    assert any("goal already met" in rec.message for rec in caplog.records)


async def test_goal_not_met_reminder_sent_via_real_scheduled_job(db):
    _seed(db, "2026-08-19T09:00:00", "water", 500.0)  # under the 2500ml default goal
    config = Config()
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    state = ReminderState()

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, db, state=state)
    job = scheduler.get_job("reminder_water_08:00")

    await job.func(*job.args)

    assert channel.sent == [i18n.t("reminder_water", i18n.resolve_unprompted_language(config))]
    assert state.last_habit_id == "water"


async def test_goal_exactly_met_is_skipped_via_real_scheduled_job_matching_documented_ge(db, caplog):
    """IMPL.md / code (`_goal_already_met`): `total >= habit.goal` -- held
    to the documented ">=" contract, not re-derived independently."""
    _seed(db, "2026-08-19T09:00:00", "water", 2500.0)  # exactly the goal
    config = Config()
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, db)
    job = scheduler.get_job("reminder_water_08:00")

    with caplog.at_level(logging.INFO, logger="habit_assistant.core.reminders"):
        await job.func(*job.args)

    assert channel.sent == []
    assert any("goal already met" in rec.message for rec in caplog.records)


# ===========================================================================
# AC9.2 -- quiet-hours suppression through the real job path; midnight
# crossing, multiple windows, half-open boundary, and the snoozed follow-up.
# ===========================================================================


@pytest.mark.parametrize(
    "hour,minute,expect_suppressed",
    [
        (23, 30, True),  # late night, inside the crossing window
        (6, 30, True),  # early morning, inside the crossing window
        (12, 0, False),  # broad daylight, outside
    ],
)
async def test_midnight_crossing_window_suppresses_only_inside_via_real_job(
    monkeypatch, db, hour, minute, expect_suppressed
):
    config = Config(quiet_hours=QuietHoursConfig(windows=[("23:00", "07:00")]))
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    _freeze_reminders_clock(monkeypatch, hour, minute)

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, db)
    job = scheduler.get_job("reminder_water_08:00")
    await job.func(*job.args)

    if expect_suppressed:
        assert channel.sent == []
    else:
        assert channel.sent == [i18n.t("reminder_water", i18n.resolve_unprompted_language(config))]


async def test_multiple_quiet_hours_windows_each_suppress_independently(monkeypatch, db):
    config = Config(quiet_hours=QuietHoursConfig(windows=[("13:00", "14:00"), ("23:00", "07:00")]))
    channel = FakeChannel()

    for hour, minute, expect_suppressed in [(13, 30, True), (23, 30, True), (3, 0, True), (16, 0, False)]:
        channel.sent.clear()
        scheduler = AsyncIOScheduler()
        _freeze_reminders_clock(monkeypatch, hour, minute)
        schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, db)
        job = scheduler.get_job("reminder_water_08:00")
        await job.func(*job.args)
        if expect_suppressed:
            assert channel.sent == [], f"expected suppressed at {hour:02d}:{minute:02d}"
        else:
            assert channel.sent != [], f"expected sent at {hour:02d}:{minute:02d}"


@pytest.mark.parametrize(
    "hour,minute,expect_suppressed",
    [
        (13, 0, True),  # start is inclusive
        (12, 59, False),  # just before start -- not suppressed
        (14, 0, False),  # end is exclusive
        (13, 59, True),  # just before end -- still suppressed
    ],
)
async def test_same_day_window_boundary_is_half_open_via_real_job(monkeypatch, db, hour, minute, expect_suppressed):
    config = Config(quiet_hours=QuietHoursConfig(windows=[("13:00", "14:00")]))
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    _freeze_reminders_clock(monkeypatch, hour, minute)

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, db)
    job = scheduler.get_job("reminder_water_08:00")
    await job.func(*job.args)

    assert (channel.sent == []) is expect_suppressed


async def test_snoozed_followup_is_also_suppressed_when_it_lands_in_quiet_hours(monkeypatch, db, fixed_clock):
    """AC9.3 says the follow-up is `send_reminder` itself -- so it must
    honor quiet hours exactly like a cron-triggered reminder. Snooze while
    NOT in quiet hours (14:30), but freeze the reminders-module clock so
    that by the time the scheduled job actually executes, it reads as
    23:30 (inside the configured night window)."""
    config = Config(quiet_hours=QuietHoursConfig(windows=[("23:00", "07:00")]))
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    state = ReminderState()
    state.last_habit_id = "water"

    await _execute_snooze(
        db, channel, config, fixed_clock, commands.Command(kind="snooze", minutes=30),
        DEFAULT_REGISTRY, "en", scheduler, state, dry_run=False,
    )
    assert channel.sent == [i18n.t("snooze_confirmed", "en", minutes=30, label="water")]

    job = scheduler.get_jobs()[0]
    _freeze_reminders_clock(monkeypatch, 23, 30)  # the moment the follow-up actually fires
    await job.func(*job.args)

    # Only the snooze confirmation was ever sent -- the follow-up reminder
    # text itself was suppressed by quiet hours.
    assert channel.sent == [i18n.t("snooze_confirmed", "en", minutes=30, label="water")]


# ===========================================================================
# AC9.3 -- snooze targets the most recently FIRED reminder (ReminderState),
# never the most recently logged habit; the scheduled job is a genuine
# one-shot (fires once, then gone).
# ===========================================================================


async def test_snooze_targets_most_recently_fired_reminder_not_most_recently_logged_habit(db, fixed_clock):
    state = ReminderState()
    channel = FakeChannel()

    # water's reminder actually fires (updates state)...
    await send_reminder(channel, DEFAULT_REGISTRY.get("water"), "en", state=state)
    assert state.last_habit_id == "water"

    # ...then the user logs a completely unrelated stretch entry via the
    # normal inbound-message path. This must NOT change the snooze target --
    # ReminderState only tracks fired reminders, not arbitrary DB writes.
    await handle_inbound_message(
        "did 10 min stretch", db=db, llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=fixed_clock,
        reminder_state=state,
    )
    assert state.last_habit_id == "water"  # unchanged by the plain log

    scheduler = AsyncIOScheduler()
    await handle_inbound_message(
        "snooze 30", db=db, llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=fixed_clock,
        scheduler=scheduler, reminder_state=state,
    )

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    scheduled_habit = jobs[0].args[1]
    assert scheduled_habit.id == "water"  # targets water, not the just-logged stretch


async def test_snooze_scheduled_job_fires_once_and_is_removed_from_scheduler(db):
    """The follow-up job must be a genuine one-shot: it fires, and
    APScheduler drops it from the job store afterward -- it must not
    recur. Uses a `clock` far enough in the past that `run_date` (clock() +
    30 min) lands ~300ms after `scheduler.start()`, well inside the
    default misfire grace window, so this doesn't depend on real wall-clock
    waiting for 30 minutes."""
    config = Config()
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    state = ReminderState()
    state.last_habit_id = "water"
    near_future_clock = lambda: datetime.now() - timedelta(minutes=30) + timedelta(milliseconds=300)

    await _execute_snooze(
        db, channel, config, near_future_clock, commands.Command(kind="snooze", minutes=30),
        DEFAULT_REGISTRY, "en", scheduler, state, dry_run=False,
    )
    jobs_before = scheduler.get_jobs()
    assert len(jobs_before) == 1
    job_id = jobs_before[0].id

    scheduler.start()
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if scheduler.get_job(job_id) is None:
                break
        assert scheduler.get_job(job_id) is None, "one-shot snooze job should be gone after firing"
        # AsyncIOScheduler removes a one-shot job from its store the moment
        # it hands the coroutine off to run, which can race slightly ahead
        # of the coroutine's own completion -- give it a beat to finish
        # before asserting on its side effect (channel.sent).
        await asyncio.sleep(0.2)
    finally:
        scheduler.shutdown(wait=False)

    # the follow-up actually executed send_reminder (not just vanished) --
    # an unprompted send resolves its own language independently of the
    # snooze confirmation's `lang` param (i18n.resolve_unprompted_language,
    # "th" by default), so check for that, not the "en" confirmation.
    assert i18n.t("reminder_water", i18n.resolve_unprompted_language(config)) in channel.sent


# ===========================================================================
# AC9.4 -- per-habit override: one habit's skip_if_goal_met=False disables
# adaptive skipping for THAT habit only, while a second habit in the same
# registry (skip_if_goal_met left at its True default) still skips.
# ===========================================================================


async def test_skip_if_goal_met_false_disables_only_that_habit_others_still_skip(db):
    config = Config.model_validate(
        {
            "habits": [
                {
                    "id": "water",
                    "type": "numeric",
                    "goal": 2500,
                    "reminder_times": ["08:00"],
                    "label": {"en": "water", "th": "น้ำ"},
                    "unit": {"en": "ml", "th": "มล."},
                    "skip_if_goal_met": False,  # override: never skip water
                },
                {
                    "id": "sleep",
                    "type": "numeric",
                    "goal": 8,
                    "reminder_times": ["07:00"],
                    "label": {"en": "sleep", "th": "นอน"},
                    "unit": {"en": "hr", "th": "ชม."},
                    # skip_if_goal_met defaults True
                },
            ]
        }
    )
    registry = HabitRegistry.from_config(config)
    _seed(db, "2026-08-19T06:00:00", "water", 3000.0)  # over goal
    _seed(db, "2026-08-19T06:00:00", "sleep", 9.0)  # over goal
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()

    schedule_reminders(scheduler, channel, config, registry, db)

    water_job = scheduler.get_job("reminder_water_08:00")
    sleep_job = scheduler.get_job("reminder_sleep_07:00")

    await water_job.func(*water_job.args)
    await sleep_job.func(*sleep_job.args)

    lang = i18n.resolve_unprompted_language(config)
    # water (skip_if_goal_met=False override) sent despite being over goal;
    # sleep (default True) stayed silent because its goal is also met.
    assert channel.sent == [i18n.t("reminder_water", lang)]


# ===========================================================================
# AC9.5 -- fail-open on a DB read error via the real scheduled-job path; no
# write ever happens from the adaptive checks; the scheduler keeps working
# afterward (this job's failure doesn't poison other jobs).
# ===========================================================================


class _RaisingDatabase:
    def sum_value(self, habit_id: str, day: str) -> float:
        import sqlite3

        raise sqlite3.OperationalError("database is locked")


async def test_db_read_raises_mid_check_reminder_still_sent_via_real_job_and_logged(caplog):
    config = Config()
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    raising_db = _RaisingDatabase()

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, raising_db)
    job = scheduler.get_job("reminder_water_08:00")

    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.reminders"):
        await job.func(*job.args)  # must not raise -- a crashing job is exactly what AC9.5 forbids

    assert channel.sent == [i18n.t("reminder_water", i18n.resolve_unprompted_language(config))]
    assert any("goal read failed" in rec.message for rec in caplog.records)


async def test_adaptive_checks_perform_zero_db_writes(monkeypatch, db):
    _seed(db, "2026-08-19T09:00:00", "water", 500.0)
    config = Config()
    channel = FakeChannel()
    rows_before = _raw_row_count(db)

    write_calls: list[str] = []
    for method_name in ("insert_log", "soft_delete", "update_value", "reclassify_log"):
        original = getattr(db, method_name)

        def _spy(*args, _name=method_name, _orig=original, **kwargs):
            write_calls.append(_name)
            return _orig(*args, **kwargs)

        monkeypatch.setattr(db, method_name, _spy)

    await send_reminder(channel, DEFAULT_REGISTRY.get("water"), "en", db=db, config=config)

    assert write_calls == []  # the goal-met read touched no write method
    assert _raw_row_count(db) == rows_before


async def test_scheduler_keeps_processing_other_jobs_after_one_jobs_db_read_raises():
    """One job's DB hiccup must not poison the scheduler for other jobs --
    fail-open is per-job, not global."""
    config = Config()
    channel = FakeChannel()
    scheduler = AsyncIOScheduler()
    raising_db = _RaisingDatabase()

    schedule_reminders(scheduler, channel, config, DEFAULT_REGISTRY, raising_db)
    water_job = scheduler.get_job("reminder_water_08:00")
    stretch_job = scheduler.get_job("reminder_stretch_11:00")

    await water_job.func(*water_job.args)  # raises internally on the DB read, fails open
    await stretch_job.func(*stretch_job.args)  # a completely independent job, unaffected

    assert i18n.t("reminder_water", i18n.resolve_unprompted_language(config)) in channel.sent
    assert i18n.t("reminder_stretch", i18n.resolve_unprompted_language(config)) in channel.sent


# ===========================================================================
# Audit -- send_reminder's pre-v0.9 3-positional-arg call is byte-identical
# to v0.8.0 output, pinned across every habit shape, even when the omitted
# db would otherwise show "goal already met".
# ===========================================================================


async def test_send_reminder_three_arg_call_matches_v080_catalog_text_for_every_habit_shape(db):
    _seed(db, "2026-08-19T09:00:00", "water", 9999.0)  # would be "goal met" if db were passed
    channel = FakeChannel()

    custom_habit = Habit(
        id="sleep",
        type="numeric",
        label_en="sleep",
        label_th="นอน",
        unit_en="hr",
        unit_th="ชม.",
        goal=8,
        reminder_times=("07:00",),
        reminder_text_en="😴 How many hours did you sleep?",
        reminder_text_th="😴 เมื่อคืนนอนกี่ชั่วโมง?",
        unit_aliases={},
    )
    generic_habit = Habit(
        id="mood",
        type="text",
        label_en="mood",
        label_th="อารมณ์",
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=("21:00",),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )

    for habit, expected_en in [
        (DEFAULT_REGISTRY.get("water"), i18n.t("reminder_water", "en")),
        (DEFAULT_REGISTRY.get("stretch"), i18n.t("reminder_stretch", "en")),
        (DEFAULT_REGISTRY.get("diary"), i18n.t("reminder_diary", "en")),
        (custom_habit, "😴 How many hours did you sleep?"),
        (generic_habit, i18n.t("reminder_generic", "en", label="mood")),
    ]:
        channel.sent.clear()
        await send_reminder(channel, habit, "en")  # 3 positional args only -- no db, no config
        assert channel.sent == [expected_en]


# ===========================================================================
# False-positive sweep -- snooze phrases embedded in ordinary diary-shaped
# prose must not be classified as the "snooze" command, and must still
# reach the parser exactly once (same guard as AC5.5's adversarial corpus,
# extended with the two cases the test brief calls out by name).
# ===========================================================================

SNOOZE_FALSE_POSITIVE_MESSAGES = [
    "เลื่อนเวลานัดหมอ",  # "postpone the doctor's appointment" -- contains เลื่อน mid-sentence
    "I snoozed my alarm today",  # diary entry mentioning "snoozed", not a command
    "ขอเลื่อนประชุมพรุ่งนี้ด้วยครับ",  # "please postpone tomorrow's meeting" -- เลื่อน mid-sentence
    "just hit snooze on my phone alarm twice",  # "snooze" mid-sentence, not anchored
]


@pytest.mark.parametrize("message", SNOOZE_FALSE_POSITIVE_MESSAGES)
def test_snooze_matcher_does_not_fire_on_diary_shaped_prose(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "snooze"


@pytest.mark.parametrize("message", SNOOZE_FALSE_POSITIVE_MESSAGES)
async def test_snooze_false_positives_still_reach_the_parser_exactly_once(db, fixed_clock, monkeypatch, message):
    channel = FakeChannel()
    config = Config()
    calls: list[str] = []

    async def counting_parse_message(text, llm, registry, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult.unknown()

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message(
        message, db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock,
        scheduler=AsyncIOScheduler(), reminder_state=ReminderState(),
    )

    assert calls == [message]


def test_dispatch_precedence_undo_edit_before_snooze_before_query():
    """Code-level confirmation of core/commands.py's stated routing order
    (undo -> edit -> snooze -> query): none of undo/edit/query's own trigger
    sets overlap snooze's, so this is a structural guard against a future
    reordering silently breaking precedence, not a test of an achievable
    ambiguous case today."""
    assert commands.dispatch("/undo", DEFAULT_REGISTRY).kind == "undo"
    assert commands.dispatch("make that 250ml", DEFAULT_REGISTRY).kind == "edit"
    assert commands.dispatch("snooze 30", DEFAULT_REGISTRY).kind == "snooze"
    assert commands.dispatch("how much water this week?", DEFAULT_REGISTRY).kind == "query"
