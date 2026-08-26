"""Recap "wrapped" card (SPEC-v1.9.md §4 Rules 21-26, module `wrapped`):
`/wrapped [month]` (alias `/recap [month]`, Thai `สรุปเดือน`/`การ์ดสรุป`) renders
ONE composite PNG for the acting user's own active registry (registry-generic
-- custom habits from v1.7 included; cadence-aware, week wording via
`streaks.streak_unit`, AC26) summarizing the last 4 weeks (28 days, the bare-
command default per the confirmed 2026-08-26 product decision, IMPL-v1.9-
shared.md) or, with a `month` tail, the current calendar month to date.

Composes ALREADY-COMPUTED pieces, no new aggregation math (Rule 21's own "It
reuses records.period_total, trends' delta math, and heatmap's intensity grid
-- no new aggregation"):
  - period totals per habit -- `core/records.py:period_total`.
  - each habit's best single day within the window -- `period_total` again,
    called once per day (still no new SUM math, just a `max()` over the same
    per-day values `records.py` already knows how to compute).
  - each habit's current streak, cadence/day-or-week aware -- `core/
    streaks.py:compute_streak`/`streak_unit` (the SAME call-site shape
    `records.py:193`/`dashboard.py:237` already use).
  - the single biggest week-over-week mover across the whole registry --
    `core/trends.py:compute` (picks the largest `|pct_change|`, falling back
    to `|delta|` when there's no previous-week baseline to divide by).
  - a mini per-habit heatmap strip over the window -- `core/heatmap.py:
    _day_intensity` (the SAME goal-fulfillment-fraction cell value the real
    `/heatmap` command shades with -- imported directly rather than
    reimplemented, per Rule 21's "no new aggregation").

matplotlib-optional, the SAME graceful-fallback shape as `core/heatmap.py`
(same `MATPLOTLIB_AVAILABLE`/`_warn_missing_once` pattern, mirrored per Rule
22): missing matplotlib, an empty registry, or any render-time exception ->
`render()` returns `None`, never raises, and `execute_wrapped` sends a
bilingual multi-line text fallback instead (mirrors `heatmap.py`'s R-H2
exactly).

Thai renders as real glyphs, not tofu (Rule 23/AC27): `core/fonts.py:
register_thai_font()` is called (idempotent, additive -- DejaVu stays
primary) under this module's own `MATPLOTLIB_AVAILABLE` guard, exactly like
`charts.py`/`heatmap.py`. UNLIKE the heatmap (which draws no bilingual text
at all, R-H3), this card's habit labels/streak/trend text ARE drawn directly
onto the figure in `lang` -- this is the first module that actually
exercises the bundled font for real chat output, not just non-Thai content.

`_build_figure` (the pure "assemble a Figure" step) is factored out of
`_render_png` (which just calls it, then `savefig`+`close`) purely for
testability: it lets `tests/test_wrapped.py` assert Thai text is actually
present as real matplotlib `Text` artists (`ax.texts`/`fig.texts`) at the
object level, mirroring the task brief's explicit "test at the matplotlib-
object level, not pixel level" instruction -- there is no PNG byte-level way
to assert "this cell contains real Thai glyphs, not tofu" short of shipping
a font-rendering oracle, which is out of scope.

The celebration "sticker" rider (Rule 25/AC29) is a separate, zero-asset
concern living in this same file only because SPEC-v1.9.md §6 assigns it to
module `wrapped`: `celebration_burst(config, lang)` returns a small bundled
emoji string (or `""` when `[wrapped] celebrate_burst` is off) for
`main.py`'s later integration wiring to append to an existing milestone/
record celebration line -- it does not touch the card at all, and needs no
i18n catalog entry (the emoji burst is language-agnostic, per the i18n.py
shared-surface skeleton's own comment)."""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from zoneinfo import ZoneInfo

from habit_assistant.core import i18n, streaks, trends
from habit_assistant.core.records import period_entry_count, period_total

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

try:
    import matplotlib

    matplotlib.use("Agg")  # non-interactive, offline -- mirrors core/charts.py / core/heatmap.py
    import matplotlib.pyplot as plt
    import numpy as np

    from habit_assistant.core.fonts import register_thai_font
    from habit_assistant.core.heatmap import _day_intensity as _heatmap_day_intensity

    # SPEC-v1.9.md Rule 23/AC6/AC27: additive Thai fallback (DejaVu Sans
    # stays primary) -- idempotent, so this is a no-op if charts.py/
    # heatmap.py already registered it earlier in this process.
    register_thai_font()

    MATPLOTLIB_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised in envs without the [charts] extra
    plt = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    MATPLOTLIB_AVAILABLE = False

_warned_missing = False

Period = Literal["4w", "month"]

# Rule 21: the emoji burst itself -- zero-asset, bundled directly (not an
# i18n catalog entry, see module docstring's "celebration burst" section).
_CELEBRATION_BURST = "🎉🎊🥳"

# Bundled fonts (DejaVu Sans / Noto Sans Thai, Rule 23) carry Latin and Thai
# script but not emoji glyphs -- an emoji drawn INTO the PNG itself (as
# opposed to a chat caption/fallback-text string, which Telegram renders
# client-side, unrelated to matplotlib's fonts) would show as a tofu box,
# same failure mode AC27 rejects for Thai text. Several `wrapped_*` catalog
# strings lead with a decorative emoji for chat display; `_for_image` strips
# exactly that leading emoji (+ its trailing space) before the SAME string
# is drawn onto the figure -- the chat caption/fallback text keeps it.
_LEADING_EMOJI_RE = re.compile(r"^[\U0001F300-\U0001FAFF☀-➿]\s*")


def _for_image(text: str) -> str:
    return _LEADING_EMOJI_RE.sub("", text)


def _warn_missing_once() -> None:
    global _warned_missing
    if not _warned_missing:
        logger.warning(
            "/wrapped requested but matplotlib is not installed; showing a text summary "
            'instead. Install with: pip install -e ".[charts]"'
        )
        _warned_missing = True


def _today_in_timezone(clock, tz_name: str) -> date:
    """Mirrors `core/heatmap.py:_today_in_timezone`'s own identical
    convention (each module keeps its own private copy rather than sharing
    one, this codebase's established pattern) -- a naive `clock()` is
    treated as already being in `tz_name`; an aware one is converted to it."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.date()


def _window_days(today: date, period: Period) -> list[str]:
    """Rule 21: "default last 28 days; `month` = current calendar month".
    `"4w"` is a fixed 28-day block ending at (and including) `today`, same
    "fixed block, not calendar-week-aligned" design `core/heatmap.py:
    _day_grid`'s own docstring already settled on (so the window never
    contains a "future" day needing a distinct blank/masked treatment).
    `"month"` starts at day 1 of `today`'s calendar month through `today`
    itself -- a genuinely partial window early in the month, same
    "nothing to show yet, not an error" posture the rest of this app's
    read-only surfaces already take toward sparse data."""
    if period == "month":
        start = today.replace(day=1)
    else:
        start = today - timedelta(days=27)
    days: list[str] = []
    day = start
    while day <= today:
        days.append(day.isoformat())
        day += timedelta(days=1)
    return days


# ===========================================================================
# Per-habit stat assembly -- every number below comes from an existing
# aggregation function (records.period_total, streaks.compute_streak/
# streak_unit); this module invents no new math, per Rule 21.
# ===========================================================================


def _best_day(db: "Database", habit: "Habit", user_id: str, day_strs: list[str]) -> tuple[str | None, float]:
    """The single best day (ISO string) within `day_strs` for `habit`, by
    `period_total` (Rule 21's own "reuses records.period_total" -- called
    once per day here rather than inventing a second sum path). `None`/`0.0`
    when every day in the window is empty."""
    best_day: str | None = None
    best_value = 0.0
    for day_str in day_strs:
        value = period_total(db, habit, user_id, [day_str])
        if best_day is None or value > best_value:
            best_day = day_str
            best_value = value
    return (best_day, best_value) if best_value > 0 else (None, 0.0)


def _format_total(total: float, habit: "Habit", lang: i18n.Language) -> str:
    if habit.type in ("numeric", "duration"):
        unit = habit.unit(lang) or ""
        return f"{total:g}{(' ' + unit) if unit else ''}"
    return i18n.t("wrapped_count_total", lang, count=int(total))


def _streak_text(db: "Database", config: "Config", habit: "Habit", today: date, user_id: str, lang: i18n.Language) -> str:
    """Rule 5/AC26 "cadence-aware": a cadence habit's streak is a WEEKS-MET
    count and renders with week wording; a daily habit renders with day
    wording -- `streaks.streak_unit` is the single switch, exactly like
    every other unit-aware renderer this release's engine rework calls for."""
    streak = streaks.compute_streak(db, config, habit, today, user_id)
    unit = streaks.streak_unit(db, habit, user_id)
    msg_id = "wrapped_streak_weeks" if unit == "week" else "wrapped_streak_days"
    return i18n.t(msg_id, lang, count=streak)


def _biggest_mover(trend_list: list["trends.HabitTrend"]) -> "trends.HabitTrend | None":
    """Rule 21's "the biggest week-over-week trend": the single habit whose
    move (by `|pct_change|`, falling back to `|delta|` when there's no
    previous-week baseline to divide by -- the same fallback `core/
    trends.py:_compute_one` itself already applies) is largest, among
    habits that actually have a previous week to compare against. `None`
    when nothing has any history yet, or every habit is perfectly flat."""
    candidates = [t for t in trend_list if t.has_history and t.delta != 0]
    if not candidates:
        return None

    def _score(t: "trends.HabitTrend") -> float:
        return abs(t.pct_change) if t.pct_change is not None else abs(t.delta)

    return max(candidates, key=_score)


def _mover_text(mover: "trends.HabitTrend", lang: i18n.Language) -> str:
    sign = "+" if mover.delta > 0 else ""
    label = mover.habit.label(lang)
    if mover.pct_change is not None:
        return i18n.t("wrapped_biggest_mover_pct", lang, label=label, sign=sign, pct=mover.pct_change)
    return i18n.t("wrapped_biggest_mover_delta", lang, label=label, sign=sign, delta=mover.delta)


def _period_label(period: Period, today: date, lang: i18n.Language) -> str:
    """R-H3-adjacent limitation, accepted explicitly (same as `core/
    heatmap.py`'s own documented one): this app never calls `locale.
    setlocale`, so `strftime("%B")` always yields an ENGLISH month name
    regardless of `lang` -- the `wrapped_period_month` Thai variant wraps
    that English name in Thai surrounding text rather than pretending to
    localize the month name itself."""
    if period == "month":
        return i18n.t("wrapped_period_month", lang, month=today.strftime("%B %Y"))
    return i18n.t("wrapped_period_4w", lang)


# ===========================================================================
# Figure assembly -- render()/execute_wrapped()'s PNG path (Rule 22).
# ===========================================================================


def _build_figure(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    day_strs: list[str],
    period: Period,
    today: date,
    clock,
):
    """Assembles the composite card as a matplotlib `Figure`, WITHOUT
    saving/closing it -- split out from `_render_png` purely so tests can
    inspect the real `Text` artists (`ax.texts`) at the object level
    (confirming Thai glyphs are actually drawn, AC27) before the figure is
    rasterized to PNG bytes and closed. Callers other than tests should go
    through `render()`/`_render_png`, which own the save+close lifecycle."""
    habits = list(registry)
    n = max(len(habits), 1)
    total_entries = sum(period_entry_count(db, h, user_id, day_strs) for h in habits)

    fig = plt.figure(figsize=(7.5, 1.3 + 1.0 * n + 0.9), dpi=150)
    gs = fig.add_gridspec(n + 2, 1, height_ratios=[1.0, *([1.0] * n), 0.9], hspace=0.6)

    title_ax = fig.add_subplot(gs[0])
    title_ax.axis("off")
    title_ax.text(0.0, 0.65, _for_image(i18n.t("wrapped_title", lang)), fontsize=16, fontweight="bold", va="center")
    title_ax.text(0.0, 0.15, _period_label(period, today, lang), fontsize=10, va="center", color="#555555")

    if not habits or total_entries == 0:
        empty_ax = fig.add_subplot(gs[1 : n + 1])
        empty_ax.axis("off")
        empty_ax.text(
            0.5, 0.5, _for_image(i18n.t("wrapped_empty_period", lang)), fontsize=13, ha="center", va="center", wrap=True
        )
    else:
        cmap = plt.get_cmap("Greens")
        for row, habit in enumerate(habits):
            # A nested 2-column gridspec per habit -- the mini heatmap strip
            # (narrow) and its text summary (wide) -- rather than drawing
            # the text via `ax.text(x > 1, ...)` axes-fraction overflow:
            # that placement is clipped at the FIGURE's own canvas edge on
            # save (`clip_on` only governs clipping to the axes, not
            # extending the canvas), so the summary text was silently
            # invisible in the saved PNG until this was caught in the
            # module's own smoke test. Giving the text its own subplot
            # keeps it on-canvas and independently inspectable (`ax.texts`).
            row_gs = gs[row + 1].subgridspec(1, 2, width_ratios=[0.32, 0.68], wspace=0.04)
            strip_ax = fig.add_subplot(row_gs[0, 0])
            text_ax = fig.add_subplot(row_gs[0, 1])

            intensities = np.array([[_heatmap_day_intensity(db, config, habit, d, user_id) for d in day_strs]])
            strip_ax.imshow(intensities, cmap=cmap, vmin=0.0, vmax=1.0, aspect="auto")
            strip_ax.set_xticks([])
            strip_ax.set_yticks([])
            for spine in strip_ax.spines.values():
                spine.set_visible(False)

            text_ax.axis("off")
            total = period_total(db, habit, user_id, day_strs)
            best_day, _best_value = _best_day(db, habit, user_id, day_strs)
            best_day_text = best_day if best_day is not None else i18n.t("wrapped_best_day_none", lang)
            summary = i18n.t(
                "wrapped_habit_line",
                lang,
                label=habit.label(lang),
                total=_format_total(total, habit, lang),
                best_day=best_day_text,
                streak=_streak_text(db, config, habit, today, user_id, lang),
            )
            text_ax.text(0.0, 0.5, summary, fontsize=8.5, va="center", ha="left", wrap=True)

    mover_ax = fig.add_subplot(gs[n + 1])
    mover_ax.axis("off")
    mover = _biggest_mover(trends.compute(db, config, registry, user_id, clock)) if habits else None
    if mover is not None:
        mover_ax.text(0.0, 0.5, _for_image(_mover_text(mover, lang)), fontsize=9.5, va="center")

    # Not `fig.tight_layout()`: several rows are `axis("off")` (title/
    # empty-state/mover), which tight_layout warns it can't size correctly
    # alongside the imshow rows -- explicit margins avoid the warning and
    # give the same predictable "no wasted whitespace" result, since the
    # gridspec's own `height_ratios`/`hspace` already control row sizing.
    fig.subplots_adjust(left=0.02, right=0.98, top=0.97, bottom=0.03, hspace=0.6)
    return fig


def _render_png(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    day_strs: list[str],
    period: Period,
    today: date,
    clock,
) -> bytes:
    fig = _build_figure(db, config, registry, lang, user_id, day_strs, period, today, clock)
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
    period: Period,
    clock=datetime.now,
) -> bytes | None:
    """AC25/AC26: one composite PNG for `user_id`'s own registry over
    `period` ("4w" = last 28 days, "month" = current calendar month to
    date). `None` whenever there's nothing to render for `user_id` at all:
    matplotlib unavailable (Rule 22), the registry is empty, or rendering
    itself raises (caught here, logged, never propagated -- mirrors `core/
    heatmap.py:render`'s identical contract)."""
    if not MATPLOTLIB_AVAILABLE:
        _warn_missing_once()
        return None
    if len(registry) == 0:
        return None

    today = _today_in_timezone(clock, config.app.timezone)
    day_strs = _window_days(today, period)

    try:
        return _render_png(db, config, registry, lang, user_id, day_strs, period, today, clock)
    except Exception:
        logger.exception("Wrapped-card rendering failed for user %r; falling back to text", user_id)
        return None


# ===========================================================================
# execute_wrapped -- the /wrapped, /recap, สรุปเดือน/การ์ดสรุป command.
# Owns the caption (bilingual, sample copy per SPEC-v1.9.md §3) and the
# no-render text fallback. A non-empty string return is a reply `main.py`'s
# integration step should `channel.send`; an empty string means the image
# (with its own bilingual caption) was already delivered via `channel.
# send_image` -- mirrors `core/heatmap.py:execute_heatmap`'s identical
# empty-string-means-already-sent contract.
# ===========================================================================


def _build_caption(habits: list["Habit"], lang: i18n.Language, period: Period, today: date) -> str:
    habit_list = ", ".join(h.label(lang) for h in habits)
    if period == "month":
        return i18n.t("wrapped_caption_month", lang, month=today.strftime("%B %Y"), habit_list=habit_list)
    return i18n.t("wrapped_caption_4w", lang, habit_list=habit_list)


def _build_fallback_text(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    period: Period,
    today: date,
) -> str:
    """Mirrors `core/heatmap.py:_build_fallback_text`'s own shape exactly
    (Rule 22's "mirrors heatmap.py R-H2 exactly"): read-only, LLM-free,
    never raises on its own -- every read here is the same aggregation
    surface `render()`'s own figure-building step uses, already proven
    fail-open by every other caller in this codebase."""
    day_strs = _window_days(today, period)
    header = i18n.t("wrapped_fallback_header", lang, period_label=_period_label(period, today, lang))
    habits = list(registry)
    total_entries = sum(period_entry_count(db, h, user_id, day_strs) for h in habits)
    if not habits or total_entries == 0:
        return "\n".join([header, i18n.t("wrapped_empty_period", lang)])

    lines = [header]
    for habit in habits:
        total = period_total(db, habit, user_id, day_strs)
        lines.append(
            i18n.t(
                "wrapped_fallback_line",
                lang,
                label=habit.label(lang),
                total=_format_total(total, habit, lang),
                streak=_streak_text(db, config, habit, today, user_id, lang),
            )
        )
    return "\n".join(lines)


async def execute_wrapped(
    command: "Command",
    *,
    db: "Database",
    channel: "Channel",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    clock=datetime.now,
    disable_notification: bool = False,
) -> str:
    """AC25/AC26/AC27: sends the rendered PNG (`channel.send_image`,
    bilingual caption) when possible, else replies with the bilingual text
    fallback. Never raises (mirrors `core/heatmap.py:execute_heatmap`'s
    identical fail-open posture): every DB/render/send step is inside a
    fail-open guard. `command.pref_value == "month"` selects the current-
    calendar-month window (Rule 21); anything else (including `None`, or an
    unrecognized tail `core/commands.py:_match_wrapped` didn't reduce to
    "month") defaults to the bare "last 4 weeks" window -- same lenient-
    tail posture `core/heatmap.py`/`core/history_view.py` already take
    toward trailing content their own shape-only layer doesn't validate.

    SPEC-v1.9.md R26/AC28 (v1.9 integration pass): `disable_notification`
    is additive, keyword-only, DEFAULTED `False` -- the interactive
    `/wrapped`/`/recap` command path never passes it (byte-identical
    `channel.send_image(user_id, image, caption)` call, no kwarg at all,
    so every pre-existing test fake's `send_image(self, chat_id, image,
    caption)` shape keeps working unmodified); only the optional month-end
    auto-send job (`main.py`, R26's "one SILENT card per active user")
    passes `True`."""
    habits = list(registry)
    if not habits:
        return i18n.t("wrapped_no_habits", lang)

    period: Period = "month" if command.pref_value == "month" else "4w"
    today = _today_in_timezone(clock, config.app.timezone)

    try:
        image = render(db, config, registry, lang, user_id, period, clock)
    except Exception:
        logger.exception("execute_wrapped: render() raised unexpectedly for user %r", user_id)
        image = None

    if image is not None:
        caption = _build_caption(habits, lang, period, today)
        try:
            if disable_notification:
                await channel.send_image(user_id, image, caption, disable_notification=True)
            else:
                await channel.send_image(user_id, image, caption)
            return ""
        except Exception:
            logger.exception("execute_wrapped: send_image failed for user %r", user_id)
            # Fall through to the text fallback below -- a delivery failure
            # must still leave the user with SOMETHING.

    try:
        return _build_fallback_text(db, config, registry, lang, user_id, period, today)
    except Exception:
        logger.exception("execute_wrapped: text fallback failed unexpectedly for user %r", user_id)
        return i18n.t("wrapped_fallback_header", lang, period_label=_period_label(period, today, lang))


# ===========================================================================
# celebration_burst -- Rule 25/AC29. Zero-asset, no i18n key (module
# docstring's own "celebration burst" section explains why).
# ===========================================================================


def celebration_burst(config: "Config", lang: i18n.Language) -> str:
    """`""` when `[wrapped] celebrate_burst` is off; else the bundled
    emoji-burst string for `main.py`'s later integration wiring to append
    to an existing milestone/record celebration line (Rule 25's own sample:
    "...keep it going!\\n🎉🎊🥳"). `lang` is accepted for interface
    consistency with every other bilingual formatter in this release (and
    in case a future locale wants a different burst) but is unused today --
    an emoji burst has no language to select between."""
    del lang
    if not config.wrapped.celebrate_burst:
        return ""
    return _CELEBRATION_BURST
