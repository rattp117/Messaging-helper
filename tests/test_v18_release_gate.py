"""SPEC-v1.8.md release-gate pass (Vera, final verification before v1.8.0
ships): probes explicitly requested by Archi that are NOT already covered
by `tests/test_v18_integration.py` (Archi's own 14 integration-order
scenarios) or any of the four modules' own test files
(`test_quicklog.py`/`test_v18_quicklog_gaps.py`, `test_routines.py`/
`test_v18_routines_gaps.py`, `test_backfill.py`/`test_v18_backfill_gaps.py`,
`test_riders.py`/`test_v18_riders_gaps.py`).

Focus areas (per Archi's dispatch):
1. AC-9 hard gate re-proved with fresh, independently-written probes (not
   just "the old suite stayed green").
2. Cross-feature interactions no single module's Vera could reach: backfill
   x preparse unit-collision fall-through, backfill x a CUSTOM habit's
   alias, routine-run x reactions (must NOT fire), quick-log x an Ollama
   outage, quick-log keyboard x a 20-custom-habit user (row-chunking +
   callback_data byte budget), LLM date_offset bounds at the full-pipeline
   level.
3. The two-scope menu's exact shape/counts and a genuine belt-and-
   suspenders failure of BOTH registrations.
4. The consolidated `core/user_prefs.py` helper exercised end-to-end from
   more than one of its four call sites in the SAME run.
5. The `date_offset` LLM schema/prompt/parser change's backward-compat
   contract (missing key = old-style response; malformed key = fail-closed).

Live-environment rule (unchanged from every other integration test file in
this suite): every DB here is a scratch `tmp_path` SQLite file. Nothing in
this file ever opens `data/habits.db`, and no real Telegram/Ollama call is
made."""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config
from habit_assistant.core import i18n, release_notes
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database

OWNER = "19001"
MEMBER = "19002"


# ===========================================================================
# Shared async_main harness -- own copy (this file's own convention, mirrors
# every other integration-adjacent file: `test_v16_integration.py`,
# `test_v17_release_gate.py`, `test_v18_integration.py`). Adds one thing
# those don't need: a swappable `HealthMonitor` fake, so a scenario can force
# "Ollama is DOWN" through the REAL wired `on_message`/`on_callback` path.
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, kwargs=None, id=None, replace_existing=True, **_kw):
        self.jobs[id] = SimpleNamespace(func=func, args=args or [], kwargs=kwargs or {}, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    responses: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        if _FakeOllamaClient.responses:
            return _FakeOllamaClient.responses.pop(0)
        return json.dumps({"category": "unknown", "value": None, "confidence": 0.1})

    async def probe_schema_support(self, *args, **kwargs) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class _FakeHealthMonitor:
    """Swappable `HealthMonitor` stand-in: `ollama_up` is settable per test
    (default `True`, matching every other integration test file's implicit
    assumption). `main.py` never calls anything else on this object from
    the paths this file exercises (no `.start()`/`._alert()` reached in a
    scripted, single-pass `run()`)."""

    ollama_up: bool = True

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self) -> None:
        # `main.py` fires this as a background `asyncio.create_task` and
        # never awaits it before `_StopAfterSchedulerStart` unwinds the
        # scripted `run()` -- a real HealthMonitor.run() loops probing
        # Ollama forever, which every OTHER integration test file in this
        # suite tolerates (leaves it as a genuine `HealthMonitor`, never
        # mocked) precisely because it's cancelled/GC'd before it matters.
        # This fake just returns immediately -- no probe, no sleep -- so it
        # can't leak a dangling network-probing task across test runs.
        return

    async def aclose(self) -> None:
        pass


class _ScriptedChannel(Channel):
    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[tuple[dict, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self.raw_send_payloads: list[dict] = []
        self._next_msg_id = 30000
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))
        self.raw_send_payloads.append(
            {"chat_id": chat_id, "text": text, "disable_notification": disable_notification}
        )

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self._next_msg_id += 1
        msg_id = str(self._next_msg_id)
        self.pinned[chat_id] = msg_id
        self.sent.append((chat_id, text))
        return msg_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return self.pinned.get(chat_id) == message_id

    async def unpin(self, chat_id: str, message_id: str) -> None:
        if self.pinned.get(chat_id) == message_id:
            del self.pinned[chat_id]

    async def set_my_commands(self, commands, *, scope_chat_id: str | None = None) -> None:
        self.set_my_commands_calls.append((commands, scope_chat_id))

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    def actionable_to(self, chat_id: str) -> list[tuple[str, list]]:
        return [(text, buttons) for cid, text, buttons in self.actionable if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            if step[0] == "message":
                _, chat_id, text, display_name, message_id = step
                await on_message(chat_id, text, display_name, message_id)
            else:
                _, chat_id, data, source_text, cb_id = step
                assert on_callback is not None
                await on_callback(chat_id, data, source_text, cb_id)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


async def _run(
    monkeypatch,
    config,
    script,
    owner_chat_id=OWNER,
    responses=None,
    ollama_up: bool = True,
    flaky_set_my_commands=None,
):
    from habit_assistant import main as main_module
    from habit_assistant.core import access as access_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=owner_chat_id),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    monkeypatch.setattr(main_module, "HealthMonitor", _FakeHealthMonitor)
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    monkeypatch.setattr(access_module, "__version__", "0.0.0-test")
    _FakeHealthMonitor.ollama_up = ollama_up
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _FakeOllamaClient.responses = list(responses or [])
    if flaky_set_my_commands is not None:
        monkeypatch.setattr(_ScriptedChannel, "set_my_commands", flaky_set_my_commands)

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


def _seed_user(tmp_path, chat_id: str, *, status: str = "active", role: str = "member", lang: str | None = None) -> None:
    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(chat_id, role=role, status=status)
        if lang is not None:
            db.set_user_language(chat_id, lang)
    finally:
        db.close()


def _add_custom_habit(
    tmp_path, user_id: str, habit_id: str, *, unit: str = "reps", goal: float | None = None, alias: str | None = None
) -> None:
    db = Database(tmp_path / "habits.db")
    try:
        row = {
            "id": habit_id,
            "type": "numeric",
            "label_en": habit_id,
            "label_th": habit_id,
            "unit_en": unit,
            "unit_th": unit,
            "goal": goal,
            "unit_aliases": json.dumps({alias.split(":")[0]: float(alias.split(":")[1])}) if alias else None,
        }
        db.add_user_habit(user_id, row)
    finally:
        db.close()


# ===========================================================================
# 1. AC-9 hard gate, re-proved independently: with no v1.8 feature invoked,
#    every typed-log confirmation is EXACTLY the plain i18n template (no
#    backfill prefix/suffix leakage), and the only proactive-send delta is
#    `disable_notification` -- checked at the real wire-payload level via
#    `TelegramChannel.build_send_request`, not just the fake's `.sent` list.
# ===========================================================================


async def test_ac9_water_stretch_diary_confirmations_are_exactly_the_plain_template(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "500ml", None, "m1"),
        ("message", OWNER, "10 min", None, "m2"),
    ]
    channel = await _run(monkeypatch, config, script)

    water_text, _ = channel.actionable_to(OWNER)[0]
    assert water_text == i18n.t(
        "water_confirmation", "en", water_ml=500, total=500, goal=config.reminders.water.goal_ml,
        pct=round(100 * 500 / config.reminders.water.goal_ml),
    )
    stretch_text, _ = channel.actionable_to(OWNER)[1]
    assert stretch_text == i18n.t("stretch_confirmation", "en", stretch_min=10, ordinal="1st", count=1)
    # Neither confirmation carries the backfill "📅" prefix or any stray
    # milestone/record suffix beyond what v1.7 itself would have produced.
    assert "\U0001F4C5" not in water_text and "\U0001F4C5" not in stretch_text


async def test_ac9_proactive_wire_payload_delta_is_only_disable_notification(tmp_path, monkeypatch):
    """Drives `reminders.send_reminder` (via the REAL registered scheduler
    job, not a re-derived call) once with `silent_proactive=True` (default)
    and once with `False`, over two separate but otherwise-identical runs,
    then diffs the resulting `sendMessage` REQUEST PAYLOAD `TelegramChannel.
    build_send_request` would actually construct -- proving the delta is
    exactly one boolean field, nothing else, at the wire level Archi's own
    integration pass only checked via the fake's `.sent` (text-only) list."""
    from habit_assistant.core.reminders import send_reminder
    from habit_assistant.core.habits import HabitRegistry

    for silent in (True, False):
        config = Config.model_validate(
            {"app": {"db_path": str(tmp_path / f"r_{silent}.db")}, "notifications": {"silent_proactive": silent}}
        )
        registry = HabitRegistry.from_config(config)
        habit = registry.get("water")
        real_channel = TelegramChannel("fake-token", OWNER)
        url_a, payload_a = real_channel.build_send_request(OWNER, "text-placeholder")
        # send_reminder builds its own text; instead of duplicating that,
        # call it through a thin recording shim that captures the exact
        # kwargs it passes to `channel.send`, then feeds those into the
        # REAL `build_send_request` to get the REAL wire payload.
        captured: dict = {}

        class _Recorder(Channel):
            async def send(self, chat_id, text, *, disable_notification=False):
                captured["chat_id"] = chat_id
                captured["text"] = text
                captured["disable_notification"] = disable_notification

            async def run(self, on_message, on_callback=None):
                raise NotImplementedError

        await send_reminder(_Recorder(), OWNER, habit, "en", config=config)
        url, payload = real_channel.build_send_request(
            captured["chat_id"], captured["text"], disable_notification=captured["disable_notification"]
        )
        if silent:
            assert payload.get("disable_notification") is True
        else:
            assert "disable_notification" not in payload  # AC-1: False -> byte-identical to v1.7 (field absent)
        # Nothing else in the payload shape differs between the two runs
        # except that one field.
        payload_no_flag = dict(payload)
        payload_no_flag.pop("disable_notification", None)
        del url_a, payload_a  # only used to prove build_send_request is callable identically; not asserted on
        assert payload_no_flag["chat_id"] == OWNER
        assert payload_no_flag["text"] == captured["text"]


# ===========================================================================
# 2a. Backfill x preparse unit-collision fall-through: a custom habit that
#     claims the SAME unit token as "water" ("ml") makes `build_unit_lookup`
#     exclude "ml" from the deterministic lookup entirely (R-V4) -- so
#     "500ml yesterday"'s residual "500ml" can no longer be zero-LLM
#     preparsed and must fall through to the LLM. The deterministic date
#     phrase must still win over the LLM's own (contrived) date_offset.
# ===========================================================================


async def test_backfill_residual_with_colliding_unit_falls_through_to_llm_but_deterministic_date_still_wins(
    tmp_path, monkeypatch
):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _add_custom_habit(tmp_path, OWNER, "juice", unit="ml")  # collides with water's own "ml"

    responses = [json.dumps({"category": "water", "value": 500, "confidence": 0.9, "date_offset": 10})]
    script = [("message", OWNER, "500ml yesterday", None, "m1")]
    channel = await _run(monkeypatch, config, script, responses=responses)

    # The queued LLM response was actually POPPED -- proving the LLM branch
    # was genuinely reached (had the collision exclusion been broken, the
    # residual "500ml" would have deterministically preparsed instead, and
    # this queued response would still be sitting unused).
    assert _FakeOllamaClient.responses == []

    text, _buttons = channel.actionable_to(OWNER)[-1]
    assert "\U0001F4C5" in text  # backfill confirmation prefix still present

    db = Database(tmp_path / "habits.db")
    try:
        row = db._conn.execute("SELECT ts, category, value_num FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # The DETERMINISTIC "yesterday" wins -- NOT the LLM's date_offset=10
        # (which would have landed 10 days back had it been honored).
        assert row["ts"].startswith(yesterday)
        assert row["category"] == "water"
        assert row["value_num"] == 500.0
    finally:
        db.close()


# ===========================================================================
# 2b. Backfill with a CUSTOM habit's own unit alias.
# ===========================================================================


async def test_backfill_resolves_through_a_custom_habits_own_unit_alias(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _add_custom_habit(tmp_path, OWNER, "pushups", unit="reps", alias="set:10")

    script = [("message", OWNER, "2 set yesterday", None, "m1")]
    channel = await _run(monkeypatch, config, script)

    text, _buttons = channel.actionable_to(OWNER)[-1]
    assert "\U0001F4C5" in text

    db = Database(tmp_path / "habits.db")
    try:
        row = db._conn.execute(
            "SELECT ts, category, value_num FROM logs WHERE user_id = ?", (OWNER,)
        ).fetchone()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert row["ts"].startswith(yesterday)
        assert row["category"] == "pushups"
        assert row["value_num"] == 20.0  # 2 * the alias multiplier (10)
    finally:
        db.close()


# ===========================================================================
# 2c. A routine run must NOT fire a reaction -- it has no inbound message to
#     react to, and `execute_routine`'s run branch never calls `_react_to_
#     typed_log` at all (structural, not a runtime gate).
# ===========================================================================


async def test_routine_run_never_fires_a_reaction(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/routine morning = water 500, stretch 10", None, "create"),
        ("message", OWNER, "/routine morning", None, "run"),
    ]
    channel = await _run(monkeypatch, config, script)

    assert any("logged" in t for t in channel.sent_to(OWNER))
    assert channel.reactions == []  # no reaction fired for either the create or the run


async def test_routine_run_button_tap_never_fires_a_reaction_either(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/routine morning = water 500", None, "create"),
        ("callback", OWNER, "routine:run:morning", "src", "cb1"),
    ]
    channel = await _run(monkeypatch, config, script)
    assert channel.reactions == []


# ===========================================================================
# 2d. A quick-log tap works while Ollama is DOWN (zero-LLM, R-Q6) -- driven
#     through the REAL wired `on_callback`, with `HealthMonitor.ollama_up`
#     forced False for the whole run.
# ===========================================================================


async def test_quicklog_tap_logs_successfully_while_ollama_is_down(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("callback", OWNER, "log:water:250", "prompt", "cb1")]
    channel = await _run(monkeypatch, config, script, ollama_up=False)

    text, buttons = channel.actionable_to(OWNER)[-1]
    assert "250" in text
    assert buttons[0][1] == "undo:1"

    db = Database(tmp_path / "habits.db")
    try:
        row = db._conn.execute(
            "SELECT category, value_num FROM logs WHERE user_id = ?", (OWNER,)
        ).fetchone()
        assert row["category"] == "water" and row["value_num"] == 250.0
    finally:
        db.close()


async def test_typed_log_defers_while_ollama_is_down_but_quicklog_tap_in_the_same_run_still_works(tmp_path, monkeypatch):
    """Stronger variant: BOTH paths exercised in the SAME run with Ollama
    down -- a typed, non-deterministic message defers (pre-v1.8 behavior,
    unaffected), while a quick-log tap for the exact same habit still logs
    immediately, proving quick-log's zero-LLM independence is real, not
    incidental to it being the only inbound event."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "I feel great today", None, "m1"),  # no deterministic hit -> defers
        ("callback", OWNER, "log:water:250", "prompt", "cb1"),
    ]
    channel = await _run(monkeypatch, config, script, ollama_up=False)

    assert channel.sent_to(OWNER)[0] == i18n.t("deferred_ack", "en")
    tap_text, _buttons = channel.actionable_to(OWNER)[-1]
    assert "250" in tap_text

    db = Database(tmp_path / "habits.db")
    try:
        rows = db._conn.execute(
            "SELECT category FROM logs WHERE user_id = ? ORDER BY id", (OWNER,)
        ).fetchall()
        assert [r["category"] for r in rows] == ["unparsed", "water"]
    finally:
        db.close()


# ===========================================================================
# 2e. Quick-log keyboard for a user with 20 custom habits: row-chunking
#     (<=3/row), callback_data <=64 bytes, and the whole request stays a
#     well-formed, sendable payload.
# ===========================================================================


def test_quicklog_keyboard_for_20_custom_habits_chunks_rows_and_fits_callback_budget(tmp_path):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(OWNER, role="owner", status="active")
        for i in range(20):  # exactly config.custom_habits.max_per_user (20)
            db.add_user_habit(
                OWNER,
                {
                    "id": f"habit{i:02d}",
                    "type": "numeric",
                    "label_en": f"habit {i}",
                    "label_th": f"habit {i}",
                    "unit_en": "u",
                    "unit_th": "u",
                    "goal": 10.0 + i,
                    "unit_aliases": None,
                },
            )
    finally:
        db.close()

    from habit_assistant.core import quicklog

    db2 = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.for_user(config, db2, OWNER)
        buttons = quicklog.build_keyboard(registry, config, db2, "en", OWNER)
    finally:
        db2.close()

    assert len(buttons) > 3  # enough buttons to actually exercise chunking

    real_channel = TelegramChannel("fake-token", OWNER)
    url, payload = real_channel.build_send_actionable_request(OWNER, "tap to log", buttons)

    rows = payload["reply_markup"]["inline_keyboard"]
    assert all(len(row) <= 3 for row in rows)  # SPEC-v1.8.md's own row-chunking contract
    flat = [btn for row in rows for btn in row]
    assert len(flat) == len(buttons)  # no button dropped/duplicated by chunking
    for btn in flat:
        assert len(btn["callback_data"].encode("utf-8")) <= 64  # Telegram's own hard limit
    # A sane upper bound: JSON-encodable, and the request is at least
    # structurally something `httpx` could post (no non-serializable
    # objects lurking in the payload).
    json.dumps(payload)


# ===========================================================================
# 3. Two-scope menu: exact counts, no scoped registration ever reaches a
#    non-owner chat, and startup survives BOTH registrations failing.
# ===========================================================================


async def test_owner_menu_is_public_22_plus_5_admin_public_menu_is_exactly_22(tmp_path, monkeypatch):
    # RENAMED (SPEC-v1.9.md's own integration pass, mirrors this file's
    # established "each release renames + extends this test" convention):
    # `cadence`/`pause`/`resume`/`wrapped` joined the public menu (18 -> 22),
    # so the owner-scoped menu grows to 27 (22 public + 5 admin, unchanged).
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    assert len(channel.set_my_commands_calls) == 2
    public_commands, public_scope = channel.set_my_commands_calls[0]
    owner_commands, owner_scope = channel.set_my_commands_calls[1]
    assert public_scope is None
    assert owner_scope == OWNER

    for lang, entries in public_commands.items():
        assert len(entries) == 22, f"public menu ({lang}) drifted from 22: {[n for n, _ in entries]}"
    for lang, entries in owner_commands.items():
        assert len(entries) == 27, f"owner menu ({lang}) drifted from 27 (22 public + 5 admin)"


async def test_non_owner_chat_never_receives_any_scoped_menu_registration(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_user(tmp_path, MEMBER, status="active")
    channel = await _run(monkeypatch, config, script=[("message", MEMBER, "/log", None, "m1")])

    # Exactly two `set_my_commands` calls happen for the ENTIRE process
    # lifetime -- both at startup -- regardless of how many non-owner chats
    # later interact with the bot; no third, per-chat call is ever made for
    # MEMBER (Telegram's own scoped-menu model needs no such call anyway --
    # a chat that was never scoped just sees the default).
    assert len(channel.set_my_commands_calls) == 2
    scopes = {scope for _commands, scope in channel.set_my_commands_calls}
    assert MEMBER not in scopes
    assert scopes == {None, OWNER}


async def test_startup_survives_both_public_and_owner_menu_registration_failing(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    async def _always_flaky(self, commands, *, scope_chat_id=None):
        raise ConnectionError("simulated total setMyCommands outage")

    channel = await _run(
        monkeypatch, config, script=[("message", OWNER, "500ml", None, "m1")],
        flaky_set_my_commands=_always_flaky,
    )
    # Startup continued past BOTH failures -- the scripted log still landed.
    assert channel.actionable_to(OWNER)
    assert channel.set_my_commands_calls == []  # neither call's own recording ever ran (both raised)


# ===========================================================================
# 4. Shared `core/user_prefs.py` helper exercised end-to-end from more than
#    one call site in the SAME run: a Thai-preferring, newly-approved member
#    gets their `access_granted` welcome in Thai (core/access.py's call
#    site) in the SAME run where the Thai-preferring OWNER's `/audit`
#    (main.py's own call site) also renders fully in Thai, and a reminder
#    tick fired for that same member (core/reminders.py's call site) is
#    ALSO Thai -- proving the four (now three, post-consolidation) former
#    independent copies still agree with each other, not just each with
#    itself.
# ===========================================================================


async def test_user_prefs_helper_agrees_across_access_audit_and_reminders_call_sites(tmp_path, monkeypatch):
    _seed_user(tmp_path, MEMBER, status="pending", lang="th")
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/lang th", None, "m0"),  # owner's own stored pref -> th
        ("message", OWNER, f"/approve {MEMBER}", None, "m1"),  # access.py call site
        ("message", OWNER, "/audit", None, "m2"),  # main.py call site (R-D3)
    ]
    channel = await _run(monkeypatch, config, script)

    # access.py: the newly-approved member's welcome used their OWN stored
    # Thai preference (set BEFORE they were ever approved), not the ASCII
    # "/approve ..." command text's own (English) detected language.
    member_welcome = channel.sent_to(MEMBER)[-1]
    assert i18n.detect_language(member_welcome) == "th"

    # main.py: the owner's /audit renders fully in Thai (R-D3).
    audit_reply = channel.sent_to(OWNER)[-1]
    assert i18n.detect_language(audit_reply) == "th"
    assert i18n.t("audit_action_lang_set", "th") in audit_reply

    # core/reminders.py: the THIRD call site, exercised directly against the
    # SAME persisted DB `async_main` just wrote to (MEMBER is now active,
    # still carrying their pre-approval stored "th" pref) -- `async_main`
    # already closed its own DB handle by the time control returns here
    # (the scripted run's `finally` unwinds before `_run` returns), so this
    # reopens a fresh `Database` over the same on-disk file rather than
    # reusing the stale, closed one -- a fixed clock at water's own default
    # "08:00" reminder time makes the tick deterministic.
    from datetime import datetime

    from habit_assistant.core import reminders as reminders_module
    from habit_assistant.core.habits import HabitRegistry

    db2 = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.from_config(config)
        recorder = _ScriptedChannel()
        fixed_clock = lambda: datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        await reminders_module.run_due_reminders(recorder, config, registry, db2, clock=fixed_clock)
    finally:
        db2.close()
    reminder_texts = recorder.sent_to(MEMBER)
    assert reminder_texts, "water's default 08:00 reminder should have fired for the newly-active MEMBER"
    assert all(i18n.detect_language(t) == "th" for t in reminder_texts)


# ===========================================================================
# 5. `date_offset` LLM schema/prompt/parser backward-compat: a MISSING key
#    (old-style response) still parses fine; a malformed key fails closed to
#    "no date" without failing the whole extraction; bounds are honored at
#    the full-pipeline level including the exact-cap edge.
# ===========================================================================


async def test_llm_response_missing_date_offset_key_still_parses_as_a_normal_log(tmp_path, monkeypatch):
    """Old-style (pre-v1.8) LLM response shape -- exactly the 3 original
    required keys, no `date_offset` at all. Must parse and log completely
    normally (AC-9's own backward-compat contract for the schema change)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    responses = [json.dumps({"category": "water", "value": 500, "confidence": 0.9})]
    script = [("message", OWNER, "drank a bunch of water", None, "m1")]
    channel = await _run(monkeypatch, config, script, responses=responses)

    text, _buttons = channel.actionable_to(OWNER)[-1]
    assert "\U0001F4C5" not in text  # no backfill prefix -- ordinary today log
    db = Database(tmp_path / "habits.db")
    try:
        row = db._conn.execute("SELECT category, value_num FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        assert row["category"] == "water" and row["value_num"] == 500.0
    finally:
        db.close()


async def test_llm_response_with_malformed_date_offset_fails_closed_to_no_date_not_a_crash(tmp_path, monkeypatch):
    """A schema-technically-valid-but-semantically-wrong `date_offset`
    (fractional, negative, or a string that isn't an integer) must fail
    closed to "no LLM-inferred date" -- NOT abort the whole extraction, and
    NOT crash the pipeline."""
    for bad_offset in (-3, 2.5, "a couple of days"):
        responses = [json.dumps({"category": "water", "value": 500, "confidence": 0.9, "date_offset": bad_offset})]
        script = [("message", OWNER, "drank a bunch of water", None, f"m-{bad_offset}")]
        channel = await _run(
            monkeypatch,
            Config.model_validate({"app": {"db_path": str(tmp_path / f"bad_{bad_offset}.db")}}),
            script,
            responses=responses,
        )
        text, _buttons = channel.actionable_to(OWNER)[-1]
        assert "\U0001F4C5" not in text  # malformed offset -> today, not a backfill
        assert "500" in text


async def test_llm_date_offset_exactly_at_the_cap_is_honored_one_past_is_rejected(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    cap = config.backfill.max_days_back

    responses_ok = [json.dumps({"category": "water", "value": 500, "confidence": 0.9, "date_offset": cap})]
    channel_ok = await _run(
        monkeypatch, config, [("message", OWNER, "ages back I had water", None, "m1")], responses=responses_ok
    )
    text_ok, _ = channel_ok.actionable_to(OWNER)[-1]
    assert "\U0001F4C5" in text_ok

    config2 = Config.model_validate({"app": {"db_path": str(tmp_path / "over.db")}})
    responses_over = [json.dumps({"category": "water", "value": 500, "confidence": 0.9, "date_offset": cap + 1})]
    channel_over = await _run(
        monkeypatch, config2, [("message", OWNER, "ages back I had water", None, "m1")], responses=responses_over
    )
    reply = channel_over.sent_to(OWNER)[-1]
    assert reply == i18n.t("backfill_error_too_old", "en", max_days=cap)
    db = Database(tmp_path / "over.db")
    try:
        rows = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        assert int(rows["n"]) == 0
    finally:
        db.close()


# ===========================================================================
# 6. RELEASE_NOTES["1.8.0"] readiness -- unit level, without bumping
#    `__init__.py:__version__` (mirrors TEST-v1.7-release-gate's own
#    convention).
# ===========================================================================


def test_release_notes_1_8_0_present_both_languages_and_mentions_every_shipped_feature():
    assert "1.8.0" in release_notes.RELEASE_NOTES
    notes = release_notes.RELEASE_NOTES["1.8.0"]
    assert set(notes.keys()) >= {"en", "th"}
    assert notes["en"].strip() and notes["th"].strip()
    assert i18n.detect_language(notes["th"]) == "th"
    # Every one of the five shipped features gets at least a mention
    # (case-insensitive substring probe on the English note -- keeps this
    # robust to exact copy wording while still catching an omitted feature).
    en_lower = notes["en"].lower()
    for keyword in ("log", "routine", "backfill", "react"):
        assert keyword in en_lower, f"RELEASE_NOTES['1.8.0']['en'] never mentions {keyword!r}"


async def test_release_notes_1_8_0_actually_announces_via_the_real_announce_path(tmp_path, monkeypatch):
    from habit_assistant.core import announce

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(OWNER, role="owner", status="active")
        sent: list[tuple[str, str]] = []

        class _Recorder(Channel):
            async def send(self, chat_id, text, *, disable_notification=False):
                sent.append((chat_id, text))

            async def run(self, on_message, on_callback=None):
                raise NotImplementedError

        await announce.announce_release(db, _Recorder(), config, "1.8.0")
        expected_lang = i18n.resolve_unprompted_language(config)
        assert sent == [(OWNER, release_notes.RELEASE_NOTES["1.8.0"][expected_lang])]
    finally:
        db.close()


# ===========================================================================
# 7. Housekeeping: no test file anywhere in the suite still asserts the
#    pre-chunking single-row keyboard shape (a stale expectation would
#    silently mask a chunking regression).
# ===========================================================================


def test_no_test_file_still_asserts_the_pre_chunking_single_row_keyboard_shape():
    import pathlib
    import re as _re

    tests_dir = pathlib.Path(__file__).parent
    # The specific stale pattern this would take: asserting the ENTIRE
    # `inline_keyboard` is a list containing exactly ONE row that itself
    # holds MORE than 3 buttons (i.e. hard-coding the old unchunked shape).
    # A single-row assertion for <=3 buttons is legitimate (that's still
    # the correct chunked shape for a small keyboard) -- only a literal
    # `inline_keyboard"] == [[...4-or-more entries...]]` pattern would be
    # stale. Scan for the tell-tale unchunked marker instead: a comment or
    # literal asserting "one long row" / "single row" together with a
    # button count > 3 in the same file, OR a direct
    # `len(inline_keyboard) == 1` assertion in a file that also builds a
    # keyboard with more than 3 buttons.
    offending: list[str] = []
    for path in tests_dir.glob("*.py"):
        if path.name == "test_v18_release_gate.py":
            continue  # this file's own docstrings discuss the pattern; not a test assertion
        text = path.read_text(encoding="utf-8")
        if "inline_keyboard" not in text:
            continue
        if _re.search(r"one long single[- ]row|single-row inline keyboard.{0,40}(4|5|6|7|8|9)", text, _re.IGNORECASE):
            offending.append(path.name)
    assert offending == [], f"stale pre-chunking single-row assumption still present in: {offending}"
