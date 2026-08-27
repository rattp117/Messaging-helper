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
from datetime import date, datetime
from typing import Literal

from habit_assistant.config import Config
from habit_assistant.core import charts, garmin, i18n, pause, streaks, targets, timeutil, trends
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import WEEKLY_REVIEW_SYSTEM_PROMPT, WEEKLY_REVIEW_USER_TEMPLATE
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


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

    SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): `unit` is this habit's
    `streak_unit` ("day" for every pre-v1.9 habit -- byte-identical output,
    AC3 -- "week" for a cadence habit) -- consulted by `_format_stretch`/
    `_format_generic`'s duration branch (the only place this dataclass's
    own `streak` field is ever rendered) to pick the matching i18n variant.
    """

    habit: Habit
    days: list[DayValue]
    total: float
    avg: float
    streak: int
    unit: Literal["day", "week"] = "day"


@dataclass(slots=True)
class WeeklyStats:
    end_date: date
    habits: list[HabitStats]

    def get(self, habit_id: str) -> HabitStats | None:
        return next((hs for hs in self.habits if hs.habit.id == habit_id), None)


def _compute_habit_stats(
    db: Database, config: Config, habit: Habit, day_strs: list[str], end_date: date, user_id: str
) -> HabitStats:
    if habit.type == "numeric":
        values = [db.sum_value(user_id, habit.id, d) for d in day_strs]
        # SPEC-v1.1.md R-T5: `targets.effective_goal` is the same helper
        # every other goal-consuming module reads (streaks, reminders,
        # charts, main.py's confirmations) -- one place decides what a
        # habit's "goal" is, DB override included (AC10.5, R-T3).
        # SPEC-v1.2.md R-D2: scoped to `user_id`.
        goal = targets.effective_goal(db, habit, config, user_id)
        days = [DayValue(d, v, goal) for d, v in zip(day_strs, values)] if goal else []
        total = sum(values)
        avg = round(total / len(values), 1) if values else 0.0
        return HabitStats(habit=habit, days=days, total=total, avg=avg, streak=0)

    if habit.type == "duration":
        counts = [db.count(user_id, habit.id, d) for d in day_strs]
        total = sum(counts)
        # ROADMAP.md v0.10.0 AC10.5: the weekly review's duration streak
        # now comes from the SAME function `core/streaks.py` uses for
        # milestone detection and the daily summary, instead of a
        # second, window-local streak loop -- no divergent math. This is
        # a superset of the old algorithm's behavior for any streak that
        # fits inside the 7-day window (every existing test's seeded
        # data does); it additionally looks further back than 7 days
        # when the streak is actually longer, which the old
        # window-clamped loop under-reported.
        streak = streaks.compute_streak(db, config, habit, end_date, user_id)
        # SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): this is the
        # ONLY branch that ever renders `streak` (see `_format_stretch`/
        # `_format_generic`'s duration case) -- `streak_unit` picks the
        # matching i18n variant there.
        unit = streaks.streak_unit(db, habit, user_id)
        return HabitStats(habit=habit, days=[], total=total, avg=0.0, streak=streak, unit=unit)

    if habit.type == "text":
        total = sum(db.count(user_id, habit.id, d) for d in day_strs)
        return HabitStats(habit=habit, days=[], total=total, avg=0.0, streak=0)

    # boolean: "done-days" -- the number of days with at least one truthy
    # log, not the raw row count (a habit logged twice in one day still
    # counts as one done day).
    done_days = sum(1 for d in day_strs if db.count_true(user_id, habit.id, d) > 0)
    return HabitStats(habit=habit, days=[], total=done_days, avg=0.0, streak=0)


def compute_weekly_stats(
    db: Database, config: Config, registry: HabitRegistry, end_date: date, user_id: str
) -> WeeklyStats:
    """Aggregate the 7 days ending on end_date (inclusive), once per
    registered habit, in registry order, for `user_id` (SPEC-v1.2.md R-D3,
    AC-U2/AC-U4).

    SPEC-v1.9.md R15/AC20 (v1.9 integration pass): a habit currently paused
    for `user_id` (as of `end_date`, the review's own reference day) is
    excluded from the review entirely -- mirrors the same per-habit pause
    skip `core/checkins.py`/`core/nudge.py`/`core/streaks.compute_daily_
    summary` all apply to their own proactive sends; other, non-paused
    habits still get their usual section.

    SPEC-v1.10.md §4 R18 (module `riders`): the per-habit check is
    `pause.is_paused_safe`, not `pause.is_paused` -- a pauses-read
    failure for this user is logged and treated as "not paused" (the
    habit still gets its section, including its chart via `render_
    weekly_review_charts`'s own call to this same function) rather than
    raising out of this comprehension and aborting `weekly_review_job`'s
    own fan-out for the users after this one (AC16)."""
    day_strs = timeutil.week_days(end_date)
    habits = [
        _compute_habit_stats(db, config, habit, day_strs, end_date, user_id)
        for habit in registry
        if not pause.is_paused_safe(db, config, user_id, habit.id, end_date)
    ]
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
    # SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): `stretch` is a
    # built-in `duration` habit, so it can carry a cadence row like any
    # other -- `hs.unit` (set by `_compute_habit_stats`) picks the week
    # variant; a non-cadence `stretch` (the pre-v1.9 default) always has
    # `unit == "day"`, so this is byte-identical to v1.8.1 (AC3).
    msg_id = "stats_stretch_summary_weeks" if hs.unit == "week" else "stats_stretch_summary"
    return [i18n.t(msg_id, lang, stretch_total=int(hs.total), stretch_streak=hs.streak)]


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
        # SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): unit-aware, same
        # switch as `_format_stretch` above.
        msg_id = "stats_generic_duration_summary_weeks" if hs.unit == "week" else "stats_generic_duration_summary"
        return [i18n.t(msg_id, lang, label=label, total=int(hs.total), streak=hs.streak)]

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
    db: Database,
    config: Config,
    registry: HabitRegistry,
    llm: OllamaClient,
    lang: i18n.Language,
    user_id: str,
    today: date | None = None,
) -> str:
    """Aggregate + narrate for `user_id`. Falls back to the plain stats
    block (no narrative) if the LLM call fails, so the review still gets
    sent.

    ROADMAP.md v0.6.0 AC6.4: the weekly review is an unprompted send (no
    inbound message to detect a language from). SPEC-v1.2.md: `lang` is
    now taken pre-resolved from the caller (main.py's per-user fan-out job)
    instead of resolved internally via `i18n.resolve_unprompted_language`
    -- this module has no way to know which user's language preference to
    consult once that becomes per-user (R-P1); the caller that does know
    resolves it once and passes the result. The narrative's system prompt
    gets the same target language via `i18n.language_instruction`, so the
    LLM-generated prose matches the factual stats block instead of being
    English inside a Thai message; the "no medical advice" constraint
    (SPEC.md/ROADMAP.md AC6.4) stays in the (English, LLM-facing, not
    user-facing) instruction text itself and is unaffected by which
    language the narrative comes back in.

    ROADMAP.md v1.0.0: a Garmin hydration cross-check section is appended
    when `[garmin] csv_path` is configured (`core/garmin.py`); with the
    default empty `csv_path` (feature off), `format_garmin_section`
    returns `""` and this function's output is byte-identical to v0.10.0
    (zero regression for every pre-v1.0 review test)."""
    end_date = today or date.today()
    stats = compute_weekly_stats(db, config, registry, end_date, user_id)
    summary = format_stats_summary(stats, registry, lang)

    narrative = await llm.chat_text(
        WEEKLY_REVIEW_SYSTEM_PROMPT.format(language_instruction=i18n.language_instruction(lang)),
        WEEKLY_REVIEW_USER_TEMPLATE.format(stats_summary=summary),
    )
    if not narrative:
        narrative = i18n.t("weekly_review_fallback_narrative", lang)

    header = i18n.t("weekly_review_header", lang)
    text = f"{header}\n\n{summary}\n\n{narrative}"

    garmin_section = garmin.format_garmin_section(
        garmin.build_garmin_report(db, config, end_date, user_id), lang
    )
    if garmin_section:
        text += f"\n\n{garmin_section}"

    # SPEC-v1.6.md R-T2 (module `insights`): a deterministic week-over-week
    # trend block, appended last (after the LLM narrative and the Garmin
    # cross-check) -- `review_block` needs "today" to resolve to THIS
    # review's own `end_date` (which may not be the real "today" if a
    # caller ever passes an explicit `today=`, e.g. a test), so it's given
    # a clock pinned to `end_date` rather than the real wall clock.
    # `review_block` is sync and already fail-open internally (mirrors
    # `garmin.format_garmin_section`'s identical "return '' on any
    # problem, never raise" contract) -- no try/except needed here.
    #
    # SPEC-v1.9.md R15/AC20 (v1.9 integration pass): this embedded trend
    # block is part of the SAME proactive weekly-review send, so a
    # currently-paused habit is excluded from it too -- via a filtered
    # registry (`trends.py` itself is untouched; the on-demand `/trends`
    # command still shows every habit, per Rule 10's own "pause mutes
    # proactive sends only; the user can always query on demand").
    #
    # SPEC-v1.10.md §4 R18 (module `riders`): `pause.is_paused_safe`, not
    # `pause.is_paused` -- a pauses-read failure here is logged and
    # treated as "not paused" (the habit stays in the trends block)
    # rather than raising out of `run_weekly_review` itself and aborting
    # `weekly_review_job`'s fan-out for the users after this one (AC16).
    trends_registry = HabitRegistry(
        [h for h in registry if not pause.is_paused_safe(db, config, user_id, h.id, end_date)]
    )
    trends_section = trends.review_block(
        db, config, trends_registry, lang, user_id, clock=lambda: datetime.combine(end_date, datetime.min.time())
    )
    if trends_section:
        text += f"\n\n{trends_section}"
    return text


def _chart_caption(hs: HabitStats, lang: i18n.Language) -> str:
    habit = hs.habit
    label = habit.label(lang)
    if habit.type == "numeric":
        return i18n.t("chart_caption_numeric", lang, label=label, total=hs.total, unit=habit.unit(lang) or "", avg=hs.avg)
    if habit.type == "duration":
        # SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): unit-aware, same
        # switch as `_format_stretch`/`_format_generic` above.
        msg_id = "chart_caption_duration_weeks" if hs.unit == "week" else "chart_caption_duration"
        return i18n.t(msg_id, lang, label=label, total=hs.total, streak=hs.streak)
    return i18n.t("chart_caption_boolean", lang, label=label, total=hs.total)  # boolean


def render_weekly_review_charts(
    db: Database,
    config: Config,
    registry: HabitRegistry,
    lang: i18n.Language,
    user_id: str,
    today: date | None = None,
) -> list[tuple[bytes, str]]:
    """ROADMAP.md v1.0.0 AC1.0.1: `(png_bytes, caption)` pairs to attach to
    `user_id`'s weekly review. `[]` whenever there's nothing to attach --
    `[charts] enabled = false`, matplotlib not installed
    (`core/charts.py` logs once and returns no images), or every
    configured habit is type `text` -- so `main.py`'s call site never
    needs its own enabled/failure branching: a text-only review is simply
    "attach zero images". SPEC-v1.2.md: `lang` is now taken pre-resolved
    (see `run_weekly_review`'s own note on why)."""
    if not config.charts.enabled:
        return []
    end_date = today or date.today()
    stats = compute_weekly_stats(db, config, registry, end_date, user_id)
    stats_by_habit_id = {hs.habit.id: hs for hs in stats.habits}

    pairs: list[tuple[bytes, str]] = []
    for habit, image in charts.render_weekly_charts(db, config, registry, end_date, lang, user_id):
        # SPEC-v1.9.md R15/AC20 (v1.9 integration pass): `compute_weekly_
        # stats` above already excludes a currently-paused habit -- skip
        # its chart too (`charts.render_weekly_charts` iterates the FULL
        # registry independently, so a paused habit's id genuinely has no
        # entry in `stats_by_habit_id` here; without this guard the lookup
        # below would raise `KeyError` for exactly that habit).
        hs = stats_by_habit_id.get(habit.id)
        if hs is None:
            continue
        pairs.append((image, _chart_caption(hs, lang)))
    return pairs
