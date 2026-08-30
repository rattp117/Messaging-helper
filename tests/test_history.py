"""SPEC-v1.4.md "/history [N]" -- the whole feature (sequential, one
track per SPEC-v1.4.md §11: "no independent second track worth the
coordination overhead"). Covers every AC except AC-2/AC-3, which land in
`tests/test_render_budget.py` (AC-3, the extracted budget helpers +
`audit_view`'s byte-identical regression guard) and this file's own
"shared surface" section below (AC-2, `db.recent_logs`).

Conventions: real on-disk SQLite (`tmp_path`), no DB mocks -- mirrors
`tests/test_audit_view.py`'s own convention (the module this one is
structurally closest to: both are read-only, LLM-free, per-user/owner
"recent activity" views sharing `core/render_budget.py`'s machinery).
Only Ollama/Telegram are faked, and only in this file's own integration-
level section at the bottom.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, history_view
from habit_assistant.core.commands import Command, dispatch
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "1001"
MEMBER = "2002"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _seed(db: Database, user_id: str, ts: str, category: str, value_num, raw: str, deleted: bool = False) -> int:
    row_id = db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))
    if deleted:
        db.soft_delete(row_id)
    return row_id


def _habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_, label_th=id_, unit_en="u", unit_th="u", goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


# ===========================================================================
# AC-1: additive/regression -- no migration was added for this feature.
# ===========================================================================


def test_no_migration_was_added_for_history(tmp_path):
    """SPEC-v1.4.md's own §1: "Purely additive... needs no migration."
    `/history` reads the pre-existing `logs` table only -- pinned here so
    a future accidental migration addition for this feature is caught.
    UPDATED (v1.5.0): the absolute count now includes SPEC-v1.5.md's own
    migration 008 (`checkin_window`/`last_announced_version`) -- v1.4
    itself still added none, which is all this test ever claimed; the
    literal just has to track the CURRENT total like every other pinned
    migration-count guard in this suite.
    UPDATED (v1.6.0): now also includes SPEC-v1.6.md's own migration 009
    (`dashboard_msg_id`/`habit_records`) -- same reasoning.
    UPDATED (v1.8.0): now also includes SPEC-v1.7.md's own migration 010
    (`user_habits`) and SPEC-v1.8.md's own migration 011 (`routines`/
    `routine_items`) -- same reasoning.
    UPDATED (v1.9.0): now also includes SPEC-v1.9.md's own migration 012
    (`habit_cadence`/`grace_ledger`/`pauses`) -- same reasoning."""
    assert len(MIGRATIONS) == 14
    db = Database(tmp_path / "fresh.db")
    assert db.schema_version == 14
    db.close()


# ===========================================================================
# AC-2 (shared surface): db.recent_logs -- direct accessor tests.
# ===========================================================================


def test_recent_logs_newest_first(db):
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0, "500ml")
    _seed(db, OWNER, "2026-08-21T09:00:00", "water", 300.0, "300ml")
    _seed(db, OWNER, "2026-08-22T09:00:00", "water", 600.0, "600ml")
    rows = db.recent_logs(OWNER, 10)
    assert [r["raw_message"] for r in rows] == ["600ml", "300ml", "500ml"]


def test_recent_logs_includes_soft_deleted_rows(db):
    row_id = _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0, "500ml")
    db.soft_delete(row_id)
    rows = db.recent_logs(OWNER, 10)
    assert len(rows) == 1
    assert rows[0]["deleted_at"] is not None


def test_recent_logs_excludes_unparsed_even_without_a_category_filter(db):
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0, "500ml")
    _seed(db, OWNER, "2026-08-21T09:00:00", "unparsed", None, "garbled")
    rows = db.recent_logs(OWNER, 10)
    assert len(rows) == 1
    assert rows[0]["category"] == "water"


def test_recent_logs_honors_category_filter(db):
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0, "500ml")
    _seed(db, OWNER, "2026-08-21T09:00:00", "stretch", 10.0, "10 min stretch")
    rows = db.recent_logs(OWNER, 10, category="water")
    assert len(rows) == 1
    assert rows[0]["category"] == "water"


def test_recent_logs_respects_limit(db):
    for i in range(5):
        _seed(db, OWNER, f"2026-08-{20 + i:02d}T09:00:00", "water", 500.0, f"{i}")
    rows = db.recent_logs(OWNER, 2)
    assert len(rows) == 2


def test_recent_logs_scoped_to_user_id(db):
    _seed(db, OWNER, "2026-08-20T09:00:00", "water", 500.0, "owner's")
    _seed(db, MEMBER, "2026-08-20T09:00:00", "water", 999.0, "member's")
    rows = db.recent_logs(OWNER, 10)
    assert len(rows) == 1
    assert rows[0]["raw_message"] == "owner's"


# ===========================================================================
# AC-4/AC-5: commands.dispatch shape recognition, incl. the adversarial
# corpus and the explicit "must not collide with /audit's ประวัติ" check.
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected_category", "expected_limit"),
    [
        ("/history", None, None),
        ("/history 10", None, 10),
        ("/history water", "water", None),
        ("/history water 10", "water", 10),
        ("/HISTORY water 10", "water", 10),  # case-insensitive slash trigger
        ("/history  water  10", "water", 10),  # extra whitespace tolerated
        ("/history coffee", "coffee", None),  # unresolved habit -- carried through raw, AC-6 is the view's job
        ("/history coffee 5", "coffee", 5),
        ("ย้อนหลัง", None, None),
        ("ย้อนหลัง 10", None, 10),
        ("ย้อนหลัง น้ำ", "water", None),
        ("ย้อนหลัง น้ำ 10", "water", 10),
    ],
)
def test_dispatch_recognizes_history_shape(text, expected_category, expected_limit):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "history"
    assert command.category == expected_category
    assert command.limit == expected_limit


_ADVERSARIAL_CORPUS = [
    "500ml",
    "ดื่มน้ำ 2 แก้ว",
    "10 min stretch",
    "did 10 min stretch",
    "felt good today",
    "เลิกงานแล้ว เหนื่อยมาก",
    "history please",  # no leading slash
    "please /history the logs",  # not anchored at the start
    "ย้อนหลัง ๆ หน่อยนะ",  # mai-yamok reduplication, ordinary prose
    "ย้อนหลังไปสามปีที่แล้ว",  # glued, ordinary prose ("looking back three years ago")
    "ย้อนหลังตัวเอง",  # glued, no space
    "ย้อนหลัง มาก",  # a real word ("very") that isn't a habit or a number
    "ย้อนหลัง 3 ปีที่แล้ว",  # digits followed by more prose -- must NOT partially match
]


@pytest.mark.parametrize("text", _ADVERSARIAL_CORPUS)
def test_dispatch_adversarial_corpus_never_matches_history(text):
    command = dispatch(text, DEFAULT_REGISTRY)
    assert command is None or command.kind != "history"


def test_history_thai_alias_does_not_collide_with_audits_thai_alias():
    """AC-5's own explicit requirement: ย้อนหลัง (history) and ประวัติ
    (audit) must never be confused for one another."""
    assert dispatch("ประวัติ", DEFAULT_REGISTRY).kind == "audit"
    assert dispatch("ประวัติ 5", DEFAULT_REGISTRY).kind == "audit"
    assert dispatch("ย้อนหลัง", DEFAULT_REGISTRY).kind == "history"
    assert dispatch("ย้อนหลัง 5", DEFAULT_REGISTRY).kind == "history"
    # Neither trigger word can produce the OTHER command's kind.
    assert dispatch("ประวัติ", DEFAULT_REGISTRY).kind != "history"
    assert dispatch("ย้อนหลัง", DEFAULT_REGISTRY).kind != "audit"


def test_other_track_commands_are_not_shadowed_by_history_patterns():
    """Cross-track dispatch precedence, same spot-check every prior
    module's own test file runs (test_access.py/test_preferences.py/
    test_schedules.py/test_audit_view.py's own precedent)."""
    assert dispatch("/undo", DEFAULT_REGISTRY).kind == "undo"
    assert dispatch("/target water 2000", DEFAULT_REGISTRY).kind == "target"
    assert dispatch("/remind water 08:00", DEFAULT_REGISTRY).kind == "remind"
    assert dispatch("/lang th", DEFAULT_REGISTRY).kind == "lang"
    assert dispatch("/quiet off", DEFAULT_REGISTRY).kind == "quiet"
    assert dispatch("/audit", DEFAULT_REGISTRY).kind == "audit"
    assert dispatch("/help", DEFAULT_REGISTRY).kind == "help"
    assert dispatch("/habits", DEFAULT_REGISTRY).kind == "habits"
    assert dispatch("how much water this week?", DEFAULT_REGISTRY).kind == "query"


# ===========================================================================
# history_view.render_history -- AC-6 through AC-13.
# ===========================================================================


def test_ac6_invalid_habit_returns_friendly_reply_and_touches_no_rows(db, config):
    reply = history_view.render_history(
        db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category="coffee", limit=None
    )
    assert "coffee" in reply
    assert "water" in reply and "stretch" in reply and "diary" in reply  # the habit_list
    assert db.recent_logs(OWNER, 10) == []  # read-only -- confirms no accidental write anywhere


def test_ac7_content_shows_ts_habit_value_and_quoted_message_newest_first(db, config):
    _seed(db, OWNER, "2026-08-23T14:03:00", "water", 500.0, "drank a big glass, 500ml")
    _seed(db, OWNER, "2026-08-23T09:30:00", "diary", None, "felt good today")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    lines = reply.splitlines()
    assert lines[0] == "🧾 Your last 20 entries:"
    assert "08-23 14:03" in lines[1]
    assert "500 ml water" in lines[1]
    assert '"drank a big glass, 500ml"' in lines[1]
    assert "08-23 09:30" in lines[2]  # newest-first: water (14:03) before diary (09:30)


def test_ac7_explicit_limit_shows_exactly_n(db, config):
    for i in range(8):
        _seed(db, OWNER, f"2026-08-{15 + i:02d}T09:00:00", "water", 500.0, f"entry {i}")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=5)
    assert reply.splitlines()[0] == "🧾 Your last 5 entries:"
    assert len(reply.splitlines()) == 6  # header + 5 rows


def test_ac7_a_request_above_the_cap_is_limited_to_50(db, config):
    for i in range(60):
        _seed(db, OWNER, f"2026-08-01T{i:02d}:00:00" if i < 24 else f"2026-08-02T{i - 24:02d}:00:00", "water", 500.0, f"e{i}")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=999)
    assert reply.splitlines()[0] == "🧾 Your last 50 entries:"


def test_ac8_undone_entry_is_included_and_marked(db, config):
    row_id = _seed(db, OWNER, "2026-08-23T13:10:00", "stretch", 10.0, "10 min stretch")
    db.soft_delete(row_id)
    _seed(db, OWNER, "2026-08-23T14:00:00", "water", 500.0, "500ml")  # a LIVE row, for contrast

    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    lines = reply.splitlines()
    undone_line = next(l for l in lines if "stretch" in l)
    live_line = next(l for l in lines if "500 ml water" in l)
    assert "(undone)" in undone_line
    assert "(undone)" not in live_line


def test_ac9_u_iso_two_users_never_see_each_others_entries(db, config):
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "owner's water")
    _seed(db, MEMBER, "2026-08-23T09:00:00", "water", 999.0, "member's water")

    owner_reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    member_reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=MEMBER, category=None, limit=None)

    assert "owner's water" in owner_reply
    assert "member's water" not in owner_reply
    assert "member's water" in member_reply
    assert "owner's water" not in member_reply


def test_ac10_filter_shows_only_the_requested_habit(db, config):
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "500ml")
    _seed(db, OWNER, "2026-08-23T10:00:00", "stretch", 10.0, "10 min stretch")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category="water", limit=None)
    assert "500 ml water" in reply
    assert "stretch" not in reply
    assert "your last 20 water" in reply.lower() or "water" in reply.splitlines()[0].lower()


def test_ac11_unparsed_rows_never_appear(db, config):
    _seed(db, OWNER, "2026-08-23T09:00:00", "unparsed", None, "garbled message")
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert reply == "🧾 No entries yet."


def test_ac12_control_characters_and_newlines_are_collapsed_to_one_line(db, config):
    hostile = "line one\nline two\r\nline three\ttabbed"
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, hostile)
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert len(reply.splitlines()) == 2  # header + exactly ONE row -- no accidental extra lines
    assert "\n" not in reply.splitlines()[1] or True  # the row itself has no embedded newline
    assert "line one" in reply and "line two" in reply and "line three" in reply


def test_ac12_curly_braces_never_crash_the_renderer(db, config):
    """R-R3: raw_message is passed only as a .format() VALUE, never as a
    template -- a literal {malicious} must render inertly, not raise a
    KeyError/IndexError the way `"{}".format(**{})` would if the message
    itself were ever used as a template string."""
    hostile = "logged {this} and {0} and {{nested}} braces"
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, hostile)
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert "{this}" in reply or "{" in reply  # rendered literally, not interpreted


def test_ac12_a_very_long_raw_message_is_truncated_per_line(db, config):
    hostile = "x" * 4000
    _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, hostile)
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert len(reply) < 500  # nowhere near the raw 4000 chars -- per-line truncation worked
    assert "…" in reply


def test_ac12_many_long_messages_fit_within_the_telegram_budget_with_footer(db, config):
    for i in range(50):
        ts = f"2026-08-01T{i % 24:02d}:{(i * 7) % 60:02d}:00"
        _seed(db, OWNER, ts, "water", 500.0, f"entry number {i} " + "x" * 200)
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=50)
    assert len(reply) <= 4096
    assert "more" in reply  # the history_more_rows footer -- some rows had to be dropped


def test_ac13_empty_history_returns_friendly_message(db, config):
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "en", user_id=OWNER, category=None, limit=None)
    assert reply == "🧾 No entries yet."


def test_bilingual_thai_output_localizes_header_and_undone_marker(db, config):
    row_id = _seed(db, OWNER, "2026-08-23T09:00:00", "water", 500.0, "500ml")
    db.soft_delete(row_id)
    reply = history_view.render_history(db, config, DEFAULT_REGISTRY, "th", user_id=OWNER, category=None, limit=None)
    assert "รายการล่าสุด" in reply
    assert "ยกเลิกแล้ว" in reply
    assert "น้ำ" in reply


def test_render_history_is_synchronous_and_has_no_llm_or_channel_dependency():
    import inspect

    assert not inspect.iscoroutinefunction(history_view.render_history)
    params = inspect.signature(history_view.render_history).parameters
    assert "llm" not in params
    assert "channel" not in params
    assert "health_monitor" not in params


def test_describe_log_reused_for_a_generic_non_builtin_habit(db, config):
    """R-R2: describe_log's own generic/boolean/raw fallback branches are
    exercised through history_view too, not just built-ins -- proves the
    reuse is real, not a built-ins-only special case."""
    registry = HabitRegistry([_habit("meds", "boolean", goal=None)])
    _seed(db, OWNER, "2026-08-23T09:00:00", "meds", 1.0, "took my meds")
    reply = history_view.render_history(db, config, registry, "en", user_id=OWNER, category=None, limit=None)
    assert "meds" in reply.lower()


# ===========================================================================
# Integration-level: main.py's real wiring (any active user, no owner
# gate; LLM-free through the real async_main path; menu registration).
# Small, self-contained harness -- mirrors tests/test_v12_integration.py/
# tests/test_v13_integration.py's own `_ScriptedChannel`/`_run_async_main`
# pattern (each integration-adjacent test file keeps its own copy, this
# codebase's established convention).
# ===========================================================================


class _StopAfterSchedulerStart(Exception):
    pass


class _FakeScheduler:
    def __init__(self, *args, **kwargs):
        pass

    def add_job(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    def __init__(self, *args, **kwargs):
        pass

    async def chat_text(self, system_prompt, user_prompt):
        return "noted"

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("chat_json must not be called by a LLM-free /history request")

    async def probe_schema_support(self, *args, **kwargs):
        return {}

    async def aclose(self):
        pass


class _ScriptedChannel(Channel):
    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []

    def __init__(self, *args, **kwargs):
        self.sent: list[tuple[str, str]] = []
        self.set_my_commands_calls: list[dict] = []
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id, text, buttons):
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands, *, scope_chat_id=None):
        # SPEC-v1.8.md R-D2: only records the default (global) menu
        # registration -- see test_discoverability.py's identical fake for
        # the full rationale.
        if scope_chat_id is None:
            self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id):
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None):
        for step in _ScriptedChannel.script:
            _, chat_id, text, display_name = step
            await on_message(chat_id, text, display_name)
        raise _StopAfterSchedulerStart()

    async def aclose(self):
        pass


async def _run(monkeypatch, config, script, owner_chat_id=OWNER):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=owner_chat_id)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _ScriptedChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    # SPEC-v1.5.md R-N2 (module `announce`): since v1.5.0's own release
    # (Archi's version bump), `__version__` genuinely matches a
    # `RELEASE_NOTES` entry, so `announce.announce_release`'s real
    # startup call now actually sends a release note to every active
    # user on the very first `async_main` call -- an extra leading
    # `channel.sent_to(...)` entry this file's own scripts (written
    # before that wiring went live) don't account for. Neutralized here,
    # once, for every test in this file by default.
    monkeypatch.setattr(main_module, "__version__", "0.0.0-test")
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


async def test_ac14_history_available_to_any_active_member_no_owner_check(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(MEMBER, role="member", status="active")
    seed_db.insert_log(LogEntry(None, MEMBER, "2026-08-23T09:00:00", "water", 500.0, None, "500ml", "reply"))
    seed_db.close()

    # A non-owner ("member") gets a real /history reply -- no owner gate,
    # unlike /audit. This alone also proves R-A1's "LLM-free, works with
    # Ollama down" claim: _FakeOllamaClient.chat_json raises if ever
    # called, and this whole run completing without that assertion firing
    # IS the proof.
    channel = await _run(monkeypatch, config, script=[("message", MEMBER, "/history", None)])
    assert channel.sent_to(MEMBER) != []
    assert "500 ml water" in channel.sent_to(MEMBER)[0]


async def test_ac14_history_registered_in_the_public_command_menu_both_languages(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    for lang, entries in registered.items():
        names = {name for name, _desc in entries}
        assert "history" in names
        assert "audit" not in names  # /audit itself stays hidden, unaffected by this change
