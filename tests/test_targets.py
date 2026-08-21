"""SPEC-v1.1.md "Undo menu + per-habit targets", Feature 2 (`targets`
module): the deterministic, LLM-free target set/show/clear path --
`core/commands.dispatch`'s `"target"` kind (R-T7-R-T9) and
`core/targets_command.execute_target` (R-T10).

Companion to `tests/test_core_targets.py` (the shared-surface
`core/targets.py` resolver, tested in isolation) and
`tests/test_target_nl.py` (the full-NL classifier, this module's other
half). This file covers `commands.dispatch(text, registry)` directly (no
`main.py` involved -- that integration wiring is explicitly deferred, per
IMPL-v1.1-shared.md/IMPL-v1.1-targets.md) and `execute_target` called
directly against a real on-disk SQLite `Database` (no mocks for the DB,
matching `tests/test_commands.py`'s own convention).
"""

from __future__ import annotations

import sqlite3

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.targets import effective_goal
from habit_assistant.core.targets_command import execute_target
from habit_assistant.storage.db import Database

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "targets.db")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


async def _execute(text: str, db: Database, config: Config, registry: HabitRegistry = DEFAULT_REGISTRY, lang="en") -> str:
    command = commands.dispatch(text, registry)
    assert command is not None and command.kind == "target"
    return await execute_target(command, db=db, config=config, registry=registry, lang=lang)


# ===========================================================================
# AC13 -- `/target water 2000`: writes the override, replies target_set
# naming the previous goal, effective_goal reflects it immediately.
# ===========================================================================


async def test_ac13_target_water_2000_sets_override_and_replies_with_previous_goal(db, config):
    reply = await _execute("/target water 2000", db, config)

    assert db.get_target("water") == 2000.0
    assert effective_goal(db, DEFAULT_REGISTRY.get("water"), config) == 2000.0
    assert reply == i18n.t("target_set", "en", label="water", goal=2000.0, unit="ml", previous="2500 ml")


def test_ac13_dispatch_shape():
    command = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
    assert command == Command(kind="target", category="water", value_num=2000.0, target_action="set")


# ===========================================================================
# AC14 -- `/target water 3 bottles`: unit alias multiplies (3 x 600 = 1800).
# ===========================================================================


async def test_ac14_target_water_3_bottles_multiplies_unit_alias(db, config):
    await _execute("/target water 3 bottles", db, config)
    assert db.get_target("water") == 1800.0


def test_ac14_dispatch_shape():
    command = commands.dispatch("/target water 3 bottles", DEFAULT_REGISTRY)
    assert command.category == "water"
    assert command.value_num == 1800.0
    assert command.target_action == "set"


# ===========================================================================
# AC15 -- non-positive value: no write, reply target_invalid_value.
# ===========================================================================


@pytest.mark.parametrize("text", ["/target water 0", "/target water -5"])
async def test_ac15_non_positive_value_writes_nothing_and_replies_invalid_value(db, config, text):
    reply = await _execute(text, db, config)

    assert db.get_target("water") is None
    assert reply == i18n.t("target_invalid_value", "en", habit_id="water", example="2500")


# ===========================================================================
# AC16 -- unknown habit id: no write, reply target_invalid_habit listing
# the tracked ids.
# ===========================================================================


async def test_ac16_unknown_habit_writes_nothing_and_lists_tracked_ids(db, config):
    reply = await _execute("/target coffee 2000", db, config)

    assert db.all_targets() == {}
    assert reply == i18n.t("target_invalid_habit", "en", habit_id="coffee", habit_list="water, stretch, diary")


# ===========================================================================
# AC17 -- a text habit (not goal-able): no write, reply target_not_goalable.
# ===========================================================================


async def test_ac17_text_habit_writes_nothing_and_replies_not_goalable(db, config):
    reply = await _execute("/target diary 5", db, config)

    assert db.all_targets() == {}
    assert reply == i18n.t("target_not_goalable", "en", label="diary")


# ===========================================================================
# AC18 -- clear (default/reset/clear/ค่าเริ่มต้น) reverts to config default.
# ===========================================================================


@pytest.mark.parametrize("clear_word", ["default", "reset", "clear", "ค่าเริ่มต้น"])
async def test_ac18_clear_synonyms_revert_to_config_default(db, config, clear_word):
    await _execute("/target water 2000", db, config)
    assert db.get_target("water") == 2000.0

    reply = await _execute(f"/target water {clear_word}", db, config)

    assert db.get_target("water") is None
    assert effective_goal(db, DEFAULT_REGISTRY.get("water"), config) == 2500
    assert reply == i18n.t("target_cleared", "en", label="water", default=2500, unit="ml")


async def test_clear_on_a_previously_goalless_habit_reports_no_goal_variant(db, config):
    """R-T5b: clearing a target on `stretch` (no config goal) must use
    `target_cleared_nogoal`, not attempt to format a None default."""
    await _execute("/target stretch 20", db, config)
    reply = await _execute("/target stretch default", db, config)

    assert db.get_target("stretch") is None
    assert reply == i18n.t("target_cleared_nogoal", "en", label="stretch")


# ===========================================================================
# AC19 -- show one habit with an active override: shows override + default.
# ===========================================================================


async def test_ac19_show_single_habit_with_override_shows_default_note(db, config):
    await _execute("/target water 2000", db, config)

    reply = await _execute("/target water", db, config)

    default_note = i18n.t("target_show_default_note", "en", default=2500, unit="ml")
    assert reply == i18n.t("target_show", "en", label="water", goal=2000.0, unit="ml", default_note=default_note)
    assert "2000" in reply
    assert "2500" in reply


async def test_show_single_habit_with_no_override_omits_default_note(db, config):
    reply = await _execute("/target water", db, config)
    assert reply == i18n.t("target_show", "en", label="water", goal=2500, unit="ml", default_note="")


# ===========================================================================
# AC20 -- show all: one line per registered habit, "no goal" for diary.
# ===========================================================================


async def test_ac20_show_all_lists_every_habit(db, config):
    reply = await _execute("/target", db, config)

    lines = reply.splitlines()
    assert lines[0] == i18n.t("target_show_all_header", "en")
    assert i18n.t("target_show_all_line", "en", label="water", goal=2500, unit="ml", default_note="") in lines
    assert i18n.t("target_show_all_line_nogoal", "en", label="stretch") in lines
    assert i18n.t("target_show_all_line_nogoal", "en", label="diary") in lines


async def test_show_all_reflects_an_active_override(db, config):
    await _execute("/target water 2000", db, config)
    reply = await _execute("/target", db, config)

    default_note = i18n.t("target_show_default_note", "en", default=2500, unit="ml")
    assert i18n.t("target_show_all_line", "en", label="water", goal=2000.0, unit="ml", default_note=default_note) in reply.splitlines()


# ===========================================================================
# AC27 -- anchored `/target water 2000` and `ตั้งเป้าน้ำ 2000` dispatch the
# same LLM-free "target"/"set" command; the existing adversarial corpus
# still returns non-target (no false positive).
# ===========================================================================


def test_ac27_slash_and_thai_anchored_forms_produce_the_same_set_command():
    slash = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
    thai = commands.dispatch("ตั้งเป้าน้ำ 2000", DEFAULT_REGISTRY)

    assert slash.kind == thai.kind == "target"
    assert slash.target_action == thai.target_action == "set"
    assert slash.category == thai.category == "water"
    assert slash.value_num == thai.value_num == 2000.0


def test_ac27_short_thai_trigger_also_works():
    command = commands.dispatch("เป้าน้ำ 2000", DEFAULT_REGISTRY)
    assert command == Command(kind="target", category="water", value_num=2000.0, target_action="set")


ADVERSARIAL_MESSAGES = [
    "ดื่มน้ำ 2 แก้ว",
    "500ml",
    "did 10 min stretch",
    "today I had to undo a mistake at work",
    "เลิกงานแล้ว เหนื่อยมาก",
    "made 3 bottles of juice",
    "I need to delete some old photos later",
    "ยกเลิกการนัดหมายพรุ่งนี้",
    "change it to feeling better today",
    "I finally decided to cancel my gym membership",
]


@pytest.mark.parametrize("message", ADVERSARIAL_MESSAGES)
def test_ac27_adversarial_corpus_never_dispatches_as_target(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "target"


class _NeverCalledLLM:
    async def chat_json(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a recognized command")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("LLM must never be called for a recognized command")


async def test_ac27_target_command_never_touches_the_llm(db, config):
    """`execute_target` itself never constructs or calls an LLM client --
    it's a pure DB-read/write + catalog-formatting function, same
    LLM-free guarantee as undo/edit/snooze (module docstring)."""
    command = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
    reply = await execute_target(command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en")
    assert "2000" in reply  # executed fine with no llm kwarg anywhere in the signature


# ===========================================================================
# AC28 -- a DB write failure during `set`/`clear` is logged and replied to
# with the generic target_save_failed message, never a traceback.
# ===========================================================================


class _ExplodingSetTarget(Database):
    def set_target(self, habit_id: str, goal: float) -> None:
        raise sqlite3.OperationalError("disk I/O error")


class _ExplodingClearTarget(Database):
    def clear_target(self, habit_id: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")


async def test_ac28_set_target_db_failure_replies_save_failed_not_a_traceback(tmp_path, config):
    exploding = _ExplodingSetTarget(tmp_path / "explode.db")
    try:
        command = commands.dispatch("/target water 2000", DEFAULT_REGISTRY)
        reply = await execute_target(command, db=exploding, config=config, registry=DEFAULT_REGISTRY, lang="en")
        assert reply == i18n.t("target_save_failed", "en")
        assert exploding.get_target("water") is None
    finally:
        exploding.close()


async def test_ac28_clear_target_db_failure_replies_save_failed_not_a_traceback(tmp_path, config):
    exploding = _ExplodingClearTarget(tmp_path / "explode2.db")
    try:
        command = commands.dispatch("/target water default", DEFAULT_REGISTRY)
        reply = await execute_target(command, db=exploding, config=config, registry=DEFAULT_REGISTRY, lang="en")
        assert reply == i18n.t("target_save_failed", "en")
    finally:
        exploding.close()


# ===========================================================================
# R-T9 -- a unit token that resolves to a DIFFERENT habit than the one
# named is a usage error, not a silent misinterpretation.
# ===========================================================================


def test_unit_belonging_to_a_different_habit_is_a_usage_error():
    command = commands.dispatch("/target water 3 min", DEFAULT_REGISTRY)  # "min" belongs to stretch
    assert command == Command(kind="target", target_action="usage")


async def test_usage_command_executes_to_the_usage_message(db, config):
    command = commands.dispatch("/target water 3 min", DEFAULT_REGISTRY)
    reply = await execute_target(command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en")
    assert reply == i18n.t("target_usage", "en")


def test_unresolved_unit_token_is_a_usage_error():
    command = commands.dispatch("/target water 3 bogusunit", DEFAULT_REGISTRY)
    assert command == Command(kind="target", target_action="usage")


# ===========================================================================
# English/Thai NL "set"-only anchored triggers (R-T7b) -- deterministic,
# not the full-NL LLM classifier (that's tests/test_target_nl.py).
# ===========================================================================


@pytest.mark.parametrize(
    "text,expected_category,expected_value",
    [
        ("set water goal to 2000", "water", 2000.0),
        ("set water target to 2000", "water", 2000.0),
        ("change water's target to 2000", "water", 2000.0),
        ("change water goal to 2000", "water", 2000.0),
        ("set stretch goal to 20", "stretch", 20.0),
    ],
)
def test_english_nl_set_triggers(text, expected_category, expected_value):
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command == Command(kind="target", category=expected_category, value_num=expected_value, target_action="set")


def test_thai_habit_label_resolves_via_slash_form_too():
    """The habit-token lookup covers Thai labels for the slash form as
    well, not just the anchored Thai trigger -- "/target น้ำ 2000" should
    resolve to the `water` habit id."""
    command = commands.dispatch("/target น้ำ 2000", DEFAULT_REGISTRY)
    assert command == Command(kind="target", category="water", value_num=2000.0, target_action="set")


def test_english_nl_trigger_with_garbled_value_is_usage_not_none():
    command = commands.dispatch("set water goal to a lot more", DEFAULT_REGISTRY)
    assert command == Command(kind="target", target_action="usage")


def test_thai_diary_style_sentence_about_an_unrelated_goal_is_not_swallowed():
    """A Thai sentence that happens to start with "เป้า" but names no
    configured habit must not be misclassified as a target command -- the
    Thai trigger's habit-token alternation is built from the LIVE registry
    (only water/stretch/diary's own id/label), so an unrelated phrase like
    "my goal is 2000 baht" (no real habit token immediately after
    "เป้า"/"ตั้งเป้า") falls through untouched."""
    command = commands.dispatch("เป้าหมายของฉันคือ 2000 บาท", DEFAULT_REGISTRY)
    assert command is None or command.kind != "target"


# ===========================================================================
# Vera adversarial additions (beyond Luna's own coverage) -- unit-token
# edge cases the deterministic parser must fail closed to "usage" on,
# rather than silently mis-parsing a number embedded in an unrecognized
# unit token. None of these are configured aliases for `water` (only
# "glass"/"แก้ว"/"bottle"/"ขวด" are, per config.toml), so each must become
# a usage error -- never a wrong-but-plausible set.
# ===========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "/target water 2.5 L",  # "L"/"liter" is not a configured water alias
        "/target water 2,000",  # thousands-separator comma -- not part of _TARGET_VALUE_RE's NUMBER grammar
        "/target water 2.5 ลิตร",  # Thai "liter" -- also not configured
        "/target water 2 500",  # a second bare number instead of a unit token
    ],
)
def test_unconfigured_or_malformed_unit_tokens_fail_closed_to_usage(text):
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command == Command(kind="target", target_action="usage")


def test_configured_unit_alias_with_no_space_still_resolves():
    """R-T9's unit resolution is not space-sensitive for the deterministic
    path -- "2000ml" (no space before the unit) must resolve exactly like
    "2000 ml"."""
    command = commands.dispatch("/target water 2000ml", DEFAULT_REGISTRY)
    assert command == Command(kind="target", category="water", value_num=2000.0, target_action="set")


# ===========================================================================
# Vera adversarial addition -- ordinary logging messages (not just the
# shared AC27 corpus) that superficially resemble a target-setting phrase
# (mention a number and a unit) must still never dispatch as "target" via
# the deterministic/anchored path. This is a fail-CLOSED guarantee
# independent of the LLM gate (tests/test_target_nl.py covers the gate
# itself) -- `commands.dispatch` has no LLM involvement at all.
# ===========================================================================


@pytest.mark.parametrize(
    "message",
    [
        "ดื่มน้ำ 500",
        "I drank 2.5L this morning",
        "drank 2 bottles of water today",
        "2.5L",
        "logged 2000ml just now",
    ],
)
def test_ordinary_log_messages_never_dispatch_as_target(message):
    command = commands.dispatch(message, DEFAULT_REGISTRY)
    assert command is None or command.kind != "target"
