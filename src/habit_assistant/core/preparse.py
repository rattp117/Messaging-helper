"""Deterministic pre-parser (SPEC-v1.5.md §4 R-L1, module `preparse`,
LLM-free): the zero-Ollama fast path for unambiguous NUMBER+UNIT logs
("500ml", "2 แก้ว", "10 min").

`deterministic_parse` reuses `core/units.py`'s exact `VALUE_RE`/
`build_unit_lookup`/`resolve_unit` (extracted from `core/commands.py`,
R-L5) -- the SAME whole-message-anchored regex and registry-driven unit
resolution that already guards `commands._parse_edit_value`'s "edit
value" parsing, so this module introduces no new unit-matching logic to
keep byte-identical (AC-2's regression guard covers both call sites).

Zero-false-positive by construction (AC-15): `VALUE_RE` is anchored to
the *whole* stripped message (`^...$`), so anything with extra words,
punctuation, or a leading non-digit character (a bare number, a
label-first log like "น้ำ 500", a sentence, a question, a command, a
target phrasing like "from now on 2.5L a day") simply fails to match and
this function returns `None` -- the caller (`main.py`'s
`handle_inbound_message`, wired by integration, NOT this module) then
falls through to the existing NL-target gate / deferral / `parse_message`
path unchanged (R-L2).

Deliberately narrower than `_parse_edit_value` in one respect: a bare
number with NO unit (`unit_raw is None`) returns `None` here rather than
defaulting to the first numeric/duration habit -- SPEC-v1.5.md §9's own
recorded decision ("pre-parser scope = number+unit only ... a bare-number
win is deliberately forgone for safety"). A missing/unregistered/
ambiguous-to-no-single-habit unit likewise returns `None` (`resolve_unit`
already returns at most one `(habit_id, multiplier)` per token --
`build_unit_lookup` EXCLUDES a token claimed by two different habits from
the lookup entirely, integration punch list #3 / TEST-v1.5-preparse.md
Finding 1 -- so a genuinely ambiguous unit falls through here too,
instead of silently picking whichever habit was registered first).

No channel/DB/LLM import here (mirrors `core/units.py` and every other
pure-logic module in this codebase) -- this module only knows about text
and a `HabitRegistry` in, an `ExtractionResult | None` out. It does not
call, import, or require an `OllamaClient` at all, which is what makes it
usable while Ollama is down (R-L2/AC-16 -- the actual Ollama-down WIRING
into `handle_inbound_message` is integration's job, not this module's)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from habit_assistant.core.units import VALUE_RE, build_unit_lookup, resolve_unit
from habit_assistant.llm.ollama_client import ExtractionResult

if TYPE_CHECKING:
    from habit_assistant.core.habits import HabitRegistry

# Deterministic parses carry full confidence -- nothing downstream reads
# `ExtractionResult.confidence` for a numeric/duration confirmation (only
# `core/parser.py`'s own LLM-response threshold check does), so this value
# is never observed by the byte-identical-confirmation guarantee (AC-14);
# it's set to 1.0 for semantic honesty (this result IS certain).
_CONFIDENCE = 1.0


def deterministic_parse(text: str, registry: "HabitRegistry") -> ExtractionResult | None:
    """Return an `ExtractionResult` only when `text.strip()` is, in its
    entirety, a positive `NUMBER` followed by a `UNIT` token that
    `core/units.py` resolves to a single configured numeric/duration
    habit -- e.g. "500ml", "2 แก้ว", "10 min". Anything else -> `None`."""
    match = VALUE_RE.match(text.strip())
    if not match:
        return None

    num = float(match.group("num"))
    if num <= 0:
        return None

    unit_raw = match.group("unit")
    if unit_raw is None:
        return None  # bare number -- always falls through to the LLM (SPEC-v1.5.md §9)

    resolved = resolve_unit(build_unit_lookup(registry), unit_raw.lower())
    if resolved is None:
        return None

    habit_id, multiplier = resolved
    return ExtractionResult(habit_id, num * multiplier, _CONFIDENCE)
