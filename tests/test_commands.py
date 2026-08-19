"""Command layer / edit-undo tests (ROADMAP.md v0.5.0 "Command Layer &
Edit/Undo", AC5.1-AC5.5), plus migration 003 (soft-delete `deleted_at`
column) which this version's undo/edit is built on.

All against real on-disk SQLite files (tmp_path) -- no mocks for the DB,
since sqlite3 is cheap, reliable, local state. The LLM is either never
constructed (a `_NeverCalledLLM` stand-in that raises if touched -- proves
the command path is LLM-free, per IMPL.md) or monkeypatched at
`habit_assistant.main.parse_message` (same pattern as test_confirmations.py)
when a test needs to prove a message *did* reach the parser and count how
many times.

Companion to test_db.py (schema/aggregation), test_migrations.py (migration
runner contract), test_resilience.py (health-monitor-aware deferral) --
this file covers only the v0.5.0 additions.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.review import compute_weekly_stats
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import (
    DEFERRED_ACK_MESSAGE,
    NOTHING_TO_EDIT_MESSAGE,
    NOTHING_TO_UNDO_MESSAGE,
    handle_inbound_message,
)
from habit_assistant.storage.db import Database
from habit_assistant.storage.migrations import MIGRATIONS, current_version, run_migrations
from habit_assistant.storage.models import LogEntry

GLASS_ML = 250
BOTTLE_ML = 600


# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message) -> None:
        raise NotImplementedError("not exercised in these tests")


class _NeverCalledLLM:
    """Proves a command path never touches the LLM at all (IMPL.md: "no LLM
    call anywhere in the new code path")."""

    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a recognized command")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a recognized command")


class _FrozenHealthMonitor:
    """Minimal `health_monitor` stand-in exposing only `.ollama_up`, so a
    test can force the "Ollama is DOWN" branch without a real HealthMonitor
    (mirrors test_resilience.py's fixture of the same name)."""

    def __init__(self, ollama_up: bool):
        self.ollama_up = ollama_up


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def fixed_clock():
    def clock():
        return datetime(2026, 8, 19, 14, 30, 0)

    return clock


def patch_parse_message(monkeypatch, result: ExtractionResult):
    async def fake_parse_message(text, llm, glass_ml, bottle_ml, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


def _seed(db: Database, ts: str, category: str, value_num=None, value_text=None, raw: str | None = None) -> int:
    return db.insert_log(LogEntry(None, ts, category, value_num, value_text, raw or ts, "reply"))


def _raw_row_count(db: Database) -> int:
    return db._conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]


# ===========================================================================
# AC5.1 -- undo removes the most recent non-deleted log, confirms what was
# removed, running totals reflect the removal; second undo removes the
# next-most-recent.
# ===========================================================================


@pytest.mark.parametrize("undo_phrase", ["/undo", "undo last", "ยกเลิกอันล่าสุด"])
async def test_undo_phrasing_soft_deletes_most_recent_log(db, fixed_clock, undo_phrase):
    """CHANGED (ROADMAP.md v0.6.0 AC6.1/AC6.3): the Thai-triggered case
    ("ยกเลิกอันล่าสุด") now gets a Thai confirmation, whose wording has no
    English "Today" marker to split on -- the old `.split("Today")[0]`
    trick to isolate the "what was removed" clause doesn't generalize
    across languages. Replaced with a language-agnostic check: the
    removed-item description (built from the same i18n catalog entry the
    production code itself resolves to) must appear verbatim in the
    confirmation -- that alone proves the 300ml row, not the 500ml one,
    was the one named, in either language."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")

    await handle_inbound_message(
        undo_phrase, db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    lang = i18n.detect_language(undo_phrase)
    removed_description = i18n.t("describe_log_water", lang, value_num=300.0)

    # Confirmation names what was removed (the most recent: 300 ml), not
    # the older 500ml row.
    assert len(channel.sent) == 1
    assert removed_description in channel.sent[0]

    # Running total reflects the removal: only the 500ml row remains.
    assert db.water_total_ml("2026-08-19") == 500.0

    # Raw check: the deleted row is still physically present, marked deleted.
    assert _raw_row_count(db) == 2
    deleted = db._conn.execute(
        "SELECT deleted_at FROM logs WHERE raw_message = '300ml'"
    ).fetchone()
    assert deleted["deleted_at"] is not None


async def test_second_undo_removes_the_next_most_recent_log(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")

    await handle_inbound_message("/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)
    await handle_inbound_message("/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert "300" in channel.sent[0]
    assert "500" in channel.sent[1]
    assert db.water_total_ml("2026-08-19") == 0.0
    assert _raw_row_count(db) == 2  # both rows still physically present


async def test_undo_confirmation_names_stretch_entry_and_updates_count(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "stretch", 10.0, raw="10 min stretch")
    _seed(db, "2026-08-19T10:00:00", "stretch", 5.0, raw="5 min stretch")
    assert db.stretch_count("2026-08-19") == 2

    await handle_inbound_message("undo last", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert "5" in channel.sent[0]
    assert "stretch" in channel.sent[0]
    assert db.stretch_count("2026-08-19") == 1


async def test_undo_confirmation_names_diary_entry(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T21:00:00", "diary", value_text="a quiet reflective day", raw="a quiet reflective day")

    await handle_inbound_message("/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert "quiet reflective day" in channel.sent[0]
    assert db.diary_count("2026-08-19") == 0


# ===========================================================================
# AC5.2 -- undo with no prior entry / all-deleted history -> friendly
# message, zero writes.
# ===========================================================================


async def test_undo_on_empty_history_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    channel = FakeChannel()

    await handle_inbound_message(
        "/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [NOTHING_TO_UNDO_MESSAGE]
    assert _raw_row_count(db) == 0


async def test_undo_on_all_deleted_history_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")

    # First undo removes the only entry.
    await handle_inbound_message("/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)
    row_count_after_first_undo = _raw_row_count(db)

    # Second undo: nothing non-deleted left.
    await handle_inbound_message("/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert channel.sent[-1] == NOTHING_TO_UNDO_MESSAGE
    assert _raw_row_count(db) == row_count_after_first_undo  # no new row written


# ===========================================================================
# AC5.3 -- edit updates the last matching entry's value in place and
# re-confirms the new daily total; edit with no matching entry is handled
# gracefully.
# ===========================================================================


@pytest.mark.parametrize(
    "edit_phrase",
    ["make that 300ml", "แก้เป็น 300 มล."],
)
async def test_edit_updates_last_matching_water_entry_and_reconfirms_total(db, fixed_clock, edit_phrase):
    """CHANGED (ROADMAP.md v0.6.0 AC6.1/AC6.3): the Thai-triggered case
    ("แก้เป็น 300 มล.") now gets a Thai confirmation instead of the single
    hardcoded English string this test asserted pre-v0.6.0
    (f"✏️ Updated to 300 ml — today {total} / 2500 ml (12%)"). Expected
    text is now built per-phrase from the same catalog id/kwargs the
    production code resolves, so both languages are covered by one
    parametrized assertion."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")

    await handle_inbound_message(
        edit_phrase, db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    row = db.last_log(category="water")
    assert row["value_num"] == 300.0
    assert _raw_row_count(db) == 1  # updated in place, not a new row

    total = db.water_total_ml("2026-08-19")
    assert total == 300.0
    lang = i18n.detect_language(edit_phrase)
    expected = i18n.t("edit_updated_water", lang, value_num=300.0, total=int(total), goal=2500, pct=12)
    assert channel.sent == [expected]


async def test_edit_updates_only_the_last_matching_entry_not_earlier_ones(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    _seed(db, "2026-08-19T10:00:00", "water", 200.0, raw="200ml")

    await handle_inbound_message(
        "make that 350ml", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    values = [
        r[0]
        for r in db._conn.execute("SELECT value_num FROM logs ORDER BY id").fetchall()
    ]
    assert values == [500.0, 350.0]  # only the more recent row changed


async def test_edit_updates_last_matching_stretch_entry(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "stretch", 10.0, raw="10 min stretch")

    await handle_inbound_message(
        "change it to 15 min", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    row = db.last_log(category="stretch")
    assert row["value_num"] == 15.0
    assert channel.sent == ["✏️ Updated to 15 min stretch — 1st today"]


async def test_edit_with_no_matching_category_entry_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    """A water-shaped edit ('300ml') with only a stretch entry on file has
    no matching-category target to update -- must not silently create a
    row or touch the unrelated stretch entry."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "stretch", 10.0, raw="10 min stretch")

    await handle_inbound_message(
        "make that 300ml", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert channel.sent == [NOTHING_TO_EDIT_MESSAGE]
    assert _raw_row_count(db) == 1
    row = db._conn.execute("SELECT value_num FROM logs WHERE category = 'stretch'").fetchone()
    assert row["value_num"] == 10.0  # untouched


async def test_edit_on_empty_history_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    channel = FakeChannel()

    await handle_inbound_message(
        "edit that to 15 min", db=db, llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [NOTHING_TO_EDIT_MESSAGE]
    assert _raw_row_count(db) == 0


# ===========================================================================
# AC5.4 -- a soft-deleted row stays in the table (raw SQL) but is excluded
# from every aggregation: water totals, stretch count/ordinal, diary count,
# weekly review stats, pending_unparsed.
# ===========================================================================


def test_soft_deleted_row_remains_in_table_but_excluded_from_water_total(db):
    log_id = _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    db.soft_delete(log_id)

    raw = db._conn.execute("SELECT id, deleted_at FROM logs WHERE id = ?", (log_id,)).fetchone()
    assert raw is not None
    assert raw["deleted_at"] is not None

    assert db.water_total_ml("2026-08-19") == 0.0


async def test_soft_deleted_row_excluded_from_stretch_count_and_ordinal(db, fixed_clock, monkeypatch):
    channel = FakeChannel()
    config = Config()
    old_id = _seed(db, "2026-08-19T09:00:00", "stretch", 10.0, raw="10 min stretch")
    db.soft_delete(old_id)
    assert db.stretch_count("2026-08-19") == 0

    patch_parse_message(monkeypatch, ExtractionResult("stretch", None, 5, None, 0.9))
    await handle_inbound_message(
        "5 min stretch", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    # Ordinal counts from 1, ignoring the soft-deleted earlier session.
    assert channel.sent == ["✅ 5 min stretch logged — 1st today"]


def test_soft_deleted_row_excluded_from_diary_count(db):
    log_id = _seed(db, "2026-08-19T21:00:00", "diary", value_text="a day", raw="a day")
    db.soft_delete(log_id)

    assert db.diary_count("2026-08-19") == 0


def test_soft_deleted_row_excluded_from_pending_unparsed(db):
    log_id = _seed(db, "2026-08-19T09:00:00", "unparsed", raw="500ml please")
    db.soft_delete(log_id)

    assert db.pending_unparsed() == []


def test_soft_deleted_rows_excluded_from_weekly_review_stats(db):
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    deleted_id = _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")
    stretch_id = _seed(db, "2026-08-19T11:00:00", "stretch", 10.0, raw="10 min")
    db.soft_delete(deleted_id)
    db.soft_delete(stretch_id)

    stats = compute_weekly_stats(db, config, date(2026, 8, 19))

    assert stats.water_total_ml == 500.0
    assert stats.stretch_total == 0


def test_soft_delete_never_issues_a_hard_delete(db):
    """Raw-SQL audit: after soft_delete, the row count in `logs` must be
    unchanged (no DELETE statement executed)."""
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")
    before = _raw_row_count(db)

    row = db.last_log()
    db.soft_delete(row["id"])

    assert _raw_row_count(db) == before


# ===========================================================================
# AC5.5 -- an adversarial false-positive corpus: normal logs that look
# commandish must still reach the parser. dispatch() returns None AND
# parse_message is called exactly once for each.
# ===========================================================================

ADVERSARIAL_MESSAGES = [
    "ดื่มน้ำ 2 แก้ว",  # contains the edit-target unit word "แก้ว" (glass) mid-sentence
    "500ml",
    "did 10 min stretch",
    "today I had to undo a mistake at work",  # contains "undo" mid-sentence
    "เลิกงานแล้ว เหนื่อยมาก",  # contains "เลิก" (a substring of the undo trigger "ยกเลิก") mid-sentence
    "made 3 bottles of juice",  # "made" is not the edit trigger "make that "
    "I need to delete some old photos later",  # contains "delete" mid-sentence
    "ยกเลิกการนัดหมายพรุ่งนี้",  # starts with the Thai undo trigger but has trailing words -> not a bare command
    "change it to feeling better today",  # matches the edit trigger prefix but the tail isn't NUMBER [+ UNIT]
    "I finally decided to cancel my gym membership",
]


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_dispatch_returns_none_for_normal_habit_messages(message):
    assert commands.dispatch(message, GLASS_ML, BOTTLE_ML) is None


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
async def test_normal_habit_messages_reach_parser_exactly_once(db, fixed_clock, monkeypatch, message):
    channel = FakeChannel()
    config = Config()
    calls: list[str] = []

    async def counting_parse_message(text, llm, glass_ml, bottle_ml, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult.unknown()

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message(
        message, db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert calls == [message]  # called exactly once, with the original text


async def test_command_layer_does_not_intercept_a_normal_log_even_after_commands_ran(db, fixed_clock, monkeypatch):
    """Regression guard mirroring Luna's own smoke test: running undo/edit
    commands earlier in a session must not leave any state that causes a
    later, perfectly normal log to be misclassified."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    await handle_inbound_message("/undo", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)
    await handle_inbound_message(
        "make that 250ml", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    calls: list[str] = []

    async def counting_parse_message(text, llm, glass_ml, bottle_ml, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult("water", 500, None, None, 0.9)

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message("500ml", db=db, llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert calls == ["500ml"]
    assert "logged" in channel.sent[-1]


# ===========================================================================
# Commands work while the LLM is DOWN (dispatch precedes the
# health-monitor / availability check in handle_inbound_message).
# ===========================================================================


async def test_undo_executes_while_llm_is_down_not_deferred(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "/undo",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert db.water_total_ml("2026-08-19") == 0.0
    assert channel.sent != [DEFERRED_ACK_MESSAGE]
    # No 'unparsed' row was written -- the command executed directly.
    assert db.pending_unparsed() == []


async def test_edit_executes_while_llm_is_down_not_deferred(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "make that 300ml",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert db.last_log(category="water")["value_num"] == 300.0
    assert channel.sent != [DEFERRED_ACK_MESSAGE]


async def test_normal_message_while_llm_down_is_still_deferred_not_intercepted(db, fixed_clock):
    """Control case: a message that is NOT a recognized command must still
    fall through to the existing v0.4.0 deferral path while Ollama is
    DOWN -- proves the new command-dispatch step doesn't broaden and start
    swallowing messages it shouldn't."""
    channel = FakeChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "500ml",
        db=db,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert channel.sent == [DEFERRED_ACK_MESSAGE]
    pending = db.pending_unparsed()
    assert len(pending) == 1
    assert pending[0]["raw_message"] == "500ml"


async def test_undo_command_in_dry_run_does_not_write_or_require_channel(db, fixed_clock, capsys):
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")

    await handle_inbound_message(
        "/undo", db=db, llm=_NeverCalledLLM(), channel=None, config=Config(), clock=fixed_clock, dry_run=True
    )

    # dry-run never writes -- the seeded row is untouched.
    assert db.water_total_ml("2026-08-19") == 500.0
    captured = capsys.readouterr()
    assert "undo" in captured.out


# ===========================================================================
# Migration 003 (soft-delete `deleted_at` column): fresh DB -> version 3;
# forward from v2 with rows intact; idempotent.
# ===========================================================================


def test_fresh_db_migrates_to_schema_version_3(tmp_path):
    """Pinned regression guard (not tautological): as of v0.5.0 there are
    exactly 3 migrations, and a fresh DB must land on version 3. Bump this
    literal deliberately the next time a migration is added -- unlike a
    bare `== len(MIGRATIONS)` comparison, this fails if a migration is
    silently added/removed without the test being updated."""
    assert len(MIGRATIONS) == 3

    database = Database(tmp_path / "fresh.db")
    assert database.schema_version_before == 0
    assert database.schema_version == 3
    database.close()


def test_migration_003_applies_forward_from_a_v2_db_with_rows_intact():
    conn = sqlite3.connect(":memory:")
    try:
        run_migrations(conn, migrations=MIGRATIONS[:2])
        assert current_version(conn) == 2
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
        assert "deleted_at" not in cols_before

        conn.execute(
            "INSERT INTO logs (ts, category, value_num, value_text, raw_message, source) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-08-10T09:00:00", "water", 500.0, None, "500ml", "reply"),
        )
        conn.commit()

        from_version, to_version = run_migrations(conn, migrations=MIGRATIONS)

        assert (from_version, to_version) == (2, 3)
        assert current_version(conn) == 3
        cols_after = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
        assert "deleted_at" in cols_after

        row = conn.execute(
            "SELECT value_num, raw_message, deleted_at FROM logs WHERE ts = ?", ("2026-08-10T09:00:00",)
        ).fetchone()
        assert row[0] == 500.0
        assert row[1] == "500ml"
        assert row[2] is None  # existing row not retroactively marked deleted
    finally:
        conn.close()


def test_migration_003_creates_deleted_at_index():
    conn = sqlite3.connect(":memory:")
    try:
        run_migrations(conn, migrations=MIGRATIONS)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_logs_deleted_at'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_migration_003_is_idempotent(tmp_path):
    db_path = tmp_path / "idem003.db"

    first = Database(db_path)
    assert first.schema_version == 3
    first.insert_log(LogEntry(None, "2026-08-19T10:00:00", "water", 250.0, None, "1 glass", "reply"))
    first.close()

    second = Database(db_path)
    assert second.schema_version_before == 3  # nothing pending
    assert second.schema_version == 3
    rows = second.logs_between("2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1  # row from the first open survived
    second.close()
