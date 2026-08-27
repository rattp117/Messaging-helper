"""Reply-to-reminder value resolution (SPEC-v1.10.md "Never lose a log"
§4 R13/R14, module M2, functional 3): given the free text of a Telegram
*reply* to one of the bot's own per-habit reminder messages, and the
`Habit` that reminder was mapped to (`core/reminders.py:ReminderState.
habit_for_reply`, shared surface), decide whether the text is a bare
value the bot can attribute to that habit with ZERO LLM involvement --
deliberately conservative (R14): a bare positive number for a numeric/
duration habit, or an affirmative token for a boolean habit; everything
else is `None`, which `core/routing.py`'s integration caller (R13) treats
as "fall through to the normal logging path" (preparse, then the LLM),
never a wrong attribution.

No channel/DB/LLM import here (mirrors `core/units.py`/`core/preparse.py`'s
own "pure text in, typed value out" seam) -- this module only knows about
a string and a `Habit`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from habit_assistant.core.units import VALUE_RE

if TYPE_CHECKING:
    from habit_assistant.core.habits import Habit

# Mirrors `core/parser.py:_BOOL_TRUTHY` (the LLM-response boolean-coercion
# vocabulary) -- no shared cross-module constant exists for this yet
# (that set is module-private to `parser.py`), so this module defines its
# own small, self-contained copy rather than importing a private name
# across files, the same "no such normalizer exists elsewhere ... define
# a small self-contained one" posture `core/backfill.py`'s own Thai/
# full-width-numeral section documents for an analogous situation. Kept
# in sync by inspection (both are short, stable, rarely-touched sets);
# a divergence here only ever makes reply-attribution MORE conservative
# or less, never wrong in a way `parser.py`'s own LLM-path coercion isn't
# already exposed to.
_AFFIRMATIVE_TOKENS = {"true", "1", "done", "yes", "ครบ", "แล้ว"}


def resolve_reply_value(text: str, habit: "Habit") -> float | None:
    """R14: a value ONLY for --

    - a **boolean** habit whose stripped, lowercased text is one of the
      established affirmative tokens (`_AFFIRMATIVE_TOKENS`, which already
      includes the bare digit `"1"`) -> `1.0`.
    - a **numeric/duration** habit whose stripped text is, in its
      entirety, a bare POSITIVE number with NO unit token
      (`core/units.py:VALUE_RE` matches and `unit` is `None`) -> that
      number, taken directly as the habit's own base-unit value (there is
      no unit to convert via a multiplier, since none was given).

    Everything else -> `None`: a **text**-type habit (no sensible bare
    value); a non-affirmative/negative token for a boolean habit (a
    reply like "no"/"ยัง" is left to the normal path rather than guessed
    as `0.0` -- R14's own "everything else -> None", not just the
    affirmative case); a number+unit (resolving to this habit or another
    one -- deliberately NOT resolved here, so a plain "500ml" reply still
    goes through the exact same, already-tested preparse path as a typed
    "500ml" message, AC13); zero or a negative number (VALUE_RE requires
    a leading digit, so a negative number never matches at all; zero is
    rejected explicitly, mirroring `preparse.deterministic_parse`'s own
    `num <= 0 -> None` posture); or any other free text.

    Zero-LLM, deterministic, therefore safe to call while Ollama is DOWN
    (R13's own "works offline" requirement) -- this function makes no
    I/O call of any kind.

    Thai/full-width numerals need no special-case handling: Python's `re`
    module matches `\\d` against any Unicode decimal-digit character
    (Nd category) by default, and `float()` converts them the same way
    -- `VALUE_RE` (no `re.ASCII` flag) and the plain `float(...)` call
    below already accept "๕๐๐"/"５００" exactly like "500", for free."""
    stripped = text.strip()

    if habit.type == "boolean":
        if stripped.lower() in _AFFIRMATIVE_TOKENS:
            return 1.0
        return None

    if habit.type not in ("numeric", "duration"):
        return None  # text habits: no bare value makes sense here

    match = VALUE_RE.match(stripped)
    if match is None or match.group("unit") is not None:
        return None

    num = float(match.group("num"))
    if num <= 0:
        return None
    return num
