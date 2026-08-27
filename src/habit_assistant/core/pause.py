"""Pause / vacation mode (SPEC-v1.9.md §4 R12-R17, module `pause`):
`/pause [<habit>] <Nd|until DATE|until WEEKDAY>` and `/resume [<habit>]`
execution -- `core/commands.dispatch` (`_match_pause`/`_match_resume`
region) only recognizes the *shape* of these commands and produces a
`Command` carrying the raw, unvalidated habit token (`Command.category`,
`None` = all habits) and duration tail (`Command.pref_value`, `None` =
"show status"/"no habit given"); this module is where that `Command` is
validated against the live `HabitRegistry`, turned into a `pauses` row
write (or delete), and rendered as a bilingual reply -- same
recognize-shape-there/validate-and-execute-here split as every other
settings-style command in this codebase (`core/targets_command.py`,
`core/schedules.py`).

This module owns exactly one read helper the REST of the app depends on:
`is_paused(db, config, user_id, habit_id, when)` -- SPEC-v1.9.md §5's own
listed interface, consulted by (a) `core/streaks.py`'s already-landed
engine rework (via the shared `db.paused_dates`/`db.active_pauses` reads,
not this function directly -- the engine reads dates in bulk for a whole
walk) and (b) integration's own later wiring into `reminders.send_reminder`
/ `checkins.run_due_checkins` / `nudge.run_due_nudges` / the weekly-review
and daily-summary inline jobs (SPEC-v1.9.md §6/§11's own "expose the
helper surface, integration does the gating" split -- this module does
NOT itself suppress any proactive send; it only answers "is this
user+habit paused right now").

Every `execute_*` function below returns a plain string (the reply) and
never raises -- same "structured op in, formatted string out, no
traceback to the user" contract as `core/targets_command.execute_target`/
`core/schedules.execute_remind`. A DB write failure is caught, logged,
and reported via `pause_save_failed`, never a stack trace.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from habit_assistant.core import audit, i18n

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# is_paused -- SPEC-v1.9.md §5's own listed interface. Reads `db.
# active_pauses` (the shared-surface raw accessor, IMPL-v1.9-shared.md)
# rather than `db.paused_dates` (that one is the ENGINE's own bulk/
# whole-window reader, `core/streaks.py`'s private concern) -- a single
# point-in-time "is this covered right now" check has no need to expand a
# date range into a `set[str]` first.
# ---------------------------------------------------------------------------


def paused_until(db: "Database", config: "Config", user_id: str, habit_id: str, when: date) -> str | None:
    """Integration helper (v1.9 integration pass, AC22): the covering row's
    own `end_date` for `habit_id` at `when`, or `None` if not paused -- the
    exact same coverage rule `is_paused` applies (habit-scoped OR an
    all-habits row), just returning the date a `/dashboard`/`/habits`
    renderer needs to show the "paused until <date>" marker, instead of a
    bare bool. If more than one covering row exists (an adversarial
    edge -- a habit-scoped row AND a differently-scoped all-habits row both
    covering the same habit, R12's own documented "the two coexist" case),
    the LATER end date is shown -- the more informative "paused until" for
    a user reading the board."""
    del config
    when_str = when.isoformat()
    latest: str | None = None
    for row in db.active_pauses(user_id):
        if row["habit_id"] is not None and row["habit_id"] != habit_id:
            continue
        if row["start_date"] <= when_str <= row["end_date"]:
            if latest is None or row["end_date"] > latest:
                latest = row["end_date"]
    return latest


def is_paused(db: "Database", config: "Config", user_id: str, habit_id: str, when: date) -> bool:
    """True iff an active `pauses` row for `user_id` covers `when` for
    `habit_id` -- either a habit-scoped row (`row.habit_id == habit_id`)
    or an all-habits row (`row.habit_id IS NULL`, R12/R14). `config` is
    accepted only to match SPEC-v1.9.md §5's own exact listed signature
    (mirrors `core/commands.py:_match_routine`'s identical `del registry`
    convention for an unused, interface-mandated parameter) -- pause
    coverage needs no config value (the cap is only relevant when a pause
    is being SET, `execute_pause`'s own concern, not read back here)."""
    del config
    when_str = when.isoformat()
    for row in db.active_pauses(user_id):
        if row["habit_id"] is not None and row["habit_id"] != habit_id:
            continue
        if row["start_date"] <= when_str <= row["end_date"]:
            return True
    return False


# ---------------------------------------------------------------------------
# is_paused_safe / active_pauses_safe -- SPEC-v1.10.md §4 R-SS9 (shared
# surface, module `riders`' own R18 dependency): fail-open wrappers around
# `is_paused`/`db.active_pauses`, so the "a pauses-read failure means NOT
# paused" decision lives in exactly one place instead of being reimplemented
# per call site. `reminders.send_reminder` already had its own inline
# fail-open try/except for this (SPEC-v1.9.md's own integration pass) --
# R18 adopts THIS helper there too (byte-identical outcome, same log
# message shape) so all 5 proactive sites (reminders/check-ins/nudge/daily
# summary/weekly review) share one implementation. `active_pauses_safe` is
# the sibling for the 2 sites (`checkins.build_checkin_message`, `nudge.
# build_nudge_message`) that read the raw `db.active_pauses(user_id)` list
# directly rather than a single point-in-time `is_paused` check.
# ---------------------------------------------------------------------------


def is_paused_safe(db: "Database", config: "Config", user_id: str, habit_id: str, when: date) -> bool:
    """`is_paused`, except any exception reading the `pauses` table is
    logged and treated as "not paused" -- a DB hiccup for one user must
    never suppress that user's send incorrectly, and (since this returns a
    plain bool rather than raising) must never abort a fan-out loop that's
    also serving other users (R18's own "never aborts the run for users B,
    C..." requirement -- enforced by each CALL SITE's own per-user loop
    structure continuing past this call, not by anything in this function
    itself)."""
    try:
        return is_paused(db, config, user_id, habit_id, when)
    except Exception:
        logger.exception(
            "Pause read failed for %s/%s; treating as not-paused (fail-open)", user_id, habit_id
        )
        return False


def active_pauses_safe(db: "Database", user_id: str) -> list:
    """`db.active_pauses(user_id)`, except any exception is logged and
    treated as "no active pauses" (an empty list) -- same fail-open
    posture as `is_paused_safe` above, for the two call sites that need
    the raw row list rather than a single habit's coverage."""
    try:
        return db.active_pauses(user_id)
    except Exception:
        logger.exception("Active-pauses read failed for %s; treating as none (fail-open)", user_id)
        return []


# ---------------------------------------------------------------------------
# Duration parsing -- R12's `<N>d | until DATE | until WEEKDAY` grammar.
# `commands.py` only recognizes the SHAPE (does the tail start with a
# digit or the literal "until"?), never validates it -- everything below
# is this module's own semantic-validation layer, mirroring `core/
# schedules.py:_validate_and_dedupe_times`'s identical split.
# ---------------------------------------------------------------------------

_DAYS_RE = re.compile(r"^(?P<n>\d+)d$", re.IGNORECASE)
_UNTIL_RE = re.compile(r"^until\s+(?P<token>\S+)$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_EN_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
# Mirrors core/backfill.py's own `_TH_WEEKDAYS` table (that module's copy
# stays private to its own past-date resolution; this module needs the
# same names for a FUTURE resolution, so it keeps its own copy rather than
# importing a private name across modules).
_TH_WEEKDAYS: dict[str, int] = {
    "จันทร์": 0,
    "อังคาร": 1,
    "พุธ": 2,
    "พฤหัสบดี": 3,
    "พฤหัส": 3,
    "ศุกร์": 4,
    "เสาร์": 5,
    "อาทิตย์": 6,
}


def _resolve_weekday_token(token: str, today: date) -> date | None:
    """"until monday"/"until จันทร์"/"until วันจันทร์" -> the NEXT
    occurrence of that weekday STRICTLY AFTER today (R12's duration is
    always a span into the future; "until <today's own weekday>" means
    next week's occurrence, never a zero-length "until today" -- the
    mirror-image judgment call of `core/backfill.py:_resolve_past_weekday`,
    which resolves the nearest PAST occurrence for the opposite,
    already-happened direction). An optional "วัน" prefix is stripped
    first (`core/backfill.py`'s own "วัน<weekday>" convention) so both the
    bare Thai weekday name and its "วัน"-prefixed form resolve."""
    lowered = token.lower()
    target = _EN_WEEKDAYS.get(lowered)
    if target is None:
        target = _TH_WEEKDAYS.get(token.removeprefix("วัน"))
    if target is None:
        return None
    delta = (target - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def _parse_until_token(token: str, today: date) -> date | None:
    if _ISO_DATE_RE.match(token):
        try:
            return date.fromisoformat(token)
        except ValueError:
            return None
    return _resolve_weekday_token(token, today)


@dataclass(frozen=True, slots=True)
class _DurationError:
    kind: Literal["usage", "invalid_date"]


def _resolve_duration(duration_raw: str, today: date) -> date | _DurationError:
    """`duration_raw` -> the pause's inclusive end date, or a
    `_DurationError` naming which friendly reply to send. R12: a token
    that doesn't even have the `<N>d`/`until ...` SHAPE at all -> "usage"
    (nothing named in SPEC-v1.9.md §3's error vocabulary covers this case
    specifically, so it gets the same generic usage nudge `target_usage`/
    `routine_create_usage` give for an unparseable slash-command tail
    elsewhere in this codebase); a well-SHAPED `until ...` whose token
    fails to parse as either an ISO date or a known weekday name, OR
    parses to a date strictly before today -> "invalid_date" (R12's own
    named error, `pause_invalid_date`, covers both sub-cases -- the spec
    text itself groups "past OR unparseable" under the one error id)."""
    days_match = _DAYS_RE.match(duration_raw)
    if days_match is not None:
        n = int(days_match.group("n"))
        if n <= 0:
            return _DurationError("usage")
        return today + timedelta(days=n - 1)

    until_match = _UNTIL_RE.match(duration_raw)
    if until_match is not None:
        parsed = _parse_until_token(until_match.group("token"), today)
        if parsed is None or parsed < today:
            return _DurationError("invalid_date")
        return parsed

    return _DurationError("usage")


# ---------------------------------------------------------------------------
# /pause status (bare, or a habit token with no duration) -- not an
# acceptance-criterion-mandated view (R12's own grammar always pairs a
# habit with a duration), but a deliberate, low-risk UX addition: rather
# than treat "no duration given" as an error, show what's currently
# active. Mirrors `core/schedules.py:_execute_show`'s identical "empty
# tail -> read-only status, not an error" posture.
# ---------------------------------------------------------------------------


def _status_target_label(row, registry: "HabitRegistry", lang: i18n.Language) -> str:
    if row["habit_id"] is None:
        return i18n.t("pause_status_all_target", lang)
    habit = registry.get(row["habit_id"])
    return habit.label(lang) if habit is not None else row["habit_id"]


def _status_lines(rows, registry: "HabitRegistry", lang: i18n.Language) -> str:
    return "\n".join(
        i18n.t("pause_status_line", lang, target=_status_target_label(row, registry, lang), date=row["end_date"])
        for row in rows
    )


def _render_status(
    db: "Database",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    habit: "Habit | None",
    today: date,
) -> str:
    """`db.active_pauses` returns every row raw, including one whose
    `end_date` has already passed -- naturally expired, or (round-3 fix,
    Vera's `TestRound2TruncateSemantics`) truncated to yesterday by an
    early `execute_resume`. This status view is the one place users
    actually see "active pauses" listed, so it filters to `end_date >=
    today` itself rather than trusting the raw row set -- mirrors
    `is_paused`'s own date-range check, just applied to the whole list
    instead of one point in time."""
    today_str = today.isoformat()
    rows = [r for r in db.active_pauses(user_id) if r["end_date"] >= today_str]
    if habit is not None:
        matches = [r for r in rows if r["habit_id"] is None or r["habit_id"] == habit.id]
        if not matches:
            return i18n.t("pause_status_none_habit", lang, label=habit.label(lang))
        return (
            i18n.t("pause_status_habit_header", lang, label=habit.label(lang))
            + "\n"
            + _status_lines(matches, registry, lang)
        )
    if not rows:
        return i18n.t("pause_status_none", lang)
    return i18n.t("pause_status_header", lang) + "\n" + _status_lines(rows, registry, lang)


# ---------------------------------------------------------------------------
# execute_pause / execute_resume -- SPEC-v1.9.md §5's own listed
# interfaces.
# ---------------------------------------------------------------------------


async def execute_pause(
    command: "Command",
    *,
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    source: str = "command",
    clock=datetime.now,
) -> str:
    """Validate and perform the `/pause` op described by `command`
    (`core/commands.dispatch`'s `kind="pause"` output), for `user_id`, and
    return the bilingual reply. Never raises: every failure mode --
    unknown habit, an unparseable/past `until` date, an over-cap duration,
    a DB write error -- resolves to a friendly catalog message.

    R12's own "pausing a habit that's already paused" adversarial edge
    resolves to EXTEND/REPLACE, not reject or stack a second row: this
    function always clears any existing pause for the exact same scope
    (`habit_id` or all-habits) immediately before writing the new one, so
    at most one active row ever exists per (user, scope) key -- a second
    `/pause water 5d` right after the first simply resets water's pause
    window starting today, confirmed with the same `pause_set_habit`
    reply. A pre-existing DIFFERENTLY-scoped pause (e.g. an active
    all-habits pause, then `/pause water 3d`) is left untouched -- the two
    coexist (both cover water; `is_paused` already treats "any covering
    row" as paused, so this is never a correctness problem, only two
    independent, differently-scoped windows)."""
    today = clock().date()
    habit_token = command.category
    duration_raw = command.pref_value

    habit: "Habit | None" = None
    if habit_token is not None:
        habit = registry.get(habit_token)
        if habit is None:
            return i18n.t("pause_invalid_habit", lang, habit_id=habit_token, habit_list=", ".join(registry.ids()))

    if duration_raw is None:
        return _render_status(db, registry, lang, user_id, habit, today)

    resolved = _resolve_duration(duration_raw, today)
    if isinstance(resolved, _DurationError):
        if resolved.kind == "invalid_date":
            return i18n.t("pause_invalid_date", lang)
        return i18n.t("pause_usage", lang)
    end_date = resolved

    total_days = (end_date - today).days + 1
    if total_days > config.pause.max_days:
        return i18n.t("pause_too_long", lang, max_days=config.pause.max_days)

    habit_id = habit.id if habit is not None else None
    previous_end = next((r["end_date"] for r in db.active_pauses(user_id) if r["habit_id"] == habit_id), None)

    try:
        db.clear_pauses(user_id, habit_id)
        db.insert_pause(user_id, habit_id, today.isoformat(), end_date.isoformat())
    except Exception:
        logger.exception("Failed to save pause for user %r habit %r", user_id, habit_id)
        return i18n.t("pause_save_failed", lang)

    audit.record(
        db,
        actor=user_id,
        action="pause_set",
        source=source,
        entity=habit_id or "all",
        old_value=previous_end,
        new_value=end_date.isoformat(),
    )

    if habit is not None:
        return i18n.t("pause_set_habit", lang, label=habit.label(lang), date=end_date.isoformat())
    return i18n.t("pause_set_all", lang, date=end_date.isoformat())


def _resume_scope(db: "Database", user_id: str, habit_id: str | None, today: date) -> int:
    """End the pause row (if any) currently covering `today` for the exact
    `(user_id, habit_id)` scope, applying R14's truncate-not-delete
    policy (Vera's `TEST-v1.9-pause.md` finding 3, round-2 fix): a row
    that has already begun accumulating protected days (`start_date <=
    yesterday`) is TRUNCATED to end yesterday, not deleted, so every
    already-elapsed paused date stays NEUTRAL-protected for a
    `compute_streak` walk that already crossed it -- deleting it outright
    would retroactively strip that protection and break a streak that was
    already correctly held. A row that hasn't started protecting any day
    yet (`start_date >= today`, R13's own "no elapsed days" case, Archi's
    round-2 ruling) is deleted outright -- there's nothing elapsed to
    preserve, and a truncated-to-yesterday row would incorrectly predate
    its own start. A row that doesn't currently cover `today` at all
    (already naturally expired, or simply absent) needs no write -- it
    already isn't protecting anything the caller could be asking to end.
    Returns 1 if a currently-covering row was ended (deleted or
    truncated), 0 if there was nothing active to end."""
    today_str = today.isoformat()
    row = next(
        (r for r in db.active_pauses(user_id) if r["habit_id"] == habit_id and r["start_date"] <= today_str <= r["end_date"]),
        None,
    )
    if row is None:
        return 0
    yesterday_str = (today - timedelta(days=1)).isoformat()
    if row["start_date"] > yesterday_str:
        db.clear_pauses(user_id, habit_id)
    else:
        db.truncate_pause(user_id, habit_id, yesterday_str)
    return 1


async def execute_resume(
    command: "Command",
    *,
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    source: str = "command",
    clock=datetime.now,
) -> str:
    """Validate and perform the `/resume` op described by `command`
    (`core/commands.dispatch`'s `kind="resume"` output), for `user_id`,
    and return the bilingual reply. Never raises.

    R13: `/resume <habit>` ends only the pause row(s) EXACTLY scoped to
    that habit (`_resume_scope`, keyed to `habit.id`) -- it does NOT
    split or otherwise touch a separately-scoped active all-habits row
    that also happens to currently cover that habit. If a user has only
    an all-habits pause active and runs `/resume water`, there is no
    habit-scoped row to end, so this reports "nothing to resume for
    water specifically" -- but (round-2 fix, Archi ruling 2) the reply
    must stay truthful about water still being paused via that untouched
    all-habits row (`pause_covered_by_all`), never claim water "isn't
    paused" (that wording, `pause_none_active_habit`, is reserved for the
    genuinely-not-paused-at-all case). The storage schema (a NULL-scope
    row has no way to exclude one habit without rewriting it as N-1
    explicit per-habit rows) makes an actual "smart split" a materially
    larger, unspecified feature -- see IMPL-v1.9-pause.md's "Known
    limitations" for the full rationale. `/resume` (bare, no token)
    always ends every active pause regardless of scope.

    `_resume_scope`'s truncate-not-delete policy (R13/R14 tension,
    round-2 finding 3) applies identically whether `/resume` is
    habit-scoped or bare -- both paths route through the same helper, so
    an early bare `/resume` can never retroactively un-protect an
    already-elapsed pause window either."""
    habit_token = command.category
    today = clock().date()
    today_str = today.isoformat()

    if habit_token is None:
        rows = db.active_pauses(user_id)
        if not rows:
            return i18n.t("pause_none_active_all", lang)
        keys = {row["habit_id"] for row in rows}
        try:
            cleared = sum(_resume_scope(db, user_id, key, today) for key in keys)
        except Exception:
            logger.exception("Failed to resume all habits for user %r", user_id)
            return i18n.t("pause_save_failed", lang)
        if cleared == 0:
            return i18n.t("pause_none_active_all", lang)
        audit.record(db, actor=user_id, action="pause_clear", source=source, entity="all", old_value=cleared)
        return i18n.t("pause_resumed_all", lang)

    habit = registry.get(habit_token)
    if habit is None:
        return i18n.t("pause_invalid_habit", lang, habit_id=habit_token, habit_list=", ".join(registry.ids()))

    try:
        cleared = _resume_scope(db, user_id, habit.id, today)
    except Exception:
        logger.exception("Failed to resume habit %r for user %r", habit.id, user_id)
        return i18n.t("pause_save_failed", lang)

    if cleared == 0:
        all_row = next(
            (r for r in db.active_pauses(user_id) if r["habit_id"] is None and r["start_date"] <= today_str <= r["end_date"]),
            None,
        )
        if all_row is not None:
            return i18n.t("pause_covered_by_all", lang, label=habit.label(lang), date=all_row["end_date"])
        return i18n.t("pause_none_active_habit", lang, label=habit.label(lang))

    audit.record(db, actor=user_id, action="pause_clear", source=source, entity=habit.id, old_value=cleared)
    return i18n.t("pause_resumed_habit", lang, label=habit.label(lang))
