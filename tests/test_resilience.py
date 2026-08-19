"""Runtime resilience / self-monitoring tests (ROADMAP.md v0.3.0 "Runtime
Resilience & Self-Monitoring", shipped as v0.4.0, AC3.1-AC3.5), plus the
new migration (002) that ships alongside it.

Everything here is mocked/offline (httpx.MockTransport or real on-disk
tmp_path SQLite) -- no real Telegram call, no touching data/habits.db, no
`.env`. Companion to test_channels.py (v0.1.0 TelegramChannel basics) and
test_fallback.py (v0.2.0 OllamaClient basics); this file covers only the
v0.4.0 additions: exponential backoff + offset safety on the Telegram
poll loop, bounded retry on the Ollama client, the new HealthMonitor,
the persistent unparsed-message deferral/recovery path, and migration
002.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config
from habit_assistant.core.health import (
    OLLAMA_DOWN_MESSAGE,
    TELEGRAM_DOWN_MESSAGE,
    HealthMonitor,
)
from habit_assistant.llm.ollama_client import EXTRACTION_JSON_SCHEMA, OllamaClient
from habit_assistant.main import (
    DEFERRED_ACK_MESSAGE,
    handle_inbound_message,
    reparse_pending_unparsed,
)
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS, current_version, run_migrations

GLASS_ML = 250
BOTTLE_ML = 600
ANY_TIME_WINDOW = ("2000-01-01T00:00:00", "2100-01-01T00:00:00")


class _StopPolling(Exception):
    """Sentinel raised by a test transport once its canned responses are
    exhausted, to end TelegramChannel.run's `while True` loop -- NOT an
    httpx.HTTPError subclass, so it propagates out of `run()` instead of
    being swallowed as just another transport failure (same pattern as
    test_channels.py's StopPolling)."""


class _RecordingChannel:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self._fail = fail

    async def send(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("simulated channel send failure")
        self.sent.append(text)


class _FrozenHealthMonitor:
    """Minimal `health_monitor` stand-in exposing only the `.ollama_up`
    attribute `handle_inbound_message` actually reads -- lets AC3.3 tests
    drive the deferral decision directly without spinning up a real
    HealthMonitor/event loop."""

    def __init__(self, ollama_up: bool):
        self.ollama_up = ollama_up


class _NeverCalledLLM:
    """Proves the deferral path never touches the LLM at all."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must never be called while Ollama is known DOWN")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("LLM must never be called while Ollama is known DOWN")


class _StaticLLM:
    """Hand-picked chat_json response, bypassing HTTP entirely (same
    pattern as test_fallback.py's `_StaticLLM`)."""

    def __init__(self, content: str | None):
        self._content = content

    async def chat_json(self, system_prompt, user_prompt, json_schema):
        return self._content

    async def chat_text(self, system_prompt, user_prompt):
        return None


def json_payload(**overrides) -> str:
    base = {
        "category": "unknown",
        "water_ml": None,
        "stretch_min": None,
        "diary_text": None,
        "confidence": 0.1,
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# AC3.1 -- Telegram getUpdates exponential backoff; offset never advances on
# failure; recovery resumes from the correct offset (no drop/duplicate).
# ---------------------------------------------------------------------------


def _failures_then_responses_handler(n_failures: int, responses: list[dict]):
    state = {"n_failures": n_failures}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["n_failures"] > 0:
            state["n_failures"] -= 1
            raise httpx.ConnectError("simulated transport failure", request=request)
        if not responses:
            raise _StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    return handler


async def test_backoff_grows_and_caps_across_consecutive_transport_errors(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("habit_assistant.channels.telegram.asyncio.sleep", fake_sleep)

    # 4 consecutive failures, capped at 4.0s -> 1 -> 2 -> 4 -> 4 (uncapped
    # would be 8 on the 4th).
    transport = httpx.MockTransport(_failures_then_responses_handler(4, []))
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel(
        "token", "chat", client=client, backoff_initial_seconds=1.0, backoff_max_seconds=4.0
    )

    async def on_message(text: str) -> None:
        pass

    with pytest.raises(_StopPolling):
        await channel.run(on_message)

    assert sleeps == [1.0, 2.0, 4.0, 4.0]
    assert channel._offset is None  # never touched -- no update was ever delivered
    await channel.aclose()


async def test_backoff_resets_to_initial_after_a_successful_poll(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("habit_assistant.channels.telegram.asyncio.sleep", fake_sleep)

    call_state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_state["n"] += 1
        n = call_state["n"]
        if n in (1, 2):
            raise httpx.ConnectError("simulated", request=request)
        if n == 3:
            return httpx.Response(200, json={"ok": True, "result": []})  # recovers
        if n in (4, 5):
            raise httpx.ConnectError("simulated again", request=request)
        raise _StopPolling()

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel(
        "token", "chat", client=client, backoff_initial_seconds=1.0, backoff_max_seconds=60.0
    )

    async def on_message(text: str) -> None:
        pass

    with pytest.raises(_StopPolling):
        await channel.run(on_message)

    # First run of 2 failures: 1 -> 2. Successful poll resets. Second run
    # of 2 failures starts back at 1 -> 2, not 4 -> 8.
    assert sleeps == [1.0, 2.0, 1.0, 2.0]
    await channel.aclose()


async def test_offset_never_advances_on_failure_and_recovery_resumes_from_correct_offset(
    monkeypatch,
):
    """The load-bearing AC3.1 assertion: 3 consecutive transport failures
    sandwiched between two successful polls must not drop or duplicate
    either update, and the offset param sent on every request during the
    failures (and on the recovering request) must still be exactly what
    the first successful poll left it at."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("habit_assistant.channels.telegram.asyncio.sleep", fake_sleep)

    captured: list[httpx.Request] = []
    call_state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        call_state["n"] += 1
        n = call_state["n"]
        if n == 1:
            return httpx.Response(
                200,
                json={"ok": True, "result": [{"update_id": 10, "message": {"text": "500ml"}}]},
            )
        if n in (2, 3, 4):
            raise httpx.ConnectError("simulated transport blip", request=request)
        if n == 5:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [{"update_id": 20, "message": {"text": "10 min stretch"}}],
                },
            )
        raise _StopPolling()

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel(
        "token", "chat", client=client, backoff_initial_seconds=0.0, backoff_max_seconds=0.0
    )

    received: list[str] = []

    async def on_message(text: str) -> None:
        received.append(text)

    with pytest.raises(_StopPolling):
        await channel.run(on_message)

    # Exactly one delivery each, in order -- no drop, no duplicate.
    assert received == ["500ml", "10 min stretch"]

    # Request #1 (before any update) carries no offset param at all.
    assert captured[0].url.params.get("offset") is None
    # Requests #2-#5 (the 3 failures + the recovering success) all ask for
    # offset=11 -- exactly what request #1's update (id 10) would have set,
    # untouched by any of the intervening failures.
    for req in captured[1:5]:
        assert req.url.params.get("offset") == "11"

    # Final state: advanced only by the second batch's update_id (20) + 1.
    assert channel._offset == 21
    await channel.aclose()


# ---------------------------------------------------------------------------
# AC3.4 -- Telegram unreachable: logged + retried, no exception escapes,
# loop/process stays alive.
# ---------------------------------------------------------------------------


async def test_telegram_unreachable_is_logged_and_retried_without_crashing(monkeypatch, caplog):
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("habit_assistant.channels.telegram.asyncio.sleep", fake_sleep)

    transport = httpx.MockTransport(_failures_then_responses_handler(6, []))
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel(
        "token", "chat", client=client, backoff_initial_seconds=0.0, backoff_max_seconds=0.0
    )

    async def on_message(text: str) -> None:
        pass

    import logging

    with caplog.at_level(logging.WARNING, logger="habit_assistant"):
        with pytest.raises(_StopPolling):
            await channel.run(on_message)

    # Every one of the 6 simulated failures logged a warning; none of them
    # escaped as an unhandled exception out of run() (only our own
    # deliberate sentinel, raised by the 7th call, did).
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("Telegram getUpdates failed" in m for m in warning_msgs) == 6
    await channel.aclose()


# ---------------------------------------------------------------------------
# AC3.2 -- HealthMonitor: exactly one channel alert per UP->DOWN
# transition; no repeat alert until UP then DOWN again; alert still
# logged even when the channel send itself fails.
# ---------------------------------------------------------------------------


def _health_handler(ollama_up_sequence: list[bool], telegram_up_sequence: list[bool]):
    calls = {"ollama": 0, "telegram": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "mac-mini":
            idx = min(calls["ollama"], len(ollama_up_sequence) - 1)
            calls["ollama"] += 1
            up = ollama_up_sequence[idx]
            if up:
                return httpx.Response(200, json={"version": "0.1.0"})
            raise httpx.ConnectError("ollama simulated down", request=request)
        if host == "api.telegram.org":
            idx = min(calls["telegram"], len(telegram_up_sequence) - 1)
            calls["telegram"] += 1
            up = telegram_up_sequence[idx]
            if up:
                return httpx.Response(200, json={"ok": True, "result": {}})
            raise httpx.ConnectError("telegram simulated down", request=request)
        raise AssertionError(f"unexpected host contacted: {host}")

    return handler


async def test_exactly_one_alert_per_up_to_down_transition_no_repeat_while_still_down():
    # up, down, down, down, up (recovers), down, down (new transition)
    sequence = [True, False, False, False, True, False, False]
    transport = httpx.MockTransport(_health_handler(sequence, [True] * len(sequence)))
    client = httpx.AsyncClient(transport=transport)
    channel = _RecordingChannel()
    recovered_calls = {"n": 0}

    async def on_recovered() -> None:
        recovered_calls["n"] += 1

    monitor = HealthMonitor(
        "http://mac-mini:11434",
        "fake-token",
        client=client,
        channel=channel,
        on_ollama_recovered=on_recovered,
    )

    for _ in sequence:
        await monitor.run_once()

    assert channel.sent == [OLLAMA_DOWN_MESSAGE, OLLAMA_DOWN_MESSAGE]  # exactly 2 alerts
    assert recovered_calls["n"] == 1  # exactly one DOWN->UP transition
    await client.aclose()


async def test_no_new_alert_while_still_down_new_alert_only_after_up_then_down_again():
    sequence = [True, False, False, False, False, False]  # never recovers in this run
    transport = httpx.MockTransport(_health_handler(sequence, [True] * len(sequence)))
    client = httpx.AsyncClient(transport=transport)
    channel = _RecordingChannel()
    monitor = HealthMonitor("http://mac-mini:11434", "fake-token", client=client, channel=channel)

    for _ in sequence:
        await monitor.run_once()

    assert channel.sent == [OLLAMA_DOWN_MESSAGE]  # only the first DOWN edge alerts
    await client.aclose()


async def test_alert_still_logged_when_channel_send_itself_fails(caplog):
    import logging

    sequence = [True, False]
    transport = httpx.MockTransport(_health_handler(sequence, [True, True]))
    client = httpx.AsyncClient(transport=transport)
    failing_channel = _RecordingChannel(fail=True)
    monitor = HealthMonitor(
        "http://mac-mini:11434", "fake-token", client=client, channel=failing_channel
    )

    with caplog.at_level(logging.WARNING, logger="habit_assistant"):
        await monitor.run_once()  # up
        await monitor.run_once()  # down -- alert send raises, must not propagate

    assert failing_channel.sent == []  # the send itself failed, nothing recorded
    messages = [r.message for r in caplog.records]
    assert any(OLLAMA_DOWN_MESSAGE in m for m in messages)  # log is still the record
    await client.aclose()


# ---------------------------------------------------------------------------
# AC3.5 -- only mac-mini:11434 and api.telegram.org are ever contacted,
# across every resilience path (Telegram backoff/poll/send, Ollama
# retry, HealthMonitor checks).
# ---------------------------------------------------------------------------


async def test_only_allowed_hosts_contacted_across_all_resilience_paths(monkeypatch):
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("habit_assistant.channels.telegram.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("habit_assistant.llm.ollama_client.asyncio.sleep", fake_sleep)

    hosts_hit: set[str] = set()
    ollama_chat_calls = {"n": 0}
    poll_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hosts_hit.add(request.url.host)
        if request.url.host not in {"mac-mini", "api.telegram.org"}:
            raise AssertionError(f"unexpected host contacted: {request.url.host}")

        path = request.url.path
        if path.endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.1.0"})
        if path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path.endswith("/api/chat"):
            ollama_chat_calls["n"] += 1
            if ollama_chat_calls["n"] == 1:
                raise httpx.ConnectError("simulated transport blip", request=request)
            return httpx.Response(200, json={"message": {"content": json_payload()}})
        if path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}})
        if path.endswith("/getUpdates"):
            poll_calls["n"] += 1
            if poll_calls["n"] > 1:
                raise _StopPolling()
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"unexpected path contacted: {path}")

    transport = httpx.MockTransport(handler)
    shared_client = httpx.AsyncClient(transport=transport)

    telegram_channel = TelegramChannel("tok", "chat", client=shared_client)
    ollama_client = OllamaClient(
        "http://mac-mini:11434",
        ["m1"],
        client=shared_client,
        retry_attempts=1,
        retry_backoff_seconds=0.01,
    )
    health = HealthMonitor(
        "http://mac-mini:11434", "tok", client=shared_client, channel=telegram_channel
    )

    # Exercise every resilience path against the shared client/transport.
    await telegram_channel.send("alert-style message")
    await ollama_client.chat_json("sys", "usr", EXTRACTION_JSON_SCHEMA)  # 1 retry then success
    await health.run_once()

    async def on_message(text: str) -> None:
        pass

    with pytest.raises(_StopPolling):
        await telegram_channel.run(on_message)

    assert hosts_hit == {"mac-mini", "api.telegram.org"}
    await shared_client.aclose()


# ---------------------------------------------------------------------------
# AC3.3 -- inbound message while LLM is DOWN: ack + `unparsed` row + LLM
# never called; excluded from aggregations; persists across a Database
# close/reopen; re-parse on recovery reclassifies + confirms + re-includes
# in aggregations; startup backlog re-parsed with no in-process transition.
# ---------------------------------------------------------------------------


async def test_deferred_message_acks_writes_unparsed_row_and_never_calls_llm(db):
    channel = _RecordingChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "500ml please",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        health_monitor=health_monitor,
    )

    assert channel.sent == [DEFERRED_ACK_MESSAGE]
    rows = db.logs_between(*ANY_TIME_WINDOW)
    assert len(rows) == 1
    assert rows[0]["category"] == "unparsed"
    assert rows[0]["raw_message"] == "500ml please"
    assert rows[0]["value_num"] is None
    assert rows[0]["value_text"] is None


async def test_deferred_row_excluded_from_aggregations_while_pending(db):
    channel = _RecordingChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "500ml please",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        health_monitor=health_monitor,
        clock=lambda: __import__("datetime").datetime(2026, 8, 19, 10, 0, 0),
    )

    assert db.water_total_ml("2026-08-19") == 0.0
    assert db.stretch_count("2026-08-19") == 0
    assert db.diary_count("2026-08-19") == 0


async def test_deferred_row_persists_across_database_close_and_reopen(tmp_path):
    """AC3.3's persistence requirement: a *closed and reopened* Database
    (simulating a full process restart) must still see the deferred row --
    this is a plain SQL query against a real on-disk file, not an
    in-memory queue that a restart would silently lose."""
    db_path = tmp_path / "restart.db"
    database = Database(db_path)
    channel = _RecordingChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "10 min stretch",
        db=database,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        health_monitor=health_monitor,
    )
    database.close()  # simulates process exit

    reopened = Database(db_path)  # simulates process restart
    pending = reopened.pending_unparsed()
    assert len(pending) == 1
    assert pending[0]["raw_message"] == "10 min stretch"
    assert pending[0]["category"] == "unparsed"
    reopened.close()


async def test_reparse_on_recovery_reclassifies_confirms_and_reincludes_in_aggregations(db):
    channel = _RecordingChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    fixed_clock = lambda: __import__("datetime").datetime(2026, 8, 19, 10, 0, 0)
    await handle_inbound_message(
        "500ml please",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        health_monitor=health_monitor,
        clock=fixed_clock,
    )
    assert db.water_total_ml("2026-08-19") == 0.0  # excluded while pending

    content = json_payload(category="water", water_ml=500, confidence=0.9)
    recovering_llm = _StaticLLM(content)

    await reparse_pending_unparsed(db, recovering_llm, channel, config)

    assert channel.sent == [
        DEFERRED_ACK_MESSAGE,
        "\U0001f501 Recovered: 500 ml logged from your earlier message.",
    ]
    rows = db.logs_between(*ANY_TIME_WINDOW)
    assert len(rows) == 1
    assert rows[0]["category"] == "water"
    assert rows[0]["value_num"] == 500.0
    assert rows[0]["raw_message"] == "500ml please"  # original text kept intact
    assert db.pending_unparsed() == []
    assert db.water_total_ml("2026-08-19") == 500.0  # now counted


async def test_startup_backlog_reparsed_with_no_in_process_transition(db):
    """The `main.py` startup call site: a row deferred by a *previous*
    process run (inserted directly here, simulating what a previous run
    left behind) is picked up by `reparse_pending_unparsed` on its own --
    no HealthMonitor, no DOWN->UP transition, ever constructed in this
    test."""
    from habit_assistant.storage.models import LogEntry

    db.insert_log(
        LogEntry(None, "2026-08-19T09:00:00", "unparsed", None, None, "did 10 min stretch", "reply")
    )
    assert len(db.pending_unparsed()) == 1

    channel = _RecordingChannel()
    config = Config()
    content = json_payload(category="stretch", stretch_min=10, confidence=0.9)
    recovering_llm = _StaticLLM(content)

    await reparse_pending_unparsed(db, recovering_llm, channel, config)

    assert db.pending_unparsed() == []
    rows = db.logs_between(*ANY_TIME_WINDOW)
    assert rows[0]["category"] == "stretch"
    assert rows[0]["value_num"] == 10.0
    assert channel.sent == ["\U0001f501 Recovered: 10 min stretch logged from your earlier message."]


async def test_reparse_leaves_genuinely_unparseable_row_as_unparsed(db):
    """A row still not parseable after Ollama recovers (bad input, not an
    outage) stays `unparsed` rather than being silently dropped or
    force-classified."""
    from habit_assistant.storage.models import LogEntry

    db.insert_log(
        LogEntry(None, "2026-08-19T09:00:00", "unparsed", None, None, "asdkjhasd", "reply")
    )

    channel = _RecordingChannel()
    config = Config()
    still_unknown_llm = _StaticLLM(json_payload(category="unknown", confidence=0.0))

    await reparse_pending_unparsed(db, still_unknown_llm, channel, config)

    pending = db.pending_unparsed()
    assert len(pending) == 1
    assert pending[0]["category"] == "unparsed"
    assert channel.sent == []  # no recovery confirmation for a row that's still unparseable


# ---------------------------------------------------------------------------
# Migration 002 (idx_logs_category): fresh DB, from a v1 DB, idempotent.
# ---------------------------------------------------------------------------


def _has_index(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def test_migration_002_creates_category_index_on_a_fresh_db(tmp_path):
    database = Database(tmp_path / "fresh.db")

    assert database.schema_version == len(MIGRATIONS)  # ROADMAP.md v0.5.0 added migration 003
    assert _has_index(database._conn, "idx_logs_category")
    database.close()


def test_migration_002_applies_forward_from_a_v1_db():
    """A DB that already has migration 001 applied (user_version=1, no
    category index yet) must pick up migration 002 on the next open.
    Runs only migrations 001-002 (not the full, possibly-longer MIGRATIONS
    list) since this test is specifically about the 001->002 transition."""
    conn = sqlite3.connect(":memory:")
    try:
        from_version, to_version = run_migrations(conn, migrations=MIGRATIONS[:1])
        assert (from_version, to_version) == (0, 1)
        assert current_version(conn) == 1
        assert not _has_index(conn, "idx_logs_category")

        from_version2, to_version2 = run_migrations(conn, migrations=MIGRATIONS[:2])
        assert (from_version2, to_version2) == (1, 2)
        assert current_version(conn) == 2
        assert _has_index(conn, "idx_logs_category")
    finally:
        conn.close()


def test_migration_002_is_idempotent(tmp_path):
    db_path = tmp_path / "idem.db"

    first = Database(db_path)
    assert first.schema_version == len(MIGRATIONS)  # ROADMAP.md v0.5.0 added migration 003
    first.close()

    second = Database(db_path)
    assert second.schema_version_before == len(MIGRATIONS)  # nothing pending
    assert second.schema_version == len(MIGRATIONS)
    assert _has_index(second._conn, "idx_logs_category")
    second.close()
