"""SPEC-v1.10.md "Never lose a log" -- shared-surface tests, built ahead of
the three parallel modules (M1 `clarify`, M2 `reply_attribution`/
`discoverability`, M3 `riders`) that consume this surface (SPEC-v1.10.md
§11): migration 013 + the unparsed-state machine's CAS methods (AC1/AC3,
the release's own load-bearing race guard -- tested hard per Archi's
directive: wrong-origin no-ops, "concurrent" one-winner), `Channel.send`
returning a message id (AC2), the `ReminderState` reminder-context map
(R-SS6), `pause.is_paused_safe`/`active_pauses_safe` fail-open helpers
(R-SS9), `/guide` recognition + reservation (AC4), config defaults, and
`RELEASE_NOTES["1.10.0"]`.

No mocks for the DB (real on-disk SQLite via tmp_path, mirroring
tests/test_v19_shared_surface.py's own convention); Telegram is mocked via
`httpx.MockTransport`.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from conftest import FakeOllamaClient, RecordingChannel

from habit_assistant.channels.base import Channel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import (
    DEFAULT_CONFIG_PATH,
    ClarifyConfig,
    Config,
    OutageConfig,
    ReplyToReminderConfig,
    load_config,
)
from habit_assistant.core import commands, pause
from habit_assistant.core.reminders import ReminderState, send_reminder
from habit_assistant.core.release_notes import RELEASE_NOTES, get_release_note
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.routing import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS
from habit_assistant.storage.models import LogEntry

OWNER = "owner"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


def _insert_unparsed(db_: Database, raw: str = "500", unparsed_state: str | None = None) -> int:
    return db_.insert_log(
        LogEntry(None, OWNER, "2026-08-27T10:00:00", "unparsed", None, None, raw, "reply", unparsed_state=unparsed_state)
    )


# ===========================================================================
# AC1 -- migration 013.
# ===========================================================================


def test_migration_count_is_13():
    assert len(MIGRATIONS) == 14


def test_fresh_db_has_unparsed_state_column_default_null(db):
    assert db.schema_version == 14
    cols = {row[1] for row in db._conn.execute("PRAGMA table_info(logs)")}
    assert "unparsed_state" in cols
    log_id = _insert_unparsed(db)
    row = db.get_log(log_id)
    assert row["unparsed_state"] is None


def test_migration_013_upgrades_a_v19_db_and_preserves_the_existing_unparsed_row(tmp_path):
    """AC1: given a v1.9 DB at user_version=12 with a pre-existing
    `category='unparsed'` row (the exact "id=13 / Streaching" zombie shape
    §1 describes), migration 013 adds `logs.unparsed_state` (default
    NULL), touches no existing row's other columns, stamps 13, and is
    idempotent on a second open."""
    path = tmp_path / "v19.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE logs (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          TEXT NOT NULL,
          category    TEXT NOT NULL,
          value_num   REAL,
          value_text  TEXT,
          raw_message TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'reply',
          created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
          deleted_at  TEXT NULL,
          habit_type  TEXT NULL,
          user_id     TEXT NULL
        )
        """
    )
    # SPEC-LINE.md §4 R-S4 (branch `line-version`): migration 014, applied
    # right after 013 on THIS same fixture, ALTERs `users` -- a real v12 DB
    # always has that table (migration 006 created it long before 012), so
    # this synthetic seed needs it too for migration 014 to have something
    # to ALTER, even though this test's own assertions below never touch it.
    conn.execute(
        """
        CREATE TABLE users (
          chat_id                 TEXT PRIMARY KEY,
          role                    TEXT NOT NULL DEFAULT 'member',
          status                  TEXT NOT NULL DEFAULT 'pending',
          display_name            TEXT,
          language_pref           TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json        TEXT,
          snooze_default_minutes  INTEGER,
          checkin_window          TEXT NULL,
          last_announced_version  TEXT NULL,
          dashboard_msg_id        TEXT NULL,
          created_at              TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("INSERT INTO users (chat_id, role, status) VALUES ('owner', 'owner', 'active')")
    conn.execute(
        "INSERT INTO logs (id, ts, category, value_num, value_text, raw_message, source, user_id) "
        "VALUES (13, '2026-08-25T10:00:00', 'unparsed', NULL, NULL, 'Streaching', 'reply', 'owner')"
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 12")
    conn.close()

    database = Database(path)
    try:
        assert database.schema_version_before == 12
        assert database.schema_version == 14
        row = database.get_log(13)
        assert row["category"] == "unparsed"
        assert row["raw_message"] == "Streaching"
        assert row["value_num"] is None
        assert row["unparsed_state"] is None
        # AC1's own point -- this zombie row is now eligible for the new
        # machinery (R-SS2), where before it had no terminal state at all.
        assert 13 in {r["id"] for r in database.pending_unparsed()}
    finally:
        database.close()

    reopened = Database(path)
    try:
        assert reopened.schema_version_before == 14
        assert reopened.schema_version == 14
    finally:
        reopened.close()


# ===========================================================================
# AC3 -- pending_unparsed() predicate (R-SS2).
# ===========================================================================


def test_pending_unparsed_includes_null_and_awaiting_llm_excludes_clarify_and_closed(db):
    legacy_id = _insert_unparsed(db, "legacy-null", unparsed_state=None)
    awaiting_id = _insert_unparsed(db, "awaiting", unparsed_state="awaiting_llm")
    clarify_id = _insert_unparsed(db, "clarify-me", unparsed_state="awaiting_clarify")
    closed_id = _insert_unparsed(db, "closed-already", unparsed_state="closed")

    ids = {row["id"] for row in db.pending_unparsed()}

    assert legacy_id in ids
    assert awaiting_id in ids
    assert clarify_id not in ids
    assert closed_id not in ids


def test_pending_unparsed_excludes_a_reclassified_row(db):
    log_id = _insert_unparsed(db, "500")
    db.resolve_unparsed(
        log_id, from_states=(None, "awaiting_llm"), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )
    assert log_id not in {row["id"] for row in db.pending_unparsed()}


# ===========================================================================
# AC3 -- resolve_unparsed / mark_unparsed_state: the CAS race guard (R-SS3/
# R11). Tested hard per Archi's directive: wrong-origin no-ops, a
# "concurrent" pair of calls resolves to exactly one winner.
# ===========================================================================


def test_resolve_unparsed_succeeds_from_null_origin_and_clears_state(db):
    log_id = _insert_unparsed(db, "500")

    won = db.resolve_unparsed(
        log_id, from_states=(None, "awaiting_llm"), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )

    assert won is True
    row = db.get_log(log_id)
    assert row["category"] == "water"
    assert row["value_num"] == 500.0
    assert row["habit_type"] == "numeric"
    assert row["unparsed_state"] is None


def test_resolve_unparsed_is_a_noop_from_the_wrong_origin(db):
    log_id = _insert_unparsed(db, "500", unparsed_state="closed")

    won = db.resolve_unparsed(
        log_id, from_states=(None, "awaiting_llm"), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )

    assert won is False
    row = db.get_log(log_id)
    assert row["category"] == "unparsed"
    assert row["unparsed_state"] == "closed"


def test_resolve_unparsed_is_a_noop_on_an_already_resolved_row(db):
    """R11's own "no double log" case: a row that already won a race
    (category no longer 'unparsed') never resolves a second time, even
    from an origin set that would otherwise match."""
    log_id = _insert_unparsed(db, "500")
    first = db.resolve_unparsed(
        log_id, from_states=(None, "awaiting_llm"), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )
    assert first is True

    second = db.resolve_unparsed(
        log_id, from_states=(None, "awaiting_llm"), category="water", value_num=999.0, value_text=None, habit_type="numeric"
    )

    assert second is False
    row = db.get_log(log_id)
    assert row["value_num"] == 500.0  # the loser's value never applied


def test_mark_unparsed_state_concurrent_pair_exactly_one_winner(db):
    """Simulates the sweep-vs-tap race (R11/AC11's own foundation): two
    CAS calls racing the same row from the SAME origin set -- exactly one
    must win (rowcount 1), the other must observe rowcount 0 and change
    nothing further."""
    log_id = _insert_unparsed(db, "Streaching")

    first = db.mark_unparsed_state(log_id, from_states=(None, "awaiting_llm"), to_state="closed")
    second = db.mark_unparsed_state(log_id, from_states=(None, "awaiting_llm"), to_state="closed")

    assert first is True
    assert second is False
    row = db.get_log(log_id)
    assert row["unparsed_state"] == "closed"


def test_disjoint_origin_sets_never_collide_sweep_vs_tap(db):
    """R11's own precondition: the sweep's guard (NULL/'awaiting_llm') and
    the tap's guard ('awaiting_clarify' only, no NULL branch) are disjoint
    -- a sweep attempt on an already-offered row is a no-op, and the tap's
    own resolve still succeeds afterward."""
    log_id = _insert_unparsed(db, "500", unparsed_state="awaiting_clarify")

    # The sweep (mark_unparsed_state from NULL/awaiting_llm) must not touch
    # a row already sitting in awaiting_clarify.
    sweep_result = db.mark_unparsed_state(log_id, from_states=(None, "awaiting_llm"), to_state="closed")
    assert sweep_result is False
    assert db.get_log(log_id)["unparsed_state"] == "awaiting_clarify"

    # The tap (resolve_unparsed from awaiting_clarify only) succeeds.
    tap_result = db.resolve_unparsed(
        log_id, from_states=("awaiting_clarify",), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )
    assert tap_result is True
    assert db.get_log(log_id)["category"] == "water"


def test_mark_unparsed_state_from_named_origin_only_does_not_match_null_row(db):
    """A `from_states` tuple that does NOT include `None` (the tap's own
    guard shape) must never match a legacy/awaiting-llm (NULL) row."""
    log_id = _insert_unparsed(db, "500", unparsed_state=None)

    result = db.mark_unparsed_state(log_id, from_states=("awaiting_clarify",), to_state="closed")

    assert result is False
    assert db.get_log(log_id)["unparsed_state"] is None


def test_resolve_unparsed_guards_on_category_still_unparsed(db):
    """A row that is no longer `category='unparsed'` at all (e.g. an
    ordinary log) can never be "resolved" by either CAS method, even if
    its (irrelevant) unparsed_state happens to be NULL."""
    log_id = db.insert_log(LogEntry(None, OWNER, "2026-08-27T10:00:00", "water", 300.0, None, "300ml", "reply"))

    result = db.resolve_unparsed(
        log_id, from_states=(None, "awaiting_llm"), category="stretch", value_num=10.0, value_text=None, habit_type="duration"
    )

    assert result is False
    row = db.get_log(log_id)
    assert row["category"] == "water"
    assert row["value_num"] == 300.0


# ===========================================================================
# AC2 -- Channel.send returns the message id (R-SS5).
# ===========================================================================


async def test_telegram_send_returns_the_stringified_message_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 909}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    message_id = await channel.send("999", "hello")

    assert message_id == "909"
    await channel.aclose()


async def test_telegram_send_returns_none_when_result_has_no_message_id():
    """Defensive degradation (not a spec requirement of the real Telegram
    API, but this codebase's own pre-existing test doubles mock a bare
    `{"result": {}}` at many call sites) -- a missing id is a valid
    `None`, never a raised exception."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    message_id = await channel.send("999", "hello")

    assert message_id is None
    await channel.aclose()


async def test_telegram_send_payload_is_byte_identical_at_default():
    """AC2: the default-False payload is unaffected by the return-type
    change."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.send("999", "hello world")

    import json as _json

    assert _json.loads(captured[0].content) == {"chat_id": "999", "text": "hello world"}
    await channel.aclose()


# ===========================================================================
# R-SS6 -- ReminderState.reminder_context (remember_reminder/habit_for_reply).
# ===========================================================================


def test_remember_reminder_and_habit_for_reply_roundtrip():
    state = ReminderState()

    state.remember_reminder("chat1", "100", "water", cap=32)

    assert state.habit_for_reply("chat1", "100") == "water"
    assert state.habit_for_reply("chat1", "999") is None  # unmapped message id
    assert state.habit_for_reply("chat2", "100") is None  # different chat


def test_remember_reminder_evicts_oldest_beyond_cap():
    state = ReminderState()
    for i in range(5):
        state.remember_reminder("chat1", str(i), f"habit{i}", cap=3)

    assert state.habit_for_reply("chat1", "0") is None
    assert state.habit_for_reply("chat1", "1") is None
    assert state.habit_for_reply("chat1", "2") == "habit2"
    assert state.habit_for_reply("chat1", "3") == "habit3"
    assert state.habit_for_reply("chat1", "4") == "habit4"


def test_remember_reminder_cap_is_per_chat_not_global():
    state = ReminderState()
    for i in range(3):
        state.remember_reminder("chatA", str(i), "water", cap=2)
    for i in range(3):
        state.remember_reminder("chatB", str(i), "stretch", cap=2)

    assert len(state.reminder_context["chatA"]) == 2
    assert len(state.reminder_context["chatB"]) == 2


async def test_send_reminder_records_reminder_context_when_send_returns_an_id():
    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    state = ReminderState()
    channel = RecordingChannel()

    await send_reminder(channel, OWNER, water, "en", db=None, config=config, state=state)

    assert state.last_habit_id[OWNER] == "water"
    assert channel.sent != []
    assert state.habit_for_reply(OWNER, "1") == "water"  # RecordingChannel's first synthetic id


async def test_send_reminder_state_unaffected_when_channel_returns_no_id():
    """Byte-identical fallback: a channel that can't provide an id (most
    pre-1.10 fakes) simply records nothing extra -- `last_habit_id` still
    updates exactly as before."""

    class _NoIdChannel(Channel):
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send(self, chat_id, text, *, disable_notification=False):
            self.sent.append((chat_id, text))
            return None

        async def run(self, on_message, on_callback=None):
            raise NotImplementedError

    config = Config()
    registry = HabitRegistry.from_config(config)
    water = registry.get("water")
    state = ReminderState()
    channel = _NoIdChannel()

    await send_reminder(channel, OWNER, water, "en", db=None, config=config, state=state)

    assert state.last_habit_id[OWNER] == "water"
    assert state.reminder_context == {}


# ===========================================================================
# R-SS9 -- pause.is_paused_safe / active_pauses_safe.
# ===========================================================================


def test_is_paused_safe_passes_through_on_success(db):
    from datetime import date

    assert pause.is_paused_safe(db, Config(), OWNER, "water", date(2026, 8, 27)) is False
    db.insert_pause(OWNER, "water", "2026-08-27", "2026-08-28")
    assert pause.is_paused_safe(db, Config(), OWNER, "water", date(2026, 8, 27)) is True


def test_is_paused_safe_fail_opens_to_false_on_read_error():
    from datetime import date

    class _BrokenDb:
        def active_pauses(self, user_id):
            raise RuntimeError("db hiccup")

    assert pause.is_paused_safe(_BrokenDb(), Config(), OWNER, "water", date(2026, 8, 27)) is False


def test_active_pauses_safe_passes_through_on_success(db):
    assert pause.active_pauses_safe(db, OWNER) == []
    db.insert_pause(OWNER, None, "2026-08-27", "2026-08-28")
    assert len(pause.active_pauses_safe(db, OWNER)) == 1


def test_active_pauses_safe_fail_opens_to_empty_list_on_read_error():
    class _BrokenDb:
        def active_pauses(self, user_id):
            raise RuntimeError("db hiccup")

    assert pause.active_pauses_safe(_BrokenDb(), OWNER) == []


# ===========================================================================
# AC4 -- /guide recognition + reservation (R-SS8).
# ===========================================================================


def test_guide_slash_and_thai_alias_dispatch_to_guide_kind():
    registry = HabitRegistry.from_config(Config())

    en = commands.dispatch("/guide", registry)
    th = commands.dispatch("คู่มือ", registry)

    assert en is not None and en.kind == "guide"
    assert th is not None and th.kind == "guide"


def test_guide_is_case_insensitive_slash_form():
    registry = HabitRegistry.from_config(Config())
    result = commands.dispatch("/GUIDE", registry)
    assert result is not None and result.kind == "guide"


def test_guide_does_not_fire_on_ordinary_text():
    registry = HabitRegistry.from_config(Config())
    assert commands.dispatch("here is my guide to life", registry) is None
    assert commands.dispatch("คู่มือการใช้งาน", registry) is None  # trailing text -> not a bare match


def test_guide_and_thai_alias_are_reserved_trigger_words():
    reserved = commands.reserved_trigger_words()
    assert "guide" in reserved
    assert "คู่มือ" in reserved


def test_dispatch_invariants_still_hold_after_guide_insertion():
    """Re-verifies the table-driven dispatcher's own structural guard
    (SPEC-REFACTOR.md Stage 3 rule 14) still passes at import time with the
    28th row added -- if this import itself raised, this whole test module
    would already have failed to collect, so this test also documents the
    intent for a reader."""
    commands._assert_dispatch_invariants(commands._MATCHERS)
    kinds = [m.kind for m in commands._MATCHERS]
    assert kinds[-1] == "query"
    assert kinds.index("guide") < kinds.index("query")


# ===========================================================================
# Config defaults ([outage]/[clarify]/[reply_to_reminder]).
# ===========================================================================


def test_config_defaults_match_spec_5():
    config = Config()
    assert config.outage == OutageConfig(honest_reply=True)
    assert config.clarify == ClarifyConfig(enabled=True, max_guesses=4, plausibility_lower=0.05, plausibility_upper=5.0)
    assert config.reply_to_reminder == ReplyToReminderConfig(enabled=True, context_cap=32)


def test_config_toml_absent_sections_use_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[app]\ntimezone = "Asia/Bangkok"\n', encoding="utf-8")
    config = load_config(path)
    assert config.outage.honest_reply is True
    assert config.clarify.enabled is True
    assert config.reply_to_reminder.context_cap == 32


def test_config_toml_sections_are_overridable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[outage]\nhonest_reply = false\n"
        "[clarify]\nenabled = false\nmax_guesses = 2\nplausibility_lower = 0.1\nplausibility_upper = 3.0\n"
        "[reply_to_reminder]\nenabled = false\ncontext_cap = 8\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.outage.honest_reply is False
    assert config.clarify.enabled is False
    assert config.clarify.max_guesses == 2
    assert config.clarify.plausibility_lower == 0.1
    assert config.clarify.plausibility_upper == 3.0
    assert config.reply_to_reminder.enabled is False
    assert config.reply_to_reminder.context_cap == 8


def test_repo_config_toml_loads_cleanly_with_v110_sections():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config.outage.honest_reply is True
    assert config.clarify.enabled is True
    assert config.clarify.max_guesses == 4
    assert config.reply_to_reminder.enabled is True
    assert config.reply_to_reminder.context_cap == 32


@pytest.mark.parametrize("value", [0, -1])
def test_clarify_max_guesses_must_be_positive(value):
    with pytest.raises(Exception):
        ClarifyConfig(max_guesses=value)


@pytest.mark.parametrize("value", [0, -5])
def test_reply_to_reminder_context_cap_must_be_positive(value):
    with pytest.raises(Exception):
        ReplyToReminderConfig(context_cap=value)


# ===========================================================================
# RELEASE_NOTES["1.10.0"].
# ===========================================================================


def test_release_notes_1_10_0_has_both_languages_and_headline():
    en = get_release_note("1.10.0", "en")
    th = get_release_note("1.10.0", "th")
    assert en is not None and en.startswith("🎉 What's new in v1.10.0")
    assert th is not None and th.startswith("🎉 มีอะไรใหม่ใน v1.10.0")
    assert en != th
    assert "/guide" in en and "/guide" in th


def test_release_notes_1_10_0_mentions_the_four_headline_bullets():
    en = get_release_note("1.10.0", "en")
    assert "log" in en.lower()  # never lose a log
    assert "reply" in en.lower()  # reply-to-reminder
    assert "offline" in en.lower() or "outage" in en.lower()
    assert "/guide" in en


def test_release_notes_catalog_structural_check():
    assert set(RELEASE_NOTES["1.10.0"].keys()) == {"en", "th"}


# ===========================================================================
# R-SS7 -- inbound reply_to_message_id plumbing (TelegramChannel.run ->
# on_message). Plumbing only: TelegramChannel.run's own extraction, and
# core/routing.py:handle_inbound_message's inert acceptance of the new
# trailing kwarg.
# ===========================================================================


async def test_run_passes_the_reply_to_message_id_to_on_message():
    calls: list[tuple] = []
    responses = [
        {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": "chat"},
                        "text": "500",
                        "message_id": 8802,
                        "reply_to_message": {"message_id": 8801},
                    },
                }
            ],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            raise _StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = TelegramChannel("token", "chat", client=client)

    async def on_message(
        chat_id: str,
        text: str,
        display_name: str | None = None,
        message_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> None:
        calls.append((chat_id, text, display_name, message_id, reply_to_message_id))

    with pytest.raises(_StopPolling):
        await channel.run(on_message)

    assert calls == [("chat", "500", None, "8802", "8801")]
    await channel.aclose()


async def test_run_passes_none_when_the_message_is_not_a_reply():
    calls: list[str | None] = []
    responses = [
        {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": "chat"}, "text": "500ml", "message_id": 5}}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            raise _StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channel = TelegramChannel("token", "chat", client=client)

    async def on_message(
        chat_id: str,
        text: str,
        display_name: str | None = None,
        message_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> None:
        calls.append(reply_to_message_id)

    with pytest.raises(_StopPolling):
        await channel.run(on_message)

    assert calls == [None]
    await channel.aclose()


class _StopPolling(Exception):
    """Sentinel, mirrors tests/test_channels.py's own convention: raised by
    a test transport once its canned responses are exhausted, to end
    `TelegramChannel.run`'s `while True` loop -- NOT caught by `run`'s own
    `except httpx.HTTPError`, so it propagates out and ends the test."""


async def test_handle_inbound_message_accepts_reply_to_message_id_kwarg_inert(db):
    """Plumbing-only: threading `reply_to_message_id` through does not
    change *how* `handle_inbound_message` behaves for an ordinary message
    -- no feature logic reads it yet (that lands at the routing.py
    integration pass, module `reply_attribution`'s own R13 wiring)."""
    FakeOllamaClient.responses = ['{"category": "water", "value": 250, "confidence": 0.9}']
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = RecordingChannel()

    await handle_inbound_message(
        "250ml",
        db=db,
        llm=FakeOllamaClient(),
        channel=channel,
        config=config,
        user_id=OWNER,
        registry=registry,
        reply_to_message_id="123456",
    )

    assert channel.sent != []  # a normal confirmation was sent, same as without the kwarg
