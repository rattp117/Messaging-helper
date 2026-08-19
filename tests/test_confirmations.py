"""Confirmation-message tests (AC6): exact SPEC.md §6 formats, including the
running water total/percentage, stretch ordinal, diary reflection, and the
unknown -> clarifying question / no DB row path.

Uses a fake Channel (captures sent text) + a fake OllamaClient (canned
responses) + a real on-disk SQLite DB (tmp_path), per parser tests already
covering the LLM extraction layer in isolation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Awaitable, Callable

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.llm.ollama_client import ExtractionResult, OllamaClient
from habit_assistant.main import CLARIFYING_QUESTION, handle_inbound_message, ordinal
from habit_assistant.storage.db import Database


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    """Stand-in for OllamaClient: parse_message is monkeypatched to bypass
    the real LLM call entirely (these tests are about confirmation
    formatting, not extraction -- that's test_parser.py's job), and
    chat_text is used for the diary reflection line."""

    def __init__(self, reflection: str | None = "Sounds like a good day!"):
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
    async def fake_parse_message(text, llm, glass_ml, bottle_ml, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


# ---------------------------------------------------------------------------
# Water confirmation (SPEC §6): "✅ 500 ml logged — today 1500 / 2500 ml (60%)"
# ---------------------------------------------------------------------------


async def test_water_confirmation_format_first_log(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, None, None, 0.9))
    channel = FakeChannel()
    config = Config()  # goal_ml = 2500

    await handle_inbound_message(
        "500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]


async def test_water_confirmation_running_total_accumulates(db, fixed_clock, monkeypatch):
    channel = FakeChannel()
    config = Config()

    patch_parse_message(monkeypatch, ExtractionResult("water", 500, None, None, 0.9))
    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)
    patch_parse_message(monkeypatch, ExtractionResult("water", 1000, None, None, 0.9))
    await handle_inbound_message("1000ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)

    assert channel.sent[-1] == "✅ 1000 ml logged — today 1500 / 2500 ml (60%)"


async def test_water_confirmation_uses_configured_goal(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 100, None, None, 0.9))
    channel = FakeChannel()
    config = Config.model_validate({"reminders": {"water": {"goal_ml": 1000}}})

    await handle_inbound_message("100ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)

    assert channel.sent == ["✅ 100 ml logged — today 100 / 1000 ml (10%)"]


async def test_water_log_writes_one_row(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, None, None, 0.9))
    channel = FakeChannel()
    config = Config()

    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    assert rows[0]["category"] == "water"
    assert rows[0]["value_num"] == 500.0
    assert rows[0]["raw_message"] == "500ml"


# ---------------------------------------------------------------------------
# Stretch confirmation (SPEC §6): "✅ 10 min stretch logged — 2nd today"
# ---------------------------------------------------------------------------


async def test_stretch_confirmation_ordinal_first_today(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("stretch", None, 10, None, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "10 min stretch", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == ["✅ 10 min stretch logged — 1st today"]


async def test_stretch_confirmation_ordinal_second_today(db, fixed_clock, monkeypatch):
    channel = FakeChannel()
    config = Config()

    patch_parse_message(monkeypatch, ExtractionResult("stretch", None, 10, None, 0.9))
    await handle_inbound_message("10 min", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)
    patch_parse_message(monkeypatch, ExtractionResult("stretch", None, 5, None, 0.9))
    await handle_inbound_message("5 min", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)

    assert channel.sent[-1] == "✅ 5 min stretch logged — 2nd today"


@pytest.mark.parametrize(
    ("n", "expected"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (101, "101st"), (111, "111th")],
)
def test_ordinal_helper(n, expected):
    assert ordinal(n) == expected


# ---------------------------------------------------------------------------
# Diary confirmation (SPEC §6): "✅ Saved. <one gentle reflection line>"
# ---------------------------------------------------------------------------


async def test_diary_confirmation_uses_llm_reflection(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", None, None, "today was tiring but good", 0.85))
    channel = FakeChannel()

    await handle_inbound_message(
        "today was tiring but good",
        db=db,
        llm=FakeLLM(reflection="Your imagination dances beautifully today!"),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
    )

    assert channel.sent == ["✅ Saved. Your imagination dances beautifully today!"]


async def test_diary_confirmation_falls_back_when_reflection_is_none(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", None, None, "a quiet day", 0.8))
    channel = FakeChannel()

    await handle_inbound_message(
        "a quiet day", db=db, llm=FakeLLM(reflection=None), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == ["✅ Saved. Thanks for sharing — noted."]


async def test_diary_log_writes_value_text_not_value_num(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", None, None, "a quiet day", 0.8))
    channel = FakeChannel()

    await handle_inbound_message(
        "a quiet day", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows[0]["category"] == "diary"
    assert rows[0]["value_num"] is None
    assert rows[0]["value_text"] == "a quiet day"


# ---------------------------------------------------------------------------
# Unknown: clarifying question, no DB row (SPEC §6)
# ---------------------------------------------------------------------------


async def test_unknown_sends_clarifying_question(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    channel = FakeChannel()

    await handle_inbound_message(
        "purple elephants dance sideways", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [CLARIFYING_QUESTION]


async def test_unknown_writes_no_db_row(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    channel = FakeChannel()

    await handle_inbound_message(
        "purple elephants dance sideways", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows == []


async def test_unknown_after_valid_logs_does_not_add_a_row(db, fixed_clock, monkeypatch):
    """Regression guard: an unknown message interleaved with valid ones
    must not silently add a row (row count must only grow on water/stretch/
    diary)."""
    channel = FakeChannel()
    config = Config()

    patch_parse_message(monkeypatch, ExtractionResult("water", 500, None, None, 0.9))
    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    await handle_inbound_message("asdf", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock)

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1  # only the water log


# ---------------------------------------------------------------------------
# --dry-run: parse + print, no DB write, no channel.send (AC9's dry-run half)
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_write_db_or_require_channel(db, fixed_clock, monkeypatch, capsys):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, None, None, 0.9))

    await handle_inbound_message(
        "500ml", db=db, llm=FakeLLM(), channel=None, config=Config(), clock=fixed_clock, dry_run=True
    )

    rows = db.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows == []
    captured = capsys.readouterr()
    assert "water" in captured.out
    assert "500" in captured.out


# ---------------------------------------------------------------------------
# End-to-end through the real parser (mocked Ollama) -- confirms parser +
# confirmation formatting compose correctly, not just each piece alone.
# ---------------------------------------------------------------------------


async def test_end_to_end_water_confirmation_with_mocked_ollama(db, fixed_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {"category": "water", "water_ml": 500, "stretch_min": None, "diary_text": None, "confidence": 0.95}
        )
        return httpx.Response(200, json={"message": {"content": content}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    llm = OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", client=client)
    channel = FakeChannel()
    config = Config()

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=config, clock=fixed_clock)

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]
    await llm.aclose()
