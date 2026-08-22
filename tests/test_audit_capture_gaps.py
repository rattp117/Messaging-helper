"""Vera's adversarial hardening pass on top of Luna's `tests/test_audit_capture.py`
(SPEC-v1.3.md module `audit-capture`, IMPL-v1.3-capture.md). Same conventions as the
Luna file this extends: a real on-disk SQLite `Database` (tmp_path), no DB mocks,
`execute_*` functions called directly.

Owned ACs in scope: AC-C1 (undo), AC-C3 (target), AC-C4 (remind), AC-C5 (lang/quiet),
AC-C6 (admin), AC-P1 (privacy). This file does NOT re-test the happy paths Luna
already covered -- it targets angles her 32 tests left open:

1. Old-value correctness under multi-step sequences (target set->set->clear->set,
   remind set->off->default, lang th->en->th) -- each row's old_value must be the
   TRUE prior state, not a stale/re-derived one.
2. Record-only-on-success in the PRIMARY-write-fails direction (distinct from
   Luna's fail-open tests, which force the AUDIT write to fail) -- if the user's
   own write fails, no audit row may exist at all.
3. Fail-open at capture sites Luna's suite didn't force-fail (admin block,
   handle_gate's unknown->pending).
4. Cross-user attribution under interleaved actions.
5. Vocabulary conformance -- every action/source this module's capture sites can
   ever write is drawn from `audit.ACTIONS`/`audit.SOURCES`, and every action has a
   corresponding `audit_view` label (cross-checked against
   `core/audit_view.py::_ACTION_LABEL_MSG_IDS`, so a captured action with no viewer
   label would be caught here too).
6. Idempotent/no-op paths -- a second tap on an already-undone log must not record
   a second row; a repeat-approve of an already-active user's actual recorded
   behavior is asserted explicitly (documented, not assumed).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, audit, audit_view, commands, preferences, schedules, targets_command, undo_ui
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "1574572064"
MEMBER = "88899900"
STRANGER = "55544433"
MEMBER2 = "22233344"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "audit_capture_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    database.upsert_user(MEMBER2, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def channel():
    return FakeChannel()


def _rows(db: Database) -> list:
    return db.recent_audit(100)


def _boom(*args, **kwargs):
    raise RuntimeError("simulated failure")


# ===========================================================================
# 1. Old-value correctness under sequences.
# ===========================================================================


async def test_target_set_set_clear_set_chain_old_values_track_true_prior_state(db, config):
    """set(2000) -> set(1500) -> clear() -> set(3000). Each row's old_value
    must be the TRUE prior state: config default, then 2000, then 1500 (the
    override, per SPEC-v1.3.md's "clear's old is the prior OVERRIDE, not the
    effective goal" rule), then None (post-clear, back to config default)."""
    config_default = config.reminders.water.goal_ml

    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=2000.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=1500.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="clear", category="water"),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=3000.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )

    rows = list(reversed(_rows(db)))  # oldest first for readability
    assert [r["action"] for r in rows] == ["target_set", "target_set", "target_clear", "target_set"]

    assert rows[0]["old_value"] == f"{config_default:g}"  # first set: old = config default (no override yet)
    assert rows[0]["new_value"] == "2000"

    assert rows[1]["old_value"] == "2000"  # second set: old = the just-set override
    assert rows[1]["new_value"] == "1500"

    assert rows[2]["old_value"] == "1500"  # clear: old = the prior OVERRIDE (1500), not the config default
    assert rows[2]["new_value"] is None

    assert rows[3]["old_value"] == f"{config_default:g}"  # set after clear: old = effective goal = config default again
    assert rows[3]["new_value"] == "3000"


async def test_remind_set_off_default_chain_old_values_track_true_prior_state(db, config):
    """set(["08:00","12:00"]) -> off -> default. Each row's old_value must be
    the true prior stored times, and remind_default's old is the times that
    were active right before clearing (i.e. ["off"], not the original set)."""
    await schedules.execute_remind(
        commands.dispatch("/remind water 08:00 12:00", DEFAULT_REGISTRY),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await schedules.execute_remind(
        commands.dispatch("/remind water off", DEFAULT_REGISTRY),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await schedules.execute_remind(
        commands.dispatch("/remind water default", DEFAULT_REGISTRY),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )

    rows = list(reversed(_rows(db)))
    assert [r["action"] for r in rows] == ["remind_set", "remind_off", "remind_default"]

    assert json.loads(rows[0]["old_value"]) == []
    assert json.loads(rows[0]["new_value"]) == ["08:00", "12:00"]

    assert json.loads(rows[1]["old_value"]) == ["08:00", "12:00"]  # off: old = the just-set times
    assert rows[1]["new_value"] == "off"

    # default: old = whatever was stored right before this write. The prior
    # write stored the literal ["off"] sentinel, so that -- not the original
    # ["08:00","12:00"] -- is the true immediate prior state.
    assert json.loads(rows[2]["old_value"]) == ["off"]
    assert rows[2]["new_value"] is None


async def test_lang_th_en_th_chain_old_values_track_true_prior_state(db):
    """th -> en -> th. Each row's old_value is the immediately-preceding
    stored pref, not the original."""
    await preferences.execute_lang(commands.dispatch("/lang th", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await preferences.execute_lang(commands.dispatch("/lang en", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await preferences.execute_lang(commands.dispatch("/lang th", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)

    rows = list(reversed(_rows(db)))
    assert [r["action"] for r in rows] == ["lang_set", "lang_set", "lang_set"]
    assert rows[0]["old_value"] == "auto"
    assert rows[0]["new_value"] == "th"
    assert rows[1]["old_value"] == "th"
    assert rows[1]["new_value"] == "en"
    assert rows[2]["old_value"] == "en"
    assert rows[2]["new_value"] == "th"


async def test_quiet_set_off_set_chain_old_values_track_true_prior_state(db):
    """set -> off -> set (a different window). old_value on the second set
    must be the empty-list state left by 'off', not the first window."""
    await preferences.execute_quiet(commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await preferences.execute_quiet(commands.dispatch("/quiet off", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await preferences.execute_quiet(commands.dispatch("/quiet 23:00-06:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)

    rows = list(reversed(_rows(db)))
    assert [r["action"] for r in rows] == ["quiet_set", "quiet_off", "quiet_set"]
    assert rows[0]["old_value"] is None  # never-set before -> NULL (inherit), not "[]"
    assert json.loads(rows[0]["new_value"]) == [["22:00", "07:00"]]
    assert json.loads(rows[1]["old_value"]) == [["22:00", "07:00"]]  # off: old = the just-set window
    assert json.loads(rows[1]["new_value"]) == []
    assert json.loads(rows[2]["old_value"]) == []  # after 'off', prior state is []
    assert json.loads(rows[2]["new_value"]) == [["23:00", "06:00"]]


# ===========================================================================
# 2. Record-only-on-success: PRIMARY write fails -> no audit row (distinct
# from Luna's fail-open tests, which force the AUDIT write to fail).
# ===========================================================================


async def test_target_set_no_audit_row_when_primary_write_fails(db, config, monkeypatch):
    monkeypatch.setattr(db, "set_target", _boom)
    command = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
    reply = await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert "2000" not in reply  # target_save_failed, not a success reply
    assert _rows(db) == []


async def test_target_clear_no_audit_row_when_primary_write_fails(db, config, monkeypatch):
    db.set_target(OWNER, "water", 3000.0)
    monkeypatch.setattr(db, "clear_target", _boom)
    command = commands.dispatch("/target water default", DEFAULT_REGISTRY)
    await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []
    assert db.get_target(OWNER, "water") == 3000.0  # write never landed either


async def test_remind_set_no_audit_row_when_primary_write_fails(db, config, monkeypatch):
    monkeypatch.setattr(db, "set_reminder_times", _boom)
    command = commands.dispatch("/remind water 08:00", DEFAULT_REGISTRY)
    reply = await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert "08:00" not in reply
    assert _rows(db) == []


async def test_remind_off_no_audit_row_when_primary_write_fails(db, config, monkeypatch):
    db.set_reminder_times(OWNER, "water", ["08:00"])
    monkeypatch.setattr(db, "set_reminder_times", _boom)
    command = commands.dispatch("/remind water off", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []


async def test_remind_default_no_audit_row_when_primary_write_fails(db, config, monkeypatch):
    db.set_reminder_times(OWNER, "water", ["08:00"])
    monkeypatch.setattr(db, "clear_reminder_times", _boom)
    command = commands.dispatch("/remind water default", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []


async def test_lang_set_no_audit_row_when_primary_write_fails(db, monkeypatch):
    monkeypatch.setattr(db, "set_user_language", _boom)
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    reply = await preferences.execute_lang(command, db=db, lang="en", user_id=OWNER)
    assert reply  # preferences_save_failed, still a reply
    assert db.get_user(OWNER)["language_pref"] != "th"
    assert _rows(db) == []


async def test_quiet_set_no_audit_row_when_primary_write_fails(db, monkeypatch):
    monkeypatch.setattr(db, "set_user_quiet_hours", _boom)
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await preferences.execute_quiet(command, db=db, lang="en", user_id=OWNER)
    assert _rows(db) == []


async def test_quiet_off_no_audit_row_when_primary_write_fails(db, monkeypatch):
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await preferences.execute_quiet(command, db=db, lang="en", user_id=OWNER)
    monkeypatch.setattr(db, "set_user_quiet_hours", _boom)
    off_command = commands.dispatch("/quiet off", DEFAULT_REGISTRY)
    await preferences.execute_quiet(off_command, db=db, lang="en", user_id=OWNER)
    assert len(_rows(db)) == 1  # only the first (successful) quiet_set, not a second row for the failed off


async def test_admin_approve_no_audit_row_when_primary_write_fails(db, config, channel, monkeypatch):
    db.upsert_user(MEMBER, status="pending")
    monkeypatch.setattr(db, "upsert_user", _boom)
    command = Command(kind="approve", target_chat=MEMBER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    assert _rows(db) == []


async def test_admin_block_no_audit_row_when_primary_write_fails(db, config, channel, monkeypatch):
    monkeypatch.setattr(db, "upsert_user", _boom)
    command = Command(kind="block", target_chat=MEMBER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    assert _rows(db) == []


async def test_handle_gate_unknown_no_audit_row_when_primary_write_fails(db, config, channel, monkeypatch):
    monkeypatch.setattr(db, "upsert_user", _boom)
    # handle_gate's own try/except around upsert_user+record must swallow
    # this and still send the pending reply -- see access.py's docstring.
    result = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Name", "hello", lang="en")
    assert result is False
    assert _rows(db) == []
    assert channel.sent  # the asker still gets a reply despite the write failure


async def test_undo_primary_soft_delete_failure_writes_no_audit_row(db, config, monkeypatch):
    """undo_ui.send_undo_confirmation has no try/except around db.soft_delete
    itself (unlike the other four modules) -- a forced failure propagates.
    Regardless of whether it propagates or not, the load-bearing property
    holds: no audit row can exist for a write that never completed, since
    record() is only reached AFTER the soft_delete call returns."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml"))
    row = db.last_log(OWNER)
    monkeypatch.setattr(db, "soft_delete", _boom)
    with pytest.raises(RuntimeError):
        await undo_ui.send_undo_confirmation(
            db, FakeChannel(), config, lambda: datetime(2026, 8, 22, 9, 5), DEFAULT_REGISTRY,
            "en", row,
        )
    assert _rows(db) == []


# ===========================================================================
# 3. Fail-open at capture sites Luna's suite didn't force-fail.
# ===========================================================================


async def test_admin_block_fail_open_when_recorder_raises(db, config, channel, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", _boom)
    command = Command(kind="block", target_chat=MEMBER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    assert db.get_user(MEMBER)["status"] == "blocked"  # the block itself still landed
    assert channel.sent  # confirmation still sent
    assert _rows(db) == []


async def test_handle_gate_unknown_fail_open_when_recorder_raises(db, config, channel, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", _boom)
    result = await access.handle_gate(db, channel, config, OWNER, STRANGER, "Name", "hello", lang="en")
    assert result is False
    assert db.get_user(STRANGER)["status"] == "pending"  # the pending row still landed
    assert channel.sent
    assert _rows(db) == []


# ===========================================================================
# 4. Cross-user attribution under interleaved actions.
# ===========================================================================


async def test_interleaved_target_sets_from_two_users_attribute_and_scope_correctly(db, config):
    """OWNER and MEMBER set independent targets for the same habit,
    interleaved. Each row's actor and old->new must reflect only that
    user's own history -- SPEC-v1.2.md R-D2's per-user scoping, now also
    verified through the audit trail."""
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=1000.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=4000.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=MEMBER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=1200.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=4500.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=MEMBER,
    )

    rows = list(reversed(_rows(db)))
    assert [(r["user_id"], r["old_value"], r["new_value"]) for r in rows] == [
        (OWNER, f"{config.reminders.water.goal_ml:g}", "1000"),
        (MEMBER, f"{config.reminders.water.goal_ml:g}", "4000"),  # MEMBER's own old, unaffected by OWNER's 1000
        (OWNER, "1000", "1200"),  # OWNER's own old, unaffected by MEMBER's 4000
        (MEMBER, "4000", "4500"),
    ]


async def test_admin_approve_two_different_targets_near_simultaneously_attribute_correctly(db, config, channel):
    """Owner approves two different pending chats back-to-back. Both rows
    must have actor=OWNER but the correct, distinct target_user_id each."""
    db.upsert_user(MEMBER, status="pending")
    db.upsert_user(MEMBER2, status="pending")
    await access.execute_admin(
        Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    await access.execute_admin(
        Command(kind="approve", target_chat=MEMBER2),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    rows = list(reversed(_rows(db)))
    assert [(r["user_id"], r["target_user_id"]) for r in rows] == [(OWNER, MEMBER), (OWNER, MEMBER2)]


# ===========================================================================
# 5. Vocabulary conformance -- every action/source this module can write is
# in audit.ACTIONS/SOURCES, and every action has an audit_view label.
# ===========================================================================


async def test_every_module_owned_action_and_source_is_in_the_closed_vocabulary_and_has_a_viewer_label(
    db, config, channel
):
    """Exercises one instance of every action this module (audit-capture)
    can record, then asserts (a) every recorded action/source is a member
    of audit.ACTIONS/audit.SOURCES (no drift/typo at any capture site) and
    (b) audit_view has a real (non-fallback) label for each -- cross-checked
    against core/audit_view.py's own _ACTION_LABEL_MSG_IDS, per the
    dispatch's "a captured action with no viewer label is a FAIL finding"."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml"))
    row = db.last_log(OWNER)
    await undo_ui.send_undo_confirmation(
        db, FakeChannel(), config, lambda: datetime(2026, 8, 22, 9, 5), DEFAULT_REGISTRY, "en", row,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=2000.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await targets_command.execute_target(
        Command(kind="target", target_action="clear", category="water"),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await schedules.execute_remind(
        commands.dispatch("/remind water 08:00", DEFAULT_REGISTRY),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await schedules.execute_remind(
        commands.dispatch("/remind water off", DEFAULT_REGISTRY),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await schedules.execute_remind(
        commands.dispatch("/remind water default", DEFAULT_REGISTRY),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
    )
    await preferences.execute_lang(commands.dispatch("/lang th", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await preferences.execute_quiet(commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await preferences.execute_quiet(commands.dispatch("/quiet off", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    db.upsert_user(MEMBER, status="pending")
    await access.execute_admin(
        Command(kind="approve", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    await access.execute_admin(
        Command(kind="block", target_chat=MEMBER),
        db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en",
    )
    await access.handle_gate(db, channel, config, OWNER, STRANGER, "Name", "hi", lang="en")

    rows = _rows(db)
    seen_actions = {r["action"] for r in rows}
    seen_sources = {r["source"] for r in rows}

    expected_actions = {
        "undo", "target_set", "target_clear", "remind_set", "remind_off", "remind_default",
        "lang_set", "quiet_set", "quiet_off", "user_approve", "user_block", "user_pending",
    }
    assert seen_actions == expected_actions
    assert seen_actions <= set(audit.ACTIONS)
    assert seen_sources <= set(audit.SOURCES)

    for action in seen_actions:
        assert action in audit_view._ACTION_LABEL_MSG_IDS, (
            f"action {action!r} recorded by a capture site has no audit_view label mapping "
            "-- viewer would silently fall back to the raw string"
        )
        # And the label must actually resolve through the i18n catalog in
        # both languages, not just be a dict key.
        from habit_assistant.core import i18n
        msg_id = audit_view._ACTION_LABEL_MSG_IDS[action]
        assert i18n.t(msg_id, "en")
        assert i18n.t(msg_id, "th")


async def test_target_set_full_nl_source_is_a_valid_vocabulary_member(db, config):
    await targets_command.execute_target(
        Command(kind="target", target_action="set", category="water", value_num=2500.0),
        db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, source="nl",
    )
    entry = _rows(db)[0]
    assert entry["source"] == "nl"
    assert entry["source"] in audit.SOURCES


# ===========================================================================
# 6. Idempotent / no-op paths.
# ===========================================================================


async def test_undo_button_double_tap_does_not_record_a_second_row(db, config):
    """First tap soft-deletes and records; the second tap on the same
    (now-deleted) log must hit the already_undone short-circuit and NOT
    call send_undo_confirmation / record a second row."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml"))
    row = db.last_log(OWNER)
    channel = FakeChannel()
    await undo_ui.handle_undo_callback(
        OWNER, f"undo:{row['id']}", "500ml", "cb-1",
        db=db, channel=channel, config=config,
        clock=lambda: datetime(2026, 8, 22, 9, 5), registry=DEFAULT_REGISTRY,
    )
    assert len(_rows(db)) == 1

    sent_before = len(channel.sent)
    await undo_ui.handle_undo_callback(
        OWNER, f"undo:{row['id']}", "500ml", "cb-2",
        db=db, channel=channel, config=config,
        clock=lambda: datetime(2026, 8, 22, 9, 6), registry=DEFAULT_REGISTRY,
    )
    assert len(_rows(db)) == 1  # still just one row -- the idempotent tap recorded nothing
    assert len(channel.sent) == sent_before + 1  # the already_undone reply was still sent


async def test_admin_reapproving_an_already_active_user_still_records_a_row_by_design(db, config, channel):
    """Documents actual behavior (not a bug): execute_admin's approve branch
    always writes+records after a successful db.upsert_user call -- it does
    not compare old vs new status first. Re-approving an already-active user
    therefore DOES produce a user_approve row with old==new=="active". This
    is consistent with SPEC-v1.3.md R-C1 ("each execute path calls record()
    immediately after its successful DB write") and AC-C7's carve-out, which
    only exempts READ-ONLY commands, not idempotent state-changing ones.
    Flagged in TEST-v1.3-capture.md as a documented design note, not a FAIL."""
    command = Command(kind="approve", target_chat=MEMBER)  # MEMBER is already active (db fixture)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    entry = _rows(db)[0]
    assert entry["action"] == "user_approve"
    assert entry["old_value"] == "active"
    assert entry["new_value"] == "active"


async def test_handle_gate_active_user_never_records_anything(db, config, channel):
    """A no-op path: an already-active chat's message short-circuits
    handle_gate's very first branch (`return True`) before any DB write or
    audit call -- correctly produces zero rows, unlike the admin re-approve
    case above."""
    result = await access.handle_gate(db, channel, config, OWNER, MEMBER, "Name", "hello", lang="en")
    assert result is True
    assert _rows(db) == []
