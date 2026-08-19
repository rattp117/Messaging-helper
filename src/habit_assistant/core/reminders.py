"""Reminder definitions + APScheduler wiring (ROADMAP.md v0.7.0 "Multi-Habit
Extensibility", SPEC-v0.7.md §4 R15 / §5, module M2). Depends only on the
Channel ABC (SPEC.md §8) and the HabitRegistry (core/habits.py, shared
surface) -- never a concrete channel, never `Config.habits` directly."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import BUILTIN_IDS, Habit, HabitRegistry

logger = logging.getLogger(__name__)

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


async def send_reminder(channel: Channel, habit: Habit, language: i18n.Language = "en") -> None:
    """Unprompted send (SPEC.md §7/§8) -- `language` is resolved by the
    caller (`schedule_reminders` passes `i18n.resolve_unprompted_language
    (config)`, which defaults to Thai per ROADMAP.md v0.6.0 AC6.3).

    Copy resolution (SPEC-v0.7.md §4 R15): a built-in id reuses its
    existing v0.6.0 catalog entry byte-for-byte (AC13); else the habit's
    own `reminder_text` if the config set one; else the type-generic
    `reminder_generic` template parameterized by `label` (AC14)."""
    if habit.id in BUILTIN_IDS:
        text = i18n.t(BUILTIN_REMINDER_MESSAGE_IDS[habit.id], language)
    else:
        custom = habit.reminder_text(language)
        text = custom if custom is not None else i18n.t("reminder_generic", language, label=habit.label(language))
    await channel.send(text)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def schedule_reminders(scheduler: AsyncIOScheduler, channel: Channel, config: Config, registry: HabitRegistry) -> None:
    """Register one cron job per `reminder_times` entry, for every habit in
    the registry (SPEC-v0.7.md §4 R15) -- a habit with no `reminder_times`
    schedules nothing. Reminders are unprompted (no inbound message to
    detect a language from), so the language is resolved once from
    `config.i18n` and baked into every job's args (ROADMAP.md v0.6.0
    AC6.3)."""
    language = i18n.resolve_unprompted_language(config)
    for habit in registry:
        for t in habit.reminder_times:
            hour, minute = _parse_hhmm(t)
            scheduler.add_job(
                send_reminder,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=config.app.timezone),
                args=[channel, habit, language],
                id=f"reminder_{habit.id}_{t}",
                replace_existing=True,
            )
            logger.info("Scheduled %s reminder at %s (%s, lang=%s)", habit.id, t, config.app.timezone, language)
