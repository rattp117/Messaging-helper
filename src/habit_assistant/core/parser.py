"""Message -> structured entry. Calls the LLM, then validates its output
strictly. Fails closed to ExtractionResult.unknown() on any failure —
never raises, so a bad LLM response can never crash the inbound loop.

No channel imports here (SPEC.md §8) — this module only knows about text in,
ExtractionResult out.

v0.7.0 (ROADMAP.md "Multi-Habit Extensibility", SPEC-v0.7.md §4 R7/R8,
module M1): `parse_message`/`_validate` are now generated from a live
`HabitRegistry` instead of the fixed water/stretch/diary shape -- the
schema/prompt are built per call from the registry (`build_extraction_schema`,
`llm/prompts.build_extraction_system_prompt`), and `_validate` dispatches
per-type (numeric/duration/text/boolean) instead of per hardcoded category.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from habit_assistant.llm.ollama_client import ExtractionResult, OllamaClient, build_extraction_schema
from habit_assistant.llm.prompts import build_extraction_system_prompt, build_extraction_user_prompt

if TYPE_CHECKING:
    from habit_assistant.core.habits import HabitRegistry

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.55

_BOOL_TRUTHY = {"true", "1", "done", "yes", "ครบ", "แล้ว"}
_BOOL_FALSY = {"false", "0", "no", "ยัง"}


async def parse_message(
    text: str,
    llm: OllamaClient,
    registry: "HabitRegistry",
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> ExtractionResult:
    """Parse inbound text into a structured ExtractionResult via the LLM,
    using a schema/prompt generated from `registry` (SPEC-v0.7.md §4 R7)."""
    system_prompt = build_extraction_system_prompt(registry)
    user_prompt = build_extraction_user_prompt(text)
    schema = build_extraction_schema(registry.category_enum())
    valid_categories = set(registry.category_enum())

    try:
        raw_json = await llm.chat_json(system_prompt, user_prompt, schema, valid_categories)
        if raw_json is None:
            return ExtractionResult.unknown()
        data = json.loads(raw_json)
        return _validate(data, registry, confidence_threshold)
    except Exception:
        logger.exception("Parser failed to extract structured data; failing closed to unknown")
        return ExtractionResult.unknown()


def _coerce_number(value: object) -> float | None:
    """numeric/duration coercion (SPEC-v0.7.md §4 R8): accept a genuine
    number or a numeric-looking string ("500", "7.5"); reject anything
    else. `bool` is a `int` subclass in Python, so it's explicitly
    excluded -- a stray `true`/`false` must not be misread as 1/0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_boolean(value: object) -> bool | None:
    """boolean coercion (SPEC-v0.7.md §4 R8): a genuine `bool`; a numeric
    1/0; or one of the bilingual truthy/falsy string forms. Anything else
    (including an un-coercible string) -> None, which the caller treats as
    `unknown`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUTHY:
            return True
        if normalized in _BOOL_FALSY:
            return False
    return None


def _validate(
    data: dict, registry: "HabitRegistry", confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
) -> ExtractionResult:
    category = data.get("category")
    if category not in registry.ids():
        return ExtractionResult.unknown()
    habit = registry.get(category)
    assert habit is not None  # category just confirmed to be a registered id

    # Track whether `confidence` was itself a genuine, parseable number, as
    # opposed to missing/garbled. AC2.3/AC7 only demotes a *valid*
    # extraction whose confidence is below threshold to `unknown` -- a
    # missing/garbled confidence field is a separate, pre-existing
    # malformed-response case that already defaults to 0.0 without being
    # reclassified (v0.1.0 behavior, preserved here).
    confidence_raw = data.get("confidence")
    confidence_is_genuine = confidence_raw is not None
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
        confidence_is_genuine = False

    value = data.get("value")

    if habit.type in ("numeric", "duration"):
        number = _coerce_number(value)
        if number is None or number <= 0:
            return ExtractionResult.unknown()
        result = ExtractionResult(category, number, confidence)

    elif habit.type == "text":
        if value is None:
            return ExtractionResult.unknown()
        text_value = value if isinstance(value, str) else str(value)
        if not text_value.strip():
            return ExtractionResult.unknown()
        result = ExtractionResult(category, text_value, confidence)

    else:  # boolean
        flag = _coerce_boolean(value)
        if flag is None:
            return ExtractionResult.unknown()
        result = ExtractionResult(category, flag, confidence)

    # AC2.3/AC7: a schema-valid, business-valid extraction whose confidence
    # is below the configured threshold is treated as unknown (clarify, no
    # row). Only demotes a *genuinely numeric* confidence, matching v0.2's
    # preserved behavior.
    if confidence_is_genuine and result.confidence < confidence_threshold:
        return ExtractionResult.unknown()
    return result
