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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CommandKind = Literal["undo", "edit"]


@dataclass(slots=True)
class Command:
    kind: CommandKind
    category: str | None = None  # 'water' | 'stretch' -- only set for "edit"
    value_num: float | None = None  # new value -- only set for "edit"


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

_VALUE_RE = re.compile(
    r"^(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>ml|มล\.?|มิลลิลิตร|ลิตร|liters?|litres?|l|"
    r"glass(?:es)?|แก้ว|bottle(?:s)?|ขวด|"
    r"min(?:ute)?s?|นาที)?\.?\s*$",
    re.IGNORECASE,
)

_STRETCH_UNITS = {"min", "mins", "minute", "minutes", "นาที"}
_GLASS_UNITS = {"glass", "glasses", "แก้ว"}
_BOTTLE_UNITS = {"bottle", "bottles", "ขวด"}
_LITER_UNITS = {"l", "liter", "liters", "litre", "litres", "ลิตร"}


def _match_undo(stripped: str) -> bool:
    return any(pattern.match(stripped) for pattern in _UNDO_PATTERNS)


def _parse_edit_value(value_str: str, glass_ml: int, bottle_ml: int) -> tuple[str, float] | None:
    """Parse the text after an edit trigger into (category, new_value).
    Returns None if it doesn't cleanly parse as a positive NUMBER [+ UNIT]
    -- the caller treats that as "not actually a command" (AC5.5's
    conservatism applies to edit targets too)."""
    match = _VALUE_RE.match(value_str.strip())
    if not match:
        return None
    num = float(match.group("num"))
    if num <= 0:
        return None
    unit = (match.group("unit") or "").lower()

    if unit in _STRETCH_UNITS:
        return "stretch", num
    if unit in _GLASS_UNITS:
        return "water", num * glass_ml
    if unit in _BOTTLE_UNITS:
        return "water", num * bottle_ml
    if unit in _LITER_UNITS:
        return "water", num * 1000
    # "ml"/"มล."/no unit at all defaults to water -- matches this router's
    # only specified edit example (ROADMAP.md: "300ml" / "300 มล.").
    return "water", num


def dispatch(text: str, glass_ml: int, bottle_ml: int) -> Command | None:
    """Classify `text` as an explicit command, or return None to fall
    through to the LLM parser (AC5.5: normal habit messages like "500ml"
    or "ดื่มน้ำ 2 แก้ว" must route unchanged -- zero false positives)."""
    stripped = text.strip()
    if not stripped:
        return None

    if _match_undo(stripped):
        return Command(kind="undo")

    trigger_match = _EDIT_TRIGGER.match(stripped)
    if trigger_match is None:
        return None

    parsed = _parse_edit_value(trigger_match.group("value"), glass_ml, bottle_ml)
    if parsed is None:
        return None
    category, value_num = parsed
    return Command(kind="edit", category=category, value_num=value_num)
