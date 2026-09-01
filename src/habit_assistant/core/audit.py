"""Audit trail recorder (SPEC-v1.3.md §4 "Storage & recorder (shared
surface)", R-W1/R-W2): the ONE function every capture site (`undo_ui`,
`targets_command`, `schedules`, `preferences`, `access`, `main.py`) calls
to durably record a state-changing action -- who (actor), when, what
(action + entity), old value -> new value, and how (source).

**R-W2 is the load-bearing safety rule: `record` never raises and never
blocks the user's action.** A capture site calls `record(...)` AFTER its
own successful DB write completes, and ignores the return value (there is
none -- `-> None`) -- an audit-write failure (DB locked, table missing,
a bad value that somehow slips past `_stringify_value`) is caught, logged,
and swallowed right here, one layer beneath every caller, so no capture
site needs its own try/except around this call. This is deliberately
"structurally hard to misuse" (the coordinator's own framing): the
ENTIRE body below is one `try`, not just the DB call, so literally
nothing this function does -- not even stringifying a value -- can ever
propagate an exception to a caller.

No channel import here (mirrors `core/reminders.py`/`core/query.py`'s own
"no channel imports" seam) -- this module only knows about the DB."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from habit_assistant.storage.models import AuditEntry

if TYPE_CHECKING:
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# SPEC-v1.3.md §5: the closed vocabulary every capture site's `action=`
# argument must come from -- spelling can't drift between a capture site
# and `audit_view`'s own label lookup because both import from here.
Action = Literal[
    "undo",
    "edit",
    "target_set",
    "target_clear",
    "remind_set",
    "remind_off",
    "remind_default",
    "lang_set",
    "quiet_set",
    "quiet_off",
    "checkin_set",
    "checkin_off",
    "checkin_default",
    "dashboard_set",
    "dashboard_off",
    "user_approve",
    "user_block",
    "user_pending",
    # SPEC-v1.7.md R-A1 (module `habitdef`): /addhabit / /delhabit, one
    # fail-open audit row each -- `habit_archive` for the soft-delete
    # branch (has history), `habit_delete` for the hard-delete branch (no
    # logs yet), per R-C2's own smart-delete split.
    "habit_create",
    "habit_archive",
    "habit_delete",
    # SPEC-v1.8.md R-S6 (shared surface, module `routines`' own dependency):
    # /routine create/delete/run, one fail-open audit row each -- quick-log
    # and backfill produce ordinary `logs` rows and are deliberately NOT
    # audited (logging has never been audited in this codebase, only
    # mutations -- undo/edit/target/remind/lang/quiet/approve/block/invite/
    # onboard/addhabit/delhabit, and now routine create/delete/run).
    "routine_create",
    "routine_delete",
    "routine_run",
    # SPEC-v1.9.md §5/§6 (shared surface, modules `cadence`/`pause`/
    # `grace`'s own dependency): the five new mutations Theme A's engine
    # rework introduces -- `cadence_set`/`cadence_clear` (/cadence <habit>
    # <N>|off), `pause_set`/`pause_clear` (/pause, /resume), and
    # `grace_consumed` (the one write the nightly `evaluate_grace` job
    # makes, R9). Quick-log/backfill-style READS (a plain habit log) are
    # deliberately NOT audited here either, same "mutations only" line
    # this vocabulary has always drawn.
    "cadence_set",
    "cadence_clear",
    "pause_set",
    "pause_clear",
    "grace_consumed",
    # SPEC-LINE.md §4 R-C4 (module C, branch `line-version`): `/digest
    # on|off`'s own mutation, one fail-open audit row each -- `digest_set`
    # for "on" (subscribed), `digest_off` for "off" (opted out), mirroring
    # `dashboard_set`/`dashboard_off`'s identical naming.
    "digest_set",
    "digest_off",
]
ACTIONS: tuple[Action, ...] = (
    "undo",
    "edit",
    "target_set",
    "target_clear",
    "remind_set",
    "remind_off",
    "remind_default",
    "lang_set",
    "quiet_set",
    "quiet_off",
    "checkin_set",
    "checkin_off",
    "checkin_default",
    "dashboard_set",
    "dashboard_off",
    "user_approve",
    "user_block",
    "user_pending",
    "habit_create",
    "habit_archive",
    "habit_delete",
    "routine_create",
    "routine_delete",
    "routine_run",
    "cadence_set",
    "cadence_clear",
    "pause_set",
    "pause_clear",
    "grace_consumed",
    "digest_set",
    "digest_off",
)


# SPEC-LINE-PORTAL.md §4 R-USERACT-1/§5 (module USERS, admin web portal,
# branch line-version): "portal" added for `access.approve_user`/
# `block_user`'s portal-sourced writes (AC16/AC17/AC18) -- the shared-
# surface pass that extracted those functions typed `source` as this very
# `audit.Source` but didn't extend the closed vocabulary itself; closed
# here since AC16-18 are unsatisfiable against `test_sources_matches_the_
# spec_vocabulary_exactly` otherwise (see tests/test_audit.py, updated
# alongside this).
Source = Literal["command", "nl", "button", "admin", "system", "portal"]
SOURCES: tuple[Source, ...] = ("command", "nl", "button", "admin", "system", "portal")


def _stringify_value(value: object) -> str | None:
    """R-W1's own value-shaping rule: `None` -> `None` (stored as SQL
    `NULL`); a `list`/`dict` -> `json.dumps` (the exact shape remind
    times / quiet windows are already stored/read as elsewhere in this
    codebase, so an `audit_view` reader can `json.loads` it straight
    back); a number (`int`/`float`, excluding `bool` -- `isinstance(True,
    int)` is `True` in Python, and a boolean habit's 0.0/1.0 already
    renders through the float branch, never reaching here as a literal
    `bool`) -> `"{:g}"`-style text (matches `core/i18n.py`'s own
    `"{goal:g}"` number formatting elsewhere in this app); everything
    else (a plain string -- a language code, a status word, an
    already-formatted value a capture site passed pre-stringified) ->
    `str(value)`, a no-op for an actual `str`."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return str(value)


def record(
    db: "Database",
    *,
    actor: str,
    action: Action,
    source: Source,
    entity: str | None = None,
    old_value: object = None,
    new_value: object = None,
    target_user_id: str | None = None,
    clock=datetime.now,
) -> None:
    """SPEC-v1.3.md R-W1/R-W2: record one `audit_log` row. Fail-open --
    see this module's own docstring above for why the entire body is one
    `try`. Every capture site's own calling convention is: perform your
    write, THEN call `record(db, actor=..., action=..., source=..., ...)`
    with the plain Python values you already have in hand (a `float`
    goal, a `list[str]` of times, a `str` status) -- never pre-format
    them yourselves; `_stringify_value` above is the one place that
    happens, so every action's `old_value`/`new_value` column is shaped
    identically regardless of which capture site wrote it.

    `actor` is who performed the action (the row's `user_id`); for an
    admin action done TO another chat (`user_approve`/`user_block`/
    `user_pending`), `actor` is the ACTING chat (the owner, or -- for
    `user_pending`, R-C3 -- the chat itself, since nobody else "acted")
    and `target_user_id` is the affected chat. `clock` is the same
    injectable-clock shape every other testable "what time is it" call
    site in this codebase already uses (`main.py`'s own
    `handle_inbound_message`, `core/undo_ui.py`'s `send_undo_confirmation`)
    -- defaults to the real `datetime.now` so no caller needs to pass it
    explicitly in production."""
    try:
        entry = AuditEntry(
            id=None,
            ts=clock().isoformat(timespec="seconds"),
            user_id=actor,
            action=action,
            entity=entity,
            old_value=_stringify_value(old_value),
            new_value=_stringify_value(new_value),
            source=source,
            target_user_id=target_user_id,
        )
        db.insert_audit(entry)
    except Exception:
        # R-W2: logged and swallowed, never re-raised. The caller already
        # completed its own write and reply before calling record() --
        # this except block is the entire point of the function existing
        # as a separate, fail-open layer instead of every capture site
        # calling db.insert_audit directly.
        logger.exception(
            "Audit record failed (fail-open, action=%r actor=%r); continuing", action, actor
        )
