"""Reminder definitions + APScheduler wiring. Depends only on the Channel
ABC (SPEC.md §8) — never a concrete channel."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config

logger = logging.getLogger(__name__)

REMINDER_TEXTS = {
    "water": "💧 Time for water. How much did you drink?",
    "stretch": "🧘 Stretch break — do a few minutes and tell me how long.",
    "diary": "📓 How was today? A few lines is enough.",
}


async def send_reminder(channel: Channel, category: str) -> None:
    text = REMINDER_TEXTS.get(category)
    if text is None:
        raise ValueError(f"Unknown reminder category: {category!r}. Valid: {sorted(REMINDER_TEXTS)}")
    await channel.send(text)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def schedule_reminders(scheduler: AsyncIOScheduler, channel: Channel, config: Config) -> None:
    """Register one cron job per configured reminder time, per category."""
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
                args=[channel, category],
                id=f"reminder_{category}_{t}",
                replace_existing=True,
            )
            logger.info("Scheduled %s reminder at %s (%s)", category, t, config.app.timezone)
