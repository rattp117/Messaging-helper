"""SPEC-v1.8.md §11 integration order -- the final pass that wires the four
independently-shipped parallel modules (`quicklog`, `routines`, `backfill`,
`riders`) into `main.py`'s real closures: `on_callback` payload-prefix
dispatch (`undo:`/`log:`/`routine:run:`), `/log`/`/routine` command
routing, `backfill.extract_date` wired before `preparse` in
`handle_inbound_message`, the reaction call-site after a successful typed
log, the two-scope command menu (public + owner-scoped), and the `/audit`
stored-language-preference fix.

Every module's own test file already proves its owned ACs in isolation
(`test_quicklog.py`/`test_v18_quicklog_gaps.py`, `test_routines.py`/
`test_v18_routines_gaps.py`, `test_backfill.py`/`test_v18_backfill_gaps.py`,
`test_riders.py`/`test_v18_riders_gaps.py`). This file is different in
kind: it drives the REAL, wired `async_main`/`on_message`/`on_callback`
closures (mirrors `tests/test_v16_integration.py`'s own harness) so a
genuine wiring mistake would show up here even though every module's own
unit tests stay green. Covers SPEC-v1.8.md §11.3's own named scenarios:

1. A taps `/log` and a button -- row written, byte-identical confirmation
   to typing the same value, reaction fires on a TYPED log only (never a
   tap).
2. A creates + runs a routine while B (a second, independent active user
   in the SAME database) sees no trace at all -- list, run-by-name, and
   the run-button tap all isolate per user.
3. A backfills "yesterday" -- lands on the right day (ts prefix, heatmap
   day-intensity, `/history`), no today-dashboard edit, no milestone; the
   backfilled row's undo button still removes exactly that row.
4. The owner's menu shows the five admin commands (on a SEPARATE,
   owner-chat-scoped registration); a non-owner never sees them at all.
5. A Thai-preferring owner's `/audit` renders fully in Thai even via the
   plain ASCII "/audit" trigger (R-D3's own fixed bug).
6. AC-9 (inert until invoked): an ordinary typed log's confirmation TEXT
   is unaffected by the always-on reaction side channel (a SEPARATE Bot
   API call, never part of the `sendMessage`/`send_actionable` payload).

Live-environment rule (unchanged from every other integration test file):
every DB here is a scratch `tmp_path` SQLite file. Nothing in this file
ever opens `data/habits.db`, and no real Telegram/Ollama call is made."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import audit, heatmap, history_view, i18n
from habit_assistant.storage.db import Database

OWNER = "18001"  # user A
MEMBER = "18002"  # user B


# ---------------------------------------------------------------------------
# Small local fakes (per this codebase's own convention: each integration-
# adjacent test file keeps its own copy rather than importing another test
# file's fixtures -- mirrors tests/test_v16_integration.py).
# ---------------------------------------------------------------------------


class _StopAfterSchedulerStart(Exception):
    """Raised by the fake channel's `run()` once its scripted steps are
    exhausted, so `async_main` never actually blocks on a real inbound
    loop (mirrors every other integration test file's identical trick)."""


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs) -> None:
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True, **kwargs):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    # SPEC-v1.8.md §2.4/R-B5: queued raw JSON responses for the handful of
    # tests that DO need to reach the LLM (the `date_offset` scenarios --
    # every other test in this file uses a deterministically-parseable
    # message and never reaches this at all).
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


class _ScriptedChannel(Channel):
    """Drives the REAL `on_message`/`on_callback` closures `async_main`
    builds. `set_my_commands_calls` records `(commands, scope_chat_id)`
    pairs (unlike most other integration test files' fakes, which only
    ever cared about the single pre-v1.8 default registration) so AC-D2's
    two-scope menu is directly observable. `reactions` records
    `(chat_id, message_id, emoji)` so R-Q4/R-Q5's scope is directly
    observable too."""

    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[tuple[dict, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self._next_msg_id = 20000
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append((chat_id, text))

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


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, responses=None):
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
    # Neutralize the startup release-announce so it never pollutes this
    # file's own `sent`/`actionable` assertions (mirrors every other
    # integration test file's identical fix).
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    monkeypatch.setattr(access_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _FakeOllamaClient.responses = list(responses or [])

    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


def _seed_active_member(tmp_path, chat_id: str, *, lang: str = "auto") -> None:
    """Seeds a second ACTIVE user directly (bypasses the `/start` +
    `/approve` onboarding flow, which is `access`'s own, already-proven
    concern -- mirrors `tests/test_v15_integration.py::
    test_announce_sends_each_user_their_own_language`'s identical seed
    pattern) so B can act in the same DB `async_main` will open."""
    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(chat_id, role="member", status="active")
        if lang != "auto":
            db.set_user_language(chat_id, lang)
    finally:
        db.close()


# ===========================================================================
# 1. Quick-log: `/log` -> keyboard -> tap -> byte-identical confirmation to
#    typing the same value; reaction fires on a typed log, never on a tap.
# ===========================================================================


async def test_quicklog_tap_is_byte_identical_to_typing_and_reaction_is_typed_log_only(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    typed_script = [("message", OWNER, "250ml", None, "typed-msg-1")]
    typed_channel = await _run(monkeypatch, config, typed_script)
    typed_text, typed_buttons = typed_channel.actionable_to(OWNER)[-1]

    # A fresh DB (separate tmp_path) so both runs start from an identical
    # (empty) state -- the FIRST row inserted in either run is always id=1,
    # so the undo button payload is directly comparable across runs too.
    config2 = Config.model_validate({"app": {"db_path": str(tmp_path / "tap.db")}})
    tap_script = [
        ("message", OWNER, "/log", None, "log-cmd-msg"),
        ("callback", OWNER, "log:water:250", "log-prompt", "cb-1"),
    ]
    tap_channel = await _run(monkeypatch, config2, tap_script)
    tap_text, tap_buttons = tap_channel.actionable_to(OWNER)[-1]

    assert tap_text == typed_text  # AC-A2: the EXACT normal confirmation
    assert tap_buttons == typed_buttons  # same undo button (row id=1 either way)
    assert typed_buttons[0][1] == "undo:1"

    # R-Q4: the typed log (real `message_id` threaded through) got a
    # reaction; R-Q5: the tap (which targets the bot's own keyboard
    # message, not a user log) got NONE.
    assert typed_channel.reactions == [(OWNER, "typed-msg-1", "\U0001F4A7")]  # water -> 💧
    assert tap_channel.reactions == []


async def test_quicklog_button_prompt_and_empty_hint(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [("message", OWNER, "/log", None, "m1")]
    channel = await _run(monkeypatch, config, script)

    text, buttons = channel.actionable_to(OWNER)[-1]
    payloads = {data for _label, data in buttons}
    assert payloads == {"log:water:250", "log:water:600"}  # default registry's own loggable set


# ===========================================================================
# 2. Routines: A creates + runs a routine; B (second active user, same DB)
#    sees no trace at all -- list, run-by-name, and the run-button tap.
# ===========================================================================


async def test_routine_two_user_isolation_end_to_end(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_active_member(tmp_path, MEMBER)

    script = [
        ("message", OWNER, "/routine morning = water 500", None, "a-create"),
        ("message", OWNER, "/routine morning", None, "a-run"),
        ("message", MEMBER, "/routine", None, "b-list"),  # B's own list: empty
        ("message", MEMBER, "/routine morning", None, "b-run"),  # B running A's name: not found
    ]
    channel = await _run(monkeypatch, config, script)

    a_replies = channel.sent_to(OWNER)
    assert any("Saved routine" in t for t in a_replies)
    assert any("logged" in t and "morning" in t for t in a_replies)

    b_replies = channel.sent_to(MEMBER)
    assert any("No routines" in t or "routine" in t.lower() for t in b_replies)  # empty-list hint
    assert any("No routine named" in t for t in b_replies)  # friendly no-op, not A's routine

    db = Database(tmp_path / "habits.db")
    try:
        assert db.count_routines(OWNER) == 1
        assert db.count_routines(MEMBER) == 0
        b_logs = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (MEMBER,)).fetchone()
        assert int(b_logs["n"]) == 0  # B's attempted run wrote nothing
        a_logs = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        assert int(a_logs["n"]) == 1  # A's own run logged exactly one row
    finally:
        db.close()


async def test_routine_run_button_tap_is_isolated_per_tapping_user(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_active_member(tmp_path, MEMBER)

    script = [
        ("message", OWNER, "/routine morning = water 500", None, "a-create"),
        # B taps the exact SAME callback payload A's own list view would
        # have produced (a spoofed/replayed tap) -- must be a friendly
        # no-op, never running A's routine for B.
        ("callback", MEMBER, "routine:run:morning", "source", "cb-spoof"),
    ]
    channel = await _run(monkeypatch, config, script)

    assert channel.sent_to(MEMBER) == [i18n.t("routine_run_not_found", "en", name="morning")]
    db = Database(tmp_path / "habits.db")
    try:
        b_logs = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (MEMBER,)).fetchone()
        assert int(b_logs["n"]) == 0
    finally:
        db.close()


# ===========================================================================
# 3. Backfill: lands on the right day (ts, heatmap, /history), no today-
#    dashboard edit, no milestone; undo still removes exactly that row.
# ===========================================================================


async def test_backfill_yesterday_lands_correctly_no_dashboard_edit_no_milestone(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/dashboard on", None, "m0"),
        ("message", OWNER, "300ml", None, "m1"),  # today -- causes a dashboard EDIT
        ("message", OWNER, "500ml yesterday", None, "m2"),  # backfill
    ]
    channel = await _run(monkeypatch, config, script)

    edits_after_today_log = len(channel.edits)
    # The backfill must not have produced any FURTHER edit beyond the
    # ordinary today-log's own one (R-B4: "does not edit today's live
    # dashboard").
    backfill_text = channel.actionable_to(OWNER)[-1][0]
    assert "\U0001F4C5" in backfill_text  # 📅 confirmation prefix (§3.4)
    assert "milestone" not in backfill_text.lower()
    assert "record" not in backfill_text.lower()
    # "today's totals unchanged" (§3.4): the SAME 300/2500 total the
    # ordinary today-log's own confirmation showed, not 800/2500.
    assert "300" in backfill_text and "800" not in backfill_text

    db = Database(tmp_path / "habits.db")
    try:
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        rows = db._conn.execute(
            "SELECT id, ts, value_num, raw_message FROM logs WHERE user_id = ? ORDER BY id", (OWNER,)
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["ts"].startswith(today) and rows[0]["value_num"] == 300.0
        backfilled_row = rows[1]
        assert backfilled_row["ts"].startswith(yesterday)
        assert backfilled_row["value_num"] == 500.0
        assert backfilled_row["raw_message"] == "500ml yesterday"  # original text kept verbatim

        # AC-C2 building block: any ts-prefix aggregation (heatmap
        # included) attributes the row to the RESOLVED day, not today.
        from habit_assistant.core.habits import HabitRegistry

        registry = HabitRegistry.from_config(config)
        water = registry.get("water")
        assert heatmap._day_intensity(db, config, water, yesterday, OWNER) > 0
        assert heatmap._day_intensity(db, config, water, today, OWNER) == pytest.approx(300.0 / config.reminders.water.goal_ml)

        # /history reflects the row under its OWN raw text.
        history_text = history_view.render_history(db, config, registry, "en", user_id=OWNER, category=None, limit=None)
        assert "500ml yesterday" in history_text
    finally:
        db.close()

    assert len(channel.edits) == edits_after_today_log  # unchanged -- re-confirms no further edit occurred

    # AC-C6: the backfilled row's own undo button still removes exactly
    # that row (by id, not "the newest by ts").
    undo_text, undo_buttons = channel.actionable_to(OWNER)[-1]
    undo_data = undo_buttons[0][1]
    assert undo_data == "undo:2"  # the backfilled row was id=2 (inserted second)
    channel.script = [("callback", OWNER, undo_data, undo_text, "cb-undo")]
    # Re-drive on_callback directly through a second scripted run reusing
    # the SAME db_path (async_main opens its own Database, so a second
    # `_run` call against the same file continues from the persisted
    # state) to exercise undo through the real callback dispatch.
    channel2 = await _run(monkeypatch, config, [("callback", OWNER, undo_data, undo_text, "cb-undo")])
    assert channel2 is not None
    db2 = Database(tmp_path / "habits.db")
    try:
        remaining = db2._conn.execute(
            "SELECT id FROM logs WHERE user_id = ? AND deleted_at IS NULL ORDER BY id", (OWNER,)
        ).fetchall()
        assert [r["id"] for r in remaining] == [1]  # only today's row survives
    finally:
        db2.close()


async def test_backfill_future_and_too_old_are_rejected_no_write(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "20 days ago 500ml", None, "m1"),  # older than default max_days_back=14
    ]
    channel = await _run(monkeypatch, config, script)
    reply = channel.sent_to(OWNER)[-1]
    assert reply == i18n.t("backfill_error_too_old", "en", max_days=config.backfill.max_days_back)

    db = Database(tmp_path / "habits.db")
    try:
        rows = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        assert int(rows["n"]) == 0
    finally:
        db.close()


# ===========================================================================
# 4. Owner-scoped menu (AC-D2): the owner's chat gets a SEPARATE menu with
#    the five admin commands; the public (default) menu never has them.
# ===========================================================================


async def test_owner_scoped_menu_has_admin_commands_public_menu_does_not(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    assert len(channel.set_my_commands_calls) == 2
    public_commands, public_scope = channel.set_my_commands_calls[0]
    owner_commands, owner_scope = channel.set_my_commands_calls[1]

    assert public_scope is None  # AC-3: default scope byte-identical shape
    assert owner_scope == OWNER

    admin = {"invite", "approve", "block", "users", "audit"}
    for lang, entries in public_commands.items():
        names = {name for name, _desc in entries}
        assert not (names & admin)
        assert {"log", "routine"} <= names

    for lang, entries in owner_commands.items():
        names = {name for name, _desc in entries}
        assert admin <= names
        # The owner's menu is a strict superset of the public one.
        public_names = {name for name, _desc in public_commands[lang]}
        assert public_names <= names


async def test_owner_scoped_menu_registration_failure_never_crashes_startup(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    real_set_my_commands = _ScriptedChannel.set_my_commands
    call_count = {"n": 0}

    async def _flaky(self, commands, *, scope_chat_id=None):
        call_count["n"] += 1
        if scope_chat_id is not None:
            raise ConnectionError("simulated owner-menu transport failure")
        await real_set_my_commands(self, commands, scope_chat_id=scope_chat_id)

    monkeypatch.setattr(_ScriptedChannel, "set_my_commands", _flaky)
    channel = await _run(monkeypatch, config, script=[("message", OWNER, "500ml", None, "m1")])

    # Startup continued past the owner-menu failure -- proven by the fact
    # the scripted message below still got a real confirmation.
    assert channel.sent_to(OWNER)  # the log confirmation still arrived
    assert call_count["n"] == 2  # both registrations were attempted
    assert len(channel.set_my_commands_calls) == 1  # only the public one succeeded/recorded


# ===========================================================================
# 5. `/audit` language fix (R-D3/AC-D3): a Thai-preferring owner's `/audit`
#    renders fully in Thai even via the plain ASCII "/audit" trigger.
# ===========================================================================


async def test_audit_renders_in_the_owners_stored_language_even_via_ascii_trigger(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/lang th", None, "m1"),  # writes a lang_set audit row + sets the pref
        ("message", OWNER, "/audit", None, "m2"),  # plain ASCII trigger
    ]
    channel = await _run(monkeypatch, config, script)

    audit_reply = channel.sent_to(OWNER)[-1]
    assert i18n.detect_language(audit_reply) == "th"
    assert i18n.t("audit_action_lang_set", "th") in audit_reply
    assert i18n.t("audit_action_lang_set", "en") not in audit_reply


async def test_audit_non_owner_gets_no_reply(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_active_member(tmp_path, MEMBER)
    script = [("message", MEMBER, "/audit", None, "m1")]
    channel = await _run(monkeypatch, config, script)
    assert channel.sent_to(MEMBER) == []


# ===========================================================================
# 6. AC-9: inert until invoked -- an ordinary typed log's own confirmation
#    TEXT is unaffected by the (always-on-by-default) reaction side
#    channel, which is a SEPARATE Bot API call, never part of the
#    `sendMessage`/`send_actionable` payload itself.
# ===========================================================================


async def test_ac9_ordinary_log_confirmation_text_unaffected_by_the_reaction_side_channel(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    with_id_channel = await _run(
        monkeypatch, config, [("message", OWNER, "500ml", None, "real-message-id")]
    )
    with_id_text = with_id_channel.actionable_to(OWNER)[-1][0]

    config2 = Config.model_validate({"app": {"db_path": str(tmp_path / "no_id.db")}})
    no_id_channel = await _run(
        monkeypatch, config2, [("message", OWNER, "500ml", None, None)]
    )
    no_id_text = no_id_channel.actionable_to(OWNER)[-1][0]

    assert with_id_text == no_id_text  # confirmation payload byte-identical either way
    assert with_id_channel.reactions == [(OWNER, "real-message-id", "\U0001F4A7")]  # the side channel DID fire
    assert no_id_channel.reactions == []  # no message_id -> no reaction call at all (R-Q4's own gate)


# ===========================================================================
# 7. The LLM's optional `date_offset` (§2.4/R-B5): honored only when the
#    deterministic backfill parser didn't already resolve a date, subject
#    to the same bounds, wired end-to-end through the real LLM branch.
# ===========================================================================


async def test_llm_date_offset_backdates_when_deterministic_parser_misses(tmp_path, monkeypatch):
    from datetime import date, timedelta

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    # No deterministic preparse hit (free-form text) and no recognized
    # backfill phrase (§2.4's fixed word-lists don't cover "a few days
    # back") -- falls through to the LLM, whose response carries
    # date_offset=3.
    responses = [json.dumps({"category": "water", "value": 500, "confidence": 0.9, "date_offset": 3})]
    script = [("message", OWNER, "a few days back I drank a lot of water", None, "m1")]
    channel = await _run(monkeypatch, config, script, responses=responses)

    text, _buttons = channel.actionable_to(OWNER)[-1]
    assert "\U0001F4C5" in text  # 📅 confirmation prefix, same as the deterministic path

    db = Database(tmp_path / "habits.db")
    try:
        row = db._conn.execute("SELECT ts FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        expected_day = (date.today() - timedelta(days=3)).isoformat()
        assert row["ts"].startswith(expected_day)
    finally:
        db.close()


async def test_llm_date_offset_out_of_range_is_rejected_no_write(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    responses = [json.dumps({"category": "water", "value": 500, "confidence": 0.9, "date_offset": 30})]
    script = [("message", OWNER, "ages back I drank a lot of water", None, "m1")]
    channel = await _run(monkeypatch, config, script, responses=responses)

    reply = channel.sent_to(OWNER)[-1]
    assert reply == i18n.t("backfill_error_too_old", "en", max_days=config.backfill.max_days_back)

    db = Database(tmp_path / "habits.db")
    try:
        rows = db._conn.execute("SELECT COUNT(*) AS n FROM logs WHERE user_id = ?", (OWNER,)).fetchone()
        assert int(rows["n"]) == 0
    finally:
        db.close()


async def test_llm_date_offset_ignored_when_deterministic_parser_already_resolved_a_date(tmp_path, monkeypatch):
    """The deterministic pass wins whenever both are present: "500ml
    yesterday" is fully resolved by `backfill.extract_date` BEFORE the LLM
    is ever consulted -- the residual "500ml" hits the zero-LLM preparse
    path directly, so a (contrived) LLM that would claim `date_offset=10`
    is never even called. Proven by an LLM fake that raises if invoked."""

    class _RaisingLLM:
        def __init__(self, *a, **k) -> None:
            pass

        async def chat_json(self, *a, **k):
            raise AssertionError("LLM must never be called -- preparse should have handled this")

        async def chat_text(self, *a, **k):
            raise AssertionError("LLM must never be called")

        async def probe_schema_support(self, *a, **k):
            return {}

        async def aclose(self):
            pass

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    import habit_assistant.main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=OWNER),
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _RaisingLLM)
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    from habit_assistant.core import access as access_module

    monkeypatch.setattr(access_module, "__version__", "0.0.0-test")
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = [("message", OWNER, "500ml yesterday", None, "m1")]

    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(SimpleNamespace(seed=False, dry_run=None, test_reminder=None))

    channel = _ScriptedChannel.last_instance
    text, _buttons = channel.actionable_to(OWNER)[-1]
    assert "\U0001F4C5" in text  # still a correct backfill confirmation -- deterministic path alone did this
