"""SPEC-LINE.md §4 Module B (no-LLM mode, branch `line-version`), §5.2's
normative 8-row call-site table, §8 AC15-AC19.

`config.ollama.enabled == False` is this branch's own default; this file
proves, per §5.2 row, that: (a) DISABLED mode makes zero LLM calls -- not
just "the output looks right", but structurally proven via a `Poisoned*`
double that RAISES if any LLM-shaped method is ever invoked -- and (b)
ENABLED mode (`config.ollama.enabled == True`, the pre-LINE default) is
byte-identical to v1.10.0: the same call still happens, unchanged.

Row-by-row coverage:
  1. core/routing.py:handle_inbound_message -> core/parser.py:parse_message
  2. core/routing.py:reparse_pending_unparsed -> parse_message
  3. core/routing.py -> core/target_nl.py:classify_target_intent
  4. core/routing.py (kind=="query") -> core/query.py:answer_question -> classify_query_intent
  5. core/confirmation.py:confirmation_text/generic_confirmation (text/diary) -> chat_text
  6. core/jobs.py:weekly_review_job -> core/review.py:run_weekly_review -> chat_text
  7. core/app.py:async_main -> probe_schema_support -- OUT OF SCOPE (app.py is
     Integration's own file, SPEC-LINE.md §11; not exercised here).
  8. core/health.py:HealthMonitor.run_once -> check_ollama (HTTP ping)

Rows 1-3 share one code block in `handle_inbound_message` (the preparse-miss
`elif config.ollama.enabled: ... else: ...` split) and are covered together
below."""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config, OllamaConfig
from habit_assistant.core import confirmation, i18n, query, review, routing
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.health import HealthMonitor
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

from conftest import FakeOllamaClient, RecordingChannel

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def disabled_config(**overrides) -> Config:
    return Config(ollama=OllamaConfig(enabled=False, **overrides))


def enabled_config(**overrides) -> Config:
    return Config(ollama=OllamaConfig(enabled=True, **overrides))


# ---------------------------------------------------------------------------
# Structural zero-LLM doubles: raise (not just "return something wrong") if
# ANY LLM-shaped method is ever called -- the "poisoned client at every
# site" proof Archi's dispatch asked for.
# ---------------------------------------------------------------------------


class PoisonedOllamaClient:
    async def chat_json(self, *args, **kwargs):
        raise AssertionError("chat_json must never be called in no-LLM mode")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("chat_text must never be called in no-LLM mode")

    async def probe_schema_support(self, *args, **kwargs):
        raise AssertionError("probe_schema_support must never be called in no-LLM mode")

    async def aclose(self) -> None:
        pass


async def _poisoned_parse_message(*args, **kwargs):
    raise AssertionError("parse_message must never be called in no-LLM mode")


class PoisonedHTTPTransport(httpx.AsyncBaseTransport):
    """A transport that raises if any request is ever sent through it --
    row 8's proof that a disabled `HealthMonitor` never pings Ollama at
    all (not even a failed ping)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP request may be made in no-LLM mode, got: {request.url}")

    async def aclose(self) -> None:
        pass


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _reset_fake_ollama_responses():
    yield
    FakeOllamaClient.responses = []


# ---------------------------------------------------------------------------
# Rows 1-3 -- handle_inbound_message's preparse-miss branch.
# ---------------------------------------------------------------------------


async def test_row1_disabled_preparse_miss_goes_straight_to_generic_clarify_no_llm(db):
    """AC15/AC16 (R-B1/R-B2): a message with no preparse hit AND no
    tier1_guesses -> the generic clarifying question + /log keyboard,
    zero LLM calls, and (AC15) NO unparsed/awaiting_llm row is written --
    `pending_unparsed()` stays empty."""
    channel = RecordingChannel()
    config = disabled_config()

    await routing.handle_inbound_message(
        "asdkjqwexyz not a real habit phrase",
        db=db,
        llm=PoisonedOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
        parse_message=_poisoned_parse_message,
    )

    assert channel.sent_to("u1") == [i18n.t("clarifying_question", "en")]
    assert db.pending_unparsed() == []


async def test_row1_disabled_preparse_miss_with_guesses_offers_tap_to_fix_no_llm(db):
    """AC16 (R-B2): tier1_guesses non-empty ("500" is unit-plausible only
    for water, per core/clarify.py's own documented example) -> the
    tap-to-fix offer, and the row it writes is `awaiting_clarify`, NEVER
    `awaiting_llm` -- R-B1 forbids the latter, not the former."""
    channel = RecordingChannel()
    config = disabled_config()

    await routing.handle_inbound_message(
        "500",
        db=db,
        llm=PoisonedOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
        parse_message=_poisoned_parse_message,
    )

    sent = channel.sent_to("u1")
    assert len(sent) == 1
    assert sent[0] == i18n.t("clarify_offer", "en", text="500")
    # The row exists and is `awaiting_clarify`, not `awaiting_llm` --
    # `pending_unparsed()` (which only returns NULL/'awaiting_llm' rows)
    # must NOT see it.
    assert db.pending_unparsed() == []
    row = db._conn.execute("SELECT category, unparsed_state FROM logs WHERE user_id='u1'").fetchone()
    assert row["category"] == "unparsed"
    assert row["unparsed_state"] == "awaiting_clarify"


async def test_row1_enabled_preparse_miss_still_calls_parse_message(db):
    """Enabled=true byte-identical (R-B1/R-B2's own gate): with
    `ollama.enabled=True` (the pre-LINE default), a preparse miss reaches
    `parse_message` exactly as before -- proven by seeding a real
    extraction response and observing the resulting log."""
    channel = RecordingChannel()
    config = enabled_config()
    FakeOllamaClient.responses = [json.dumps({"category": "water", "value": 750, "confidence": 0.9})]

    await routing.handle_inbound_message(
        "chugged a bunch of water just now",
        db=db,
        llm=FakeOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
    )

    row = db._conn.execute("SELECT category, value_num FROM logs WHERE user_id='u1'").fetchone()
    assert row["category"] == "water"
    assert row["value_num"] == 750.0


async def test_row2_disabled_reparse_pending_unparsed_is_dead_code_no_llm(db):
    """AC19/R-B8, §5.2 row 2: a leftover `awaiting_llm` row (e.g. from
    before the branch flipped to no-LLM mode) is never handed to
    `parse_message` -- the guard returns before even the single-flight
    check, and the row is left completely untouched."""
    row_id = db.insert_log(LogEntry(None, "u1", "2026-08-20T10:00:00", "unparsed", None, None, "500", "reply"))
    config = disabled_config()

    await routing.reparse_pending_unparsed(
        db, PoisonedOllamaClient(), RecordingChannel(), config, DEFAULT_REGISTRY, parse_message=_poisoned_parse_message
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed"
    assert row["unparsed_state"] is None


async def test_row2_enabled_reparse_pending_unparsed_still_reparses(db):
    """Enabled=true byte-identical: the same leftover row IS reparsed and
    resolved when ollama is enabled -- unchanged pre-LINE behavior."""
    db.insert_log(LogEntry(None, "u1", "2026-08-20T10:00:00", "unparsed", None, None, "500ml", "reply"))
    config = enabled_config()
    FakeOllamaClient.responses = [json.dumps({"category": "water", "value": 500, "confidence": 0.9})]
    channel = RecordingChannel()

    await routing.reparse_pending_unparsed(db, FakeOllamaClient(), channel, config, DEFAULT_REGISTRY)

    row = db._conn.execute("SELECT category, unparsed_state FROM logs WHERE user_id='u1'").fetchone()
    assert row["category"] == "water"
    assert row["unparsed_state"] is None
    assert channel.sent_to("u1") != []


async def test_row3_disabled_target_phrasing_points_at_target_command_no_llm(db):
    """AC17 (R-B3): "from now on 3L a day" (SPEC-LINE.md's own example) --
    no goal is set, the reply points at /target, zero LLM calls, and no
    `logs` row is written for it."""
    channel = RecordingChannel()
    config = disabled_config()

    await routing.handle_inbound_message(
        "from now on 3L a day",
        db=db,
        llm=PoisonedOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
        parse_message=_poisoned_parse_message,
    )

    assert channel.sent_to("u1") == [i18n.t("target_nl_no_llm_pointer", "en")]
    assert db.get_target("u1", "water") is None
    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id='u1'").fetchone()["n"] == 0


async def test_row3_enabled_target_phrasing_still_classifies_and_sets_target(db):
    """Enabled=true byte-identical: the same style of phrasing still runs
    the real LLM target-intent classifier and sets the goal, unchanged."""
    channel = RecordingChannel()
    config = enabled_config()
    FakeOllamaClient.responses = [json.dumps({"category": "water", "goal": 3000, "confidence": 0.9})]

    await routing.handle_inbound_message(
        "from now on I want 3000 ml a day",
        db=db,
        llm=FakeOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
    )

    assert db.get_target("u1", "water") == 3000.0
    assert channel.sent_to("u1") != []


# ---------------------------------------------------------------------------
# Row 4 -- core/query.py:answer_question -> classify_query_intent.
# ---------------------------------------------------------------------------


def make_llm(response_json: dict) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": json.dumps(response_json)}})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


def _seed_water(db: Database, ts: str, ml: float) -> None:
    db.insert_log(LogEntry(None, "owner", ts, "water", ml, None, "seed", "reply", habit_type="numeric"))


async def test_row4_disabled_query_never_classifies_returns_command_pointer(db):
    """AC17 (R-B4): an NL question gets the deterministic /records,
    /trends, /dashboard pointer, zero LLM calls, no writes."""
    config = disabled_config()

    answer = await query.answer_question(
        "how much water this week?",
        db=db,
        llm=PoisonedOllamaClient(),
        registry=DEFAULT_REGISTRY,
        config=config,
        lang="en",
        user_id="owner",
    )

    assert answer == i18n.t("query_no_llm_pointer", "en")
    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"] == 0


async def test_row4_disabled_query_via_full_routing_path(db):
    """Same proof, but end-to-end through handle_inbound_message's own
    `command.kind == "query"` dispatch (core/commands.py's own zero-LLM
    "?" detector) -- AC17's exact end-user scenario."""
    channel = RecordingChannel()
    config = disabled_config()

    await routing.handle_inbound_message(
        "how much water this week?",
        db=db,
        llm=PoisonedOllamaClient(),
        channel=channel,
        config=config,
        user_id="u1",
        registry=DEFAULT_REGISTRY,
        parse_message=_poisoned_parse_message,
    )

    assert channel.sent_to("u1") == [i18n.t("query_no_llm_pointer", "en")]


async def test_row4_enabled_query_still_classifies_and_answers(db):
    """Enabled=true byte-identical: `classify_query_intent` still runs the
    real chat_json call and answers with a real number, unchanged."""
    config = enabled_config()
    _seed_water(db, "2026-08-19T09:00:00", 500.0)
    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})

    def clock():
        return datetime(2026, 8, 19, 14, 0, 0)

    answer = await query.answer_question(
        "how much water today?", db=db, llm=llm, registry=DEFAULT_REGISTRY, config=config,
        lang="en", user_id="owner", clock=clock,
    )

    assert answer != i18n.t("query_no_llm_pointer", "en")
    assert answer != i18n.t("query_cant_answer", "en")
    assert "500" in answer


# ---------------------------------------------------------------------------
# Row 5 -- core/confirmation.py (text/diary) -> chat_text.
# ---------------------------------------------------------------------------


class PoisonedLLM:
    async def chat_text(self, *args, **kwargs):
        raise AssertionError("chat_text must never be called in no-LLM mode")


class RecordingLLM:
    def __init__(self, text: str | None):
        self._text = text
        self.called = False

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        self.called = True
        return self._text


async def test_row5_disabled_diary_confirmation_forces_static_fallback_no_llm(db):
    """AC18 (R-B5): the diary (built-in text habit) confirmation uses the
    static fallback, zero LLM calls."""
    config = disabled_config()

    text = await confirmation.confirmation_text(
        db, PoisonedLLM(), DEFAULT_REGISTRY.get("diary"), "had a good day", "2026-08-19", "en", config, "u1"
    )

    assert i18n.t("diary_reflection_fallback", "en") in text


async def test_row5_disabled_generic_text_habit_confirmation_forces_static_fallback_no_llm(db):
    """AC18 (R-B5): same guarantee for a non-built-in `type=="text"`
    habit, via `generic_confirmation` directly."""
    from habit_assistant.core.habits import Habit

    text_habit = Habit(
        id="journal", type="text", label_en="journal", label_th="journal",
        unit_en=None, unit_th=None, goal=None, reminder_times=(), reminder_text_en=None,
        reminder_text_th=None, unit_aliases={},
    )
    config = disabled_config()

    text = await confirmation.generic_confirmation(db, PoisonedLLM(), text_habit, "wrote something", "2026-08-19", "en", config, "u1")

    assert i18n.t("diary_reflection_fallback", "en") in text


async def test_row5_enabled_diary_confirmation_still_calls_llm():
    """Enabled=true byte-identical: `chat_text` is still awaited and its
    result used verbatim, unchanged."""
    config = enabled_config()
    llm = RecordingLLM("What a lovely day.")

    database = Database(":memory:")
    try:
        text = await confirmation.confirmation_text(
            database, llm, DEFAULT_REGISTRY.get("diary"), "had a good day", "2026-08-19", "en", config, "u1"
        )
    finally:
        database.close()

    assert llm.called is True
    assert "What a lovely day." in text


# ---------------------------------------------------------------------------
# Row 6 -- core/review.py:run_weekly_review -> chat_text.
# ---------------------------------------------------------------------------


@pytest.fixture
def review_db(tmp_path):
    database = Database(tmp_path / "review.db")
    database.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 2500.0, None, "seed", "reply"))
    yield database
    database.close()


async def test_row6_disabled_weekly_review_forces_static_narrative_no_llm(review_db):
    """AC18 (R-B6): the weekly-review narrative uses the static fallback,
    zero LLM calls; the stats block is unaffected."""
    config = disabled_config()

    text = await review.run_weekly_review(
        review_db, config, DEFAULT_REGISTRY, PoisonedLLM(), "en", "owner", today=date(2026, 8, 19)
    )

    assert i18n.t("weekly_review_fallback_narrative", "en") in text
    assert i18n.t("stats_water_total", "en", water_total_ml=2500, water_avg_ml=357.1) in text


async def test_row6_enabled_weekly_review_still_calls_llm(review_db):
    """Enabled=true byte-identical: the narrative comes from `chat_text`
    verbatim, unchanged (mirrors tests/test_review.py's own coverage)."""
    config = enabled_config()
    llm = RecordingLLM("Solid week overall.")

    text = await review.run_weekly_review(review_db, config, DEFAULT_REGISTRY, llm, "en", "owner", today=date(2026, 8, 19))

    assert llm.called is True
    assert "Solid week overall." in text


# ---------------------------------------------------------------------------
# Row 8 -- core/health.py:HealthMonitor.run_once -> check_ollama.
# ---------------------------------------------------------------------------


async def test_row8_disabled_health_monitor_never_pings_ollama_no_alert_no_recovery():
    """AC19 (R-B8): `ollama_enabled=False` -- `check_ollama` reports "up"
    without any HTTP call (a poisoned transport would raise if hit),
    `run_once` never alerts, and `on_ollama_recovered` never fires (no
    DOWN->UP transition is even possible when every check is a no-op
    "always up")."""
    poisoned_client = httpx.AsyncClient(transport=PoisonedHTTPTransport())
    channel = RecordingChannel()
    recovered_calls = []

    async def on_recovered():
        recovered_calls.append(1)

    monitor = HealthMonitor(
        "http://mac-mini:11434",
        "fake-telegram-token",
        "owner",
        client=poisoned_client,
        channel=channel,
        on_ollama_recovered=on_recovered,
        ollama_enabled=False,
    )
    # check_telegram would also hit the poisoned transport -- monkeypatch it
    # to a no-op success so this test isolates the Ollama half only (row 8
    # is specifically about the Ollama ping, not the Telegram one, which is
    # N/A on LINE for a different reason, R-B8's own module docstring note).
    monitor.check_telegram = lambda: _true()

    assert await monitor.check_ollama() is True
    await monitor.run_once()

    assert monitor.ollama_up is True
    assert channel.sent == []
    assert recovered_calls == []
    await poisoned_client.aclose()


async def _true() -> bool:
    return True


async def test_row8_enabled_health_monitor_still_pings_ollama_default_behavior():
    """Enabled=true byte-identical (`ollama_enabled` default is `True`):
    `check_ollama` still makes the real HTTP call and reports failure
    honestly when the host is unreachable -- unchanged pre-LINE
    behavior."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    failing_client = httpx.AsyncClient(transport=transport)

    monitor = HealthMonitor("http://mac-mini:11434", "fake-telegram-token", "owner", client=failing_client)

    assert monitor._ollama_enabled is True
    assert await monitor.check_ollama() is False
    await failing_client.aclose()
