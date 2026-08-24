"""Consistency heatmap (SPEC-v1.6.md §4 Feature 2, R-H1-R-H4, module
`heatmap`): `/heatmap [<habit>] [<weeks>]` renders a GitHub-style calendar
PNG -- one cell per day, colour = that day's goal-fulfillment -- for one
habit or, by default, every configured habit (one strip per habit, stacked
in one image).

Mirrors `core/charts.py`'s optional-`matplotlib`/graceful-fallback pattern
exactly (same `MATPLOTLIB_AVAILABLE`/`_warn_missing_once` shape, R-H2):
missing matplotlib, or any render-time exception, makes `render()` return
`None` -- never raise -- and `execute_heatmap` replies with a bilingual
text summary instead (R-H2/AC-H2). Registry-generic (R-X1): every read
goes through `db.sum_value`/`count`/`count_true` and `targets.
effective_goal`, the exact same aggregation surface `core/streaks.py:
day_qualifies` already reads -- an extra configured habit shows up here
with zero code changes, and soft-deleted/`unparsed` rows are excluded for
free (those DB methods already filter them, same as `charts.py`/`query.
py`/`streaks.py`). Every read is scoped to `user_id` (R-X2/AC-X3).

R-H3 (accept the Thai-tofu limitation explicitly): the PNG itself draws
NOTHING but digits (`1`..`7` row ticks, ISO weekday numbers) and English
month abbreviations (`ax.set_xticklabels` via `date.strftime("%b")`, which
uses Python's default "C" locale regardless of `lang` -- this app never
calls `locale.setlocale`, so this is always English, never Thai). No
habit label -- bilingual or otherwise -- is ever drawn on the figure; the
multi-habit case identifies each strip only by its plain, ASCII-guaranteed
`Habit.id` (`config.py`'s `_HABIT_ID_RE` = `^[a-z0-9_]+$`) for anyone
reading the un-captioned bytes directly (e.g. saved to disk) -- their
proper bilingual label + weeks + row order all live in the chat caption
`execute_heatmap` builds (`send_image`'s `caption`, not the image).

Design simplification (not spec-mandated, a deliberate choice): each
column is a fixed 7-day block ending at today, NOT a calendar week aligned
to Monday -- so the grid never contains a "future" day needing a distinct
blank/masked colour. `weeks` defaults to 12 (R-H1) and is capped at 52 (a
year) -- same "sane default + cap, no spec-mandated number" posture
`core/history_view.py`'s own `DEFAULT_LIMIT`/`MAX_LIMIT` already
established for this codebase's other tail-grammar commands.
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from habit_assistant.core import i18n, streaks, targets

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

try:
    import matplotlib

    matplotlib.use("Agg")  # non-interactive, offline -- mirrors core/charts.py
    import matplotlib.pyplot as plt
    import numpy as np

    MATPLOTLIB_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised in envs without the [charts] extra
    plt = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    MATPLOTLIB_AVAILABLE = False

_warned_missing = False

# R-H1: default 12 weeks; not spec-mandated but a sane upper bound (1 year)
# so `/heatmap water 9999` can't ask for an absurdly wide/slow image --
# mirrors history_view.py's DEFAULT_LIMIT/MAX_LIMIT convention exactly.
DEFAULT_WEEKS = 12
MAX_WEEKS = 52


def _warn_missing_once() -> None:
    global _warned_missing
    if not _warned_missing:
        logger.warning(
            "/heatmap requested but matplotlib is not installed; showing a text summary "
            'instead. Install with: pip install -e ".[charts]"'
        )
        _warned_missing = True


def _today_in_timezone(clock, tz_name: str) -> date:
    """Mirrors `core/query.py:_today_in_timezone` / `core/reminders.py:
    _today_str`'s own identical convention (each module keeps its own
    private copy rather than sharing one) -- a naive `clock()` is treated
    as already being in `tz_name`; an aware one is converted to it."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.date()


def _effective_weeks(weeks: int | None) -> int:
    """A missing/non-positive weeks count uses the default (12); any given
    count is capped at 52 -- mirrors `history_view._effective_limit`."""
    if weeks is None or weeks <= 0:
        return DEFAULT_WEEKS
    return min(weeks, MAX_WEEKS)


def _day_grid(today: date, weeks: int) -> list[list[date]]:
    """`weeks` columns x 7 rows of dates, oldest-to-newest, left-to-right;
    the very last cell (bottom-right) is always `today`. Each column is a
    fixed 7-day block (not a calendar week aligned to Monday) -- see the
    module docstring's "Design simplification" note -- so every cell is a
    real past-or-today date; nothing needs masking as "future"."""
    start = today - timedelta(days=weeks * 7 - 1)
    return [[start + timedelta(days=week * 7 + day) for day in range(7)] for week in range(weeks)]


def _flatten_day_strs(day_grid: list[list[date]]) -> list[str]:
    return [d.isoformat() for week in day_grid for d in week]


def _day_intensity(db: "Database", config: "Config", habit: "Habit", day_str: str, user_id: str) -> float:
    """R-H1/task-brief: "intensity from goal-fulfillment (goal-bearing: %
    of effective goal via targets.effective_goal; goal-less: any-entry)".
    Branches exactly like `core/streaks.py:day_qualifies` (goal ->
    `sum_value` comparison; boolean -> `count_true`; else -> `count`) so
    this module's notion of "on track" never drifts from the app's one
    streak/qualification definition -- the only difference is a continuous
    fraction (clamped to [0, 1]) instead of a bool for the goal-bearing
    case, so a heatmap cell can shade partial progress, not just met/not."""
    goal = targets.effective_goal(db, habit, config, user_id)
    if goal:
        total = db.sum_value(user_id, habit.id, day_str)
        return max(0.0, min(total / goal, 1.0))
    if habit.type == "boolean":
        return 1.0 if db.count_true(user_id, habit.id, day_str) > 0 else 0.0
    return 1.0 if db.count(user_id, habit.id, day_str) > 0 else 0.0


def _set_month_ticks(ax, day_grid: list[list[date]]) -> None:
    """R-H3: the ONLY x-axis text is an English month abbreviation
    (`strftime("%b")`, default "C" locale -- this app never calls
    `locale.setlocale`, confirmed via a full-repo grep), placed at the
    first column of each month that appears in the grid."""
    ticks: list[int] = []
    labels: list[str] = []
    last_month: str | None = None
    for col, week in enumerate(day_grid):
        month = week[0].strftime("%b")
        if month != last_month:
            ticks.append(col)
            labels.append(month)
            last_month = month
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=7)


def _render_png(db: "Database", config: "Config", habits: list["Habit"], day_grid: list[list[date]], user_id: str) -> bytes:
    weeks = len(day_grid)
    n = len(habits)
    fig, axes = plt.subplots(n, 1, figsize=(max(6.0, weeks * 0.32), 1.6 * n), dpi=150, squeeze=False)
    cmap = plt.get_cmap("Greens")

    for row_idx, (ax, habit) in enumerate(zip(axes[:, 0], habits)):
        matrix = np.zeros((7, weeks))
        for col, week in enumerate(day_grid):
            for row, day in enumerate(week):
                matrix[row, col] = _day_intensity(db, config, habit, day.isoformat(), user_id)
        ax.imshow(matrix, cmap=cmap, vmin=0.0, vmax=1.0, aspect="auto")
        # R-H3: row ticks are plain ISO weekday numbers (1=Mon..7=Sun) --
        # numbers only, never a weekday NAME (which would need a Thai
        # variant to stay bilingual-consistent and isn't worth the risk).
        ax.set_yticks(range(7))
        ax.set_yticklabels([str(i + 1) for i in range(7)], fontsize=7)
        if row_idx == n - 1:
            _set_month_ticks(ax, day_grid)
        else:
            ax.set_xticks([])

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def render(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    habit_id: str | None,
    weeks: int | None,
    clock=datetime.now,
) -> bytes | None:
    """R-H1: a calendar-grid PNG for `user_id` -- `habit_id`'s own strip
    if given (assumed already validated against `registry`; `execute_
    heatmap` is where an unresolved habit token gets the friendly
    `heatmap_invalid_habit` reply, same recognize-shape/execute split
    every other module in this codebase uses), else one stacked strip per
    configured habit, registry order. `None` whenever there is nothing to
    render for `user_id` at all: matplotlib unavailable (R-H2), the
    registry is empty, `habit_id` doesn't resolve, or rendering itself
    raises (caught here, logged, never propagated -- R-H2/AC-H2)."""
    del lang  # R-H3: no bilingual text is ever drawn into the image -- see module docstring.
    if not MATPLOTLIB_AVAILABLE:
        _warn_missing_once()
        return None

    if habit_id is not None:
        habit = registry.get(habit_id)
        habits = [habit] if habit is not None else []
    else:
        habits = list(registry)
    if not habits:
        return None

    eff_weeks = _effective_weeks(weeks)
    today = _today_in_timezone(clock, config.app.timezone)
    day_grid = _day_grid(today, eff_weeks)

    try:
        return _render_png(db, config, habits, day_grid, user_id)
    except Exception:
        logger.exception("Heatmap rendering failed for user %r; falling back to text", user_id)
        return None


# ===========================================================================
# execute_heatmap -- the /heatmap, ปฏิทิน `core/commands.dispatch` kind
# feeds. Owns the caption (bilingual, R-H3) and the no-render text fallback
# (R-H2). SPEC-v1.6.md §5: `-> str` -- a NON-empty string is a reply
# `main.py`'s integration step should `channel.send` (the invalid-habit
# reply, or the R-H2 text fallback); an EMPTY string means the image (with
# its own bilingual caption) was already delivered via `channel.send_image`
# inside this function -- nothing more to send. See this module's own
# IMPL-v1.6-heatmap.md "How it works" for the exact main.py wiring this
# contract expects (integration seam, not this module's own file).
# ===========================================================================


def _resolve_habits(registry: "HabitRegistry", habit_id: str | None) -> list["Habit"]:
    if habit_id is not None:
        habit = registry.get(habit_id)
        return [habit] if habit is not None else []
    return list(registry)


def _build_caption(habits: list["Habit"], lang: i18n.Language, weeks: int) -> str:
    if len(habits) == 1:
        return i18n.t("heatmap_caption_single", lang, habit=habits[0].label(lang), weeks=weeks)
    habit_list = ", ".join(h.label(lang) for h in habits)
    return i18n.t("heatmap_caption_all", lang, weeks=weeks, habit_list=habit_list)


def _build_fallback_text(
    db: "Database", config: "Config", habits: list["Habit"], today: date, weeks: int, user_id: str, lang: i18n.Language
) -> str:
    """R-H2's "friendly text summary": for each habit, how many of the
    period's days qualified (`streaks.day_qualifies` -- the SAME
    definition the streak/milestone/dashboard-adjacent features use, R-X1)
    out of the total. Read-only, LLM-free, never raises on its own (every
    DB read here is the same aggregation surface `render`'s own `_day_
    intensity` uses, already proven fail-open by every other caller)."""
    day_strs = [(today - timedelta(days=offset)).isoformat() for offset in range(weeks * 7 - 1, -1, -1)]
    total_days = len(day_strs)
    lines = []
    for habit in habits:
        goal = targets.effective_goal(db, habit, config, user_id)
        qualifying = sum(1 for d in day_strs if streaks.day_qualifies(db, config, habit, d, user_id, goal=goal))
        lines.append(i18n.t("heatmap_fallback_line", lang, habit=habit.label(lang), qualifying=qualifying, total=total_days))
    header = i18n.t("heatmap_fallback_header", lang, weeks=weeks)
    return "\n".join([header, *lines])


async def execute_heatmap(
    command: "Command",
    *,
    db: "Database",
    channel: "Channel",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    clock=datetime.now,
) -> str:
    """R-H1/R-H2: validates `command.category` (an unresolved habit token
    -> `heatmap_invalid_habit`, same convention as `history_view.
    render_history`'s own `history_invalid_habit`, R-D2's precedent) then
    either sends the rendered PNG (`channel.send_image`, bilingual caption,
    R-H3) or replies with the R-H2 text fallback. Never raises (R-3.4 "all
    read-only surfaces ... never raise" -- AC-H2's own "never crashes"):
    every DB/render/send step is inside a fail-open guard."""
    if command.category is not None and registry.get(command.category) is None:
        return i18n.t("heatmap_invalid_habit", lang, habit_id=command.category, habit_list=", ".join(registry.ids()))

    habits = _resolve_habits(registry, command.category)
    if not habits:
        return i18n.t("heatmap_no_habits", lang)

    eff_weeks = _effective_weeks(command.limit)
    today = _today_in_timezone(clock, config.app.timezone)

    try:
        image = render(db, config, registry, lang, user_id, command.category, command.limit, clock)
    except Exception:
        logger.exception("execute_heatmap: render() raised unexpectedly for user %r", user_id)
        image = None

    if image is not None:
        caption = _build_caption(habits, lang, eff_weeks)
        try:
            await channel.send_image(user_id, image, caption)
            return ""
        except Exception:
            logger.exception("execute_heatmap: send_image failed for user %r", user_id)
            # Fall through to the text fallback below -- a delivery
            # failure must still leave the user with SOMETHING (R-3.4).

    try:
        return _build_fallback_text(db, config, habits, today, eff_weeks, user_id, lang)
    except Exception:
        logger.exception("execute_heatmap: text fallback failed unexpectedly for user %r", user_id)
        return i18n.t("heatmap_fallback_header", lang, weeks=eff_weeks)
