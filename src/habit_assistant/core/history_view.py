"""Personal log-entry statement (SPEC-v1.4.md §4 "Rendering (module
`history`)", R-R1-R-R5): `render_history`, the single formatter behind
the per-user, LLM-free `/history [<habit>] [N]` command (`core/
commands.py`'s `"history"` kind, module `history`'s own addition).

Deterministic, LLM-free, read-only -- built entirely on `db.recent_logs`
(shared surface, `storage/db.py`) and `core/i18n.py`'s catalog, so it
works with Ollama down exactly like `/help`/`/habits`/`/audit` do
(SPEC-v1.1.md R-D2/R-D3, SPEC-v1.3.md R-V2's own precedent). No channel
import (mirrors every other formatter module in this codebase).

Unlike `/audit` (owner-only, `core/audit_view.py`), `/history` is
available to ANY active user, for their OWN data only -- there is no
"actor" concept here at all (every row already belongs to the one
`user_id` asking, R-D1's own `WHERE user_id = ?` scoping) and no
`owner_chat_id` parameter, matching SPEC-v1.4.md §4 R-A1's explicit "no
owner check" contract. Reuses `core/render_budget.py`'s shared
truncation/length-budget machinery (extracted from `core/audit_view.py`,
R-B1/R-B2) rather than reimplementing it, and `core/undo_ui.py:
describe_log` for the habit + value/unit segment (R-R2) rather than
duplicating that per-type formatting a third time."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from habit_assistant.core import i18n, undo_ui
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET, fit_within_budget, truncate

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

# R-R1: default 20, capped at 50 -- mirrors `core/audit_view.py`'s own
# `DEFAULT_LIMIT`/`MAX_LIMIT` exactly (same names, same values, kept as a
# separate pair rather than importing audit_view's -- these two modules
# have no other dependency on each other, and duplicating two int
# constants is cheaper than introducing one for a coincidence that isn't
# actually a shared contract, R-B1 only asks for the RENDER-BUDGET helpers
# to be shared, not these).
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# R-R3: user-controlled `raw_message` must not break the renderer.
# Collapses any run of control characters (newlines, carriage returns,
# tabs, other C0 controls, and DEL) to a single space, so one entry
# always renders as exactly one line regardless of what the user actually
# typed. Ordinary printable Unicode (Thai script, emoji, punctuation,
# literal `{`/`}`) is left completely untouched by this regex -- `{`/`}`
# need no escaping at all, handled instead by NEVER passing the sanitized
# text as a format template (see `_line` below).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _effective_limit(limit: int | None) -> int:
    """R-R1: a missing/invalid N uses the default (20); any parsed N is
    capped at 50 -- mirrors `core/audit_view.py:_effective_limit`
    exactly (a request above the cap is silently limited, never
    rejected; `/history 0` is a well-formed request for zero rows,
    indistinguishable in its result from "no entries yet", both render
    `history_empty`)."""
    if limit is None:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _sanitize_raw_message(text: str) -> str:
    """R-R3: collapse control characters to a single space, strip the
    result, then `truncate` via the shared `render_budget` helper. The
    sanitized text is passed to `i18n.t` only as a keyword VALUE (never
    as the template string itself), so a literal `{`/`}` the user typed
    can never be misinterpreted as a `.format()` placeholder -- `i18n.t`
    only ever calls `.format(**kwargs)` on the CATALOG entry, not on any
    caller-supplied value (AC-12)."""
    collapsed = _CONTROL_CHARS_RE.sub(" ", text).strip()
    return truncate(collapsed)


def _format_ts(ts: str) -> str:
    """"MM-DD HH:MM" for a compact chat line (SPEC-v1.4.md §3.1's own
    sample: "08-23 14:03") -- mirrors `core/audit_view.py:_format_ts`
    exactly. `ts` is already the local wall-clock string `main.py`
    stored at log time (`config.app.timezone` correctness is that
    write-time caller's responsibility); this function only reformats an
    already-correct local timestamp, never converts it. Falls back to
    the raw stored string on an unexpected shape rather than raising --
    a read-only view must never crash on a row it didn't write."""
    try:
        return datetime.fromisoformat(ts).strftime("%m-%d %H:%M")
    except ValueError:
        return ts


def _line(row, registry: "HabitRegistry", lang: i18n.Language) -> str:
    """One statement line (SPEC-v1.4.md §3.1): timestamp, `describe_log`'s
    habit+value/unit summary (R-R2 -- the exact same one-liner `/undo`'s
    own confirmation reuses, so a habit's description never drifts
    between the two surfaces), the sanitized+quoted original message, and
    -- only for a soft-deleted row -- the localized undone marker."""
    description = undo_ui.describe_log(row, registry, lang)
    message = _sanitize_raw_message(row["raw_message"] or "")
    undone = i18n.t("history_undone_marker", lang) if row["deleted_at"] is not None else ""
    return i18n.t(
        "history_line",
        lang,
        ts=_format_ts(row["ts"]),
        description=description,
        message=message,
        undone=undone,
    )


def render_history(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    *,
    user_id: str,
    category: str | None,
    limit: int | None,
) -> str:
    """R-R1: the caller's (`user_id`'s own, U-ISO) most recent `limit`
    (default 20, capped 50) logged entries -- newest-first (`db.
    recent_logs`'s own `ORDER BY ts DESC, id DESC`), including soft-
    deleted (undone) entries clearly marked (R-R2), excluding
    `category='unparsed'` rows (`db.recent_logs`'s own contract).

    `category`, when given, is validated HERE (not by `core/commands.py`,
    which only recognizes shape) -- an unresolved habit token (the raw,
    lowercased string `_resolve_target_category` couldn't match against
    the registry) short-circuits to the friendly `history_invalid_habit`
    reply before any DB read happens at all (R-D2/AC-6). No rows at all
    (a genuinely empty history, or a valid filter that just has none) ->
    the friendly `history_empty` message (§3.2/AC-13).

    The fully-rendered message is always checked against `render_budget.
    TELEGRAM_MESSAGE_BUDGET` before being returned -- an overflow (many
    rows, long sanitized messages, or both) is repaired by `render_
    budget.fit_within_budget` dropping the oldest shown rows and
    appending a bilingual `history_more_rows` footer (R-R4/AC-12), the
    exact same structural guarantee `core/audit_view.py:render_recent`
    already has, reusing the SAME shared helper rather than a second
    copy of the same fix (R-B1)."""
    del config  # not used -- parity with build_help_text/build_habits_overview/audit_view.render_recent's own signature shape.
    if category is not None and registry.get(category) is None:
        return i18n.t("history_invalid_habit", lang, habit_id=category, habit_list=", ".join(registry.ids()))

    effective = _effective_limit(limit)
    rows = db.recent_logs(user_id, effective, category)
    if not rows:
        return i18n.t("history_empty", lang)

    if category is not None:
        habit = registry.get(category)
        header = i18n.t("history_header_filtered", lang, limit=effective, habit=habit.label(lang))
    else:
        header = i18n.t("history_header", lang, limit=effective)

    row_lines = [_line(row, registry, lang) for row in rows]

    full = "\n".join([header, *row_lines])
    if len(full) <= TELEGRAM_MESSAGE_BUDGET:
        return full
    return fit_within_budget(header, row_lines, render_footer=lambda dropped: i18n.t("history_more_rows", lang, count=dropped))
