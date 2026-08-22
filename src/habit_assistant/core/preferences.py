"""Per-user preference commands (SPEC-v1.2.md §4 R-P1/R-P2, module
`preferences`): `/lang` (per-user reply-language pin) and `/quiet`
(per-user quiet-hours windows). `core/commands.dispatch` (this module's
own disjoint `"lang"`/`"quiet"` kinds, added alongside `access`'s and
`schedules`'s) only recognizes the *shape* of a `/lang ...`/`/quiet ...`
(or Thai-alias `ภาษา`/`เงียบ`) command and produces a `Command`; this
module is where that `Command` is validated and turned into a `users`
table write + bilingual reply -- same "structured op in, formatted
string out, never raises" contract as `core/targets_command.
execute_target` and `core/schedules.execute_remind`.

`execute_lang` writes `users.language_pref` via `Database.
set_user_language` (shared surface, IMPL-v1.2-shared.md). `execute_quiet`
writes `users.quiet_hours_json` via `Database.set_user_quiet_hours`
(shared surface) as a JSON `[[start, end], ...]` list -- the EXACT shape
`core/reminders.py:effective_quiet_windows` already reads (R-P2: NULL =
inherit `config.quiet_hours.windows`, `"[]"` = an explicit "no quiet
hours for me", distinct states). Together with the shared surface's
already-landed READ side (`core/i18n.py:resolve_reply_language`/
`resolve_unprompted_language`'s `user_pref` parameter and `core/
reminders.py:effective_quiet_windows`), these two functions close the
per-user language/quiet-hours loop end to end (AC-P1/AC-P2) -- proven by
this module's own tests (`tests/test_preferences.py`) composing the
write side here with the shared surface's read side directly, without
needing `main.py` wired up.

Neither function is called from `main.py` yet (SPEC-v1.2.md §11:
`access`/`preferences`/`schedules` are independent parallel modules;
routing `Command.kind in ("lang", "quiet")` to these two functions is
integration's job, landed after all three report done) -- see
IMPL-v1.2-preferences.md's "How it works" for the exact wiring calls.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from habit_assistant.config import _HHMM_RE
from habit_assistant.core import audit, i18n

if TYPE_CHECKING:
    from habit_assistant.core.commands import Command
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

_VALID_LANG_VALUES = {"en", "th", "auto"}


async def execute_lang(
    command: "Command", *, db: "Database", lang: i18n.Language, user_id: str, source: str = "command"
) -> str:
    """SPEC-v1.2.md R-P1: set `user_id`'s reply-language preference.
    `command.pref_value` is the lowercased trigger tail `core/commands.
    dispatch` captured -- "en"/"th"/"auto", or `None` for a bare "/lang"/
    "ภาษา" (or a Thai-alias match, which never matches with no value at
    all -- `_match_lang`'s own contract). An unrecognized value (a typo,
    e.g. "/lang thai") is reported exactly the same way as "no value at
    all" (`lang_usage`) -- never silently guessed at or defaulted, same
    conservatism as `execute_target`'s `target_invalid_value`/
    `target_usage` split.

    The confirmation replies in the NEWLY set language when it's a
    concrete "en"/"th" -- so "/lang th" confirms in Thai even though
    `lang` (resolved by the caller from the inbound "/lang th" text
    itself, which has zero Thai characters) would otherwise have been
    English -- directly satisfying AC-P1's "B's replies are Thai
    regardless of input language" starting with this very confirmation.
    "auto" has no concrete language of its own to confirm in, so it
    falls back to the caller-supplied `lang` (byte-identical to every
    pre-v1.2 confirmation's own posture).

    SPEC-v1.3.md R-C1: `source` defaults to `"command"` (there is no
    full-NL `/lang` intent in this codebase). The prior preference is read
    BEFORE the write (`row["language_pref"]`, or `None` if `user_id` has
    no `users` row yet -- unlikely for an already-gated active user, but
    not this function's job to assume)."""
    raw = (command.pref_value or "").strip().lower()
    if raw not in _VALID_LANG_VALUES:
        return i18n.t("lang_usage", lang)

    reply_lang: i18n.Language = raw if raw in ("en", "th") else lang
    try:
        previous_row = db.get_user(user_id)
    except Exception:
        previous_row = None
    previous_pref = previous_row["language_pref"] if previous_row is not None else None
    try:
        db.set_user_language(user_id, raw)
    except Exception:
        logger.exception("Failed to set language preference for user %r", user_id)
        return i18n.t("preferences_save_failed", reply_lang)
    audit.record(db, actor=user_id, action="lang_set", source=source, old_value=previous_pref, new_value=raw)
    return i18n.t("lang_set", reply_lang, value=raw)


def _parse_quiet_windows(raw: str) -> list[list[str]] | None:
    """Parse "HH:MM-HH:MM[,HH:MM-HH:MM...]" into `[[start, end], ...]`,
    or `None` if ANY token fails to parse cleanly -- mirrors R-S5's own
    "reject any invalid token, write nothing" contract for `/remind`
    times (`core/schedules.py`). Reuses `config._HHMM_RE` (the exact
    pattern `QuietHoursConfig` itself validates `[quiet_hours].windows`
    with), so a stored window is guaranteed parseable by `core/
    reminders.py:_in_quiet_hours` later -- no drift between what this
    function accepts and what the config-level default already accepts."""
    windows: list[list[str]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            return None
        parts = token.split("-")
        if len(parts) != 2:
            return None
        start, end = parts[0].strip(), parts[1].strip()
        if not (_HHMM_RE.match(start) and _HHMM_RE.match(end)):
            return None
        windows.append([start, end])
    return windows


def _format_windows(windows: list[list[str]]) -> str:
    return ", ".join(f"{start}-{end}" for start, end in windows)


async def execute_quiet(
    command: "Command", *, db: "Database", lang: i18n.Language, user_id: str, source: str = "command"
) -> str:
    """SPEC-v1.2.md R-P2: set `user_id`'s quiet-hours override.
    `command.pref_value` is the lowercased trigger tail -- "off" clears to
    an explicit EMPTY list (distinct from never-set/NULL-inherit -- R-P2's
    own distinction; `core/reminders.py:effective_quiet_windows` treats
    only a NULL `quiet_hours_json` as "inherit config", so `"[]"`
    genuinely means "no quiet hours for me" even when the global config
    has some), one or more comma-separated "HH:MM-HH:MM" windows sets
    them, and anything else (a malformed token, or a bare "/quiet"/
    "เงียบ" with no value at all) is rejected with `quiet_usage`/
    `quiet_invalid_window` -- no write, mirroring R-S5's "reject, don't
    guess" contract for `/remind`.

    SPEC-v1.3.md R-C1: `source` defaults to `"command"` (no full-NL
    `/quiet` intent exists). The prior `quiet_hours_json` (already a JSON
    string or `None` -- the column's own stored shape) is read BEFORE the
    write and passed through to `audit.record` unchanged, exactly as
    stored (`_stringify_value` is a no-op on an already-`str` value)."""
    raw = (command.pref_value or "").strip().lower()
    if not raw:
        return i18n.t("quiet_usage", lang)

    try:
        previous_row = db.get_user(user_id)
    except Exception:
        previous_row = None
    previous_json = previous_row["quiet_hours_json"] if previous_row is not None else None

    if raw == "off":
        try:
            db.set_user_quiet_hours(user_id, "[]")
        except Exception:
            logger.exception("Failed to clear quiet hours for user %r", user_id)
            return i18n.t("preferences_save_failed", lang)
        audit.record(db, actor=user_id, action="quiet_off", source=source, old_value=previous_json, new_value=[])
        return i18n.t("quiet_cleared", lang)

    windows = _parse_quiet_windows(raw)
    if windows is None:
        return i18n.t("quiet_invalid_window", lang)

    try:
        db.set_user_quiet_hours(user_id, json.dumps(windows))
    except Exception:
        logger.exception("Failed to set quiet hours for user %r", user_id)
        return i18n.t("preferences_save_failed", lang)
    audit.record(
        db, actor=user_id, action="quiet_set", source=source, old_value=previous_json, new_value=windows
    )
    return i18n.t("quiet_set", lang, windows=_format_windows(windows))
