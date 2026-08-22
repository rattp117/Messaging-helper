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

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-V2: default 20, capped at 50.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# TEST-v1.3-view.md's finding: the 50-row cap alone does not bound message
# length -- a plausible mix of `remind_set` edits at 50 rows already
# exceeds Telegram's hard `sendMessage` limit (`channels/telegram.py:
# send()` posts `text` as-is, no chunking). Two independent mitigations,
# both scoped entirely to this module:
#   1. `_MAX_VALUE_CHARS` -- per-value truncation (SPEC-v1.3.md §9: "the
#      viewer truncates the value display"), keeps a pathologically long
#      single old/new value (e.g. a MAX_REMINDER_TIMES=24 schedule) from
#      dominating one row.
#   2. `_TELEGRAM_MESSAGE_BUDGET` -- a STRUCTURAL total-length guard
#      (`_fit_within_budget`): the actual guarantee. Per-value truncation
#      alone does not bound the TOTAL message (50 rows of even short,
#      untruncated values can still add up past 4096 -- exactly Vera's
#      failing case, each row's value well under the per-value cap), so
#      `render_recent` always verifies the fully-rendered message against
#      this budget and drops the oldest shown rows (the tail of a
#      newest-first list) until it fits, appending a bilingual "N more"
#      footer -- regardless of which field caused the overflow.
_MAX_VALUE_CHARS = 60
_TELEGRAM_MESSAGE_BUDGET = 4096

# `core/audit.py:ACTIONS`'s 13 values -> the i18n catalog id for their
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
    "user_approve": "audit_action_user_approve",
    "user_block": "audit_action_user_block",
    "user_pending": "audit_action_user_pending",
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


def _truncate(text: str, max_chars: int = _MAX_VALUE_CHARS) -> str:
    """SPEC-v1.3.md §9: "the viewer truncates the value display." A single
    ellipsis character replaces the tail past `max_chars` -- no attempt to
    cut on a nicer boundary (item/word), since this is a technical log
    view (mirrors `core/undo_ui.py:describe_log`'s own diary-snippet
    truncation, which is likewise a flat character cut, not word-aware)."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


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
    """TEST-v1.3-view.md's finding: the 50-row cap plus per-value
    truncation (`_humanize_stored_value`) still does not structurally
    bound the TOTAL message -- many short, untruncated rows can add up
    past Telegram's `_TELEGRAM_MESSAGE_BUDGET` just as easily as a few
    long ones. This is the actual guarantee: drop the OLDEST shown rows
    (the tail of `row_lines`, since `render_recent` builds it
    newest-first) one at a time until `header` + the kept rows + a
    bilingual "N more" footer fits, so the caller's `channel.send` never
    receives a message `channels/telegram.py` would get a 400 back for.
    `row_lines` is never empty when this is called (`render_recent` only
    calls it after confirming the FULL message overflows, which requires
    at least one row)."""
    kept = list(row_lines)
    while True:
        dropped = len(row_lines) - len(kept)
        parts = [header, *kept]
        if dropped:
            parts.append(i18n.t("audit_more_rows", lang, count=dropped))
        candidate = "\n".join(parts)
        if len(candidate) <= _TELEGRAM_MESSAGE_BUDGET or not kept:
            # `not kept` is a defensive floor, not a reachable case in
            # practice: header + one footer line is well under the budget
            # by construction (both are short, fixed-shape catalog
            # strings), so the loop above always finds a fit before
            # `kept` empties out.
            return candidate
        kept.pop()


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
