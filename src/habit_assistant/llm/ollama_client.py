"""Ollama /api/chat client: structured JSON extraction + free-form text.

Handles "thinking" Qwen variants: sends think=false where supported, and
always strips <think>...</think> blocks / surrounding prose before parsing,
so a model that ignores think=false still produces usable output.

v0.2.0: supports an ordered model fallback chain for chat_json (the MLX
backend for qwen3.5:9b-mlx was found, live, to ignore the JSON-schema
`format` constraint entirely -- see IMPL.md's v0.1.0 "Known limitations").
`probe_schema_support()` checks each configured model once at startup and
logs whether it honors `format`, purely for operator visibility; it never
gates behavior and never raises. The runtime safety net is unchanged:
core/parser.py._validate still fails closed on anything malformed.

v0.4.0 (ROADMAP.md "Runtime Resilience", scope item 2): `_post` now retries
a *transport*-level failure (connection refused/timeout -- the host is
unreachable) a bounded number of times with backoff before giving up on
that model, and `self.available` tracks whether the most recent request
actually reached the host, as a signal distinguishable from "reached the
host but got an off-schema/low-confidence response" (which is a parser
concern, handled in core/parser.py, not an availability concern). An
application-level HTTP error status (host responded, just badly) is NOT
retried here and still counts as "available" -- only a transport error
means the host itself is unreachable.

v0.7.0 (ROADMAP.md "Multi-Habit Extensibility", SPEC-v0.7.md §4 R4/R6):
`ExtractionResult` becomes generic (`category`, a single `value` field
typed number|string|bool|None, `confidence`) instead of one field per
habit, and `build_extraction_schema(category_enum)` generates the JSON
schema from the live habit registry -- the schema's *size* stays
independent of habit count (one `value` field, not N), which matters
because the schema-weak MLX backend already struggles with `format`
(see v0.1.0/v0.2.0 findings below; the fallback chain remains the safety
net). `chat_json`/`probe_schema_support` take the system/user prompt and
schema as parameters instead of reading module-level constants, since the
prompt is now built per-registry (`llm/prompts.py`, module M1) rather than
being a fixed template.

`EXTRACTION_JSON_SCHEMA`/`VALID_CATEGORIES`/`REQUIRED_SCHEMA_KEYS` (the old
fixed water/stretch/diary schema) are kept as-is, unchanged, purely so
`core/parser.py` (not yet migrated onto the registry -- that is module M1's
work) still imports cleanly; nothing in this module derives from or reads
them anymore.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# --- Legacy (v0.1.0-v0.6.0) fixed schema -----------------------------------
# Kept unchanged, purely as a backward-compatibility import target for
# core/parser.py (module M1's file, not yet migrated onto the registry).
# Nothing in this module derives from these anymore -- see module docstring.
EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["water", "stretch", "diary", "unknown"]},
        "water_ml": {"type": ["integer", "null"]},
        "stretch_min": {"type": ["integer", "null"]},
        "diary_text": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["category", "water_ml", "stretch_min", "diary_text", "confidence"],
}

VALID_CATEGORIES = set(EXTRACTION_JSON_SCHEMA["properties"]["category"]["enum"])
REQUIRED_SCHEMA_KEYS = set(EXTRACTION_JSON_SCHEMA["required"])


# --- v0.7.0 generic extraction result + schema builder ----------------------


@dataclass(slots=True)
class ExtractionResult:
    """Generic across every habit type (SPEC-v0.7.md §4 R9): `category` is
    a habit id or `"unknown"`; `value` is the single extracted value,
    typed per the matched habit (`float` for numeric/duration, `str` for
    text, `bool` for boolean), or `None` for `unknown`."""

    category: str
    value: float | str | bool | None
    confidence: float

    @classmethod
    def unknown(cls) -> "ExtractionResult":
        return cls(category="unknown", value=None, confidence=0.0)


def build_extraction_schema(category_enum: list[str]) -> dict[str, Any]:
    """Generate the extraction JSON schema from the live habit registry's
    category enum (SPEC-v0.7.md §4 R4). Deliberately a single `value`
    field regardless of habit count -- the schema's *size* is independent
    of how many habits are configured (one field, not one per habit),
    to avoid growing the payload the schema-weak MLX backend already
    struggles with (module docstring; the fallback chain in `chat_json`
    remains the safety net)."""
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(category_enum)},
            "value": {"type": ["number", "string", "boolean", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["category", "value", "confidence"],
    }


def strip_think_and_prose(raw: str) -> str:
    """Remove <think>...</think> blocks, then trim to the outermost {...}
    so any leading/trailing prose the model adds is discarded before
    json.loads. Returns the best-effort candidate JSON substring (caller
    still needs to try/except the parse)."""
    without_think = THINK_BLOCK_RE.sub("", raw).strip()
    start = without_think.find("{")
    end = without_think.rfind("}")
    if start == -1 or end == -1 or end < start:
        return without_think
    return without_think[start : end + 1]


def _has_recognizable_category(data: Any, valid_categories: set[str]) -> bool:
    """Lightweight fallback-worthiness gate for chat_json's model chain
    (AC2.2): does the parsed JSON at least carry a recognized `category`
    enum value? A model that ignores `format` entirely (the known MLX gap)
    tends to return a wholly different shape/value here (e.g. the live
    `{"category": "Beverage", ...}` case documented in IMPL.md), which is
    exactly what should trigger falling through to the next model.

    Deliberately permissive beyond that: extra keys, a missing/garbled
    `confidence`, or an out-of-range numeric field are NOT treated as
    off-schema here -- core/parser.py._validate already fails those closed
    to `unknown` without needing a second model, and that's the behavior
    v0.1.0 already tested and relied on (AC2.4 no-regression).

    v0.7.0 (SPEC-v0.7.md §4 R6): `valid_categories` is now a parameter --
    the live habit registry's ids + `"unknown"` -- instead of the fixed
    module-level `VALID_CATEGORIES`, so the gate accepts whatever habits
    are actually configured."""
    return isinstance(data, dict) and data.get("category") in valid_categories


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        models: str | Sequence[str],
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        retry_attempts: int = 1,
        retry_backoff_seconds: float = 0.3,
    ):
        self._base_url = base_url.rstrip("/")
        self._models: list[str] = [models] if isinstance(models, str) else list(models)
        if not self._models:
            raise ValueError("OllamaClient requires at least one model")
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._retry_attempts = max(retry_attempts, 0)
        self._retry_backoff = retry_backoff_seconds
        # v0.4.0: reachability of the *last* request actually made (any
        # model, any of chat_json/chat_text/probe_schema_support) -- "LLM
        # unavailable" state, distinct from a schema-valid-but-off-target
        # or low-confidence parse (see module docstring).
        self.available: bool = True

    async def _post(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
    ) -> str | None:
        """POST /api/chat for one model. Returns the raw, unstripped
        `message.content` string, or None on any transport/HTTP failure.
        Never raises.

        v0.4.0: a transport-level failure (host unreachable/timed out) is
        retried up to `retry_attempts` times with exponential backoff
        before giving up and setting `self.available = False`. An
        HTTP error *status* (the host responded) is not retried here --
        it sets `self.available = True` (host is up) and returns None so
        the existing fail-closed-to-unknown behavior is unchanged."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
        }
        if json_schema is not None:
            payload["format"] = json_schema

        backoff = self._retry_backoff
        attempts = self._retry_attempts + 1
        for attempt in range(attempts):
            try:
                resp = await self._client.post(f"{self._base_url}/api/chat", json=payload)
            except httpx.TransportError as exc:
                self.available = False
                if attempt < attempts - 1:
                    logger.warning(
                        "Ollama request unreachable (model=%s, attempt %d/%d, retrying in %.1fs): %s",
                        model,
                        attempt + 1,
                        attempts,
                        backoff,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                logger.warning(
                    "Ollama request failed after %d attempt(s) (model=%s): %s", attempts, model, exc
                )
                return None

            self.available = True  # a response (of any status) means the host is reachable
            try:
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Ollama request failed (model=%s): %s", model, exc)
                return None
            return body.get("message", {}).get("content", "")

        return None  # unreachable in practice -- defensive

    async def chat_json(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any], valid_categories: set[str]
    ) -> str | None:
        """POST /api/chat with stream=false and a JSON schema in `format`,
        trying each configured model in order (AC2.2). Returns the
        extracted JSON substring from the first model whose response has a
        recognized `category`; if every model fails (transport/HTTP error,
        unparseable JSON, or an off-schema category), returns None so the
        caller fails closed to unknown. Never raises.

        v0.7.0 (SPEC-v0.7.md §4 R6): `valid_categories` is now a required
        parameter -- the live habit registry's `category_enum()` -- instead
        of the fixed module-level `VALID_CATEGORIES`."""
        for model in self._models:
            content = await self._post(model, system_prompt, user_prompt, json_schema)
            if content is None:
                continue
            logger.debug("Raw Ollama output (chat_json, model=%s): %s", model, content)
            raw = strip_think_and_prose(content)
            try:
                data = json.loads(raw)
            except ValueError:
                logger.warning("Model %s returned unparseable JSON, trying next model in chain", model)
                continue
            if _has_recognizable_category(data, valid_categories):
                return raw
            logger.warning(
                "Model %s returned off-schema JSON (category=%r), trying next model in chain",
                model,
                data.get("category") if isinstance(data, dict) else type(data).__name__,
            )
        return None

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """POST /api/chat for free-form text (diary reflection, weekly
        narrative), using the first model in the chain. Returns
        think-stripped text, or None on failure. Not part of the fallback
        chain (out of scope for v0.2.0 -- only structured extraction has an
        observed schema-conformance gap)."""
        content = await self._post(self._models[0], system_prompt, user_prompt, None)
        if content is None:
            return None
        logger.debug("Raw Ollama output (chat_text): %s", content)
        return THINK_BLOCK_RE.sub("", content).strip()

    async def probe_schema_support(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]
    ) -> dict[str, bool]:
        """AC2.1: send a known message + the extraction schema to each
        configured model once, and log per-model whether the response
        honors `format` (exact-keys check: the parsed JSON is a dict whose
        keys are exactly the schema's required keys). Informational only --
        never gates chat_json's own fallback logic, and a probe failure
        (network error, bad JSON, unreachable model) for one model is
        logged and counted as non-conformant rather than raised, so a
        broken probe can never crash startup.

        v0.7.0 (SPEC-v0.7.md §4 R4/R6): `system_prompt`/`user_prompt`/
        `json_schema` are now parameters instead of built internally from
        the fixed v0.6.0 prompt/schema -- the caller (main.py) builds them
        from the live habit registry."""
        required_keys = set(json_schema.get("required", []))
        results: dict[str, bool] = {}
        for model in self._models:
            conformant = False
            try:
                content = await self._post(model, system_prompt, user_prompt, json_schema)
                if content is not None:
                    data = json.loads(strip_think_and_prose(content))
                    conformant = isinstance(data, dict) and set(data.keys()) == required_keys
            except Exception:
                logger.warning("Schema conformance probe errored for model %s", model, exc_info=True)
                conformant = False
            results[model] = conformant
            logger.info(
                "Ollama schema probe: model=%s format_conformant=%s%s",
                model,
                conformant,
                "" if conformant else " (ignores `format`? falls back to next model in chain at runtime)",
            )
        return results

    async def aclose(self) -> None:
        await self._client.aclose()
