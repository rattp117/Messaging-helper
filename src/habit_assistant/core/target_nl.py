"""Full natural-language target-setting (SPEC-v1.1.md §4 R-T12-R-T16, OQ3,
module `targets`): "from now on I want to drink 2.5L a day" / "ต่อไปอยาก
ดื่มน้ำวันละ 2.5 ลิตร" set a habit's daily goal via an LLM intent
classifier, extending `core/query.py`'s v0.8.0 pattern -- structured JSON
+ strict code-side validation, fail-closed to `None` on ANY failure
(transport/HTTP error, unparseable JSON, an off-schema/unconfigured
category, a non-goalable habit, `goal <= 0`, or low confidence).

Two pieces, matching SPEC-v1.1.md §5's split:

1. `looks_like_target_phrasing` -- a cheap, bilingual, substring-based gate
   (R-T13.1). This is a COST optimization only, called by `main.py`'s
   integration wiring *before* spending an LLM call -- it is deliberately
   NOT part of `classify_target_intent`'s own safety contract. A message
   that slips past this gate (or bypasses it entirely, e.g. a direct unit
   test calling `classify_target_intent`) is still fully protected by
   `classify_target_intent`'s own fail-closed validation (R-T14) and by
   the calling side never writing a log for anything the gate itself
   didn't approve running an LLM call for.

2. `classify_target_intent` -- the actual `{habit_id, goal_base_unit}`
   classification + strict validation, mirroring `core/query.py.
   classify_query_intent` almost line for line: same `OllamaClient.
   chat_json` call, same `_validate_intent` fail-closed shape, same
   never-raises contract. The critical extra validation step beyond
   `core/query.py`'s (SPEC-v1.1.md R-T14/R-T15) is: the classified habit
   must be goal-able (`core/targets.is_goalable`) and the classified goal
   (already unit-normalized to the habit's base unit BY THE MODEL, per
   R-T15) must be a genuine, strictly-positive number.

Only `set` intents exist here (SPEC-v1.1.md §10: clearing/showing a target
via free-form NL is explicitly out of scope) -- there is no
`TargetIntent.action` field because there is only one possible action.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from habit_assistant.core.targets import is_goalable
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import build_target_intent_system_prompt, build_target_intent_user_prompt

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TargetIntent:
    """A validated `{habit_id, goal}` pair -- by the time this is
    constructed (see `_validate_intent`), `habit_id` is a real, goal-able
    configured habit id and `goal_base_unit` is a genuine positive number
    already expressed in that habit's base unit."""

    habit_id: str
    goal_base_unit: float


# ---------------------------------------------------------------------------
# R-T13.1 -- the cheap bilingual gate. Deliberately simple substring/word
# checks, not a full parser: false positives here only cost one extra LLM
# call (still fail-closed downstream); false negatives just mean a genuine
# target phrasing without any of these markers falls back to the
# deterministic `/target`/`ตั้งเป้า` path (R-T13.1's own documented
# residual, SPEC-v1.1.md §9 risk note). The compound phrases named in
# SPEC-v1.1.md R-T13.1 ("aim for ... a day", "want to ... a day", "อยากได้
# วันละ") are not separately encoded -- each already contains one of the
# atomic markers below ("a day" / "วันละ") as a substring, so they're
# covered without a second pattern.
# ---------------------------------------------------------------------------

_EN_MARKER_RE = re.compile(
    r"\bgoal\b|\btarget\b|\bfrom now on\b|\bper day\b|\ba day\b|\beach day\b|\bevery day\b|\bdaily\b",
    re.IGNORECASE,
)
_TH_MARKER_RE = re.compile("เป้า|ต่อไป|ตั้งแต่นี้|วันละ|ต่อวัน")


def looks_like_target_phrasing(text: str) -> bool:
    """R-T13.1: does `text` contain any forward-looking / daily-goal
    marker at all? A pure cost gate -- callers must not treat a `True`
    result as "this IS a target change" (that's `classify_target_intent`'s
    job), and must not treat `False` as proof it ISN'T one (a genuine
    target phrasing lacking every marker is still reachable via the
    deterministic `/target` path, by design)."""
    return bool(_EN_MARKER_RE.search(text) or _TH_MARKER_RE.search(text))


def build_target_intent_schema(category_enum: list[str]) -> dict:
    """The target-intent JSON schema, generated from the live registry's
    `category_enum()` (habit ids + "unknown", same convention
    `build_extraction_schema`/`build_query_intent_schema` use). `goal` is
    typed `number | null` (null only valid alongside category "unknown")
    -- `_validate_intent` is what actually enforces `goal > 0` and
    goal-ability; the schema itself stays permissive, matching how
    `build_extraction_schema` leaves business-rule validation to
    `core/parser.py._validate` rather than encoding it in JSON Schema."""
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(category_enum)},
            "goal": {"type": ["number", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["category", "goal", "confidence"],
    }


def _validate_intent(data: object, registry: "HabitRegistry", config: "Config") -> TargetIntent | None:
    """Strict validation (R-T14): `category` must be a real configured
    habit id (never "unknown" -- the model's own "not a goal change"
    signal, rejected here like any other unrecognized value) AND
    goal-able (`is_goalable`) -- a target can never be classified onto a
    text/boolean habit. `goal` must coerce to a genuine number `> 0`
    (R-T15's post-normalization validation). `confidence` must meet
    `config.ollama.confidence_threshold` (the same knob `core/parser.py`
    uses, default 0.55) -- missing/garbled confidence defaults to 0.0,
    which fails the threshold and so fails closed, never silently
    treated as "confident". Any mismatch, including a malformed/non-dict
    payload, returns None."""
    if not isinstance(data, dict):
        return None
    habit_id = data.get("category")
    if habit_id not in registry.ids():  # excludes "unknown" -- registry.ids() doesn't include it
        return None
    habit = registry.get(habit_id)
    if habit is None or not is_goalable(habit):
        return None

    goal_raw = data.get("goal")
    try:
        goal = float(goal_raw)
    except (TypeError, ValueError):
        return None
    if goal <= 0:
        return None

    confidence_raw = data.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < config.ollama.confidence_threshold:
        return None

    return TargetIntent(habit_id=habit_id, goal_base_unit=goal)


async def classify_target_intent(
    text: str, llm: OllamaClient, registry: "HabitRegistry", config: "Config"
) -> TargetIntent | None:
    """Classify `text` into a validated `TargetIntent`, or `None` on any
    failure whatsoever (transport/HTTP error, unparseable JSON, an
    off-schema/unrecognized category, a non-goalable habit, `goal <= 0`,
    or below-threshold confidence) -- never raises, matching `core/query.py.
    classify_query_intent`'s and `core/parser.py.parse_message`'s
    fail-closed contract. `None` is the caller's (`main.py`'s) signal to
    fall through to the normal log-parsing path unchanged (R-T13.4)."""
    system_prompt = build_target_intent_system_prompt(registry)
    user_prompt = build_target_intent_user_prompt(text)
    schema = build_target_intent_schema(registry.category_enum())
    valid_categories = set(registry.category_enum())

    try:
        raw_json = await llm.chat_json(system_prompt, user_prompt, schema, valid_categories)
        if raw_json is None:
            return None
        data = json.loads(raw_json)
        return _validate_intent(data, registry, config)
    except Exception:
        logger.exception("Target intent classification failed; failing closed to no target change")
        return None
