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

v1.1.0 (SPEC-v1.1.md §4 R-T7-R-T9, module `targets`): a fifth, LLM-free
`"target"` kind -- deterministic per-habit daily-goal set/show/clear,
recognized by (a) the slash form `/target [<habit> [<value>|default]]`
and (b) conservative anchored bilingual NL "set" triggers ("set <habit>
goal to <value>", "change <habit>('s) goal to <value>", Thai "ตั้งเป้า
<habit> <value>" / "เป้า <habit> <value>"). Checked *after* snooze and
*before* query (R-T7's stated precedence: "undo/edit -> snooze -> target
-> query -> extractor"). Unlike edit's "garbled tail -> None" contract, a
`/target ...`/NL trigger whose value tail doesn't cleanly parse as
NUMBER [+ UNIT] still returns a `Command(kind="target",
target_action="usage")` -- never a silent `None` fall-through -- so the
caller (`core/targets_command.execute_target`) can reply with concrete
usage help instead of the message being misfiled as an unrecognized log
(R-T7's own explicit carve-out). This module never resolves whether a
named habit id actually exists/is goal-able -- an unresolved habit token
is carried through verbatim as `Command.category` (lowercased) so
`execute_target`'s own registry lookup is what reports
`target_invalid_habit`/`target_not_goalable` (R-T10); this module only
recognizes the *shape* of a target command and, for a value tail that DID
parse, converts any recognized unit alias to the habit's base unit
(mirrors `_parse_edit_value`'s registry-driven unit resolution). The full
free-form NL target-setting path (R-T13-R-T16, "from now on I want to
drink 2.5L a day") is NOT here -- that is `core/target_nl.py`'s
LLM-classification job, gated and routed by `main.py`, not this
anchored/deterministic layer.

v1.1.0 (SPEC-v1.1.md §4 R-D1, module `discoverability`, sequential
follow-on landed after the v1.1 shared surface + integration -- SPEC-v1.1.md
§11's own note that this module edits the same file the `targets` module
already touched, hence NOT parallel-safe with it): two more LLM-free kinds,
`"help"` (`^/help$`, `^ช่วยเหลือ$`, `^วิธีใช้$`) and `"habits"`
(`^/habits$`, `^นิสัย$`) -- each anchored to the *whole* stripped message,
same conservatism as every pattern above (R-C5: a plain log can never
accidentally equal one of these five exact strings). Checked alongside the
other anchored commands, after target and before query (R-D1's own stated
precedence: "... -> target -> help/habits -> query -> extractor") -- neither
kind carries any payload (`Command(kind="help")`/`Command(kind="habits")`
is the whole of it); the actual reply text is `core/discoverability.py`'s
job, dispatched by `main.py`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from habit_assistant.core.habits import HabitRegistry

CommandKind = Literal["undo", "edit", "query", "snooze", "target", "help", "habits"]


@dataclass(slots=True)
class Command:
    kind: CommandKind
    category: str | None = None  # a configured habit id -- "edit", and "target" (set/show/clear)
    value_num: float | None = None  # new value -- "edit", and "target" (the new goal, in base unit) for "set"
    minutes: int | None = None  # explicit snooze minutes -- only set for "snooze"; None = use the configured default
    # SPEC-v1.1.md §5: which target operation -- only set for kind="target".
    target_action: Literal["set", "clear", "show", "show_all", "usage"] | None = None


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
# target set/show/clear -- SPEC-v1.1.md §4 R-T7-R-T9 (module `targets`).
# Slash form `/target [<habit> [<value>|default]]`, plus conservative
# anchored bilingual NL "set" triggers. Both forms resolve a habit TOKEN
# (an id or a configured label) via `_build_habit_token_lookup`, and a
# value tail via `_parse_target_value`, which reuses the same
# `_build_unit_lookup`/`_resolve_unit` machinery `_parse_edit_value`
# already uses for edit-value unit resolution (R-T9).
# ---------------------------------------------------------------------------

_TARGET_SLASH_RE = re.compile(r"^/target(?:\s+(?P<rest>\S.*))?$", re.IGNORECASE)
_TARGET_CLEAR_WORDS = {"default", "reset", "clear", "ค่าเริ่มต้น"}

# English NL "set"-only triggers (R-T7b). `\S+?` (non-greedy) on the habit
# group so the "change <habit>('s) ..." form correctly splits a possessive
# ("water's" -> habit "water", the "'s" consumed by its own optional
# group) instead of swallowing the apostrophe-s into the habit token.
_TARGET_EN_SET_PATTERNS = [
    re.compile(r"^set\s+(?P<habit>\S+?)\s+(?:goal|target)\s+to\s+(?P<value>.+)$", re.IGNORECASE),
    re.compile(r"^change\s+(?P<habit>\S+?)(?:'s)?\s+(?:goal|target)\s+to\s+(?P<value>.+)$", re.IGNORECASE),
]


def _build_habit_token_lookup(registry: "HabitRegistry") -> dict[str, str]:
    """Maps a lowercased habit id / English label / Thai label -> habit id,
    so a target command's habit token can be given as the raw id (slash
    form, English NL triggers: "water") or its configured label (Thai NL
    trigger: "น้ำ"). Iterated in registry order with `setdefault` --
    first-match-wins on a shared token, same convention as
    `_build_unit_lookup`. Unlike that lookup, this one is NOT filtered to
    numeric/duration habits -- a target command must also be able to name
    a non-goalable habit (e.g. "/target diary 5") so `execute_target` can
    report `target_not_goalable` rather than a bare `target_invalid_habit`."""
    lookup: dict[str, str] = {}
    for habit in registry:
        lookup.setdefault(habit.id.strip().lower(), habit.id)
        if habit.label_en:
            lookup.setdefault(habit.label_en.strip().lower(), habit.id)
        if habit.label_th:
            lookup.setdefault(habit.label_th.strip().lower(), habit.id)
    return lookup


def _resolve_habit_token(token: str, registry: "HabitRegistry") -> str | None:
    return _build_habit_token_lookup(registry).get(token.strip().lower())


def _resolve_target_category(habit_token: str, registry: "HabitRegistry") -> str:
    """The habit id if `habit_token` resolves, else the raw (lowercased)
    token itself -- letting `execute_target`'s own registry lookup fail
    and report `target_invalid_habit` (R-T10/AC16) rather than this
    module silently rejecting an unrecognized habit name."""
    resolved = _resolve_habit_token(habit_token, registry)
    return resolved if resolved is not None else habit_token.strip().lower()


def _build_target_th_set_pattern(registry: "HabitRegistry") -> re.Pattern[str] | None:
    """Thai "ตั้งเป้า<habit><value>" / "เป้า<habit><value>" (R-T7b), habit
    token built from the LIVE registry's ids/Thai labels rather than a
    generic "any non-digit run" character class. Thai script is normally
    written with no spaces between words, so a generic habit-token class
    risks a false positive on an unrelated sentence that happens to start
    with "เป้า" (e.g. "เป้าหมายของฉันคือ 2000 บาท", a diary-style reflection
    about a personal goal in baht, not a habit target) -- anchoring the
    habit token to only the habits actually configured eliminates that
    false-positive class entirely (a message naming a habit this bot
    doesn't track can't accidentally look like this trigger). Returns None
    if the registry has no matchable Thai/id tokens (defensive; every
    shipped config has at least `water`'s "น้ำ")."""
    tokens: set[str] = set()
    for habit in registry:
        tokens.add(habit.id)
        if habit.label_th:
            tokens.add(habit.label_th)
    escaped = sorted((re.escape(t) for t in tokens if t.strip()), key=len, reverse=True)
    if not escaped:
        return None
    habit_alt = "|".join(escaped)
    return re.compile(rf"^(?:ตั้งเป้า|เป้า)\s*(?P<habit>{habit_alt})\s*(?P<value>\d.*)$")



# Unlike `_VALUE_RE` (edit values, always a physical quantity actually
# logged), a target's proposed value can legitimately be typed as a
# non-positive number ("/target water 0"/"-5", AC15) -- that must still
# reach `execute_target`'s own `target_invalid_value` reply, not silently
# become a `target_usage` reply. So target-value parsing accepts an
# optional leading "-" that `_VALUE_RE` deliberately does not.
_TARGET_VALUE_RE = re.compile(r"^(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>\S+)?\s*$")


def _parse_target_value(habit_token: str, value_str: str, registry: "HabitRegistry") -> tuple[str, float] | None:
    """Parse a target-command value tail into (category, goal_in_base_unit),
    or None if the tail doesn't cleanly parse as NUMBER [+ UNIT], or an
    explicit unit resolves to a habit OTHER than the one named (R-T9's
    usage-error case). `category` is the resolved habit id when the named
    habit token matches a configured habit; otherwise it is the raw
    (lowercased) token, so an unrecognized habit ("/target coffee 2000")
    still produces a "set" command `execute_target` can reject with
    `target_invalid_habit` (AC16) -- this function never treats an
    unresolvable habit token as a parse failure by itself. A non-positive
    number ("/target water 0"/"-5") is likewise passed through unchanged
    (AC15) -- `execute_target`'s own "set" validation is what reports
    `target_invalid_value`, not this layer."""
    match = _TARGET_VALUE_RE.match(value_str.strip())
    if not match:
        return None
    num = float(match.group("num"))
    unit_raw = match.group("unit")
    resolved_habit_id = _resolve_habit_token(habit_token, registry)

    if resolved_habit_id is None:
        # The named habit itself isn't recognized -- a trailing unit token
        # can't be validated against an unknown habit's alias table, so
        # it's ignored; the raw token is what execute_target reports as
        # invalid, regardless of what the unit might otherwise suggest.
        return habit_token.strip().lower(), num

    if unit_raw is None:
        return resolved_habit_id, num

    unit_resolution = _resolve_unit(_build_unit_lookup(registry), unit_raw.lower())
    if unit_resolution is None:
        return None  # unrecognized unit token -> usage (can't tell what's meant)
    unit_habit_id, multiplier = unit_resolution
    if unit_habit_id != resolved_habit_id:
        return None  # unit belongs to a different habit than the one named -> usage (R-T9)
    return resolved_habit_id, num * multiplier


def _build_target_set_or_usage(habit_token: str, value_str: str, registry: "HabitRegistry") -> "Command":
    parsed = _parse_target_value(habit_token, value_str, registry)
    if parsed is None:
        return Command(kind="target", target_action="usage")
    category, value_num = parsed
    return Command(kind="target", category=category, value_num=value_num, target_action="set")


def _match_target_slash(stripped: str, registry: "HabitRegistry") -> "Command | None":
    match = _TARGET_SLASH_RE.match(stripped)
    if match is None:
        return None
    rest = match.group("rest")
    if rest is None:
        return Command(kind="target", target_action="show_all")

    parts = rest.strip().split(None, 1)
    habit_token = parts[0]
    tail = parts[1].strip() if len(parts) > 1 else None

    if tail is None:
        return Command(kind="target", category=_resolve_target_category(habit_token, registry), target_action="show")

    if tail.lower() in _TARGET_CLEAR_WORDS:
        return Command(kind="target", category=_resolve_target_category(habit_token, registry), target_action="clear")

    return _build_target_set_or_usage(habit_token, tail, registry)


def _match_target_nl(stripped: str, registry: "HabitRegistry") -> "Command | None":
    for pattern in _TARGET_EN_SET_PATTERNS:
        match = pattern.match(stripped)
        if match is not None:
            return _build_target_set_or_usage(match.group("habit"), match.group("value"), registry)

    th_pattern = _build_target_th_set_pattern(registry)
    if th_pattern is not None:
        match = th_pattern.match(stripped)
        if match is not None:
            return _build_target_set_or_usage(match.group("habit"), match.group("value"), registry)

    return None


def _match_target(stripped: str, registry: "HabitRegistry") -> "Command | None":
    return _match_target_slash(stripped, registry) or _match_target_nl(stripped, registry)


# ---------------------------------------------------------------------------
# help / habits -- SPEC-v1.1.md §4 R-D1 (module `discoverability`). Anchored
# to the whole stripped message, exactly the five literal strings R-D1
# names -- no partial/prefix matching, so neither can ever fire on a real
# habit log (AC40).
# ---------------------------------------------------------------------------

_HELP_RE = re.compile(r"^(?:/help|ช่วยเหลือ|วิธีใช้)$", re.IGNORECASE)
_HABITS_RE = re.compile(r"^(?:/habits|นิสัย)$", re.IGNORECASE)


def _match_help(stripped: str) -> bool:
    return _HELP_RE.match(stripped) is not None


def _match_habits(stripped: str) -> bool:
    return _HABITS_RE.match(stripped) is not None


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

    ROADMAP.md v0.9.0 / SPEC-v1.1.md §4 R-T7/R-D1: checked in order undo ->
    edit -> snooze -> target -> help -> habits -> query -> (fall through to
    the parser), matching this version's own required routing ("undo/edit ->
    snooze -> target -> help/habits -> query -> extractor"). An edit-trigger
    phrase whose tail doesn't parse as NUMBER [+ UNIT] returns None
    immediately (pre-v0.8 behavior, unchanged) rather than also being
    offered to the snooze/target/help/habits/query matchers -- it already
    committed to "edit" shape, not any of those."""
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

    target_command = _match_target(stripped, registry)
    if target_command is not None:
        return target_command

    if _match_help(stripped):
        return Command(kind="help")

    if _match_habits(stripped):
        return Command(kind="habits")

    if _match_query(stripped):
        return Command(kind="query")

    return None
