"""Per-user, per-habit reminder-time set/show/clear/off execution
(SPEC-v1.2.md §4 R-S5, module `schedules`). `core/commands.dispatch`
(R-S5, the "remind" kind) only recognizes the *shape* of a `/remind ...`
/ `เตือน ...` command and produces a `Command` carrying the raw,
unvalidated tail in `Command.times`; this module is where that `Command`
is validated against the live `HabitRegistry`/`Database` and turned into
a bilingual reply -- and, for "set"/"off"/"default", where the DB write
happens via the shared `user_reminder_times` store accessors
(`storage/db.py:get_reminder_times`/`set_reminder_times`/
`clear_reminder_times`, landed in the shared surface, IMPL-v1.2-shared.md).

Every branch below returns a plain string (the reply) and never raises --
same "structured op in, formatted string out, no traceback to the user"
contract as `core/targets_command.execute_target`/`core/query.py.
answer_question`. A DB write failure is caught, logged, and reported via
the generic `remind_save_failed` catalog message, never a stack trace.

R-S4/R-S6: the resolver both this module's "show" path and the minutely
tick (`core/reminders.run_due_reminders`) consult is the SAME
`effective_reminder_times` function (`core/reminders.py`, shared surface)
-- so they can never diverge, and a `/remind` write here takes effect on
the tick's very next run with no scheduler rebuild (AC-S4): the tick
re-reads `user_reminder_times` live on every call, and this module writes
straight to that same table.

Unlike `/target`'s `is_goalable` gate, EVERY habit in the registry is a
valid `/remind` target regardless of type/whether it has a config
`reminder_times` default (SPEC-v1.2.md §4 R-S5's own carve-out: "a
non-reminderable habit ... is allowed to gain custom ones") -- so this
module has no analogous "not remindable" rejection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from habit_assistant.config import _HHMM_RE
from habit_assistant.core import audit, i18n
from habit_assistant.core.reminders import effective_reminder_times

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# SPEC-v1.2.md §4 R-S5: "enforce a sane cap (<= 24 times)".
MAX_REMINDER_TIMES = 24


def _format_times_list(times: list[str], lang: i18n.Language) -> str:
    """A comma-joined `HH:MM` list for a reply, or a localized "no times"
    phrase for the empty-list edge case (e.g. `/remind <habit> default`
    on a habit whose config `reminder_times` is itself empty -- a text/
    boolean habit with no default schedule at all)."""
    if not times:
        return i18n.t("remind_no_default_times", lang)
    return ", ".join(times)


def _validate_and_dedupe_times(tokens: list[str]) -> list[str] | str:
    """Validate every token as `HH:MM` (reusing `config._HHMM_RE`, R-S5),
    then de-dupe and sort. Returns the offending token itself (a `str`) as
    soon as one fails validation -- the caller reports it via
    `remind_invalid_time` -- rejecting the WHOLE set with no partial write
    (R-S5: "reject any invalid token ... (no write)"). Only once every
    token validates does de-dupe run, matching R-S5's stated order
    ("each token must match ... reject any invalid token ...; de-dupe;
    enforce a sane cap")."""
    for token in tokens:
        if not _HHMM_RE.match(token):
            return token
    deduped: list[str] = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
    return sorted(deduped)


def _execute_show(db: "Database", config: "Config", habit: "Habit", lang: i18n.Language, user_id: str) -> str:
    stored = db.get_reminder_times(user_id, habit.id)
    if stored == ["off"]:
        return i18n.t("remind_show_off", lang, label=habit.label(lang))
    source_key = "remind_source_custom" if stored else "remind_source_default"
    times = effective_reminder_times(db, config, habit, user_id)
    return i18n.t(
        "remind_show",
        lang,
        label=habit.label(lang),
        times=_format_times_list(times, lang),
        source=i18n.t(source_key, lang),
    )


def _execute_off(db: "Database", habit: "Habit", lang: i18n.Language, user_id: str, source: str) -> str:
    # SPEC-v1.3.md §2.1: remind_off's old_value is the prior stored times
    # (JSON) -- read BEFORE the write, same "capture in hand" convention as
    # every other capture site here.
    previous_times = db.get_reminder_times(user_id, habit.id)
    try:
        db.set_reminder_times(user_id, habit.id, ["off"])
    except Exception:
        logger.exception("Failed to turn off reminders for user %r habit %r", user_id, habit.id)
        return i18n.t("remind_save_failed", lang)
    audit.record(
        db,
        actor=user_id,
        action="remind_off",
        source=source,
        entity=habit.id,
        old_value=previous_times,
        new_value="off",
    )
    return i18n.t("remind_off", lang, label=habit.label(lang))


def _execute_default(db: "Database", habit: "Habit", lang: i18n.Language, user_id: str, source: str) -> str:
    previous_times = db.get_reminder_times(user_id, habit.id)
    try:
        db.clear_reminder_times(user_id, habit.id)
    except Exception:
        logger.exception("Failed to clear reminder override for user %r habit %r", user_id, habit.id)
        return i18n.t("remind_save_failed", lang)
    audit.record(
        db,
        actor=user_id,
        action="remind_default",
        source=source,
        entity=habit.id,
        old_value=previous_times,
        new_value=None,
    )
    return i18n.t(
        "remind_cleared", lang, label=habit.label(lang), times=_format_times_list(list(habit.reminder_times), lang)
    )


def _execute_set(
    db: "Database", habit: "Habit", tokens: list[str], lang: i18n.Language, user_id: str, source: str
) -> str:
    validated = _validate_and_dedupe_times(tokens)
    if isinstance(validated, str):
        return i18n.t("remind_invalid_time", lang, token=validated)
    if len(validated) > MAX_REMINDER_TIMES:
        return i18n.t("remind_too_many_times", lang, max=MAX_REMINDER_TIMES)

    previous_times = db.get_reminder_times(user_id, habit.id)
    try:
        db.set_reminder_times(user_id, habit.id, validated)
    except Exception:
        logger.exception("Failed to set reminder times for user %r habit %r", user_id, habit.id)
        return i18n.t("remind_save_failed", lang)
    audit.record(
        db,
        actor=user_id,
        action="remind_set",
        source=source,
        entity=habit.id,
        old_value=previous_times,
        new_value=validated,
    )
    return i18n.t("remind_set", lang, label=habit.label(lang), times=_format_times_list(validated, lang))


async def execute_remind(
    command: "Command",
    *,
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    source: str = "command",
) -> str:
    """Validate and perform the `/remind` op described by `command`
    (`core/commands.dispatch`'s `kind="remind"` output), for `user_id`,
    and return the bilingual reply. Never raises: every failure mode --
    unknown habit, an invalid `HH:MM` token, too many times, a DB write
    error -- resolves to a friendly catalog message. `user_id` is not
    part of the SPEC-v1.2.md §5 interface list verbatim, but is required
    here exactly like `core/targets_command.execute_target`'s own
    (already-landed) `user_id` kwarg -- every DB read/write below is
    scoped to the acting user, so two users' `/remind` overrides for the
    same habit are fully independent (R-D4/R-D2's own pattern, applied to
    `user_reminder_times`).

    SPEC-v1.3.md R-C1: `source` defaults to `"command"` (every current
    `/remind` caller is the deterministic command path; there is no
    full-NL `/remind` intent in this codebase, unlike `/target`). `show`
    is read-only (AC-C7) and never records."""
    habit_id = command.category or ""
    habit = registry.get(habit_id)
    if habit is None:
        return i18n.t("remind_invalid_habit", lang, habit_id=habit_id, habit_list=", ".join(registry.ids()))

    times = command.times if command.times is not None else []
    if times == []:
        return _execute_show(db, config, habit, lang, user_id)
    if times == ["off"]:
        return _execute_off(db, habit, lang, user_id, source)
    if times == ["default"]:
        return _execute_default(db, habit, lang, user_id, source)
    return _execute_set(db, habit, times, lang, user_id, source)
