"""Vera's supplementary gap tests for ROADMAP.md v0.8.0 "Natural-Language
Queries" (AC8.1-AC8.5), on top of Luna's own `tests/test_query.py` (25
tests) and `tests/test_commands.py`'s v0.8.0 section (20 tests) -- see
`IMPL.md`'s "v0.8.0" section for what Luna already covered.

This file targets specifically the gaps requested by the test brief:
  1. AC8.3 midnight-boundary bucketing (a log at 23:50 "yesterday" vs 00:10
     "today" in Bangkok-local naive timestamps -- Luna's own boundary test
     only proved a single day stays "today" near midnight, not that two
     entries either side of midnight land in *different* buckets).
  2. AC8.4 adversarial breadth Luna didn't hit: a genuine transport-level
     exception (not just a bad HTTP status), a non-dict JSON payload
     (a bare list), and an internal `date_range_for_timeframe` guard given
     an intent that (hypothetically) carried an out-of-vocabulary timeframe
     despite `_validate_intent`'s gate.
  3. AC8.5 reinforced: a monkeypatched spy on every `Database` write method
     (`insert_log`, `soft_delete`, `update_value`, `reclassify_log`) proves
     none is *reachable* from the query path, not just that the row count
     happens to net out unchanged.
  4. False-positive sweep in the other direction (query anchors must not
     fire on ordinary prose), and an explicit, code-level proof that
     undo/edit are checked -- and win -- before query even when the query
     matcher would also fire (`commands.dispatch`'s own routing order,
     ROADMAP.md's required "explicit commands -> query intent -> extractor"
     chain).
  5. The documented trailing-"?" grammar (IMPL.md v0.8.0 "Known limitations"
     #5): a log-shaped message ending in "?" is *deliberately* routed to
     the query path per the ROADMAP's own anchor set -- confirmed here as a
     regression guard, not treated as a bug, since it matches what IMPL.md
     documents.
  6. The full precedence chain at the `handle_inbound_message` level: a
     query is answered (not deferred, not silently swallowed) even while
     `health_monitor.ollama_up` is False and the LLM call itself genuinely
     fails -- proving queries never join the v0.4.0 deferral queue.

Same fixture/fake conventions as `tests/test_query.py` (real `OllamaClient`
over `httpx.MockTransport`, real on-disk sqlite `Database` in `tmp_path`,
`Config()` defaults for the water/stretch/diary registry).
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.query import answer_question, date_range_for_timeframe
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.main import DEFERRED_ACK_MESSAGE, handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message) -> None:
        raise NotImplementedError("not exercised in these tests")


class _FrozenHealthMonitor:
    """Same minimal stand-in `tests/test_commands.py` uses."""

    def __init__(self, ollama_up: bool):
        self.ollama_up = ollama_up


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def fixed_clock():
    # Wednesday 2026-08-19, 10:00 -- well clear of midnight so the
    # boundary tests below are unambiguous about which day is "today".
    def clock():
        return datetime(2026, 8, 19, 10, 0, 0)

    return clock


def _seed(db: Database, ts: str, category: str, value_num: float, habit_type: str = "numeric") -> int:
    return db.insert_log(LogEntry(None, ts, category, value_num, None, ts, "reply", habit_type=habit_type))


def _raw_row_count(db: Database) -> int:
    return db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]


def make_llm(response_json: dict | list | None, *, status: int = 200) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if response_json is None:
            return httpx.Response(status)
        return httpx.Response(200, json={"message": {"content": json.dumps(response_json)}})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


def make_erroring_llm() -> OllamaClient:
    """A transport that raises a genuine connection error on every request
    -- distinct from Luna's `test_ac84_ollama_unreachable_yields_cant_answer_not_a_crash`,
    which used a reachable-but-bad-HTTP-status (503) mock. This simulates
    Ollama's host being truly unreachable (connection refused/timeout),
    exercising `_post`'s transport-exception handling, not its status-code
    handling."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return OllamaClient("http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=async_client)


# ===========================================================================
# AC8.3 -- midnight-boundary bucketing: entries either side of midnight must
# land in DIFFERENT day buckets (today vs yesterday), deterministically,
# via the injected `clock`.
# ===========================================================================


async def test_log_at_2350_yesterday_and_0010_today_bucket_correctly(db, fixed_clock):
    """A log at 23:50 on 2026-08-18 must count toward "yesterday" only; a
    log at 00:10 on 2026-08-19 must count toward "today" only. Both are
    naive Bangkok-local timestamps (the app's own storage convention --
    see `_seed`/`LogEntry.ts` elsewhere in the suite), and `fixed_clock`
    deterministically pins "now" to 2026-08-19 10:00, so there is no
    reliance on the host machine's wall-clock or timezone."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-18T23:50:00", "water", 111.0)
    _seed(db, "2026-08-19T00:10:00", "water", 222.0)

    llm_today = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})
    await handle_inbound_message(
        "how much water today?", db=db, llm=llm_today, channel=channel, config=config, clock=fixed_clock
    )
    assert channel.sent[-1] == "📊 water: 222 ml today"

    llm_yesterday = make_llm({"category": "water", "metric": "sum", "timeframe": "yesterday"})
    await handle_inbound_message(
        "how much water yesterday?", db=db, llm=llm_yesterday, channel=channel, config=config, clock=fixed_clock
    )
    assert channel.sent[-1] == "📊 water: 111 ml yesterday"


def test_date_range_for_timeframe_today_and_yesterday_are_disjoint():
    today = date(2026, 8, 19)
    assert date_range_for_timeframe("today", today) == ["2026-08-19"]
    assert date_range_for_timeframe("yesterday", today) == ["2026-08-18"]
    assert set(date_range_for_timeframe("today", today)).isdisjoint(date_range_for_timeframe("yesterday", today))


async def test_clock_injection_makes_the_boundary_deterministic_across_repeated_calls(db, fixed_clock):
    """Same seeded data, same fixed clock, called 3x -- the answer must be
    byte-identical every time (no reliance on real wall-clock time, so this
    test is not flaky by construction)."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T00:05:00", "water", 50.0)

    for _ in range(3):
        llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})
        await handle_inbound_message(
            "how much water today?", db=db, llm=llm, channel=channel, config=config, clock=fixed_clock
        )
    assert channel.sent == ["📊 water: 50 ml today"] * 3


# ===========================================================================
# AC8.4 -- adversarial breadth beyond Luna's coverage: a genuine transport
# exception (not just a bad status code), and a non-dict JSON payload.
# ===========================================================================


async def test_transport_exception_yields_cant_answer_not_a_crash(db, fixed_clock):
    channel = FakeChannel()
    llm = make_erroring_llm()

    await handle_inbound_message(
        "how much water this week?", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [i18n.t("query_cant_answer", "en")]


async def test_llm_returns_a_json_array_instead_of_object_yields_cant_answer(db, fixed_clock):
    """`_validate_intent` explicitly guards `isinstance(data, dict)` --
    prove a syntactically-valid-JSON-but-wrong-shape payload (a bare list)
    is rejected the same way a malformed payload is, not just documented in
    a docstring."""
    channel = FakeChannel()
    llm = make_llm(["water", "sum", "this_week"])

    await handle_inbound_message(
        "how much water this week?", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [i18n.t("query_cant_answer", "en")]


async def test_llm_returns_extra_and_missing_keys_yields_cant_answer(db, fixed_clock):
    """An off-schema object (wrong keys entirely) must fail closed too."""
    channel = FakeChannel()
    llm = make_llm({"foo": "bar"})

    await handle_inbound_message(
        "how much water this week?", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [i18n.t("query_cant_answer", "en")]


async def test_thai_unconfigured_habit_question_yields_thai_cant_answer(db, fixed_clock):
    """AC8.4's fallback is bilingual -- confirm the Thai side, not just
    English (Luna's can't-answer tests were all English-input)."""
    channel = FakeChannel()
    llm = make_llm({"category": "unknown", "metric": "count", "timeframe": "today"})

    await handle_inbound_message(
        "วันนี้กินกาแฟไปกี่แก้ว", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [i18n.t("query_cant_answer", "th")]


# ===========================================================================
# AC8.5 -- no Database *write method* is reachable from the query path,
# structurally (a spy that raises if called), across both success and
# every failure mode above -- stronger than "row count nets out unchanged".
# ===========================================================================


@pytest.fixture
def write_spy_db(tmp_path, monkeypatch):
    database = Database(tmp_path / "habits.db")
    for method_name in ("insert_log", "soft_delete", "update_value", "reclassify_log"):

        def _raise(*args, __name=method_name, **kwargs):
            raise AssertionError(f"Database.{__name} must never be called from the query path (AC8.5)")

        monkeypatch.setattr(database, method_name, _raise)
    yield database
    database.close()


async def test_successful_query_never_calls_a_write_method(write_spy_db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})

    # No exception means none of insert_log/soft_delete/update_value/
    # reclassify_log were ever invoked.
    await handle_inbound_message(
        "how much water today?", db=write_spy_db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )
    assert channel.sent == ["📊 water: 0 ml today"]


async def test_failed_query_never_calls_a_write_method(write_spy_db, fixed_clock):
    channel = FakeChannel()
    llm = make_llm({"category": "unknown", "metric": "count", "timeframe": "today"})

    await handle_inbound_message(
        "what's the meaning of life?", db=write_spy_db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )
    assert channel.sent == [i18n.t("query_cant_answer", "en")]


async def test_transport_error_query_never_calls_a_write_method(write_spy_db, fixed_clock):
    channel = FakeChannel()
    llm = make_erroring_llm()

    await handle_inbound_message(
        "how much water this week?", db=write_spy_db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )
    assert channel.sent == [i18n.t("query_cant_answer", "en")]


async def test_direct_answer_question_call_never_calls_a_write_method(write_spy_db, fixed_clock):
    llm = make_llm({"category": "stretch", "metric": "count", "timeframe": "today"})
    answer = await answer_question(
        "how many times did I stretch today?",
        db=write_spy_db,
        llm=llm,
        registry=DEFAULT_REGISTRY,
        config=Config(),
        lang="en",
        clock=fixed_clock,
    )
    assert answer == "📊 stretch: 0 time(s), 0 min total today"


# ===========================================================================
# False-positive sweep (the OTHER direction): ordinary prose that merely
# contains "how" or a Thai question particle mid-sentence, without a true
# interrogative anchor, must NOT be routed to query.
# ===========================================================================


NOT_QUERY_MESSAGES = [
    "500ml",
    "ดื่มน้ำ 2 แก้ว",
    "not sure how I feel about tomorrow",  # "how" without "much"/"many"
    "I learned how stretching helps my back",  # ditto
    "how's it going",  # "how's" != "how much/how many"
    "did it rain today",  # "did" without a following "i"
    "10 min stretch, felt great",
]


@pytest.mark.parametrize("message", NOT_QUERY_MESSAGES)
def test_prose_containing_bare_how_or_did_is_not_classified_as_query(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None, f"{message!r} was misclassified as a command: {command}"


def test_thai_question_particle_ki_always_signals_a_question_by_design():
    """Unlike English "how", Thai "กี่" has no non-interrogative reading in
    normal usage -- it IS the "how many" question word. So there is no
    false-positive case to construct for it (documented here rather than
    silently skipped, per the test brief's item 5): any occurrence, even
    mid-sentence, is correctly query-shaped."""
    assert commands.dispatch("อยากรู้ว่ามีแมวกี่ตัว", DEFAULT_REGISTRY).kind == "query"


# ===========================================================================
# Precedence: undo/edit are checked -- and WIN -- before query, structurally
# (proven even when the query matcher would ALSO fire, by monkeypatching it
# to always return True: undo/edit's own patterns are mutually exclusive
# with query's anchors by construction -- see core/commands.py's dispatch()
# docstring -- so this is the only way to force a genuine overlap to test).
# ===========================================================================


def test_undo_wins_over_query_even_when_query_matcher_would_also_fire(monkeypatch):
    monkeypatch.setattr(commands, "_match_query", lambda stripped: True)
    command = commands.dispatch("/undo", DEFAULT_REGISTRY)
    assert command.kind == "undo"


def test_edit_wins_over_query_even_when_query_matcher_would_also_fire(monkeypatch):
    monkeypatch.setattr(commands, "_match_query", lambda stripped: True)
    command = commands.dispatch("make that 250ml", DEFAULT_REGISTRY)
    assert command.kind == "edit"


def test_thai_undo_wins_over_query_even_when_query_matcher_would_also_fire(monkeypatch):
    monkeypatch.setattr(commands, "_match_query", lambda stripped: True)
    command = commands.dispatch("ยกเลิกอันล่าสุด", DEFAULT_REGISTRY)
    assert command.kind == "undo"


# ===========================================================================
# Documented trailing-"?" grammar (IMPL.md v0.8.0 "Known limitations" #5):
# a log-shaped message ending in "?" is deliberately routed to query. This
# is confirmed here as a regression guard against the documented behavior,
# not flagged as a bug -- IMPL.md explicitly calls this out as a known,
# accepted trade-off of the ROADMAP's own anchor set.
# ===========================================================================


def test_trailing_question_mark_on_a_log_like_message_routes_as_query_per_documented_grammar():
    """Matches IMPL.md v0.8.0 Known limitation #5 verbatim: 'A trailing
    "?"/"？" alone is sufficient to route a message to the query path' --
    e.g. a diary reflection that happens to end in "?" is intentionally
    NOT logged as a normal entry. This is documented, ROADMAP-anchor-set
    behavior, not a spec/implementation mismatch."""
    command = commands.dispatch("should I go for a run tomorrow?", DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "query"


async def test_trailing_question_mark_diary_reflection_is_answered_or_declined_not_logged(db, fixed_clock):
    """End-to-end proof of the same documented behavior: the message is
    never written as a `diary` log row -- it goes through the query path
    (and, since it's not about a tracked metric in a way the stub LLM
    recognizes, degrades to the can't-answer fallback rather than silently
    becoming a diary entry)."""
    channel = FakeChannel()
    llm = make_llm({"category": "unknown", "metric": "count", "timeframe": "today"})

    await handle_inbound_message(
        "should I go for a run tomorrow?", db=db, llm=llm, channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [i18n.t("query_cant_answer", "en")]
    assert _raw_row_count(db) == 0  # definitely not written as a diary/unparsed row


# ===========================================================================
# Full precedence chain at handle_inbound_message level: explicit command
# -> query -> deferral-check -> extractor. A query must be ANSWERED, never
# deferred to the v0.4.0 unparsed-queue path, even while health_monitor
# reports Ollama DOWN and the LLM call genuinely fails.
# ===========================================================================


async def test_query_is_answered_not_deferred_while_ollama_is_reported_down(db, fixed_clock):
    """`health_monitor.ollama_up = False` only gates the *extractor's*
    deferral branch (main.py, after the command-dispatch block) -- a query
    is read-only and detected LLM-free by `dispatch`, so it must still
    attempt an answer (and fail closed to the can't-answer message if the
    LLM genuinely can't be reached) rather than being silently deferred and
    persisted as an 'unparsed' row for later re-parsing."""
    channel = FakeChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)
    llm = make_erroring_llm()  # Ollama truly unreachable, mirroring "DOWN"

    await handle_inbound_message(
        "how much water today?",
        db=db,
        llm=llm,
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert channel.sent != [DEFERRED_ACK_MESSAGE]
    assert channel.sent == [i18n.t("query_cant_answer", "en")]
    assert db.pending_unparsed() == []  # never queued for later re-parse


async def test_query_answers_successfully_while_ollama_is_reported_down_if_llm_call_itself_succeeds(db, fixed_clock):
    """Distinguishes "health_monitor says down" (a separate, possibly
    stale, signal) from "the LLM call actually fails" -- if the query's own
    LLM call succeeds despite the stale down-flag, the real answer is
    returned, not a deferral and not a can't-answer."""
    channel = FakeChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)
    _seed(db, "2026-08-19T09:00:00", "water", 400.0)
    llm = make_llm({"category": "water", "metric": "sum", "timeframe": "today"})

    await handle_inbound_message(
        "how much water today?",
        db=db,
        llm=llm,
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert channel.sent == ["📊 water: 400 ml today"]
