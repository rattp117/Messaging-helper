"""SPEC-v1.3.md "Audit log", module `audit-capture` (§11): the `record(...)`
calls added at the five execute modules' real DB-write sites --
`core/undo_ui.py`, `core/targets_command.py`, `core/schedules.py`,
`core/preferences.py`, `core/access.py`.

Owned ACs (SPEC-v1.3.md §11): AC-C1 (undo), AC-C3 (target), AC-C4 (remind),
AC-C5 (lang/quiet), AC-C6 (admin), AC-P1 (privacy). Not owned by this file:
AC-C2 (edit, `main.py:_execute_edit` -- integration), AC-C7's read-only-command
half (owned collectively, but this file covers the read-only branches of the
five modules it DOES own).

Same convention as every other v1.2/v1.3 module test file: a real on-disk
SQLite `Database` (tmp_path), no DB mocks, `execute_*` functions called
directly (no `main.py` involved -- capture sites are self-contained per
SPEC-v1.3.md §11's file-ownership table)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import access, audit, commands, preferences, schedules, targets_command, undo_ui
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

OWNER = "1574572064"
MEMBER = "88899900"
STRANGER = "55544433"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "audit_capture.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
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


def _only_row(db: Database):
    rows = _rows(db)
    assert len(rows) == 1, rows
    return rows[0]


# ===========================================================================
# AC-C1: undo -- source=command (text /undo) vs source=button, entity,
# old=removed value, new=NULL, user_id=actor. Recorded inside
# core/undo_ui.send_undo_confirmation (R-C2), shared by both paths.
# ===========================================================================


async def test_undo_text_path_records_command_source(db, config):
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml"))
    row = db.last_log(OWNER)
    await undo_ui.send_undo_confirmation(
        db, FakeChannel(), config, lambda: datetime(2026, 8, 22, 9, 5), DEFAULT_REGISTRY,
        "en", row,
    )
    entry = _only_row(db)
    assert entry["action"] == "undo"
    assert entry["source"] == "command"
    assert entry["entity"] == "water"
    assert entry["old_value"] == "500"
    assert entry["new_value"] is None
    assert entry["user_id"] == OWNER


async def test_undo_button_path_records_button_source(db, config):
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml"))
    row = db.last_log(OWNER)
    await undo_ui.handle_undo_callback(
        OWNER, f"undo:{row['id']}", "500ml", "cb-1",
        db=db, channel=FakeChannel(), config=config,
        clock=lambda: datetime(2026, 8, 22, 9, 5),
        registry=DEFAULT_REGISTRY,
    )
    entry = _only_row(db)
    assert entry["action"] == "undo"
    assert entry["source"] == "button"
    assert entry["entity"] == "water"
    assert entry["old_value"] == "500"


async def test_undo_diary_records_redacted_marker_not_the_text_value(db, config):
    """RULING (integration pass, MAJOR FINDING, TEST-PORTAL-audit.md):
    this test used to pin the OLD (leaky) contract -- `old_value` held
    the raw diary text verbatim, which the admin portal's `/audit` page
    then rendered unredacted for any owner to read (a pre-existing,
    pre-portal capture-site bug the portal only made materially more
    exposed -- see `core/undo_ui.py:_redacted_text_marker`'s own
    docstring for the full account). Flipped to pin the FIXED contract: a
    text habit's undo records a bilingual-neutral marker + a character
    count derived from the actual removed text, never the text itself."""
    db.insert_log(
        LogEntry(None, OWNER, "2026-08-22T09:00:00", "diary", None, "had a good day", "had a good day", habit_type="text")
    )
    row = db.last_log(OWNER)
    await undo_ui.send_undo_confirmation(
        db, FakeChannel(), config, lambda: datetime(2026, 8, 22, 9, 5), DEFAULT_REGISTRY,
        "en", row,
    )
    entry = _only_row(db)
    assert entry["old_value"] == "[text entry removed] (14 chars)"  # len("had a good day") == 14
    assert "had a good day" not in entry["old_value"]


async def test_undo_diary_records_redacted_marker_even_when_habit_type_not_stamped(db, config):
    """Defense-in-depth companion to the test above: `category == "diary"`
    alone (habit_type NULL/unstamped -- a legacy or edge-case write path)
    still redacts, matching `describe_log`'s own explicit `category ==
    "diary"` special-case just below in this file."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "diary", None, "had a good day", "had a good day"))
    row = db.last_log(OWNER)
    assert row["habit_type"] is None  # this test's own precondition
    await undo_ui.send_undo_confirmation(
        db, FakeChannel(), config, lambda: datetime(2026, 8, 22, 9, 5), DEFAULT_REGISTRY,
        "en", row,
    )
    entry = _only_row(db)
    assert entry["old_value"] == "[text entry removed] (14 chars)"


async def test_undo_fail_open_when_recorder_raises(db, config, monkeypatch):
    """AC-A2/R-C5 applied at this specific capture site: forcing the
    underlying DB write that `audit.record` uses to raise must not affect
    the undo action's own reply or DB write."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-22T09:00:00", "water", 500.0, None, "500ml"))
    row = db.last_log(OWNER)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated audit DB failure")

    monkeypatch.setattr(db, "insert_audit", _boom)
    channel = FakeChannel()
    await undo_ui.send_undo_confirmation(
        db, channel, config, lambda: datetime(2026, 8, 22, 9, 5), DEFAULT_REGISTRY,
        "en", row,
    )
    # The undo itself still happened and the confirmation was still sent --
    # no exception escaped, no audit row exists (the forced failure), but
    # the user's own action is completely unaffected.
    assert channel.sent, "confirmation reply must still be sent"
    assert db.get_log(row["id"])["deleted_at"] is not None
    assert _rows(db) == []


# ===========================================================================
# AC-C3: target -- set (source=command/nl), clear.
# ===========================================================================


async def test_target_set_records_command_source_with_prev_and_new_goal(db, config):
    command = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
    reply = await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert "2000" in reply
    entry = _only_row(db)
    assert entry["action"] == "target_set"
    assert entry["source"] == "command"
    assert entry["entity"] == "water"
    assert entry["old_value"] == f"{config.reminders.water.goal_ml:g}"  # prev EFFECTIVE goal (config default, no override yet)
    assert entry["new_value"] == "2000"
    assert entry["user_id"] == OWNER


async def test_target_set_full_nl_path_records_nl_source(db, config):
    command = Command(kind="target", target_action="set", category="water", value_num=2500.0)
    await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, source="nl"
    )
    entry = _only_row(db)
    assert entry["action"] == "target_set"
    assert entry["source"] == "nl"
    assert entry["new_value"] == "2500"


async def test_target_clear_records_prev_override_and_null_new(db, config):
    db.set_target(OWNER, "water", 3000.0)
    command = commands.dispatch("/target water default", DEFAULT_REGISTRY)
    await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    entry = _only_row(db)
    assert entry["action"] == "target_clear"
    assert entry["source"] == "command"
    assert entry["entity"] == "water"
    assert entry["old_value"] == "3000"
    assert entry["new_value"] is None


async def test_target_show_does_not_record(db, config):
    command = commands.dispatch("/target water", DEFAULT_REGISTRY)
    await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []


async def test_target_show_all_does_not_record(db, config):
    command = commands.dispatch("/target", DEFAULT_REGISTRY)
    await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []


async def test_target_invalid_value_does_not_record(db, config):
    command = commands.dispatch("/target water -5", DEFAULT_REGISTRY)
    await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []


async def test_target_fail_open_when_recorder_raises(db, config, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    command = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
    reply = await targets_command.execute_target(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert "2000" in reply
    assert db.get_target(OWNER, "water") == 2000.0
    assert _rows(db) == []


# ===========================================================================
# AC-C4: remind -- set/off/default.
# ===========================================================================


async def test_remind_set_records_prev_times_json_and_new_times(db, config):
    command = commands.dispatch("/remind water 08:00 12:00", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    entry = _only_row(db)
    assert entry["action"] == "remind_set"
    assert entry["source"] == "command"
    assert entry["entity"] == "water"
    assert json.loads(entry["old_value"]) == []
    assert json.loads(entry["new_value"]) == ["08:00", "12:00"]


async def test_remind_set_records_prior_custom_times_as_old_value(db, config):
    db.set_reminder_times(OWNER, "water", ["06:00"])
    command = commands.dispatch("/remind water 08:00 12:00", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    entry = _only_row(db)
    assert json.loads(entry["old_value"]) == ["06:00"]


async def test_remind_off_records_prev_times_and_off_literal(db, config):
    db.set_reminder_times(OWNER, "water", ["08:00", "12:00"])
    command = commands.dispatch("/remind water off", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    entry = _only_row(db)
    assert entry["action"] == "remind_off"
    assert json.loads(entry["old_value"]) == ["08:00", "12:00"]
    assert entry["new_value"] == "off"


async def test_remind_default_records_prev_times_and_null_new(db, config):
    db.set_reminder_times(OWNER, "water", ["08:00"])
    command = commands.dispatch("/remind water default", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    entry = _only_row(db)
    assert entry["action"] == "remind_default"
    assert json.loads(entry["old_value"]) == ["08:00"]
    assert entry["new_value"] is None


async def test_remind_show_does_not_record(db, config):
    command = commands.dispatch("/remind water", DEFAULT_REGISTRY)
    await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert _rows(db) == []


async def test_remind_fail_open_when_recorder_raises(db, config, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    command = commands.dispatch("/remind water 08:00", DEFAULT_REGISTRY)
    reply = await schedules.execute_remind(
        command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER
    )
    assert "08:00" in reply
    assert db.get_reminder_times(OWNER, "water") == ["08:00"]
    assert _rows(db) == []


# ===========================================================================
# AC-C5: lang / quiet.
# ===========================================================================


async def test_lang_set_records_prev_and_new_pref(db):
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    await preferences.execute_lang(command, db=db, lang="en", user_id=OWNER)
    entry = _only_row(db)
    assert entry["action"] == "lang_set"
    assert entry["source"] == "command"
    assert entry["entity"] is None
    assert entry["old_value"] == "auto"
    assert entry["new_value"] == "th"


async def test_quiet_set_records_prev_json_and_new_windows(db):
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await preferences.execute_quiet(command, db=db, lang="en", user_id=OWNER)
    entry = _only_row(db)
    assert entry["action"] == "quiet_set"
    assert entry["source"] == "command"
    assert entry["old_value"] is None
    assert json.loads(entry["new_value"]) == [["22:00", "07:00"]]


async def test_quiet_off_records_prev_json_and_empty_list_new(db):
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    await preferences.execute_quiet(command, db=db, lang="en", user_id=OWNER)
    off_command = commands.dispatch("/quiet off", DEFAULT_REGISTRY)
    await preferences.execute_quiet(off_command, db=db, lang="en", user_id=OWNER)
    rows = _rows(db)
    assert len(rows) == 2
    off_entry = rows[0]  # newest first
    assert off_entry["action"] == "quiet_off"
    assert json.loads(off_entry["old_value"]) == [["22:00", "07:00"]]
    assert json.loads(off_entry["new_value"]) == []


async def test_lang_invalid_value_does_not_record(db):
    command = commands.dispatch("/lang thai", DEFAULT_REGISTRY)
    await preferences.execute_lang(command, db=db, lang="en", user_id=OWNER)
    assert _rows(db) == []


async def test_quiet_invalid_window_does_not_record(db):
    command = commands.dispatch("/quiet notatime", DEFAULT_REGISTRY)
    await preferences.execute_quiet(command, db=db, lang="en", user_id=OWNER)
    assert _rows(db) == []


async def test_lang_fail_open_when_recorder_raises(db, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    command = commands.dispatch("/lang th", DEFAULT_REGISTRY)
    reply = await preferences.execute_lang(command, db=db, lang="en", user_id=OWNER)
    assert reply
    assert db.get_user(OWNER)["language_pref"] == "th"
    assert _rows(db) == []


async def test_quiet_fail_open_when_recorder_raises(db, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    command = commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY)
    reply = await preferences.execute_quiet(command, db=db, lang="en", user_id=OWNER)
    assert reply
    assert db.get_user(OWNER)["quiet_hours_json"] is not None
    assert _rows(db) == []


# ===========================================================================
# AC-C6: admin -- approve/block/invite, and handle_gate's unknown->pending.
# ===========================================================================


async def test_admin_approve_records_user_approve_with_target_and_prev_status(db, config, channel):
    db.upsert_user(MEMBER, status="pending")
    command = Command(kind="approve", target_chat=MEMBER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    entry = _only_row(db)
    assert entry["action"] == "user_approve"
    assert entry["source"] == "admin"
    assert entry["user_id"] == OWNER  # actor
    assert entry["target_user_id"] == MEMBER
    assert entry["old_value"] == "pending"
    assert entry["new_value"] == "active"


async def test_admin_invite_is_recorded_as_user_approve(db, config, channel):
    command = Command(kind="invite", target_chat="9999")
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    entry = _only_row(db)
    assert entry["action"] == "user_approve"
    assert entry["old_value"] is None  # no prior row for a never-contacted chat
    assert entry["new_value"] == "active"


async def test_admin_block_records_user_block_with_prev_status(db, config, channel):
    command = Command(kind="block", target_chat=MEMBER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    entry = _only_row(db)
    assert entry["action"] == "user_block"
    assert entry["source"] == "admin"
    assert entry["target_user_id"] == MEMBER
    assert entry["old_value"] == "active"
    assert entry["new_value"] == "blocked"


async def test_admin_approve_by_non_owner_does_not_record(db, config, channel):
    command = Command(kind="approve", target_chat=STRANGER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=MEMBER, lang="en"
    )
    assert _rows(db) == []


async def test_handle_gate_unknown_chat_records_user_pending(db, config, channel):
    await access.handle_gate(db, channel, config, OWNER, STRANGER, "Some Name", "hello there, secret stuff", lang="en")
    entry = _only_row(db)
    assert entry["action"] == "user_pending"
    assert entry["source"] == "admin"
    assert entry["user_id"] == STRANGER  # actor == target (nobody else acted)
    assert entry["target_user_id"] == STRANGER
    assert entry["old_value"] is None
    assert entry["new_value"] == "pending"


async def test_handle_gate_pending_chat_second_message_does_not_record_again(db, config, channel):
    await access.handle_gate(db, channel, config, OWNER, STRANGER, None, "first", lang="en")
    await access.handle_gate(db, channel, config, OWNER, STRANGER, None, "second", lang="en")
    assert len(_rows(db)) == 1  # only the unknown->pending transition, not the repeat


async def test_admin_fail_open_when_recorder_raises(db, config, channel, monkeypatch):
    monkeypatch.setattr(db, "insert_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    command = Command(kind="approve", target_chat=MEMBER)
    await access.execute_admin(
        command, db=db, channel=channel, config=config, owner_chat_id=OWNER, chat_id=OWNER, lang="en"
    )
    assert db.get_user(MEMBER)["status"] == "active"
    assert channel.sent  # the approve confirmation was still sent
    assert _rows(db) == []


# ===========================================================================
# AC-P1: privacy -- a pending/blocked user's message content is never
# stored in the audit row (§2.1's "new=pending" only, never `text`).
# ===========================================================================


async def test_pending_transition_never_stores_message_text(db, config, channel):
    secret_message = "my secret health data 12345"
    await access.handle_gate(db, channel, config, OWNER, STRANGER, "A Name", secret_message, lang="en")
    entry = _only_row(db)
    for value in (entry["old_value"], entry["new_value"], entry["entity"]):
        assert value != secret_message
        if value is not None:
            assert secret_message not in value
    assert entry["new_value"] == "pending"
    # Corroborates R-C4's existing v1.2 guarantee this row must not weaken:
    # no `logs` row was written for the pending chat either.
    assert db.last_log(STRANGER) is None
