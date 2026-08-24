"""HabitRegistry tests (AC2): building an ordered registry from `Config`,
`get`/`ids`/`category_enum`/iteration, and `log_entry_from_result`'s
per-type mapping to a `LogEntry` (AC7.5 groundwork, SPEC-v0.7.md §4 R10).
"""

from __future__ import annotations

from habit_assistant.config import Config, HabitConfig, HabitLabel
from habit_assistant.core.habits import BUILTIN_IDS, Habit, HabitRegistry, log_entry_from_result
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.storage.db import Database

# ---------------------------------------------------------------------------
# AC2: HabitRegistry.from_config -- default (water/stretch/diary) registry
# ---------------------------------------------------------------------------


def test_from_config_default_ids_in_config_order():
    registry = HabitRegistry.from_config(Config())
    assert registry.ids() == ["water", "stretch", "diary"]


def test_from_config_category_enum_appends_unknown():
    registry = HabitRegistry.from_config(Config())
    assert registry.category_enum() == ["water", "stretch", "diary", "unknown"]


def test_get_water_has_expected_type_goal_and_aliases():
    registry = HabitRegistry.from_config(Config())
    water = registry.get("water")
    assert water is not None
    assert water.type == "numeric"
    assert water.goal == 2500
    assert water.unit_aliases == {"glass": 250, "แก้ว": 250, "bottle": 600, "ขวด": 600}
    assert water.label("en") == "water"
    assert water.label("th") == "น้ำ"
    assert water.unit("en") == "ml"
    assert water.unit("th") == "มล."


def test_get_stretch_has_expected_type_and_no_goal():
    registry = HabitRegistry.from_config(Config())
    stretch = registry.get("stretch")
    assert stretch is not None
    assert stretch.type == "duration"
    assert stretch.goal is None
    assert stretch.unit("en") == "min"


def test_get_diary_has_expected_type_and_no_unit():
    registry = HabitRegistry.from_config(Config())
    diary = registry.get("diary")
    assert diary is not None
    assert diary.type == "text"
    assert diary.unit("en") is None
    assert diary.goal is None


def test_get_unknown_habit_id_returns_none():
    registry = HabitRegistry.from_config(Config())
    assert registry.get("nope") is None
    assert registry.get("unknown") is None


def test_iteration_yields_habit_objects_in_config_order():
    registry = HabitRegistry.from_config(Config())
    habits = list(registry)
    assert [h.id for h in habits] == ["water", "stretch", "diary"]
    assert all(isinstance(h, Habit) for h in habits)


def test_len_matches_configured_habit_count():
    registry = HabitRegistry.from_config(Config())
    assert len(registry) == 3


def test_builtin_ids_match_the_three_shipped_habits():
    assert BUILTIN_IDS == frozenset({"water", "stretch", "diary"})


# ---------------------------------------------------------------------------
# AC2 (extended, AC7.2 groundwork): a registry built from a config carrying
# an extra habit -- "add a habit -> registry picks it up, zero code
# changes needed here.
# ---------------------------------------------------------------------------


def _config_with_sleep_habit() -> Config:
    sleep = HabitConfig(
        id="sleep",
        type="numeric",
        goal=8,
        label=HabitLabel(en="sleep", th="นอน"),
        unit=HabitLabel(en="h", th="ชม."),
        reminder_text=HabitLabel(en="😴 How many hours did you sleep?", th="😴 เมื่อคืนนอนกี่ชั่วโมง?"),
    )
    return Config(habits=[*Config().habits, sleep])


def test_registry_picks_up_an_added_habit():
    registry = HabitRegistry.from_config(_config_with_sleep_habit())
    assert registry.ids() == ["water", "stretch", "diary", "sleep"]
    sleep = registry.get("sleep")
    assert sleep is not None
    assert sleep.type == "numeric"
    assert sleep.goal == 8
    assert sleep.reminder_text("en") == "😴 How many hours did you sleep?"
    assert sleep.reminder_text("th") == "😴 เมื่อคืนนอนกี่ชั่วโมง?"


def test_category_enum_includes_added_habit_and_unknown():
    registry = HabitRegistry.from_config(_config_with_sleep_habit())
    assert registry.category_enum() == ["water", "stretch", "diary", "sleep", "unknown"]


# ---------------------------------------------------------------------------
# log_entry_from_result (SPEC-v0.7.md §4 R10): per-type mapping to LogEntry.
# ---------------------------------------------------------------------------


def _habit(type_: str, goal=None) -> Habit:
    return Habit(
        id="test_habit",
        type=type_,
        label_en="test",
        label_th="ทดสอบ",
        unit_en="u" if type_ in ("numeric", "duration") else None,
        unit_th="ห" if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


def test_log_entry_from_result_numeric_sets_value_num():
    habit = _habit("numeric", goal=8)
    result = ExtractionResult("test_habit", 7, 0.9)

    entry = log_entry_from_result(habit, result, "2026-08-19T10:00:00", "7h", "reply", "owner")

    assert entry.category == "test_habit"
    assert entry.habit_type == "numeric"
    assert entry.value_num == 7.0
    assert entry.value_text is None
    assert entry.user_id == "owner"


def test_log_entry_from_result_duration_sets_value_num():
    habit = _habit("duration")
    result = ExtractionResult("test_habit", 15, 0.9)

    entry = log_entry_from_result(habit, result, "2026-08-19T10:00:00", "15 min", "reply", "owner")

    assert entry.habit_type == "duration"
    assert entry.value_num == 15.0
    assert entry.value_text is None


def test_log_entry_from_result_text_sets_value_text():
    habit = _habit("text")
    result = ExtractionResult("test_habit", "a note", 0.9)

    entry = log_entry_from_result(habit, result, "2026-08-19T10:00:00", "a note", "reply", "owner")

    assert entry.habit_type == "text"
    assert entry.value_num is None
    assert entry.value_text == "a note"


def test_log_entry_from_result_boolean_true_sets_value_num_one():
    habit = _habit("boolean")
    result = ExtractionResult("test_habit", True, 0.9)

    entry = log_entry_from_result(habit, result, "2026-08-19T10:00:00", "took meds", "reply", "owner")

    assert entry.habit_type == "boolean"
    assert entry.value_num == 1.0
    assert entry.value_text is None


def test_log_entry_from_result_boolean_false_sets_value_num_zero():
    habit = _habit("boolean")
    result = ExtractionResult("test_habit", False, 0.9)

    entry = log_entry_from_result(habit, result, "2026-08-19T10:00:00", "no meds yet", "reply", "owner")

    assert entry.value_num == 0.0


# ---------------------------------------------------------------------------
# SPEC-v1.7.md R-G1 (AC-2/AC-5): HabitRegistry.for_user -- base config
# habits + the user's own active `user_habits` rows.
# ---------------------------------------------------------------------------


def test_for_user_with_no_rows_is_byte_identical_to_from_config(tmp_path):
    """AC-2/AC-5's own hard regression gate: a user with zero `user_habits`
    rows must get a registry indistinguishable from the base-only one --
    same ids, same order, same every field on every habit."""
    db = Database(tmp_path / "byte_identical.db")
    config = Config()

    base = HabitRegistry.from_config(config)
    per_user = HabitRegistry.for_user(config, db, "owner-chat-id")

    assert per_user.ids() == base.ids()
    assert len(per_user) == len(base)
    for base_habit, per_user_habit in zip(base, per_user):
        assert base_habit == per_user_habit
    db.close()


def test_for_user_appends_active_custom_habits_after_the_base_catalog(tmp_path):
    db = Database(tmp_path / "append.db")
    config = Config()
    db.add_user_habit(
        "u1",
        {
            "id": "reading",
            "type": "duration",
            "label_en": "reading",
            "label_th": "อ่านหนังสือ",
            "unit_en": "min",
            "unit_th": "นาที",
            "goal": 30.0,
            "unit_aliases": '{"minutes": 1.0}',
        },
    )

    registry = HabitRegistry.for_user(config, db, "u1")

    assert registry.ids() == ["water", "stretch", "diary", "reading"]
    reading = registry.get("reading")
    assert reading is not None
    assert reading.type == "duration"
    assert reading.label_en == "reading"
    assert reading.label_th == "อ่านหนังสือ"
    assert reading.unit_en == "min"
    assert reading.unit_th == "นาที"
    assert reading.goal == 30.0
    assert reading.unit_aliases == {"minutes": 1.0}
    # A fresh custom habit has no reminders of its own yet (SPEC-v1.7.md
    # §9: reuses the existing per-user /remind machinery, no new storage).
    assert reading.reminder_times == ()
    assert reading.reminder_text_en is None
    assert reading.reminder_text_th is None
    db.close()


def test_for_user_excludes_archived_habits_from_the_registry(tmp_path):
    db = Database(tmp_path / "archived.db")
    config = Config()
    db.add_user_habit("u1", {
        "id": "reading", "type": "duration", "label_en": "reading", "label_th": "อ่านหนังสือ",
        "unit_en": "min", "unit_th": "นาที", "goal": 30.0, "unit_aliases": None,
    })
    db.archive_user_habit("u1", "reading")

    registry = HabitRegistry.for_user(config, db, "u1")

    assert registry.ids() == ["water", "stretch", "diary"]
    assert registry.get("reading") is None
    db.close()


def test_for_user_is_isolated_per_user(tmp_path):
    db = Database(tmp_path / "isolated.db")
    config = Config()
    db.add_user_habit("u1", {
        "id": "reading", "type": "duration", "label_en": "reading", "label_th": "อ่านหนังสือ",
        "unit_en": "min", "unit_th": "นาที", "goal": 30.0, "unit_aliases": None,
    })

    u1_registry = HabitRegistry.for_user(config, db, "u1")
    u2_registry = HabitRegistry.for_user(config, db, "u2")

    assert "reading" in u1_registry.ids()
    assert "reading" not in u2_registry.ids()
    assert u2_registry.ids() == ["water", "stretch", "diary"]
    db.close()


def test_for_user_unit_aliases_missing_defaults_to_empty_dict(tmp_path):
    db = Database(tmp_path / "no_aliases.db")
    config = Config()
    db.add_user_habit("u1", {
        "id": "pushups", "type": "numeric", "label_en": "pushups", "label_th": "วิดพื้น",
        "unit_en": "reps", "unit_th": "ครั้ง", "goal": None, "unit_aliases": None,
    })

    registry = HabitRegistry.for_user(config, db, "u1")

    pushups = registry.get("pushups")
    assert pushups is not None
    assert pushups.unit_aliases == {}
    assert pushups.goal is None
    db.close()
