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

# Fixed probe message for probe_schema_support(): a simple, unambiguous
# water log that every configured model should extract the same way.
_PROBE_MESSAGE = "500ml"
_PROBE_GLASS_ML = 250
_PROBE_BOTTLE_ML = 600


@dataclass(slots=True)
class ExtractionResult:
    category: str
    water_ml: int | None
    stretch_min: int | None
    diary_text: str | None
    confidence: float

    @classmethod
    def unknown(cls) -> "ExtractionResult":
        return cls(category="unknown", water_ml=None, stretch_min=None, diary_text=None, confidence=0.0)


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


def _has_recognizable_category(data: Any) -> bool:
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
    v0.1.0 already tested and relied on (AC2.4 no-regression)."""
    return isinstance(data, dict) and data.get("category") in VALID_CATEGORIES


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

    async def chat_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> str | None:
        """POST /api/chat with stream=false and a JSON schema in `format`,
        trying each configured model in order (AC2.2). Returns the
        extracted JSON substring from the first model whose response has a
        recognized `category`; if every model fails (transport/HTTP error,
        unparseable JSON, or an off-schema category), returns None so the
        caller fails closed to unknown. Never raises."""
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
            if _has_recognizable_category(data):
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

    async def probe_schema_support(self) -> dict[str, bool]:
        """AC2.1: send a known message + the extraction schema to each
        configured model once, and log per-model whether the response
        honors `format` (exact-keys check: the parsed JSON is a dict whose
        keys are exactly the 5 required schema keys). Informational only --
        never gates chat_json's own fallback logic, and a probe failure
        (network error, bad JSON, unreachable model) for one model is
        logged and counted as non-conformant rather than raised, so a
        broken probe can never crash startup."""
        # Local import: llm.prompts has no import of ollama_client, so this
        # is not a cycle, but keeping it here (rather than module-level)
        # makes clear the probe is the only user of these specific prompts.
        from habit_assistant.llm.prompts import EXTRACTION_SYSTEM_PROMPT, EXTRACTION_USER_TEMPLATE

        system_prompt = EXTRACTION_SYSTEM_PROMPT.format(glass_ml=_PROBE_GLASS_ML, bottle_ml=_PROBE_BOTTLE_ML)
        user_prompt = EXTRACTION_USER_TEMPLATE.format(message=_PROBE_MESSAGE)

        results: dict[str, bool] = {}
        for model in self._models:
            conformant = False
            try:
                content = await self._post(model, system_prompt, user_prompt, EXTRACTION_JSON_SCHEMA)
                if content is not None:
                    data = json.loads(strip_think_and_prose(content))
                    conformant = isinstance(data, dict) and set(data.keys()) == REQUIRED_SCHEMA_KEYS
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
