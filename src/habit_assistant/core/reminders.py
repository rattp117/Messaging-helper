"""Reminder definitions + APScheduler wiring. Depends only on the Channel
ABC (SPEC.md §8) — never a concrete channel."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n

logger = logging.getLogger(__name__)

# ROADMAP.md v0.6.0: reminder text lives in core/i18n.py's catalog now
# (AC6.2). REMINDER_MESSAGE_IDS is the category -> catalog-id mapping
# send_reminder actually resolves through. REMINDER_TEXTS is kept as the
# resolved *English* text for backward-compat callers (existing tests,
# any future dev tooling) that want a plain string without threading a
# language through -- it's always equal to CATALOG[<id>]["en"], never a
# second, independently-maintained copy of the copy.
REMINDER_MESSAGE_IDS = {
    "water": "reminder_water",
    "stretch": "reminder_stretch",
    "diary": "reminder_diary",
}

REMINDER_TEXTS = {category: i18n.t(msg_id, "en") for category, msg_id in REMINDER_MESSAGE_IDS.items()}


async def send_reminder(channel: Channel, category: str, language: i18n.Language = "en") -> None:
    """Unprompted send (SPEC.md §7/§8) -- `language` is resolved by the
    caller: production call sites (`schedule_reminders`, `main.py`'s
    `--test-reminder`) pass `i18n.resolve_unprompted_language(config)`
    (defaults to Thai, ROADMAP.md v0.6.0 AC6.3); the default here is
    English purely so a caller that doesn't care about localization
    (tests, ad hoc scripts) gets the same text `REMINDER_TEXTS` exposes."""
    msg_id = REMINDER_MESSAGE_IDS.get(category)
    if msg_id is None:
        raise ValueError(f"Unknown reminder category: {category!r}. Valid: {sorted(REMINDER_MESSAGE_IDS)}")
    await channel.send(i18n.t(msg_id, language))


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def schedule_reminders(scheduler: AsyncIOScheduler, channel: Channel, config: Config) -> None:
    """Register one cron job per configured reminder time, per category.
    Reminders are unprompted (no inbound message to detect a language
    from), so the language is resolved once from config.i18n and baked
    into every job's args (ROADMAP.md v0.6.0 AC6.3)."""
    language = i18n.resolve_unprompted_language(config)
    per_category = (
        ("water", config.reminders.water.times),
        ("stretch", config.reminders.stretch.times),
        ("diary", config.reminders.diary.times),
    )
    for category, times in per_category:
        for t in times:
            hour, minute = _parse_hhmm(t)
            scheduler.add_job(
                send_reminder,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=config.app.timezone),
                args=[channel, category, language],
                id=f"reminder_{category}_{t}",
                replace_existing=True,
            )
            logger.info("Scheduled %s reminder at %s (%s, lang=%s)", category, t, config.app.timezone, language)
