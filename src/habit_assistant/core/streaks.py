"""Per-habit streak computation, milestone-crossing detection, and the
daily-summary aggregation (ROADMAP.md v0.10.0 "Streaks, Gentle
Gamification & Daily Summary").

ONE definition of "streak" for the whole app (AC10.5): `compute_streak`
below is the sole streak algorithm. `core/review.py`'s weekly "current
streak" column and this module's own milestone-crossing check both call
it, so the two can never diverge -- `tests/test_streaks.py` asserts the
same numbers come back from both call sites on the same seeded data. All
computation here is read-only and reuses the same `Database` aggregations
(`sum_value`/`count`/`count_true`) every other module already reads
through (AC10.5's "reuses v0.7 aggregation").

Streak definition (ROADMAP.md v0.10.0 scope item 1):
- goal-bearing habit (an *effective* goal is configured -- see
  `effective_goal`): a day "qualifies" when that day's total
  (`db.sum_value`) meets or exceeds the goal.
- non-goal habit: a day "qualifies" on any entry -- `count_true` (truthy
  rows only) for boolean habits ("done-days"), `count` (any row) for
  duration/text/numeric-without-goal.
A streak is the number of consecutive qualifying days ending at (and
including) a given date, walking backward; the first non-qualifying day
resets/ends it (AC10.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

# A streak can't sensibly run longer than this many days -- caps the
# backward walk so a years-old, always-logged habit doesn't turn a single
# confirmation/summary into an unbounded table scan.
_MAX_LOOKBACK_DAYS = 3650


def effective_goal(habit: Habit, config: "Config") -> float | None:
    """The goal actually used for streak/summary/milestone purposes. The
    built-in `water` habit's goal has always lived in the legacy
    `config.reminders.water.goal_ml` (SPEC-v0.7.md's "byte-identical to
    v0.6.0" contract -- `core/review.py`'s pre-v0.10 `_water_goal_ml` did
    the same thing) rather than `habit.goal`; both default to 2500, but a
    config that sets them differently must keep behaving like v0.6.0/
    v0.7.0 did. Every other habit just uses its own configured `goal`."""
    if habit.id == "water":
        return config.reminders.water.goal_ml
    return habit.goal


def day_qualifies(db: "Database", config: "Config", habit: Habit, day: str) -> bool:
    """Does `day` ('YYYY-MM-DD') count toward this habit's streak?"""
    goal = effective_goal(habit, config)
    if goal:
        return db.sum_value(habit.id, day) >= goal
    if habit.type == "boolean":
        return db.count_true(habit.id, day) > 0
    return db.count(habit.id, day) > 0


def compute_streak(db: "Database", config: "Config", habit: Habit, end_date: date) -> int:
    """Consecutive qualifying days ending at (and including) `end_date`,
    walking backward until the first gap (AC10.1). Shared by
    `core/review.py`'s weekly summary and this module's own
    milestone-crossing check (AC10.5) -- one function, one number,
    everywhere a streak is surfaced."""
    streak = 0
    day = end_date
    for _ in range(_MAX_LOOKBACK_DAYS):
        if not day_qualifies(db, config, habit, day.isoformat()):
            break
        streak += 1
        day -= timedelta(days=1)
    return streak


def crossed_milestone(
    db: "Database", config: "Config", habit: Habit, today: date, was_qualified_before: bool
) -> int | None:
    """ROADMAP.md v0.10.0 scope item 3 / AC10.2: did the log that was just
    written make `today` transition from not-qualifying to qualifying, AND
    does the resulting streak land exactly on a configured milestone?

    `was_qualified_before` is the caller's pre-insert `day_qualifies(...,
    today)` snapshot. Comparing it to the post-insert state is what makes
    "crossed today already?" derivable purely from log history (no schema
    change, no persisted "already announced" flag, per the task's own
    persistence note): a day can only transition false->true once, so a
    second/third log within an already-qualifying day is inert here by
    construction -- `was_qualified_before` is already `True` for those
    calls, so this returns `None` without even recomputing the streak.
    That gives "exactly once per crossing, not repeated within the same
    streak level" (AC10.2) for free, without tracking which milestones
    were already announced.

    Returns the streak length that was just reached, if (and only if) it
    is one of `config.gamification.milestones` -- else `None`. Callers are
    expected to gate this behind `config.gamification.enabled` (AC10.4);
    this function itself doesn't check that flag since it has no side
    effects to suppress (read-only, AC10.5) -- it's cheap to skip calling
    it at all when gamification is disabled, which is what
    `main.py:handle_inbound_message` does."""
    if was_qualified_before:
        return None
    if not day_qualifies(db, config, habit, today.isoformat()):
        return None
    streak = compute_streak(db, config, habit, today)
    if streak in config.gamification.milestones:
        return streak
    return None


@dataclass(slots=True)
class DailySummaryLine:
    """One habit's row in the end-of-day recap (ROADMAP.md v0.10.0 scope
    item 2, AC10.3). `total` is a sum (`db.sum_value`) when `goal` is set
    (matching the quantity `day_qualifies` actually compares against the
    goal), else a count (`db.count`/`db.count_true`) -- e.g. a
    goal-less `stretch` shows "3 sessions today", a goal-bearing `water`
    shows "1800/2500 ml"."""

    habit: Habit
    total: float
    goal: float | None
    streak: int


def compute_daily_summary(
    db: "Database", config: "Config", registry: "HabitRegistry", today: date
) -> list[DailySummaryLine]:
    """Today's per-habit total (+ goal, when configured) and current streak,
    in registry order -- read-only, reuses the same `Database` aggregations
    as the weekly review and confirmations (AC10.5)."""
    today_str = today.isoformat()
    lines: list[DailySummaryLine] = []
    for habit in registry:
        goal = effective_goal(habit, config)
        if goal or habit.type == "numeric":
            # Goal-bearing (any type) or a goal-less numeric habit: both are
            # inherently summed quantities (e.g. ml), matching what
            # `day_qualifies` compares against the goal when one is set.
            total = db.sum_value(habit.id, today_str)
        elif habit.type == "boolean":
            total = float(db.count_true(habit.id, today_str))
        else:  # duration (no goal), text
            total = float(db.count(habit.id, today_str))
        streak = compute_streak(db, config, habit, today)
        lines.append(DailySummaryLine(habit=habit, total=total, goal=goal, streak=streak))
    return lines


def format_daily_summary(lines: list[DailySummaryLine], lang: i18n.Language) -> str:
    """Bilingual rendering via the i18n catalog (AC10.3)."""
    out = [i18n.t("daily_summary_header", lang)]
    for hs in lines:
        habit = hs.habit
        label = habit.label(lang)
        unit = habit.unit(lang) or ""

        if habit.type in ("numeric", "duration") and hs.goal:
            pct = round(100 * hs.total / hs.goal) if hs.goal else 0
            out.append(
                i18n.t(
                    "daily_summary_numeric_goal",
                    lang,
                    label=label,
                    total=hs.total,
                    goal=hs.goal,
                    unit=unit,
                    pct=pct,
                    streak=hs.streak,
                )
            )
        elif habit.type == "numeric":
            out.append(
                i18n.t("daily_summary_numeric_nogoal", lang, label=label, total=hs.total, unit=unit, streak=hs.streak)
            )
        elif habit.type == "duration":
            out.append(i18n.t("daily_summary_duration_nogoal", lang, label=label, total=int(hs.total), streak=hs.streak))
        elif habit.type == "boolean":
            status = i18n.t("bool_status_done" if hs.total > 0 else "bool_status_not_done", lang)
            out.append(i18n.t("daily_summary_boolean", lang, label=label, status=status, streak=hs.streak))
        else:  # text
            out.append(i18n.t("daily_summary_text", lang, label=label, total=int(hs.total), streak=hs.streak))
    return "\n".join(out)


def run_daily_summary(
    db: "Database", config: "Config", registry: "HabitRegistry", today: date | None = None
) -> str:
    """Aggregate + format the end-of-day recap (AC10.3). `today` defaults
    to the real current date; tests pass a fixed date for determinism.
    Language follows `i18n.resolve_unprompted_language` -- an unprompted
    send, same rule as reminders/the weekly review (ROADMAP.md v0.6.0)."""
    lang = i18n.resolve_unprompted_language(config)
    lines = compute_daily_summary(db, config, registry, today or date.today())
    return format_daily_summary(lines, lang)
