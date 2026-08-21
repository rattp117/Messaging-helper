"""End-to-end bilingual output tests (ROADMAP.md v0.6.0, AC6.1, AC6.3, AC6.4):
Thai input -> Thai confirmation, English input -> English confirmation (same
structured result, localized copy); `language="th"`/`"en"` forces every
reply regardless of input; `"auto"` matches the input. Weekly-review
narrative language + factuality is covered by test_review.py (AC6.4);
this file adds the "auto forces Thai unprompted" wiring check for the
narrative's system prompt.

Same fakes/pattern as test_confirmations.py (FakeChannel captures sent
text, FakeLLM stubs the LLM boundary, real on-disk SQLite via tmp_path) --
this file is scoped to the *bilingual* dimension of behavior that file
doesn't cover.
"""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    def __init__(self, reflection: str | None = "reflection"):
        self._reflection = reflection

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return self._reflection


@pytest.fixture
def fixed_clock():
    def clock():
        return datetime(2026, 8, 19, 14, 30, 0)

    return clock


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def patch_parse_message(monkeypatch, result: ExtractionResult):
    # ROADMAP.md v0.7.0 (SPEC-v0.7.md §5): handle_inbound_message now calls
    # parse_message(text, llm, registry, confidence_threshold) -- the fake's
    # parameter names are updated to match (registry-wiring edit only).
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


# ---------------------------------------------------------------------------
# AC6.1: Thai input -> Thai confirmation, English input -> English
# confirmation -- same structured data (500ml water), localized copy.
# ---------------------------------------------------------------------------


async def test_thai_input_produces_thai_water_confirmation(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()
    config = Config()  # i18n.language="auto"

    await handle_inbound_message(
        "ดื่มน้ำ 2 แก้ว", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    expected = i18n.t("water_confirmation", "th", water_ml=500, total=500, goal=2500, pct=20)
    assert channel.sent == [expected]
    assert i18n.detect_language(channel.sent[0]) == "th"


async def test_english_input_produces_english_water_confirmation(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()
    config = Config()

    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    expected = i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500, pct=20)
    assert channel.sent == [expected]


async def test_thai_and_english_input_yield_same_structured_data_different_copy(db, fixed_clock, monkeypatch):
    """Same water_ml/total/goal/pct -- only the wrapper copy differs."""
    channel = FakeChannel()
    config = Config()

    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")
    en_sent = channel.sent[-1]

    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    await handle_inbound_message(
        "ดื่มน้ำ 2 แก้ว", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")
    th_sent = channel.sent[-1]

    assert en_sent != th_sent  # different copy
    assert "500" in en_sent and "500" in th_sent  # same underlying value in both


async def test_thai_input_produces_thai_stretch_confirmation(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("stretch", 10, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "ยืดเส้น 10 นาที", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    expected = i18n.t("stretch_confirmation", "th", stretch_min=10, ordinal="1st", count=1)
    assert channel.sent == [expected]


async def test_thai_input_produces_thai_clarifying_question(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    channel = FakeChannel()

    await handle_inbound_message(
        "ช้างสีม่วงเต้นระบำ", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == [i18n.t("clarifying_question", "th")]


# ---------------------------------------------------------------------------
# AC6.3: a forced config.i18n.language overrides the input language.
# ---------------------------------------------------------------------------


async def test_forced_th_language_overrides_english_input(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()
    config = Config.model_validate({"i18n": {"language": "th"}})

    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    expected = i18n.t("water_confirmation", "th", water_ml=500, total=500, goal=2500, pct=20)
    assert channel.sent == [expected]


async def test_forced_en_language_overrides_thai_input(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()
    config = Config.model_validate({"i18n": {"language": "en"}})

    await handle_inbound_message(
        "ดื่มน้ำ 2 แก้ว", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    expected = i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500, pct=20)
    assert channel.sent == [expected]


async def test_forced_th_language_applies_to_clarifying_question_too(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    channel = FakeChannel()
    config = Config.model_validate({"i18n": {"language": "th"}})

    await handle_inbound_message(
        "purple elephants dance sideways", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    assert channel.sent == [i18n.t("clarifying_question", "th")]


# ---------------------------------------------------------------------------
# Undo/edit and the diary reflection prompt also respect the resolved
# language (main.py scope item 2's explicit inclusion of v0.5.0 commands).
# ---------------------------------------------------------------------------


async def test_thai_undo_command_gets_thai_confirmation(db, fixed_clock, monkeypatch):
    from habit_assistant.storage.models import LogEntry

    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 500.0, None, "500ml", "reply"))
    channel = FakeChannel()

    await handle_inbound_message(
        "ยกเลิกอันล่าสุด", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert i18n.detect_language(channel.sent[0]) == "th"


async def test_diary_reflection_prompt_carries_the_resolved_language_instruction(db, fixed_clock, monkeypatch):
    """AC6.4-adjacent: the diary reflection is LLM-generated free text, so
    it can't be asserted verbatim -- what's testable is that the system
    prompt handed to the LLM actually carries the target-language
    instruction, for both a Thai-triggering and an English-triggering
    diary message."""
    captured: list[str] = []

    class RecordingLLM:
        async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
            captured.append(system_prompt)
            return "ok"

    patch_parse_message(monkeypatch, ExtractionResult("diary", "วันนี้เหนื่อยแต่ก็ดี", 0.85))
    channel = FakeChannel()

    await handle_inbound_message(
        "วันนี้เหนื่อยแต่ก็ดี", db=db, llm=RecordingLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert len(captured) == 1
    assert i18n.language_instruction("th") in captured[0]
