"""Command layer / edit-undo tests (ROADMAP.md v0.5.0 "Command Layer &
Edit/Undo", AC5.1-AC5.5), plus migration 003 (soft-delete `deleted_at`
column) which this version's undo/edit is built on.

ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (module M1, AC12):
`commands.dispatch(text, registry)` is now registry-aware -- edit values
resolve to a habit id via the habit's configured `unit`/`unit_aliases`
instead of a hardcoded water/stretch check. Every direct `dispatch(...)`
call below updated to the new 2-arg signature; `ExtractionResult`
construction updated to the new 3-field generic shape.

IMPORTANT (documented per this task's explicit audit requirement -- see
IMPL-v0.7-M1.md "Known limitations"/"Iteration log"): the tests in this
file that go through `handle_inbound_message` (undo/edit end-to-end, the
adversarial false-positive corpus's "reaches the parser" half, the
LLM-down-still-executes-commands cases) currently FAIL against the live
tree, not because of a bug in this file or in `core/commands.py`, but
because `main.py` (shared-surface, frozen, not owned by module M1) still
calls `commands.dispatch(text, config.units.glass_ml, config.units.bottle_ml)`
at its v0.6.0 call site (main.py line ~402) instead of the new
`commands.dispatch(text, registry)` contract SPEC-v0.7.md §5 assigns to
this module. That one-line wiring flip is explicitly deferred to
SPEC-v0.7.md §11's "Integration order" step 1 and is outside this file's
(and module M1's) ownership. `commands.dispatch()` itself is fully correct
and covered directly (AC12, AC5.5) by the tests that call it without going
through `handle_inbound_message`.

All against real on-disk SQLite files (tmp_path) -- no mocks for the DB,
since sqlite3 is cheap, reliable, local state. The LLM is either never
constructed (a `_NeverCalledLLM` stand-in that raises if touched -- proves
the command path is LLM-free, per IMPL.md) or monkeypatched at
`habit_assistant.main.parse_message` (same pattern as test_confirmations.py)
when a test needs to prove a message *did* reach the parser and count how
many times.

Companion to test_db.py (schema/aggregation), test_migrations.py (migration
runner contract), test_resilience.py (health-monitor-aware deferral) --
this file covers only the v0.5.0 additions (now generalized per v0.7.0).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from apscheduler.triggers.date import DateTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.reminders import ReminderState
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

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message, on_callback=None) -> None:
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
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


def _seed(db: Database, ts: str, category: str, value_num=None, value_text=None, raw: str | None = None) -> int:
    return db.insert_log(LogEntry(None, "owner", ts, category, value_num, value_text, raw or ts, "reply"))


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
        undo_phrase, db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    lang = i18n.detect_language(undo_phrase)
    removed_description = i18n.t("describe_log_water", lang, value_num=300.0)

    # Confirmation names what was removed (the most recent: 300 ml), not
    # the older 500ml row.
    assert len(channel.sent) == 1
    assert removed_description in channel.sent[0]

    # Running total reflects the removal: only the 500ml row remains.
    assert db.water_total_ml("owner", "2026-08-19") == 500.0

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

    await handle_inbound_message("/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)
    await handle_inbound_message("/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert "300" in channel.sent[0]
    assert "500" in channel.sent[1]
    assert db.water_total_ml("owner", "2026-08-19") == 0.0
    assert _raw_row_count(db) == 2  # both rows still physically present


async def test_undo_confirmation_names_stretch_entry_and_updates_count(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "stretch", 10.0, raw="10 min stretch")
    _seed(db, "2026-08-19T10:00:00", "stretch", 5.0, raw="5 min stretch")
    assert db.stretch_count("owner", "2026-08-19") == 2

    await handle_inbound_message("undo last", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert "5" in channel.sent[0]
    assert "stretch" in channel.sent[0]
    assert db.stretch_count("owner", "2026-08-19") == 1


async def test_undo_confirmation_names_diary_entry(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T21:00:00", "diary", value_text="a quiet reflective day", raw="a quiet reflective day")

    await handle_inbound_message("/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert "quiet reflective day" in channel.sent[0]
    assert db.diary_count("owner", "2026-08-19") == 0


# ===========================================================================
# AC5.2 -- undo with no prior entry / all-deleted history -> friendly
# message, zero writes.
# ===========================================================================


async def test_undo_on_empty_history_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    channel = FakeChannel()

    await handle_inbound_message(
        "/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [NOTHING_TO_UNDO_MESSAGE]
    assert _raw_row_count(db) == 0


async def test_undo_on_all_deleted_history_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")

    # First undo removes the only entry.
    await handle_inbound_message("/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)
    row_count_after_first_undo = _raw_row_count(db)

    # Second undo: nothing non-deleted left.
    await handle_inbound_message("/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)

    assert channel.sent[-1] == NOTHING_TO_UNDO_MESSAGE
    assert _raw_row_count(db) == row_count_after_first_undo  # no new row written


# ===========================================================================
# AC5.3 / AC12 -- edit updates the last matching entry's value in place and
# re-confirms the new daily total; edit with no matching entry is handled
# gracefully; edit values resolve to a habit via the registry.
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
        edit_phrase, db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    row = db.last_log("owner", category="water")
    assert row["value_num"] == 300.0
    assert _raw_row_count(db) == 1  # updated in place, not a new row

    total = db.water_total_ml("owner", "2026-08-19")
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
        "make that 350ml", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
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
        "change it to 15 min", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    row = db.last_log("owner", category="stretch")
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
        "make that 300ml", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert channel.sent == [NOTHING_TO_EDIT_MESSAGE]
    assert _raw_row_count(db) == 1
    row = db._conn.execute("SELECT value_num FROM logs WHERE category = 'stretch'").fetchone()
    assert row["value_num"] == 10.0  # untouched


async def test_edit_on_empty_history_sends_friendly_message_and_writes_nothing(db, fixed_clock):
    channel = FakeChannel()

    await handle_inbound_message(
        "edit that to 15 min", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=Config(), clock=fixed_clock
    )

    assert channel.sent == [NOTHING_TO_EDIT_MESSAGE]
    assert _raw_row_count(db) == 0


# ---------------------------------------------------------------------------
# AC12 -- `commands.dispatch(text, registry)` called directly (SPEC-v0.7.md
# §8's own AC12 examples, verbatim): edit-value unit resolution is now
# registry-driven, not hardcoded to water/stretch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edit_phrase", ["make that 300ml", "แก้เป็น 300 มล."])
def test_ac12_edit_water_examples_classify_as_water_edit(edit_phrase):
    command = commands.dispatch(edit_phrase, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "edit"
    assert command.category == "water"
    assert command.value_num == 300.0


def test_ac12_edit_stretch_example_classifies_as_stretch_edit():
    command = commands.dispatch("edit that to 15 min", DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "edit"
    assert command.category == "stretch"
    assert command.value_num == 15.0


@pytest.mark.parametrize(
    "garbled_phrase",
    ["make that a lot more", "edit that to blah", "/edit   ", "change it to feeling better today"],
)
def test_ac12_garbled_edit_tail_returns_none(garbled_phrase):
    assert commands.dispatch(garbled_phrase, DEFAULT_REGISTRY) is None


def test_ac12_edit_value_resolves_via_a_non_default_habit_alias():
    """A habit added purely for this test (not one of the three built-ins)
    with its own `unit`/`unit_aliases` must be resolvable the same way,
    proving edit-unit resolution is generic, not water/stretch-specific."""
    from habit_assistant.core.habits import Habit

    sleep = Habit(
        id="sleep",
        type="numeric",
        label_en="sleep",
        label_th="นอน",
        unit_en="h",
        unit_th="ชม.",
        goal=8,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={"hour": 1, "hours": 1},
    )
    registry = HabitRegistry([sleep])

    command = commands.dispatch("make that 7h", registry)

    assert command == commands.Command(kind="edit", category="sleep", value_num=7.0)


def test_ac12_ambiguous_unit_falls_through_to_none_regardless_of_registry_order():
    """SPEC-v0.7.md §9 risk 6's original note ("resolves to the FIRST habit
    in registry order") is SUPERSEDED by SPEC-v1.5.md's integration punch
    list #3 (TEST-v1.5-preparse.md Finding 1): `core/units.build_unit_lookup`
    now excludes a token claimed by two DIFFERENT habits from the lookup
    entirely, so an ambiguous unit resolves to `None` -- `_parse_edit_value`
    (which shares that lookup, R-L5) can no longer silently guess which
    habit "20 min" means; `dispatch` falls through to `None` (not a
    recognized command), same as any other unresolvable unit, letting the
    LLM path handle it with context instead of misattributing the edit to
    whichever habit happened to be listed first."""
    from habit_assistant.core.habits import Habit

    def duration_habit(id_: str) -> Habit:
        return Habit(
            id=id_,
            type="duration",
            label_en=id_,
            label_th=id_,
            unit_en="min",
            unit_th="นาที",
            goal=None,
            reminder_times=(),
            reminder_text_en=None,
            reminder_text_th=None,
            unit_aliases={},
        )

    registry = HabitRegistry([duration_habit("stretch"), duration_habit("yoga")])

    command = commands.dispatch("edit that to 20 min", registry)

    assert command is None

    reordered = HabitRegistry([duration_habit("yoga"), duration_habit("stretch")])
    assert commands.dispatch("edit that to 20 min", reordered) is None


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

    assert db.water_total_ml("owner", "2026-08-19") == 0.0


async def test_soft_deleted_row_excluded_from_stretch_count_and_ordinal(db, fixed_clock, monkeypatch):
    channel = FakeChannel()
    config = Config()
    old_id = _seed(db, "2026-08-19T09:00:00", "stretch", 10.0, raw="10 min stretch")
    db.soft_delete(old_id)
    assert db.stretch_count("owner", "2026-08-19") == 0

    patch_parse_message(monkeypatch, ExtractionResult("stretch", 5, 0.9))
    await handle_inbound_message(
        "5 min stretch", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    # Ordinal counts from 1, ignoring the soft-deleted earlier session.
    assert channel.sent == ["✅ 5 min stretch logged — 1st today"]


def test_soft_deleted_row_excluded_from_diary_count(db):
    log_id = _seed(db, "2026-08-19T21:00:00", "diary", value_text="a day", raw="a day")
    db.soft_delete(log_id)

    assert db.diary_count("owner", "2026-08-19") == 0


def test_soft_deleted_row_excluded_from_pending_unparsed(db):
    log_id = _seed(db, "2026-08-19T09:00:00", "unparsed", raw="500ml please")
    db.soft_delete(log_id)

    assert db.pending_unparsed() == []


def test_soft_deleted_rows_excluded_from_weekly_review_stats(db):
    """CHANGED (ROADMAP.md v0.7.0 integration): `compute_weekly_stats` is
    module M3's function (`core/review.py`) -- its new registry-aware
    signature (`db, config, registry, end_date`) and per-habit
    `WeeklyStats.get(habit_id)` accessor shape (`SPEC-v0.7.md` §5) are now
    wired through; same underlying math/assertions as before, only the
    call/accessor shape changed (registry-wiring edit, per
    `IMPL-v0.7-M3.md`'s own rename table for the equivalent `test_review.py`
    tests)."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    deleted_id = _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")
    stretch_id = _seed(db, "2026-08-19T11:00:00", "stretch", 10.0, raw="10 min")
    db.soft_delete(deleted_id)
    db.soft_delete(stretch_id)

    stats = compute_weekly_stats(db, config, registry, date(2026, 8, 19), "owner")

    assert stats.get("water").total == 500.0
    assert stats.get("stretch").total == 0


def test_soft_delete_never_issues_a_hard_delete(db):
    """Raw-SQL audit: after soft_delete, the row count in `logs` must be
    unchanged (no DELETE statement executed)."""
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    _seed(db, "2026-08-19T10:00:00", "water", 300.0, raw="300ml")
    before = _raw_row_count(db)

    row = db.last_log("owner")
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
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# SPEC-v1.5.md R-L1 (module `preparse`, AC-14): "500ml" is a whole-message
# "NUMBER UNIT" shape -- it now resolves via `core/preparse.py:
# deterministic_parse` BEFORE `parse_message` is ever reached, so it no
# longer belongs in the "must still reach the LLM parser" parametrization
# below (see `test_ac14_bare_number_unit_resolves_via_preparse_not_the_llm_
# parser` just below for its own dedicated coverage, mirroring this test's
# shape). Still fully valid for `test_dispatch_returns_none_for_normal_
# habit_messages` above (dispatch() correctly returns None for it either
# way, unaffected by preparse) and for `did 10 min stretch`, which is NOT a
# whole-message "NUMBER UNIT" shape (extra surrounding words) and so still
# correctly falls through to the LLM parser, unchanged.
MESSAGES_THAT_MUST_STILL_REACH_THE_LLM_PARSER = [m for m in ADVERSARIAL_MESSAGES if m != "500ml"]


@pytest.mark.parametrize("message", MESSAGES_THAT_MUST_STILL_REACH_THE_LLM_PARSER)
async def test_normal_habit_messages_reach_parser_exactly_once(db, fixed_clock, monkeypatch, message):
    channel = FakeChannel()
    config = Config()
    calls: list[str] = []

    async def counting_parse_message(text, llm, registry, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult.unknown()

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message(
        message, db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert calls == [message]  # called exactly once, with the original text


async def test_ac14_bare_number_unit_resolves_via_preparse_not_the_llm_parser(db, fixed_clock, monkeypatch):
    """SPEC-v1.5.md AC-14: unlike every other message in ADVERSARIAL_
    MESSAGES, "500ml" is a whole-message "NUMBER UNIT" shape -- it now
    resolves via `core/preparse.py:deterministic_parse` and never reaches
    `parse_message` (or the LLM `_NeverCalledLLM` would raise on) at all."""
    channel = FakeChannel()
    config = Config()
    calls: list[str] = []

    async def counting_parse_message(text, llm, registry, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult.unknown()

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message(
        "500ml", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert calls == []  # parse_message never reached
    assert "500" in channel.sent[-1]


async def test_command_layer_does_not_intercept_a_normal_log_even_after_commands_ran(db, fixed_clock, monkeypatch):
    """Regression guard mirroring Luna's own smoke test: running undo/edit
    commands earlier in a session must not leave any state that causes a
    later, perfectly normal log to be misclassified.

    SPEC-v1.5.md AC-14: the final log uses "300ml water" (not a whole-
    message "NUMBER UNIT" shape, so it still reaches `parse_message`,
    unlike a bare "500ml" -- see `test_ac14_bare_number_unit_resolves_via_
    preparse_not_the_llm_parser` for that shape's own dedicated coverage)
    to keep this test's original "still reaches the parser exactly once"
    assertion meaningful."""
    channel = FakeChannel()
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")
    await handle_inbound_message("/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock)
    await handle_inbound_message(
        "make that 250ml", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    calls: list[str] = []

    async def counting_parse_message(text, llm, registry, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult("water", 500, 0.9)

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message(
        "300ml water", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=channel, config=config, clock=fixed_clock
    )

    assert calls == ["300ml water"]
    assert "logged" in channel.sent[-1]


# ===========================================================================
# ROADMAP.md v0.8.0 AC8.1-AC8.5 -- query-intent detection. dispatch() itself
# is LLM-free: it only decides a message LOOKS like a question (anchored
# bilingual interrogative markers). The actual {habit_id, metric, timeframe}
# classification is tests/test_query.py's job (core/query.py). Checked
# *after* undo/edit in dispatch()'s own routing order, and confirmed here
# to never fire on the same adversarial corpus AC5.5 already guards (none
# of those messages contain a query anchor).
# ===========================================================================

QUERY_MESSAGES = [
    "how much water this week?",
    "How Many times did I stretch today?",
    "did I journal yesterday?",
    "have I logged water today?",
    "อาทิตย์นี้ยืดกี่ครั้ง",
    "วันนี้ดื่มน้ำไปเท่าไหร่",
    "ดื่มน้ำครบไหม",
    "เขียนไดอารี่หรือยัง",
    "what's my water total?",  # trailing "?" alone is sufficient
]


@pytest.mark.parametrize("message", QUERY_MESSAGES)
def test_dispatch_classifies_interrogative_messages_as_query(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "query"
    assert command.category is None
    assert command.value_num is None


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_query_matcher_never_fires_on_the_ac55_adversarial_corpus(message):
    """The same normal-log corpus AC5.5 guards for undo/edit must also
    never be misclassified as a query -- none of these messages end in
    "?" or contain a Thai/English interrogative anchor."""
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None


class _StaticQueryLLM:
    """Unlike undo/edit, the query path DOES call the LLM (to classify
    intent, core/query.py) -- this stub returns a canned, valid intent JSON
    for chat_json, so this test isolates "does the message reach
    parse_message" (it must not) without needing a real query answer."""

    def __init__(self, category: str, metric: str = "sum", timeframe: str = "this_week"):
        self._payload = json.dumps({"category": category, "metric": metric, "timeframe": timeframe})

    async def chat_json(self, system_prompt, user_prompt, json_schema, valid_categories):
        return self._payload

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("not exercised by a query")


async def test_query_message_does_not_reach_the_parser(db, fixed_clock, monkeypatch):
    """A query-shaped message must be answered by the query path, not fall
    through to parse_message (which would misfile it as an 'unknown' log
    attempt)."""
    channel = FakeChannel()
    config = Config()
    calls: list[str] = []

    async def counting_parse_message(text, llm, registry, confidence_threshold=None):
        calls.append(text)
        return ExtractionResult.unknown()

    monkeypatch.setattr("habit_assistant.main.parse_message", counting_parse_message)

    await handle_inbound_message(
        "how much water this week?",
        db=db, user_id="owner",
        llm=_StaticQueryLLM("water"),
        channel=channel,
        config=config,
        clock=fixed_clock,
    )

    assert calls == []
    assert channel.sent  # the query path (LLM-classify-then-answer) replied instead


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
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert db.water_total_ml("owner", "2026-08-19") == 0.0
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
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert db.last_log("owner", category="water")["value_num"] == 300.0
    assert channel.sent != [DEFERRED_ACK_MESSAGE]


async def test_normal_message_while_llm_down_is_still_deferred_not_intercepted(db, fixed_clock):
    """Control case: a message that is NOT a recognized command must still
    fall through to the existing v0.4.0 deferral path while Ollama is
    DOWN -- proves the new command-dispatch step doesn't broaden and start
    swallowing messages it shouldn't.

    SPEC-v1.5.md AC-16 flips this specific outcome for a bare "500ml"
    (a whole-message "NUMBER UNIT" shape now resolves via the pre-parser
    and logs successfully even with Ollama down -- see the dedicated
    `test_ac16_bare_number_unit_logs_successfully_while_llm_down_not_
    deferred` test right below). This test now uses "not feeling great
    today" (never a "NUMBER UNIT" shape) so its own original point --
    non-preparseable messages still defer while Ollama is down -- stays
    meaningful."""
    channel = FakeChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "not feeling great today",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert channel.sent == [DEFERRED_ACK_MESSAGE]
    pending = db.pending_unparsed()
    assert len(pending) == 1
    assert pending[0]["raw_message"] == "not feeling great today"


async def test_ac16_bare_number_unit_logs_successfully_while_llm_down_not_deferred(db, fixed_clock):
    """SPEC-v1.5.md AC-16: the pre-parser gate sits BEFORE the health-
    monitor deferral check in `handle_inbound_message` -- a whole-message
    "NUMBER UNIT" shape like "500ml" now logs successfully (and confirms)
    even while Ollama is DOWN, using `_NeverCalledLLM` to prove zero LLM
    calls along the way. Companion to the control case just above."""
    channel = FakeChannel()
    config = Config()
    health_monitor = _FrozenHealthMonitor(ollama_up=False)

    await handle_inbound_message(
        "500ml",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        health_monitor=health_monitor,
    )

    assert channel.sent != [DEFERRED_ACK_MESSAGE]
    assert "500" in channel.sent[-1]
    assert db.pending_unparsed() == []


async def test_undo_command_in_dry_run_does_not_write_or_require_channel(db, fixed_clock, capsys):
    _seed(db, "2026-08-19T09:00:00", "water", 500.0, raw="500ml")

    await handle_inbound_message(
        "/undo", db=db, user_id="owner", llm=_NeverCalledLLM(), channel=None, config=Config(), clock=fixed_clock, dry_run=True
    )

    # dry-run never writes -- the seeded row is untouched.
    assert db.water_total_ml("owner", "2026-08-19") == 500.0
    captured = capsys.readouterr()
    assert "undo" in captured.out


# ===========================================================================
# Migration 003 (soft-delete `deleted_at` column): fresh DB -> version 3;
# forward from v2 with rows intact; idempotent.
# ===========================================================================


def test_fresh_db_migrates_to_schema_version_8(tmp_path):
    """Pinned regression guard (not tautological): as of SPEC-v1.5.md
    "Hourly check-ins + DND + LLM-call minimization + release
    announcements" (migration 008: `users.checkin_window` +
    `users.last_announced_version`, purely additive) there are exactly 8
    migrations, and a fresh DB must land on version 8. Bump this literal
    deliberately the next time a migration is added -- unlike a bare
    `== len(MIGRATIONS)` comparison, this fails if a migration is
    silently added/removed without the test being updated.
    CHANGED (v1.5.0): was `== 7` / `test_..._schema_version_7` before
    migration 008 was added -- see IMPL-v1.5-shared.md."""
    assert len(MIGRATIONS) == 8

    database = Database(tmp_path / "fresh.db")
    assert database.schema_version_before == 0
    assert database.schema_version == 8
    database.close()


def test_migration_003_applies_forward_from_a_v2_db_with_rows_intact():
    """CHANGED (v0.7.0): `run_migrations(conn, migrations=MIGRATIONS)` ->
    `migrations=MIGRATIONS[:3]` -- this test is specifically about the
    v2->v3 (`deleted_at`) transition, so it now pins its own migration
    slice instead of the full (growing) `MIGRATIONS` list, which would
    otherwise also apply migration 004 and change the expected
    from/to version tuple. See IMPL.md."""
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

        from_version, to_version = run_migrations(conn, migrations=MIGRATIONS[:3])

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
    """CHANGED (v0.7.0): the hardcoded `== 3` literals became
    `== len(MIGRATIONS)` -- this test's point is reopen-idempotency, not
    pinning the exact migration count (that's
    `test_fresh_db_migrates_to_schema_version_4`'s job), so it shouldn't
    need updating every time a migration is appended. See IMPL.md."""
    db_path = tmp_path / "idem003.db"

    first = Database(db_path)
    assert first.schema_version == len(MIGRATIONS)
    first.insert_log(LogEntry(None, "owner", "2026-08-19T10:00:00", "water", 250.0, None, "1 glass", "reply"))
    first.close()

    second = Database(db_path)
    assert second.schema_version_before == len(MIGRATIONS)  # nothing pending
    assert second.schema_version == len(MIGRATIONS)
    rows = second.logs_between("owner", "2026-08-19T00:00:00", "2026-08-19T23:59:59")
    assert len(rows) == 1  # row from the first open survived
    second.close()


# ===========================================================================
# ROADMAP.md v0.9.0 "Adaptive Reminders, Snooze & Quiet Hours" AC9.3 --
# snooze command detection (LLM-free, core/commands.py) and end-to-end
# scheduling of a one-off follow-up reminder (main.py).
# ===========================================================================

SNOOZE_MESSAGES_WITH_EXPECTED_MINUTES = [
    ("snooze", None),
    ("Snooze", None),
    ("/snooze", None),
    ("snooze 30", 30),
    ("snooze for 30", 30),
    ("snooze 15 min", 15),
    ("snooze 15 minutes", 15),
    ("/snooze 45", 45),
    ("เลื่อน", None),
    ("เลื่อนก่อน", None),
    ("เลื่อน 30 นาที", 30),
]


@pytest.mark.parametrize("message,expected_minutes", SNOOZE_MESSAGES_WITH_EXPECTED_MINUTES)
def test_dispatch_classifies_snooze_phrases_with_correct_minutes(message, expected_minutes):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == "snooze"
    assert command.minutes == expected_minutes
    assert command.category is None
    assert command.value_num is None


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_snooze_matcher_never_fires_on_the_ac55_adversarial_corpus(message):
    """Same normal-log guard as undo/edit/query -- none of the adversarial
    corpus messages contain "snooze" or "เลื่อน" as a whole-message trigger."""
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None


class _FakeSnoozeScheduler:
    """Records `add_job` calls -- a minimal stand-in for AsyncIOScheduler so
    `_execute_snooze` can be exercised without a real scheduler running."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=True):
        self.jobs.append({"func": func, "trigger": trigger, "args": args, "id": id})


async def test_snooze_with_no_prior_reminder_sends_friendly_fallback_and_schedules_nothing(db, fixed_clock):
    channel = FakeChannel()
    scheduler = _FakeSnoozeScheduler()
    reminder_state = ReminderState()  # last_habit_id is empty -- nothing ever fired this process

    await handle_inbound_message(
        "snooze 30",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        scheduler=scheduler,
        reminder_state=reminder_state,
    )

    assert channel.sent == [i18n.t("snooze_no_recent_reminder", "en")]
    assert scheduler.jobs == []


async def test_snooze_with_explicit_minutes_schedules_a_one_off_job_for_last_reminded_habit(db, fixed_clock):
    channel = FakeChannel()
    scheduler = _FakeSnoozeScheduler()
    reminder_state = ReminderState()
    reminder_state.last_habit_id["owner"] = "water"

    await handle_inbound_message(
        "snooze 30",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        scheduler=scheduler,
        reminder_state=reminder_state,
    )

    assert len(channel.sent) == 1
    assert "30" in channel.sent[0]
    assert "water" in channel.sent[0]

    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    assert isinstance(job["trigger"], DateTrigger)
    (
        scheduled_channel,
        scheduled_chat_id,
        scheduled_habit,
        scheduled_lang,
        scheduled_db,
        scheduled_config,
        scheduled_state,
    ) = job["args"]
    assert scheduled_channel is channel
    assert scheduled_chat_id == "owner"
    assert scheduled_habit.id == "water"
    assert scheduled_db is db
    assert scheduled_state is reminder_state


async def test_snooze_without_explicit_minutes_uses_configured_default(db, fixed_clock):
    channel = FakeChannel()
    scheduler = _FakeSnoozeScheduler()
    reminder_state = ReminderState()
    reminder_state.last_habit_id["owner"] = "stretch"
    config = Config.model_validate({"snooze": {"default_minutes": 45}})

    await handle_inbound_message(
        "snooze",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=config,
        clock=fixed_clock,
        scheduler=scheduler,
        reminder_state=reminder_state,
    )

    assert "45" in channel.sent[0]
    assert len(scheduler.jobs) == 1


async def test_snooze_thai_phrase_gets_thai_confirmation_and_correct_run_time(db, fixed_clock):
    channel = FakeChannel()
    scheduler = _FakeSnoozeScheduler()
    reminder_state = ReminderState()
    reminder_state.last_habit_id["owner"] = "water"

    await handle_inbound_message(
        "เลื่อน 20 นาที",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        scheduler=scheduler,
        reminder_state=reminder_state,
    )

    assert i18n.t("snooze_confirmed", "th", minutes=20, label="น้ำ") in channel.sent
    run_date = scheduler.jobs[0]["trigger"].run_date
    assert run_date.replace(tzinfo=None) == fixed_clock() + timedelta(minutes=20)


async def test_snooze_command_in_dry_run_prints_and_schedules_nothing(db, fixed_clock, capsys):
    reminder_state = ReminderState()
    reminder_state.last_habit_id["owner"] = "water"
    scheduler = _FakeSnoozeScheduler()

    await handle_inbound_message(
        "snooze 10",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=None,
        config=Config(),
        clock=fixed_clock,
        dry_run=True,
        scheduler=scheduler,
        reminder_state=reminder_state,
    )

    captured = capsys.readouterr()
    assert "snooze" in captured.out
    assert "10" in captured.out
    assert scheduler.jobs == []


async def test_snooze_never_reaches_the_llm(db, fixed_clock):
    reminder_state = ReminderState()
    reminder_state.last_habit_id["owner"] = "water"
    scheduler = _FakeSnoozeScheduler()
    channel = FakeChannel()

    # _NeverCalledLLM raises AssertionError if either method is touched --
    # not raising here proves snooze is LLM-free, same guarantee undo/edit
    # already have (module docstring: "no LLM call ... for the patterns
    # matched here").
    await handle_inbound_message(
        "snooze",
        db=db, user_id="owner",
        llm=_NeverCalledLLM(),
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        scheduler=scheduler,
        reminder_state=reminder_state,
    )

    assert channel.sent  # got a confirmation, no exception raised
