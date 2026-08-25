"""SPEC-v1.8.md §4 Feature "quicklog" (`core/quicklog.py` + the disjoint
`core/commands.py:_match_log`/dispatch-branch edit) -- module tests for the
ACs this module owns (SPEC-v1.8.md §11): AC-A1 through AC-A6.

Mirrors `tests/test_undo_ui.py`'s own conventions for this codebase: real
on-disk SQLite via `tmp_path` (no DB mocks), a minimal `FakeChannel`
recording sends, and byte-identical-confirmation comparisons against
`main.py:handle_inbound_message` (unmodified) as the "other side" of
AC-A2's "same confirmation as the typed path" contract.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import commands, i18n, quicklog
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database

OWNER = "owner"
OTHER = "other-user"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.actionable: list[tuple[str, list[tuple[str, str]]]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.pins: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        self.sent.append(text)

    async def send_actionable(self, chat_id: str, text: str, buttons: list[tuple[str, str]]) -> None:
        self.actionable.append((text, buttons))
        self.sent.append(text)

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return True

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        self.pins.append((chat_id, text))
        return "pinned-msg-id"

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
    database.upsert_user(OTHER, role="member", status="active")
    yield database
    database.close()


def _extended_config() -> Config:
    """A registry with one habit of each shape build_keyboard/the
    confirmation formatter branch on, beyond the three built-ins: a
    goal-bearing numeric with NO aliases (workout... note: duration
    below), an aliased numeric with no unit text (pushups, mirrors
    SPEC-v1.8.md AC-A1's own literal example), a goal-less numeric
    (candy), and a boolean (meds). Mirrors tests/test_undo_ui.py's own
    `_extended_config` pattern."""
    return Config(
        habits=[
            *Config().habits,
            {
                "id": "pushups",
                "type": "numeric",
                "label": {"en": "pushups", "th": "วิดพื้น"},
                "unit": {"en": "", "th": ""},
                "unit_aliases": {"set": 10},
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
                "goal": 20,
                "label": {"en": "workout", "th": "ออกกำลังกาย"},
                # A unit distinct from `stretch`'s own "min"/"นาที" --
                # `core/units.build_unit_lookup` excludes a unit token
                # claimed by two different habits, so a colliding unit
                # would make "15min" fail to deterministically parse at
                # all (falls through to the LLM instead), unrelated to
                # anything this module owns.
                "unit": {"en": "wmin", "th": "นาทีว"},
            },
            {
                "id": "meds",
                "type": "boolean",
                "label": {"en": "meds", "th": "ยา"},
            },
        ]
    )


def _add_custom_habit(db: Database, user_id: str, *, id: str, type: str, goal: float | None = None, aliases: dict | None = None, unit_en: str = "", unit_th: str = "") -> None:
    db.add_user_habit(
        user_id,
        {
            "id": id,
            "type": type,
            "label_en": id,
            "label_th": id,
            "unit_en": unit_en if type in ("numeric", "duration") else None,
            "unit_th": unit_th if type in ("numeric", "duration") else None,
            "goal": goal,
            "unit_aliases": json.dumps(aliases) if aliases else None,
        },
    )


# ---------------------------------------------------------------------------
# AC-A1: build_keyboard -- the /log inline keyboard, from the acting user's
# own per-user registry.
# ---------------------------------------------------------------------------


def test_build_keyboard_default_config_only_water_gets_amount_buttons(db):
    """Default config: water has aliases (glass/bottle -> 250/600ml), so
    it gets amount buttons; stretch has neither a goal nor aliases, so
    it's skipped entirely; diary is text, always omitted."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)
    assert keyboard == [
        ("💧 250ml", "log:water:250"),
        ("💧 600ml", "log:water:600"),
    ]


def test_build_keyboard_bilingual_thai_labels_and_units(db):
    config = Config()
    registry = HabitRegistry.from_config(config)
    keyboard = quicklog.build_keyboard(registry, config, db, "th", OWNER)
    assert keyboard == [
        ("💧 250มล.", "log:water:250"),
        ("💧 600มล.", "log:water:600"),
    ]


def test_build_keyboard_boolean_habit_gets_one_done_button(db):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    keyboard_en = quicklog.build_keyboard(registry, config, db, "en", OWNER)
    keyboard_th = quicklog.build_keyboard(registry, config, db, "th", OWNER)

    assert ("done ✓", "log:meds:1") in keyboard_en
    assert ("เสร็จแล้ว ✓", "log:meds:1") in keyboard_th


def test_build_keyboard_text_habit_always_omitted(db):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    assert all("diary" not in payload for _label, payload in keyboard)


def test_build_keyboard_aliased_habit_matches_ac_a1_pushups_example(db):
    """SPEC-v1.8.md AC-A1's own literal example: a user who defined
    "pushups | alias=set:10" gets [💪10]."""
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    assert ("💪 10", "log:pushups:10") in keyboard


def test_build_keyboard_goal_less_numeric_no_aliases_is_skipped(db):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    assert all("candy" not in payload for _label, payload in keyboard)


def test_build_keyboard_goal_derived_ladder_for_duration_habit_with_no_aliases(db):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    workout_payloads = {payload for _label, payload in keyboard if payload.startswith("log:workout:")}
    # goal=20 -> ladder [round(5), round(10), 20] = [5, 10, 20]
    assert workout_payloads == {"log:workout:5", "log:workout:10", "log:workout:20"}
    assert ("💪 5wmin", "log:workout:5") in keyboard
    assert ("💪 20wmin", "log:workout:20") in keyboard


def test_build_keyboard_alias_multipliers_capped_at_max_buttons_per_habit(tmp_path):
    config = Config(
        habits=[
            *Config().habits,
            {
                "id": "coffee",
                "type": "numeric",
                "label": {"en": "coffee", "th": "กาแฟ"},
                "unit": {"en": "ml", "th": "มล."},
                "unit_aliases": {"small": 100, "medium": 200, "large": 350, "extra_large": 500},
            },
        ]
    )
    config.quicklog.max_buttons_per_habit = 2
    registry = HabitRegistry.from_config(config)
    db = Database(tmp_path / "coffee.db")
    try:
        keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)
        coffee_payloads = [payload for _label, payload in keyboard if payload.startswith("log:coffee:")]
        assert coffee_payloads == ["log:coffee:100", "log:coffee:200"]  # smallest-first, sorted ascending, capped
    finally:
        db.close()


def test_build_keyboard_ladder_capped_at_max_buttons_per_habit(db):
    config = _extended_config()
    config.quicklog.max_buttons_per_habit = 1
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    workout_payloads = [payload for _label, payload in keyboard if payload.startswith("log:workout:")]
    assert workout_payloads == ["log:workout:5"]


def test_build_keyboard_small_goal_ladder_deduplicates_and_never_hits_zero(db):
    """goal=2 -> ¼=0.5 (rounds to 0, floored to 1), ½=1.0 (rounds to 1,
    dedup with the floored ¼ rung) -> ladder = [1, 2], never a 0-amount
    button."""
    config = Config(
        habits=[
            *Config().habits,
            {"id": "tiny", "type": "numeric", "goal": 2, "label": {"en": "tiny", "th": "tiny"}, "unit": {"en": "x", "th": "x"}},
        ]
    )
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    tiny_payloads = [payload for _label, payload in keyboard if payload.startswith("log:tiny:")]
    assert tiny_payloads == ["log:tiny:1", "log:tiny:2"]


def test_build_keyboard_uses_effective_goal_target_override_not_config_default(db):
    """stretch has no config goal (skipped by default) -- once a
    `/target stretch 20` override exists, `targets.effective_goal` makes
    it goal-bearing, so it now gets a ladder (R-Q1's own "effective
    goal", not the raw config default)."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    db.set_target(OWNER, "stretch", 20.0)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    stretch_payloads = {payload for _label, payload in keyboard if payload.startswith("log:stretch:")}
    assert stretch_payloads == {"log:stretch:5", "log:stretch:10", "log:stretch:20"}


def test_build_keyboard_empty_loggable_registry_yields_no_buttons_and_a_hint(db):
    config = Config(habits=[{"id": "diary", "type": "text", "label": {"en": "diary", "th": "diary"}}])
    registry = HabitRegistry.from_config(config)

    keyboard = quicklog.build_keyboard(registry, config, db, "en", OWNER)

    assert keyboard == []
    hint = quicklog.empty_keyboard_hint("en")
    assert "/addhabit" in hint
    hint_th = quicklog.empty_keyboard_hint("th")
    assert "/addhabit" in hint_th
    assert hint != hint_th


def test_build_keyboard_per_user_isolation_custom_habits(db):
    """R-Q1's own "per-user registry" contract: user OWNER's custom habit
    must not appear in OTHER's keyboard, and vice versa."""
    config = Config()
    _add_custom_habit(db, OWNER, id="pushups", type="numeric", aliases={"set": 10})

    owner_registry = HabitRegistry.for_user(config, db, OWNER)
    other_registry = HabitRegistry.for_user(config, db, OTHER)

    owner_keyboard = quicklog.build_keyboard(owner_registry, config, db, "en", OWNER)
    other_keyboard = quicklog.build_keyboard(other_registry, config, db, "en", OTHER)

    assert any("pushups" in payload for _label, payload in owner_keyboard)
    assert all("pushups" not in payload for _label, payload in other_keyboard)


def test_keyboard_prompt_text_is_bilingual():
    assert quicklog.keyboard_prompt_text("en") != quicklog.keyboard_prompt_text("th")
    assert quicklog.keyboard_prompt_text("en")
    assert quicklog.keyboard_prompt_text("th")


# ---------------------------------------------------------------------------
# AC-A2: handle_log_callback -- a tap inserts the log and sends the SAME
# confirmation the typed path sends (undo button included) + dashboard
# refresh.
# ---------------------------------------------------------------------------


async def test_handle_log_callback_water_matches_ac_output_shape(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:water:500", "👇 Tap to log:", "cb-1", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert len(channel.actionable) == 1
    text, buttons = channel.actionable[0]
    assert "500" in text and "2500" in text
    assert len(buttons) == 1 and buttons[0][1].startswith("undo:")

    row = db.get_log(int(buttons[0][1].split(":")[1]))
    assert row["category"] == "water"
    assert row["value_num"] == 500.0
    assert row["user_id"] == OWNER


async def _confirmation_via_typed_path(db, config, registry, clock, text: str, llm=None) -> str:
    channel = FakeChannel()
    await handle_inbound_message(
        text, db=db, llm=llm or FakeLLM(), channel=channel, config=config, registry=registry, clock=clock, user_id=OWNER
    )
    assert len(channel.sent) == 1
    return channel.sent[0]


async def _confirmation_via_tap_path(db, config, registry, clock, data: str) -> str:
    channel = FakeChannel()
    await quicklog.handle_log_callback(
        OWNER, data, "👇 Tap to log:", "cb", db=db, channel=channel, config=config, registry=registry, clock=clock
    )
    assert len(channel.sent) == 1
    return channel.sent[0]


async def test_byte_identical_water(tmp_path, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "500ml")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:water:500")
        assert typed == tapped
    finally:
        db_a.close()
        db_b.close()


async def test_byte_identical_stretch(tmp_path, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "10min")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:stretch:10")
        assert typed == tapped
    finally:
        db_a.close()
        db_b.close()


async def test_byte_identical_generic_numeric_with_goal(tmp_path, fixed_clock):
    config = Config(
        habits=[
            *Config().habits,
            # Unit "jml" (not "ml") -- water already claims "ml", and
            # `core/units.build_unit_lookup` excludes a token claimed by
            # two different habits from the lookup entirely, which would
            # make "250ml" fail to deterministically parse for "juice" at
            # all (falls through to the LLM instead), unrelated to
            # anything this module owns.
            {"id": "juice", "type": "numeric", "goal": 1000, "label": {"en": "juice", "th": "น้ำผลไม้"}, "unit": {"en": "jml", "th": "จมล."}},
        ]
    )
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "250jml")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:juice:250")
        assert typed == tapped
        assert "250" in typed and "1000" in typed
    finally:
        db_a.close()
        db_b.close()


async def test_byte_identical_generic_numeric_without_goal(tmp_path, fixed_clock):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "3pcs")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:candy:3")
        assert typed == tapped
    finally:
        db_a.close()
        db_b.close()


async def test_byte_identical_generic_duration(tmp_path, fixed_clock):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "15wmin")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:workout:15")
        assert typed == tapped
    finally:
        db_a.close()
        db_b.close()


async def test_byte_identical_generic_boolean(tmp_path, fixed_clock, monkeypatch):
    from habit_assistant.llm.ollama_client import ExtractionResult

    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return ExtractionResult("meds", True, 0.9)

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)

    config = _extended_config()
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "took my meds")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:meds:1")
        assert typed == tapped
    finally:
        db_a.close()
        db_b.close()


async def test_handle_log_callback_refreshes_the_pinned_dashboard(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()
    db.set_dashboard_msg_id(OWNER, "board-1")

    await quicklog.handle_log_callback(
        OWNER, "log:water:500", "👇 Tap to log:", "cb-2", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert len(channel.edits) == 1


async def test_handle_log_callback_milestone_and_record_lines_appear_like_the_typed_path(tmp_path, fixed_clock):
    """AC-A2's "same confirmation" extends to the milestone/record
    celebration suffixes -- not just the base confirmation line. Uses
    `stretch` (goal-less by default -- ANY single log qualifies a day,
    per `streaks.day_qualifies`'s own fallback) so a streak milestone is
    reliably reached without needing a goal-sized log."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    db_a = Database(tmp_path / "a.db")
    db_b = Database(tmp_path / "b.db")
    try:
        milestones = sorted(config.gamification.milestones) if config.gamification.milestones else [3]
        target_streak = milestones[0]
        for db_ in (db_a, db_b):
            for day_offset in range(1, target_streak):
                ts = f"2026-08-{19 - day_offset:02d}T08:00:00"
                db_.insert_log(_stretch_log(ts))

        typed = await _confirmation_via_typed_path(db_a, config, registry, fixed_clock, "10min")
        tapped = await _confirmation_via_tap_path(db_b, config, registry, fixed_clock, "log:stretch:10")
        assert typed == tapped
        assert f"{target_streak}-day" in typed or str(target_streak) in typed  # sanity: milestone actually fired
    finally:
        db_a.close()
        db_b.close()


def _stretch_log(ts: str):
    from habit_assistant.storage.models import LogEntry

    return LogEntry(None, OWNER, ts, "stretch", 10.0, None, "seed", "reply")


# ---------------------------------------------------------------------------
# AC-A3: ownership + safety -- foreign/unknown habit -> friendly no-op, no
# write; malformed/oversized/out-of-bounds payload -> logged + ignored, no
# read/write.
# ---------------------------------------------------------------------------


async def test_handle_log_callback_unknown_habit_is_a_friendly_no_op(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:nonexistent:500", "text", "cb-3", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == [i18n.t("quicklog_unknown_habit", "en")]


async def test_handle_log_callback_another_users_custom_habit_is_a_friendly_no_op(db, fixed_clock):
    """R-Q3/AC-A3: `registry` is scoped to the TAPPING user -- a habit_id
    that only exists in another user's per-user registry isn't present
    here, so it's treated exactly like an unknown habit."""
    config = Config()
    _add_custom_habit(db, OTHER, id="pushups", type="numeric", aliases={"set": 10})
    owner_registry = HabitRegistry.for_user(config, db, OWNER)  # OWNER's own registry -- no "pushups"
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:pushups:10", "text", "cb-4", db=db, channel=channel, config=config, registry=owner_registry, clock=fixed_clock
    )

    assert channel.sent == [i18n.t("quicklog_unknown_habit", "en")]
    assert db.sum_value(OWNER, "pushups", "2026-08-19") == 0


@pytest.mark.parametrize(
    "bad_data",
    [
        "foo",
        "log:",
        "log:water",
        "log:water:",
        "log:water:abc",
        "log:WATER:500",  # uppercase habit id -- not `^[a-z0-9_]+$`
        "log:water:500 ",  # trailing space
        " log:water:500",  # leading space
        "log:water:-500",
        "log:water:500:extra",
        "LOG:water:500",
        "",
        "log:" + "a" * 33 + ":500",  # habit id over the 32-char bound
    ],
)
async def test_handle_log_callback_malformed_data_no_write_no_send(db, fixed_clock, bad_data):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, bad_data, "text", "cb-5", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == []
    assert db.sum_value(OWNER, "water", "2026-08-19") == 0


async def test_handle_log_callback_malformed_data_is_logged(db, fixed_clock, caplog):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    with caplog.at_level(logging.INFO, logger="habit_assistant.core.quicklog"):
        await quicklog.handle_log_callback(
            OWNER, "not-a-log-payload", "text", "cb-6", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
        )

    assert any("malformed" in record.message.lower() for record in caplog.records)


async def test_handle_log_callback_astronomically_large_value_is_ignored(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER,
        "log:water:999999999999999",
        "text",
        "cb-7",
        db=db,
        channel=channel,
        config=config,
        registry=registry,
        clock=fixed_clock,
    )

    assert channel.sent == []
    assert db.sum_value(OWNER, "water", "2026-08-19") == 0


async def test_handle_log_callback_zero_value_for_numeric_habit_is_ignored(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:water:0", "text", "cb-8", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == []
    assert db.sum_value(OWNER, "water", "2026-08-19") == 0


async def test_handle_log_callback_boolean_with_non_one_value_is_ignored(db, fixed_clock):
    config = _extended_config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:meds:2", "text", "cb-9", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == []
    assert db.count_true(OWNER, "meds", "2026-08-19") == 0


async def test_handle_log_callback_text_habit_payload_is_ignored_not_a_friendly_reply(db, fixed_clock):
    """R-Q1: text habits never get a quick-log button, so a forged
    payload naming one (e.g. `log:diary:5`) can't come from a legitimate
    tap -- silently ignored, same bucket as a regex mismatch, NOT the
    friendly "unknown habit" reply (which is reserved for a habit not in
    the registry at all)."""
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:diary:5", "text", "cb-10", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.sent == []


# ---------------------------------------------------------------------------
# AC-A4/AC-A5 (this module's own half): a quick-log tap NEVER fires a
# reaction -- `handle_log_callback` has no `reactions` import/call at all.
# The typed-log reaction call site is `main.py`'s own later integration
# step (R-Q4), out of this module's scope; this test proves the structural
# half THIS module owns.
# ---------------------------------------------------------------------------


async def test_handle_log_callback_never_calls_set_message_reaction(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:water:500", "text", "cb-11", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert channel.reactions == []


def test_quicklog_module_imports_no_llm_client():
    """AC-A6 (zero-LLM, structural proof): `core/quicklog.py` never
    imports `OllamaClient`/`llm.ollama_client` at all, so it cannot
    possibly make an Ollama call -- unlike `main.py:_generic_confirmation`,
    whose `text`-type branch is simply unreachable from this module (R-Q1
    omits text habits before `_generic_confirmation`'s local mirror in
    this module is ever called). Checks the actual import machinery, not
    the word "Ollama" (which this module's own docstrings/comments
    legitimately mention when explaining WHY no such import exists)."""
    import inspect

    source = inspect.getsource(quicklog)
    assert "ollama_client" not in source.lower()
    assert "OllamaClient" not in source
    assert not hasattr(quicklog, "OllamaClient")
    assert "llm" not in inspect.signature(quicklog.handle_log_callback).parameters
    assert "llm" not in inspect.signature(quicklog.build_keyboard).parameters


# ---------------------------------------------------------------------------
# AC-A6 (bilingual): keyboard + confirmation follow the user's language.
# ---------------------------------------------------------------------------


async def test_handle_log_callback_detects_thai_language_from_source_text(db, fixed_clock):
    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    await quicklog.handle_log_callback(
        OWNER, "log:water:500", "👇 แตะเพื่อบันทึก:", "cb-12", db=db, channel=channel, config=config, registry=registry, clock=fixed_clock
    )

    assert i18n.detect_language(channel.sent[0]) == "th"


# ---------------------------------------------------------------------------
# _match_log / commands.dispatch -- the CRITICAL false-positive hazard: the
# Thai alias "บันทึก" is a common word that opens ordinary diary prose and
# must NEVER be misdispatched as the /log command in that shape.
# ---------------------------------------------------------------------------


def test_match_log_positive_slash_and_thai_bare_word():
    registry = HabitRegistry.from_config(Config())
    assert commands.dispatch("/log", registry) == commands.Command(kind="log")
    assert commands.dispatch("บันทึก", registry) == commands.Command(kind="log")
    assert commands.dispatch("/LOG", registry) == commands.Command(kind="log")  # case-insensitive slash form
    assert commands.dispatch("  /log  ", registry) == commands.Command(kind="log")  # stripped


@pytest.mark.parametrize(
    "message",
    [
        "บันทึกไดอารี่ วันนี้เหนื่อย",  # "[diary] entry: today I'm tired" -- must log as diary, not /log
        "บันทึก 500 น้ำ",
        "บันทึกด้วยนะ",
        "อยากบันทึกอะไรสักอย่าง",
        "วันนี้บันทึก",
        "บันทึกไว้ก่อน",
        "log",  # bare English word, no leading "/" -- §2.1 gives no bare-English alias
        "logging today",
        "logbook",
        "please log this",
        "/logging",
        "/log 500",  # R-Q1: bare command only, no tail grammar
        "/log water",
    ],
)
def test_match_log_never_misfires_on_ordinary_prose_or_other_log_shapes(message):
    registry = HabitRegistry.from_config(Config())
    result = commands.dispatch(message, registry)
    assert result is None or result.kind != "log", f"{message!r} unexpectedly dispatched as 'log': {result!r}"


async def test_match_log_thai_diary_prose_still_falls_through_to_normal_parsing(db, fixed_clock, monkeypatch):
    """End-to-end regression guard (R-G/AC-9 spirit): a real diary message
    starting with "บันทึก" must still reach the normal parser/LLM path and
    log as diary, not silently vanish or get treated as the /log command."""
    from habit_assistant.llm.ollama_client import ExtractionResult

    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return ExtractionResult("diary", "วันนี้เหนื่อย", 0.9)

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)

    config = Config()
    registry = HabitRegistry.from_config(config)
    channel = FakeChannel()

    command = commands.dispatch("บันทึกไดอารี่ วันนี้เหนื่อย", registry)
    assert command is None  # never intercepted as a command at all

    await handle_inbound_message(
        "บันทึกไดอารี่ วันนี้เหนื่อย",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=config,
        registry=registry,
        clock=fixed_clock,
        user_id=OWNER,
    )
    row = db.get_log(int(channel.actionable[0][1][0][1].split(":")[1]))
    assert row["category"] == "diary"
