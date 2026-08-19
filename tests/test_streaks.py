"""Vera (tester) coverage for ROADMAP.md v0.10.0 "Streaks, Gentle
Gamification & Daily Summary", AC10.1-AC10.5. Luna wrote NO tests this
round (documented in IMPL.md) -- this file is the entire AC test suite.

AC -> section map:
- AC10.1 -> "Streak arithmetic (day_qualifies / compute_streak)"
- AC10.2 -> "Milestone crossing via the REAL handle_inbound_message"
- AC10.3 -> "Daily summary job"
- AC10.4 -> "gamification.enabled / daily_summary independence"
- AC10.5 -> "Shared math: review vs streaks module parity, read-only proof"
- Regression -> "review.py refactor regression (pre-v0.10 byte-identical)"
- Audit -> "Deferred-reparse scope trim (documented, benign)"

Conventions borrowed from the existing suite: `_seed`/`_synthetic_habit`
mirror `tests/test_v07_m3_review_extra.py`; `FakeChannel`/`FakeLLM`/
`patch_parse_message` mirror `tests/test_confirmations.py`;
`_FakeScheduler`/`_FakeTelegramChannel`/`_StopAfterSchedulerStart` mirror
`tests/test_reminders.py`; `_FixedDatetime`/`_freeze_reminders_clock`
mirror `tests/test_adaptive_reminders.py`. No mocks for the DB (real
on-disk SQLite via tmp_path) -- only Ollama/Telegram are faked.

Live-environment rule: every DB in this file is a scratch `tmp_path`
SQLite file. Nothing here ever opens `data/habits.db`, and no real
Telegram/Ollama call is made (all channels/LLMs are fakes; the one
end-to-end `async_main` path patches `TelegramChannel`/`AsyncIOScheduler`
exactly like `test_reminders.py` already does)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config, GamificationConfig, I18nConfig, QuietHoursConfig
from habit_assistant.core import i18n, streaks
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.review import compute_weekly_stats
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message, reparse_pending_unparsed
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_v07_m3_review_extra.py and
# tests/test_confirmations.py's own copies of these -- each test file keeps
# its own per this codebase's convention).
# ---------------------------------------------------------------------------


def _seed(db: Database, ts: str, category: str, value_num: float | None, raw: str = "x") -> int:
    entry = LogEntry(None, ts, category, value_num, None, raw, "reply")
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


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    """Stand-in for OllamaClient -- parse_message is monkeypatched (see
    `patch_parse_message`) so only `chat_text` (diary reflection) is ever
    called for real, and none of these tests touch the diary branch."""

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"


def patch_parse_message(monkeypatch, result: ExtractionResult) -> None:
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


class _StepClock:
    """A callable clock (matches `handle_inbound_message`'s `clock=
    datetime.now`-shaped parameter) whose returned moment can be advanced
    between calls -- needed for AC10.2's multi-day milestone-crossing
    scenarios (a single `fixed_clock` fixture can't move across days)."""

    def __init__(self, start: datetime) -> None:
        self._current = start

    def __call__(self) -> datetime:
        return self._current

    def set(self, dt: datetime) -> None:
        self._current = dt


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# AC10.1 -- streak arithmetic: day_qualifies / compute_streak, parity across
# all 4 habit types, gap resets, today-partial semantics.
# ---------------------------------------------------------------------------


def test_goal_habit_day_exactly_at_goal_qualifies(db):
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "juice", 1000.0)

    assert streaks.day_qualifies(db, config, juice, "2026-08-19") is True


def test_goal_habit_day_just_below_goal_does_not_qualify(db):
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "juice", 999.99)

    assert streaks.day_qualifies(db, config, juice, "2026-08-19") is False


def test_goal_habit_day_above_goal_qualifies(db):
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "juice", 1500.0)

    assert streaks.day_qualifies(db, config, juice, "2026-08-19") is True


def test_nongoal_numeric_any_entry_qualifies_regardless_of_size(db):
    steps = _synthetic_habit("steps", "numeric", goal=None)
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "steps", 1.0)  # tiny value, still "an entry"

    assert streaks.day_qualifies(db, config, steps, "2026-08-19") is True


def test_boolean_only_truthy_entry_counts_as_a_done_day(db):
    meds = _synthetic_habit("meds", "boolean")
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "meds", 0.0)  # falsy -- "took no meds"

    assert streaks.day_qualifies(db, config, meds, "2026-08-19") is False

    _seed(db, "2026-08-19T10:00:00", "meds", 1.0)  # truthy

    assert streaks.day_qualifies(db, config, meds, "2026-08-19") is True


def test_duration_any_entry_qualifies(db):
    yoga = _synthetic_habit("yoga", "duration")
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "yoga", 10.0)

    assert streaks.day_qualifies(db, config, yoga, "2026-08-19") is True


def test_text_type_any_entry_qualifies(db):
    journal = _synthetic_habit("journal", "text")
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "journal", None)

    assert streaks.day_qualifies(db, config, journal, "2026-08-19") is True


@pytest.mark.parametrize(
    ("type_", "goal", "seed_value"),
    [
        ("numeric", 1000, 1000.0),  # goal-bearing: exact goal-met days
        ("numeric", None, 5.0),  # non-goal numeric: any-entry days
        ("duration", None, 10.0),  # non-goal duration: any-entry days
        ("boolean", None, 1.0),  # boolean: truthy done-days
        ("text", None, None),  # text: any-entry days
    ],
    ids=["numeric-goal", "numeric-nogoal", "duration", "boolean", "text"],
)
def test_compute_streak_gap_resets_across_all_habit_types(db, type_, goal, seed_value):
    """AC10.1: a gap resets the streak -- only the trailing consecutive run
    ending at `end_date` counts, even though an earlier run exists further
    back. Parametrized across every habit type/goal shape streaks.py
    special-cases (AC10.1's "parity across all 4 habit types")."""
    habit = _synthetic_habit("h", type_, goal=goal)
    config = Config()
    end_date = date(2026, 8, 19)
    qualifying_offsets = {0, 1, 2, 5, 6}  # gap at offsets 3, 4 -> trailing run = {0,1,2}

    for offset in qualifying_offsets:
        d = end_date - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T09:00:00", "h", seed_value)

    assert streaks.compute_streak(db, config, habit, end_date) == 3


def test_goal_habit_partial_today_does_not_qualify_but_past_run_is_preserved(db):
    """AC10.1's "today-partial" semantics for a goal-bearing habit: a
    below-goal entry today does NOT extend the streak, but the streak that
    existed as of yesterday is still correctly computed when asked for
    (i.e. the walk starts fresh from whatever `end_date` is given).

    Uses a non-"water" id deliberately: `streaks.effective_goal` special-
    cases `water` to always read the legacy `config.reminders.water.
    goal_ml` (2500) rather than `habit.goal` (IMPL.md's documented,
    inherited-from-v0.6 behavior) -- a plain synthetic goal habit avoids
    that special case entirely."""
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = Config()
    today = date(2026, 8, 19)
    yesterday = today - timedelta(days=1)
    for offset in (1, 2, 3):  # goal-met on the 3 days ending yesterday
        d = today - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{today.isoformat()}T09:00:00", "juice", 500.0)  # today: partial, below goal

    assert streaks.compute_streak(db, config, juice, today) == 0
    assert streaks.compute_streak(db, config, juice, yesterday) == 3


def test_nongoal_habit_partial_today_entry_still_qualifies(db):
    """Contrast with the goal-bearing case above: for a non-goal habit,
    ANY entry today (however small) qualifies -- there is no "partial"
    concept, only presence/absence."""
    steps = _synthetic_habit("steps", "numeric", goal=None)
    config = Config()
    today = date(2026, 8, 19)
    for offset in (1, 2):
        d = today - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T09:00:00", "steps", 8000.0)
    _seed(db, f"{today.isoformat()}T23:50:00", "steps", 1.0)  # tiny, but present

    assert streaks.compute_streak(db, config, steps, today) == 3


def test_duration_multiple_sessions_same_day_counts_as_one_streak_day(db):
    yoga = _synthetic_habit("yoga", "duration")
    config = Config()
    end_date = date(2026, 8, 19)
    _seed(db, f"{end_date.isoformat()}T09:00:00", "yoga", 10.0)
    _seed(db, f"{end_date.isoformat()}T18:00:00", "yoga", 15.0)

    assert streaks.compute_streak(db, config, yoga, end_date) == 1


# ---------------------------------------------------------------------------
# AC10.2 -- milestone crossing via the REAL handle_inbound_message: exactly
# one line per crossing, never repeated within the same qualifying day,
# never emitted for a non-milestone streak length, and bilingual.
# ---------------------------------------------------------------------------


def _water_confirmation(lang: i18n.Language, *, water_ml: int, total: int, goal: int = 2500) -> str:
    pct = round(100 * total / goal)
    return i18n.t("water_confirmation", lang, water_ml=water_ml, total=total, goal=goal, pct=pct)


def _milestone_line(lang: i18n.Language, streak: int, label: str) -> str:
    return i18n.t("milestone_reached", lang, streak=streak, label=label)


async def test_milestone_crossing_sequence_3_then_no_repeat_then_7(db, monkeypatch):
    config = Config()  # gamification: enabled=True, milestones=[3,7,30]
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    clock = _StepClock(datetime(2026, 8, 17, 9, 0, 0))  # day 1

    # Background days 1 & 2, goal-met, seeded directly (not through
    # handle_inbound_message -- keeps the test focused on the crossing).
    _seed(db, "2026-08-17T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-18T09:00:00", "water", 2500.0)

    # Day 3: this log crosses the 3-day milestone -> exactly one line.
    clock.set(datetime(2026, 8, 19, 9, 0, 0))
    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))
    await handle_inbound_message(
        "2500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )
    expected = _water_confirmation("en", water_ml=2500, total=2500) + "\n\n" + _milestone_line("en", 3, "water")
    assert channel.sent[-1] == expected

    # Same day, second log: today already qualified BEFORE this write, so
    # it cannot flip day_qualifies again -- no milestone line, not repeated.
    patch_parse_message(monkeypatch, ExtractionResult("water", 100, 0.9))
    await handle_inbound_message(
        "100ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )
    assert "🔥" not in channel.sent[-1]
    assert channel.sent[-1] == _water_confirmation("en", water_ml=100, total=2600)

    # Days 4, 5, 6: goal-met, seeded directly.
    for ts in ("2026-08-20T09:00:00", "2026-08-21T09:00:00", "2026-08-22T09:00:00"):
        _seed(db, ts, "water", 2500.0)

    # Day 7: crosses the next configured milestone.
    clock.set(datetime(2026, 8, 23, 9, 0, 0))
    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))
    await handle_inbound_message(
        "2500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )
    expected7 = _water_confirmation("en", water_ml=2500, total=2500) + "\n\n" + _milestone_line("en", 7, "water")
    assert channel.sent[-1] == expected7


async def test_streak_reaching_a_non_milestone_number_produces_no_line(db, monkeypatch):
    """A log that DOES flip day_qualifies (a genuine new crossing) but
    lands on a streak length that is not in `gamification.milestones`
    (default [3, 7, 30]) must not append anything."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    clock = _StepClock(datetime(2026, 8, 17, 9, 0, 0))

    for ts in ("2026-08-17T09:00:00", "2026-08-18T09:00:00", "2026-08-19T09:00:00"):
        _seed(db, ts, "water", 2500.0)  # 3 days goal-met already (day 1-3)

    clock.set(datetime(2026, 8, 20, 9, 0, 0))  # day 4 -> streak becomes 4, not a milestone
    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))
    await handle_inbound_message(
        "2500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )

    assert streaks.compute_streak(db, config, registry.get("water"), date(2026, 8, 20)) == 4
    assert "🔥" not in channel.sent[-1]
    assert channel.sent[-1] == _water_confirmation("en", water_ml=2500, total=2500)


async def test_milestone_line_is_thai_for_thai_input(db, monkeypatch):
    """AC10.2 bilingual: Thai input -> Thai confirmation AND Thai
    milestone line (language is resolved once, per AC6.1/AC6.3, and both
    the confirmation and the milestone suffix follow it)."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    clock = _StepClock(datetime(2026, 8, 17, 9, 0, 0))

    _seed(db, "2026-08-17T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-18T09:00:00", "water", 2500.0)
    clock.set(datetime(2026, 8, 19, 9, 0, 0))
    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))

    await handle_inbound_message(
        "ดื่มน้ำ 2500 มล", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )

    expected = _water_confirmation("th", water_ml=2500, total=2500) + "\n\n" + _milestone_line("th", 3, "น้ำ")
    assert channel.sent[-1] == expected


# ---------------------------------------------------------------------------
# AC10.3 -- daily summary: correct content (per habit type, in the
# resolved language), scheduled registration, custom time, quiet hours.
# ---------------------------------------------------------------------------


def test_run_daily_summary_content_default_thai(db):
    config = Config()  # primary_language default "th"
    registry = HabitRegistry.from_config(config)
    today = date(2026, 8, 19)

    _seed(db, f"{today.isoformat()}T09:00:00", "water", 1800.0)  # goal 2500, partial
    _seed(db, f"{today.isoformat()}T11:00:00", "stretch", 10.0)  # duration, 2 sessions
    _seed(db, f"{today.isoformat()}T16:00:00", "stretch", 15.0)
    db.insert_log(
        LogEntry(None, f"{today.isoformat()}T21:00:00", "diary", None, "good day", "good day", "reply")
    )  # text, 1 entry

    text = streaks.run_daily_summary(db, config, registry, today=today)
    lines = text.splitlines()

    assert lines[0] == i18n.t("daily_summary_header", "th")
    assert i18n.t(
        "daily_summary_numeric_goal", "th", label="น้ำ", total=1800.0, goal=2500.0, unit="มล.", pct=72, streak=0
    ) in text
    assert i18n.t("daily_summary_duration_nogoal", "th", label="ยืดเส้น", total=2, streak=1) in text
    assert i18n.t("daily_summary_text", "th", label="ไดอารี่", total=1, streak=1) in text


def test_run_daily_summary_respects_forced_language_english(tmp_path):
    db = Database(tmp_path / "habits.db")
    config = Config(i18n=I18nConfig(language="en"))
    registry = HabitRegistry.from_config(config)
    today = date(2026, 8, 19)
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 2500.0)

    text = streaks.run_daily_summary(db, config, registry, today=today)

    assert text.splitlines()[0] == i18n.t("daily_summary_header", "en")
    assert i18n.t(
        "daily_summary_numeric_goal", "en", label="water", total=2500.0, goal=2500.0, unit="ml", pct=100, streak=1
    ) in text
    db.close()


def test_daily_summary_includes_every_registered_habit_even_with_zero_entries(db):
    """Per IMPL.md's documented "Known limitations": the summary always
    includes every habit in registry order, even ones with no entries
    today -- verifies this deliberately-honest behavior, not a bug."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    today = date(2026, 8, 19)
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 500.0)
    # stretch and diary: zero entries today.

    text = streaks.run_daily_summary(db, config, registry, today=today)

    assert i18n.t("daily_summary_duration_nogoal", "th", label="ยืดเส้น", total=0, streak=0) in text
    assert i18n.t("daily_summary_text", "th", label="ไดอารี่", total=0, streak=0) in text


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    """Records add_job calls; start/shutdown are no-ops. Mirrors
    tests/test_reminders.py's `_FakeScheduler` exactly."""

    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)


class _FakeTelegramChannel:
    """Captures sent text (unlike test_reminders.py's no-op version, this
    module needs to inspect what the daily-summary job actually sends).

    `invoke_daily_summary_job_on_run`: when a test needs to actually CALL
    the registered `daily_summary` job's function (not just inspect its
    trigger), it must happen from inside `run()`, before this raises --
    `async_main`'s own `try/finally` closes `db`/`llm`/`channel` as soon as
    `channel.run()` returns or raises (see `main.py`'s tail), so invoking
    the job any later (e.g. after `pytest.raises(...)` catches the stop
    exception) would hit an already-closed DB."""

    last_instance: "_FakeTelegramChannel | None" = None
    invoke_daily_summary_job_on_run: bool = False

    def __init__(self, *args, **kwargs):
        self.sent: list[str] = []
        _FakeTelegramChannel.last_instance = self

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message):
        if _FakeTelegramChannel.invoke_daily_summary_job_on_run:
            job = _FakeScheduler.last_instance.get_job("daily_summary")
            if job is not None:
                await job.func()
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _run_async_main_and_capture_scheduler(monkeypatch, config, tmp_path, invoke_daily_summary_job: bool = False):
    """Drives main.async_main up to (but not through) the Telegram
    long-poll loop, per test_reminders.py's established pattern, and
    returns main_module for job/content inspection via
    `_FakeScheduler.last_instance`/`_FakeTelegramChannel.last_instance`."""
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id="fake")
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeTelegramChannel)
    _FakeScheduler.last_instance = None
    _FakeTelegramChannel.last_instance = None
    _FakeTelegramChannel.invoke_daily_summary_job_on_run = invoke_daily_summary_job
    return main_module


async def test_async_main_registers_daily_summary_job_at_configured_time(tmp_path, monkeypatch):
    config = Config.model_validate(
        {
            "app": {"db_path": str(tmp_path / "habits.db")},
            "gamification": {"daily_summary_time": "22:10"},
        }
    )
    main_module = _run_async_main_and_capture_scheduler(monkeypatch, config, tmp_path)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    scheduler = _FakeScheduler.last_instance
    job = scheduler.get_job("daily_summary")
    assert job is not None
    trigger_fields = {f.name: str(f) for f in job.trigger.fields}
    assert trigger_fields["hour"] == "22"
    assert trigger_fields["minute"] == "10"


class _FixedDatetime(datetime):
    """Mirrors tests/test_adaptive_reminders.py's `_FixedDatetime`."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.replace(tzinfo=tz) if tz is not None else cls._fixed


def _freeze_reminders_clock(monkeypatch, hour: int, minute: int) -> None:
    fixed = _FixedDatetime(2026, 8, 19, hour, minute, 0)
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": fixed})
    monkeypatch.setattr("habit_assistant.core.reminders.datetime", frozen)


async def test_daily_summary_job_suppressed_during_quiet_hours(tmp_path, monkeypatch):
    config = Config.model_validate(
        {
            "app": {"db_path": str(tmp_path / "habits.db")},
            "quiet_hours": {"windows": [["23:00", "07:00"]]},
        }
    )
    main_module = _run_async_main_and_capture_scheduler(monkeypatch, config, tmp_path, invoke_daily_summary_job=True)
    _freeze_reminders_clock(monkeypatch, 23, 30)  # inside the configured window
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _FakeTelegramChannel.last_instance
    assert channel.sent == []


async def test_daily_summary_job_sends_when_not_quiet_hours(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})  # windows=[] by default
    main_module = _run_async_main_and_capture_scheduler(monkeypatch, config, tmp_path, invoke_daily_summary_job=True)
    _freeze_reminders_clock(monkeypatch, 12, 0)  # broad daylight, no windows configured anyway
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _FakeTelegramChannel.last_instance
    assert len(channel.sent) == 1


# ---------------------------------------------------------------------------
# AC10.4 -- `gamification.enabled` and `gamification.daily_summary` are
# fully independent: disabling one never suppresses/affects the other.
# ---------------------------------------------------------------------------


def test_daily_summary_flag_defaults_independently_of_enabled_flag():
    config_disabled_gamification = Config.model_validate({"gamification": {"enabled": False}})
    assert config_disabled_gamification.gamification.daily_summary is True

    config_disabled_summary = Config.model_validate({"gamification": {"daily_summary": False}})
    assert config_disabled_summary.gamification.enabled is True


async def test_gamification_disabled_suppresses_milestone_lines(db, monkeypatch):
    config = Config.model_validate({"gamification": {"enabled": False}})
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    clock = _StepClock(datetime(2026, 8, 17, 9, 0, 0))
    _seed(db, "2026-08-17T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-18T09:00:00", "water", 2500.0)
    clock.set(datetime(2026, 8, 19, 9, 0, 0))
    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))

    await handle_inbound_message(
        "2500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )

    # The underlying streak DID reach a milestone (3) -- streak math itself
    # is never gated by `enabled` (AC10.5) -- but the confirmation carries
    # no milestone line because gamification is disabled.
    assert streaks.compute_streak(db, config, registry.get("water"), date(2026, 8, 19)) == 3
    assert "🔥" not in channel.sent[-1]
    assert channel.sent[-1] == _water_confirmation("en", water_ml=2500, total=2500)


def test_gamification_disabled_does_not_affect_daily_summary_content(db):
    """The other half of "no leakage": `enabled=False` must not change
    what the daily summary produces -- it's a fully separate flag."""
    config = Config.model_validate({"gamification": {"enabled": False}})
    registry = HabitRegistry.from_config(config)
    today = date(2026, 8, 19)
    _seed(db, f"{today.isoformat()}T09:00:00", "water", 2500.0)

    text = streaks.run_daily_summary(db, config, registry, today=today)

    assert i18n.t("daily_summary_header", "th") in text
    assert "น้ำ" in text


async def test_daily_summary_disabled_job_sends_nothing(tmp_path, monkeypatch):
    config = Config.model_validate(
        {"app": {"db_path": str(tmp_path / "habits.db")}, "gamification": {"daily_summary": False}}
    )
    main_module = _run_async_main_and_capture_scheduler(monkeypatch, config, tmp_path, invoke_daily_summary_job=True)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)

    channel = _FakeTelegramChannel.last_instance
    assert channel.sent == []


async def test_milestones_still_fire_when_daily_summary_disabled(db, monkeypatch):
    """The other half of "no leakage" for milestones: `daily_summary=False`
    must not suppress milestone lines on the live confirmation path."""
    config = Config.model_validate({"gamification": {"daily_summary": False}})
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    clock = _StepClock(datetime(2026, 8, 17, 9, 0, 0))
    _seed(db, "2026-08-17T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-18T09:00:00", "water", 2500.0)
    clock.set(datetime(2026, 8, 19, 9, 0, 0))
    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))

    await handle_inbound_message(
        "2500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=clock, registry=registry
    )

    assert "🔥" in channel.sent[-1]


# ---------------------------------------------------------------------------
# AC10.5 -- shared math: core/review.py's duration-streak column and
# core/streaks.py's own functions must never diverge (same seeded data,
# same number), including for streaks longer than the review's 7-day
# window. Plus: streaks.py is provably read-only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("streak_len", [1, 3, 7, 10, 15, 40])
def test_review_and_streaks_module_agree_on_duration_streak_length(tmp_path, streak_len):
    """Parametrized across streak shapes including several that exceed the
    weekly review's 7-day aggregation window -- `compute_weekly_stats`'s
    duration streak and `streaks.compute_streak`'s own answer, and
    `compute_daily_summary`'s per-habit streak, must all agree. A mismatch
    here is exactly the "contradictory numbers for the same data" failure
    mode this AC exists to prevent."""
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])
    config = Config()
    end_date = date(2026, 8, 19)
    for offset in range(streak_len):
        d = end_date - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T09:00:00", "yoga", 10.0)

    review_streak = compute_weekly_stats(db, config, registry, end_date).get("yoga").streak
    direct_streak = streaks.compute_streak(db, config, yoga, end_date)
    summary_streak = streaks.compute_daily_summary(db, config, registry, end_date)[0].streak

    assert review_streak == direct_streak == summary_streak == streak_len
    db.close()


class _ReadOnlyGuardDatabase(Database):
    """A Database whose write methods raise -- used to prove a code path
    never calls them, without relying on inspecting `logs` row counts
    (which would only catch *some* kinds of accidental writes)."""

    def insert_log(self, *args, **kwargs):
        raise AssertionError("core/streaks.py must never write (AC10.5 read-only)")

    def reclassify_log(self, *args, **kwargs):
        raise AssertionError("core/streaks.py must never write (AC10.5 read-only)")

    def soft_delete(self, *args, **kwargs):
        raise AssertionError("core/streaks.py must never write (AC10.5 read-only)")

    def update_value(self, *args, **kwargs):
        raise AssertionError("core/streaks.py must never write (AC10.5 read-only)")


def test_streaks_module_is_provably_read_only(tmp_path):
    seed_db = Database(tmp_path / "habits.db")
    config = Config()
    registry = HabitRegistry.from_config(config)
    today = date(2026, 8, 19)
    _seed(seed_db, f"{today.isoformat()}T09:00:00", "water", 2500.0)
    _seed(seed_db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "water", 2500.0)
    seed_db.close()

    guarded = _ReadOnlyGuardDatabase(tmp_path / "habits.db")
    water = registry.get("water")

    # Every public entry point in core/streaks.py, exercised against a DB
    # whose write methods would raise if called.
    streaks.day_qualifies(guarded, config, water, today.isoformat())
    streaks.compute_streak(guarded, config, water, today)
    streaks.crossed_milestone(guarded, config, water, today, was_qualified_before=False)
    streaks.compute_daily_summary(guarded, config, registry, today)
    streaks.run_daily_summary(guarded, config, registry, today=today)
    # Bonus: core/review.py's refactored duration branch also goes through
    # streaks.compute_streak -- confirm the whole review path stays
    # read-only too, now that it shares the function.
    compute_weekly_stats(guarded, config, registry, today)

    guarded.close()  # reached only if nothing above raised


# ---------------------------------------------------------------------------
# Regression -- core/review.py's streak refactor (AC10.5's implementation)
# must not change duration-streak output for any pre-v0.10 scenario that
# fits inside the 7-day review window, and must only ever REPORT MORE for a
# streak longer than 7 days (documented bugfix, not a regression). The old
# algorithm below is a literal reimplementation of the removed inline loop
# from `git diff v0.9.0 -- src/habit_assistant/core/review.py` (the
# "for c in reversed(counts): if c > 0: streak += 1 else: break" hunk) --
# pinning against that instead of re-checking out the old file.
# ---------------------------------------------------------------------------


def _old_v09_duration_streak(counts: list[int]) -> int:
    streak = 0
    for c in reversed(counts):
        if c > 0:
            streak += 1
        else:
            break
    return streak


def test_duration_streak_matches_v090_algorithm_when_streak_fits_in_7day_window(tmp_path):
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])
    config = Config()
    end_date = date(2026, 8, 19)
    active_offsets = {0, 1, 2, 4, 5}  # gap at offset 3 -> trailing run = 3

    for offset in active_offsets:
        d = end_date - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T09:00:00", "yoga", 10.0)

    day_strs = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    counts = [db.count("yoga", d) for d in day_strs]
    old_streak = _old_v09_duration_streak(counts)

    new_streak = compute_weekly_stats(db, config, registry, end_date).get("yoga").streak

    assert old_streak == 3
    assert new_streak == old_streak
    db.close()


def test_duration_streak_beyond_7_days_is_a_documented_bugfix_not_a_regression(tmp_path):
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration")
    registry = HabitRegistry([yoga])
    config = Config()
    end_date = date(2026, 8, 19)

    for offset in range(10):  # unbroken 10-day streak, exceeds the old 7-day window
        d = end_date - timedelta(days=offset)
        _seed(db, f"{d.isoformat()}T09:00:00", "yoga", 10.0)

    day_strs = [(end_date - timedelta(days=o)).isoformat() for o in range(6, -1, -1)]
    counts = [db.count("yoga", d) for d in day_strs]
    old_streak = _old_v09_duration_streak(counts)  # clamped: all 7 window days active -> 7

    new_streak = compute_weekly_stats(db, config, registry, end_date).get("yoga").streak

    assert old_streak == 7
    assert new_streak == 10  # true length, strictly more than the old clamp -- not a regression
    db.close()


# ---------------------------------------------------------------------------
# Audit -- IMPL.md's documented scope trim: the deferred-reparse recovery
# path does not check for a milestone crossing. Confirm it's benign: no
# crash, and the recovered confirmation is exactly the fixed catalog line
# with no milestone suffix (absence, not a wrong line).
# ---------------------------------------------------------------------------


async def test_reparse_pending_unparsed_does_not_check_milestones(db, monkeypatch):
    config = Config()  # gamification.enabled=True (default) -- if the recovery
    # path DID check milestones, this scenario would produce one.
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    _seed(db, "2026-08-17T09:00:00", "water", 2500.0)
    _seed(db, "2026-08-18T09:00:00", "water", 2500.0)
    # A row deferred while Ollama was down, as if by AC3.3's mechanism --
    # recovering it would be water's 3rd consecutive goal-met day, a
    # milestone crossing on the LIVE path.
    db.insert_log(LogEntry(None, "2026-08-19T09:00:00", "unparsed", None, None, "2500ml", "reply"))

    patch_parse_message(monkeypatch, ExtractionResult("water", 2500, 0.9))

    await reparse_pending_unparsed(db, FakeLLM(), channel, config, registry)

    # No crash (the call above completing at all proves this); the sent
    # confirmation is the fixed `recovered_water` catalog line verbatim --
    # no milestone suffix appended, and the row was still correctly
    # reclassified despite the missing milestone check.
    assert channel.sent == [i18n.t("recovered_water", "en", water_ml=2500)]
    assert "🔥" not in channel.sent[0]
    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows[0]["category"] == "water"
    assert rows[0]["value_num"] == 2500.0

    # The streak itself was genuinely reached -- confirming the omission is
    # purely cosmetic (no in-the-moment line), not a data-correctness bug:
    # the next weekly review/daily summary will still report streak 3.
    assert streaks.compute_streak(db, config, registry.get("water"), date(2026, 8, 19)) == 3
