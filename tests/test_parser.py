"""Parser tests (AC6, AC7, AC11): mocked Ollama, no network required.

ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (module M1): `parse_message`/
`_validate` are now generated from a live `HabitRegistry` instead of the
fixed water/stretch/diary shape, and `ExtractionResult` is generic
(`category`, a single `value`, `confidence`). Covers:

- Valid extractions for water / stretch / diary through the default
  registry (AC6 bilingual + unit/alias cases -- no regression vs v0.6.0).
- <think> block + surrounding prose stripping (AC11, unchanged mechanism).
- Malformed JSON / an off-registry category -> fail-closed `unknown`, no
  row implied (AC6).
- Connection error / HTTP error -> fail-closed `unknown` (AC11).
- Per-type validation (AC7): numeric/duration <= 0 rejected, "7"/7.5
  accepted; text ""/whitespace rejected; boolean truthy/falsy/un-coercible
  forms (via a synthetic boolean habit -- the default registry ships none).
- Confidence field handling (missing / non-numeric / below-threshold) (AC7).
- Request shape: POST /api/chat, stream=false, format=<generated schema>,
  think=false, built from `registry` (AC6/AC11).
"""

from __future__ import annotations

import json

import httpx
import pytest

from habit_assistant.config import Config
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.parser import parse_message
from habit_assistant.llm.ollama_client import ExtractionResult, OllamaClient, build_extraction_schema, strip_think_and_prose

# SPEC-LINE.md §4 R-S7/R-B1 (shared surface): parse_message is the ONLY
# model-backed extractor (§5.2 row 1) -- on the LINE branch's no-LLM mode a
# preparse miss goes straight to clarify.tier1_guesses and parse_message is
# never called at all. Every test in this file exercises parse_message's
# real chat_json extraction behavior end to end, so the whole module is
# branch-N/A -- excluded from the LINE gate (`pytest -m "not telegram_only
# and not llm_only"`), not deleted (still exercised on the Telegram branch).
pytestmark = pytest.mark.llm_only

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def _synthetic_habit(
    id_: str,
    type_: str,
    *,
    goal=None,
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    unit_aliases: dict | None = None,
) -> Habit:
    """Mirrors tests/test_confirmations.py's `_synthetic_habit` helper --
    a real `Habit`, not going through `Config`/`HabitConfig`, so these
    tests are independent of what the default config.toml ships (the
    default registry has no boolean habit -- SPEC-v0.7.md §9 risk 5)."""
    return Habit(
        id=id_,
        type=type_,
        label_en=id_,
        label_th=id_,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases=unit_aliases or {},
    )


BOOLEAN_REGISTRY = HabitRegistry([_synthetic_habit("meds", "boolean", unit_en=None, unit_th=None)])


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
    base = {"category": "unknown", "value": None, "confidence": 0.1}
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Valid extractions (AC6) -- default registry, no regression vs v0.6.0
# ---------------------------------------------------------------------------


async def test_water_glass_thai_normalizes_to_ml():
    """"ดื่มน้ำ 2 แก้ว" -> water 500 ml (2 x 250 ml glass alias, resolved by
    the LLM per the generated prompt -- the parser just validates the
    number it's given)."""
    content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("ดื่มน้ำ 2 แก้ว", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.9)


async def test_stretch_english_message():
    """"did 10 min stretch" -> stretch 10 min."""
    content = json_payload(category="stretch", value=10, confidence=0.95)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("did 10 min stretch", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("stretch", 10.0, 0.95)


async def test_explicit_ml_message():
    """"500ml" -> water 500 ml (explicit ml, no unit conversion needed)."""
    content = json_payload(category="water", value=500, confidence=0.95)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.95)


async def test_bottle_message_normalizes_to_ml():
    """"1 bottle of water" -> 600 ml (bottle alias)."""
    content = json_payload(category="water", value=600, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("1 bottle of water", llm, DEFAULT_REGISTRY)

    assert result.category == "water"
    assert result.value == 600.0


async def test_diary_message():
    content = json_payload(
        category="diary",
        value="today was a good day, felt productive",
        confidence=0.85,
    )
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("today was a good day, felt productive", llm, DEFAULT_REGISTRY)

    assert result.category == "diary"
    assert result.value == "today was a good day, felt productive"


async def test_registry_unit_aliases_reach_the_prompt():
    """AC8-adjacent (prompt generalization, verified end-to-end through
    parse_message): the default registry's water `unit_aliases`
    (glass=250, bottle=600) must reach the generated system prompt sent to
    the LLM, exactly as the old fixed prompt's injected glass_ml/bottle_ml
    constants did."""
    captured: list[httpx.Request] = []
    content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content, captured=captured))

    await parse_message("2 glasses", llm, DEFAULT_REGISTRY)

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    system_msg = body["messages"][0]["content"]
    assert "250" in system_msg
    assert "600" in system_msg


async def test_arbitrary_custom_unit_alias_value_reaches_the_prompt():
    """Integration-Vera audit note (v0.7.0 final pass): the test above only
    ever exercises the *default* registry's alias multipliers (250/600).
    That alone can't distinguish "the prompt genuinely reads
    habit.unit_aliases" from "the prompt has 250/600 hardcoded somewhere"
    -- exactly the distinction the pre-v0.7 test
    (`test_unit_constants_are_configurable`, `git show
    v0.6.0:tests/test_parser.py`) was designed to prove, by deliberately
    passing NON-default glass_ml=450/bottle_ml=900 and asserting those
    (not the shipped defaults) reached the prompt. This test restores that
    guarantee under the v0.7 registry-driven contract: a synthetic habit
    with a deliberately unusual, non-default alias multiplier must have
    that exact number appear in the generated prompt."""
    cup_habit = Habit(
        id="broth",
        type="numeric",
        label_en="broth",
        label_th="ซุป",
        unit_en="ml",
        unit_th="มล.",
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={"cup": 337},  # deliberately arbitrary, not a real-world default
    )
    registry = HabitRegistry([cup_habit])
    captured: list[httpx.Request] = []
    content = json_payload(category="broth", value=337, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content, captured=captured))

    await parse_message("1 cup broth", llm, registry)

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    system_msg = body["messages"][0]["content"]
    assert "337" in system_msg


# ---------------------------------------------------------------------------
# Think-block + prose stripping (AC11)
# ---------------------------------------------------------------------------


def test_strip_think_and_prose_removes_think_block():
    raw = "<think>reasoning about the message...</think>\n" + json_payload(category="water", value=500)
    stripped = strip_think_and_prose(raw)
    assert stripped == json_payload(category="water", value=500)
    json.loads(stripped)  # must be valid JSON on its own


def test_strip_think_and_prose_removes_surrounding_prose():
    raw = f"Sure, here is the JSON:\n{json_payload(category='stretch', value=10)}\nHope that helps!"
    stripped = strip_think_and_prose(raw)
    parsed = json.loads(stripped)
    assert parsed["category"] == "stretch"
    assert parsed["value"] == 10


async def test_parse_message_with_think_block_and_prose_wrapped_json():
    """End-to-end through parse_message: <think> block AND leading/trailing
    prose around valid JSON must still parse correctly."""
    inner = json_payload(category="water", value=500, confidence=0.95)
    content = f"<think>\nThe user drank water, let me extract this.\n</think>\nHere you go:\n{inner}\nDone."
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.95)


# ---------------------------------------------------------------------------
# Fail-closed behavior (AC6, AC11)
# ---------------------------------------------------------------------------


async def test_malformed_json_fails_closed_to_unknown():
    llm = make_ollama_client(content_response_handler("not even json {{{"))

    result = await parse_message("garbled reply", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_connection_error_fails_closed_to_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    llm = make_ollama_client(handler)

    # Must not raise -- the parser fails closed instead of crashing the loop.
    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_http_error_status_fails_closed_to_unknown():
    llm = make_ollama_client(content_response_handler("irrelevant", status_code=500))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_category_not_in_registry_fails_closed_to_unknown():
    """AC6: a category the LLM invents that isn't one of the registry's
    configured habit ids (off-schema/off-registry response) must fail
    closed, not raise or fabricate a match."""
    content = json.dumps({"category": "Beverage", "value": 500, "confidence": 0.8})
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_purple_elephants_unknown_category_response():
    content = json_payload(category="unknown", confidence=0.1)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("purple elephants dance sideways", llm, DEFAULT_REGISTRY)

    assert result.category == "unknown"


async def test_missing_required_keys_fails_closed():
    """Response missing keys entirely (not even null) must not crash."""
    content = json.dumps({"category": "water"})  # value key absent
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_extra_unexpected_keys_do_not_crash():
    content = json.dumps(
        {
            "category": "water",
            "value": 500,
            "confidence": 0.9,
            "extra_field": "should be ignored",
        }
    )
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.9)


# ---------------------------------------------------------------------------
# Per-type validation (AC7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", ["abc", None, [], {}])
async def test_numeric_with_non_numeric_value_fails_closed(bad_value):
    content = json_payload(category="water", value=bad_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("some water message", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


@pytest.mark.parametrize("bad_value", [0, -100])
async def test_numeric_with_non_positive_value_fails_closed(bad_value):
    content = json_payload(category="water", value=bad_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("0 water", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


@pytest.mark.parametrize("good_value,expected", [("7", 7.0), (7.5, 7.5), ("7.5", 7.5)])
async def test_numeric_stringified_or_float_value_is_accepted(good_value, expected):
    """AC7: "7"/7.5 -> accepted number (the model returning a stringified
    number is tolerated, per SPEC-v0.7.md §9 risk 4)."""
    content = json_payload(category="water", value=good_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("some water message", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", expected, 0.9)


@pytest.mark.parametrize("bad_value", ["abc", None, -5, 0])
async def test_duration_with_invalid_value_fails_closed(bad_value):
    content = json_payload(category="stretch", value=bad_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("stretch message", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


@pytest.mark.parametrize("bad_value", [None, "", "   "])
async def test_text_with_empty_value_fails_closed(bad_value):
    content = json_payload(category="diary", value=bad_value, confidence=0.8)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("...", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_text_with_non_empty_value_is_accepted():
    content = json_payload(category="diary", value="a quiet reflective day", confidence=0.8)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("a quiet reflective day", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("diary", "a quiet reflective day", 0.8)


@pytest.mark.parametrize("truthy_value", [True, 1, "done", "yes", "ครบ", "แล้ว"])
async def test_boolean_truthy_forms_coerce_to_true(truthy_value):
    """AC7: boolean with "done"/true/1 (and the bilingual equivalents) ->
    True. Uses a synthetic boolean habit since the default registry ships
    none (SPEC-v0.7.md §9 risk 5)."""
    content = json_payload(category="meds", value=truthy_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("took my meds", llm, BOOLEAN_REGISTRY)

    assert result == ExtractionResult("meds", True, 0.9)


@pytest.mark.parametrize("falsy_value", [False, 0, "no", "ยัง"])
async def test_boolean_falsy_forms_coerce_to_false(falsy_value):
    """AC7: boolean with "no"/0 -> False."""
    content = json_payload(category="meds", value=falsy_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("no meds yet", llm, BOOLEAN_REGISTRY)

    assert result == ExtractionResult("meds", False, 0.9)


@pytest.mark.parametrize("uncoercible_value", ["maybe", None, [], {}, 2, -1])
async def test_boolean_uncoercible_forms_fail_closed(uncoercible_value):
    """AC7: an un-coercible boolean value -> unknown."""
    content = json_payload(category="meds", value=uncoercible_value, confidence=0.9)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("meds?", llm, BOOLEAN_REGISTRY)

    assert result == ExtractionResult.unknown()


# ---------------------------------------------------------------------------
# Confidence field handling (AC7)
# ---------------------------------------------------------------------------


async def test_confidence_missing_defaults_to_zero():
    content = json.dumps({"category": "water", "value": 500})  # confidence key entirely absent
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result.category == "water"
    assert result.confidence == 0.0


async def test_confidence_non_numeric_defaults_to_zero_without_crash():
    content = json_payload(category="water", value=500, confidence="very sure")
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result.category == "water"
    assert result.confidence == 0.0


async def test_confidence_integer_is_coerced_to_float():
    content = json.dumps({"category": "water", "value": 500, "confidence": 1})
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result.confidence == 1.0
    assert isinstance(result.confidence, float)


async def test_below_threshold_confidence_fails_closed():
    """AC7 (v0.2 AC2.3 preserved): a schema-valid, business-valid
    extraction whose confidence is below the configured threshold ->
    unknown."""
    content = json_payload(category="water", value=500, confidence=0.3)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY, confidence_threshold=0.55)

    assert result == ExtractionResult.unknown()


async def test_at_threshold_confidence_is_kept():
    """Threshold comparison is exclusive (`< threshold` fails, `==
    threshold` passes)."""
    content = json_payload(category="water", value=500, confidence=0.55)
    llm = make_ollama_client(content_response_handler(content))

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY, confidence_threshold=0.55)

    assert result == ExtractionResult("water", 500.0, 0.55)


# ---------------------------------------------------------------------------
# Request shape (AC11: POST /api/chat, stream=false, format=<generated
# schema>, think=false)
# ---------------------------------------------------------------------------


async def test_request_shape_matches_registry_generated_schema():
    captured: list[httpx.Request] = []
    content = json_payload(category="unknown")
    llm = make_ollama_client(content_response_handler(content, captured=captured), base_url="http://mac-mini:11434")

    await parse_message("hello", llm, DEFAULT_REGISTRY)

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "http://mac-mini:11434/api/chat"

    body = json.loads(request.content)
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == build_extraction_schema(DEFAULT_REGISTRY.category_enum())
    assert body["messages"][1]["content"] == "Message: hello"
    assert body["model"] == "qwen3.5:9b-mlx"
