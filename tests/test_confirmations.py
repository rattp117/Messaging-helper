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
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.llm.ollama_client import ExtractionResult, OllamaClient
from habit_assistant.main import CLARIFYING_QUESTION, handle_inbound_message, ordinal
from habit_assistant.storage.db import Database


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
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
    # ROADMAP.md v0.7.0 (SPEC-v0.7.md §5): handle_inbound_message now calls
    # parse_message(text, llm, registry, confidence_threshold) -- the fake's
    # parameter names are updated to match (registry-wiring edit only, the
    # body/behavior is unchanged: still just returns the canned `result`).
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


# ---------------------------------------------------------------------------
# Water confirmation (SPEC §6): "✅ 500 ml logged — today 1500 / 2500 ml (60%)"
# ---------------------------------------------------------------------------


async def test_water_confirmation_format_first_log(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()
    config = Config()  # goal_ml = 2500

    await handle_inbound_message(
        "500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]


async def test_water_confirmation_running_total_accumulates(db, fixed_clock, monkeypatch):
    channel = FakeChannel()
    config = Config()

    # SPEC-v1.6.md R-R2 (module `insights`, integration-wired): a fresh
    # user+habit's first-ever log seeds a `best_day`/`best_week` baseline
    # silently, so this test's own SECOND same-day log (a higher running
    # total) would otherwise genuinely break it and append a celebration
    # line -- real, correct v1.6.0 behavior, but not what THIS test means
    # to exercise (the running-total/percentage text format). Pre-seeding
    # both record types comfortably above anything these two logs reach
    # keeps the assertion below testing exactly what it always tested.
    db.upsert_record("owner", "water", "best_day", 999999.0, "2000-01-01")
    db.upsert_record("owner", "water", "best_week", 999999.0, "2000-01-01")

    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")
    patch_parse_message(monkeypatch, ExtractionResult("water", 1000, 0.9))
    await handle_inbound_message("1000ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    assert channel.sent[-1] == "✅ 1000 ml logged — today 1500 / 2500 ml (60%)"


async def test_water_confirmation_uses_configured_goal(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 100, 0.9))
    channel = FakeChannel()
    config = Config.model_validate({"reminders": {"water": {"goal_ml": 1000}}})

    await handle_inbound_message("100ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 100 ml logged — today 100 / 1000 ml (10%)"]


async def test_water_log_writes_one_row(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()
    config = Config()

    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    assert rows[0]["category"] == "water"
    assert rows[0]["value_num"] == 500.0
    assert rows[0]["raw_message"] == "500ml"


# ---------------------------------------------------------------------------
# Stretch confirmation (SPEC §6): "✅ 10 min stretch logged — 2nd today"
# ---------------------------------------------------------------------------


async def test_stretch_confirmation_ordinal_first_today(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("stretch", 10, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "10 min stretch", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 10 min stretch logged — 1st today"]


async def test_stretch_confirmation_ordinal_second_today(db, fixed_clock, monkeypatch):
    channel = FakeChannel()
    config = Config()

    # SPEC-v1.6.md R-R2 (module `insights`, integration-wired): see the
    # identical pre-seed note in test_water_confirmation_running_total_
    # accumulates above -- a fresh user+habit's second same-day log would
    # otherwise genuinely break a just-seeded best_day/best_week record
    # and append a celebration line; this test is about the ordinal
    # count, not records.
    db.upsert_record("owner", "stretch", "best_day", 999999.0, "2000-01-01")
    db.upsert_record("owner", "stretch", "best_week", 999999.0, "2000-01-01")

    patch_parse_message(monkeypatch, ExtractionResult("stretch", 10, 0.9))
    await handle_inbound_message("10 min", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")
    patch_parse_message(monkeypatch, ExtractionResult("stretch", 5, 0.9))
    await handle_inbound_message("5 min", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

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
    patch_parse_message(monkeypatch, ExtractionResult("diary", "today was tiring but good", 0.85))
    channel = FakeChannel()

    await handle_inbound_message(
        "today was tiring but good",
        db=db,
        llm=FakeLLM(reflection="Your imagination dances beautifully today!"),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
    user_id="owner")

    assert channel.sent == ["✅ Saved. Your imagination dances beautifully today!"]


async def test_diary_confirmation_falls_back_when_reflection_is_none(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", "a quiet day", 0.8))
    channel = FakeChannel()

    await handle_inbound_message(
        "a quiet day", db=db, llm=FakeLLM(reflection=None), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ Saved. Thanks for sharing — noted."]


async def test_diary_log_writes_value_text_not_value_num(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", "a quiet day", 0.8))
    channel = FakeChannel()

    await handle_inbound_message(
        "a quiet day", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
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
        "purple elephants dance sideways", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == [CLARIFYING_QUESTION]


async def test_unknown_writes_no_db_row(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    channel = FakeChannel()

    await handle_inbound_message(
        "purple elephants dance sideways", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows == []


async def test_unknown_after_valid_logs_does_not_add_a_row(db, fixed_clock, monkeypatch):
    """Regression guard: an unknown message interleaved with valid ones
    must not silently add a row (row count must only grow on water/stretch/
    diary)."""
    channel = FakeChannel()
    config = Config()

    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    await handle_inbound_message("asdf", db=db, llm=FakeLLM(), channel=channel, config=config, clock=fixed_clock, user_id="owner")

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1  # only the water log


# ---------------------------------------------------------------------------
# --dry-run: parse + print, no DB write, no channel.send (AC9's dry-run half)
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_write_db_or_require_channel(db, fixed_clock, monkeypatch, capsys):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))

    await handle_inbound_message(
        "500ml", db=db, llm=FakeLLM(), channel=None, config=Config(), clock=fixed_clock, dry_run=True, user_id="owner")

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert rows == []
    captured = capsys.readouterr()
    assert "water" in captured.out
    assert "500" in captured.out


# ---------------------------------------------------------------------------
# End-to-end through the real parser (mocked Ollama) -- confirms parser +
# confirmation formatting compose correctly, not just each piece alone.
# ---------------------------------------------------------------------------


# UNSKIPPED (ROADMAP.md v0.7.0 integration): module M1's core/parser.py has
# landed the registry-aware parse_message/_validate, so the REAL parser is
# no longer frozen (was previously @pytest.mark.skip'd during the
# shared-surface build -- see IMPL.md's v0.7.0 "Known limitations"). The
# mocked HTTP response below is updated to the new generic `value` schema
# key (was `water_ml`/`stretch_min`/`diary_text`), matching
# `build_extraction_schema`'s output.
async def test_end_to_end_water_confirmation_with_mocked_ollama(db, fixed_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"category": "water", "value": 500, "confidence": 0.95})
        return httpx.Response(200, json={"message": {"content": content}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    llm = OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", client=client)
    channel = FakeChannel()
    config = Config()

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=config, clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]
    await llm.aclose()


# ---------------------------------------------------------------------------
# AC9 -- type-generic confirmations for a habit that is NOT one of the three
# built-ins: numeric+goal, numeric-no-goal, duration, text, boolean
# (SPEC-v0.7.md §3.2). Each uses a synthetic registry (a real Habit, not
# going through Config/HabitConfig) so these are independent of what the
# default config.toml ships (SPEC-v0.7.md §9 risk 5: boolean/no-goal-numeric
# have no shipped habit).
# ---------------------------------------------------------------------------


def _synthetic_habit(
    id_: str,
    type_: str,
    *,
    goal=None,
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    label_en: str | None = None,
    label_th: str | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en or id_,
        label_th=label_th or id_,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


async def test_generic_numeric_with_goal_confirmation_matches_spec_example(db, fixed_clock, monkeypatch):
    """SPEC-v0.7.md §3.2: "sleep (en, numeric+goal): ✅ 7 h logged — today
    7 / 8 h (88%)"."""
    sleep = _synthetic_habit("sleep", "numeric", goal=8, unit_en="h", unit_th="ชม.", label_en="sleep", label_th="นอน")
    registry = HabitRegistry([sleep])
    patch_parse_message(monkeypatch, ExtractionResult("sleep", 7, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "slept 7h", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")

    assert channel.sent == ["✅ 7 h logged — today 7 / 8 h (88%)"]


async def test_generic_numeric_with_goal_confirmation_thai(db, fixed_clock, monkeypatch):
    sleep = _synthetic_habit("sleep", "numeric", goal=8, unit_en="h", unit_th="ชม.", label_en="sleep", label_th="นอน")
    registry = HabitRegistry([sleep])
    patch_parse_message(monkeypatch, ExtractionResult("sleep", 7, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "ดื่มน้ำ", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")
    # "ดื่มน้ำ" is Thai-detected input, driving the Thai confirmation copy.

    assert channel.sent == ["✅ บันทึกนอน 7 ชม. แล้ว — วันนี้ 7 / 8 ชม. (88%)"]


async def test_generic_numeric_without_goal_confirmation_matches_spec_example(db, fixed_clock, monkeypatch):
    """SPEC-v0.7.md §3.2: "steps (en, numeric no-goal): ✅ 8000 steps
    logged today"."""
    steps = _synthetic_habit("steps", "numeric", goal=None, unit_en="steps", unit_th="ก้าว")
    registry = HabitRegistry([steps])
    patch_parse_message(monkeypatch, ExtractionResult("steps", 8000, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "8000 steps", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")

    assert channel.sent == ["✅ 8000 steps logged today"]


async def test_generic_duration_confirmation_ordinal(db, fixed_clock, monkeypatch):
    yoga = _synthetic_habit("yoga", "duration", unit_en="min", unit_th="นาที", label_en="yoga", label_th="โยคะ")
    registry = HabitRegistry([yoga])
    channel = FakeChannel()

    # SPEC-v1.6.md R-R2 (module `insights`, integration-wired): see the
    # identical pre-seed note in test_water_confirmation_running_total_
    # accumulates above -- this test is about the ordinal count, not records.
    db.upsert_record("owner", "yoga", "best_day", 999999.0, "2000-01-01")
    db.upsert_record("owner", "yoga", "best_week", 999999.0, "2000-01-01")

    patch_parse_message(monkeypatch, ExtractionResult("yoga", 20, 0.9))
    await handle_inbound_message(
        "20 min yoga", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")
    patch_parse_message(monkeypatch, ExtractionResult("yoga", 15, 0.9))
    await handle_inbound_message(
        "15 min yoga", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")

    assert channel.sent == [
        "✅ 20 min yoga logged — 1st today",
        "✅ 15 min yoga logged — 2nd today",
    ]


async def test_generic_text_confirmation_uses_llm_reflection(db, fixed_clock, monkeypatch):
    gratitude = _synthetic_habit("gratitude", "text", label_en="gratitude", label_th="ขอบคุณ")
    registry = HabitRegistry([gratitude])
    patch_parse_message(monkeypatch, ExtractionResult("gratitude", "grateful for my friends", 0.85))
    channel = FakeChannel()

    await handle_inbound_message(
        "grateful for my friends",
        db=db,
        llm=FakeLLM(reflection="What a lovely thing to notice!"),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    user_id="owner")

    assert channel.sent == ["✅ gratitude saved. What a lovely thing to notice!"]


async def test_generic_boolean_confirmation_matches_spec_example(db, fixed_clock, monkeypatch):
    """SPEC-v0.7.md §3.2: "meds (en, boolean): ✅ meds — done today"."""
    meds = _synthetic_habit("meds", "boolean", label_en="meds", label_th="ยา")
    registry = HabitRegistry([meds])
    patch_parse_message(monkeypatch, ExtractionResult("meds", True, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "took my meds", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")

    assert channel.sent == ["✅ meds — done today"]


async def test_generic_boolean_confirmation_not_done(db, fixed_clock, monkeypatch):
    meds = _synthetic_habit("meds", "boolean", label_en="meds", label_th="ยา")
    registry = HabitRegistry([meds])
    patch_parse_message(monkeypatch, ExtractionResult("meds", False, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "no meds yet", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")

    assert channel.sent == ["✅ meds — not done today"]


async def test_generic_habit_row_written_with_correct_habit_type(db, fixed_clock, monkeypatch):
    sleep = _synthetic_habit("sleep", "numeric", goal=8, unit_en="h", unit_th="ชม.")
    registry = HabitRegistry([sleep])
    patch_parse_message(monkeypatch, ExtractionResult("sleep", 7, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "7h sleep", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, registry=registry, user_id="owner")

    rows = db.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1
    assert rows[0]["category"] == "sleep"
    assert rows[0]["value_num"] == 7.0
    assert rows[0]["habit_type"] == "numeric"
