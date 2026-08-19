"""Typed configuration: non-secret settings from config.toml, secrets from .env.

Two separate models on purpose: config.toml (schedule, goals, model tag, unit
constants) is safe to commit; Secrets (bot token, chat id) comes only from
the environment / .env and must never be written to config.toml.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


class ConfigError(RuntimeError):
    """Raised when config.toml or .env is missing/invalid. Callers should
    print this and exit with a non-zero status rather than crash with a
    traceback."""


# ROADMAP.md v0.7.0 "Multi-Habit Extensibility": `[[habits]]` (HabitConfig,
# below) is now the single source of truth for water/stretch/diary's
# schedule/goal/units. The four models below (through UnitsConfig) are
# retained-but-ignored -- kept loadable so an existing config.toml's
# `[reminders.*]`/`[units]` sections don't raise, per SPEC-v0.7.md §6/§10
# ("dropping them is future cleanup") -- until the leaf modules that still
# read them (core/commands.py, core/reminders.py, core/review.py) are
# migrated onto the registry at integration.
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
    retry_attempts: int = 2
    retry_backoff_seconds: float = 1.0

    @property
    def model_chain(self) -> list[str]:
        """Ordered fallback chain for OllamaClient (AC2.2). `models` wins
        when set; a config carrying only `model` (v0.1.0 shape) falls back
        to a single-element chain, so old configs behave identically
        (AC2.4)."""
        return list(self.models) if self.models else [self.model]


class TelegramConfig(BaseModel):
    poll_timeout: int = 30
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 60.0


class WeeklyReviewConfig(BaseModel):
    day_of_week: str = "sun"
    time: str = "20:00"


class BackupConfig(BaseModel):
    dir: str = "data/backups"
    retain: int = 14


class HealthConfig(BaseModel):
    interval_seconds: float = 60.0


class QuietHoursConfig(BaseModel):
    """ROADMAP.md v0.9.0 "Adaptive Reminders, Snooze & Quiet Hours": do-not-
    disturb windows during which no reminder fires (regardless of goal
    state) -- e.g. a nap or a sleep window. `windows` is a list of
    `[start, end]` "HH:MM" (24h, `config.app.timezone` wall-clock) pairs.
    `start <= end` is a normal same-day window; `start > end` crosses
    midnight (e.g. `["23:00", "07:00"]` spans the night) -- see
    `core/reminders.py:_in_quiet_hours`. Deliberately EMPTY by default
    (AC9.2's own risk note / ROADMAP's "⚑ NEEDS USER INPUT" resolution:
    opt-in, so an unconfigured install's reminder behavior never changes
    silently)."""

    windows: list[tuple[str, str]] = Field(default_factory=list)

    @field_validator("windows")
    @classmethod
    def _windows_are_hhmm_pairs(cls, v: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for start, end in v:
            if not _HHMM_RE.match(start) or not _HHMM_RE.match(end):
                raise ValueError(f"quiet_hours window {(start, end)!r} entries must match HH:MM")
        return v


class SnoozeConfig(BaseModel):
    """ROADMAP.md v0.9.0: default snooze duration when a "snooze"/"เลื่อน"
    command doesn't itself carry an explicit minute count (e.g. "snooze",
    "เลื่อนก่อน") -- `core/commands.py`'s `Command.minutes` is `None` in that
    case and the caller (`main.py`) falls back to this value."""

    default_minutes: int = 30

    @field_validator("default_minutes")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("snooze.default_minutes must be a positive integer")
        return v


class I18nConfig(BaseModel):
    """ROADMAP.md v0.6.0: `language` forces every reply to Thai/English
    regardless of input; "auto" (default) matches the detected language
    of the inbound message being replied to (AC6.3). `primary_language`
    is only consulted in "auto" mode, and only for unprompted sends that
    have no inbound message to detect from (reminders, health alerts,
    the weekly review) -- default Thai, per ROADMAP.md's own resolution
    for that ambiguity, confirmed by Archi."""

    language: Literal["auto", "th", "en"] = "auto"
    primary_language: Literal["th", "en"] = "th"


class AppConfig(BaseModel):
    timezone: str = "Asia/Bangkok"
    db_path: str = "data/habits.db"
    log_level: str = "INFO"


_HABIT_ID_RE = re.compile(r"^[a-z0-9_]+$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_RESERVED_HABIT_IDS = {"unknown", "unparsed"}


class HabitLabel(BaseModel):
    """A bilingual (en/th) string, used for a habit's `label`, `unit`, and
    `reminder_text` (ROADMAP.md v0.7.0 "Multi-Habit Extensibility")."""

    en: str
    th: str


class HabitConfig(BaseModel):
    """One `[[habits]]` entry: SPEC-v0.7.md §2.1 -- the single source of
    truth that generates the extraction schema/prompt, per-type validation,
    storage, reminders, confirmations, and review for a habit, instead of
    that behavior being hand-coded per category (water/stretch/diary) as
    it was through v0.6.0."""

    id: str
    type: Literal["numeric", "duration", "text", "boolean"]
    label: HabitLabel
    unit: HabitLabel | None = None
    goal: float | None = None
    reminder_times: list[str] = Field(default_factory=list)
    reminder_text: HabitLabel | None = None
    unit_aliases: dict[str, float] = Field(default_factory=dict)
    # ROADMAP.md v0.9.0 AC9.1/AC9.4: when this habit's daily goal is already
    # met, core/reminders.py skips (logs, doesn't send) its reminder instead
    # of nagging. Per-habit, default True; set False to disable adaptive
    # skipping for just this habit. No-op for a habit with no `goal`.
    skip_if_goal_met: bool = True

    @field_validator("id")
    @classmethod
    def _id_is_valid_and_not_reserved(cls, v: str) -> str:
        if not _HABIT_ID_RE.match(v):
            raise ValueError(f"habit id {v!r} must match ^[a-z0-9_]+$")
        if v in _RESERVED_HABIT_IDS:
            raise ValueError(f"habit id {v!r} is reserved and may not be used")
        return v

    @field_validator("label")
    @classmethod
    def _label_both_languages_present(cls, v: HabitLabel) -> HabitLabel:
        if not v.en.strip() or not v.th.strip():
            raise ValueError("habit label.en and label.th must both be non-empty")
        return v

    @field_validator("reminder_times")
    @classmethod
    def _reminder_times_are_hhmm(cls, v: list[str]) -> list[str]:
        for t in v:
            if not _HHMM_RE.match(t):
                raise ValueError(f"reminder_times entry {t!r} must match HH:MM")
        return v

    @model_validator(mode="after")
    def _unit_and_goal_match_type(self) -> "HabitConfig":
        numeric_or_duration = self.type in ("numeric", "duration")
        if numeric_or_duration and self.unit is None:
            raise ValueError(f"habit {self.id!r} of type {self.type!r} requires a unit")
        if not numeric_or_duration and self.unit is not None:
            raise ValueError(f"habit {self.id!r} of type {self.type!r} must not have a unit")
        if not numeric_or_duration and self.goal is not None:
            raise ValueError(f"habit {self.id!r} of type {self.type!r} must not have a goal")
        return self


def _default_habits() -> list[HabitConfig]:
    """The three habits generalized (SPEC-v0.7.md §2.1): with no
    `config.toml`, or one that omits `[[habits]]`, `Config()` must be
    behaviourally identical to v0.6.0 (AC1)."""
    return [
        HabitConfig(
            id="water",
            type="numeric",
            goal=2500,
            reminder_times=["08:00", "10:30", "13:00", "15:30", "18:00", "20:30"],
            label=HabitLabel(en="water", th="น้ำ"),
            unit=HabitLabel(en="ml", th="มล."),
            unit_aliases={"glass": 250, "แก้ว": 250, "bottle": 600, "ขวด": 600},
        ),
        HabitConfig(
            id="stretch",
            type="duration",
            reminder_times=["11:00", "16:00"],
            label=HabitLabel(en="stretch", th="ยืดเส้น"),
            unit=HabitLabel(en="min", th="นาที"),
        ),
        HabitConfig(
            id="diary",
            type="text",
            reminder_times=["21:30"],
            label=HabitLabel(en="diary", th="ไดอารี่"),
        ),
    ]


class Config(BaseModel):
    app: AppConfig = AppConfig()
    telegram: TelegramConfig = TelegramConfig()
    ollama: OllamaConfig = OllamaConfig()
    reminders: RemindersConfig = RemindersConfig()
    units: UnitsConfig = UnitsConfig()
    weekly_review: WeeklyReviewConfig = WeeklyReviewConfig()
    backup: BackupConfig = BackupConfig()
    health: HealthConfig = HealthConfig()
    i18n: I18nConfig = I18nConfig()
    quiet_hours: QuietHoursConfig = QuietHoursConfig()
    snooze: SnoozeConfig = SnoozeConfig()
    habits: list[HabitConfig] = Field(default_factory=_default_habits)

    @field_validator("habits")
    @classmethod
    def _habit_ids_are_unique(cls, v: list[HabitConfig]) -> list[HabitConfig]:
        ids = [h.id for h in v]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate habit id(s): {dupes}")
        return v


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
