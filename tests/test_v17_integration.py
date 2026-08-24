"""SPEC-v1.7.md §11 integration step -- the final pass that wires the two
independently-shipped parallel tracks (`habitdef`, `sweep`) into `main.py`'s
real closures: `command.kind in ("addhabit", "delhabit")` routing inside
`handle_inbound_message`, `/addhabit`/`/delhabit` in the public
`set_my_commands` menu, and their own `/help` lines.

Both tracks' own test files (`tests/test_habitdef.py`, `tests/
test_v17_habitdef_gaps.py`, `tests/test_v17_isolation_sweep.py`) already
prove their owned ACs -- `habitdef`'s at the `commands.dispatch()` ->
`execute_addhabit`/`execute_delhabit` layer (never through a live inbound
message), `sweep`'s by inserting `user_habits` rows directly (never through
`/addhabit`/`/delhabit` at all). This file is different in kind: it drives
the REAL, wired `async_main`/`on_message` closures end-to-end (mirroring
`tests/test_v16_integration.py`'s own harness) -- `/addhabit` typed as a
genuine inbound Telegram message, all the way through the access gate,
`commands.dispatch`, the new routing branch, `RegistryProvider.invalidate`,
and back out through a REAL subsequent message from the SAME user, proving
AC-3's "no restart" claim isn't just true at the unit level but true of the
actual wired path a real user would hit.

Covers SPEC-v1.7.md §11's own named integration scenarios: a two-user
end-to-end (A creates "pages" and logs/dashboard/records/habits/help pick
it up; B sees zero trace); a habit named "help"/"เตือน" is rejected through
real dispatch; `/delhabit` archives a habit-with-history and hard-deletes an
empty one through real dispatch; a Thai-numeral log preparses through the
real wired path; the public menu registers `/addhabit`/`/delhabit` at real
startup.

Live-environment rule (unchanged from every other integration test file):
every DB here is a scratch `tmp_path` SQLite file. Nothing in this file ever
opens `data/habits.db`, and no real Telegram/Ollama call is made."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.storage.db import Database

OWNER = "1001"
MEMBER = "2002"


# ---------------------------------------------------------------------------
# Shared async_main harness -- mirrors tests/test_v16_integration.py's own
# "Section B" copy (this codebase's own convention: each integration-
# adjacent test file keeps its own copy rather than importing another test
# file's fixtures).
# ---------------------------------------------------------------------------


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    last_instance: "_FakeScheduler | None" = None

    def __init__(self, *args, **kwargs):
        self.jobs: dict[str, object] = {}
        _FakeScheduler.last_instance = self

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True, **kwargs):
        self.jobs[id] = SimpleNamespace(func=func, trigger=trigger, args=args, id=id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    responses: list[str] = []

    def __init__(self, *args, **kwargs):
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
    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list]] = []
        self.set_my_commands_calls: list[dict] = []
        self.pinned: dict[str, str] = {}
        self.edits: list[tuple[str, str, str]] = []
        self._next_msg_id = 7000
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str) -> None:
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

    async def set_my_commands(self, commands) -> None:
        self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            _, chat_id, text, display_name = step
            await on_message(chat_id, text, display_name)
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
    # SPEC-v1.5.md R-N2: neutralize the startup announce (mirrors every
    # other integration test file's own identical fix) so it never
    # pollutes this file's own sent-message assertions.
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


def _seed_users(tmp_path, *, member: bool = False) -> None:
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    if member:
        seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.close()


# ===========================================================================
# 1. /addhabit through real dispatch -> registered immediately -> a
#    SEPARATE, later message from the same user preparses instantly with NO
#    LLM call and no restart (AC-3, R-G3) -> dashboard and /records pick it
#    up -> /habits and /help mention it too.
# ===========================================================================


async def test_addhabit_end_to_end_no_restart_dashboard_records_habits_help_pick_it_up(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [
        ("message", OWNER, "/dashboard on", None),
        # Unit deliberately does NOT collide with any base habit's own unit
        # (water=ml, stretch=min) -- a non-colliding unit is required for
        # the instant zero-LLM preparse step below to actually fire
        # (R-V4/AC-H4 is a DIFFERENT, already-covered scenario).
        ("message", OWNER, "/addhabit id=pages|type=numeric|en=pages|unit=pages|goal=20", None),
        # A SEPARATE, later message -- this is the "no restart" claim: the
        # same long-lived process-global RegistryProvider built once in
        # async_main must have already rebuilt OWNER's registry from the
        # /addhabit two lines above, with nothing re-run in between.
        ("message", OWNER, "15 pages", None),
        ("message", OWNER, "/records", None),
        ("message", OWNER, "/habits", None),
        ("message", OWNER, "/help", None),
    ]
    # No canned LLM response provided at all -- if "15 pages" fell through
    # to the LLM path instead of preparsing, _FakeOllamaClient.chat_json
    # would return its "unknown" default and the reply would be the
    # clarifying question instead of a real confirmation (asserted below).
    channel = await _run(monkeypatch, config, script)

    sent = channel.sent_to(OWNER)

    addhabit_reply = next(t for t in sent if 'Added "pages"' in t)
    assert "goal 20/day" in addhabit_reply
    assert '"20 pages"' in addhabit_reply  # the worked example uses the NEW unit

    # The instant, zero-LLM preparse confirmation for "15 pages" -- template
    # is "✅ {value:g} {unit} logged — today {total:g} / {goal:g} {unit}
    # ({pct}%)" (core/i18n.py:confirm_numeric_goal), so a successful
    # zero-LLM preparse hit reads "15 pages logged"; a fallthrough to the
    # LLM path (which would return "unknown" from the unprimed fake client)
    # would instead have produced the clarifying question.
    log_reply = next(t for t in sent if "15 pages logged" in t)
    assert "clarifying" not in log_reply.lower()

    # dashboard.refresh, called from inside the SAME log-confirmation call
    # that just fired, edited the live pin to include the custom habit.
    assert OWNER in channel.pinned
    assert any("pages" in text for _cid, _mid, text in channel.edits)

    records_reply = next(t for t in sent if "🏆" in t)
    assert "pages" in records_reply

    habits_reply = next(t for t in sent if "📋" in t)
    assert "pages" in habits_reply

    help_reply = sent[-1]
    assert "/addhabit" in help_reply
    assert "/delhabit" in help_reply


async def test_member_sees_zero_trace_of_owners_custom_habit(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path, member=True)

    script = [
        ("message", OWNER, "/addhabit id=pages|type=numeric|en=pages|unit=pages|goal=20", None),
        ("message", MEMBER, "/habits", None),
        ("message", MEMBER, "/help", None),
    ]
    channel = await _run(monkeypatch, config, script)

    member_habits_reply = channel.sent_to(MEMBER)[0]
    assert "pages" not in member_habits_reply
    # /help's own addhabit/delhabit lines are unconditional app copy (every
    # user can always run both), so they're expected here regardless --
    # the isolation claim is about the CUSTOM HABIT'S OWN name, not the
    # commands that create one.
    member_help_reply = channel.sent_to(MEMBER)[-1]
    assert "/addhabit" in member_help_reply


# ===========================================================================
# 2. Label/id collision safety through real dispatch (R-V3/AC-H3) -- a
#    habit named "help"/"เตือน" is rejected, no write.
# ===========================================================================


async def test_addhabit_id_help_is_rejected_through_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [("message", OWNER, "/addhabit id=help|type=numeric|en=x|unit=u", None)]
    channel = await _run(monkeypatch, config, script)

    reply = channel.sent_to(OWNER)[0]
    assert reply.startswith("🤔")
    assert "help" in reply

    db = Database(tmp_path / "habits.db")
    try:
        assert db.count_active_user_habits(OWNER) == 0
    finally:
        db.close()


async def test_addhabit_thai_label_เตือน_is_rejected_through_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [("message", OWNER, "/addhabit id=customid|type=numeric|en=x|th=เตือน|unit=u", None)]
    channel = await _run(monkeypatch, config, script)

    reply = channel.sent_to(OWNER)[0]
    assert reply.startswith("🤔")

    db = Database(tmp_path / "habits.db")
    try:
        assert db.count_active_user_habits(OWNER) == 0
    finally:
        db.close()


# ===========================================================================
# 3. /delhabit smart-delete semantics through real dispatch (R-C2/AC-H5):
#    archives a habit-with-history, hard-deletes an empty one (id freed).
# ===========================================================================


async def test_delhabit_hard_deletes_an_empty_habit_and_frees_the_id_through_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [
        ("message", OWNER, "/addhabit id=temp|type=numeric|en=temp|unit=u", None),
        ("message", OWNER, "/delhabit temp", None),
        # id must be immediately reusable -- no restart needed (AC-3).
        ("message", OWNER, "/addhabit id=temp|type=numeric|en=temp again|unit=u", None),
    ]
    channel = await _run(monkeypatch, config, script)

    sent = channel.sent_to(OWNER)
    assert sent[1].startswith("🗑️")
    assert "Removed" in sent[1]
    assert sent[2].startswith("✅")  # the id was free to reuse
    assert "temp again" in sent[2]

    db = Database(tmp_path / "habits.db")
    try:
        assert db.count_active_user_habits(OWNER) == 1
    finally:
        db.close()


async def test_delhabit_archives_a_habit_with_history_and_reserves_its_id_through_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [
        ("message", OWNER, "/addhabit id=journal|type=numeric|en=journal|unit=entries", None),
        ("message", OWNER, "3 entries", None),  # gives it a `logs` row -> archive branch
        ("message", OWNER, "/delhabit journal", None),
        ("message", OWNER, "/addhabit id=journal|type=numeric|en=journal|unit=entries", None),
        ("message", OWNER, "/habits", None),
    ]
    channel = await _run(monkeypatch, config, script)

    sent = channel.sent_to(OWNER)
    delhabit_reply = next(t for t in sent if t.startswith("🗄️"))
    assert "Archived" in delhabit_reply

    # A re-add of the SAME id is rejected (id stays reserved, R-V1/OQ2) --
    # this is the reply that comes right after the failed re-add attempt.
    readd_index = sent.index(delhabit_reply) + 1
    assert sent[readd_index].startswith("🤔")
    assert "reserved" in sent[readd_index]

    habits_reply = sent[-1]
    assert "journal" not in habits_reply  # archived -- gone from the active listing

    db = Database(tmp_path / "habits.db")
    try:
        assert db.count_active_user_habits(OWNER) == 0  # archived, not active
        row = db.get_user_habit(OWNER, "journal")
        assert row is not None and row["archived_at"] is not None
    finally:
        db.close()


# ===========================================================================
# 4. Thai-numeral preparse still works through the real, v1.7-wired path
#    (AC-6's own normative lock, re-checked end-to-end after the routing
#    change, not just at the unit level).
# ===========================================================================


async def test_thai_numeral_log_preparses_with_no_llm_through_the_real_wired_path(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    script = [("message", OWNER, "๕๐๐ มล.", None)]
    channel = await _run(monkeypatch, config, script)

    reply = channel.sent_to(OWNER)[0]
    assert "500" in reply  # preparsed via Unicode-decimal-aware VALUE_RE, no LLM

    db = Database(tmp_path / "habits.db")
    try:
        rows = db.logs_between(OWNER, "2000-01-01T00:00:00", "2100-01-01T00:00:00")
        assert len(rows) == 1 and rows[0]["category"] == "water" and rows[0]["value_num"] == 500.0
    finally:
        db.close()


# ===========================================================================
# 5. Public menu registers /addhabit and /delhabit at real startup (R-A2).
# ===========================================================================


async def test_startup_menu_includes_addhabit_and_delhabit(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    for lang, entries in registered.items():
        names = {name for name, _desc in entries}
        assert {"addhabit", "delhabit"} <= names, f"{lang}: {sorted(names)}"


# ===========================================================================
# 6. AC-5 regression gate, re-checked through THIS pass's own real wiring:
#    an owner with zero user_habits rows gets the byte-identical v1.6
#    water confirmation even after the addhabit/delhabit routing branch
#    was added right next to the log-confirmation code path.
# ===========================================================================


async def test_ac5_owner_with_no_custom_habits_is_still_byte_identical_through_real_dispatch(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    _seed_users(tmp_path)

    channel = await _run(monkeypatch, config, script=[("message", OWNER, "500ml", None)])

    reply = channel.sent_to(OWNER)[0]
    assert reply == "✅ 500 ml logged — today 500 / 2500 ml (20%)"
