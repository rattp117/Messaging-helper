"""Config loader tests (AC1): typed config.toml load, defaults, and a clear
error when Telegram secrets are missing."""

from __future__ import annotations

import pytest

from habit_assistant.config import Config, ConfigError, HabitConfig, HabitLabel, load_config, load_secrets


def test_load_config_reads_values_from_toml(tmp_path):
    toml_text = """
[app]
timezone = "Asia/Bangkok"
db_path = "data/habits.db"
log_level = "INFO"

[ollama]
base_url = "http://mac-mini:11434"
model = "qwen3.5:9b-mlx"

[reminders.water]
times = ["08:00", "10:30"]
goal_ml = 2500

[units]
glass_ml = 250
bottle_ml = 600
"""
    path = tmp_path / "config.toml"
    path.write_text(toml_text, encoding="utf-8")

    config = load_config(path)

    assert config.app.timezone == "Asia/Bangkok"
    assert config.ollama.base_url == "http://mac-mini:11434"
    assert config.ollama.model == "qwen3.5:9b-mlx"
    assert config.reminders.water.times == ["08:00", "10:30"]
    assert config.reminders.water.goal_ml == 2500
    assert config.units.glass_ml == 250
    assert config.units.bottle_ml == 600


def test_load_config_missing_file_falls_back_to_defaults(tmp_path):
    missing_path = tmp_path / "does_not_exist.toml"

    config = load_config(missing_path)

    assert config == Config()
    assert config.reminders.water.goal_ml == 2500
    assert config.units.glass_ml == 250
    assert config.units.bottle_ml == 600


def test_load_config_malformed_toml_raises_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is not [ valid toml", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_invalid_values_raise_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[reminders.water]\ngoal_ml = "not a number"\n', encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_load_secrets_missing_env_file_raises_clear_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    missing_env = tmp_path / ".env"  # does not exist

    with pytest.raises(ConfigError) as excinfo:
        load_secrets(missing_env)

    message = str(excinfo.value)
    assert "telegram_bot_token" in message.lower()
    assert "telegram_chat_id" in message.lower()


def test_load_secrets_partial_env_reports_only_missing_field(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=123:abc\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_secrets(env_path)

    detail = str(excinfo.value).lower().split("copy")[0]  # the "(missing: ...)" clause only
    assert "telegram_chat_id" in detail
    assert "telegram_bot_token" not in detail


def test_load_secrets_reads_token_and_chat_id_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=123456:ABC-fake\nTELEGRAM_CHAT_ID=987654321\n", encoding="utf-8")

    secrets = load_secrets(env_path)

    assert secrets.telegram_bot_token == "123456:ABC-fake"
    assert secrets.telegram_chat_id == "987654321"


def test_config_error_is_a_runtime_error():
    """main.py catches ConfigError specifically to print a clean message
    and exit(1) rather than a raw traceback -- confirm the type contract."""
    assert issubclass(ConfigError, RuntimeError)


# ---------------------------------------------------------------------------
# ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (AC1): `[[habits]]` ->
# `Config.habits`, its defaults, and its validators.
# ---------------------------------------------------------------------------


def test_config_habits_default_reproduces_the_three_builtins():
    """Config() with no file/`[[habits]]` must be behaviourally identical
    to v0.6.0: exactly water/stretch/diary, in that order."""
    config = Config()
    assert [h.id for h in config.habits] == ["water", "stretch", "diary"]

    water = config.habits[0]
    assert water.type == "numeric"
    assert water.goal == 2500
    assert water.label.en == "water" and water.label.th == "น้ำ"
    assert water.unit.en == "ml" and water.unit.th == "มล."
    assert water.reminder_times == ["08:00", "10:30", "13:00", "15:30", "18:00", "20:30"]
    assert water.unit_aliases == {"glass": 250, "แก้ว": 250, "bottle": 600, "ขวด": 600}

    stretch = config.habits[1]
    assert stretch.type == "duration"
    assert stretch.goal is None
    assert stretch.unit.en == "min"
    assert stretch.reminder_times == ["11:00", "16:00"]

    diary = config.habits[2]
    assert diary.type == "text"
    assert diary.unit is None
    assert diary.goal is None
    assert diary.reminder_times == ["21:30"]


def test_config_toml_habits_match_config_defaults(tmp_path):
    """AC1: loading the shipped config.toml's `[[habits]]` must equal
    `Config()`'s defaults in id/type/label/unit/goal/reminder_times/
    unit_aliases -- the repo-root config.toml is the real fixture here."""
    from habit_assistant.config import DEFAULT_CONFIG_PATH

    loaded = load_config(DEFAULT_CONFIG_PATH)
    defaults = Config()

    assert len(loaded.habits) == len(defaults.habits) == 3
    for loaded_habit, default_habit in zip(loaded.habits, defaults.habits):
        assert loaded_habit.id == default_habit.id
        assert loaded_habit.type == default_habit.type
        assert loaded_habit.label == default_habit.label
        assert loaded_habit.unit == default_habit.unit
        assert loaded_habit.goal == default_habit.goal
        assert loaded_habit.reminder_times == default_habit.reminder_times
        assert loaded_habit.unit_aliases == default_habit.unit_aliases


def test_habit_config_bad_id_uppercase_raises_config_error():
    with pytest.raises(ValueError):
        HabitConfig(id="UP", type="text", label=HabitLabel(en="x", th="x"))


def test_habit_config_reserved_id_unknown_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="unknown", type="text", label=HabitLabel(en="x", th="x"))


def test_habit_config_reserved_id_unparsed_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="unparsed", type="text", label=HabitLabel(en="x", th="x"))


def test_habit_config_id_with_space_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="a b", type="text", label=HabitLabel(en="x", th="x"))


def test_habit_config_missing_label_language_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="x", type="text", label=HabitLabel(en="x", th=""))


def test_habit_config_unit_on_text_habit_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="x", type="text", label=HabitLabel(en="x", th="x"), unit=HabitLabel(en="u", th="u"))


def test_habit_config_goal_on_text_habit_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="x", type="text", label=HabitLabel(en="x", th="x"), goal=5)


def test_habit_config_numeric_without_unit_raises():
    with pytest.raises(ValueError):
        HabitConfig(id="x", type="numeric", label=HabitLabel(en="x", th="x"))


def test_habit_config_bad_reminder_time_format_raises():
    with pytest.raises(ValueError):
        HabitConfig(
            id="x", type="text", label=HabitLabel(en="x", th="x"), reminder_times=["8am"]
        )


def test_config_duplicate_habit_ids_raise():
    dupe = HabitConfig(id="water", type="text", label=HabitLabel(en="x", th="x"))
    with pytest.raises(ValueError):
        Config(habits=[dupe, dupe])


def test_load_config_toml_with_invalid_habit_raises_config_error(tmp_path):
    """AC1's end-to-end path: an invalid `[[habits]]` entry in a real
    config.toml surfaces as ConfigError (existing pattern), not a raw
    pydantic ValidationError leaking out of load_config."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[[habits]]\nid = "UP"\ntype = "text"\nlabel = { en = "x", th = "x" }\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(path)
