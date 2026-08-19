"""Ollama /api/chat client: structured JSON extraction + free-form text.

Handles "thinking" Qwen variants: sends think=false where supported, and
always strips <think>...</think> blocks / surrounding prose before parsing,
so a model that ignores think=false still produces usable output.
"""

from __future__ import annotations

import logging
import re
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


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def chat_json(self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any]) -> str | None:
        """POST /api/chat with stream=false and a JSON schema in `format`.
        Returns the extracted JSON substring (think-blocks/prose stripped),
        or None on any transport/HTTP failure. Never raises."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": json_schema,
            "think": False,
        }
        try:
            resp = await self._client.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Ollama chat_json request failed: %s", exc)
            return None

        content = body.get("message", {}).get("content", "")
        logger.debug("Raw Ollama output (chat_json): %s", content)
        return strip_think_and_prose(content)

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """POST /api/chat for free-form text (diary reflection, weekly
        narrative). Returns think-stripped text, or None on failure."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
        }
        try:
            resp = await self._client.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Ollama chat_text request failed: %s", exc)
            return None
        content = body.get("message", {}).get("content", "")
        logger.debug("Raw Ollama output (chat_text): %s", content)
        return THINK_BLOCK_RE.sub("", content).strip()

    async def aclose(self) -> None:
        await self._client.aclose()
