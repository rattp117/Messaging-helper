"""AC8: `llm/prompts.build_extraction_system_prompt(registry)` is generated
from the live `HabitRegistry` -- one categories line + >=1 few-shot example
per configured habit, and for the default three the generated prompt still
covers water ml/glass/bottle, stretch minutes, diary text, and unknown (no
regression vs v0.6.0's fixed prompt). New file, module M1 scope
(`llm/prompts.py`)."""

from __future__ import annotations

from habit_assistant.config import Config
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.llm.prompts import build_extraction_system_prompt, build_extraction_user_prompt

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def test_user_prompt_wraps_the_message():
    assert build_extraction_user_prompt("hello") == "Message: hello"
    assert build_extraction_user_prompt("500ml") == "Message: 500ml"


def test_default_registry_prompt_covers_water_glass_bottle_stretch_diary_unknown():
    """No regression vs v0.6.0's fixed prompt for the default three
    (SPEC-v0.7.md §4 R5/AC8)."""
    prompt = build_extraction_system_prompt(DEFAULT_REGISTRY)

    assert '"water"' in prompt
    assert '"stretch"' in prompt
    assert '"diary"' in prompt
    assert '"unknown"' in prompt
    # water's unit + casual-unit aliases (glass/bottle, Thai + English), with
    # their configured multipliers, must reach the prompt.
    assert "ml" in prompt
    assert "250" in prompt  # glass/แก้ว multiplier
    assert "600" in prompt  # bottle/ขวด multiplier
    assert "glass" in prompt
    assert "แก้ว" in prompt  # แก้ว
    assert "bottle" in prompt
    assert "ขวด" in prompt  # ขวด
    # stretch's unit (minutes).
    assert "min" in prompt
    # At least one example per built-in habit id, plus the unknown example.
    assert prompt.count('"category": "water"') >= 1
    assert prompt.count('"category": "stretch"') >= 1
    assert prompt.count('"category": "diary"') >= 1
    assert prompt.count('"category": "unknown"') >= 1


def test_added_habit_gets_a_category_line_and_example():
    """AC8: given a registry including a new `sleep` habit (SPEC-v0.7.md
    §2.1's illustrative addition), the generated prompt contains a
    `sleep` category line with its unit and >=1 few-shot example."""
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
        unit_aliases={},
    )
    registry = HabitRegistry([*DEFAULT_REGISTRY, sleep])

    prompt = build_extraction_system_prompt(registry)

    assert '"sleep"' in prompt
    assert " h" in prompt or "h;" in prompt or "h." in prompt  # sleep's unit reaches the categories block
    assert prompt.count('"category": "sleep"') >= 1
    # Existing built-ins are untouched by the addition.
    assert '"water"' in prompt
    assert '"stretch"' in prompt
    assert '"diary"' in prompt


def test_boolean_and_text_habits_get_appropriate_examples():
    meds = Habit(
        id="meds",
        type="boolean",
        label_en="meds",
        label_th="ยา",
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    gratitude = Habit(
        id="gratitude",
        type="text",
        label_en="gratitude",
        label_th="ขอบคุณ",
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    registry = HabitRegistry([meds, gratitude])

    prompt = build_extraction_system_prompt(registry)

    assert '"meds"' in prompt
    assert '"gratitude"' in prompt
    assert prompt.count('"category": "meds"') >= 1
    assert prompt.count('"category": "gratitude"') >= 1
    assert "true" in prompt  # boolean example value


def test_prompt_size_grows_with_habit_count_unlike_the_schema():
    """Contrast to `build_extraction_schema`'s deliberate flat size
    (AC5): the *prompt* legitimately grows with habit count (each habit
    needs its own category line + example) -- only the schema needs to
    stay flat for the MLX backend (SPEC-v0.7.md §4 R4 note)."""
    small_prompt = build_extraction_system_prompt(HabitRegistry([DEFAULT_REGISTRY.get("water")]))
    big_prompt = build_extraction_system_prompt(DEFAULT_REGISTRY)

    assert len(big_prompt) > len(small_prompt)
