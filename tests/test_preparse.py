"""SPEC-v1.5.md §4 R-L1/R-L2 -- `core/preparse.py`'s deterministic
pre-parser (module `preparse`, owned ACs: AC-14, AC-15).

Four groups of coverage:

1. Supported shapes (AC-14): every shape SPEC-v1.5.md §3.3 names
   ("500ml", "2 แก้ว", "10 min") plus their Thai/English counterparts,
   proven BYTE-IDENTICAL in downstream confirmation against the real,
   unmodified `handle_inbound_message` confirmation pipeline (`main.py`,
   not touched by this module) on a scratch on-disk DB -- exactly the
   verification this task requires, without wiring `deterministic_parse`
   into `main.py` (that's integration's job, not `preparse`'s, per
   SPEC-v1.5.md §11's module split).
2. Unit coverage (AC-14): every unit/alias `core/units.build_unit_lookup`
   derives from the default registry resolves correctly.
3. Zero-false-positive adversarial corpus (AC-15): bare numbers, unknown
   units, multi-clause sentences, label-first phrasing, questions, target
   phrasings, commands, and boundary values (0/negative/huge/decimal/
   comma) -- including the historical near-miss shapes from
   `tests/test_commands.py`'s own AC5.5 corpus and the Thai-alias-false-
   positive lessons from `tests/test_schedules.py`/`tests/test_
   preferences.py` (v1.2/v1.3), plus all 4 habit kinds (numeric, duration,
   text, boolean) via a purpose-built registry.
4. Structural zero-LLM proof (AC-14): `deterministic_parse`'s own
   signature carries no llm/channel/db parameter, and running it with a
   raising `OllamaClient` double patched in never trips it.
"""

from __future__ import annotations

import inspect

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.preparse import deterministic_parse
from habit_assistant.core.units import build_unit_lookup
from habit_assistant.llm.ollama_client import ExtractionResult, OllamaClient
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


# ---------------------------------------------------------------------------
# Shared fixtures / fakes (mirrors tests/test_commands.py's own conventions)
# ---------------------------------------------------------------------------


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class _NeverCalledLLM:
    """The `llm=` argument `handle_inbound_message` requires -- never
    actually touched below since `parse_message` is always monkeypatched
    to bypass it (mirrors tests/test_commands.py's own fixture)."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("must never be called -- parse_message is monkeypatched in every test here")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("must never be called -- parse_message is monkeypatched in every test here")


def _fake_parse_message_returning(result: ExtractionResult):
    async def fake(text, llm, registry, confidence_threshold=None):
        return result

    return fake


@pytest.fixture
def fixed_clock():
    from datetime import datetime

    def clock():
        return datetime(2026, 8, 23, 9, 0, 0)

    return clock


async def _confirmation_for(tmp_path, name: str, text: str, result: ExtractionResult, clock, monkeypatch) -> list[str]:
    """Drive `text` through the REAL, unmodified `handle_inbound_message`
    confirmation pipeline on a fresh scratch DB, with `parse_message`
    monkeypatched to return `result` -- i.e. simulate "the LLM (or the
    pre-parser) extracted exactly `result` for this text" and capture what
    the existing, untouched confirmation code sends. Used twice per shape
    (once per candidate origin of `result`) to prove byte-identity."""
    monkeypatch.setattr("habit_assistant.main.parse_message", _fake_parse_message_returning(result))
    db = Database(tmp_path / f"{name}.db")
    channel = FakeChannel()
    try:
        await handle_inbound_message(
            text, db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=clock
        )
    finally:
        db.close()
    return channel.sent


# ===========================================================================
# 1. Supported shapes -- byte-identical confirmation proof (AC-14).
# ===========================================================================

SUPPORTED_SHAPES = [
    ("500ml", "water", 500.0),
    ("500 ml", "water", 500.0),
    ("2 แก้ว", "water", 500.0),  # Thai alias, glass = 250ml
    ("2 ขวด", "water", 1200.0),  # Thai alias, bottle = 600ml
    ("2 bottle", "water", 1200.0),
    ("10 min", "stretch", 10.0),
    ("10 นาที", "stretch", 10.0),
    ("2.5 มล.", "water", 2.5),
]


@pytest.mark.parametrize("text,category,value", SUPPORTED_SHAPES)
def test_ac14_deterministic_parse_produces_the_expected_result(text, category, value):
    result = deterministic_parse(text, DEFAULT_REGISTRY)
    assert result == ExtractionResult(category, value, 1.0)


@pytest.mark.parametrize("text,category,value", SUPPORTED_SHAPES)
async def test_ac14_confirmation_is_byte_identical_to_the_llm_path(tmp_path, fixed_clock, monkeypatch, text, category, value):
    """The pre-parser's own result (confidence=1.0) and a genuine-LLM-style
    result carrying the SAME category/value but a DIFFERENT confidence
    (0.81, as a real model response would) must produce byte-identical
    confirmations through the real `handle_inbound_message` pipeline --
    proving confirmation shape depends only on (category, value), so the
    pre-parser is a safe, invisible substitute for the LLM path (R-L2)."""
    preparsed = deterministic_parse(text, DEFAULT_REGISTRY)
    assert preparsed == ExtractionResult(category, value, 1.0)

    llm_style_result = ExtractionResult(category, value, 0.81)

    sent_llm_path = await _confirmation_for(tmp_path, "llm", text, llm_style_result, fixed_clock, monkeypatch)
    sent_preparse_path = await _confirmation_for(tmp_path, "preparse", text, preparsed, fixed_clock, monkeypatch)

    assert len(sent_llm_path) == 1
    assert sent_llm_path == sent_preparse_path


# ===========================================================================
# 2. Unit coverage from the registry (AC-14): every unit/alias
# `build_unit_lookup` derives from the shipped default registry resolves
# through `deterministic_parse`, systematically -- not just the three
# spec-named examples above.
# ===========================================================================


def test_ac14_every_registered_unit_and_alias_resolves_via_deterministic_parse():
    lookup = build_unit_lookup(DEFAULT_REGISTRY)
    assert lookup  # sanity: the default registry does configure units

    for unit_token, (habit_id, multiplier) in lookup.items():
        text = f"3 {unit_token}"
        result = deterministic_parse(text, DEFAULT_REGISTRY)
        assert result == ExtractionResult(habit_id, 3.0 * multiplier, 1.0), f"failed for unit token {unit_token!r}"


# ===========================================================================
# 3. Zero-false-positive adversarial corpus (AC-15).
# ===========================================================================


def _habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_,
        label_th=id_,
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


FOUR_KIND_REGISTRY = HabitRegistry(
    [
        _habit("water", "numeric", unit_en="ml", unit_th="มล.", unit_aliases={"glass": 250.0, "แก้ว": 250.0}),
        _habit("stretch", "duration", unit_en="min", unit_th="นาที"),
        _habit("diary", "text"),
        _habit("meditated", "boolean"),
    ]
)

# Bare numbers -- no unit at all. SPEC-v1.5.md §9's own recorded decision:
# "a bare-number win is deliberately forgone for safety" -- always None,
# regardless of registry.
BARE_NUMBER_MESSAGES = ["500", "2", "0", "-5", "2.5", "1"]

# Unrecognized/unregistered unit tokens.
UNKNOWN_UNIT_MESSAGES = ["500xyz", "5 kg", "10 furlongs", "3 units", "7 liters", "1 meditated", "500 diary"]

# Boundary values that DO carry a real, registered unit but must still fail
# (zero/negative value, or a shape VALUE_RE can't parse at all).
BOUNDARY_FAILURE_MESSAGES = [
    "0 ml",
    "0ml",
    "-5 ml",
    "-2.5 แก้ว",
    "1,500 ml",
    "1,500ml",
    "2,000 ml",
    "",
    "   ",
]

# Multi-clause sentences / extra words wrapped around a real NUMBER+UNIT --
# whole-message anchoring must reject every one of these (the entire point
# of R-L1's "whole-message-anchored" contract).
MULTI_CLAUSE_MESSAGES = [
    "did 10 min stretch",
    "made 3 bottles of juice",
    "ดื่มน้ำ 2 แก้ว",  # v1.2/v1.3 Thai-alias false-positive shape, mid-sentence
    "drank 500ml of water",
    "500ml please log this",
    "I drank about 500 ml today",
    "500 ml, roughly",
]

# Label-first phrasing -- explicitly out of scope (SPEC-v1.5.md §10).
LABEL_FIRST_MESSAGES = ["น้ำ 500", "water 500", "water 500ml", "stretch 10 min"]

# Questions (v0.8.0's own query corpus, reused verbatim from
# tests/test_commands.py's QUERY_MESSAGES).
QUESTION_MESSAGES = [
    "how much water this week?",
    "How Many times did I stretch today?",
    "did I journal yesterday?",
    "have I logged water today?",
    "อาทิตย์นี้ยืดกี่ครั้ง",
    "วันนี้ดื่มน้ำไปเท่าไหร่",
    "ดื่มน้ำครบไหม",
    "เขียนไดอารี่หรือยัง",
    "what's my water total?",
]

# Target phrasings -- full-NL target-setting is a different module's job
# (core/target_nl.py), explicitly out of scope here (SPEC-v1.5.md §10).
TARGET_PHRASING_MESSAGES = [
    "from now on 2.5L a day",
    "from now on I want to drink 2.5L a day",
    "set water goal to 2000ml",
    "change stretch's goal to 20 min",
    "ตั้งเป้า น้ำ 2000",
    "เป้า น้ำ 2000",
    "ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร",
    "/target water 2000",
    "/target water 2000ml",
]

# Slash / explicit anchored commands, and the historical near-miss
# substrings that once tripped up commands.py's own matchers (v1.2/v1.3
# Thai-alias battles -- see tests/test_commands.py's ADVERSARIAL_MESSAGES
# and tests/test_schedules.py's post-audit fix commentary).
COMMAND_AND_NEAR_MISS_MESSAGES = [
    "/undo",
    "/quiet 22:00-07:00",
    "/dnd off",
    "/dnd 22:00-07:00",
    "/checkin on",
    "/checkin 09:00-18:00",
    "/remind water 09:00",
    "/help",
    "/habits",
    "undo last",
    "ยกเลิกอันล่าสุด",
    "make that 250ml",
    "change it to 300ml",
    "today I had to undo a mistake at work",
    "เลิกงานแล้ว เหนื่อยมาก",
    "I finally decided to cancel my gym membership",
    "ยกเลิกการนัดหมายพรุ่งนี้",
    "เตือน ๆ หน่อยนะ",
    "เตือนฉันด้วยนะ",
    "เงียบไปเลย ไม่อยากคุย",
    "ภาษาอังกฤษยากมากวันนี้",
    "งดรบกวนหน่อยนะ",
]

ADVERSARIAL_MESSAGES = (
    BARE_NUMBER_MESSAGES
    + UNKNOWN_UNIT_MESSAGES
    + BOUNDARY_FAILURE_MESSAGES
    + MULTI_CLAUSE_MESSAGES
    + LABEL_FIRST_MESSAGES
    + QUESTION_MESSAGES
    + TARGET_PHRASING_MESSAGES
    + COMMAND_AND_NEAR_MISS_MESSAGES
)


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_ac15_adversarial_corpus_never_produces_a_false_positive(message):
    assert deterministic_parse(message, DEFAULT_REGISTRY) is None


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_ac15_adversarial_corpus_never_produces_a_false_positive_four_kind_registry(message):
    """Same corpus, re-run against a registry that additionally has a
    `text` and a `boolean` habit -- proves the extra habit kinds (which
    `build_unit_lookup` already excludes, tests/test_units.py's own
    coverage) don't open any new false-positive surface for the
    pre-parser specifically."""
    assert deterministic_parse(message, FOUR_KIND_REGISTRY) is None


def test_ac15_boundary_huge_and_decimal_values_are_supported_not_false_positives():
    """The flip side of the boundary corpus above: huge numbers and
    decimals ARE legitimate NUMBER+UNIT logs and must still resolve --
    only zero/negative/comma-separated are the actual failure boundary."""
    assert deterministic_parse("999999999 ml", DEFAULT_REGISTRY) == ExtractionResult("water", 999999999.0, 1.0)
    assert deterministic_parse("2.5 ml", DEFAULT_REGISTRY) == ExtractionResult("water", 2.5, 1.0)
    assert deterministic_parse("0.5 ml", DEFAULT_REGISTRY) == ExtractionResult("water", 0.5, 1.0)


# ===========================================================================
# 4. Structural zero-LLM proof (AC-14).
# ===========================================================================


class _RaisingLLM:
    """A raising double for `OllamaClient`'s own async methods -- mirrors
    tests/test_commands.py's `_NeverCalledLLM` idiom. Patched directly onto
    the real `OllamaClient` class below (not just passed as an argument,
    since `deterministic_parse` doesn't accept one) so that even an
    incidental, already-constructed client instance sitting in scope can't
    silently be used without tripping this."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("deterministic_parse must never call the LLM")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("deterministic_parse must never call the LLM")


def test_ac14_deterministic_parse_never_touches_an_llm_even_with_a_raising_double_patched_in(monkeypatch):
    monkeypatch.setattr(OllamaClient, "chat_json", _RaisingLLM.chat_json)
    monkeypatch.setattr(OllamaClient, "chat_text", _RaisingLLM.chat_text)

    for text, category, value in SUPPORTED_SHAPES:
        result = deterministic_parse(text, DEFAULT_REGISTRY)
        assert result == ExtractionResult(category, value, 1.0)

    for message in ADVERSARIAL_MESSAGES:
        assert deterministic_parse(message, DEFAULT_REGISTRY) is None


def test_ac14_deterministic_parse_signature_has_no_llm_channel_or_db_parameter():
    params = list(inspect.signature(deterministic_parse).parameters)
    assert params == ["text", "registry"]
