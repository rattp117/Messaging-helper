"""Audit-log read surface (SPEC-v1.3.md §4 "Read surface (module
`audit-view`)", R-V2): `render_recent`, the single formatter behind the
owner-only `/audit [N]` command (`core/commands.py`'s `"audit"` kind,
module `audit-view`'s own addition).

Deterministic, LLM-free, read-only -- built entirely on `db.recent_audit`
(shared surface, IMPL-v1.3-shared.md) and `core/i18n.py`'s catalog, so it
works with Ollama down exactly like `core/discoverability.py`'s `/help`/
`/habits` do (SPEC-v1.1.md R-D2/R-D3's own precedent). No channel import
(mirrors every other formatter module in this codebase -- `core/
discoverability.py`, `core/undo_ui.py`'s `describe_log`) -- this module
only builds a string; `main.py`'s integration step is the one that sends
it, behind its own `access.classify(...) == "owner"` gate (R-V3).

This module never writes to the DB and never resolves whether the caller
is actually the owner -- that gate lives in `main.py` (R-V3), same
"recognize shape here, authorize there" split every other admin command
in this codebase already uses (`core/access.py:execute_admin`'s own
owner re-check)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from habit_assistant.core import i18n
from habit_assistant.core.render_budget import MAX_VALUE_CHARS, TELEGRAM_MESSAGE_BUDGET, fit_within_budget
from habit_assistant.core.render_budget import truncate as _truncate

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-V2: default 20, capped at 50.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# SPEC-v1.4.md R-B1/R-B2: the length-budget machinery below (per-value
# truncation + the structural total-message-length guard,
# TEST-v1.3-view.md's own finding that the 50-row cap alone does not
# bound message length) now lives in `core/render_budget.py`, shared with
# `core/history_view.py` (v1.4.0) -- imported above, not redefined here.
# This module keeps only the two aliases (`_truncate`, and
# `_MAX_VALUE_CHARS`/`_TELEGRAM_MESSAGE_BUDGET` below) so every call site
# already written against this file's own private names needed zero
# further edits -- a pure extract-and-delegate, output byte-identical
# (AC-3, the regression guard is the existing audit-view test suite
# passing unmodified).
_MAX_VALUE_CHARS = MAX_VALUE_CHARS
_TELEGRAM_MESSAGE_BUDGET = TELEGRAM_MESSAGE_BUDGET

# `core/audit.py:ACTIONS`'s 21 values -> the i18n catalog id for their
# localized label (R-V2: "Action/labels localize via core/i18n.py").
# Spelling can't drift from the recorder's own vocabulary because this is
# a lookup keyed by the literal action string, not a hand-typed parallel
# list -- an action recorded under a value not in this map still renders
# (falls back to the raw string below) rather than raising, since a
# read-only view must never crash on a row an older/newer recorder wrote.
_ACTION_LABEL_MSG_IDS: dict[str, str] = {
    "undo": "audit_action_undo",
    "edit": "audit_action_edit",
    "target_set": "audit_action_target_set",
    "target_clear": "audit_action_target_clear",
    "remind_set": "audit_action_remind_set",
    "remind_off": "audit_action_remind_off",
    "remind_default": "audit_action_remind_default",
    "lang_set": "audit_action_lang_set",
    "quiet_set": "audit_action_quiet_set",
    "quiet_off": "audit_action_quiet_off",
    "checkin_set": "audit_action_checkin_set",
    "checkin_off": "audit_action_checkin_off",
    "checkin_default": "audit_action_checkin_default",
    "dashboard_set": "audit_action_dashboard_set",
    "dashboard_off": "audit_action_dashboard_off",
    "user_approve": "audit_action_user_approve",
    "user_block": "audit_action_user_block",
    "user_pending": "audit_action_user_pending",
    "habit_create": "audit_action_habit_create",
    "habit_archive": "audit_action_habit_archive",
    "habit_delete": "audit_action_habit_delete",
    # SPEC-v1.8.md R-S6 (shared surface, module `routines`' own dependency).
    "routine_create": "audit_action_routine_create",
    "routine_delete": "audit_action_routine_delete",
    "routine_run": "audit_action_routine_run",
    # SPEC-v1.9.md §5/§6 (shared surface, modules `cadence`/`pause`/
    # `grace`'s own dependency).
    "cadence_set": "audit_action_cadence_set",
    "cadence_clear": "audit_action_cadence_clear",
    "pause_set": "audit_action_pause_set",
    "pause_clear": "audit_action_pause_clear",
    "grace_consumed": "audit_action_grace_consumed",
}


def _effective_limit(limit: int | None) -> int:
    """R-V2: a missing/invalid N (`None`, from `commands.py`'s own
    "non-numeric tail -> None" contract) uses the default (20); any parsed
    N is capped at 50 (a request above the cap is silently limited, never
    rejected). No lower bound is applied -- `/audit 0` is a well-formed
    request for zero rows, indistinguishable in its result from "nothing
    recorded yet" (both render `audit_empty`); `core/commands.py`'s own
    `\\d+` shape already rejects a negative token down to `None` before
    this is ever reached, so a negative `limit` never occurs in
    practice."""
    if limit is None:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _humanize_stored_value(raw: str | None) -> str:
    """`old_value`/`new_value` are already stringified by `core/audit.py:
    record` (R-W1) -- `None` -> the SQL-NULL sentinel dash; a JSON list
    (remind times, e.g. '["08:00", "12:00"]') -> a compact bracket-joined
    form ("[08:00,12:00]", matching SPEC-v1.3.md §3.1's own sample line)
    instead of the raw JSON's quotes/spaces; everything else (a number's
    `"{:g}"` text, a plain status/language-code string) is already
    human-readable and shown verbatim. Either shape is then truncated to
    `_MAX_VALUE_CHARS` (TEST-v1.3-view.md's finding) -- a long value
    (e.g. a many-time remind schedule) no longer dominates one row; this
    alone does not bound the TOTAL message (many short rows can still
    exceed the limit), which is what `_fit_within_budget`/`render_recent`
    guard separately."""
    if raw is None:
        return "—"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return _truncate(raw)
    if isinstance(parsed, list):
        return _truncate("[" + ",".join(str(item) for item in parsed) + "]")
    return _truncate(raw)


def _format_ts(ts: str) -> str:
    """"MM-DD HH:MM" for a compact chat line (SPEC-v1.3.md §3.1's own
    sample: "08-22 14:03"). `ts` is already the local wall-clock string
    `core/audit.py:record` stored (R-M1: "ISO8601 local, when the action
    happened") -- `config.app.timezone` correctness is that write-time
    caller's responsibility, exactly as `core/discoverability.py:
    build_habits_overview`'s own docstring states for `clock`; this
    function only reformats an already-correct local timestamp for
    display, never converts it. Falls back to the raw stored string on an
    unexpected shape rather than raising -- a read-only view must never
    crash on a row it didn't write."""
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
    except ValueError:
        return ts


def _actor_display(db: "Database", user_id: str, owner_chat_id: str, lang: i18n.Language) -> str:
    """R-V2: the owner's own rows render as "you"; any other actor renders
    as their stored `display_name`, falling back to the raw chat id when
    absent (a user onboarded before `display_name` capture, or a lookup
    failure) -- mirrors `core/access.py:_resolve_unprompted_language_for`'s
    identical fail-open "best-effort lookup, never crash" shape."""
    if user_id == owner_chat_id:
        return i18n.t("audit_actor_you", lang)
    try:
        row = db.get_user(user_id)
    except Exception:
        logger.exception("User lookup failed for audit actor display, user_id=%r; falling back to chat id", user_id)
        row = None
    display_name = row["display_name"] if row is not None else None
    return display_name or user_id


def _action_label(action: str, lang: i18n.Language) -> str:
    msg_id = _ACTION_LABEL_MSG_IDS.get(action)
    if msg_id is None:
        # Defensive only (see _ACTION_LABEL_MSG_IDS's own docstring) --
        # every action `core/audit.py:record` can actually write is in
        # the map above; this never fires against real data.
        return action
    return i18n.t(msg_id, lang)


def _detail(row) -> str:
    """The entity + old→new segment (SPEC-v1.3.md §3.1's own sample:
    "water · 2500 → 2000"). Admin actions (§2.1: `user_approve`/
    `user_block`/`user_pending`) record `entity=NULL` but carry a
    `target_user_id` -- shown in entity's own slot instead (the "target
    user" the dispatch note calls for), since a line about "what changed
    on WHICH chat" reads the same way whether that "which" is a habit id
    or another chat's id."""
    entity = row["entity"] or row["target_user_id"]
    change = f"{_humanize_stored_value(row['old_value'])} → {_humanize_stored_value(row['new_value'])}"
    if entity:
        return f"{entity} · {change}"
    return change


def _fit_within_budget(header: str, row_lines: list[str], lang: i18n.Language) -> str:
    """SPEC-v1.4.md R-B1/R-B2: thin wrapper over the now-shared
    `render_budget.fit_within_budget`, supplying the bilingual
    `audit_more_rows` footer this module owns -- the length/drop logic
    itself lives in `core/render_budget.py` (shared with `core/
    history_view.py`'s own `history_more_rows` footer). Output
    byte-identical to the pre-extraction inline version (AC-3)."""
    return fit_within_budget(header, row_lines, render_footer=lambda dropped: i18n.t("audit_more_rows", lang, count=dropped))


def render_recent(db: "Database", config: "Config", lang: i18n.Language, *, limit: int | None, owner_chat_id: str) -> str:
    """R-V2: the most recent `limit` (default 20, capped 50) `audit_log`
    rows, newest-first (`db.recent_audit`'s own `ORDER BY id DESC`), one
    bilingual line each. No rows at all -> the friendly `audit_empty`
    message (§3.2), regardless of whether that's because nothing has ever
    been recorded or because `limit` itself resolved to 0. `config` is
    accepted for parity with this codebase's other view-builders
    (`core/discoverability.py:build_help_text`/`build_habits_overview`)
    and reserved for a future config-driven rendering knob; nothing this
    function renders today varies by it.

    TEST-v1.3-view.md's finding: the fully-rendered message is always
    checked against `_TELEGRAM_MESSAGE_BUDGET` before being returned --
    an overflow (any cause: many rows, long values, or both) is repaired
    by `_fit_within_budget` dropping the oldest shown rows and appending
    a "N more" footer, never by silently handing `channel.send` a message
    Telegram's API would reject."""
    del config  # not used -- reserved for parity with build_help_text/build_habits_overview's own signature shape.
    effective = _effective_limit(limit)
    rows = db.recent_audit(effective)
    if not rows:
        return i18n.t("audit_empty", lang)

    header = i18n.t("audit_header", lang, limit=effective)
    row_lines = [
        i18n.t(
            "audit_line",
            lang,
            ts=_format_ts(row["ts"]),
            actor=_actor_display(db, row["user_id"], owner_chat_id, lang),
            action=_action_label(row["action"], lang),
            detail=_detail(row),
            source=row["source"],
        )
        for row in rows
    ]

    full = "\n".join([header, *row_lines])
    if len(full) <= _TELEGRAM_MESSAGE_BUDGET:
        return full
    return _fit_within_budget(header, row_lines, lang)
