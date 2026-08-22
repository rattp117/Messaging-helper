"""SPEC-v1.3.md "Audit log" (§4 "Storage & recorder (shared surface)") --
shared-surface unit tests for `core/audit.py`'s `record`/`ACTIONS`/
`SOURCES`, built ahead of the two parallel modules (`audit-capture`,
`audit-view`) that call `record(...)` directly. This file pins the
recorder's own contract in isolation, against a real on-disk SQLite DB
(no mocks -- same convention as `tests/test_migrations.py`/
`tests/test_core_targets.py`).

Owns AC-A2 (fail-open) per SPEC-v1.3.md §11's own ownership table.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import audit
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import AuditEntry

OWNER = "owner-chat-id"
MEMBER = "member-chat-id"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "audit.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# ACTIONS/SOURCES: the closed vocabulary SPEC-v1.3.md §5 defines verbatim --
# a capture site importing a typo'd action string should fail fast (an
# AttributeError/ImportError on a mistyped constant name), not silently
# write a wrong value; the actual value spelling is fixed here so it can
# never drift between a capture site and `audit_view`'s own label lookup.
# ---------------------------------------------------------------------------


def test_actions_matches_the_spec_vocabulary_exactly():
    assert set(audit.ACTIONS) == {
        "undo", "edit", "target_set", "target_clear", "remind_set", "remind_off",
        "remind_default", "lang_set", "quiet_set", "quiet_off",
        "user_approve", "user_block", "user_pending",
    }


def test_sources_matches_the_spec_vocabulary_exactly():
    assert set(audit.SOURCES) == {"command", "nl", "button", "admin"}


# ---------------------------------------------------------------------------
# record() happy path -- value shaping (R-W1), row content, ordering.
# ---------------------------------------------------------------------------


def test_record_writes_a_row_with_every_field_populated(db):
    audit.record(
        db,
        actor=OWNER,
        action="target_set",
        source="command",
        entity="water",
        old_value=2500.0,
        new_value=2000.0,
    )
    rows = db.recent_audit(10)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == OWNER
    assert row["action"] == "target_set"
    assert row["entity"] == "water"
    assert row["old_value"] == "2500"
    assert row["new_value"] == "2000"
    assert row["source"] == "command"
    assert row["target_user_id"] is None


def test_record_admin_action_carries_target_user_id(db):
    audit.record(
        db,
        actor=OWNER,
        action="user_approve",
        source="admin",
        old_value="pending",
        new_value="active",
        target_user_id=MEMBER,
    )
    row = db.recent_audit(1)[0]
    assert row["user_id"] == OWNER  # actor
    assert row["target_user_id"] == MEMBER  # who it was done TO
    assert row["entity"] is None
    assert row["old_value"] == "pending"
    assert row["new_value"] == "active"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (2500.0, "2500"),
        (2500, "2500"),
        (0.5, "0.5"),
        (["08:00", "12:00"], '["08:00", "12:00"]'),
        ({"a": 1}, '{"a": 1}'),
        ("th", "th"),
        ("active", "active"),
    ],
)
def test_record_stringifies_every_value_shape_per_rw1(db, value, expected):
    audit.record(db, actor=OWNER, action="lang_set", source="command", old_value=value, new_value=None)
    row = db.recent_audit(1)[0]
    assert row["old_value"] == expected


def test_record_list_value_round_trips_through_json():
    """R-W1: a list old/new value must be readable straight back via
    json.loads by a future viewer -- the exact shape remind times are
    already stored/read as elsewhere in this codebase."""
    from habit_assistant.core import audit as audit_module

    stringified = audit_module._stringify_value(["08:00", "12:00", "18:00"])
    assert json.loads(stringified) == ["08:00", "12:00", "18:00"]


def test_record_uses_the_injected_clock(db):
    fixed = datetime(2026, 8, 22, 14, 3, 0)
    audit.record(db, actor=OWNER, action="undo", source="command", entity="water", clock=lambda: fixed)
    row = db.recent_audit(1)[0]
    assert row["ts"] == "2026-08-22T14:03:00"


def test_record_defaults_clock_to_real_now_when_not_given(db):
    before = datetime.now()
    audit.record(db, actor=OWNER, action="undo", source="command")
    after = datetime.now()
    row = db.recent_audit(1)[0]
    ts = datetime.fromisoformat(row["ts"])
    assert before.replace(microsecond=0) <= ts <= after


def test_multiple_records_are_newest_first_via_recent_audit(db):
    audit.record(db, actor=OWNER, action="undo", source="command", entity="water")
    audit.record(db, actor=OWNER, action="edit", source="command", entity="stretch")
    audit.record(db, actor=OWNER, action="lang_set", source="command")
    rows = db.recent_audit(10)
    assert [r["action"] for r in rows] == ["lang_set", "edit", "undo"]


# ---------------------------------------------------------------------------
# AC-A2 (fail-open, R-W2) -- the load-bearing safety rule. `record` must
# NEVER raise, regardless of what fails: the DB write itself, or (more
# subtly) the value-stringification step that runs before it.
# ---------------------------------------------------------------------------


class _RaisingDatabase:
    """A minimal stand-in whose `insert_audit` always raises -- proves
    `record` swallows a DB-layer failure (table missing / DB locked /
    disk full, all surface as *some* exception from insert_audit) without
    ever propagating it to the caller."""

    def insert_audit(self, entry):
        raise RuntimeError("simulated DB failure (table missing / locked)")


def test_record_fail_open_when_insert_audit_raises():
    # Must not raise -- this call completing at all IS the assertion.
    audit.record(_RaisingDatabase(), actor=OWNER, action="undo", source="command", entity="water", old_value=500.0)


def test_record_fail_open_logs_the_exception(caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.audit"):
        audit.record(_RaisingDatabase(), actor=OWNER, action="undo", source="button")
    assert any("fail-open" in rec.message.lower() for rec in caplog.records)


def test_record_fail_open_when_value_stringification_itself_raises(db):
    """A pathological caller-supplied value whose __str__ raises (e.g. a
    custom object with a broken __str__) must still not escape record --
    the ENTIRE body is one try, not just the db.insert_audit call, per
    this module's own documented "structurally hard to misuse" design."""

    class _BrokenStr:
        def __str__(self):
            raise ValueError("boom")

    audit.record(db, actor=OWNER, action="lang_set", source="command", old_value=_BrokenStr())
    # No row was written (the failure happened before the insert), and no
    # exception escaped -- the user's own action is completely unaffected.
    assert db.recent_audit(10) == []


def test_record_never_raises_across_every_action_and_source_combination(db):
    """Breadth pass: every documented action/source pair, through the
    real (working) db this time -- confirms the happy path doesn't
    accidentally raise for any combination R-W1's vocabulary allows."""
    for action in audit.ACTIONS:
        for source in audit.SOURCES:
            audit.record(db, actor=OWNER, action=action, source=source, entity="water", old_value=1, new_value=2)
    assert len(db.recent_audit(1000)) == len(audit.ACTIONS) * len(audit.SOURCES)


# ---------------------------------------------------------------------------
# AC-R1 (retention): the startup prune call this pass wired into
# `main.py:async_main`, exercised through the real function (not
# re-implemented) -- mirrors tests/test_v11_integration.py's own
# `_run_async_main`/`_StopAfterSchedulerStart` pattern, trimmed to the
# minimum this test actually needs (no scheduler-job/channel-message
# assertions -- those belong to other files' own scopes).
# ---------------------------------------------------------------------------


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


class _FakeChannel(Channel):
    def __init__(self, *args, **kwargs):
        pass

    async def send(self, chat_id, text):
        pass

    async def set_my_commands(self, commands):
        pass

    async def run(self, on_message, on_callback=None):
        raise _StopAfterSchedulerStart()

    async def aclose(self):
        pass


class _FakeOllamaClient:
    def __init__(self, *args, **kwargs):
        pass

    async def probe_schema_support(self, *args, **kwargs):
        return {}

    async def aclose(self):
        pass


async def _run_async_main_against(tmp_path, monkeypatch, config):
    from habit_assistant import main as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        main_module, "load_secrets", lambda: SimpleNamespace(telegram_bot_token="fake", telegram_chat_id=OWNER)
    )
    monkeypatch.setattr(main_module, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(main_module, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(main_module, "OllamaClient", _FakeOllamaClient)
    args = SimpleNamespace(seed=False, dry_run=None, test_reminder=None)
    with pytest.raises(_StopAfterSchedulerStart):
        await main_module.async_main(args)


async def test_startup_prunes_audit_rows_older_than_retention_days(tmp_path, monkeypatch):
    db_path = tmp_path / "prune.db"
    seed_db = Database(db_path)
    old_ts = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    recent_ts = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    seed_db.insert_audit(AuditEntry(None, old_ts, OWNER, "undo", "water", "500", None, "command"))
    seed_db.insert_audit(AuditEntry(None, recent_ts, OWNER, "lang_set", None, "auto", "th", "command"))
    seed_db.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}, "audit": {"retention_days": 365}})
    await _run_async_main_against(tmp_path, monkeypatch, config)

    db = Database(db_path)
    try:
        remaining = db.recent_audit(10)
        assert len(remaining) == 1
        assert remaining[0]["action"] == "lang_set"  # only the recent row survives
    finally:
        db.close()


async def test_startup_prunes_nothing_when_retention_days_is_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "no_prune.db"
    seed_db = Database(db_path)
    ancient_ts = (datetime.now() - timedelta(days=3650)).isoformat(timespec="seconds")
    seed_db.insert_audit(AuditEntry(None, ancient_ts, OWNER, "undo", "water", "500", None, "command"))
    seed_db.close()

    config = Config.model_validate({"app": {"db_path": str(db_path)}, "audit": {"retention_days": 0}})
    await _run_async_main_against(tmp_path, monkeypatch, config)

    db = Database(db_path)
    try:
        assert len(db.recent_audit(10)) == 1  # nothing pruned, no matter how old
    finally:
        db.close()
