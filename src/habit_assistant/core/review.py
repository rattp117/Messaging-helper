"""Weekly aggregation + narrative. Runs Sunday 20:00 (configurable) over the
last 7 days: water adherence %/day, total/average, stretch count + current
streak, diary entry count. Narrative is factual, no medical advice
(enforced via the system prompt in llm/prompts.py)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from habit_assistant.config import Config
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import WEEKLY_REVIEW_SYSTEM_PROMPT, WEEKLY_REVIEW_USER_TEMPLATE
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# TODO(garmin): future work — import a Garmin hydration CSV export and join
# it (by date) against `water` category logs, to cross-check/augment
# self-reported intake in the weekly review. Not implemented for MVP
# (SPEC.md §12 non-goals).


@dataclass(slots=True)
class DayStats:
    day: str
    water_ml: float
    water_goal_ml: int
    stretch_count: int

    @property
    def water_pct(self) -> float:
        if self.water_goal_ml <= 0:
            return 0.0
        return round(100 * self.water_ml / self.water_goal_ml, 1)


@dataclass(slots=True)
class WeeklyStats:
    days: list[DayStats]
    water_total_ml: float
    water_avg_ml: float
    stretch_total: int
    stretch_streak: int
    diary_count: int


def compute_weekly_stats(db: Database, config: Config, end_date: date) -> WeeklyStats:
    """Aggregate the 7 days ending on end_date (inclusive)."""
    days: list[DayStats] = []
    for offset in range(6, -1, -1):
        d = end_date - timedelta(days=offset)
        day_str = d.isoformat()
        water_ml = db.water_total_ml(day_str)
        stretch_count = db.stretch_count(day_str)
        days.append(DayStats(day_str, water_ml, config.reminders.water.goal_ml, stretch_count))

    water_total = sum(d.water_ml for d in days)
    water_avg = water_total / len(days) if days else 0.0
    stretch_total = sum(d.stretch_count for d in days)

    streak = 0
    for d in reversed(days):
        if d.stretch_count > 0:
            streak += 1
        else:
            break

    diary_count = sum(db.diary_count(d.day) for d in days)

    return WeeklyStats(
        days=days,
        water_total_ml=water_total,
        water_avg_ml=round(water_avg, 1),
        stretch_total=stretch_total,
        stretch_streak=streak,
        diary_count=diary_count,
    )


def format_stats_summary(stats: WeeklyStats) -> str:
    lines = ["Water (ml / goal / %):"]
    for d in stats.days:
        lines.append(f"  {d.day}: {int(d.water_ml)} / {d.water_goal_ml} ({d.water_pct}%)")
    lines.append(f"Water total: {int(stats.water_total_ml)} ml, average/day: {stats.water_avg_ml} ml")
    lines.append(f"Stretch sessions this week: {stats.stretch_total}, current streak: {stats.stretch_streak} day(s)")
    lines.append(f"Diary entries this week: {stats.diary_count}")
    return "\n".join(lines)


async def run_weekly_review(db: Database, config: Config, llm: OllamaClient, today: date | None = None) -> str:
    """Aggregate + narrate. Falls back to the plain stats block (no
    narrative) if the LLM call fails, so the review still gets sent."""
    end_date = today or date.today()
    stats = compute_weekly_stats(db, config, end_date)
    summary = format_stats_summary(stats)

    narrative = await llm.chat_text(
        WEEKLY_REVIEW_SYSTEM_PROMPT,
        WEEKLY_REVIEW_USER_TEMPLATE.format(stats_summary=summary),
    )
    if not narrative:
        narrative = "Here is your weekly summary."

    return f"📊 Weekly Review\n\n{summary}\n\n{narrative}"
