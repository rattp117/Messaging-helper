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
  `core/targets.py:effective_goal`, SPEC-v1.1.md R-T3): a day "qualifies"
  when that day's total (`db.sum_value`) meets or exceeds the goal.
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

from habit_assistant.core import i18n, targets
from habit_assistant.core.habits import Habit

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

# A streak can't sensibly run longer than this many days -- caps the
# backward walk so a years-old, always-logged habit doesn't turn a single
# confirmation/summary into an unbounded table scan.
_MAX_LOOKBACK_DAYS = 3650

# Sentinel: "no pre-resolved goal was passed" (distinct from a resolved
# goal of `None`, which means "this habit genuinely has no goal right
# now"). SPEC-v1.1.md R-T6: `day_qualifies` resolves its own goal via
# `targets.effective_goal` (a DB read) when this default is left in place
# -- fine for a single-day call site, but `compute_streak`'s backward walk
# below passes its own once-resolved `goal` explicitly instead, so the
# walk issues at most one `db.get_target` call per invocation, not one per
# day (AC26).
_GOAL_UNSET = object()


def day_qualifies(
    db: "Database",
    config: "Config",
    habit: Habit,
    day: str,
    user_id: str,
    goal: float | None = _GOAL_UNSET,  # type: ignore[assignment]
) -> bool:
    """Does `day` ('YYYY-MM-DD') count toward `user_id`'s streak for this
    habit? `goal` lets a caller that already resolved
    `targets.effective_goal` (e.g. `compute_streak`'s loop, R-T6) pass it
    straight through instead of triggering a second DB read; omit it to
    resolve fresh (the common case for a single-day check). SPEC-v1.2.md
    R-D3: every DB read here is scoped to `user_id` -- two users'
    qualification for the same habit/day are computed independently
    (AC-U2/AC-U-ISO)."""
    if goal is _GOAL_UNSET:
        goal = targets.effective_goal(db, habit, config, user_id)
    if goal:
        return db.sum_value(user_id, habit.id, day) >= goal
    if habit.type == "boolean":
        return db.count_true(user_id, habit.id, day) > 0
    return db.count(user_id, habit.id, day) > 0


def compute_streak(db: "Database", config: "Config", habit: Habit, end_date: date, user_id: str) -> int:
    """Consecutive qualifying days ending at (and including) `end_date`,
    walking backward until the first gap (AC10.1), for `user_id`. Shared by
    `core/review.py`'s weekly summary and this module's own
    milestone-crossing check (AC10.5) -- one function, one number,
    everywhere a streak is surfaced.

    SPEC-v1.1.md R-T6: `targets.effective_goal` is resolved ONCE here, up
    front, and passed to every `day_qualifies` call in the walk below --
    not re-resolved (i.e. not a fresh `db.get_target` read) per day, even
    though the walk can span up to `_MAX_LOOKBACK_DAYS` iterations (AC26)."""
    goal = targets.effective_goal(db, habit, config, user_id)
    streak = 0
    day = end_date
    for _ in range(_MAX_LOOKBACK_DAYS):
        if not day_qualifies(db, config, habit, day.isoformat(), user_id, goal=goal):
            break
        streak += 1
        day -= timedelta(days=1)
    return streak


def crossed_milestone(
    db: "Database", config: "Config", habit: Habit, today: date, was_qualified_before: bool, user_id: str
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
    if not day_qualifies(db, config, habit, today.isoformat(), user_id):
        return None
    streak = compute_streak(db, config, habit, today, user_id)
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
    db: "Database", config: "Config", registry: "HabitRegistry", today: date, user_id: str
) -> list[DailySummaryLine]:
    """Today's per-habit total (+ goal, when configured) and current streak
    for `user_id`, in registry order -- read-only, reuses the same
    `Database` aggregations as the weekly review and confirmations
    (AC10.5). SPEC-v1.2.md R-D3: scoped throughout (AC-U2/AC-U3)."""
    today_str = today.isoformat()
    lines: list[DailySummaryLine] = []
    for habit in registry:
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal or habit.type == "numeric":
            # Goal-bearing (any type) or a goal-less numeric habit: both are
            # inherently summed quantities (e.g. ml), matching what
            # `day_qualifies` compares against the goal when one is set.
            total = db.sum_value(user_id, habit.id, today_str)
        elif habit.type == "boolean":
            total = float(db.count_true(user_id, habit.id, today_str))
        else:  # duration (no goal), text
            total = float(db.count(user_id, habit.id, today_str))
        streak = compute_streak(db, config, habit, today, user_id)
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
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    today: date | None = None,
) -> str:
    """Aggregate + format the end-of-day recap (AC10.3) for `user_id`.
    `today` defaults to the real current date; tests pass a fixed date for
    determinism. SPEC-v1.2.md: `lang` is now taken pre-resolved from the
    caller (main.py's per-user fan-out job resolves it once per user via
    `i18n.resolve_unprompted_language`) rather than resolved internally --
    this module has no way to know which user's language preference to
    consult on its own once that becomes per-user (R-P1), so the caller
    that DOES know (main.py) resolves it and passes the result, matching
    every other formatter in this codebase's own convention."""
    lines = compute_daily_summary(db, config, registry, today or date.today(), user_id)
    return format_daily_summary(lines, lang)
