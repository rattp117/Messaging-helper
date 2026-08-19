"""Config loader tests (AC1): typed config.toml load, defaults, and a clear
error when Telegram secrets are missing."""

from __future__ import annotations

import pytest

from habit_assistant.config import Config, ConfigError, load_config, load_secrets


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
