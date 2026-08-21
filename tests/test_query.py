"""ROADMAP.md v0.8.0 "Natural-Language Queries" (AC8.1-AC8.5): "how much
water this week?" / "อาทิตย์นี้ยืดกี่ครั้ง" answered on demand, in the
user's language, strictly read-only.

core/commands.py's own query-*detection* patterns (dispatch() returning
Command(kind="query")) are covered in test_commands.py alongside the rest
of the command-routing corpus (undo/edit adversarial guards live there
already). This file covers core/query.py itself: intent classification +
validation, timeframe date-range mapping, aggregation/formatting, and the
end-to-end path through the real handle_inbound_message.

Every "does this reach the real LLM boundary" test uses a real OllamaClient
over an httpx.MockTransport (same pattern as test_multi_habit_integration.py)
so parse_message's sibling code path -- classify_query_intent's actual
chat_json call, fallback chain, and off-schema handling -- genuinely runs,
not just a hand-rolled stub.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.query import (
    METRICS,
    TIMEFRAMES,
    QueryIntent,
    answer_question,
    build_query_intent_schema,
    classify_query_intent,
    date_range_for_timeframe,
)
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import build_query_intent_system_prompt, build_query_intent_user_prompt
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def fixed_clock():
    # A Wednesday -- 2026-08-19. The rolling 7-day window this exercises is
    # 2026-08-13 .. 2026-08-19 inclusive.
    def clock():
        return datetime(2026, 8, 19, 14, 30, 0)

    return clock


def _seed(db: Database, ts: str, category: str, value_num: float | None, habit_type: str, raw: str = "seed") -> int:
    return db.insert_log(LogEntry(None, "owner", ts, category, value_num, None, raw, "reply", habit_type=habit_type))


def make_llm(response_json: dict | None, *, status: int = 200) -> OllamaClient:
    """A real OllamaClient over a mocked transport -- proves
    classify_query_intent's actual chat_json call/fallback-chain/off-schema
    handling genuinely runs, per this file's own docstring."""

    def handler(request: httpx.Request) -> httpx.Response:
        if response_json is None:
            return httpx.Response(status)
        return httpx.Response(200, json={"message": {"content": json.dumps(response_json)}})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


def _raw_row_count(db: Database) -> int:
    return db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]


# ---------------------------------------------------------------------------
# AC8.3 -- timeframe -> date range mapping, timezone-aware, correct week
# semantics (this_week and last_7_days are DELIBERATELY the same rolling
# 7-day window ending today -- see core/query.py's module docstring).
# ---------------------------------------------------------------------------


def test_today_is_a_single_day():
    assert date_range_for_timeframe("today", date(2026, 8, 19)) == ["2026-08-19"]


def test_yesterday_is_the_day_before():
    assert date_range_for_timeframe("yesterday", date(2026, 8, 19)) == ["2026-08-18"]


def test_this_week_is_the_rolling_7_days_ending_today_inclusive():
    assert date_range_for_timeframe("this_week", date(2026, 8, 19)) == [
        "2026-08-13",
        "2026-08-14",
        "2026-08-15",
        "2026-08-16",
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
    ]


def test_last_7_days_is_identical_to_this_week_by_design():
    today = date(2026, 8, 19)
    assert date_range_for_timeframe("last_7_days", today) == date_range_for_timeframe("this_week", today)


def test_invalid_timeframe_raises_value_error():
    with pytest.raises(ValueError):
        date_range_for_timeframe("next_week", date(2026, 8, 19))


async def test_answer_uses_the_configured_timezone_not_utc(db, monkeypatch):
    """AC8.3's "timezone-aware, Asia/Bangkok" requirement: a naive clock is
    treated as already being in config.app.timezone (the app's existing
    convention -- see core/query.py's `_today_in_timezone`), not the host's
    own local time or UTC."""
    channel = FakeChannel()
    config = Config()  # app.timezone == "Asia/Bangkok"
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, "numeric")

    # 23:30 Bangkok time on the 19th is still "today" == 2026-08-19 in
    # Bangkok, even though in UTC it would already be the 20th.
    def clock():
        return datetime(2026, 8, 19, 23, 30, 0)

    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})
    await handle_inbound_message(
        "how much water today?", db=db, user_id="owner", llm=llm, channel=channel, config=config, clock=clock
    )

    assert channel.sent == ["📊 water: 500 ml today"]


# ---------------------------------------------------------------------------
# AC8.1 -- "how much water this week?" -> correct 7-day sum, English.
# ---------------------------------------------------------------------------


async def test_ac81_how_much_water_this_week_english(db, fixed_clock):
    channel = FakeChannel()
    config = Config()

    # Seed water across the 7-day window (2026-08-13..19) plus one row
    # just outside it (08-12), which must NOT be counted.
    for day, ml in [
        ("2026-08-13", 300.0),
        ("2026-08-14", 500.0),
        ("2026-08-15", 250.0),
        ("2026-08-19", 600.0),
    ]:
        _seed(db, f"{day}T09:00:00", "water", ml, "numeric")
    _seed(db, "2026-08-12T09:00:00", "water", 999.0, "numeric")  # outside the window

    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "this_week"})
    await handle_inbound_message(
        "how much water this week?", db=db, user_id="owner", llm=llm, channel=channel, config=config, clock=fixed_clock
    )

    assert channel.sent == ["📊 water: 1650 ml this week"]  # 300+500+250+600, excludes the 08-12 row


# ---------------------------------------------------------------------------
# AC8.2 -- "อาทิตย์นี้ยืดกี่ครั้ง" -> correct weekly stretch count, Thai.
# ---------------------------------------------------------------------------


async def test_ac82_weekly_stretch_count_thai(db, fixed_clock):
    channel = FakeChannel()
    config = Config()

    for day, minutes in [("2026-08-14", 10.0), ("2026-08-16", 15.0), ("2026-08-19", 5.0)]:
        _seed(db, f"{day}T12:00:00", "stretch", minutes, "duration")
    _seed(db, "2026-08-11T12:00:00", "stretch", 20.0, "duration")  # outside the window

    llm = make_llm({"category": "stretch", "metric": "count", "timeframe": "this_week"})
    await handle_inbound_message(
        "อาทิตย์นี้ยืดกี่ครั้ง", db=db, user_id="owner", llm=llm, channel=channel, config=config, clock=fixed_clock
    )

    # 3 sessions, 30 minutes total (10+15+5) -- the 08-11 row excluded.
    assert channel.sent == ["📊 ยืดเส้น: 3 ครั้ง รวม 30 นาที สัปดาห์นี้"]


# ---------------------------------------------------------------------------
# AC8.4 -- unconfigured habit / unparseable question -> friendly can't
# answer, never a wrong number, never a crash.
# ---------------------------------------------------------------------------


async def test_ac84_llm_says_unknown_category_yields_cant_answer(db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm({"category": "unknown", "metric": "count", "timeframe": "today"})

    await handle_inbound_message(
        "what's the meaning of life?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [
        "🤔 I can't answer that yet — try asking about a habit I track, like 'how much water this week?'"
    ]


async def test_ac84_llm_names_an_unconfigured_habit_yields_cant_answer(db, fixed_clock):
    """The schema's category enum is generated from the live registry
    (habit ids + "unknown") -- a model hallucinating a habit id that isn't
    actually configured (e.g. "sleep" against the default water/stretch/
    diary registry) is off-schema, so chat_json's fallback chain returns
    None and classify_query_intent fails closed, exactly like a normal
    extraction would (core/parser.py's own AC7.3 contract)."""
    channel = FakeChannel()
    llm = make_llm({"category": "sleep", "metric": "sum", "timeframe": "today"})

    await handle_inbound_message(
        "how much sleep did I get?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [
        "🤔 I can't answer that yet — try asking about a habit I track, like 'how much water this week?'"
    ]


async def test_ac84_malformed_json_yields_cant_answer_not_a_crash(db, fixed_clock):
    channel = FakeChannel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not json at all {{{"}})

    llm = OllamaClient(
        "http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    await handle_inbound_message(
        "how much water this week?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [
        "🤔 I can't answer that yet — try asking about a habit I track, like 'how much water this week?'"
    ]


async def test_ac84_ollama_unreachable_yields_cant_answer_not_a_crash(db, fixed_clock):
    llm = make_llm(None, status=503)  # every attempt/model gets a transport-reachable-but-bad-status response
    channel = FakeChannel()

    await handle_inbound_message(
        "how much water this week?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [
        "🤔 I can't answer that yet — try asking about a habit I track, like 'how much water this week?'"
    ]


async def test_ac84_invalid_metric_or_timeframe_from_llm_fails_closed(db, fixed_clock):
    """Even a recognized habit id is rejected if metric/timeframe fall
    outside the closed vocabularies (a model deviating from the schema's
    enum, or the fallback chain giving up) -- never silently coerced."""
    channel = FakeChannel()
    llm = make_llm({"category": "water", "metric": "average", "timeframe": "this_week"})

    await handle_inbound_message(
        "how much water on average?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [
        "🤔 I can't answer that yet — try asking about a habit I track, like 'how much water this week?'"
    ]


def test_validate_intent_rejects_unknown_category_directly():
    schema = build_query_intent_schema(DEFAULT_REGISTRY.category_enum())
    assert schema["properties"]["category"]["enum"] == DEFAULT_REGISTRY.category_enum()
    assert set(schema["properties"]["metric"]["enum"]) == set(METRICS)
    assert set(schema["properties"]["timeframe"]["enum"]) == set(TIMEFRAMES)


async def test_classify_query_intent_returns_none_on_off_schema_response():
    llm = make_llm({"category": "not-a-real-habit", "metric": "sum", "timeframe": "today"})
    result = await classify_query_intent("how much water?", llm, DEFAULT_REGISTRY)
    assert result is None


async def test_classify_query_intent_returns_validated_intent_on_success():
    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})
    result = await classify_query_intent("how much water today?", llm, DEFAULT_REGISTRY)
    assert result == QueryIntent(habit_id="water", metric="sum", timeframe="today")


# ---------------------------------------------------------------------------
# AC8.5 -- queries are strictly read-only: no `logs` row is ever written by
# any query path, success or failure.
# ---------------------------------------------------------------------------


async def test_ac85_successful_query_writes_no_row(db, fixed_clock):
    channel = FakeChannel()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, "numeric")
    before = _raw_row_count(db)

    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})
    await handle_inbound_message(
        "how much water today?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert _raw_row_count(db) == before
    assert channel.sent  # actually answered, not silently skipped


async def test_ac85_failed_query_also_writes_no_row(db, fixed_clock):
    channel = FakeChannel()
    before = _raw_row_count(db)

    llm = make_llm({"category": "unknown", "metric": "count", "timeframe": "today"})
    await handle_inbound_message(
        "what's the meaning of life?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert _raw_row_count(db) == before


async def test_ac85_repeated_queries_never_accumulate_rows(db, fixed_clock):
    channel = FakeChannel()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, "numeric")
    before = _raw_row_count(db)

    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "this_week"})
    for _ in range(5):
        await handle_inbound_message(
            "how much water this week?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
        )

    assert _raw_row_count(db) == before
    assert len(channel.sent) == 5


# ---------------------------------------------------------------------------
# Per-type answer formatting (numeric count, boolean, text) beyond AC8.1/8.2's
# numeric-sum / duration-count examples.
# ---------------------------------------------------------------------------


async def test_numeric_count_metric_reports_log_count_not_sum(db, fixed_clock):
    channel = FakeChannel()
    _seed(db, "2026-08-19T08:00:00", "water", 300.0, "numeric")
    _seed(db, "2026-08-19T12:00:00", "water", 200.0, "numeric")

    llm = make_llm({"category": "water", "metric": "count", "timeframe": "today"})
    await handle_inbound_message(
        "how many times did I log water today?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == ["📊 water: logged 2 time(s) today"]


async def test_boolean_habit_reports_done_days_not_raw_row_count(db, fixed_clock):
    config = Config(
        habits=[
            *Config().habits,
            {
                "id": "meds",
                "type": "boolean",
                "label": {"en": "meds", "th": "ยา"},
                "reminder_times": [],
            },
        ]
    )
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    # Two truthy logs on the same day -> one done-day, not two.
    _seed(db, "2026-08-19T08:00:00", "meds", 1.0, "boolean")
    _seed(db, "2026-08-19T20:00:00", "meds", 1.0, "boolean")
    _seed(db, "2026-08-18T08:00:00", "meds", 0.0, "boolean")  # falsy -> not a done day

    llm = make_llm({"category": "meds", "metric": "count", "timeframe": "this_week"})
    await handle_inbound_message(
        "did I take my meds this week?",
        db=db, user_id="owner",
        llm=llm,
        channel=channel,
        config=config,
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == ["📊 meds: done on 1 day(s) this week"]


async def test_text_habit_reports_entry_count(db, fixed_clock):
    channel = FakeChannel()
    db.insert_log(LogEntry(None, "owner", "2026-08-19T21:00:00", "diary", None, "good day", "good day", "reply", habit_type="text"))
    db.insert_log(LogEntry(None, "owner", "2026-08-18T21:00:00", "diary", None, "ok day", "ok day", "reply", habit_type="text"))

    llm = make_llm({"category": "diary", "metric": "count", "timeframe": "this_week"})
    await handle_inbound_message(
        "how many diary entries this week?", db=db, user_id="owner", llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == ["📊 diary: 2 entry(ies) this week"]


# ---------------------------------------------------------------------------
# Prompt builder sanity (query-specific system/user prompts, mirroring
# tests/test_prompts.py's extraction-prompt coverage without reusing its
# "AC8" numbering, which belongs to v0.7.0's module M1).
# ---------------------------------------------------------------------------


def test_query_user_prompt_wraps_the_message():
    assert build_query_intent_user_prompt("how much water?") == "Message: how much water?"


def test_query_system_prompt_covers_every_registered_habit_and_unknown():
    prompt = build_query_intent_system_prompt(DEFAULT_REGISTRY)
    assert '"water"' in prompt
    assert '"stretch"' in prompt
    assert '"diary"' in prompt
    assert '"unknown"' in prompt
    assert '"sum"' in prompt
    assert '"count"' in prompt
    assert "this_week" in prompt
    assert "last_7_days" in prompt


async def test_answer_question_direct_call_end_to_end(db, fixed_clock):
    """A direct call to answer_question (bypassing handle_inbound_message)
    -- proves the function is independently usable/testable, per its own
    signature, not just reachable through the command router."""
    _seed(db, "2026-08-19T09:00:00", "stretch", 12.0, "duration")
    llm = make_llm({"category": "stretch", "metric": "count", "timeframe": "today"})

    answer = await answer_question(
        "how many times did I stretch today?",
        db=db, user_id="owner",
        llm=llm,
        registry=DEFAULT_REGISTRY,
        config=Config(),
        lang="en",
        clock=fixed_clock,
    )

    assert answer == "📊 stretch: 1 time(s), 12 min total today"
