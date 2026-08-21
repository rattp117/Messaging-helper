"""Model fallback / schema-probe / confidence-threshold tests
(ROADMAP.md v0.2.0, AC2.1-AC2.5; v0.7.0 AC7 confidence gate): the ordered
model chain in OllamaClient.chat_json, the startup schema-conformance probe
(probe_schema_support), the confidence-threshold gate in
core/parser.py._validate, single-model back-compat (bare-string `model`
config vs `models = [x]`), and fail-closed behavior on transport/HTTP
errors at every new seam.

ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (module M1): `chat_json`/
`probe_schema_support` now take `valid_categories`/the schema as explicit
parameters (built from a live `HabitRegistry`) instead of reading fixed
module globals, and `parse_message` takes `registry` instead of
`glass_ml`/`bottle_ml`. Every call site below updated accordingly; no
assertion changed.

Companion to test_parser.py, which already covers v0.1.0's single-model
extraction path (think-block stripping, schema/enum/type validation,
malformed JSON, connection errors) -- those cases are not re-duplicated
here. Everything below is mocked via httpx.MockTransport; no network
required.
"""

from __future__ import annotations

import json

import httpx
import pytest

from habit_assistant.config import Config, OllamaConfig
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.parser import parse_message
from habit_assistant.llm.ollama_client import ExtractionResult, OllamaClient, build_extraction_schema
from habit_assistant.llm.prompts import build_extraction_system_prompt, build_extraction_user_prompt
from habit_assistant.main import CLARIFYING_QUESTION, handle_inbound_message
from habit_assistant.storage.db import Database

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

# A fixed probe message/schema, built from the default registry, reused by
# every probe_schema_support() test below (mirrors what main.py's real
# startup probe call builds -- SPEC-v0.7.md §4 R6).
PROBE_SYSTEM_PROMPT = build_extraction_system_prompt(DEFAULT_REGISTRY)
PROBE_USER_PROMPT = build_extraction_user_prompt("500ml")
PROBE_SCHEMA = build_extraction_schema(DEFAULT_REGISTRY.category_enum())

# Wide-open window so tests don't need to fake `clock` just to query rows
# back out; handle_inbound_message defaults to datetime.now() here.
ANY_TIME_WINDOW = ("2000-01-01T00:00:00", "2100-01-01T00:00:00")

# Sentinel telling model_responses_handler to raise a connection error for
# that model instead of returning a response.
CONNECT_ERROR = object()

# The live-observed MLX off-schema shape (IMPL.md v0.1.0 "Known limitations"):
# a valid-looking JSON object, but not the extraction schema at all.
OFF_SCHEMA_MLX_SHAPE = json.dumps({"category": "Beverage", "unit": "ml"})


def model_responses_handler(responses: dict, captured: list | None = None):
    """httpx.MockTransport handler that replies per the *requesting* model
    name (read from the POST body), so one client with several models in
    its chain can be given a different canned response -- or a forced
    failure -- per model. `responses[model]` may be:
    - a `str`: 200 response with that string as message.content
    - a `(status_code, content)` tuple: that status/content
    - `CONNECT_ERROR`: raises httpx.ConnectError for that request
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        body = json.loads(request.content)
        model = body["model"]
        spec = responses[model]
        if spec is CONNECT_ERROR:
            raise httpx.ConnectError("simulated connection failure", request=request)
        if isinstance(spec, tuple):
            status_code, content = spec
        else:
            status_code, content = 200, spec
        return httpx.Response(status_code, json={"message": {"content": content}})

    return handler


def make_client(handler, models, base_url: str = "http://mac-mini:11434") -> OllamaClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient(base_url, models, timeout_seconds=5.0, client=async_client)


def json_payload(**overrides) -> str:
    base = {"category": "unknown", "value": None, "confidence": 0.1}
    base.update(overrides)
    return json.dumps(base)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


class _StaticLLM:
    """Minimal OllamaClient stand-in for handle_inbound_message tests where
    we want to hand-pick the raw chat_json response directly (bypassing
    HTTP entirely) rather than route it through a MockTransport."""

    def __init__(self, content: str | None):
        self._content = content

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        return self._content

    async def chat_text(self, system_prompt, user_prompt):
        return None


class _RecordingChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def send_actionable(self, text: str, buttons) -> None:
        # SPEC-v1.1.md R-U7: mirrors the Channel ABC's own default -- drop
        # the buttons, record text (this file's own log confirmations are
        # unaffected in content by v1.1's undo button).
        self.sent.append(text)


# ---------------------------------------------------------------------------
# AC2.1 -- probe_schema_support(): per-model conformance check, never raises
# ---------------------------------------------------------------------------


async def test_probe_reports_conformant_model_true():
    content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_client(model_responses_handler({"good-model": content}), models=["good-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)

    assert results == {"good-model": True}


async def test_probe_reports_non_conformant_model_false():
    """A response whose shape doesn't match the schema at all (the
    live-observed MLX case) is reported non-conformant, not conformant."""
    llm = make_client(model_responses_handler({"mlx-model": OFF_SCHEMA_MLX_SHAPE}), models=["mlx-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)

    assert results == {"mlx-model": False}


async def test_probe_extra_key_is_non_conformant():
    """AC2.1's exact-keys check: the 3 required keys PLUS an extra key must
    not count as conformant."""
    content = json.dumps({"category": "water", "value": 500, "confidence": 0.9, "extra_field": "oops"})
    llm = make_client(model_responses_handler({"chatty-model": content}), models=["chatty-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)

    assert results == {"chatty-model": False}


async def test_probe_missing_key_is_non_conformant():
    content = json.dumps({"category": "water", "value": 500})  # missing confidence
    llm = make_client(model_responses_handler({"partial-model": content}), models=["partial-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)

    assert results == {"partial-model": False}


async def test_probe_unreachable_model_reports_false_and_does_not_raise():
    llm = make_client(model_responses_handler({"dead-model": CONNECT_ERROR}), models=["dead-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)  # must not raise

    assert results == {"dead-model": False}


async def test_probe_http_error_status_reports_false_and_does_not_raise():
    llm = make_client(model_responses_handler({"error-model": (500, "irrelevant")}), models=["error-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)

    assert results == {"error-model": False}


async def test_probe_checks_every_configured_model_independently():
    """One bad model must not stop the probe from reaching the others, and
    each model's result reflects only its own response."""
    good_content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_client(
        model_responses_handler(
            {
                "conformant": good_content,
                "non-conformant": OFF_SCHEMA_MLX_SHAPE,
                "unreachable": CONNECT_ERROR,
            }
        ),
        models=["conformant", "non-conformant", "unreachable"],
    )

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)

    assert results == {"conformant": True, "non-conformant": False, "unreachable": False}


async def test_probe_unexpected_exception_does_not_raise():
    """probe_schema_support must survive more than just httpx errors -- a
    genuinely unexpected exception from one model must still not crash the
    caller (belt-and-suspenders per the docstring)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom - not an httpx error at all")

    llm = make_client(handler, models=["flaky-model"])

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)  # must not raise

    assert results == {"flaky-model": False}


# ---------------------------------------------------------------------------
# AC2.2 -- chat_json ordered fallback chain
# ---------------------------------------------------------------------------


async def test_fallback_first_model_off_schema_second_model_wins():
    """First model returns the off-schema MLX shape; chat_json must fall
    through to the second model and return ITS valid extraction."""
    captured: list[httpx.Request] = []
    valid_content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_client(
        model_responses_handler({"model-a": OFF_SCHEMA_MLX_SHAPE, "model-b": valid_content}, captured=captured),
        models=["model-a", "model-b"],
    )

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.9)
    assert [json.loads(r.content)["model"] for r in captured] == ["model-a", "model-b"]


async def test_fallback_first_model_unparseable_json_second_model_wins():
    """Fallback also triggers when the first model's failure mode is
    unparseable JSON rather than a wrong category."""
    valid_content = json_payload(category="stretch", value=10, confidence=0.9)
    llm = make_client(
        model_responses_handler({"model-a": "not even json {{{", "model-b": valid_content}),
        models=["model-a", "model-b"],
    )

    result = await parse_message("did 10 min stretch", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("stretch", 10.0, 0.9)


async def test_fallback_all_models_off_schema_yields_unknown():
    llm = make_client(
        model_responses_handler({"model-a": OFF_SCHEMA_MLX_SHAPE, "model-b": OFF_SCHEMA_MLX_SHAPE}),
        models=["model-a", "model-b"],
    )

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_fallback_first_model_valid_second_model_never_called():
    """First-match-wins: if model 1 already clears the gate, model 2 must
    not be called at all."""
    captured: list[httpx.Request] = []
    valid_content = json_payload(category="water", value=500, confidence=0.9)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        if body["model"] == "model-b":
            raise AssertionError("model-b should never be called when model-a already succeeded")
        return httpx.Response(200, json={"message": {"content": valid_content}})

    llm = make_client(handler, models=["model-a", "model-b"])

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.9)
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# AC2.3/AC7 -- confidence threshold gate, exercised through the handler path
# ---------------------------------------------------------------------------


async def test_below_threshold_confidence_yields_clarifying_question_and_no_row(db):
    """AC2.3/AC7: a schema-valid water extraction whose confidence (0.3) is
    below the configured threshold (0.55) must clarify, not log."""
    content = json_payload(category="water", value=500, confidence=0.3)
    llm = _StaticLLM(content)
    channel = _RecordingChannel()
    config = Config.model_validate({"ollama": {"confidence_threshold": 0.55}})

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=config)

    assert channel.sent == [CLARIFYING_QUESTION]
    assert db.logs_between(*ANY_TIME_WINDOW) == []


async def test_at_threshold_confidence_is_logged(db):
    """Threshold comparison is exclusive (`< threshold` fails, `==
    threshold` passes) -- confidence exactly at the boundary is kept."""
    content = json_payload(category="water", value=500, confidence=0.55)
    llm = _StaticLLM(content)
    channel = _RecordingChannel()
    config = Config.model_validate({"ollama": {"confidence_threshold": 0.55}})

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=config)

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]
    rows = db.logs_between(*ANY_TIME_WINDOW)
    assert len(rows) == 1
    assert rows[0]["category"] == "water"


async def test_above_threshold_confidence_is_logged(db):
    content = json_payload(category="stretch", value=10, confidence=0.9)
    llm = _StaticLLM(content)
    channel = _RecordingChannel()
    config = Config.model_validate({"ollama": {"confidence_threshold": 0.55}})

    await handle_inbound_message("10 min stretch", db=db, llm=llm, channel=channel, config=config)

    assert channel.sent == ["✅ 10 min stretch logged — 1st today"]
    assert len(db.logs_between(*ANY_TIME_WINDOW)) == 1


async def test_custom_threshold_from_config_is_honored(db):
    """A confidence that would pass the default threshold must still be
    demoted under a stricter, user-configured threshold."""
    content = json_payload(category="water", value=500, confidence=0.6)
    llm = _StaticLLM(content)
    channel = _RecordingChannel()
    config = Config.model_validate({"ollama": {"confidence_threshold": 0.9}})

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=config)

    assert channel.sent == [CLARIFYING_QUESTION]
    assert db.logs_between(*ANY_TIME_WINDOW) == []


async def test_below_threshold_diary_also_clarifies_and_writes_no_row(db):
    """AC2.3/AC7 applies uniformly across habit types, not just water."""
    content = json_payload(category="diary", value="a quiet day", confidence=0.2)
    llm = _StaticLLM(content)
    channel = _RecordingChannel()
    config = Config.model_validate({"ollama": {"confidence_threshold": 0.55}})

    await handle_inbound_message("a quiet day", db=db, llm=llm, channel=channel, config=config)

    assert channel.sent == [CLARIFYING_QUESTION]
    assert db.logs_between(*ANY_TIME_WINDOW) == []


# ---------------------------------------------------------------------------
# AC2.4 -- single-model (bare string) back-compat
# ---------------------------------------------------------------------------


def test_ollama_config_model_chain_defaults_to_single_model_list():
    """A config carrying only `model` (the v0.1.0 shape) yields a 1-element
    chain, not a crash or an empty chain."""
    config = OllamaConfig(model="qwen3.5:9b-mlx")
    assert config.model_chain == ["qwen3.5:9b-mlx"]


def test_ollama_config_model_chain_prefers_models_when_set():
    config = OllamaConfig(model="qwen3.5:9b-mlx", models=["qwen3.5:9b-mlx", "qwen3:8b"])
    assert config.model_chain == ["qwen3.5:9b-mlx", "qwen3:8b"]


def test_ollama_client_rejects_empty_model_list():
    with pytest.raises(ValueError):
        OllamaClient("http://fake", [])


async def test_ollama_client_accepts_bare_string_model_and_calls_it():
    """OllamaClient(base_url, "single-model-string", ...) -- not a list --
    must work exactly like a 1-element list."""
    captured: list[httpx.Request] = []
    content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_client(model_responses_handler({"solo": content}, captured=captured), models="solo")

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.9)
    assert len(captured) == 1
    assert json.loads(captured[0].content)["model"] == "solo"


async def test_bare_string_model_behaves_identically_to_list_of_one():
    """AC2.4: a bare-string `model` config produces the same result as an
    equivalent `models = ["x"]` list for a schema-valid response -- no
    regression from v0.1.0's single-model behavior."""
    content = json_payload(category="stretch", value=10, confidence=0.95)

    bare_llm = make_client(model_responses_handler({"qwen3.5:9b-mlx": content}), models="qwen3.5:9b-mlx")
    list_llm = make_client(model_responses_handler({"qwen3.5:9b-mlx": content}), models=["qwen3.5:9b-mlx"])

    bare_result = await parse_message("did 10 min stretch", bare_llm, DEFAULT_REGISTRY)
    list_result = await parse_message("did 10 min stretch", list_llm, DEFAULT_REGISTRY)

    assert bare_result == list_result == ExtractionResult("stretch", 10.0, 0.95)


# ---------------------------------------------------------------------------
# AC2.5 -- transport/HTTP errors at every new seam fail closed, never raise
# ---------------------------------------------------------------------------


async def test_chat_json_all_models_connect_error_fails_closed():
    llm = make_client(
        model_responses_handler({"model-a": CONNECT_ERROR, "model-b": CONNECT_ERROR}),
        models=["model-a", "model-b"],
    )

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)  # must not raise

    assert result == ExtractionResult.unknown()


async def test_chat_json_all_models_http_500_fails_closed():
    llm = make_client(
        model_responses_handler({"model-a": (500, "irrelevant"), "model-b": (500, "irrelevant")}),
        models=["model-a", "model-b"],
    )

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()


async def test_chat_json_first_model_connect_error_second_model_recovers():
    """A transport failure on model 1 is itself just another fallback
    trigger -- model 2 still gets a chance."""
    valid_content = json_payload(category="water", value=500, confidence=0.9)
    llm = make_client(
        model_responses_handler({"model-a": CONNECT_ERROR, "model-b": valid_content}),
        models=["model-a", "model-b"],
    )

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult("water", 500.0, 0.9)


async def test_probe_schema_support_total_outage_does_not_raise():
    llm = make_client(
        model_responses_handler({"model-a": CONNECT_ERROR, "model-b": CONNECT_ERROR}),
        models=["model-a", "model-b"],
    )

    results = await llm.probe_schema_support(PROBE_SYSTEM_PROMPT, PROBE_USER_PROMPT, PROBE_SCHEMA)  # must not raise

    assert results == {"model-a": False, "model-b": False}


async def test_handler_path_survives_total_llm_outage_no_exception_escapes(db):
    """End-to-end through handle_inbound_message with a real, mocked
    OllamaClient (not the _StaticLLM stand-in): every model in the chain
    unreachable must still resolve to the clarifying question, never an
    unhandled exception reaching the inbound loop."""
    llm = make_client(
        model_responses_handler({"model-a": CONNECT_ERROR, "model-b": CONNECT_ERROR}),
        models=["model-a", "model-b"],
    )
    channel = _RecordingChannel()
    config = Config()

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=config)

    assert channel.sent == [CLARIFYING_QUESTION]
    assert db.logs_between(*ANY_TIME_WINDOW) == []


async def test_chat_json_none_result_still_fails_closed_through_parser():
    """Belt-and-suspenders: even if chat_json itself returns None (every
    seam already covered above), parse_message's own outer try/except is
    still the second line of defense -- confirm end to end one more time
    with a client whose chat_json is monkeypatched to return None directly."""
    llm = _StaticLLM(None)

    result = await parse_message("500ml", llm, DEFAULT_REGISTRY)

    assert result == ExtractionResult.unknown()
