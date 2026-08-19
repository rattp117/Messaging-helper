"""Weekly aggregation + narrative. Runs Sunday 20:00 (configurable) over the
last 7 days, per habit: numeric -> per-day goal-adherence (when a goal is
configured) + total/average; duration -> session count + current streak;
text -> entry count; boolean -> done-day count (ROADMAP.md v0.7.0
"Multi-Habit Extensibility", SPEC-v0.7.md §4 R16, §5 module M3). Narrative
is factual, no medical advice (enforced via the system prompt in
llm/prompts.py).

Built-in habits (`water`/`stretch`/`diary`) render through the exact,
unmodified v0.6.0 catalog entries and math (`stats_water_*`/
`stats_stretch_summary`/`stats_diary_summary`), matching `main.py`'s own
built-in-vs-generic confirmation dispatch -- this is what makes AC7.1/AC15's
"byte-identical to v0.6.0" provable by construction rather than by
re-derivation (SPEC-v0.7.md §9 risk 2). Any other configured habit renders
through the type-generic `stats_generic_*` templates (AC16)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import WEEKLY_REVIEW_SYSTEM_PROMPT, WEEKLY_REVIEW_USER_TEMPLATE
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# TODO(garmin): future work — import a Garmin hydration CSV export and join
# it (by date) against `water` category logs, to cross-check/augment
# self-reported intake in the weekly review. Not implemented for MVP
# (SPEC.md §12 non-goals; ROADMAP.md v1.0.0).


@dataclass(slots=True)
class DayValue:
    """One day's aggregate for a habit that gets a per-day breakdown in the
    review (numeric with a goal, and the built-in `water`). `goal` is
    carried per-row (rather than looked up separately) so `pct` is
    self-contained."""

    day: str
    value: float
    goal: float | None

    @property
    def pct(self) -> float:
        if not self.goal or self.goal <= 0:
            return 0.0
        return round(100 * self.value / self.goal, 1)


@dataclass(slots=True)
class HabitStats:
    """Per-habit weekly aggregate. Shape is uniform across types (so
    `WeeklyStats.habits` is a plain list generated from the registry, per
    SPEC-v0.7.md R16) but which fields are meaningful depends on
    `habit.type`:

    - numeric: `total`/`avg` always; `days` (per-day goal-adherence) only
      when `habit.goal` (or, for the built-in `water`, the legacy
      `config.reminders.water.goal_ml`) is set.
    - duration: `total` (session count) + `streak`; `days`/`avg` unused.
    - text: `total` (entry count); `days`/`avg`/`streak` unused.
    - boolean: `total` (done-day count); `days`/`avg`/`streak` unused.
    """

    habit: Habit
    days: list[DayValue]
    total: float
    avg: float
    streak: int


@dataclass(slots=True)
class WeeklyStats:
    end_date: date
    habits: list[HabitStats]

    def get(self, habit_id: str) -> HabitStats | None:
        return next((hs for hs in self.habits if hs.habit.id == habit_id), None)


def _week_days(end_date: date) -> list[str]:
    return [(end_date - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]


def _water_goal_ml(config: Config) -> float:
    """The built-in `water` habit keeps reading its goal from the legacy
    `config.reminders.water.goal_ml` field, mirroring `main.py`'s water
    confirmation branch exactly (both are the same "reuse v0.6 verbatim"
    strategy) -- not `registry.get("water").goal`, so the two stay
    byte-identical even if a config sets them differently."""
    return config.reminders.water.goal_ml


def _compute_habit_stats(db: Database, config: Config, habit: Habit, day_strs: list[str]) -> HabitStats:
    if habit.type == "numeric":
        values = [db.sum_value(habit.id, d) for d in day_strs]
        goal = _water_goal_ml(config) if habit.id == "water" else habit.goal
        days = [DayValue(d, v, goal) for d, v in zip(day_strs, values)] if goal else []
        total = sum(values)
        avg = round(total / len(values), 1) if values else 0.0
        return HabitStats(habit=habit, days=days, total=total, avg=avg, streak=0)

    if habit.type == "duration":
        counts = [db.count(habit.id, d) for d in day_strs]
        total = sum(counts)
        streak = 0
        for c in reversed(counts):
            if c > 0:
                streak += 1
            else:
                break
        return HabitStats(habit=habit, days=[], total=total, avg=0.0, streak=streak)

    if habit.type == "text":
        total = sum(db.count(habit.id, d) for d in day_strs)
        return HabitStats(habit=habit, days=[], total=total, avg=0.0, streak=0)

    # boolean: "done-days" -- the number of days with at least one truthy
    # log, not the raw row count (a habit logged twice in one day still
    # counts as one done day).
    done_days = sum(1 for d in day_strs if db.count_true(habit.id, d) > 0)
    return HabitStats(habit=habit, days=[], total=done_days, avg=0.0, streak=0)


def compute_weekly_stats(db: Database, config: Config, registry: HabitRegistry, end_date: date) -> WeeklyStats:
    """Aggregate the 7 days ending on end_date (inclusive), once per
    registered habit, in registry order."""
    day_strs = _week_days(end_date)
    habits = [_compute_habit_stats(db, config, habit, day_strs) for habit in registry]
    return WeeklyStats(end_date=end_date, habits=habits)


def _format_water(hs: HabitStats, lang: i18n.Language) -> list[str]:
    lines = [i18n.t("stats_water_header", lang)]
    for d in hs.days:
        lines.append(
            i18n.t(
                "stats_water_line",
                lang,
                day=d.day,
                water_ml=int(d.value),
                water_goal_ml=int(d.goal) if d.goal else 0,
                water_pct=d.pct,
            )
        )
    lines.append(i18n.t("stats_water_total", lang, water_total_ml=int(hs.total), water_avg_ml=hs.avg))
    return lines


def _format_stretch(hs: HabitStats, lang: i18n.Language) -> list[str]:
    return [i18n.t("stats_stretch_summary", lang, stretch_total=int(hs.total), stretch_streak=hs.streak)]


def _format_diary(hs: HabitStats, lang: i18n.Language) -> list[str]:
    return [i18n.t("stats_diary_summary", lang, diary_count=int(hs.total))]


def _format_generic(hs: HabitStats, lang: i18n.Language) -> list[str]:
    habit = hs.habit
    label = habit.label(lang)

    if habit.type == "numeric":
        lines: list[str] = []
        unit = habit.unit(lang) or ""
        if hs.days:
            lines.append(i18n.t("stats_generic_numeric_header", lang, label=label, unit=unit))
            for d in hs.days:
                lines.append(
                    i18n.t("stats_generic_numeric_line", lang, day=d.day, value=d.value, goal=d.goal, pct=d.pct)
                )
        lines.append(i18n.t("stats_generic_numeric_total", lang, label=label, unit=unit, total=hs.total, avg=hs.avg))
        return lines

    if habit.type == "duration":
        return [i18n.t("stats_generic_duration_summary", lang, label=label, total=int(hs.total), streak=hs.streak)]

    # text and boolean both render as a single entry/done-day count line.
    return [i18n.t("stats_generic_count_summary", lang, label=label, count=int(hs.total))]


def format_stats_summary(stats: WeeklyStats, registry: HabitRegistry, lang: i18n.Language = "en") -> str:
    """`lang` defaults to English so a caller that just wants the plain
    factual block (e.g. feeding it to the LLM prompt is language-agnostic
    by nature) doesn't have to think about localization -- `run_weekly_review`
    below is the one production call site, and it always resolves and
    passes the target language explicitly (ROADMAP.md v0.6.0, "weekly-review
    labels localized")."""
    lines: list[str] = []
    for hs in stats.habits:
        habit_id = hs.habit.id
        if habit_id == "water":
            lines.extend(_format_water(hs, lang))
        elif habit_id == "stretch":
            lines.extend(_format_stretch(hs, lang))
        elif habit_id == "diary":
            lines.extend(_format_diary(hs, lang))
        else:
            lines.extend(_format_generic(hs, lang))
    return "\n".join(lines)


async def run_weekly_review(
    db: Database, config: Config, registry: HabitRegistry, llm: OllamaClient, today: date | None = None
) -> str:
    """Aggregate + narrate. Falls back to the plain stats block (no
    narrative) if the LLM call fails, so the review still gets sent.

    ROADMAP.md v0.6.0 AC6.4: the weekly review is an unprompted send (no
    inbound message to detect a language from), so its language is
    `i18n.resolve_unprompted_language(config)` -- forced language wins as
    usual, "auto" uses the configured primary language (default Thai).
    The narrative's system prompt gets the same target language via
    `i18n.language_instruction`, so the LLM-generated prose matches the
    factual stats block instead of being English inside a Thai message;
    the "no medical advice" constraint (SPEC.md/ROADMAP.md AC6.4) stays in
    the (English, LLM-facing, not user-facing) instruction text itself and
    is unaffected by which language the narrative comes back in."""
    end_date = today or date.today()
    stats = compute_weekly_stats(db, config, registry, end_date)
    lang = i18n.resolve_unprompted_language(config)
    summary = format_stats_summary(stats, registry, lang)

    narrative = await llm.chat_text(
        WEEKLY_REVIEW_SYSTEM_PROMPT.format(language_instruction=i18n.language_instruction(lang)),
        WEEKLY_REVIEW_USER_TEMPLATE.format(stats_summary=summary),
    )
    if not narrative:
        narrative = i18n.t("weekly_review_fallback_narrative", lang)

    header = i18n.t("weekly_review_header", lang)
    return f"{header}\n\n{summary}\n\n{narrative}"
