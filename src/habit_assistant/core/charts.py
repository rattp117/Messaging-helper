"""Per-habit weekly chart rendering (ROADMAP.md v1.0.0 "Insights: Charts-as-
Images"). Renders one PNG per chartable habit over the 7-day review window:
numeric habits get daily bars (plus a goal line when a goal is configured);
duration/boolean habits get daily count bars; text habits have nothing
numeric to plot and are skipped. Entirely offline (matplotlib's
non-interactive `Agg` backend -- no display, no network, AC1.0.5).

`matplotlib` is an OPTIONAL dependency (`pip install -e ".[charts]"`,
ROADMAP.md v1.0.0's "one new dependency in the whole roadmap"). The import
below is guarded so an install without it never crashes: with
`[charts] enabled = true` in config but matplotlib missing, every render
call here logs once and returns None, and `core/review.py` simply attaches
zero images -- the weekly review still sends as plain text (AC1.0.1)."""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta

from habit_assistant.config import Config
from habit_assistant.core import i18n, targets
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

try:
    import matplotlib

    matplotlib.use("Agg")  # non-interactive, offline -- no display backend needed
    import matplotlib.pyplot as plt

    from habit_assistant.core.fonts import register_thai_font

    # SPEC-v1.9.md Rule 23/AC6: additive Thai fallback (DejaVu Sans stays
    # primary) -- idempotent, so this is a no-op on a second module (e.g.
    # core/heatmap.py) importing after this one already registered it.
    register_thai_font()

    MATPLOTLIB_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised in envs without the [charts] extra
    plt = None  # type: ignore[assignment]
    MATPLOTLIB_AVAILABLE = False

_warned_missing = False


def _warn_missing_once() -> None:
    global _warned_missing
    if not _warned_missing:
        logger.warning(
            "[charts] enabled but matplotlib is not installed; weekly review will be "
            'text-only. Install with: pip install -e ".[charts]"'
        )
        _warned_missing = True


def _week_days(end_date: date) -> list[str]:
    return [(end_date - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]


def _day_labels(day_strs: list[str]) -> list[str]:
    return [date.fromisoformat(d).strftime("%a") for d in day_strs]


def render_habit_chart(
    db: Database, config: Config, habit: Habit, end_date: date, lang: i18n.Language, user_id: str
) -> bytes | None:
    """One habit's weekly PNG for `user_id`, or None if there's nothing to
    render for it (text habits, or matplotlib unavailable). Never raises:
    any matplotlib failure is caught, logged, and treated as "no chart" so
    a single bad render can't take down the whole weekly review
    (AC1.0.1). SPEC-v1.2.md R-D3: every DB read scoped to `user_id`
    (AC-U-ISO)."""
    if not MATPLOTLIB_AVAILABLE:
        _warn_missing_once()
        return None
    if habit.type == "text":
        return None

    day_strs = _week_days(end_date)
    labels = _day_labels(day_strs)

    try:
        if habit.type == "numeric":
            values = [db.sum_value(user_id, habit.id, d) for d in day_strs]
            # SPEC-v1.1.md R-T5/AC22: a numeric habit's goal-line reflects a
            # DB target override the moment one is set (targets.effective_goal).
            goal = targets.effective_goal(db, habit, config, user_id)
            unit = habit.unit(lang) or ""
            return _render_bar_chart(labels, values, habit.label(lang), unit, goal)
        if habit.type == "duration":
            values = [float(db.count(user_id, habit.id, d)) for d in day_strs]
            return _render_bar_chart(labels, values, habit.label(lang), i18n.t("chart_ylabel_count", lang), None)
        # boolean
        values = [float(db.count_true(user_id, habit.id, d)) for d in day_strs]
        return _render_bar_chart(labels, values, habit.label(lang), i18n.t("chart_ylabel_count", lang), None)
    except Exception:
        logger.exception("Chart rendering failed for habit %r; skipping its chart", habit.id)
        return None


def _render_bar_chart(
    labels: list[str], values: list[float], title: str, ylabel: str, goal: float | None
) -> bytes:
    fig, ax = plt.subplots(figsize=(6, 3.2), dpi=150)
    ax.bar(labels, values, color="#4C72B0")
    if goal:
        ax.axhline(goal, color="#C44E52", linestyle="--", linewidth=1.5)
    ax.set_title(title, fontsize=12)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(axis="both", labelsize=8)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def render_weekly_charts(
    db: Database, config: Config, registry: HabitRegistry, end_date: date, lang: i18n.Language, user_id: str
) -> list[tuple[Habit, bytes]]:
    """One `(habit, png_bytes)` pair per chartable habit for `user_id`, in
    registry order. Empty when matplotlib is unavailable or every habit is
    type `text`. This function doesn't read `config.charts.enabled` itself
    -- that gate lives in `core/review.py`, so this stays a pure "render
    what you can" helper other callers (tests, a future export command)
    can use without threading the flag through."""
    charts: list[tuple[Habit, bytes]] = []
    for habit in registry:
        image = render_habit_chart(db, config, habit, end_date, lang, user_id)
        if image is not None:
            charts.append((habit, image))
    return charts
