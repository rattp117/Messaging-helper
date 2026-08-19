"""Message -> structured entry. Calls the LLM, then validates its output
strictly. Fails closed to ExtractionResult.unknown() on any failure —
never raises, so a bad LLM response can never crash the inbound loop.

No channel imports here (SPEC.md §8) — this module only knows about text in,
ExtractionResult out.
"""

from __future__ import annotations

import json
import logging

from habit_assistant.llm.ollama_client import EXTRACTION_JSON_SCHEMA, ExtractionResult, OllamaClient
from habit_assistant.llm.prompts import EXTRACTION_SYSTEM_PROMPT, EXTRACTION_USER_TEMPLATE

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"water", "stretch", "diary", "unknown"}


async def parse_message(
    text: str,
    llm: OllamaClient,
    glass_ml: int,
    bottle_ml: int,
) -> ExtractionResult:
    """Parse inbound text into a structured ExtractionResult via the LLM."""
    system_prompt = EXTRACTION_SYSTEM_PROMPT.format(glass_ml=glass_ml, bottle_ml=bottle_ml)
    user_prompt = EXTRACTION_USER_TEMPLATE.format(message=text)

    try:
        raw_json = await llm.chat_json(system_prompt, user_prompt, EXTRACTION_JSON_SCHEMA)
        if raw_json is None:
            return ExtractionResult.unknown()
        data = json.loads(raw_json)
        return _validate(data)
    except Exception:
        logger.exception("Parser failed to extract structured data; failing closed to unknown")
        return ExtractionResult.unknown()


def _validate(data: dict) -> ExtractionResult:
    category = data.get("category")
    if category not in VALID_CATEGORIES:
        return ExtractionResult.unknown()

    confidence_raw = data.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0

    if category == "water":
        try:
            water_ml = int(data.get("water_ml"))
        except (TypeError, ValueError):
            return ExtractionResult.unknown()
        if water_ml <= 0:
            return ExtractionResult.unknown()
        return ExtractionResult("water", water_ml, None, None, confidence)

    if category == "stretch":
        try:
            stretch_min = int(data.get("stretch_min"))
        except (TypeError, ValueError):
            return ExtractionResult.unknown()
        if stretch_min <= 0:
            return ExtractionResult.unknown()
        return ExtractionResult("stretch", None, stretch_min, None, confidence)

    if category == "diary":
        diary_text = data.get("diary_text")
        if not diary_text or not str(diary_text).strip():
            return ExtractionResult.unknown()
        return ExtractionResult("diary", None, None, str(diary_text), confidence)

    return ExtractionResult.unknown()
