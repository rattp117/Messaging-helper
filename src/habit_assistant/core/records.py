"""Personal bests & records (SPEC-v1.6.md §4 Feature 3, module `insights`,
R-R1-R-R4): lifetime per-user, per-habit records -- best single day, best
rolling week, longest-ever streak -- stored (not re-derived) so "did this
log just beat a record?" is one cheap compare per record type, and
"celebrate once" is exact (mirrors `core/streaks.py:crossed_milestone`'s
own once-per-crossing design, R-R1's explicit stated parallel).

Two independent halves:
- **`update_on_log`** (R-R2): called from the SAME place `main.py`'s own
  `streaks.crossed_milestone` check already runs (right after
  `db.insert_log`, before the per-habit-type confirmation is sent) --
  recomputes today's total, this rolling week's total, and the current
  streak for the ONE habit that was just logged, and upserts any
  `RECORD_TYPES` entry that's genuinely improved. A brand-new record (no
  `habit_records` row yet for that `(user_id, habit_id, record_type)`) is
  **seeded silently** -- stored so future comparisons have something to
  compare against, but NOT celebrated (Archi's ruling, 2026-08-24,
  overriding this module's own earlier migration-docstring-based
  resolution: R-R2's "strictly exceeds the stored record" presupposes a
  stored record to exceed, and celebrating a habit's very first log is
  structurally noisier than the milestone precedent it claims to mirror
  -- `streaks.crossed_milestone` never fires on a literal day-1 streak,
  since the default milestone list has no "1"). A celebration fires only
  when a value strictly exceeds an ALREADY-stored record -- same rule for
  all three record types, including `longest_streak` (a first-ever streak
  seeds silently too; it only starts celebrating once it exceeds its own
  previously-seeded length).
- **`render`** (R-R3): the `/records [habit]` view -- registry-generic,
  bilingual, render-budget-aware (reuses `core/render_budget.py`, R-B1's
  shared machinery, same as `core/history_view.py`).

Fail-open (R-R2's own explicit "a records error never blocks the
confirmation" contract): `update_on_log`'s entire body is one `try`,
mirroring `core/audit.py:record`'s identical "structurally hard to
misuse" shape -- a DB/compute failure here can never propagate up into
the log-confirmation path that called it.

No channel import here (mirrors every other formatter/logic module in
this codebase's own "no channel imports" seam)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from habit_assistant.core import i18n, streaks
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET, fit_within_budget

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# SPEC-v1.6.md §5: the closed vocabulary of storable record kinds --
# `storage/db.py:upsert_record`'s own `record_type` values, migration
# 009's `habit_records` table.
RECORD_TYPES: tuple[str, ...] = ("best_day", "best_week", "longest_streak")


# ===========================================================================
# Shared day/week resolution + aggregation -- reused by core/trends.py too
# (both are the SAME `insights` module, so this is intra-module reuse, not
# a new cross-module dependency; mirrors `core/review.py` importing `core/
# streaks.py:compute_streak` rather than reimplementing streak math a
# second time -- the two modules' aggregates must never diverge, since a
# user comparing `/records`' `best_week` against `/trends`' weekly totals
# for the same data should always see numbers that agree). `_today` itself
# stays private and is duplicated in `trends.py`, per this codebase's own
# established convention for this specific "resolve today's date from an
# injectable clock + config timezone" shim (see `core/checkins.py`/`core/
# nudge.py`'s own near-identical, each independently duplicated,
# `_today_str`/`_now_hhmm` helpers) -- it's a trivial, module-local
# mechanical detail, not a business rule that must never diverge.
# ===========================================================================


def _today(config: "Config", clock) -> date:
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(config.app.timezone))
    else:
        now = now.astimezone(ZoneInfo(config.app.timezone))
    return now.date()


def week_day_strs(end_date: date) -> list[str]:
    """The 7 ISO day strings ending at (and including) `end_date` --
    identical convention to `core/review.py:_week_days` (the SAME "week"
    definition SPEC-v1.6.md R-T1 requires `core/trends.py` to share with
    the review)."""
    return [(end_date - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]


def period_total(db: "Database", habit: "Habit", user_id: str, day_strs: list[str]) -> float:
    """R-R1's own aggregate rule, generalized to any list of days (a
    single day for `best_day`, 7 days for `best_week`, an arbitrary
    window for `core/trends.py`'s weekly totals): sum for numeric/
    duration, count of logged rows for boolean/text (`count_true` for
    boolean -- only truthy entries count, mirrors `core/streaks.py:
    day_qualifies`'s own boolean handling -- `count` for text)."""
    if habit.type in ("numeric", "duration"):
        return sum(db.sum_value(user_id, habit.id, d) for d in day_strs)
    if habit.type == "boolean":
        return float(sum(db.count_true(user_id, habit.id, d) for d in day_strs))
    return float(sum(db.count(user_id, habit.id, d) for d in day_strs))  # text


def period_entry_count(db: "Database", habit: "Habit", user_id: str, day_strs: list[str]) -> int:
    """Raw logged-row count over `day_strs`, regardless of habit type or
    truthiness -- `core/trends.py`'s own "does this window have ANY data
    at all" existence check (R-T3), distinct from `period_total` (which,
    for a boolean habit, only counts TRUTHY rows -- a week of nothing but
    explicit "no" logs would have `period_total == 0` but genuinely IS a
    week with history, not an empty one)."""
    return sum(db.count(user_id, habit.id, d) for d in day_strs)


# ===========================================================================
# update_on_log -- R-R2.
# ===========================================================================


def _maybe_break_record(
    db: "Database", user_id: str, habit_id: str, record_type: str, value: float, achieved_on: str
) -> tuple[str, float] | None:
    """Upserts always when `value` is a genuine new best; REPORTS a break
    (returns non-`None`) only when it strictly exceeds an ALREADY-stored
    record. No record existing yet for this `(user_id, habit_id,
    record_type)` is a SILENT seed (Archi's ruling, 2026-08-24): the row
    is written so future comparisons have a baseline, but nothing is
    celebrated -- there is nothing yet for a first observation to have
    "strictly exceeded" (R-R2's own wording). `value <= 0` never creates/
    updates a record at all -- a habit untouched today, or a false
    boolean log, has nothing to seed or celebrate."""
    if value <= 0:
        return None
    current = db.get_record(user_id, habit_id, record_type)
    if current is not None and value <= current:
        return None
    db.upsert_record(user_id, habit_id, record_type, value, achieved_on)
    if current is None:
        return None  # first-ever observation: seeded silently, no celebration
    return record_type, value


def update_on_log(
    db: "Database", config: "Config", registry: "HabitRegistry", habit: "Habit", user_id: str, clock=datetime.now
) -> list[tuple[str, float]]:
    """R-R2: called right after a log lands, in the same place `main.py`'s
    own `streaks.crossed_milestone` check runs -- recomputes today's
    total, this rolling week's total, and the current streak for `habit`,
    and upserts any of the three `RECORD_TYPES` that just improved.
    Returns the `(record_type, value)` pairs that were newly BROKEN --
    i.e. strictly exceeded an already-stored record (`[]` when none
    were) -- so the caller can append one `record_broken` line per entry
    to that log's own confirmation (`format_celebration` below renders
    them). A habit's very first-ever observation for a given record type
    still gets STORED (seeded silently, per `_maybe_break_record`'s own
    docstring) but never appears in the returned list -- nothing to
    celebrate on a fresh baseline.

    `registry` isn't read here -- `habit` is already resolved by the
    caller -- kept in the signature purely for interface consistency with
    every other registry-generic module in this release (R-X1); SPEC-
    v1.6.md §5's own signature.

    Fail-open (R-R2's explicit "a records error never blocks the
    confirmation" contract): the entire body is one `try`, mirroring
    `core/audit.py:record`'s identical shape -- nothing here can ever
    propagate to the caller."""
    del registry
    try:
        today = _today(config, clock)
        today_str = today.isoformat()
        week_days = week_day_strs(today)

        broken: list[tuple[str, float]] = []

        day_total = period_total(db, habit, user_id, [today_str])
        result = _maybe_break_record(db, user_id, habit.id, "best_day", day_total, today_str)
        if result is not None:
            broken.append(result)

        week_total = period_total(db, habit, user_id, week_days)
        result = _maybe_break_record(db, user_id, habit.id, "best_week", week_total, today_str)
        if result is not None:
            broken.append(result)

        streak = streaks.compute_streak(db, config, habit, today, user_id)
        result = _maybe_break_record(db, user_id, habit.id, "longest_streak", float(streak), today_str)
        if result is not None:
            broken.append(result)

        return broken
    except Exception:
        logger.exception(
            "Updating records failed for %s/%s (fail-open); no record change, no celebration", user_id, habit.id
        )
        return []


# ===========================================================================
# format_celebration -- R-R2's own confirmation-suffix line(s).
# ===========================================================================


def _celebration_line(
    record_type: str, value: float, habit: "Habit", lang: i18n.Language, unit: Literal["day", "week"] = "day"
) -> str:
    label = habit.label(lang)
    if record_type == "longest_streak":
        # SPEC-v1.9.md Rule 5/AC12 (v1.9 integration pass): a cadence
        # habit's stored `longest_streak` is a WEEK count -- `unit`
        # (resolved by the caller via `streaks.streak_unit`) picks the
        # matching wording; a non-cadence habit's `unit` is always "day",
        # so this is byte-identical to v1.8.1 (AC3).
        if unit == "week":
            return i18n.t("record_broken_longest_streak_weeks", lang, label=label, weeks=int(value))
        return i18n.t("record_broken_longest_streak", lang, label=label, days=int(value))
    if habit.type in ("numeric", "duration"):
        unit_str = habit.unit(lang) or ""
        msg_id = "record_broken_best_day" if record_type == "best_day" else "record_broken_best_week"
        return i18n.t(msg_id, lang, label=label, value=value, unit=unit_str)
    msg_id = "record_broken_best_day_count" if record_type == "best_day" else "record_broken_best_week_count"
    return i18n.t(msg_id, lang, label=label, count=int(value))


def format_celebration(
    broken: list[tuple[str, float]],
    habit: "Habit",
    lang: i18n.Language,
    unit: Literal["day", "week"] = "day",
) -> str:
    """R-R2: renders the celebration line(s) `update_on_log` just earned,
    for `main.py` to append to that log's confirmation -- mirrors `core/
    streaks.py`'s own `milestone_reached`-suffix call-site pattern
    (`"\\n\\n" + this_function(...)`) exactly, just generalized to
    possibly more than one broken record at once (a single log CAN break
    `best_day` and `best_week` and `longest_streak` all together, each
    getting its own line). `[]` -> `""` (nothing to append).

    SPEC-v1.9.md Rule 5/AC12 (v1.9 integration pass): `unit` (the caller's
    already-resolved `streaks.streak_unit(db, habit, user_id)`) is passed
    straight through to `_celebration_line` for its `longest_streak`
    branch only -- omitted (the default `"day"`), every pre-v1.9 caller
    keeps byte-identical output (AC3)."""
    return "\n".join(_celebration_line(record_type, value, habit, lang, unit) for record_type, value in broken)


# ===========================================================================
# render -- R-R3: /records [habit].
# ===========================================================================


def _week_range_str(end_str: str) -> str:
    """`achieved_on` for `best_week` is stored as the rolling window's END
    date (the day `update_on_log` computed it on, same convention as
    `best_day`/`longest_streak`) -- the DISPLAYED range in `/records` is
    derived from it, mirroring `core/review.py`'s own "end date minus 6"
    week convention. Dates render as plain ISO strings (`YYYY-MM-DD`),
    matching every other bilingual date already shown in this codebase
    (`core/review.py`'s own `stats_water_line`/`DayValue.day`) rather than
    inventing a new localized "12 Aug" formatter -- SPEC-v1.6.md §3.3's own
    sample text is illustrative, not a literal format requirement."""
    end = date.fromisoformat(end_str)
    start = end - timedelta(days=6)
    return f"{start.isoformat()}–{end.isoformat()}"


def _record_line(habit: "Habit", record_type: str, row, lang: i18n.Language, unit: Literal["day", "week"] = "day") -> str:
    value = float(row["value"])
    achieved_on = row["achieved_on"]

    if record_type == "longest_streak":
        # SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): the stored
        # `longest_streak` for a cadence habit is a week count -- mirrors
        # `records.py:_celebration_line`'s identical switch.
        if unit == "week":
            return i18n.t("records_line_longest_streak_weeks", lang, weeks=int(value), achieved_on=achieved_on)
        return i18n.t("records_line_longest_streak", lang, days=int(value), achieved_on=achieved_on)

    display_date = achieved_on if record_type == "best_day" else _week_range_str(achieved_on)
    if habit.type in ("numeric", "duration"):
        unit = habit.unit(lang) or ""
        msg_id = "records_line_best_day" if record_type == "best_day" else "records_line_best_week"
        return i18n.t(msg_id, lang, value=value, unit=unit, achieved_on=display_date)
    msg_id = "records_line_best_day_count" if record_type == "best_day" else "records_line_best_week_count"
    return i18n.t(msg_id, lang, count=int(value), achieved_on=display_date)


def _habit_block(db: "Database", habit: "Habit", lang: i18n.Language, user_id: str) -> str:
    header = i18n.t("records_habit_header", lang, habit=habit.label(lang))
    rows_by_type = {row["record_type"]: row for row in db.get_records(user_id, habit.id)}
    if not rows_by_type:
        return header + "\n" + i18n.t("records_none_yet", lang)
    # SPEC-v1.9.md Rule 5/AC9 (v1.9 integration pass): resolved once per
    # habit (mirrors `compute_streak`'s own "resolve once, reuse" posture)
    # -- only the `longest_streak` line consults it.
    unit = streaks.streak_unit(db, habit, user_id)
    lines = [header]
    for record_type in RECORD_TYPES:
        row = rows_by_type.get(record_type)
        if row is not None:
            lines.append(_record_line(habit, record_type, row, lang, unit))
    return "\n".join(lines)


def render(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    habit_id: str | None = None,
) -> str:
    """R-R3: `user_id`'s own lifetime records (U-ISO) -- one self-headed
    block (🏆 + habit label) per habit, `/records` (no filter) showing
    every configured habit (registry-generic, R-X1/R-R4), `/records
    <habit>` showing just that one. A habit with no records yet still
    gets its own block, with the friendly `records_none_yet` line instead
    of any bullet rows (R-R3's own explicit "no-records-yet renders
    gracefully" contract). An unresolved `habit_id` (validated HERE, not
    by `core/commands.py`, mirrors `core/history_view.py:render_history`'s
    identical split) short-circuits to `records_invalid_habit`.

    Render-budget-aware (R-B1's shared machinery, reused rather than
    reimplemented): each habit's block is treated as one "row" for `core/
    render_budget.py:fit_within_budget`'s own oldest-first drop, so an
    installation with an unusually large number of configured habits still
    produces a message `channels/telegram.py` won't 400 on.

    Fail-open (SPEC-v1.6.md §3.4: "All read-only surfaces (`/heatmap`,
    `/records`, `/trends`, `/dashboard` show) ... never raise; a DB/
    render/edit failure is logged and degraded"): the whole body below is
    wrapped in one `try`, mirroring `core/audit.py:record`'s identical
    "structurally hard to misuse" shape -- a DB read failure here can
    never propagate up and crash the message-handling loop."""
    del config
    try:
        if habit_id is not None:
            habit = registry.get(habit_id)
            if habit is None:
                return i18n.t(
                    "records_invalid_habit", lang, habit_id=habit_id, habit_list=", ".join(registry.ids())
                )
            return _habit_block(db, habit, lang, user_id)

        blocks = [_habit_block(db, habit, lang, user_id) for habit in registry]
        full = "\n\n".join(blocks)
        if len(full) <= TELEGRAM_MESSAGE_BUDGET:
            return full
        return fit_within_budget(
            "", blocks, render_footer=lambda dropped: i18n.t("records_more_habits", lang, count=dropped)
        )
    except Exception:
        logger.exception("Rendering /records failed for %s (fail-open)", user_id)
        return i18n.t("records_render_failed", lang)
