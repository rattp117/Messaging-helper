"""Weekly-cadence goal declaration & display -- SPEC-v1.9.md §4 R18-R20
(module `cadence`, Theme A.1: "gym 3x/week" rest-day-tolerant habits).

`core/commands.dispatch`'s `"cadence"` kind (`_match_cadence` in
`core/commands.py`) only recognizes the *shape* of a `/cadence ...` /
Thai-alias command and produces a `Command`; this module is where that
`Command` is validated against the live `HabitRegistry`/`Database` and
turned into a bilingual reply -- and where the `habit_cadence` write
happens (`storage/db.py:set_cadence`/`clear_cadence`, M1's own disjoint
region). Every branch returns a plain string and never raises -- same
"structured op in, formatted string out, no traceback to the user"
contract as `core/targets_command.execute_target`/`core/schedules.
execute_remind` (this module's own closest templates: same per-habit
set/clear shape, same recognize-shape-in-commands.py/validate-here split).

`weekly_progress` (R19) is the module's one other public surface: how
many days of THIS ISO week already qualify for a cadence habit, out of
its configured N -- a pure, read-only computation meant to be reused
(not reimplemented) by `/habits`'/the dashboard's own "X of N this week"
line. Per SPEC-v1.9.md §11's file-ownership table, `core/review.py`/
`core/dashboard.py`/`main.py` are the INTEGRATION seam (not this
module's owned files) -- this module ships the pure computation +
`cadence_status_line` formatter; wiring either into `/habits`/`/dashboard`
output is the later integration pass's job.

The engine itself (the cadence-aware weekly walk, `streak_unit`) already
landed in the v1.9 shared surface (`core/streaks.py`, IMPL-v1.9-shared.md)
-- this module never recomputes a streak itself; it only reads/writes the
`habit_cadence` DECLARATION row that engine consults.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from habit_assistant.core import audit, i18n, targets
from habit_assistant.core.streaks import day_qualifies

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


def _iso_week_start(day: date) -> date:
    """Monday of the ISO week `day` falls in (mirrors `core/streaks.py:
    _iso_week_bounds`'s own Monday derivation -- duplicated here rather
    than imported, per this codebase's own established convention for a
    trivial module-local date helper, e.g. `core/records.py`'s/`core/
    trends.py`'s own independently-duplicated `_today` helper)."""
    return day - timedelta(days=day.isoweekday() - 1)


def weekly_progress(db: "Database", config: "Config", habit: "Habit", user_id: str, today: date) -> tuple[int, int]:
    """SPEC-v1.9.md R19: `(qualifying_days_this_iso_week, N)` for a
    cadence habit -- the "/habits"/dashboard "X of N this week" line's own
    pure computation. Reuses `streaks.day_qualifies` (the SAME
    qualification rule the engine's own weekly walk uses, R2/R4) rather
    than a second aggregation -- so this can never disagree with what the
    engine itself would count for the same days.

    `N` is `0` for a habit with no cadence row (defensive; callers are
    expected to gate on `db.get_cadence`/`streaks.streak_unit` themselves
    before calling this -- R20's own "a non-cadence habit shows the
    existing daily line unchanged" is the CALLER's branch, not this
    function's)."""
    per_week = db.get_cadence(user_id, habit.id)
    if per_week is None:
        return 0, 0
    goal = targets.effective_goal(db, habit, config, user_id)
    week_start = _iso_week_start(today)
    done = 0
    day = week_start
    while day <= today:
        if day_qualifies(db, config, habit, day.isoformat(), user_id, goal=goal):
            done += 1
        day += timedelta(days=1)
    return done, per_week


def cadence_status_line(db: "Database", config: "Config", habit: "Habit", user_id: str, today: date, lang: i18n.Language) -> str:
    """R19's own pure formatter -- "🗓 gym — 3×/week · this week 2 of 3 ✅"
    (§3's own sample copy), the checkmark appended only once `done >= n`
    (mirrors the reduction rule the weekly walk itself uses, R4: the
    current partial week only "counts" once it's already MET). Callers
    (the integration pass's `/habits`/dashboard rendering) are expected to
    call this ONLY for a habit that already has a cadence row (mirrors
    `weekly_progress`'s own "caller gates on `get_cadence` first"
    contract) -- calling it for a non-cadence habit renders a degenerate
    "0×/week" line rather than raising, fail-open rather than a crash."""
    done, n = weekly_progress(db, config, habit, user_id, today)
    check = " ✅" if n > 0 and done >= n else ""
    return i18n.t("cadence_status_line", lang, label=habit.label(lang), n=n, done=done, check=check)


# ===========================================================================
# execute_cadence -- R18.
# ===========================================================================


def _execute_clear(db: "Database", habit: "Habit", lang: i18n.Language, user_id: str, source: str) -> str:
    # Unconditional write + audit even when there was nothing to clear --
    # mirrors `core/targets_command.py:_execute_clear`'s own established
    # convention (idempotent `DELETE`, always records, `old_value=None`
    # when there was no prior row) rather than a special "already off"
    # short-circuit SPEC-v1.9.md doesn't actually list among its required
    # errors (§3's own error enumeration has no `cadence_already_off`).
    previous = db.get_cadence(user_id, habit.id)
    try:
        db.clear_cadence(user_id, habit.id)
    except Exception:
        logger.exception("Failed to clear cadence for user %r habit %r", user_id, habit.id)
        return i18n.t("cadence_save_failed", lang)
    audit.record(
        db,
        actor=user_id,
        action="cadence_clear",
        source=source,
        entity=habit.id,
        old_value=previous,
        new_value=None,
    )
    return i18n.t("cadence_cleared", lang, label=habit.label(lang))


def _execute_set(
    db: "Database",
    config: "Config",
    habit: "Habit",
    value_num: float | None,
    lang: i18n.Language,
    user_id: str,
    source: str,
) -> str:
    max_per_week = config.cadence.max_per_week
    if value_num is None or float(value_num) != int(value_num) or not (1 <= int(value_num) <= max_per_week):
        return i18n.t("cadence_invalid_value", lang, habit_id=habit.id, max=max_per_week)
    per_week = int(value_num)

    previous = db.get_cadence(user_id, habit.id)
    try:
        db.set_cadence(user_id, habit.id, per_week)
    except Exception:
        logger.exception("Failed to set cadence for user %r habit %r", user_id, habit.id)
        return i18n.t("cadence_save_failed", lang)
    audit.record(
        db,
        actor=user_id,
        action="cadence_set",
        source=source,
        entity=habit.id,
        old_value=previous,
        new_value=per_week,
    )

    done, _ = weekly_progress(db, config, habit, user_id, date.today())
    return i18n.t("cadence_set", lang, label=habit.label(lang), n=per_week, done=done)


async def execute_cadence(
    command: "Command",
    *,
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    source: str = "command",
) -> str:
    """Validate and perform the `/cadence` op described by `command`
    (`core/commands.dispatch`'s `kind="cadence"` output), for `user_id`,
    and return the bilingual reply. Never raises: every failure mode --
    no habit token at all, an unresolvable habit, a malformed/out-of-range
    N, a DB write error -- resolves to a friendly catalog message.

    SPEC-v1.9.md's own §2 input spec: habits resolve through the acting
    user's `RegistryProvider.for_user` registry (base catalog + that
    user's own `user_habits`, R20's "registry-generic ... a custom habit
    may carry cadence") -- `registry` here is expected to already BE that
    per-user registry (the caller's job, mirroring `execute_target`'s/
    `execute_remind`'s own identical convention), not the static base
    catalog. Every DB read/write below is scoped to `user_id` (R20's own
    "per-user scoped")."""
    if command.category is None:
        return i18n.t("cadence_usage", lang)

    habit = registry.get(command.category)
    if habit is None:
        return i18n.t("cadence_invalid_habit", lang, habit_id=command.category, habit_list=", ".join(registry.ids()))

    if command.pref_value == "off":
        return _execute_clear(db, habit, lang, user_id, source)
    if command.value_num is not None:
        return _execute_set(db, config, habit, command.value_num, lang, user_id, source)
    return i18n.t("cadence_usage", lang)
