"""SPEC-v1.9.md §4 Rules 8-11 (module `grace`, M2): the automatic,
fully-deterministic streak-protecting "grace day" -- at most one per
`(user_id, habit_id)` per ISO week, decided and WRITTEN at exactly one
place, the nightly `evaluate_grace` job, never inside `core/streaks.py`'s
own read-only `compute_streak` (that engine only ever *reads*
`grace_ledger` via the shared `db.grace_protected_dates` accessor -- see
`streaks.py`'s own module docstring, "Grace *consumption* is a write and
is confined to the nightly `evaluate_grace` job").

No command of its own (R8: "auto-only; there is no manual spend") and no
channel import here (mirrors `core/audit.py`'s/`core/records.py`'s own "no
channel imports" seam) -- `evaluate_grace` below WRITES the ledger + audit
row but does not itself send anything; `main.py`'s integration step (SPEC-
v1.9.md §6, out of this module's scope) is what calls this once per active
user from the 00:05 tick (reusing the `active_user_ids()` fan-out, same
convention `core/nudge.py:run_due_nudges`/`core/checkins.py:
run_due_checkins` already establish for their own minutely ticks) and THEN
sends `format_grace_message`'s result via `channel.send`.

**Grace never fires during a pause** (an adversarial case worth stating
explicitly, even though it needs no special-case code below): a paused
date is NEUTRAL, not MISSED (SPEC-v1.9.md Rule 2 -- `classify_day`'s own
three-way split), so `evaluate_grace`'s own "yesterday is MISSED" check
(mirroring that exact same NEUTRAL-excludes-MISSED rule via
`day_qualifies` + `db.paused_dates`, see `_yesterday_state` below) already
never treats a paused yesterday as a miss to bridge -- pause's own
`is_paused` module (`core/pause.py`, M3, disjoint from this file) never
needs to be imported here at all.

**A voluntary log later that day beats the neutral default** (Rule 16's
general "a real entry beats the neutral default" principle, stated under
pause's section but explicitly framed as `classify_day`'s own general
rule, R2): once a date is grace-protected, `classify_day` still checks
`day_qualifies` FIRST -- if the user backfills a genuine log for that
already-bridged date later, the day is QUALIFIED, not NEUTRAL, on every
subsequent read. This needs no code here either -- it is purely a
property of the shared engine this module writes into (`tests/
test_grace.py`'s own `test_a_backfilled_log_on_an_already_bridged_date_
counts_as_qualified_not_neutral` proves it end-to-end); the `grace_ledger`
row itself is never deleted by this (R8's "one grace already spent this
week" stays spent, even if it turned out unneeded in hindsight -- the
week's budget was still consumed at the moment of the nightly decision,
which is the only information `evaluate_grace` had at the time)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from habit_assistant.core import audit, i18n, streaks
from habit_assistant.core.habits import Habit

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


def _iso_week_bounds(day: date) -> tuple[date, date]:
    """Monday..Sunday (inclusive) of the ISO week `day` falls in --
    duplicated from `core/streaks.py`'s own private helper rather than
    imported, mirroring `core/nudge.py:_now_hhmm`'s own documented
    precedent ("duplicated here rather than imported since that helper is
    a private, module-local convention every ... call site in this
    codebase re-derives on its own")."""
    monday = day - timedelta(days=day.isoweekday() - 1)
    return monday, monday + timedelta(days=6)


def _period_key(day: date) -> str:
    """R8's `period_key = "<iso_year>-W<iso_week>"`. `date.isocalendar()`
    (not `day.year`/a manual week count) is what correctly attributes a
    late-December Monday-start-of-week or an early-January Sunday-end-of-
    week to the ISO year it actually belongs to (e.g. Dec 31 2029 can be
    ISO week 1 of 2030) -- the same year-boundary correctness
    `core/streaks.py:_iso_week_bounds`'s own week-Monday derivation
    relies on, just expressed as the grouping key `grace_used_in_week`
    reads back."""
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _yesterday_is_a_genuine_miss(
    db: "Database", config: "Config", habit: Habit, yesterday: str, user_id: str
) -> bool:
    """R9's "whose yesterday is MISSED" -- re-derives `classify_day`'s own
    QUALIFIED/NEUTRAL/MISSED split (Rule 2) for exactly one date rather
    than calling `streaks.classify_day` directly, because that function
    also wants a `grace_dates` set (irrelevant here -- a date can't
    already be grace-protected AND still be "yesterday's fresh miss" in
    the same run; the caller, `evaluate_grace`, checks
    `grace_protected_dates` separately, see below) and a pre-resolved
    `goal` (this is a one-off single-date check, not a backward walk, so
    resolving fresh via `day_qualifies`'s own default is the right cost
    here, same as e.g. `core/nudge.py:build_nudge_message`'s per-habit
    per-day checks). QUALIFIED -> not a miss. Paused -> NEUTRAL, not a
    miss (this IS the "grace never fires during a pause" guarantee, see
    this module's own docstring above) -- reads `db.paused_dates` (the
    SHARED accessor `streaks.py` itself reads) directly rather than
    importing `core/pause.py:is_paused`, keeping this module's own file
    ownership disjoint from M3's (SPEC-v1.9.md §11 module-split table)."""
    if streaks.day_qualifies(db, config, habit, yesterday, user_id):
        return False
    paused = db.paused_dates(user_id, habit.id, yesterday, yesterday)
    return yesterday not in paused


def evaluate_grace(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    user_id: str,
    today: date,
    clock=datetime.now,
) -> list[tuple[Habit, int]]:
    """SPEC-v1.9.md R9: for `user_id`, for each DAILY (non-cadence, R6/
    AC16) habit in `registry` whose YESTERDAY (relative to `today`, the
    caller's already-resolved local date -- `config.app.timezone`
    correctness is the caller's responsibility, exactly mirroring
    `core/streaks.py:run_daily_summary`'s own `today` contract) is a
    genuine miss (not qualified, not paused, not already grace-protected),
    AND has an active streak >= 1 ending the day BEFORE yesterday, AND has
    not already used its grace this ISO week: write the `grace_ledger`
    row for yesterday, a fail-open `audit.record` row (`grace_consumed`),
    and include `(habit, protected_streak_len)` in the returned list --
    `protected_streak_len` is exactly the streak length that would have
    broken (the number the kind message reports, "your N-day streak is
    safe"), computed via the SAME `streaks.compute_streak` every other
    call site uses (AC10.5's "one function, one number, everywhere").

    R8: `[grace] enabled = false` disables the ENTIRE mechanism -- returns
    `[]` immediately, no DB read/write of any kind past that check
    (byte-identical to a graceless world, AC17).

    "No streak to protect" (a miss on a habit's very first day, or any
    habit with zero log history): `compute_streak(..., day_before_
    yesterday, ...)` is naturally `0` for a habit with no qualifying
    history before the miss -- the `< 1` guard below skips it without any
    habit-age special-casing (mirrors `core/nudge.py:build_nudge_message`'s
    own "a goal-less habit naturally never contributes, no special-casing
    needed" posture for a structurally analogous "nothing to do" case).

    Idempotent against a same-night re-run (e.g. a process restart between
    00:05 and the next day): `grace_protected_dates` is checked BEFORE
    writing, so a date already bridged in an earlier run this same job
    contributes nothing to the returned list and writes nothing a second
    time (AC18's "exactly one audit `grace_consumed` row" holds even
    across a restart) -- `db.record_grace`'s own `INSERT OR IGNORE` is a
    second, storage-layer backstop for the same guarantee.

    Fail-open per habit (mirrors `run_due_nudges`'/`run_due_checkins`'s
    own "one bad habit/user never blocks the rest of the fan-out"
    posture): an exception evaluating or bridging ONE habit is logged and
    that habit alone is skipped, never aborting the loop for `user_id`'s
    other habits and never propagating to the caller's own per-user
    fan-out (SPEC-v1.9.md §3.4-style discipline every proactive job in
    this codebase already follows)."""
    if not config.grace.enabled:
        return []

    yesterday = today - timedelta(days=1)
    day_before_yesterday = today - timedelta(days=2)
    yesterday_str = yesterday.isoformat()
    period_key = _period_key(yesterday)

    bridged: list[tuple[Habit, int]] = []
    for habit in registry:
        try:
            if db.get_cadence(user_id, habit.id) is not None:
                continue  # R6/AC16: grace never applies to a cadence habit

            if not _yesterday_is_a_genuine_miss(db, config, habit, yesterday_str, user_id):
                continue

            if yesterday_str in db.grace_protected_dates(user_id, habit.id, yesterday_str, yesterday_str):
                continue  # already bridged this run/night (idempotency)

            if db.grace_used_in_week(user_id, habit.id, period_key):
                continue  # R11: one grace per ISO week -- already spent

            protected_streak = streaks.compute_streak(db, config, habit, day_before_yesterday, user_id)
            if protected_streak < 1:
                continue  # "no streak to protect" -- nothing would have broken

            db.record_grace(user_id, habit.id, yesterday_str, period_key)
            audit.record(
                db,
                actor=user_id,
                action="grace_consumed",
                # Integration ruling (Archi, v1.9 integration pass): grace
                # is the first purely SYSTEM-initiated mutation in this
                # codebase's audit history (nobody typed a command, tapped
                # a button, or triggered an NL match -- the nightly 00:05
                # tick decided this on its own) -- "admin" (Luna's own
                # placeholder, see IMPL-v1.9-grace.md's "Known limitations")
                # is now replaced with the dedicated "system" source
                # `core/audit.py:SOURCES` gained for exactly this case.
                source="system",
                entity=habit.id,
                old_value=None,
                new_value=yesterday_str,
                clock=clock,
            )
            bridged.append((habit, protected_streak))
        except Exception:
            logger.exception(
                "Evaluating grace failed for user_id=%r habit=%r; skipping (fail-open)", user_id, habit.id
            )
            continue

    return bridged


def grace_status_line(db: "Database", config: "Config", habit: Habit, user_id: str, today: date, lang: i18n.Language) -> str:
    """R17: the `/habits` per-daily-habit grace-balance line -- "available
    this week" or "used {weekday} (streak protected)". `today`'s own ISO
    week (not `yesterday`'s -- this is "am I still able to use my grace
    THIS week", the forward-looking question a user asks right now, unlike
    `evaluate_grace`'s own backward-looking "was YESTERDAY's miss bridged"
    decision) is what's checked here.

    R17's "with `[grace] enabled=false` ... byte-identical to a graceless
    world" is read as applying to this display line too -- an install with
    grace turned off shows no grace line at all (`""`), exactly as if the
    feature didn't exist, rather than an always-true "available" line that
    can never actually be spent; `discoverability.py`'s integration-owned
    `/habits` renderer (SPEC-v1.9.md §6) is expected to skip appending a
    falsy line, mirroring `core/nudge.py:build_nudge_message`'s own
    "`None`/falsy means nothing to show" contract.

    A cadence habit (R6) is never expected to be passed here by the
    renderer (a cadence habit has no grace concept at all) -- this
    function itself does not special-case `db.get_cadence`, since it's a
    pure "what's this week's ledger state" read that's harmless either way
    (a cadence habit simply never has a `grace_ledger` row, so it would
    always read "available"); the DECISION of whether to call this at all
    for a given habit belongs to the renderer, mirroring `streak_unit`'s
    own "every renderer consults it, this module doesn't gate itself"
    split (SPEC-v1.9.md Rule 5's own docstring in `core/streaks.py`)."""
    if not config.grace.enabled:
        return ""

    week_start, week_end = _iso_week_bounds(today)
    protected = db.grace_protected_dates(user_id, habit.id, week_start.isoformat(), week_end.isoformat())
    if not protected:
        return i18n.t("grace_status_available", lang)

    # R8: at most one grace per ISO week, so `protected` holds exactly one
    # date here by construction -- `min` is a defensive, not a
    # load-bearing, tie-break if that invariant were ever violated.
    used_on = date.fromisoformat(min(protected))
    return i18n.t("grace_status_used", lang, weekday=used_on.strftime("%a"))


def format_grace_message(broken: list[tuple[Habit, int]], lang: i18n.Language) -> str:
    """R9/R10: the one-time kind message, bilingual, folding every habit
    `evaluate_grace` bridged for one user in the SAME nightly run into ONE
    send (mirrors `core/nudge.py:build_nudge_message`'s own "never one
    send per habit" discipline, R-N2's identical rationale applied here) --
    joined with a blank line between habits in the rare case more than one
    daily habit was bridged the same night. `""` for an empty `broken`
    list -- the caller (`main.py`'s integration step) is expected to treat
    a falsy result as "nothing to send", mirroring `build_nudge_message`'s
    own `None`-means-skip contract (this function returns `str`, not
    `str | None`, per SPEC-v1.9.md §5's own interface signature -- `""` is
    this module's equivalent falsy sentinel)."""
    if not broken:
        return ""
    lines = [
        i18n.t("grace_message_line", lang, label=habit.label(lang), streak=streak) for habit, streak in broken
    ]
    return "\n\n".join(lines)
