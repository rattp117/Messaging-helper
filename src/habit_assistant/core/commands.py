"""Command-dispatch seam (ROADMAP.md v0.5.0 "Command Layer & Edit/Undo").

Runs BEFORE the LLM parser. Matches a small, conservative set of explicit
bilingual commands -- a leading `/command` and explicit undo/delete/edit
phrases -- and returns a structured `Command` describing the action to
take. Anything that doesn't match returns `None`: the caller falls through
to the normal `parse_message` LLM path unchanged (AC5.5) -- this module
never mutates or misclassifies a message it doesn't recognize.

Pure functions only, no channel import, no DB import, no LLM call for the
patterns matched here -- mirrors core/parser.py's "no channel imports"
rule and keeps the seam callable from main.py, tests, and later versions
(v0.8 queries, v0.9 snooze) alike. Conservative by design: every pattern
below is anchored to the *whole* stripped message (not a substring match),
because a false positive here would silently swallow a real habit log --
the worst failure mode for this router (ROADMAP.md's own risk note).

v0.7.0 (ROADMAP.md "Multi-Habit Extensibility", SPEC-v0.7.md §4 R14, module
M1): `dispatch`'s edit-value parsing is now driven by the live
`HabitRegistry` instead of hardcoded water/stretch units -- any configured
habit's `unit`/`unit_aliases` can be an edit target, not just water/stretch.
Ambiguous units (two habits sharing the same unit token) resolve first-match
in registry order (SPEC-v0.7.md §9 risk 6).

v0.8.0 (ROADMAP.md "Natural-Language Queries", AC8.1-AC8.5): a third,
LLM-free `"query"` kind, detected purely by anchored bilingual interrogative
patterns -- "how much/many", "did I"/"have I", Thai "กี่"/"เท่าไหร่"/
"เท่าไร"/"ไหม"/"หรือยัง", or a trailing "?"/"？". This module still never
calls the LLM itself (the actual `{habit_id, metric, timeframe}`
classification is `core/query.py`'s job, which `main.py` invokes only after
`dispatch` has flagged the message as query-shaped) -- keeping the
conservative "no false positives on a real log" contract for THIS layer:
none of the anchors above can appear in a plain log like "500ml" or "10 min
stretch" (see `tests/test_commands.py`'s adversarial corpus, unchanged and
still green). Checked after undo/edit and after snooze, so an edit-trigger
phrase that fails to parse as NUMBER [+ UNIT] still falls through to the
parser exactly as it did pre-v0.8 (it never reaches the query check) --
only a message that matched *neither* undo nor an edit trigger gets a
chance to match query.

v0.9.0 (ROADMAP.md "Adaptive Reminders, Snooze & Quiet Hours", AC9.3): a
fourth, LLM-free `"snooze"` kind -- an explicit, conservative bilingual
trigger ("snooze"/"snooze 30"/"/snooze 30", "เลื่อน"/"เลื่อนก่อน"/"เลื่อน 30
นาที") optionally carrying an explicit minute count in `Command.minutes`
(`None` means "use the configured default", `core/reminders.py`/`main.py`'s
job, not this module's -- this module never reads `Config.snooze`). Checked
*between* edit and query (SPEC-v0.7.md's own routing brief: "undo/edit ->
snooze -> query -> extractor") because "snooze" and "เลื่อน" don't overlap
either the undo/edit triggers or any query anchor, so ordering relative to
undo/edit doesn't matter in practice -- it's placed there to match the
brief's exact stated precedence. Resolving *which* habit to snooze is not
this module's job either (it has no DB/registry-state access beyond the
static `registry` argument) -- `main.py` resolves that from
`core/reminders.ReminderState.last_habit_id` at dispatch time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from habit_assistant.core.habits import HabitRegistry

CommandKind = Literal["undo", "edit", "query", "snooze"]


@dataclass(slots=True)
class Command:
    kind: CommandKind
    category: str | None = None  # a configured habit id -- only set for "edit"
    value_num: float | None = None  # new value -- only set for "edit"
    minutes: int | None = None  # explicit snooze minutes -- only set for "snooze"; None = use the configured default


# ---------------------------------------------------------------------------
# undo / delete last entry -- English "undo"/"delete", Thai "ยกเลิก"/"ลบ",
# and the literal "/undo" / "/delete" slash-commands.
# ---------------------------------------------------------------------------

_UNDO_PATTERNS = [
    re.compile(r"^/(undo|delete)$", re.IGNORECASE),
    re.compile(r"^(undo|delete)(\s+(the\s+)?(last|that))?(\s+(entry|log|message))?$", re.IGNORECASE),
    re.compile(r"^(ยกเลิก|ลบ)(อันล่าสุด|ล่าสุด|อันนั้น)?$"),
]

# ---------------------------------------------------------------------------
# edit-value -- an explicit trigger phrase followed by a new value. The
# trigger must lead the message; whatever follows must parse cleanly as
# NUMBER [+ UNIT] or the whole message is rejected (falls through to the
# parser) rather than guessed at.
# ---------------------------------------------------------------------------

_EDIT_TRIGGER = re.compile(
    r"^(?:/edit\s+|make that\s+|change (?:it|that)\s+to\s+|edit (?:it|that|last)\s+to\s+|"
    r"แก้(?:ไข)?(?:ล่าสุด)?เป็น\s*)"
    r"(?P<value>.+)$",
    re.IGNORECASE,
)

# NUMBER, optionally followed directly (or space-separated) by a unit token
# (any run of non-whitespace characters -- Thai and Latin unit strings both
# come through as a single such run, e.g. "ml", "มล.", "min", "glass").
_VALUE_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>\S+)?\s*$")

# ---------------------------------------------------------------------------
# snooze -- ROADMAP.md v0.9.0 (AC9.3). English "snooze"/"snooze 30"/
# "/snooze 30" (an optional trailing minute count, with or without a
# "min(s)"/"minutes" unit word); Thai "เลื่อน"/"เลื่อนก่อน" (bare postpone)
# or "เลื่อน 30 นาที" (an explicit minute count). Anchored to the whole
# stripped message, same conservative "explicit trigger only" philosophy
# as undo/edit -- a message that merely mentions "snooze"/"เลื่อน" mid-
# sentence must not be swallowed (verified against the adversarial corpus
# in tests/test_commands.py).
# ---------------------------------------------------------------------------

_SNOOZE_EN_RE = re.compile(
    r"^/?snooze(?:\s+(?:for\s+)?(?P<minutes>\d+)\s*(?:min(?:ute)?s?)?)?$", re.IGNORECASE
)
_SNOOZE_TH_RE = re.compile(r"^เลื่อน(?:ก่อน)?(?:\s*(?P<minutes>\d+)\s*นาที)?$")


def _match_snooze(stripped: str) -> tuple[bool, int | None]:
    """Returns `(matched, minutes)`. `minutes` is the explicit count parsed
    out of the phrase (e.g. "snooze 30" -> 30), or `None` when the phrase
    carried no number (e.g. bare "snooze"/"เลื่อนก่อน") -- the caller falls
    back to `Config.snooze.default_minutes` for `None`."""
    for pattern in (_SNOOZE_EN_RE, _SNOOZE_TH_RE):
        match = pattern.match(stripped)
        if match is not None:
            minutes_str = match.group("minutes")
            return True, (int(minutes_str) if minutes_str else None)
    return False, None


# ---------------------------------------------------------------------------
# query intent -- ROADMAP.md v0.8.0 (AC8.1-AC8.5). Anchored, conservative
# interrogative markers only: none of these substrings/endings can occur in
# a normal habit log (verified against the full adversarial corpus in
# tests/test_commands.py). The actual {habit_id, metric, timeframe}
# classification happens in core/query.py via the LLM -- this function only
# decides "does this look like a question about past data at all".
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = [
    re.compile(r"\bhow\s+(much|many)\b", re.IGNORECASE),
    re.compile(r"\b(did|have|has)\s+i\b", re.IGNORECASE),
    re.compile("กี่"),
    re.compile("เท่าไหร่|เท่าไร"),
    re.compile("ไหม"),
    re.compile("หรือยัง"),
]
_TRAILING_QUESTION_MARK_RE = re.compile(r"[?？]\s*$")


def _match_query(stripped: str) -> bool:
    if _TRAILING_QUESTION_MARK_RE.search(stripped):
        return True
    return any(pattern.search(stripped) for pattern in _QUERY_PATTERNS)


def _match_undo(stripped: str) -> bool:
    return any(pattern.match(stripped) for pattern in _UNDO_PATTERNS)


def _build_unit_lookup(registry: "HabitRegistry") -> dict[str, tuple[str, float]]:
    """Map a lowercased unit token -> (habit_id, multiplier), built from
    every numeric/duration habit's own `unit` (multiplier 1) and
    `unit_aliases` (each alias's configured multiplier). Iterated in
    registry order with `setdefault`, so an earlier habit's token claims
    the slot over a later habit's identical token (first-match-wins,
    SPEC-v0.7.md §9 risk 6). `str.lower()` is a no-op on Thai text, so this
    single pass handles both scripts uniformly."""
    lookup: dict[str, tuple[str, float]] = {}
    for habit in registry:
        if habit.type not in ("numeric", "duration"):
            continue
        if habit.unit_en:
            lookup.setdefault(habit.unit_en.strip().lower(), (habit.id, 1.0))
        if habit.unit_th:
            lookup.setdefault(habit.unit_th.strip().lower(), (habit.id, 1.0))
        for alias, multiplier in habit.unit_aliases.items():
            lookup.setdefault(alias.strip().lower(), (habit.id, float(multiplier)))
    return lookup


def _resolve_unit(lookup: dict[str, tuple[str, float]], unit_lower: str) -> tuple[str, float] | None:
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


def _default_numeric_habit(registry: "HabitRegistry") -> str | None:
    """A bare number with no unit at all defaults to the first
    numeric/duration habit in registry order (generalizes v0.6.0's
    hardcoded "no unit -> water" default; water is first in the shipped
    registry order, so this reproduces that behavior exactly for the
    default config)."""
    for habit in registry:
        if habit.type in ("numeric", "duration"):
            return habit.id
    return None


def _parse_edit_value(value_str: str, registry: "HabitRegistry") -> tuple[str, float] | None:
    """Parse the text after an edit trigger into (habit_id, new_value).
    Returns None if it doesn't cleanly parse as a positive NUMBER [+ UNIT]
    resolvable to a configured habit -- the caller treats that as "not
    actually a command" (AC5.5's conservatism applies to edit targets
    too)."""
    match = _VALUE_RE.match(value_str.strip())
    if not match:
        return None
    num = float(match.group("num"))
    if num <= 0:
        return None
    unit_raw = match.group("unit")

    if unit_raw is None:
        habit_id = _default_numeric_habit(registry)
        if habit_id is None:
            return None
        return habit_id, num

    resolved = _resolve_unit(_build_unit_lookup(registry), unit_raw.lower())
    if resolved is None:
        return None
    habit_id, multiplier = resolved
    return habit_id, num * multiplier


def dispatch(text: str, registry: "HabitRegistry") -> Command | None:
    """Classify `text` as an explicit command, or return None to fall
    through to the LLM parser (AC5.5: normal habit messages like "500ml"
    or "ดื่มน้ำ 2 แก้ว" must route unchanged -- zero false positives).

    SPEC-v0.7.md §4 R14 / AC12: edit values resolve to a habit id via
    `registry` (its configured `unit`/`unit_aliases`), not a hardcoded
    water/stretch check.

    ROADMAP.md v0.9.0: checked in order undo -> edit -> snooze -> query ->
    (fall through to the parser), matching this version's own required
    routing ("undo/edit -> snooze -> query -> extractor"). An edit-trigger
    phrase whose tail doesn't parse as NUMBER [+ UNIT] returns None
    immediately (pre-v0.8 behavior, unchanged) rather than also being
    offered to the snooze/query matchers -- it already committed to "edit"
    shape, not a snooze or a question."""
    stripped = text.strip()
    if not stripped:
        return None

    if _match_undo(stripped):
        return Command(kind="undo")

    trigger_match = _EDIT_TRIGGER.match(stripped)
    if trigger_match is not None:
        parsed = _parse_edit_value(trigger_match.group("value"), registry)
        if parsed is None:
            return None
        category, value_num = parsed
        return Command(kind="edit", category=category, value_num=value_num)

    snoozed, minutes = _match_snooze(stripped)
    if snoozed:
        return Command(kind="snooze", minutes=minutes)

    if _match_query(stripped):
        return Command(kind="query")

    return None
