"""Reminder definitions + APScheduler wiring (ROADMAP.md v0.7.0 "Multi-Habit
Extensibility", SPEC-v0.7.md §4 R15 / §5, module M2). Depends only on the
Channel ABC (SPEC.md §8) and the HabitRegistry (core/habits.py, shared
surface) -- never a concrete channel, never `Config.habits` directly.

ROADMAP.md v0.9.0 "Adaptive Reminders, Snooze & Quiet Hours" (AC9.1/AC9.2/
AC9.4/AC9.5): `send_reminder` gained two opt-in, additive checks run right
before actually sending -- both no-ops (byte-identical to v0.7.0/v0.8.0
behavior) unless a caller passes `db`/`config`:

- **Quiet hours** (AC9.2): if the current wall-clock time (`config.app.
  timezone`) falls inside any `config.quiet_hours.windows` entry -- including
  a window that crosses midnight -- the reminder is suppressed and logged.
  Pure time-of-day comparison, no DB involved, so there is nothing to fail
  open on here.
- **Goal-met skip** (AC9.1/AC9.4): for a goal-bearing habit
  (`habit.goal is not None`) with `habit.skip_if_goal_met` true (the
  default), today's progress is read from `db` and compared to the goal;
  already-met -> suppressed and logged. **Fail-open** (AC9.5): if the DB
  read itself raises, the error is logged and the reminder is sent anyway --
  a DB hiccup must never silently swallow every reminder.

`ReminderState` (below) is a tiny, in-memory, single-process "which habit's
reminder last actually fired" tracker -- `core/commands.py`'s bare "snooze"/
"เลื่อน" (no explicit habit named in the phrase) needs to know which habit
to reschedule (ROADMAP.md v0.9.0's own wording: "target habit = the most
recently reminded habit"). It's plain data, not a channel/DB import, so it
stays inside this "no channel imports" module without breaking the seam."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import BUILTIN_IDS, Habit, HabitRegistry
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class ReminderState:
    """ROADMAP.md v0.9.0: `last_habit_id` is updated by `send_reminder`
    every time a reminder actually fires (i.e. survives the quiet-hours/
    goal-met checks below) -- read by `main.py`'s snooze handler to resolve
    a bare "snooze"/"เลื่อน" command to a habit. One instance lives for the
    lifetime of the process (built once in `async_main`); lost on restart,
    which is fine -- there is nothing to snooze immediately after a fresh
    start anyway."""

    last_habit_id: str | None = None


# Built-in habits reuse their v0.6.0 catalog entries verbatim (SPEC-v0.7.md
# §4 R15: "built-in id -> its reminder_water/stretch/diary catalog entry
# (byte-identical)") instead of the type-generic `reminder_generic` template
# every other habit falls back to.
BUILTIN_REMINDER_MESSAGE_IDS = {
    "water": "reminder_water",
    "stretch": "reminder_stretch",
    "diary": "reminder_diary",
}

# Back-compat only: `main.py` (frozen shared-surface file, not touched here
# per module ownership -- SPEC-v0.7.md §11) still has a *module-level,
# unconditional* `from habit_assistant.core.reminders import REMINDER_TEXTS,
# schedule_reminders, send_reminder`. Dropping this name would raise
# ImportError at import time for every module that imports `main` --
# collection-time breakage across ~7 unrelated test files, not a graceful
# per-call TypeError like the `registry`/`habit` contract changes below.
# Kept only so that import keeps working; nothing in this module's own code
# reads it anymore. Remove once Archi's integration step flips main.py's
# call sites (SPEC-v0.7.md §11 "Integration order" step 1).
REMINDER_TEXTS = {category: i18n.t(msg_id, "en") for category, msg_id in BUILTIN_REMINDER_MESSAGE_IDS.items()}


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def _in_quiet_hours(now: time, windows: list[tuple[str, str]]) -> bool:
    """ROADMAP.md v0.9.0 AC9.2: does wall-clock `now` fall inside ANY
    configured `[start, end)` window? `start <= end` is a normal same-day
    window; `start > end` crosses midnight (e.g. `("23:00", "07:00")` spans
    the night -- true for `now` >= 23:00 OR `now` < 07:00). The project is
    fixed to `Asia/Bangkok` with no DST, so a plain time-of-day comparison
    is correct with no date arithmetic needed."""
    for start_s, end_s in windows:
        start_h, start_m = _parse_hhmm(start_s)
        end_h, end_m = _parse_hhmm(end_s)
        start, end = time(start_h, start_m), time(end_h, end_m)
        if start <= end:
            if start <= now < end:
                return True
        elif now >= start or now < end:  # crosses midnight
            return True
    return False


def _today_str(config: Config) -> str:
    return datetime.now(ZoneInfo(config.app.timezone)).date().isoformat()


def is_quiet_hours_now(config: Config) -> bool:
    """ROADMAP.md v0.10.0: a plain "is right now inside a configured
    quiet-hours window?" check, for callers outside this module (e.g.
    `main.py`'s daily-summary job, which must also respect quiet hours)
    that shouldn't have to duplicate `_in_quiet_hours`'s window-parsing
    logic or reach into a private function."""
    if not config.quiet_hours.windows:
        return False
    now_local = datetime.now(ZoneInfo(config.app.timezone)).time()
    return _in_quiet_hours(now_local, config.quiet_hours.windows)


def _goal_already_met(db: Database, habit: Habit, config: Config) -> bool:
    """ROADMAP.md v0.9.0 AC9.1/AC9.4/AC9.5: True only for a goal-bearing
    habit (`habit.goal is not None`) with adaptive skipping enabled
    (`habit.skip_if_goal_met`, default True) whose today's total already
    meets the goal. Fail-open (AC9.5): a DB read error is logged and
    treated as "not met" -- the reminder always sends rather than a
    scheduler job ever crashing or silently going dark on a DB hiccup."""
    if habit.goal is None or not habit.skip_if_goal_met:
        return False
    try:
        total = db.sum_value(habit.id, _today_str(config))
    except Exception:
        logger.exception("Adaptive-reminder goal read failed for %s; sending reminder anyway (fail-open)", habit.id)
        return False
    if total >= habit.goal:
        logger.info("Skipping %s reminder: goal already met (%s/%s)", habit.id, total, habit.goal)
        return True
    return False


async def send_reminder(
    channel: Channel,
    habit: Habit,
    language: i18n.Language = "en",
    db: Database | None = None,
    config: Config | None = None,
    state: ReminderState | None = None,
) -> None:
    """Unprompted send (SPEC.md §7/§8) -- `language` is resolved by the
    caller (`schedule_reminders` passes `i18n.resolve_unprompted_language
    (config)`, which defaults to Thai per ROADMAP.md v0.6.0 AC6.3).

    Copy resolution (SPEC-v0.7.md §4 R15): a built-in id reuses its
    existing v0.6.0 catalog entry byte-for-byte (AC13); else the habit's
    own `reminder_text` if the config set one; else the type-generic
    `reminder_generic` template parameterized by `label` (AC14).

    ROADMAP.md v0.9.0: `db`/`config` are additive and default to `None` --
    every pre-v0.9 caller (a test calling `send_reminder(channel, habit,
    lang)` directly) is unaffected, byte-identical output, no adaptive
    checks run. `schedule_reminders` below is the real production caller
    and always binds both. Order: quiet hours first (cheap, no I/O), then
    the goal-met DB read -- either suppression short-circuits before the
    send and before `state` is updated (a suppressed reminder never counts
    as "the most recently reminded habit" for snooze purposes, ROADMAP.md
    v0.9.0's own AC9.3 target-habit rule)."""
    if config is not None and config.quiet_hours.windows:
        now_local = datetime.now(ZoneInfo(config.app.timezone)).time()
        if _in_quiet_hours(now_local, config.quiet_hours.windows):
            logger.info("Suppressing %s reminder: inside a quiet-hours window (now=%s)", habit.id, now_local)
            return

    if db is not None and config is not None and _goal_already_met(db, habit, config):
        return

    if habit.id in BUILTIN_IDS:
        text = i18n.t(BUILTIN_REMINDER_MESSAGE_IDS[habit.id], language)
    else:
        custom = habit.reminder_text(language)
        text = custom if custom is not None else i18n.t("reminder_generic", language, label=habit.label(language))
    await channel.send(text)
    if state is not None:
        state.last_habit_id = habit.id


def schedule_reminders(
    scheduler: AsyncIOScheduler,
    channel: Channel,
    config: Config,
    registry: HabitRegistry,
    db: Database | None = None,
    state: ReminderState | None = None,
) -> None:
    """Register one cron job per `reminder_times` entry, for every habit in
    the registry (SPEC-v0.7.md §4 R15) -- a habit with no `reminder_times`
    schedules nothing. Reminders are unprompted (no inbound message to
    detect a language from), so the language is resolved once from
    `config.i18n` and baked into every job's args (ROADMAP.md v0.6.0
    AC6.3).

    ROADMAP.md v0.9.0: `db`/`state` (both optional, default `None` for
    backward compat with every pre-v0.9 caller/test) are bound into each
    job's `args` alongside `config`, so the adaptive quiet-hours/goal-met
    checks run with fresh state at *fire* time, not at scheduling time."""
    language = i18n.resolve_unprompted_language(config)
    for habit in registry:
        for t in habit.reminder_times:
            hour, minute = _parse_hhmm(t)
            scheduler.add_job(
                send_reminder,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=config.app.timezone),
                args=[channel, habit, language, db, config, state],
                id=f"reminder_{habit.id}_{t}",
                replace_existing=True,
            )
            logger.info("Scheduled %s reminder at %s (%s, lang=%s)", habit.id, t, config.app.timezone, language)
