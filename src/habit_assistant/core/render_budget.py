"""Shared render-budget / truncation helpers (SPEC-v1.4.md §4 R-B1).

Extracted from `core/audit_view.py` (v1.3.0's own "the 50-row cap alone
doesn't bound message length" fix, `TEST-v1.3-view.md`'s finding) so
`core/history_view.py` (v1.4.0) can reuse the EXACT same machinery
instead of copy-pasting it -- one place this class of "a Telegram
`sendMessage` would 400 on an over-length reply" bug gets fixed, not two.

**i18n-agnostic by design** (R-B1): the "N more" footer text is injected
by the caller via `render_footer`, so this module never imports
`core/i18n.py` and never needs to know which catalog id or language a
caller wants -- `core/audit_view.py` passes a closure rendering
`audit_more_rows`, `core/history_view.py` passes one rendering
`history_more_rows`. This module also never imports a channel or the DB
-- it only knows about strings and their lengths (mirrors every other
formatter module's own "no channel/DB import" seam, `core/
discoverability.py`/`core/undo_ui.py:describe_log`)."""

from __future__ import annotations

from typing import Callable

# SPEC-v1.3.md §9 / SPEC-v1.4.md §5: per-value truncation cap and
# Telegram's `sendMessage` hard length limit. Both callers (`audit_view`,
# `history_view`) share these exact numbers -- there is no per-caller
# override anywhere in this codebase, so they're plain module constants,
# not function parameters with a default that could drift between callers.
MAX_VALUE_CHARS = 60
TELEGRAM_MESSAGE_BUDGET = 4096


def truncate(text: str, max_chars: int = MAX_VALUE_CHARS) -> str:
    """A single ellipsis character replaces the tail past `max_chars` --
    no attempt to cut on a nicer boundary (item/word), since every
    caller of this is a technical log/statement view (mirrors `core/
    undo_ui.py:describe_log`'s own diary-snippet truncation, which is
    likewise a flat character cut, not word-aware)."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def fit_within_budget(header: str, row_lines: list[str], *, render_footer: Callable[[int], str]) -> str:
    """The STRUCTURAL total-length guard (`TEST-v1.3-view.md`'s own
    finding): per-value truncation alone does not bound the TOTAL
    message -- many short, untruncated rows can add up past
    `TELEGRAM_MESSAGE_BUDGET` just as easily as a few long ones. This is
    the actual guarantee: drop the OLDEST shown rows (the tail of
    `row_lines` -- every caller builds this newest-first) one at a time
    until `header` + the kept rows + `render_footer(dropped_count)` fits,
    so a caller's `channel.send` never receives a message
    `channels/telegram.py` would get a 400 back for.

    `row_lines` is never empty when a caller reaches this (both current
    callers only call it after confirming the FULL message overflows,
    which requires at least one row); the `not kept` floor below is
    defensive only, not a reachable case in practice -- `header` + one
    footer line is well under the budget by construction (both are
    short, fixed-shape catalog strings)."""
    kept = list(row_lines)
    while True:
        dropped = len(row_lines) - len(kept)
        parts = [header, *kept]
        if dropped:
            parts.append(render_footer(dropped))
        candidate = "\n".join(parts)
        if len(candidate) <= TELEGRAM_MESSAGE_BUDGET or not kept:
            return candidate
        kept.pop()
