"""SPEC-v1.6.md §4 Feature 5, "Almost there" end-of-day nudge (module
`nudge`, R-N1-R-N3): one gentle push per day for a user whose goal-bearing
habit(s) are close to done, near end of day.

No command of its own (OQ2 RESOLVED: rides `/checkin` enablement -- one
opt-in, `/checkin on`, gives the whole gentle-proactive suite: hourly
check-ins AND the end-of-day nudge; no separate toggle/column). Mirrors
`core/checkins.py`'s own module shape closely -- same `effective_checkin`
gate, same `reminders.in_dnd_now` DND check, same per-user language
resolution -- but is its own tick (`run_due_nudges`), fired from the SAME
minutely job as `run_due_reminders`/`run_due_checkins` (integration step,
main.py) alongside them, not merged into either.

Firing discipline (R-N1): `run_due_nudges` returns immediately unless the
current minute is exactly `[nudge] time` (default "20:00") -- a single
fixed minute, so it fires at most once/day BY CONSTRUCTION (no separate
"already sent today" state needed, mirroring `run_due_checkins`'s own
":00"-only guard for the same reason). Independent of the check-in
WINDOW -- OQ2 only gates on whether check-ins are enabled at all, not
whether the current hour happens to fall inside the user's own check-in
window (a user with a narrow "12:00-14:00" check-in window still gets
their 20:00 nudge, as long as check-ins are ON).

"Close" (R-N1/R-N3): a goal-bearing habit (`targets.effective_goal` is
non-`None` for `user_id` -- a per-user `/target` override, if set, is
respected the same way every other goal-consuming module already reads
through it) whose today's total is `>= threshold_pct% of goal` but
`< goal`. A habit already met, or nowhere near goal, contributes nothing.
Zero-LLM throughout -- purely `db.sum_value` + `targets.effective_goal`
comparisons, no Ollama call anywhere in this path.

Non-nagging (R-N2): every close habit for a user is folded into ONE
message (`build_nudge_message`) -- never one send per habit -- and if
nothing is close, nothing is sent at all (silence, not a "keep going"
nag). Per-user isolation (R-X2/AC-X3): every DB read below is scoped to
the acting `user_id`; `run_due_nudges`' own fan-out (`db.active_user_ids()`)
is the only place this module iterates across users.

Fail-open (SPEC-v1.6.md §3.4: "the nudge never raises"): a failure
building one user's message is logged and that user is skipped -- it can
never block the fan-out for every OTHER user in the same tick, mirroring
`core/reminders.py:_user_language_pref`'s and `core/checkins.py:
effective_checkin`'s identical fail-open posture for the same reason.

Wired into `main.py` at the integration step (documented in
IMPL-v1.6-nudge.md, not this module): `run_due_nudges` runs on its own
`nudge_tick` job, the same minutely `CronTrigger(second=0, ...)` cadence
as `reminder_tick`/`checkin_tick`, registered alongside them.

SPEC-REFACTOR.md Stage 1 rule 7: `build_nudge_message` used to call
`pause.is_paused` once per habit, and each call re-read the user's whole
`db.active_pauses(user_id)` set from scratch. `_is_paused_in` below is a
same-file mirror of `pause.is_paused`'s own coverage check, operating on a
pre-fetched row list instead -- `pause.py` is out of this Stage-1 track's
file ownership (see `core/checkins.py`'s identical mirror + docstring note
for the full rationale), so the fetch-once-reuse-across-habits remedy
lives here too. Byte-identical: same coverage rule, one `active_pauses`
read per build instead of one per habit.

SPEC-REFACTOR.md Stage 1 rules 2/AC4: `main.py`'s three minutely ticks
(`reminder_tick`/`checkin_tick`/`nudge_tick`) are consolidated into ONE
scheduler job that fetches `db.active_user_ids()` once and threads it into
all three tick functions via each one's new optional `active_user_ids`
param -- this docstring's own "registered alongside them" above still
describes the functional relationship; only the scheduler wiring (three
jobs -> one) changed."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from habit_assistant.core import i18n, targets, timeutil, user_prefs
from habit_assistant.core.checkins import effective_checkin
from habit_assistant.core.reminders import in_dnd_now

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


# ===========================================================================
# build_nudge_message -- R-N1/R-N3: the deterministic bilingual body,
# folding every "close" habit into one message.
# ===========================================================================


def _is_paused_in(pause_rows: list, habit_id: str, when_str: str) -> bool:
    """Mirrors `core/pause.py:is_paused`'s exact coverage rule (a
    habit-scoped row, OR an all-habits row where `row["habit_id"] is
    None`, whose `[start_date, end_date]` covers `when_str`) -- operating
    on a pre-fetched `db.active_pauses(user_id)` row list instead of
    re-querying it per habit. Byte-identical copy of `core/checkins.py:
    _is_paused_in` (SPEC-REFACTOR.md Stage 1 rule 7; not shared/imported
    across the two files -- see this module's own docstring)."""
    for row in pause_rows:
        if row["habit_id"] is not None and row["habit_id"] != habit_id:
            continue
        if row["start_date"] <= when_str <= row["end_date"]:
            return True
    return False


def build_nudge_message(
    db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language, user_id: str, clock=datetime.now
) -> str | None:
    """R-N1/R-N3: one line per "close" habit (today's total `>=
    threshold_pct% of goal` but `< goal`), joined under a single header
    into ONE message (R-N2: never one send per habit). `None` when no
    habit qualifies -- a met or far-from-goal habit contributes nothing,
    and an empty result means the caller sends nothing at all (silence).

    Registry-generic (R-X1): iterates every configured habit, gated purely
    by `targets.effective_goal` returning non-`None` -- a goal-less habit
    (text/boolean, or a numeric/duration habit with no configured goal and
    no `/target` override) naturally never contributes, no special-casing
    needed. `goal <= 0` is a defensive skip (can't happen through normal
    config/`/target` validation, but a percentage-of-goal comparison is
    meaningless against a non-positive goal, and this surface must never
    raise -- SPEC-v1.6.md §3.4).

    Threshold math is cross-multiplied (`total * 100 >= threshold_pct *
    goal`) rather than divided, avoiding a `total / goal` float-precision
    wobble right at a boundary like exactly 80%."""
    today_str = timeutil.today_in_timezone(clock, config.app.timezone).isoformat()
    threshold_pct = config.nudge.threshold_pct
    # SPEC-REFACTOR.md Stage 1 rule 7: read once per build, reused across
    # every habit below via `_is_paused_in`, instead of `pause.is_paused`
    # re-reading the whole set once per habit.
    pause_rows = db.active_pauses(user_id)

    close: list[tuple[object, float, float]] = []
    for habit in registry:
        # SPEC-v1.9.md R15 (v1.9 integration pass): mirrors `core/
        # checkins.py:build_checkin_message`'s identical per-habit pause
        # skip -- a paused habit never contributes its own line to this
        # proactive, folded message; other habits still get theirs.
        if _is_paused_in(pause_rows, habit.id, today_str):
            continue
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal is None or goal <= 0:
            continue
        total = db.sum_value(user_id, habit.id, today_str)
        if total >= goal:
            continue  # already met (or over) -- not "close", nothing to nudge
        if total * 100.0 >= threshold_pct * goal:
            close.append((habit, total, goal))

    if not close:
        return None

    lines = [i18n.t("nudge_header", lang)]
    for habit, total, goal in close:
        label = habit.label(lang)
        unit = habit.unit(lang) or ""
        remaining = goal - total
        lines.append(i18n.t("nudge_line", lang, label=label, remaining=remaining, unit=unit))
    return "\n".join(lines)


# ===========================================================================
# run_due_nudges -- R-N1: fires exactly once/day, at `[nudge] time`, on the
# same minutely job as run_due_reminders/run_due_checkins.
# ===========================================================================


async def run_due_nudges(
    channel: "Channel",
    config: "Config",
    registry: "HabitRegistry",
    db: "Database",
    clock=datetime.now,
    registry_for: "Callable[[str], HabitRegistry] | None" = None,
    active_user_ids: "list[str] | None" = None,
) -> None:
    """R-N1: called from the SAME minutely job that runs `run_due_reminders`/
    `run_due_checkins` (integration step, main.py). Returns immediately
    unless the current minute is exactly `config.nudge.time` -- once/day by
    construction (a single fixed minute, R-N1's own explicit wording).

    On that minute, for each `db.active_user_ids()`: skip unless check-ins
    are enabled (`effective_checkin`'s boolean half only -- OQ2 gates on
    enablement, NOT on whether "now" happens to fall inside the user's own
    check-in WINDOW, which is a separate, unrelated setting); skip if
    inside DND (`reminders.in_dnd_now`); skip if `build_nudge_message`
    returns `None` (nothing close today). Strictly LLM-free throughout.

    Fail-open (SPEC-v1.6.md §3.4): any exception building one user's
    message -- OR sending it -- is logged and that user alone is skipped,
    each in its OWN try/except (mirrors `core/announce.py:announce_release`'s
    identical two-stage shape: one try around building/resolving what to
    send, a separate try around the `channel.send` call itself) -- never
    lets a single bad row, or a single transport failure, abort the
    fan-out for every other active user, and never lets either propagate
    out of `run_due_nudges` itself.

    SPEC-v1.7.md R-G3: `registry_for`, when given, resolves the PER-USER
    registry inside the fan-out loop below (inside the SAME per-user
    try/except, so a bad per-user registry build is fail-open exactly like
    every other per-user failure in this loop) -- same additive, optional,
    backward-compatible convention as `run_due_reminders`'s own
    `registry_for`: omitted, every existing caller/test keeps using
    `registry` for every user, byte-identical to pre-v1.7 (AC-5).

    SPEC-v1.8.md R-D1 (module `riders`): the send below passes
    `disable_notification=config.notifications.silent_proactive` (default
    `True`, AC-D1); `false` restores the pre-v1.8 notifying payload
    (AC-D4).

    SPEC-REFACTOR.md Stage 1 rules 2/AC4: `active_user_ids`, when given
    (typically `main.py`'s consolidated minutely tick, which fetches the
    fan-out set once and threads it into all three tick functions), is
    used in place of a fresh `db.active_user_ids()` call; omitted (`None`,
    the default), this function calls it itself, byte-identical to
    pre-Stage-1. This call only ever happens AFTER the fixed-minute guard
    below returns early -- so on a non-`[nudge] time` minute,
    `active_user_ids` is never even consulted, matching this function's
    pre-Stage-1 0-query idle-minute behavior."""
    hhmm = timeutil.now_hhmm(clock, config.app.timezone)
    if hhmm != config.nudge.time:
        return

    for user_id in active_user_ids if active_user_ids is not None else db.active_user_ids():
        try:
            enabled, _window = effective_checkin(db, config, user_id)
            if not enabled:
                continue
            if in_dnd_now(db, config, user_id, clock):
                logger.info("Suppressing end-of-day nudge for %s: inside their own DND window", user_id)
                continue

            lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
            user_registry = registry_for(user_id) if registry_for is not None else registry
            message = build_nudge_message(db, config, user_registry, lang, user_id, clock)
        except Exception:
            logger.exception("Building the end-of-day nudge failed for %s; skipping (fail-open)", user_id)
            continue

        if message is None:
            continue

        try:
            await channel.send(user_id, message, disable_notification=config.notifications.silent_proactive)
        except Exception:
            logger.exception("Sending the end-of-day nudge failed for %s; skipping (fail-open)", user_id)
            continue
