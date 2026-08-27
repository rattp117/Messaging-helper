"""Deterministic week-over-week trends (SPEC-v1.6.md §4 Feature 4, module
`insights`, R-T1-R-T3): a transparent, zero-LLM contrast to the existing
LLM-narrated weekly review -- per habit, this rolling week's total vs last
week's, the signed delta + percent change, and the run-length of
consecutive rising/falling weeks.

Two independent halves:
- **`render`** (R-T2): the `/trends [habit]` view.
- **`review_block`** (R-T2): a self-contained block for `main.py`/`core/
  review.py`'s own weekly-review composition to append (mirrors `core/
  garmin.py:format_garmin_section`'s identical "return a block, the
  caller decides whether/where to append it" contract) -- integration
  wires this in, not this module.

Both are built on `compute` (R-T1), the one place the actual week-over-
week math happens -- same "compute once, format twice" split every other
view pair in this codebase already uses (`core/review.py:
compute_weekly_stats` -> `format_stats_summary`/`render_weekly_review_
charts`).

Reuses `core/records.py`'s own `period_total` (the SAME `insights` module,
so this is intra-module reuse -- see that module's own docstring for why
this is imported rather than reimplemented: the two modules' aggregates
must never diverge, since a user comparing `/records`' `best_week` against
`/trends`' weekly totals for the same data should always see numbers that
agree). "Today" and "the 7 ISO day strings ending at a date" both resolve
via `core/timeutil.py` (SPEC-REFACTOR.md Stage 3 rule 12(b)/(e))."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from habit_assistant.core import i18n, timeutil
from habit_assistant.core.records import period_total
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET, fit_within_budget

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-T1: how far back the run-length walk looks before giving up -- mirrors
# `core/streaks.py:_MAX_LOOKBACK_DAYS`'s own generous-but-bounded safety
# cap (that one bounds a per-day walk at ~10 years; this bounds a per-week
# walk at 2 years, plenty for any realistic "N weeks rising" callout while
# keeping a single `/trends` call's worst-case DB cost bounded).
_MAX_LOOKBACK_WEEKS = 104


@dataclass(slots=True)
class HabitTrend:
    habit: "Habit"
    current_total: float
    previous_total: float
    delta: float
    pct_change: int | None  # None: no previous-week baseline to divide by (R-T3)
    has_history: bool  # False: no logged entries at all in the previous week (R-T3's own "no last-week data")
    rising_weeks: int  # >=2 when the current week extends a rising run (R-T2's own callout gate)
    falling_weeks: int


def _weekly_totals_backward(
    db: "Database", habit: "Habit", user_id: str, end_date: date, max_weeks: int
) -> list[float]:
    """Most-recent-first weekly totals (`totals[0]` = the week ending
    `end_date`, `totals[1]` = the week before that, ...), stopping at the
    first week with NO logged entries at all -- not just a zero aggregate
    (R-T3: a week of nothing-but-"no" boolean logs genuinely has history;
    a week nobody touched at all does not). A gap in an otherwise-active
    habit's history must not be silently treated as a legitimate "0" data
    point for the run-length walk, nor mistaken for a real previous-week
    baseline. The current week (index 0) is always included even if it
    happens to have zero entries too -- there is nothing to compare it
    against either way (`len(totals) == 1`)."""
    totals: list[float] = []
    day = end_date
    for i in range(max_weeks):
        days = timeutil.week_days(day)
        if i > 0 and sum(db.count(user_id, habit.id, d) for d in days) == 0:
            break
        totals.append(period_total(db, habit, user_id, days))
        day -= timedelta(days=7)
    return totals


def _run_lengths(totals: list[float]) -> tuple[int, int]:
    """`(rising_weeks, falling_weeks)`: the number of most-recent weeks
    (counting the current week itself, `totals[0]`) forming a strictly
    monotonic run against the immediately preceding week. `totals[0] >
    totals[1]` starts a rising run of (at least) 2 weeks; each further
    strictly-greater step backward extends it by one more (symmetric for
    falling). Flat (`totals[0] == totals[1]`) or too little data
    (`len(totals) < 2`) -> `(0, 0)` for both -- R-T2's own callout only
    fires at `>= 2`, so a fresh/flat week correctly never triggers it."""
    if len(totals) < 2:
        return 0, 0
    if totals[0] > totals[1]:
        weeks = 2
        for i in range(1, len(totals) - 1):
            if totals[i] > totals[i + 1]:
                weeks += 1
            else:
                break
        return weeks, 0
    if totals[0] < totals[1]:
        weeks = 2
        for i in range(1, len(totals) - 1):
            if totals[i] < totals[i + 1]:
                weeks += 1
            else:
                break
        return 0, weeks
    return 0, 0


def _compute_one(db: "Database", habit: "Habit", user_id: str, today: date) -> HabitTrend:
    totals = _weekly_totals_backward(db, habit, user_id, today, _MAX_LOOKBACK_WEEKS)
    current_total = totals[0]
    has_history = len(totals) > 1
    previous_total = totals[1] if has_history else 0.0
    delta = current_total - previous_total
    pct_change = round(100 * delta / previous_total) if has_history and previous_total > 0 else None
    rising_weeks, falling_weeks = _run_lengths(totals) if has_history else (0, 0)
    return HabitTrend(
        habit=habit,
        current_total=current_total,
        previous_total=previous_total,
        delta=delta,
        pct_change=pct_change,
        has_history=has_history,
        rising_weeks=rising_weeks,
        falling_weeks=falling_weeks,
    )


def compute(
    db: "Database", config: "Config", registry: "HabitRegistry", user_id: str, clock=datetime.now
) -> list[HabitTrend]:
    """R-T1: this-week-vs-last-week totals (two rolling 7-day windows,
    the SAME "week" convention `core/timeutil.week_days` uses), per
    configured habit, in registry order (R-X1). Pure, deterministic,
    read-only aggregation over `db.sum_value`/`count`/`count_true` --
    zero LLM calls anywhere in this module."""
    today = timeutil.today_in_timezone(clock, config.app.timezone)
    return [_compute_one(db, habit, user_id, today) for habit in registry]


# ===========================================================================
# Formatting -- shared by render() and review_block().
# ===========================================================================


def _format_trend_line(trend: HabitTrend, lang: i18n.Language) -> str:
    habit = trend.habit
    label = habit.label(lang)

    if not trend.has_history:
        return i18n.t("trends_line_no_history", lang, label=label)

    is_numeric = habit.type in ("numeric", "duration")
    unit = (habit.unit(lang) or "") if is_numeric else ""

    if trend.pct_change is None:
        msg_id = "trends_line_no_pct" if is_numeric else "trends_line_no_pct_count"
        line = i18n.t(
            msg_id, lang, label=label, previous=trend.previous_total, current=trend.current_total, unit=unit
        )
    else:
        msg_id = "trends_line" if is_numeric else "trends_line_count"
        line = i18n.t(
            msg_id,
            lang,
            label=label,
            previous=trend.previous_total,
            current=trend.current_total,
            unit=unit,
            pct=trend.pct_change,
        )

    if trend.rising_weeks >= 2:
        line += " · " + i18n.t("trends_rising_suffix", lang, weeks=trend.rising_weeks)
    elif trend.falling_weeks >= 2:
        line += " · " + i18n.t("trends_falling_suffix", lang, weeks=trend.falling_weeks)
    return line


# ===========================================================================
# render -- R-T2: /trends [habit].
# ===========================================================================


def render(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    habit_id: str | None = None,
    clock=datetime.now,
) -> str:
    """R-T2: `user_id`'s own week-over-week trend, one self-headed (📊)
    line per configured habit for a bare `/trends` (registry-generic,
    R-X1), or just the one line for `/trends <habit>`. An unresolved
    `habit_id` (validated HERE, mirrors `core/history_view.py:
    render_history`'s identical split) -> `trends_invalid_habit`.
    Render-budget-aware (R-B1's shared machinery).

    Fail-open (SPEC-v1.6.md §3.4: "All read-only surfaces ... never
    raise; a DB/render/edit failure is logged and degraded"): the whole
    body below is wrapped in one `try`, same posture as `core/records.py:
    render`."""
    try:
        if habit_id is not None:
            habit = registry.get(habit_id)
            if habit is None:
                return i18n.t(
                    "trends_invalid_habit", lang, habit_id=habit_id, habit_list=", ".join(registry.ids())
                )
            today = timeutil.today_in_timezone(clock, config.app.timezone)
            return _format_trend_line(_compute_one(db, habit, user_id, today), lang)

        trend_list = compute(db, config, registry, user_id, clock)
        lines = [_format_trend_line(t, lang) for t in trend_list]
        full = "\n".join(lines)
        if len(full) <= TELEGRAM_MESSAGE_BUDGET:
            return full
        return fit_within_budget(
            "", lines, render_footer=lambda dropped: i18n.t("trends_more_habits", lang, count=dropped)
        )
    except Exception:
        logger.exception("Rendering /trends failed for %s (fail-open)", user_id)
        return i18n.t("trends_render_failed", lang)


# ===========================================================================
# review_block -- R-T2's own weekly-review integration surface.
# ===========================================================================


def review_block(
    db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language, user_id: str, clock=datetime.now
) -> str:
    """R-T2: a self-contained "📊 Trends" block, one line per configured
    habit, for `main.py`/`core/review.py`'s own weekly-review composition
    to append (mirrors `core/garmin.py:format_garmin_section`'s identical
    "return a block, the caller decides whether/where to append it"
    contract -- integration wires this in, not this module).

    Fail-open, same posture as `render` above: a failure here must not
    take down the rest of `main.py`'s weekly-review composition (the LLM
    narrative, the chart attachments, ...) alongside it."""
    try:
        trend_list = compute(db, config, registry, user_id, clock)
        lines = [_format_trend_line(t, lang) for t in trend_list]
        return "\n".join([i18n.t("trends_review_header", lang), *lines])
    except Exception:
        logger.exception("Building the weekly-review trends block failed for %s (fail-open)", user_id)
        return i18n.t("trends_render_failed", lang)
