"""Registry-driven unit-resolution machinery (SPEC-v1.5.md §4 R-L5).

Extracted from `core/commands.py` (where it powered edit-value and
target-value parsing since ROADMAP.md v0.7.0) so `core/preparse.py`
(v1.5.0's deterministic pre-parser, module `preparse`) can reuse the
EXACT same unit lookup/resolution logic instead of copy-pasting it --
one place a habit's configured `unit`/`unit_aliases` gets turned into a
`(habit_id, multiplier)` resolution, not two. `core/commands.py` now
imports these three names rather than defining its own copies (byte-
identical behavior -- the existing command tests are the regression
guard, AC-2).

No channel/DB import (mirrors every other pure-logic module in this
codebase) -- this module only knows about a `HabitRegistry`."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from habit_assistant.core.habits import HabitRegistry

# NUMBER, optionally followed directly (or space-separated) by a unit token
# (any run of non-whitespace characters -- Thai and Latin unit strings both
# come through as a single such run, e.g. "ml", "มล.", "min", "glass").
VALUE_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>\S+)?\s*$")


def build_unit_lookup(registry: "HabitRegistry") -> dict[str, tuple[str, float]]:
    """Map a lowercased unit token -> (habit_id, multiplier), built from
    every numeric/duration habit's own `unit` (multiplier 1) and
    `unit_aliases` (each alias's configured multiplier). `str.lower()` is a
    no-op on Thai text, so this single pass handles both scripts uniformly.

    SPEC-v1.5.md integration punch list #3 (TEST-v1.5-preparse.md Finding
    1): a token claimed by two DIFFERENT habit ids is a genuine
    configuration ambiguity, not a "first one wins" ordering accident --
    EXCLUDED from the returned lookup entirely, so `resolve_unit` returns
    `None` for it regardless of registry order. This makes
    `core/preparse.py:deterministic_parse` fall through to the LLM path
    for a colliding token (R-L's own "a wrong value is worse than a
    missed parse" principle, extended here to "a wrong HABIT is worse
    still") instead of silently misattributing a log to whichever habit
    happened to be registered first. A token re-registered by the SAME
    habit id (e.g. its `unit`/`unit_th` and an alias happen to coincide)
    is NOT a collision -- that habit's own first value for the token is
    kept, mirroring the old first-match-wins behavior for the
    non-ambiguous case. The shipped default registry (water/stretch/
    diary) has no colliding tokens, so this is inert against production
    `config.toml` today (SPEC-v0.7.md §9 risk 6's original "first-match-
    wins" note is superseded by this rule for genuine cross-habit
    collisions)."""
    lookup: dict[str, tuple[str, float]] = {}
    claimed_by: dict[str, str] = {}
    collided: set[str] = set()

    def _register(token: str, habit_id: str, multiplier: float) -> None:
        if token in collided:
            return
        owner = claimed_by.get(token)
        if owner is None:
            lookup[token] = (habit_id, multiplier)
            claimed_by[token] = habit_id
        elif owner != habit_id:
            collided.add(token)
            del lookup[token]
            del claimed_by[token]
        # else: same habit re-claiming its own token -- first value stands.

    for habit in registry:
        if habit.type not in ("numeric", "duration"):
            continue
        if habit.unit_en:
            _register(habit.unit_en.strip().lower(), habit.id, 1.0)
        if habit.unit_th:
            _register(habit.unit_th.strip().lower(), habit.id, 1.0)
        for alias, multiplier in habit.unit_aliases.items():
            _register(alias.strip().lower(), habit.id, float(multiplier))
    return lookup


def resolve_unit(lookup: dict[str, tuple[str, float]], unit_lower: str) -> tuple[str, float] | None:
    """Exact match first; then a trailing "." stripped (some configured
    Thai units, e.g. "มล.", already include the dot in the exact-match
    lookup, but a model/user-typed variant might drop or add one); then a
    simple trailing-"s" singularization (covers "mins" -> "min",
    "bottles" -> "bottle"). Irregular plurals (e.g. "glasses") are not
    guessed at -- configure them as an explicit `unit_aliases` entry if
    needed (see IMPL-v0.7-M1.md Known limitations)."""
    if unit_lower in lookup:
        return lookup[unit_lower]
    if unit_lower.endswith(".") and unit_lower[:-1] in lookup:
        return lookup[unit_lower[:-1]]
    if len(unit_lower) > 1 and unit_lower.endswith("s") and unit_lower[:-1] in lookup:
        return lookup[unit_lower[:-1]]
    return None
