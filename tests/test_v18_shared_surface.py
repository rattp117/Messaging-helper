"""SPEC-v1.8.md "One-tap quick-log keyboard + reactions, routines,
backfill, gentle riders" -- shared-surface tests, built ahead of the four
parallel modules (`quicklog`, `routines`, `backfill`, `riders`) that
consume this surface (SPEC-v1.8.md §11): channel payloads (AC-1/AC-2/AC-3),
inbound message_id plumbing (AC-4), config defaults (AC-5), audit vocab
(AC-6), release notes (AC-7), reserved words (AC-8), and the AC-9 "inert
until invoked" spirit for every knob this pass adds.

No mocks for the DB (real on-disk SQLite via tmp_path, mirroring
tests/test_v11_shared_surface.py's own convention); Telegram/Ollama are
faked/mocked.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Awaitable, Callable

import httpx
import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import (
    DEFAULT_CONFIG_PATH,
    BackfillConfig,
    Config,
    NotificationsConfig,
    QuicklogConfig,
    ReactionsConfig,
    RoutinesConfig,
    load_config,
)
from habit_assistant.core import audit, audit_view, commands, habitdef, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.release_notes import RELEASE_NOTES, get_release_note
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database

OWNER = "owner"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"


def patch_parse_message(monkeypatch, result: ExtractionResult) -> None:
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


# ---------------------------------------------------------------------------
# AC-1: Channel.send(disable_notification=)
# ---------------------------------------------------------------------------


def test_build_send_request_default_is_byte_identical_to_v17():
    channel = TelegramChannel("123456:ABC-fake", "999")
    url, payload = channel.build_send_request("999", "hello")
    assert url == "https://api.telegram.org/bot123456:ABC-fake/sendMessage"
    assert payload == {"chat_id": "999", "text": "hello"}
    assert "disable_notification" not in payload


def test_build_send_request_disable_notification_true_adds_the_field():
    channel = TelegramChannel("123456:ABC-fake", "999")
    _, payload = channel.build_send_request("999", "hello", disable_notification=True)
    assert payload["disable_notification"] is True


async def test_send_posts_disable_notification_field_only_when_true():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.send("999", "hi")
    await channel.send("999", "hi", disable_notification=True)

    assert "disable_notification" not in captured[0]
    assert captured[1]["disable_notification"] is True
    await channel.aclose()


def test_send_actionable_and_send_and_pin_requests_are_unaffected():
    """R-S1: "No other send method changes" -- send_actionable/send_and_pin
    both build their request via build_send_request with no
    disable_notification argument, so their payload shape is untouched."""
    channel = TelegramChannel("123456:ABC-fake", "999")
    _, payload = channel.build_send_actionable_request("999", "hi", [])
    assert "disable_notification" not in payload


# ---------------------------------------------------------------------------
# AC-2: Channel.set_message_reaction
# ---------------------------------------------------------------------------


class _BareChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def run(self, on_message, on_callback=None):
        raise NotImplementedError("not exercised in these tests")


async def test_set_message_reaction_default_is_a_silent_noop():
    channel = _BareChannel()
    result = await channel.set_message_reaction("chat1", "msg-1", "💧")
    assert result is None


def test_build_set_message_reaction_request_shape():
    channel = TelegramChannel("123456:ABC-fake", "999")
    url, payload = channel.build_set_message_reaction_request("999", "555", "💧")
    assert url == "https://api.telegram.org/bot123456:ABC-fake/setMessageReaction"
    assert payload == {
        "chat_id": "999",
        "message_id": "555",
        "reaction": [{"type": "emoji", "emoji": "💧"}],
    }


async def test_set_message_reaction_posts_the_request():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.set_message_reaction("999", "555", "💧")

    assert len(captured) == 1
    assert captured[0].url.path.endswith("/setMessageReaction")
    await channel.aclose()


async def test_set_message_reaction_never_raises_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    result = await channel.set_message_reaction("999", "555", "💧")  # must not raise
    assert result is None
    await channel.aclose()


async def test_set_message_reaction_never_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    result = await channel.set_message_reaction("999", "555", "💧")  # must not raise
    assert result is None
    await channel.aclose()


# ---------------------------------------------------------------------------
# AC-3: Channel.set_my_commands(scope_chat_id=)
# ---------------------------------------------------------------------------


def test_set_my_commands_default_scope_is_byte_identical_to_v17():
    channel = TelegramChannel("123456:ABC-fake", "999")
    requests = channel.build_set_my_commands_requests({"en": [("undo", "Undo the last entry")]})
    assert len(requests) == 1
    _, payload = requests[0]
    assert "scope" not in payload
    assert payload == {"commands": [{"command": "undo", "description": "Undo the last entry"}]}


def test_set_my_commands_with_scope_chat_id_adds_scope_to_every_request():
    channel = TelegramChannel("123456:ABC-fake", "999")
    requests = channel.build_set_my_commands_requests(
        {"en": [("audit", "Owner activity log")], "th": [("audit", "ประวัติกิจกรรม")]},
        scope_chat_id="42",
    )
    assert len(requests) == 2
    for _, payload in requests:
        assert payload["scope"] == {"type": "chat", "chat_id": "42"}


async def test_set_my_commands_default_is_a_silent_noop_on_base_abc():
    channel = _BareChannel()
    result = await channel.set_my_commands({"en": [("undo", "Undo")]})
    assert result is None
    result_scoped = await channel.set_my_commands({"en": [("undo", "Undo")]}, scope_chat_id="42")
    assert result_scoped is None


async def test_set_my_commands_posts_scoped_payload():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("123456:ABC-fake", "999", client=client)

    await channel.set_my_commands({"en": [("audit", "Owner activity log")]}, scope_chat_id="42")

    assert captured[0]["scope"] == {"type": "chat", "chat_id": "42"}
    await channel.aclose()


# ---------------------------------------------------------------------------
# AC-4: inbound message_id plumbing (run -> on_message -> handle_inbound_message)
# ---------------------------------------------------------------------------


async def test_run_passes_the_inbound_message_id_to_on_message():
    responses = [
        {
            "ok": True,
            "result": [{"update_id": 1, "message": {"text": "500ml", "message_id": 777}}],
        },
    ]

    class StopPolling(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            raise StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    calls: list[tuple[str, str, str | None, str | None]] = []

    async def on_message(
        chat_id: str, text: str, display_name: str | None = None, message_id: str | None = None
    ) -> None:
        calls.append((chat_id, text, display_name, message_id))

    with pytest.raises(StopPolling):
        await channel.run(on_message)

    assert calls == [("", "500ml", None, "777")]
    await channel.aclose()


async def test_run_passes_none_when_the_update_has_no_message_id():
    responses = [
        {"ok": True, "result": [{"update_id": 1, "message": {"text": "500ml"}}]},
    ]

    class StopPolling(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            raise StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    calls: list[str | None] = []

    async def on_message(
        chat_id: str, text: str, display_name: str | None = None, message_id: str | None = None
    ) -> None:
        calls.append(message_id)

    with pytest.raises(StopPolling):
        await channel.run(on_message)

    assert calls == [None]
    await channel.aclose()


async def test_on_message_4_arg_default_fakes_still_work_unmodified():
    """R-S4/AC-4: `TelegramChannel.run` always supplies 4 positional args
    now, so a caller/fake keeps working unmodified only if its OWN
    signature defaults the new trailing params (`display_name=None`,
    `message_id=None`) -- exactly the shape every real caller in this
    codebase (`main.py:on_message`) and every updated test fake uses.
    (A fake that declares fewer params with no defaults would raise
    TypeError -- but `run`'s own per-update try/except around the
    on_message call, proven by test_run_on_message_exception_does_not_
    crash_the_loop in tests/test_channels.py, swallows that too, so the
    inbound loop is resilient to a caller bug either way; this test
    instead proves the SUPPORTED shape works end to end.)"""
    responses = [
        {"ok": True, "result": [{"update_id": 1, "message": {"text": "500ml", "message_id": 5}}]},
    ]

    class StopPolling(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        if not responses:
            raise StopPolling()
        return httpx.Response(200, json=responses.pop(0))

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    channel = TelegramChannel("token", "chat", client=client)

    calls: list[str] = []

    async def defaulted_on_message(chat_id: str, text: str, display_name=None, message_id=None) -> None:
        calls.append(text)

    with pytest.raises(StopPolling):
        await channel.run(defaulted_on_message)

    assert calls == ["500ml"]
    await channel.aclose()


async def test_handle_inbound_message_accepts_inbound_message_id_kwarg_inert(db, registry, monkeypatch):
    """AC-9 spirit: threading `inbound_message_id` through does not change
    the confirmation this shared-surface pass sends -- it's plumbing only,
    not yet read anywhere in handle_inbound_message's body."""
    config = Config()

    patch_parse_message(monkeypatch, ExtractionResult(category="water", value=500.0, confidence=0.9))
    channel_without = FakeChannel()
    await handle_inbound_message(
        "500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel_without,
        config=config,
        registry=registry,
        clock=lambda: datetime(2026, 8, 19, 9, 0, 0),
        user_id=OWNER,
    )

    channel_with = FakeChannel()
    await handle_inbound_message(
        "500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel_with,
        config=config,
        registry=registry,
        clock=lambda: datetime(2026, 8, 19, 9, 0, 0),
        user_id=OWNER,
        inbound_message_id="123456",
    )

    # Different totals between the two calls (both inserted a 500ml log
    # against the same db) are expected and irrelevant -- what matters is
    # that supplying inbound_message_id changes nothing about *how* the
    # confirmation is built. Assert on the confirmation SHAPE (prefix),
    # not exact totals, since this is call #2 against an already-seeded db.
    assert channel_without.sent[0].startswith("✅ 500 ml logged")
    assert channel_with.sent[0].startswith("✅ 500 ml logged")


# ---------------------------------------------------------------------------
# AC-5: config defaults ([notifications]/[quicklog]/[reactions]/[backfill]/
# [routines])
# ---------------------------------------------------------------------------


def test_config_defaults_match_spec_2_5():
    config = Config()
    assert config.notifications == NotificationsConfig(silent_proactive=True)
    assert config.quicklog == QuicklogConfig(enabled=True, max_buttons_per_habit=3)
    assert config.reactions == ReactionsConfig(enabled=True)
    assert config.backfill == BackfillConfig(max_days_back=14)
    assert config.routines == RoutinesConfig(max_per_user=20)


def test_config_toml_absent_sections_use_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[app]\ntimezone = "Asia/Bangkok"\n', encoding="utf-8")
    config = load_config(path)
    assert config.notifications.silent_proactive is True
    assert config.quicklog.enabled is True
    assert config.quicklog.max_buttons_per_habit == 3
    assert config.reactions.enabled is True
    assert config.backfill.max_days_back == 14
    assert config.routines.max_per_user == 20


def test_config_toml_sections_are_overridable(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[notifications]\nsilent_proactive = false\n"
        "[quicklog]\nenabled = false\nmax_buttons_per_habit = 5\n"
        "[reactions]\nenabled = false\n"
        "[backfill]\nmax_days_back = 7\n"
        "[routines]\nmax_per_user = 3\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.notifications.silent_proactive is False
    assert config.quicklog.enabled is False
    assert config.quicklog.max_buttons_per_habit == 5
    assert config.reactions.enabled is False
    assert config.backfill.max_days_back == 7
    assert config.routines.max_per_user == 3


def test_repo_config_toml_loads_cleanly_with_v18_sections():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config.notifications.silent_proactive is True
    assert config.quicklog.enabled is True
    assert config.reactions.enabled is True
    assert config.backfill.max_days_back == 14
    assert config.routines.max_per_user == 20


@pytest.mark.parametrize(
    "field,value",
    [("max_buttons_per_habit", 0), ("max_buttons_per_habit", -1)],
)
def test_quicklog_max_buttons_per_habit_must_be_positive(field, value):
    with pytest.raises(Exception):
        QuicklogConfig(**{field: value})


def test_backfill_max_days_back_must_be_positive():
    with pytest.raises(Exception):
        BackfillConfig(max_days_back=0)


def test_routines_max_per_user_must_be_positive():
    with pytest.raises(Exception):
        RoutinesConfig(max_per_user=0)


# ---------------------------------------------------------------------------
# AC-6: audit vocab (routine_create/routine_delete/routine_run)
# ---------------------------------------------------------------------------


def test_audit_actions_include_the_three_new_routine_actions():
    assert "routine_create" in audit.ACTIONS
    assert "routine_delete" in audit.ACTIONS
    assert "routine_run" in audit.ACTIONS


def test_audit_view_has_localized_labels_for_the_three_new_actions(db):
    config = Config()
    for action in ("routine_create", "routine_delete", "routine_run"):
        audit.record(db, actor=OWNER, action=action, source="command", entity="morning")
        en_reply = audit_view.render_recent(db, config, "en", limit=None, owner_chat_id=OWNER)
        th_reply = audit_view.render_recent(db, config, "th", limit=None, owner_chat_id=OWNER)
        en_line = en_reply.splitlines()[1]
        th_line = th_reply.splitlines()[1]
        assert "_" not in en_line.split(" · ")[2]
        assert action not in th_line


# ---------------------------------------------------------------------------
# AC-7: release notes
# ---------------------------------------------------------------------------


def test_release_notes_1_8_0_exists_in_both_languages():
    assert "1.8.0" in RELEASE_NOTES
    assert get_release_note("1.8.0", "en")
    assert get_release_note("1.8.0", "th")


def test_release_notes_1_8_0_mentions_every_shipped_feature():
    en = get_release_note("1.8.0", "en")
    assert "/log" in en
    assert "react" in en.lower()
    assert "/routine" in en
    assert "yesterday" in en.lower() or "backfill" in en.lower()
    assert "silent" in en.lower() or "notification" in en.lower()


# ---------------------------------------------------------------------------
# AC-8: reserved words (log/บันทึก/routine/กิจวัตร)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["log", "บันทึก", "routine", "กิจวัตร"])
def test_reserved_trigger_words_contains_the_four_v18_literals(word):
    assert word in commands.reserved_trigger_words()


@pytest.mark.parametrize("word", ["log", "routine", "กิจวัตร"])
def test_commandkind_reserved_words_do_not_yet_dispatch(word):
    # "บันทึก" excluded here -- module `quicklog` landed its own
    # `_match_log` Thai-alias matcher (SPEC-v1.8.md R-Q1), so it now
    # dispatches live (see `test_reserved_word_log_thai_alias_now_
    # dispatches_as_log` below, and tests/test_quicklog.py for the full
    # adversarial corpus). Bare "log" (no leading "/") stays reserved but
    # not-yet-dispatching -- §2.1 gives no bare-English alias, only the
    # slash form "/log". "routine"/"กิจวัตร" await module `routines`'s
    # own matcher.
    base_registry = HabitRegistry.from_config(Config())
    assert commands.dispatch(word, base_registry) is None


def test_reserved_word_log_thai_alias_now_dispatches_as_log():
    base_registry = HabitRegistry.from_config(Config())
    result = commands.dispatch("บันทึก", base_registry)
    assert result is not None and result.kind == "log"


@pytest.mark.parametrize("word", ["log", "routine"])
def test_habitdef_rejects_a_custom_habit_id_named_after_a_v18_reserved_word(word):
    base_registry = HabitRegistry.from_config(Config())
    user_registry = base_registry
    fields = {"id": word, "type": "boolean", "en": "whatever"}
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        fields, base_registry, user_registry, commands.reserved_trigger_words(), cap=20
    )
    assert row is None
    assert msg_id == "addhabit_reserved_word"
    assert kwargs == {"word": word}


@pytest.mark.parametrize("word", ["บันทึก", "กิจวัตร"])
def test_habitdef_rejects_a_custom_habit_thai_label_named_after_a_v18_reserved_word(word):
    base_registry = HabitRegistry.from_config(Config())
    user_registry = base_registry
    fields = {"id": "my_new_habit", "type": "boolean", "en": "whatever", "th": word}
    row, msg_id, kwargs = habitdef.validate_and_normalize(
        fields, base_registry, user_registry, commands.reserved_trigger_words(), cap=20
    )
    assert row is None
    assert msg_id == "addhabit_reserved_word"
    assert kwargs == {"word": word}


def test_v18_kinds_present_in_command_kind_literal():
    for kind in ("log", "routine"):
        cmd = commands.Command(kind=kind)
        assert cmd.kind == kind
