"""Typed configuration: non-secret settings from config.toml, secrets from .env.

Two separate models on purpose: config.toml (schedule, goals, model tag, unit
constants) is safe to commit; Secrets (bot token, chat id) comes only from
the environment / .env and must never be written to config.toml.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


class ConfigError(RuntimeError):
    """Raised when config.toml or .env is missing/invalid. Callers should
    print this and exit with a non-zero status rather than crash with a
    traceback."""


class WaterConfig(BaseModel):
    times: list[str] = ["08:00", "10:30", "13:00", "15:30", "18:00", "20:30"]
    goal_ml: int = 2500


class StretchConfig(BaseModel):
    times: list[str] = ["11:00", "16:00"]


class DiaryConfig(BaseModel):
    times: list[str] = ["21:30"]


class RemindersConfig(BaseModel):
    water: WaterConfig = WaterConfig()
    stretch: StretchConfig = StretchConfig()
    diary: DiaryConfig = DiaryConfig()


class UnitsConfig(BaseModel):
    glass_ml: int = 250
    bottle_ml: int = 600


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen3.5:9b-mlx"
    models: list[str] | None = None
    timeout_seconds: float = 30.0
    confidence_threshold: float = 0.55

    @property
    def model_chain(self) -> list[str]:
        """Ordered fallback chain for OllamaClient (AC2.2). `models` wins
        when set; a config carrying only `model` (v0.1.0 shape) falls back
        to a single-element chain, so old configs behave identically
        (AC2.4)."""
        return list(self.models) if self.models else [self.model]


class TelegramConfig(BaseModel):
    poll_timeout: int = 30


class WeeklyReviewConfig(BaseModel):
    day_of_week: str = "sun"
    time: str = "20:00"


class BackupConfig(BaseModel):
    dir: str = "data/backups"
    retain: int = 14


class AppConfig(BaseModel):
    timezone: str = "Asia/Bangkok"
    db_path: str = "data/habits.db"
    log_level: str = "INFO"


class Config(BaseModel):
    app: AppConfig = AppConfig()
    telegram: TelegramConfig = TelegramConfig()
    ollama: OllamaConfig = OllamaConfig()
    reminders: RemindersConfig = RemindersConfig()
    units: UnitsConfig = UnitsConfig()
    weekly_review: WeeklyReviewConfig = WeeklyReviewConfig()
    backup: BackupConfig = BackupConfig()


class Secrets(BaseSettings):
    """Loaded from environment variables / .env. Never persisted to config.toml."""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    telegram_chat_id: str


def load_config(path: Path | None = None) -> Config:
    """Load config.toml. Missing file falls back to defaults; malformed
    file or values that fail validation raise ConfigError."""
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return Config()
    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse {config_path}: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {config_path}:\n{exc}") from exc


def load_secrets(env_file: Path | None = None) -> Secrets:
    """Load TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env / environment.
    Raises ConfigError with a clear, actionable message if either is missing."""
    try:
        if env_file is not None:
            return Secrets(_env_file=str(env_file))  # type: ignore[call-arg]
        return Secrets()
    except ValidationError as exc:
        missing = [str(e["loc"][0]) for e in exc.errors() if e["type"] == "missing"]
        detail = f"missing: {', '.join(missing)}" if missing else str(exc)
        raise ConfigError(
            "Could not load Telegram credentials from .env "
            f"({detail}). Copy .env.example to .env and fill in "
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        ) from exc
