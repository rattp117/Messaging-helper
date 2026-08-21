"""SPEC-v1.1.md §4 Feature 1 "undo-ui" (core/undo_ui.py) -- module tests for
the ACs this parallel module owns (SPEC-v1.1.md §11): AC1, AC2, AC5, AC7,
AC8, AC9, AC11.

This module is self-contained (does not touch `main.py`), so tests exercise
`core/undo_ui.py`'s public functions directly, plus `main.handle_inbound_
message`/`main._execute_undo` (unmodified, pre-existing) as the "other side"
of the AC11 byte-identical comparison. No mocks for the DB (real on-disk
SQLite via tmp_path, mirroring tests/test_v11_shared_surface.py's own
convention).

AC1/AC2 (startup `set_my_commands` registration + failure handling) are
`async_main`-level behavior that lives in `main.py`'s integration wiring,
explicitly out of this module's scope (see IMPL-v1.1-undo-ui.md "Wiring for
the integrator"). This file covers this module's building block for AC1
(`command_menu_entries()`'s shape/content) and documents the exact call the
integrator must make and wrap in try/except for AC2; the full end-to-end
assertion against a real `async_main` run is the integration Vera pass.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n, undo_ui
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


OWNER = "owner"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, list[tuple[str, str]]]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons: list[tuple[str, str]]) -> None:
        self.actionable.append((text, buttons))
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"


@pytest.fixture
def fixed_clock():
    return lambda: datetime(2026, 8, 19, 9, 0, 0)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


def _extended_config() -> Config:
    """A registry carrying one habit of each shape undo confirmations
    branch on, beyond the three built-ins: a goal-bearing numeric (juice),
    a goal-less numeric (candy), a duration (workout), and a boolean
    (meds). Mirrors tests/test_query.py's own extension pattern."""
    return Config(
        habits=[
            *Config().habits,
            {
                "id": "juice",
                "type": "numeric",
                "goal": 1000,
                "label": {"en": "juice", "th": "น้ำผลไม้"},
                "unit": {"en": "ml", "th": "มล."},
            },
            {
                "id": "candy",
                "type": "numeric",
                "label": {"en": "candy", "th": "ลูกอม"},
                "unit": {"en": "pcs", "th": "เม็ด"},
            },
            {
                "id": "workout",
                "type": "duration",
                "label": {"en": "workout", "th": "ออกกำลังกาย"},
                "unit": {"en": "min", "th": "นาที"},
            },
            {
                "id": "meds",
                "type": "boolean",
                "label": {"en": "meds", "th": "ยา"},
            },
        ]
    )


def _seed(
    db: Database, ts: str, category: str, value_num: float | None, value_text: str | None = None, user_id: str = OWNER
) -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, value_text, "raw", "reply"))


# ---------------------------------------------------------------------------
# undo_button (R-U2/R-U5, supports AC3/AC5 -- the button itself, attached by
# main.py's integration step)
# ---------------------------------------------------------------------------


def test_undo_button_en():
    assert undo_ui.undo_button(1234, "en") == [(i18n.t("undo_button_label", "en"), "undo:1234")]


def test_undo_button_th():
    assert undo_ui.undo_button(1234, "th") == [(i18n.t("undo_button_label", "th"), "undo:1234")]


# ---------------------------------------------------------------------------
# AC5 (module-level guarantee): a message carrying the milestone suffix and
# the undo button travels through send_actionable as ONE message with
# exactly one button. Full end-to-end (a real milestone crossing through
# handle_inbound_message) is verified at the main.py integration step, since
# wiring send_actionable into the confirmation call sites is integration's
# job (SPEC-v1.1.md §11), not this module's.
# ---------------------------------------------------------------------------


async def test_button_and_milestone_suffix_travel_as_one_actionable_message():
    channel = FakeChannel()
    confirmation_text = "500ml logged — 500/2500 ml today (20%)"
    milestone_suffix = "\n\n🔥 3-day streak!"
    buttons = undo_ui.undo_button(77, "en")

    await channel.send_actionable(OWNER, confirmation_text + milestone_suffix, buttons)

    assert len(channel.actionable) == 1
    sent_text, sent_buttons = channel.actionable[0]
    assert "500ml logged" in sent_text
    assert "3-day streak" in sent_text
    assert sent_buttons == [(i18n.t("undo_button_label", "en"), "undo:77")]


# ---------------------------------------------------------------------------
# AC1 (building block): command_menu_entries()'s shape -- what main.py's
# startup wiring merges with targets_command's own entries before the one
# set_my_commands call.
# ---------------------------------------------------------------------------


def test_command_menu_entries_has_undo_for_both_languages():
    entries = undo_ui.command_menu_entries()

    assert set(entries.keys()) == {"en", "th"}
    for lang, commands in entries.items():
        assert commands == [("undo", undo_ui.UNDO_COMMAND_DESCRIPTIONS[lang])]
        command, description = commands[0]
        assert command == "undo"
        assert description.strip()

    assert entries["en"][0][1] != entries["th"][0][1]  # actually localized, not copy-pasted


# ---------------------------------------------------------------------------
# AC7: tapping Undo on a live entry soft-deletes it and sends the removed +
# recomputed-total confirmation, in the language detected from the source
# confirmation text.
# ---------------------------------------------------------------------------


async def test_handle_undo_callback_soft_deletes_and_sends_confirmation(db, registry, fixed_clock):
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        f"undo:{log_id}",
        "💧 500ml logged — 500/2500 ml today (20%)",
        "cb-1",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    row = db.get_log(log_id)
    assert row["deleted_at"] is not None
    assert len(channel.sent) == 1
    assert "Undone" in channel.sent[0]
    assert "0 / 2500 ml (0%)" in channel.sent[0]


async def test_handle_undo_callback_detects_thai_language_from_source_text(db, registry, fixed_clock):
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        f"undo:{log_id}",
        "💧 บันทึกน้ำ 500 มล. แล้ว",
        "cb-2",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert i18n.detect_language(channel.sent[0]) == "th"


async def test_handle_undo_callback_forced_language_overrides_source_text(db, registry, fixed_clock):
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    channel = FakeChannel()
    config = Config(i18n={"language": "th"})

    await undo_ui.handle_undo_callback(
        OWNER,
        f"undo:{log_id}",
        "500ml logged",  # English source text
        "cb-3",
        db=db,
        channel=channel,
        config=config,
        clock=fixed_clock,
        registry=registry,
    )

    assert i18n.detect_language(channel.sent[0]) == "th"


# ---------------------------------------------------------------------------
# AC8: re-tapping an already-deleted entry (or a genuinely missing id) is
# idempotent -- no second delete, a friendly already_undone reply, no crash.
# ---------------------------------------------------------------------------


async def test_handle_undo_callback_already_deleted_sends_already_undone_and_no_second_delete(
    db, registry, fixed_clock
):
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    db.soft_delete(log_id)
    deleted_at_before = db.get_log(log_id)["deleted_at"]
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        f"undo:{log_id}",
        "500ml logged",
        "cb-4",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == [i18n.t("already_undone", "en")]
    assert db.get_log(log_id)["deleted_at"] == deleted_at_before  # unchanged, not re-stamped


async def test_handle_undo_callback_missing_row_sends_already_undone(db, registry, fixed_clock):
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        "undo:999999",
        "500ml logged",
        "cb-5",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == [i18n.t("already_undone", "en")]


# ---------------------------------------------------------------------------
# AC9: malformed callback data -> no DB write, logged, nothing sent (the
# channel run loop -- shared surface, already tested in test_channels.py --
# is what still calls answerCallbackQuery regardless).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_data", ["foo", "undo:abc", "undo:", "undo:12abc", "undo: 12", "UNDO:12", ""])
async def test_handle_undo_callback_malformed_data_no_write_no_send(db, registry, fixed_clock, bad_data):
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        bad_data,
        "500ml logged",
        "cb-6",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == []
    assert db.get_log(log_id)["deleted_at"] is None  # untouched


async def test_handle_undo_callback_malformed_data_is_logged(db, registry, fixed_clock, caplog):
    channel = FakeChannel()

    with caplog.at_level(logging.INFO, logger="habit_assistant.core.undo_ui"):
        await undo_ui.handle_undo_callback(
            OWNER,
            "not-an-undo-payload",
            "500ml logged",
            "cb-7",
            db=db,
            channel=channel,
            config=Config(),
            clock=fixed_clock,
            registry=registry,
        )

    assert any("malformed" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# Vera adversarial additions (beyond Luna's coverage): a callback_data digit
# string that matches `undo:<int>` syntactically but is hostile in ways the
# regex alone doesn't rule out.
# ---------------------------------------------------------------------------


async def test_handle_undo_callback_negative_id_no_write_no_send(db, registry, fixed_clock):
    """"undo:-1" must not match `_UNDO_CALLBACK_RE` (only `\\d+` is
    accepted) -- treated as malformed, same as AC9's other examples."""
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        "undo:-1",
        "500ml logged",
        "cb-neg",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == []
    assert db.get_log(log_id)["deleted_at"] is None


async def test_handle_undo_callback_astronomically_large_id_does_not_raise(db, registry, fixed_clock):
    """A digit string that syntactically matches `undo:<int>` but exceeds
    SQLite's 64-bit INTEGER range (a hostile/malformed client payload a
    real Telegram client would never send, but nothing stops a forged
    callback_query from sending it). AC9/R-U6's contract is "malformed
    data -> logged, ignored, no write, never raises" -- this must hold for
    any digit string, not just ones that also fail the regex.

    Regression test for the OverflowError Luna fixed (TEST-v1.1-undo-ui.md
    finding): `_SQLITE_MAX_INTEGER` bounds-check in `handle_undo_callback`
    now rejects this before any `db.get_log` call."""
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        "undo:999999999999999999999999999999999",
        "500ml logged",
        "cb-huge",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == []
    assert db.get_log(log_id)["deleted_at"] is None  # untouched -- no DB write


async def test_handle_undo_callback_id_at_sqlite_max_int_is_a_normal_missing_id(db, registry, fixed_clock):
    """Boundary value: 2**63 - 1 (9223372036854775807) is the largest
    value SQLite's INTEGER can hold, so it is still IN range -- it must be
    treated as an ordinary (if nonexistent) log id, not silently dropped
    by the overflow guard. `db.get_log` is reached, returns no row, and
    the friendly `already_undone` reply is sent."""
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        "undo:9223372036854775807",
        "500ml logged",
        "cb-max",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == [i18n.t("already_undone", "en")]


async def test_handle_undo_callback_id_one_past_sqlite_max_int_is_ignored(db, registry, fixed_clock):
    """Boundary value: 2**63 (9223372036854775808) is the smallest value
    that overflows SQLite's INTEGER -- one past the max-int test above.
    Must be silently ignored (logged, no send, no DB read/write), not
    raise."""
    channel = FakeChannel()

    await undo_ui.handle_undo_callback(
        OWNER,
        "undo:9223372036854775808",
        "500ml logged",
        "cb-maxplus1",
        db=db,
        channel=channel,
        config=Config(),
        clock=fixed_clock,
        registry=registry,
    )

    assert channel.sent == []


async def test_handle_undo_callback_idempotent_after_text_undo_command(db, registry, fixed_clock):
    """Cross-path idempotency (AC8's guarantee must hold across BOTH undo
    entry points, not just repeated button-taps): a log already removed
    via the text `/undo` command, then tapped via the button, must not be
    deleted a second time and must get the friendly already_undone reply."""
    config = Config()
    log_id = _seed(db, "2026-08-19T08:00:00", "water", 500.0)

    text_channel = FakeChannel()
    await handle_inbound_message(
        "undo",
        db=db,
        llm=FakeLLM(),
        channel=text_channel,
        config=config,
        registry=registry,
        clock=fixed_clock,
        user_id=OWNER,
    )
    assert db.get_log(log_id)["deleted_at"] is not None

    button_channel = FakeChannel()
    await undo_ui.handle_undo_callback(
        OWNER,
        f"undo:{log_id}",
        "500ml logged",
        "cb-cross",
        db=db,
        channel=button_channel,
        config=config,
        clock=fixed_clock,
        registry=registry,
    )

    assert button_channel.sent == [i18n.t("already_undone", "en")]


# ---------------------------------------------------------------------------
# AC11: button-undo and command-undo (`/undo`, unmodified in main.py) must
# produce byte-identical confirmation copy for the same row + language,
# across every branch send_undo_confirmation/main._execute_undo shares.
# ---------------------------------------------------------------------------


async def _confirmation_via_command_path(db, config, registry, clock, lang: i18n.Language, undo_text: str) -> str:
    channel = FakeChannel()
    await handle_inbound_message(
        undo_text, db=db, llm=FakeLLM(), channel=channel, config=config, registry=registry, clock=clock, user_id=OWNER
    )
    assert len(channel.sent) == 1
    return channel.sent[0]


async def _confirmation_via_button_path(db, config, registry, clock, lang: i18n.Language, row) -> str:
    channel = FakeChannel()
    await undo_ui.send_undo_confirmation(db, channel, config, clock, registry, lang, row)
    assert len(channel.sent) == 1
    return channel.sent[0]


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_water(tmp_path, fixed_clock, lang, undo_text):
    config = Config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "water", 500.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "water", 500.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_stretch(tmp_path, fixed_clock, lang, undo_text):
    config = Config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "stretch", 10.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "stretch", 10.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_diary_generic_fallback(tmp_path, fixed_clock, lang, undo_text):
    config = Config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "diary", None, "a fine day")
        row_id = _seed(db_b, "2026-08-19T08:00:00", "diary", None, "a fine day")

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_generic_numeric_with_goal(tmp_path, fixed_clock, lang, undo_text):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "juice", 250.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "juice", 250.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
        assert "250" in command_side and "1000" in command_side  # sanity: reached the numeric-goal branch
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_generic_numeric_without_goal_falls_back_to_generic(tmp_path, fixed_clock, lang, undo_text):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "candy", 3.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "candy", 3.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_generic_duration(tmp_path, fixed_clock, lang, undo_text):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "workout", 15.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "workout", 15.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_generic_boolean(tmp_path, fixed_clock, lang, undo_text):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        _seed(db_a, "2026-08-19T08:00:00", "meds", 1.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "meds", 1.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
    finally:
        db_a.close()
        db_b.close()


@pytest.mark.parametrize("lang,undo_text", [("en", "undo"), ("th", "ยกเลิก")])
async def test_byte_identical_water_with_target_override(tmp_path, fixed_clock, lang, undo_text):
    """AC11 combined with R-T5/AC23: the override-aware goal resolution
    (targets.effective_goal) must also stay byte-identical between the two
    undo paths once a `/target` override is active."""
    config = Config()
    registry = HabitRegistry.from_config(config)

    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        db_a.set_target(OWNER, "water", 1000.0)
        db_b.set_target(OWNER, "water", 1000.0)
        _seed(db_a, "2026-08-19T08:00:00", "water", 500.0)
        row_id = _seed(db_b, "2026-08-19T08:00:00", "water", 500.0)

        command_side = await _confirmation_via_command_path(db_a, config, registry, fixed_clock, lang, undo_text)
        button_side = await _confirmation_via_button_path(
            db_b, config, registry, fixed_clock, lang, db_b.get_log(row_id)
        )
        assert command_side == button_side
        assert "1000" in command_side
    finally:
        db_a.close()
        db_b.close()
