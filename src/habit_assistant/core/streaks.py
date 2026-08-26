"""Per-habit streak computation, milestone-crossing detection, and the
daily-summary aggregation (ROADMAP.md v0.10.0 "Streaks, Gentle
Gamification & Daily Summary"; reworked for SPEC-v1.9.md "Life happens"
Theme A -- weekly-cadence goals, grace days, pause/vacation mode).

ONE definition of "streak" for the whole app (AC10.5): `compute_streak`
below is the sole streak algorithm. `core/review.py`'s weekly "current
streak" column, `core/records.py`'s longest-streak tracking, `core/
dashboard.py`'s per-row streak, and this module's own milestone-crossing
check all call it, so none of them can ever diverge -- `tests/
test_streaks.py` asserts the same numbers come back from every call site
on the same seeded data. All computation here is read-only and reuses the
same `Database` aggregations (`sum_value`/`count`/`count_true`) every
other module already reads through (AC10.5's "reuses v0.7 aggregation").

SPEC-v1.9.md §4 (the engine rework, Rules 1-7): `compute_streak` keeps its
EXACT signature (AC2 -- every existing caller is unchanged) but is now
cadence/pause/grace-aware internally:

- `classify_day` (new, Rule 2): a date is QUALIFIED if `day_qualifies`
  (below, unchanged rule) is true; NEUTRAL if it is paused OR
  grace-protected (and not otherwise qualifying -- a real entry always
  wins, Rule 16); MISSED otherwise.
- **Daily walk** (Rule 3, no `habit_cadence` row): walk backward from
  `end_date`; QUALIFIED -> `streak += 1`; NEUTRAL -> skip (held, doesn't
  increment, doesn't break); MISSED -> break. **Reduction guarantee**:
  with no `pauses` rows and no `grace_ledger` rows, `paused_dates`/
  `grace_protected_dates` are always empty sets, so every day is
  QUALIFIED-or-MISSED -- this walk is then BYTE-IDENTICAL to the pre-v1.9
  algorithm (count consecutive qualifying, break on first gap). This is
  the byte-identical gate's core (AC2/AC3).
- **Weekly walk** (Rule 4, a `habit_cadence` row with `per_week=N`): the
  streak unit becomes ISO weeks (Mon-Sun). A completed week is MET if its
  qualifying-day count >= N; NEUTRAL if paused enough that fewer than N
  non-paused days remain (N was unreachable, not failed); else MISSED.
  The CURRENT (partial) week only counts once it is already MET
  (qualifying-days-so-far >= N) -- never over-reported mid-week.
- `streak_unit` (Rule 5, new): "week" iff the habit has a cadence row,
  else "day" -- every renderer that wants unit-aware wording (milestone,
  records, review, dashboard, daily summary) is meant to consult this;
  none of them do yet in this shared-surface pass (integration wiring,
  SPEC-v1.9.md §6, lands with the four parallel modules).
- Grace applies ONLY to daily (non-cadence) habits (Rule 6) -- the weekly
  walk never reads `grace_protected_dates` at all, so a cadence habit can
  never have a grace-neutral day (avoids double tolerance, AC16).

Streak definition, unchanged (ROADMAP.md v0.10.0 scope item 1):
- goal-bearing habit (an *effective* goal is configured -- see
  `core/targets.py:effective_goal`, SPEC-v1.1.md R-T3): a day "qualifies"
  when that day's total (`db.sum_value`) meets or exceeds the goal.
- non-goal habit: a day "qualifies" on any entry -- `count_true` (truthy
  rows only) for boolean habits ("done-days"), `count` (any row) for
  duration/text/numeric-without-goal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

from habit_assistant.core import i18n, pause, targets
from habit_assistant.core.habits import Habit

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

# A streak can't sensibly run longer than this many days -- caps the
# backward walk so a years-old, always-logged habit doesn't turn a single
# confirmation/summary into an unbounded table scan.
_MAX_LOOKBACK_DAYS = 3650

# SPEC-v1.9.md Rule 4: the weekly-walk equivalent of `_MAX_LOOKBACK_DAYS`
# above -- 520 ISO weeks is exactly 3640 days (~10 years), the same order
# of magnitude cap, just expressed in the cadence walk's own unit.
_MAX_LOOKBACK_WEEKS = 520

DayState = Literal["qualified", "neutral", "missed"]

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


def classify_day(
    db: "Database",
    config: "Config",
    habit: Habit,
    day: str,
    user_id: str,
    *,
    goal: float | None,
    paused_dates: set[str],
    grace_dates: set[str],
) -> DayState:
    """SPEC-v1.9.md Rule 2: QUALIFIED if `day_qualifies` (unchanged rule)
    is true; NEUTRAL if `day` is in `paused_dates` OR `grace_dates`
    (checked only once `day_qualifies` has already said no -- Rule 16's
    "a real entry beats the neutral default", so a voluntary log on a
    paused/grace-protected day still counts as QUALIFIED, never NEUTRAL);
    MISSED otherwise. `paused_dates`/`grace_dates` are the caller's own
    once-resolved sets for the whole walk (`compute_streak`'s own R95
    "loads ... once" rule) -- this function does no DB read of its own for
    either, only for `day_qualifies`'s own aggregation lookup (already
    unavoidable per day, unchanged from pre-v1.9)."""
    if day_qualifies(db, config, habit, day, user_id, goal=goal):
        return "qualified"
    if day in paused_dates or day in grace_dates:
        return "neutral"
    return "missed"


def _daily_walk(
    db: "Database", config: "Config", habit: Habit, end_date: date, user_id: str, goal: float | None
) -> int:
    """SPEC-v1.9.md Rule 3: the no-cadence walk. QUALIFIED -> increment;
    NEUTRAL -> skip (held -- doesn't increment, doesn't break); MISSED ->
    break. **Reduction guarantee**: with empty `paused_dates`/
    `grace_dates` (AC2/AC3's own precondition -- no `pauses` row, no
    `grace_ledger` row for anyone), `classify_day` can only ever return
    "qualified" or "missed" for any date, so this loop is BYTE-IDENTICAL
    to the pre-v1.9 algorithm (count consecutive qualifying, break on
    first gap) -- the same loop shape, just routed through `classify_day`
    instead of a bare `day_qualifies` check."""
    lookback_start = end_date - timedelta(days=_MAX_LOOKBACK_DAYS - 1)
    paused_dates = db.paused_dates(user_id, habit.id, lookback_start.isoformat(), end_date.isoformat())
    # Rule 6: grace applies only to daily habits -- this IS the daily
    # walk, so grace dates are always consulted here (never for the
    # weekly walk below).
    grace_dates = db.grace_protected_dates(user_id, habit.id, lookback_start.isoformat(), end_date.isoformat())

    streak = 0
    day = end_date
    for _ in range(_MAX_LOOKBACK_DAYS):
        state = classify_day(
            db, config, habit, day.isoformat(), user_id, goal=goal, paused_dates=paused_dates, grace_dates=grace_dates
        )
        if state == "qualified":
            streak += 1
        elif state == "missed":
            break
        # "neutral": held -- neither increments nor breaks.
        day -= timedelta(days=1)
    return streak


def _iso_week_bounds(day: date) -> tuple[date, date]:
    """Monday..Sunday (inclusive) of the ISO week `day` falls in --
    `date.isoweekday()` is 1 (Mon) through 7 (Sun), so subtracting
    `isoweekday() - 1` days always lands on that week's Monday."""
    monday = day - timedelta(days=day.isoweekday() - 1)
    return monday, monday + timedelta(days=6)


def _week_qualifying_count(
    db: "Database", config: "Config", habit: Habit, week_start: date, week_end: date, user_id: str, goal: float | None
) -> int:
    """How many days in `[week_start, week_end]` (inclusive) qualify
    (Rule 4's "qualifying-day count") -- plain `day_qualifies`, NOT
    `classify_day`: a paused day that also carries a genuine log still
    qualifies (Rule 16), and a paused day with no log simply doesn't
    qualify -- pause's effect on a cadence habit's week is entirely
    captured by `_week_paused_count` below (how many non-paused days
    remain, Rule 4's "unreachable, not failed" NEUTRAL condition), never
    by this count."""
    count = 0
    day = week_start
    while day <= week_end:
        if day_qualifies(db, config, habit, day.isoformat(), user_id, goal=goal):
            count += 1
        day += timedelta(days=1)
    return count


def _week_paused_count(week_start: date, week_end: date, paused_dates: set[str]) -> int:
    count = 0
    day = week_start
    while day <= week_end:
        if day.isoformat() in paused_dates:
            count += 1
        day += timedelta(days=1)
    return count


def _weekly_walk(
    db: "Database", config: "Config", habit: Habit, end_date: date, user_id: str, per_week: int, goal: float | None
) -> int:
    """SPEC-v1.9.md Rule 4: the cadence walk, streak unit = ISO weeks.
    Rule 6: grace never applies to a cadence habit -- this walk never
    reads `grace_protected_dates` at all (no ledger row can exist for one
    by construction, R9, but this walk doesn't even give grace a chance
    to matter).

    The CURRENT (possibly partial) week counts toward the streak only if
    it is ALREADY MET (qualifying-days-so-far, from `week_start` through
    `end_date`, >= `per_week`) -- never evaluated as NEUTRAL/MISSED (a
    week that hasn't finished yet can't be judged as "failed", Rule 4's
    own "never over-reported mid-week"). Every PRIOR (completed) week
    then walks MET -> increment, NEUTRAL -> skip (held), MISSED -> break,
    mirroring the daily walk's own three-way shape at week granularity."""
    lookback_start = end_date - timedelta(weeks=_MAX_LOOKBACK_WEEKS)
    paused_dates = db.paused_dates(user_id, habit.id, lookback_start.isoformat(), end_date.isoformat())

    streak = 0
    current_week_start, _ = _iso_week_bounds(end_date)
    if _week_qualifying_count(db, config, habit, current_week_start, end_date, user_id, goal) >= per_week:
        streak += 1

    week_end = current_week_start - timedelta(days=1)  # the prior week's Sunday
    for _ in range(_MAX_LOOKBACK_WEEKS):
        week_start = week_end - timedelta(days=6)
        qualifying = _week_qualifying_count(db, config, habit, week_start, week_end, user_id, goal)
        if qualifying >= per_week:
            streak += 1
        else:
            non_paused_remaining = 7 - _week_paused_count(week_start, week_end, paused_dates)
            if non_paused_remaining >= per_week:
                break  # MISSED -- N was genuinely reachable but not met
            # else NEUTRAL -- held -- doesn't increment, doesn't break.
        week_end = week_start - timedelta(days=1)
    return streak


def compute_streak(db: "Database", config: "Config", habit: Habit, end_date: date, user_id: str) -> int:
    """Consecutive qualifying days (or, for a cadence habit, qualifying
    ISO weeks -- SPEC-v1.9.md Rule 4) ending at (and including)
    `end_date`, walking backward until the first genuine gap, for
    `user_id`. Shared by `core/review.py`'s weekly summary, `core/
    records.py`'s longest-streak tracking, `core/dashboard.py`'s per-row
    streak, and this module's own milestone-crossing check (AC10.5) --
    one function, one number, everywhere a streak is surfaced. SIGNATURE
    UNCHANGED from pre-v1.9 (AC2) -- every existing caller needs no edit.

    SPEC-v1.1.md R-T6: `targets.effective_goal` is resolved ONCE here, up
    front, and passed down through every `day_qualifies`/`classify_day`
    call in the walk -- not re-resolved per day, even though the walk can
    span up to `_MAX_LOOKBACK_DAYS`/`_MAX_LOOKBACK_WEEKS` iterations
    (AC26). SPEC-v1.9.md Rule 1: `db.get_cadence` is likewise checked
    ONCE, up front, to pick daily vs. weekly -- never inside either walk's
    own loop."""
    goal = targets.effective_goal(db, habit, config, user_id)
    per_week = db.get_cadence(user_id, habit.id)
    if per_week is None:
        return _daily_walk(db, config, habit, end_date, user_id, goal)
    return _weekly_walk(db, config, habit, end_date, user_id, per_week, goal)


def streak_unit(db: "Database", habit: Habit, user_id: str) -> Literal["day", "week"]:
    """SPEC-v1.9.md Rule 5: "week" iff `habit` has a `habit_cadence` row
    for `user_id`, else "day" -- every renderer that wants unit-aware
    streak wording (milestone message, records celebration/view,
    dashboard row, weekly review, daily summary) is meant to consult this
    rather than assuming "day" (SPEC-v1.9.md §6 integration wiring, not
    yet wired to any renderer in this shared-surface pass)."""
    return "week" if db.get_cadence(user_id, habit.id) is not None else "day"


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
    shows "1800/2500 ml".

    SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): `unit` is this habit's
    `streak_unit` ("day" for every pre-v1.9 habit, byte-identical; "week"
    for a cadence habit) -- `format_daily_summary` below selects the
    matching i18n variant from it, mirroring every other renderer this
    release's engine rework makes unit-aware."""

    habit: Habit
    total: float
    goal: float | None
    streak: int
    unit: Literal["day", "week"] = "day"


def compute_daily_summary(
    db: "Database", config: "Config", registry: "HabitRegistry", today: date, user_id: str
) -> list[DailySummaryLine]:
    """Today's per-habit total (+ goal, when configured) and current streak
    for `user_id`, in registry order -- read-only, reuses the same
    `Database` aggregations as the weekly review and confirmations
    (AC10.5). SPEC-v1.2.md R-D3: scoped throughout (AC-U2/AC-U3).

    SPEC-v1.9.md R15/AC20 (v1.9 integration pass): a habit currently paused
    for `user_id` contributes no line at all to this proactive recap --
    mirrors `core/checkins.py:build_checkin_message`'s/`core/nudge.py:
    build_nudge_message`'s identical per-habit pause skip; other,
    non-paused habits still get their own line as usual."""
    today_str = today.isoformat()
    lines: list[DailySummaryLine] = []
    for habit in registry:
        if pause.is_paused(db, config, user_id, habit.id, today):
            continue
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
        unit = streak_unit(db, habit, user_id)
        lines.append(DailySummaryLine(habit=habit, total=total, goal=goal, streak=streak, unit=unit))
    return lines


def format_daily_summary(lines: list[DailySummaryLine], lang: i18n.Language) -> str:
    """Bilingual rendering via the i18n catalog (AC10.3).

    SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): each line's own
    `unit` ("day"/"week", set by `compute_daily_summary` above) selects
    between the pre-v1.9 template (day wording, byte-identical -- AC3) and
    its new `_weeks`-suffixed sibling (week wording) -- a non-cadence
    habit's `unit` is always "day", so its output is unchanged."""
    out = [i18n.t("daily_summary_header", lang)]
    for hs in lines:
        habit = hs.habit
        label = habit.label(lang)
        unit = habit.unit(lang) or ""
        weeks = hs.unit == "week"

        if habit.type in ("numeric", "duration") and hs.goal:
            pct = round(100 * hs.total / hs.goal) if hs.goal else 0
            out.append(
                i18n.t(
                    "daily_summary_numeric_goal_weeks" if weeks else "daily_summary_numeric_goal",
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
                i18n.t(
                    "daily_summary_numeric_nogoal_weeks" if weeks else "daily_summary_numeric_nogoal",
                    lang,
                    label=label,
                    total=hs.total,
                    unit=unit,
                    streak=hs.streak,
                )
            )
        elif habit.type == "duration":
            out.append(
                i18n.t(
                    "daily_summary_duration_nogoal_weeks" if weeks else "daily_summary_duration_nogoal",
                    lang,
                    label=label,
                    total=int(hs.total),
                    streak=hs.streak,
                )
            )
        elif habit.type == "boolean":
            status = i18n.t("bool_status_done" if hs.total > 0 else "bool_status_not_done", lang)
            out.append(
                i18n.t(
                    "daily_summary_boolean_weeks" if weeks else "daily_summary_boolean",
                    lang,
                    label=label,
                    status=status,
                    streak=hs.streak,
                )
            )
        else:  # text
            out.append(
                i18n.t(
                    "daily_summary_text_weeks" if weeks else "daily_summary_text",
                    lang,
                    label=label,
                    total=int(hs.total),
                    streak=hs.streak,
                )
            )
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
