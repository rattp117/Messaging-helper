"""Parser tests (AC4, AC5, AC11): mocked Ollama, no network required.

Covers:
- Valid extractions for water / stretch / diary (AC5 bilingual + unit cases).
- <think> block + surrounding prose stripping (AC4).
- Malformed JSON -> fail-closed `unknown`, no exception (AC4).
- Connection error / HTTP error -> fail-closed `unknown` (AC4).
- Invalid enum / invalid types -> fail-closed `unknown` (AC4).
- Confidence field handling (missing / non-numeric) (AC4/§7 schema).
- Request shape: POST /api/chat, stream=false, format=<json schema>, think=false (AC4).
- Unit constants (glass/bottle ml) are injected into the prompt from config, i.e.
  configurable per AC5.
"""

from __future__ import annotations

import json

import httpx
import pytest

from habit_assistant.core.parser import parse_message
from habit_assistant.llm.ollama_client import (
    EXTRACTION_JSON_SCHEMA,
    ExtractionResult,
    OllamaClient,
    strip_think_and_prose,
)

GLASS_ML = 250
BOTTLE_ML = 600


def make_ollama_client(handler, base_url: str = "http://mac-mini:11434") -> OllamaClient:
    """Build an OllamaClient wired to an httpx.MockTransport so no real
    network call is made."""
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient(base_url, "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


def content_response_handler(content: str, status_code: int = 200, captured: list | None = None):
    """Returns an httpx.MockTransport handler that always replies with the
    given `content` as the Ollama chat message content. If `captured` is
    given, the raw request is appended to it for later inspection."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(status_code, json={"message": {"content": content}})

    return handler


def json_payload(**overrides) -> str:
    base = {
        "category": "unknown",
        "water_ml": None,
        "stretch_min": None,
        "diary_text": None,
        "confidence": 0.1,
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Valid extractions (AC5)
# ---------------------------------------------------------------------------


async def test_water_glass_thai_normalizes_to_ml():
    """"ดื่มน้ำ 2 แก้ว" -> water 500 ml (2 x 250 ml glass constant)."""
    content = json_payload(category="water", water_ml=2 * GLASS_ML, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("ดื่มน้ำ 2 แก้ว", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult("water", 500, None, None, 0.9)


async def test_stretch_english_message():
    """"did 10 min stretch" -> stretch 10 min."""
    content = json_payload(category="stretch", stretch_min=10, confidence=0.95)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("did 10 min stretch", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult("stretch", None, 10, None, 0.95)


async def test_explicit_ml_message():
    """"500ml" -> water 500 ml (explicit ml, no unit conversion needed)."""
    content = json_payload(category="water", water_ml=500, confidence=0.95)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult("water", 500, None, None, 0.95)


async def test_bottle_message_normalizes_to_ml():
    """"1 bottle of water" -> 600 ml (bottle constant)."""
    content = json_payload(category="water", water_ml=BOTTLE_ML, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("1 bottle of water", llm, GLASS_ML, BOTTLE_ML)

    assert result.category == "water"
    assert result.water_ml == 600


async def test_diary_message():
    content = json_payload(
        category="diary",
        diary_text="today was a good day, felt productive",
        confidence=0.85,
    )
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("today was a good day, felt productive", llm, GLASS_ML, BOTTLE_ML)

    assert result.category == "diary"
    assert result.diary_text == "today was a good day, felt productive"
    assert result.water_ml is None
    assert result.stretch_min is None


async def test_unit_constants_are_configurable():
    """Different glass/bottle ml constants must reach the LLM prompt (AC5:
    "glass/bottle constants configurable"). We don't control what the LLM
    returns, but we can assert the *request* carries the configured values."""
    captured: list[httpx.Request] = []
    content = json_payload(category="water", water_ml=900, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content, captured=captured))

    await parse_message("2 glasses", llm, glass_ml=450, bottle_ml=900)

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    system_msg = body["messages"][0]["content"]
    assert "450" in system_msg
    assert "900" in system_msg


# ---------------------------------------------------------------------------
# Think-block + prose stripping (AC4)
# ---------------------------------------------------------------------------


def test_strip_think_and_prose_removes_think_block():
    raw = "<think>reasoning about the message...</think>\n" + json_payload(category="water", water_ml=500)
    stripped = strip_think_and_prose(raw)
    assert stripped == json_payload(category="water", water_ml=500)
    json.loads(stripped)  # must be valid JSON on its own


def test_strip_think_and_prose_removes_surrounding_prose():
    raw = f"Sure, here is the JSON:\n{json_payload(category='stretch', stretch_min=10)}\nHope that helps!"
    stripped = strip_think_and_prose(raw)
    parsed = json.loads(stripped)
    assert parsed["category"] == "stretch"
    assert parsed["stretch_min"] == 10


async def test_parse_message_with_think_block_and_prose_wrapped_json():
    """End-to-end through parse_message: <think> block AND leading/trailing
    prose around valid JSON must still parse correctly."""
    inner = json_payload(category="water", water_ml=500, confidence=0.95)
    content = f"<think>\nThe user drank water, let me extract this.\n</think>\nHere you go:\n{inner}\nDone."
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult("water", 500, None, None, 0.95)


# ---------------------------------------------------------------------------
# Fail-closed behavior (AC4)
# ---------------------------------------------------------------------------


async def test_malformed_json_fails_closed_to_unknown():
    llm = make_ollama_client(content_response_handler("not even json {{{"))

    result = await parse_message("garbled reply", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


async def test_connection_error_fails_closed_to_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    llm = make_ollama_client(handler)

    # Must not raise -- the parser fails closed instead of crashing the loop.
    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


async def test_http_error_status_fails_closed_to_unknown():
    llm = make_ollama_client(content_response_handler("irrelevant", status_code=500))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


async def test_invalid_category_enum_fails_closed_to_unknown():
    """Off-schema response (e.g. non-conformant backend) with an invalid
    category value must fail closed, not raise or fabricate a category."""
    content = json.dumps(
        {"category": "Beverage", "water_ml": 500, "stretch_min": None, "diary_text": None, "confidence": 0.8}
    )
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


async def test_purple_elephants_unknown_category_response():
    content = json_payload(category="unknown", confidence=0.1)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("purple elephants dance sideways", llm, GLASS_ML, BOTTLE_ML)

    assert result.category == "unknown"


@pytest.mark.parametrize("bad_water_ml", ["abc", None, [], {}])
async def test_water_with_non_numeric_water_ml_fails_closed(bad_water_ml):
    content = json_payload(category="water", water_ml=bad_water_ml, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("some water message", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


@pytest.mark.parametrize("bad_water_ml", [0, -100])
async def test_water_with_non_positive_water_ml_fails_closed(bad_water_ml):
    content = json_payload(category="water", water_ml=bad_water_ml, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("0 water", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


@pytest.mark.parametrize("bad_stretch_min", ["abc", None, -5, 0])
async def test_stretch_with_invalid_stretch_min_fails_closed(bad_stretch_min):
    content = json_payload(category="stretch", stretch_min=bad_stretch_min, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("stretch message", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


@pytest.mark.parametrize("bad_diary_text", [None, "", "   "])
async def test_diary_with_empty_diary_text_fails_closed(bad_diary_text):
    content = json_payload(category="diary", diary_text=bad_diary_text, confidence=0.8)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("...", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


async def test_missing_required_keys_fails_closed():
    """Response missing keys entirely (not even null) must not crash."""
    content = json.dumps({"category": "water"})  # water_ml key absent
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult.unknown()


async def test_extra_unexpected_keys_do_not_crash():
    content = json.dumps(
        {
            "category": "water",
            "water_ml": 500,
            "stretch_min": None,
            "diary_text": None,
            "confidence": 0.9,
            "extra_field": "should be ignored",
        }
    )
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result == ExtractionResult("water", 500, None, None, 0.9)


# ---------------------------------------------------------------------------
# Confidence field handling
# ---------------------------------------------------------------------------


async def test_confidence_missing_defaults_to_zero():
    content = json.dumps(
        {"category": "water", "water_ml": 500, "stretch_min": None, "diary_text": None}
    )  # confidence key entirely absent
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result.category == "water"
    assert result.confidence == 0.0


async def test_confidence_non_numeric_defaults_to_zero_without_crash():
    content = json_payload(category="water", water_ml=500, confidence="very sure")
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result.category == "water"
    assert result.confidence == 0.0


async def test_confidence_integer_is_coerced_to_float():
    content = json.dumps(
        {"category": "water", "water_ml": 500, "stretch_min": None, "diary_text": None, "confidence": 1}
    )
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, GLASS_ML, BOTTLE_ML)

    assert result.confidence == 1.0
    assert isinstance(result.confidence, float)


# ---------------------------------------------------------------------------
# Request shape (AC4: POST /api/chat, stream=false, format=<schema>, think=false)
# ---------------------------------------------------------------------------


async def test_request_shape_matches_spec_section_7():
    captured: list[httpx.Request] = []
    content = json_payload(category="unknown")
    llm = make_ollama_client(content_response_handler(content, captured=captured), base_url="http://mac-mini:11434")

    await parse_message("hello", llm, GLASS_ML, BOTTLE_ML)

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "http://mac-mini:11434/api/chat"

    body = json.loads(request.content)
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == EXTRACTION_JSON_SCHEMA
    assert body["messages"][1]["content"] == "Message: hello"
    assert body["model"] == "qwen3.5:9b-mlx"
