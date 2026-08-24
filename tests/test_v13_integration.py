"""SPEC-v1.3.md §11 integration step -- the final pass that wires the two
independently-shipped parallel modules (`audit-capture`, `audit-view`)
into `main.py`'s real `on_message` closure: `source="nl"` at the full-NL
target call site, the `edit`-action recorder call inside `_execute_edit`
(the one capture site `main.py` itself owns, per §11), and
`command.kind == "audit"` routed to `audit_view.render_recent` behind an
owner gate, with `/audit` kept out of `set_my_commands`.

Every module's own test file (`test_audit.py`, `test_audit_capture.py`,
`test_audit_view.py`) already proves its owned ACs by calling its own
functions directly. This file is different in kind, mirroring
`tests/test_v12_integration.py`'s own precedent: it drives the REAL,
wired `async_main`/`on_message` closure (not re-implemented) so a genuine
wiring mistake -- a missed `source=`, a forgotten owner re-check, an
admin-hidden command leaking into the menu -- would show up here even
though every module's own unit tests stay green.

Covers the integration-owned ACs from SPEC-v1.3.md §11's table: AC-C2
(edit, recorded in main.py), AC-C7 (not-audited property, across a real
plain log AND real read-only commands), AC-V3 (owner-only routing +
menu-hidden), plus AC-A2 (fail-open) and AC-R1 (retention) re-confirmed
at the fully-wired level with a realistic mixed-action volume.

Live-environment rule (unchanged from every other v1.x test file): every
DB here is a scratch `tmp_path` SQLite file. Nothing in this file ever
opens `data/habits.db`, and no real Telegram/Ollama call is made.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, i18n, target_nl
from habit_assistant.core.commands import Command
from habit_assistant.core.target_nl import TargetIntent
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import AuditEntry, LogEntry

OWNER = "1001"
STRANGER = "2002"


# ---------------------------------------------------------------------------
# Shared harness -- mirrors tests/test_v12_integration.py's own
# `_ScriptedChannel`/`_FakeScheduler`/`_run_async_main` pattern (each
# integration test file keeps its own copy, this codebase's established
# convention).
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

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakeOllamaClient:
    """A queue of canned `chat_json` responses, consumed in call order --
    mirrors tests/test_v12_integration.py's own fake. Most tests below
    monkeypatch `target_nl.classify_target_intent` directly instead of
    routing a real NL classification call through this (matches tests/
    test_v11_integration.py's own established precedent), so this fake's
    `chat_json` is only reached by the plain-log extraction path."""

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


def _extraction(category: str, value, confidence: float = 0.9) -> str:
    return json.dumps({"category": category, "value": value, "confidence": confidence})


class _ScriptedChannel(Channel):
    """Drives the REAL `on_message`/`on_callback` closures `async_main`
    wires, in an arbitrary caller-supplied order."""

    last_instance: "_ScriptedChannel | None" = None
    script: list[tuple] = []

    def __init__(self, *args, **kwargs) -> None:
        self.sent: list[tuple[str, str]] = []
        self.set_my_commands_calls: list[dict] = []
        _ScriptedChannel.last_instance = self

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def send_actionable(self, chat_id: str, text: str, buttons) -> None:
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands) -> None:
        self.set_my_commands_calls.append(commands)

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    async def run(self, on_message, on_callback=None) -> None:
        for step in _ScriptedChannel.script:
            if step[0] == "message":
                _, chat_id, text, display_name = step
                await on_message(chat_id, text, display_name)
            else:
                _, chat_id, data, source_text, cb_id = step
                assert on_callback is not None
                await on_callback(chat_id, data, source_text, cb_id)
        raise _StopAfterSchedulerStart()

    async def aclose(self) -> None:
        pass


def _run_async_main(monkeypatch, config, script, owner_chat_id=OWNER, responses=None):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module,
        "load_secrets",
        lambda: SimpleNamespace(telegram_bot_token="fake-token", telegram_chat_id=owner_chat_id),
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
    _FakeScheduler.last_instance = None
    _ScriptedChannel.last_instance = None
    _ScriptedChannel.script = script
    _FakeOllamaClient.responses = list(responses or [])
    return main_module


async def _run(monkeypatch, config, script, owner_chat_id=OWNER, responses=None):
    main_module = _run_async_main(monkeypatch, config, script, owner_chat_id, responses)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)
    return _ScriptedChannel.last_instance


def _fake_classify_water_2000():
    async def fake_classify(text, llm, registry_, config_):
        return TargetIntent(habit_id="water", goal_base_unit=2000.0)

    return fake_classify


# ===========================================================================
# The big one: a real end-to-end flow through the wired on_message --
# NL target set (source=nl), a plain log, an edit (recorded here in
# main.py, AC-C2), then the owner's /audit sees everything newest-first
# with the right actor/old->new/source (AC-V1, AC-C2, AC-C3's nl half).
# ===========================================================================


async def test_full_flow_nl_target_then_log_then_edit_then_owner_audit_sees_all_newest_first(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    monkeypatch.setattr(target_nl, "classify_target_intent", _fake_classify_water_2000())

    script = [
        # 1. Full-NL target set -- must record with source="nl", not "command".
        ("message", OWNER, "from now on I want to drink 2 liters a day", None),
        # 2. A plain log (500ml) -- writes a `logs` row, writes NO audit row (AC-C7).
        ("message", OWNER, "500ml", None),
        # 3. An edit of that log -- the one capture site main.py itself owns (AC-C2).
        ("message", OWNER, "make that 300ml", None),
        # 4. The owner reads it all back.
        ("message", OWNER, "/audit", None),
    ]
    channel = await _run(monkeypatch, config, script, responses=[_extraction("water", 500)])

    db = Database(tmp_path / "habits.db")
    try:
        rows = db.recent_audit(10)
        # Exactly two rows: the NL target_set and the edit. The plain log
        # itself, and the /audit read, wrote none (AC-C7).
        assert [r["action"] for r in rows] == ["edit", "target_set"]  # newest-first

        edit_row = rows[0]
        assert edit_row["entity"] == "water"
        assert edit_row["old_value"] == "500"
        assert edit_row["new_value"] == "300"
        assert edit_row["source"] == "command"
        assert edit_row["user_id"] == OWNER

        target_row = rows[1]
        assert target_row["entity"] == "water"
        assert target_row["new_value"] == "2000"
        assert target_row["source"] == "nl"  # NOT "command" -- the whole point of this wiring

        # The owner's own /audit reply reflects both, newest-first (the
        # edit line renders before the target-set line), with "you" as
        # the actor and the real source labels visible.
        audit_reply = channel.sent_to(OWNER)[-1]
        assert audit_reply.index(i18n.t("audit_action_edit", "en")) < audit_reply.index(
            i18n.t("audit_action_target_set", "en")
        )
        assert "(command)" in audit_reply
        assert "(nl)" in audit_reply
        assert audit_reply.count(i18n.t("audit_actor_you", "en")) == 2  # both rows are the owner's own
    finally:
        db.close()


# ===========================================================================
# AC-C7 (integration-owned): the not-audited property, exercised through a
# real plain log AND real read-only commands, not just capture's own
# per-module claims.
# ===========================================================================


async def test_plain_habit_log_and_read_only_commands_write_no_audit_row(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "500ml", None),  # plain log
        ("message", OWNER, "/habits", None),  # read-only
        ("message", OWNER, "/help", None),  # read-only
        ("message", OWNER, "/target water", None),  # target SHOW, not set
        ("message", OWNER, "/remind water", None),  # remind SHOW, not set
    ]
    await _run(monkeypatch, config, script, responses=[_extraction("water", 500)])

    db = Database(tmp_path / "habits.db")
    try:
        assert db.recent_audit(100) == []  # not one single row from any of the above
    finally:
        db.close()


# ===========================================================================
# AC-V3: owner-only routing (silent no-op for a non-owner) + menu-hidden.
# ===========================================================================


async def test_non_owner_audit_is_silent_and_reveals_nothing(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, role="member", status="active")
    seed_db.insert_audit(AuditEntry(None, "2026-08-20T09:00:00", OWNER, "lang_set", None, "auto", "th", "command"))
    seed_db.close()

    script = [("message", STRANGER, "/audit", None)]
    channel = await _run(monkeypatch, config, script)

    assert channel.sent_to(STRANGER) == []  # not even an error/usage reply -- reveals nothing


async def test_audit_never_added_to_the_public_command_menu(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    channel = await _run(monkeypatch, config, script=[])

    registered = channel.set_my_commands_calls[0]
    for lang, entries in registered.items():
        names = {name for name, _desc in entries}
        assert "audit" not in names
        # The four true admin commands stay excluded too (unchanged from
        # v1.2 -- re-confirmed here since this is the exact same dict
        # this pass's own audit routing sits right next to).
        assert not names & {"approve", "block", "users", "invite"}


# ===========================================================================
# AC-A2 (fail-open), re-confirmed at the FULLY WIRED level: a forced
# audit-write failure must leave the triggering action's own reply and DB
# write completely unaffected, all the way through the real on_message
# closure -- not just core/audit.py:record in isolation (already proven
# in tests/test_audit.py).
# ===========================================================================


async def test_audit_db_failure_leaves_the_triggering_actions_reply_and_write_unchanged(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    def raising_insert_audit(self, entry):
        raise RuntimeError("simulated audit DB failure")

    monkeypatch.setattr(Database, "insert_audit", raising_insert_audit)

    script = [("message", OWNER, "/target water 2000", None)]
    channel = await _run(monkeypatch, config, script)

    # The reply is byte-identical to what /target water 2000 always sends
    # -- no traceback, no "something went wrong", nothing different.
    reply = channel.sent_to(OWNER)[-1]
    assert "2000" in reply

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_target(OWNER, "water") == 2000.0  # the actual write succeeded
        assert db.recent_audit(10) == []  # the audit write failed silently, as designed
    finally:
        db.close()


# ===========================================================================
# Punch-list item 1's own explicit "verify that claim": the deterministic
# /remind, /lang, /quiet, and text-/undo call sites need NO main.py
# change -- their `source` parameters' "command" defaults must already be
# correct as called, through the REAL wiring.
# ===========================================================================


async def test_deterministic_remind_lang_quiet_undo_all_record_source_command_by_default(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "/remind water 08:00 12:00", None),
        ("message", OWNER, "/lang th", None),
        ("message", OWNER, "/quiet 22:00-07:00", None),
        ("message", OWNER, "500ml", None),  # a log to undo
        ("message", OWNER, "/undo", None),
    ]
    await _run(monkeypatch, config, script, responses=[_extraction("water", 500)])

    db = Database(tmp_path / "habits.db")
    try:
        rows = {r["action"]: r for r in db.recent_audit(20)}
        assert rows["remind_set"]["source"] == "command"
        assert rows["lang_set"]["source"] == "command"
        assert rows["quiet_set"]["source"] == "command"
        assert rows["undo"]["source"] == "command"  # the TEXT /undo path, not the button path
    finally:
        db.close()


async def test_button_undo_records_source_button_through_the_real_on_callback(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    from habit_assistant.storage.models import LogEntry

    row_id = seed_db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml", "reply"))
    seed_db.close()

    script = [("callback", OWNER, f"undo:{row_id}", "500ml", "cb-1")]
    await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        row = db.recent_audit(1)[0]
        assert row["action"] == "undo"
        assert row["source"] == "button"
    finally:
        db.close()


# ===========================================================================
# AC-R1 re-verified at the fully-wired level with a realistic MIX of
# capture-generated audit rows (not just directly-inserted AuditEntry rows
# the way tests/test_audit.py's own AC-R1 tests do) -- punch-list item 4.
# ===========================================================================


async def test_startup_prune_correct_with_a_realistic_mixed_capture_volume(tmp_path, monkeypatch):
    db_path = tmp_path / "habits.db"

    # First process "run": generate a realistic mix of real capture-site
    # audit rows (not hand-built AuditEntry rows) through the real wiring.
    config = Config.model_validate({"app": {"db_path": str(db_path)}, "audit": {"retention_days": 365}})
    script = [
        ("message", OWNER, "/target water 2000", None),
        ("message", OWNER, "/remind water 08:00", None),
        ("message", OWNER, "/lang th", None),
        ("message", OWNER, "/approve 3003", None),
    ]
    await _run(monkeypatch, config, script)

    db = Database(db_path)
    fresh_count = len(db.recent_audit(100))
    assert fresh_count == 4  # target_set, remind_set, lang_set, user_approve

    # Backdate one of them, plus insert one genuinely ancient row directly,
    # so the second startup has both freshly-generated (keep) and old
    # (prune) rows to sort out -- a realistic post-migration mix, not just
    # a single hand-built scenario.
    ancient_ts = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    db._conn.execute("UPDATE audit_log SET ts = ? WHERE action = 'lang_set'", (ancient_ts,))
    db._conn.commit()
    db.insert_audit(AuditEntry(None, ancient_ts, OWNER, "quiet_set", None, None, "[]", "command"))
    db.close()

    # Second process "run" (a fresh async_main call over the SAME db_path,
    # exactly what a real restart looks like): startup prune runs once,
    # before the loop.
    await _run(monkeypatch, config, script=[])

    db2 = Database(db_path)
    try:
        remaining = db2.recent_audit(100)
        remaining_actions = sorted(r["action"] for r in remaining)
        # The 2 backdated/ancient rows are gone; the 3 fresh, un-backdated
        # ones from the first run survive untouched.
        assert "lang_set" not in remaining_actions  # pruned (backdated to 400 days old)
        assert "quiet_set" not in remaining_actions  # pruned (inserted ancient)
        assert "target_set" in remaining_actions
        assert "remind_set" in remaining_actions
        assert "user_approve" in remaining_actions
        assert len(remaining) == 3
    finally:
        db2.close()


# ===========================================================================
# Vera's integration-gate adversarial additions (coordinator's punch list,
# 2026-08-22). Everything below drives the REAL `async_main`/`on_message`/
# `on_callback` wiring, same conventions as the tests above -- tmp_path-only
# SQLite, mocked LLM/Telegram, never `data/habits.db`.
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Owner gate security: every non-owner chat state's `/audit` (including
# edge shapes -- a high N, the Thai alias) never reads audit data at all --
# not "the reply doesn't show it", `Database.recent_audit` is structurally
# never CALLED. Plus an explicit "can a member ever become the owner"
# probe (no viable impersonation vector was found by inspection -- this
# locks that in as a regression test, not just a code-reading claim).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,expected_reply_id",
    [
        ("unknown", "access_pending"),
        ("pending", "access_pending"),
        ("blocked", "access_denied"),
        ("active_member", None),  # true silence -- R-V3's "reveals nothing"
    ],
)
async def test_audit_from_every_non_owner_state_never_reads_audit_data(tmp_path, monkeypatch, state, expected_reply_id):
    """R-A1 (gate-before-dispatch) means only an ACTIVE non-owner ever
    reaches `/audit`'s own owner re-check at all -- unknown/pending/
    blocked chats are intercepted by `access.handle_gate` first and get
    the ORDINARY v1.2 onboarding/denial reply, not audit-specific
    silence. All four states share one hard guarantee, proven
    structurally here (not just "the reply looked empty"): `Database.
    recent_audit` is never invoked for any of them, even when the
    request explicitly asks for a large N (`/audit 50`, one of the
    coordinator's own named edge shapes)."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    if state == "pending":
        seed_db.upsert_user(STRANGER, status="pending")
    elif state == "blocked":
        seed_db.upsert_user(STRANGER, status="blocked")
    elif state == "active_member":
        seed_db.upsert_user(STRANGER, role="member", status="active")
    # "unknown": no row at all for STRANGER.
    seed_db.insert_audit(AuditEntry(None, "2026-08-20T09:00:00", OWNER, "lang_set", None, "auto", "th", "command"))
    seed_db.close()

    def raising_recent_audit(self, limit):
        raise AssertionError(f"recent_audit must never be called for a non-owner /audit (state={state!r})")

    monkeypatch.setattr(Database, "recent_audit", raising_recent_audit)

    script = [("message", STRANGER, "/audit 50", None)]
    channel = await _run(monkeypatch, config, script)

    if expected_reply_id is None:
        assert channel.sent_to(STRANGER) == []
    else:
        assert channel.sent_to(STRANGER) == [i18n.t(expected_reply_id, "en")]


async def test_audit_thai_alias_from_a_stranger_triggers_onboarding_not_audit_data(tmp_path, monkeypatch):
    """The Thai alias `ประวัติ` is just as gated as the slash form -- a
    never-before-seen chat sending it gets the ordinary onboarding reply
    (R-A2), never a peek at audit data."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.insert_audit(AuditEntry(None, "2026-08-20T09:00:00", OWNER, "lang_set", None, "auto", "th", "command"))
    seed_db.close()

    def raising_recent_audit(self, limit):
        raise AssertionError("recent_audit must never be called for an unknown chat's /audit attempt")

    monkeypatch.setattr(Database, "recent_audit", raising_recent_audit)

    script = [("message", STRANGER, "ประวัติ", None)]
    channel = await _run(monkeypatch, config, script)

    # "ประวัติ" itself contains Thai characters, so auto-detection (R-A2's
    # reply is a RESPONSE to this inbound text) correctly resolves Thai,
    # not English -- not audit's concern, just this test's own input choice.
    assert channel.sent_to(STRANGER) == [i18n.t("access_pending", "th")]


async def test_active_member_audit_thai_alias_is_also_silent(tmp_path, monkeypatch):
    """The inverse pairing of the test above: an ALREADY-active member
    (past the gate) sending the Thai alias gets the same true silence as
    the slash form -- `_match_audit` recognizing `ประวัติ` doesn't create
    a second, less-guarded path into the owner-only view."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, role="member", status="active")
    seed_db.close()

    script = [("message", STRANGER, "ประวัติ 5", None)]
    channel = await _run(monkeypatch, config, script)

    assert channel.sent_to(STRANGER) == []


async def test_member_cannot_impersonate_owner_or_leak_audit_via_any_exposed_command(tmp_path, monkeypatch):
    """Security probe: is there ANY exposed command through which a
    non-owner could become classified as "owner", or otherwise reach
    audit data? `role="owner"` is set exactly once, by
    `attribute_legacy_to_owner` at startup from the `.env`-loaded
    `secrets.telegram_chat_id` -- no in-chat command (`/approve`,
    `/invite`, or anything else) ever writes the `role` column. This
    fires a barrage of self-privileged attempts from an ordinary member
    and confirms every single one is a silent no-op that changes
    nothing: never a reply, never a role change, never an audit write."""
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(STRANGER, role="member", status="active")
    seed_db.close()

    script = [
        ("message", STRANGER, f"/approve {STRANGER}", None),  # self-approve attempt
        ("message", STRANGER, f"/invite {STRANGER}", None),  # self-invite attempt
        ("message", STRANGER, "/users", None),  # owner-only listing attempt
        ("message", STRANGER, "/audit", None),
        ("message", STRANGER, "/audit 50", None),
    ]
    channel = await _run(monkeypatch, config, script)

    assert channel.sent_to(STRANGER) == []  # not one of the 5 attempts produced any reply

    db = Database(tmp_path / "habits.db")
    try:
        assert access.classify(db, STRANGER) == "active"  # never escalated to "owner"
        assert db.get_user(STRANGER)["role"] == "member"
        assert db.recent_audit(50) == []  # not one of the 5 attempts wrote a single row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. End-to-end capture correctness: a realistic OWNER + MEMBER session --
# the member's own actions audit under THEIR chat id (not the owner's), the
# owner's /approve of a third, brand-new chat records target_user_id, and
# the owner's /audit shows everyone newest-first with "you" appearing ONLY
# for the owner's own row.
# ---------------------------------------------------------------------------


async def test_two_user_session_capture_attributes_correctly_and_owner_audit_shows_actor_and_you(
    tmp_path, monkeypatch
):
    member = "3003"
    newcomer = "4004"
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.upsert_user(member, role="member", status="active")
    seed_db.close()

    script = [
        ("message", member, "/target water 1800", None),
        ("message", member, "/remind water 07:30", None),
        ("message", member, "/lang th", None),
        # A brand-new chat's first-ever message -- unknown -> pending,
        # recorded as user_pending (actor=target_user_id=newcomer).
        ("message", newcomer, "this is a private message with PII in it", "Newbie"),
        # The owner approves them -- user_approve, actor=owner, target=newcomer.
        ("message", OWNER, f"/approve {newcomer}", None),
        ("message", OWNER, "/audit", None),
    ]
    channel = await _run(monkeypatch, config, script)

    db = Database(tmp_path / "habits.db")
    try:
        rows = db.recent_audit(20)
        by_action = {r["action"]: r for r in rows}
        assert set(by_action) == {"target_set", "remind_set", "lang_set", "user_pending", "user_approve"}

        # The member's own three actions all attribute to the MEMBER, not
        # the owner who never touched any of them.
        assert by_action["target_set"]["user_id"] == member
        assert by_action["remind_set"]["user_id"] == member
        assert by_action["lang_set"]["user_id"] == member

        approve_row = by_action["user_approve"]
        assert approve_row["user_id"] == OWNER
        assert approve_row["target_user_id"] == newcomer

        pending_row = by_action["user_pending"]
        assert pending_row["user_id"] == newcomer
        assert pending_row["target_user_id"] == newcomer
        # AC-P1 (privacy), re-confirmed through the real gate: the
        # newcomer's actual message text appears NOWHERE in the audit row.
        assert "PII" not in (pending_row["old_value"] or "") and "PII" not in (pending_row["new_value"] or "")
        assert pending_row["new_value"] == "pending"

        # The owner's own /audit reply: "you" appears EXACTLY once (only
        # the /approve row is the owner's own action); the member's chat id
        # and the newcomer's chat id both appear (their own rows render by
        # chat id, no display_name captured for the member).
        audit_reply = channel.sent_to(OWNER)[-1]
        assert audit_reply.count(i18n.t("audit_actor_you", "en")) == 1
        assert member in audit_reply
        assert newcomer in audit_reply

        # Newest-first: /approve was the LAST action taken chronologically,
        # so its line is the first one after the header.
        lines = audit_reply.splitlines()
        assert i18n.t("audit_action_user_approve", "en") in lines[1]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. AC-A2 fail-open at the wired level: a forced `insert_audit` failure
# during REAL message handling both (a) emits a log line (not a silent
# swallow with zero trace) and (b) recovers cleanly -- a transient failure
# on one action does not poison audit recording for the NEXT action.
# ---------------------------------------------------------------------------


async def test_audit_write_failure_emits_a_log_line_and_a_later_action_records_normally(tmp_path, monkeypatch, caplog):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})

    real_insert_audit = Database.insert_audit
    call_state = {"count": 0}

    def flaky_insert_audit(self, entry):
        call_state["count"] += 1
        if call_state["count"] == 1:
            raise RuntimeError("simulated transient audit DB failure")
        return real_insert_audit(self, entry)

    monkeypatch.setattr(Database, "insert_audit", flaky_insert_audit)

    script = [
        ("message", OWNER, "/target water 2000", None),  # audit write #1 -- fails
        ("message", OWNER, "/lang th", None),  # audit write #2 -- succeeds (recovery)
    ]
    main_module = _run_async_main(monkeypatch, config, script)
    # main.py's own setup_logging() calls logging.basicConfig(force=True),
    # which tears down pytest's caplog handler on the root logger -- a
    # real async_main run would otherwise silently defeat caplog here.
    # Console formatting is irrelevant to this test, so it's a no-op.
    monkeypatch.setattr(main_module, "setup_logging", lambda level: None)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.audit"):
        with pytest.raises(_StopAfterSchedulerStart):
            await main_module.async_main(args)
    channel = _ScriptedChannel.last_instance

    # Both actions' own replies/writes are completely unaffected by the
    # first audit failure -- fail-open means the USER never sees a trace.
    assert "2000" in channel.sent_to(OWNER)[0]
    assert any("ได้เลย" in t or "th" in t for t in channel.sent_to(OWNER)[1:])

    # But it was NOT silent at the log level -- record()'s own
    # logger.exception fired exactly once, naming the failed action.
    audit_failures = [r for r in caplog.records if "Audit record failed" in r.getMessage()]
    assert len(audit_failures) == 1
    assert "target_set" in audit_failures[0].getMessage()

    db = Database(tmp_path / "habits.db")
    try:
        assert db.get_target(OWNER, "water") == 2000.0  # write #1's real action still succeeded
        assert db.get_user(OWNER)["language_pref"] == "th"  # write #2's real action succeeded
        rows = db.recent_audit(10)
        # Exactly one row -- the RECOVERED lang_set. The failed target_set
        # attempt left no row (fail-open means no partial/corrupt row
        # either), and it did not somehow also block lang_set's own audit
        # write from succeeding afterward.
        assert [r["action"] for r in rows] == ["lang_set"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. AC-A3 (byte-identical gate): spot-check that audit's presence hasn't
# changed a single pre-v1.3 (v1.2-era) reply/behavior -- confirmation text,
# undo text, and reminder text all still read exactly as they did before
# this feature existed.
# ---------------------------------------------------------------------------


async def test_ac_a3_spot_check_confirmation_and_undo_text_unchanged_by_audit(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    script = [
        ("message", OWNER, "500ml", None),
        ("message", OWNER, "/undo", None),
    ]
    channel = await _run(monkeypatch, config, script, responses=[_extraction("water", 500)])

    owner_msgs = channel.sent_to(OWNER)
    # Exact v1.2-era strings (tests/test_v12_integration.py's own
    # byte-identical pins) -- unchanged by the new audit_log side-write.
    assert owner_msgs[0] == "✅ 500 ml logged — today 500 / 2500 ml (20%)"
    assert owner_msgs[1].startswith("↩️ Undone") and "0 / 2500 ml (0%)" in owner_msgs[1]

    # And the side-write DID happen (proving audit is genuinely wired,
    # not merely "silent because it's broken") -- one undo row, entity
    # water, matching the removed value.
    db = Database(tmp_path / "habits.db")
    try:
        rows = db.recent_audit(10)
        assert [r["action"] for r in rows] == ["undo"]
        assert rows[0]["entity"] == "water"
        assert rows[0]["old_value"] == "500"
    finally:
        db.close()


async def test_ac_a3_spot_check_reminder_text_unchanged_by_audit(tmp_path, monkeypatch):
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.reminders import ReminderState, run_due_reminders

    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    seed_db.close()

    db = Database(tmp_path / "habits.db")
    try:
        registry = HabitRegistry.from_config(config)
        channel = _ScriptedChannel()
        state = ReminderState()
        await run_due_reminders(channel, config, registry, db, state, clock=lambda: datetime(2026, 8, 22, 8, 0, 0))
        # v1.2-era exact reminder text, byte-identical -- reminders are
        # explicitly NOT a capture site (§2.2/§10), so no audit row either.
        # (Unprompted sends for a user with no /lang override resolve
        # "auto" to `config.i18n.primary_language`, Thai by default --
        # the same v1.2-era behavior tests/test_v12_integration.py's own
        # `test_lang_th_propagates_to_reminder_text` documents; English
        # would be the wrong expectation here, not audit's doing.)
        assert channel.sent_to(OWNER) == [i18n.t("reminder_water", "th")]
        assert db.recent_audit(10) == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. AC-R1 retention: retention_days=0 (keep forever) at the fully-wired
# startup level, and the exact `ts < cutoff` boundary (a row AT the cutoff
# survives; one second older is pruned) via the same `db.prune_audit`
# main.py itself calls -- driven with a caller-chosen deterministic cutoff
# rather than racing the wall clock `async_main`'s own `datetime.now()`
# call uses internally.
# ---------------------------------------------------------------------------


async def test_retention_days_zero_prunes_nothing_at_startup(tmp_path, monkeypatch):
    config = Config.model_validate({"app": {"db_path": str(tmp_path / "habits.db")}, "audit": {"retention_days": 0}})
    seed_db = Database(tmp_path / "habits.db")
    seed_db.upsert_user(OWNER, role="owner", status="active")
    ancient_ts = (datetime.now() - timedelta(days=3650)).isoformat(timespec="seconds")  # ~10 years old
    seed_db.insert_audit(AuditEntry(None, ancient_ts, OWNER, "lang_set", None, "auto", "th", "command"))
    seed_db.close()

    await _run(monkeypatch, config, script=[])

    db = Database(tmp_path / "habits.db")
    try:
        # retention_days=0 means "never prune" -- even a decade-old row
        # survives a real startup untouched.
        assert len(db.recent_audit(10)) == 1
    finally:
        db.close()


def test_prune_audit_boundary_row_exactly_at_cutoff_survives_one_second_older_is_pruned(tmp_path):
    """`db.prune_audit`'s own SQL is `WHERE ts < cutoff_ts` (strict
    inequality) -- a row whose `ts` is exactly EQUAL to the cutoff must
    survive; only a row strictly OLDER is deleted. Exercised directly
    against `db.prune_audit` (the exact method `main.py`'s startup call
    invokes) with a caller-chosen cutoff, since racing `async_main`'s own
    internal `datetime.now()` call to land a row at an exact wall-clock
    boundary would be flaky by construction."""
    db = Database(tmp_path / "habits.db")
    try:
        db.upsert_user(OWNER, role="owner", status="active")
        cutoff = "2026-01-01T00:00:00"
        db.insert_audit(AuditEntry(None, cutoff, OWNER, "lang_set", None, "auto", "en", "command"))  # AT cutoff
        db.insert_audit(
            AuditEntry(None, "2025-12-31T23:59:59", OWNER, "quiet_set", None, None, "[]", "command")
        )  # 1 second OLDER than cutoff
        db.insert_audit(
            AuditEntry(None, "2026-01-01T00:00:01", OWNER, "quiet_off", None, "[]", "[]", "command")
        )  # 1 second NEWER than cutoff

        deleted = db.prune_audit(cutoff)

        assert deleted == 1
        remaining_actions = sorted(r["action"] for r in db.recent_audit(10))
        assert remaining_actions == ["lang_set", "quiet_off"]  # AT-cutoff and newer both survive
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. Migration rehearsal: a v6-shaped (v1.2-era) scratch DB with real
# pre-existing data, opened through the REAL `async_main` startup (the same
# migration-007-then-prune sequence production runs on upgrade day) --
# audit_log starts empty, pre-existing v1.2 data still works, and a real
# action afterward populates it.
# ---------------------------------------------------------------------------


async def test_migration_007_rehearsal_on_a_v1_2_shaped_scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "upgrade_rehearsal_v13.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
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
        );
        CREATE INDEX idx_logs_ts_cat ON logs(ts, category);
        CREATE INDEX idx_logs_category ON logs(category);
        CREATE INDEX idx_logs_deleted_at ON logs(deleted_at);
        CREATE INDEX idx_logs_user ON logs(user_id, category, ts);
        CREATE TABLE habit_targets (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    TEXT,
          habit_id   TEXT NOT NULL,
          goal       REAL NOT NULL,
          updated_at TEXT,
          UNIQUE(user_id, habit_id)
        );
        CREATE TABLE users (
          chat_id                TEXT PRIMARY KEY,
          role                   TEXT NOT NULL DEFAULT 'member',
          status                 TEXT NOT NULL DEFAULT 'pending',
          display_name           TEXT,
          language_pref          TEXT NOT NULL DEFAULT 'auto',
          quiet_hours_json       TEXT,
          snooze_default_minutes INTEGER,
          created_at             TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE user_reminder_times (
          user_id  TEXT NOT NULL,
          habit_id TEXT NOT NULL,
          time     TEXT NOT NULL,
          PRIMARY KEY (user_id, habit_id, time)
        );
        PRAGMA user_version = 6;
        """
    )
    conn.execute("INSERT INTO users (chat_id, role, status) VALUES (?, 'owner', 'active')", (OWNER,))
    today_ts = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source, habit_type, user_id) "
        "VALUES (?, 'water', 500.0, NULL, '500ml', 'reply', 'numeric', ?)",
        (today_ts, OWNER),
    )
    conn.execute("INSERT INTO habit_targets (user_id, habit_id, goal) VALUES (?, 'water', 3000.0)", (OWNER,))
    conn.commit()
    conn.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}})
    script = [
        ("message", OWNER, "/habits", None),  # pre-existing v1.2 data still works
        ("message", OWNER, "/target water 2500", None),  # a real action, post-upgrade
        ("message", OWNER, "/audit", None),
    ]
    channel = await _run(monkeypatch, config, script, owner_chat_id=OWNER)

    habits_reply = next(t for t in channel.sent_to(OWNER) if i18n.t("habits_overview_header", "en") in t)
    assert "today 500 ml" in habits_reply  # the pre-existing legacy log is still readable/correct

    audit_reply = channel.sent_to(OWNER)[-1]
    # Exactly the one post-upgrade action shows up -- audit_log truly
    # started empty (migration 007 creates it empty, R-M1) and only
    # populated once a real state-changing action ran afterward.
    assert audit_reply.count(i18n.t("audit_action_target_set", "en")) == 1
    assert i18n.t("audit_actor_you", "en") in audit_reply

    db = Database(db_path)
    try:
        assert db.schema_version == 9  # SPEC-v1.5.md's additive migration 008 + SPEC-v1.6.md's additive migration 009 also land now
        assert db.get_target(OWNER, "water") == 2500.0
        rows = db.recent_audit(10)
        assert len(rows) == 1
        assert rows[0]["action"] == "target_set"
        assert rows[0]["old_value"] == "3000"
        assert rows[0]["new_value"] == "2500"
    finally:
        db.close()
