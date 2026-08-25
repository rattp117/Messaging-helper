"""Shared helper: a user's stored `/lang` preference (SPEC-v1.8.md
integration step, Archi-approved follow-up from TEST-v1.8-quicklog.md's
non-blocking note): this exact ~10-line fail-open lookup previously existed
as FOUR independent per-file copies -- `main.py:_stored_language_pref`,
`core/access.py:_resolve_unprompted_language_for`'s own inline read,
`core/reminders.py:_user_language_pref`, `core/quicklog.py:
_stored_language_pref` -- each keeping its own small copy (this codebase's
established convention up to v1.7, e.g. `_today` duplicated across
`core/records.py`/`core/trends.py`). TEST-v1.8-quicklog.md's re-verification
flagged the growing silent-drift risk of a FOURTH copy; extracted here so
every caller shares one implementation instead.

Behavior is byte-identical to all four originals: `user_id`'s stored
`users.language_pref`, defaulting to `"auto"` on a missing row or a DB read
error -- fail-open, a preference lookup must never crash or block ordinary
message handling, a proactive tick, or a reply."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


def stored_language_pref(db: "Database", user_id: str) -> str:
    """`user_id`'s stored `users.language_pref`, defaulting to `"auto"` on
    a missing row or a DB read error."""
    try:
        row = db.get_user(user_id)
    except Exception:
        logger.exception("Failed to read language preference for user %r; defaulting to auto", user_id)
        return "auto"
    return row["language_pref"] if row is not None else "auto"
