"""Per-user `HabitRegistry` cache (SPEC-v1.7.md §4 R-G2, module split's own
"shared surface -- the core"): every registry consumer in this app --
`handle_inbound_message`, the LLM reparse path, and all six scheduler
fan-outs (reminders/check-ins/nudge/weekly-review/daily-summary/dashboard
rollover) -- now needs the ACTING user's own per-user registry
(`HabitRegistry.for_user`, R-G1) on every message and every fan-out tick.
Rebuilding it from `db.list_user_habits(...)` on every single call would be
wasteful (a DB round-trip per message); this ONE process-global cache,
constructed once at startup and threaded through `main.py`, is what makes
that cheap.

No channel import (mirrors `core/reminders.py`/`core/query.py`'s own "no
channel imports" seam) -- this module only knows about `Config`/`Database`
in, `HabitRegistry` out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from habit_assistant.core.habits import HabitRegistry

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


class RegistryProvider:
    """R-G2: `.for_user(user_id)` is CACHED (a DB read + registry build
    only happens once per user, until that user's entry is invalidated);
    `.invalidate(user_id)` drops exactly one user's cached entry -- called
    by `core/habitdef.py:execute_addhabit`/`execute_delhabit` after a
    successful create/archive/delete (R-C1/R-C2), so the VERY NEXT message
    from that user (and the next scheduler fan-out tick that reaches them)
    rebuilds their registry with no process restart (AC-3). Invalidation
    is strictly per-user -- one user's change never touches another's
    already-cached entry, and never rebuilds anyone else's.

    Cache starts empty on boot (built lazily, on first use per user --
    "rebuilt lazily" per R-G2's own wording, not eagerly warmed at
    startup). Fail-open (R-G2's own explicit contract): if `for_user`
    itself raises while building a user's registry (a DB hiccup, a
    corrupt `user_habits` row), this falls back to the base
    `HabitRegistry.from_config(config)` -- logged, never raised to the
    caller, and NOT cached (so the next call retries the real per-user
    build rather than permanently pinning a user to the base-only
    fallback for the rest of the process's life)."""

    def __init__(self, config: "Config", db: "Database") -> None:
        self._config = config
        self._db = db
        self._cache: dict[str, HabitRegistry] = {}

    def for_user(self, user_id: str) -> HabitRegistry:
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached

        try:
            registry = HabitRegistry.for_user(self._config, self._db, user_id)
        except Exception:
            logger.exception(
                "Building the per-user registry failed for %s (fail-open); falling back to the base registry",
                user_id,
            )
            return HabitRegistry.from_config(self._config)

        self._cache[user_id] = registry
        return registry

    def invalidate(self, user_id: str) -> None:
        self._cache.pop(user_id, None)
