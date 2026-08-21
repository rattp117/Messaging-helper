"""AC17 [-> AC7.1 composite] (SPEC-v0.7.md SS8): "Given the default config,
When the full suite runs, Then confirmations, reminders, and the weekly
review are jointly byte-identical to v0.6.0."

This file is the integration Vera's own dedicated AC17 check, additional to
(not a replacement for) the preserved pre-v0.7 corpus
(`test_confirmations.py`, `test_bilingual_confirmations.py`,
`test_v060_bilingual_gaps.py`) that already runs unmodified-in-assertion
through the real v0.7 pipeline. What this file adds:

1. It drives the REAL `handle_inbound_message` -> REAL `parse_message` ->
   REAL `core/parser.py` / `llm/prompts.py` / `llm/ollama_client.py` chain,
   mocking only the network boundary (an `httpx.MockTransport` under a real
   `OllamaClient`), not `main.parse_message` itself -- so the registry-built
   schema/prompt and the per-type validation are actually exercised, not
   bypassed.
2. Every expected string below is copied verbatim from
   `git show v0.6.0:src/habit_assistant/core/i18n.py` / the v0.6.0-tagged
   test corpus (see the comment above each literal) -- typed in by hand as
   a Python literal, NOT produced by calling the *current* `i18n.t(...)` or
   any other current-code helper. A regression that changed both the
   catalog and every test that derives its expectation from the catalog
   would slip past a "derive from current catalog" test; it cannot slip
   past a hand-pinned literal.
3. Covers water (English, Thai bare-ml, Thai glass-alias, Thai
   bottle-alias), stretch, diary (English + Thai reflection), unknown
   (English + Thai), undo, and edit in one file, end to end.

Reminders (AC13) and the weekly review (AC15) already have their own
dedicated byte-identical checks (`test_reminders.py`,
`test_v07_m3_review_extra.py`'s live re-derivation against the real
v0.6.0 `core/review.py` module) -- not duplicated here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Awaitable, Callable

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


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


def make_llm(extraction_json: str, reflection_text: str = "noted") -> OllamaClient:
    """A real OllamaClient wired to an httpx.MockTransport (SPEC-v0.7.md
    SS11 integration order step 1: "wire the real parse_message ... into
    main.py's already-generic call sites" -- this exercises exactly that
    real chain, mocking only the network boundary). The handler
    distinguishes chat_json calls (carry `format` in the request body)
    from chat_text calls (diary reflection -- no `format`) so one client
    instance can serve both within a single handle_inbound_message call."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "format" in body:
            return httpx.Response(200, json={"message": {"content": extraction_json}})
        return httpx.Response(200, json={"message": {"content": reflection_text}})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


def extraction(category: str, value, confidence: float = 0.95) -> str:
    return json.dumps({"category": category, "value": value, "confidence": confidence})


# ---------------------------------------------------------------------------
# Water -- English, byte-identical to v0.6.0 (git show v0.6.0:tests/
# test_v060_bilingual_gaps.py::test_english_water_confirmation_byte_
# identical_to_v050).
# ---------------------------------------------------------------------------


async def test_water_english_plain_ml_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("water", 500))

    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]


# git show v0.6.0:src/habit_assistant/core/i18n.py -- "water_confirmation"/th,
# formatted with water_ml=500, total=500, goal=2500, pct=20.
async def test_water_thai_glass_alias_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("water", 500))  # 2 glasses x 250 ml, resolved by the (mocked) LLM

    await handle_inbound_message(
        "ดื่มน้ำ 2 แก้ว", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ บันทึกน้ำ 500 มล. แล้ว — วันนี้ดื่มไป 500 / 2500 มล. (20%)"]


async def test_water_english_bottle_alias_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("water", 600))  # 1 bottle = 600 ml

    await handle_inbound_message(
        "1 bottle of water", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 600 ml logged — today 600 / 2500 ml (24%)"]


async def test_water_thai_bottle_alias_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("water", 600))

    await handle_inbound_message("1 ขวดน้ำ", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ บันทึกน้ำ 600 มล. แล้ว — วันนี้ดื่มไป 600 / 2500 มล. (24%)"]


# ---------------------------------------------------------------------------
# Stretch -- English + Thai, byte-identical to v0.6.0.
# ---------------------------------------------------------------------------


async def test_stretch_english_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("stretch", 10))

    await handle_inbound_message(
        "did 10 min stretch", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 10 min stretch logged — 1st today"]


# git show v0.6.0:src/habit_assistant/core/i18n.py -- "stretch_confirmation"/
# th, formatted with stretch_min=10, count=1 ("ครั้งที่ 1 ของวันนี้").
async def test_stretch_thai_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("stretch", 10))

    await handle_inbound_message(
        "ยืดเส้น 10 นาที", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ บันทึกยืดเส้น 10 นาที แล้ว — ครั้งที่ 1 ของวันนี้"]


# ---------------------------------------------------------------------------
# Diary -- English + Thai. The reflection itself is LLM free text (never
# pinned -- see test_bilingual_confirmations.py's own reasoning); what's
# byte-identical is the *wrapper* copy ("✅ Saved. {reflection}" / "✅
# บันทึกแล้วนะ {reflection}").
# ---------------------------------------------------------------------------


async def test_diary_english_wrapper_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("diary", "today was a good day, felt productive"), reflection_text="Glad to hear it.")

    await handle_inbound_message(
        "today was a good day, felt productive", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ Saved. Glad to hear it."]


async def test_diary_thai_wrapper_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("diary", "วันนี้เหนื่อยแต่ก็ดี"), reflection_text="วันนี้เก่งมากเลยนะ")

    await handle_inbound_message(
        "วันนี้เหนื่อยแต่ก็ดี", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ บันทึกแล้วนะ วันนี้เก่งมากเลยนะ"]


# ---------------------------------------------------------------------------
# Unknown / clarifying question -- English + Thai, byte-identical to
# v0.6.0 (git show v0.6.0:src/habit_assistant/core/i18n.py ->
# "clarifying_question").
# ---------------------------------------------------------------------------


async def test_unknown_english_clarifying_question_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("unknown", None, confidence=0.1))

    await handle_inbound_message(
        "purple elephants dance sideways", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == [
        "🤔 I couldn't quite tell what you meant — was that about water, a stretch "
        "break, or today's diary? Try something like '500ml water' or '10 min stretch'."
    ]


async def test_unknown_thai_clarifying_question_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("unknown", None, confidence=0.1))

    await handle_inbound_message(
        "ช้างสีม่วงเต้นระบำ", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == [
        "🤔 เอ๊ะ ยังไม่แน่ใจว่าหมายถึงอะไรนะ เกี่ยวกับน้ำ ยืดเส้น หรือไดอารี่วันนี้หรือเปล่า "
        "ลองพิมพ์แบบนี้ดูนะ เช่น 'น้ำ 500 มล.' หรือ 'ยืดเส้น 10 นาที'"
    ]


# ---------------------------------------------------------------------------
# Undo / edit -- English, byte-identical to v0.6.0. Both commands are
# LLM-free (core/commands.dispatch runs before parse_message), so the
# setup log below is the only call that touches the mocked LLM.
# ---------------------------------------------------------------------------


async def test_undo_water_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("water", 500))
    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    await handle_inbound_message("/undo", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent[-1] == "↩️ Undone — removed 500 ml water. Today: 0 / 2500 ml (0%)"


async def test_edit_water_byte_identical(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm(extraction("water", 500))
    await handle_inbound_message("500ml", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    await handle_inbound_message(
        "make that 300ml", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent[-1] == "✏️ Updated to 300 ml — today 300 / 2500 ml (12%)"
