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
    # SPEC-v1.5.md R-L4: gates the once-per-model startup schema probe.
    # Default true preserves current (pre-v1.5) behavior; false skips it
    # (module `preparse`'s own wiring point, not read by the shared
    # surface itself).
    probe_on_startup: bool = True

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
    """SPEC-v1.5.md R-L3: the health monitor's own liveness-ping interval
    (`/api/version` + Telegram `getMe`) -- NOT an inference call, so
    raising it is a low-risk way to cut dependency-ping traffic. Default
    raised from 60 to 300 in v1.5.0; the pre-parser (module `preparse`)
    is what actually keeps most messages off Ollama during an outage, so
    a slower liveness ping is an acceptable trade (SPEC-v1.5.md §9)."""

    interval_seconds: float = 300.0


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


class CheckinConfig(BaseModel):
    """SPEC-v1.5.md §4 Feature 1 "Hourly check-ins" (R-K2), OQ1 RESOLVED
    (b): opt-in for everyone. `enabled = false` by default -- an
    unconfigured install (owner included) receives NO check-ins until
    the user runs `/checkin on` (AC-8). `window` is the "HH:MM-HH:MM"
    default applied when a user enables without specifying their own
    window (`users.checkin_window = "on"` via `/checkin on`); parsing/
    validating that string format is module `checkins`' own concern
    (`core/checkins.py:effective_checkin`), not this shared config
    surface -- this class only carries the two raw values."""

    enabled: bool = False
    window: str = "08:00-20:00"


class NudgeConfig(BaseModel):
    """SPEC-v1.6.md §4 Feature 5 "Almost there" end-of-day nudge (R-N1-
    R-N3), OQ2 RESOLVED: rides check-in enablement -- no separate toggle
    or `users` column, so this class carries only the two tuning knobs.
    `threshold_pct` (default 80): a goal-bearing habit counts as "close"
    once today's total is at least this percent of goal but still short
    of it; `time` (default "20:00"): the single fixed minute the nudge
    tick fires at, once/day by construction (mirrors `weekly_review.time`/
    `gamification.daily_summary_time`'s own "HH:MM" shape). Parsing/
    validating the tail (which habits are "close") is `core/nudge.py`'s
    own concern, not this shared config surface."""

    threshold_pct: int = 80
    time: str = "20:00"

    @field_validator("threshold_pct")
    @classmethod
    def _threshold_in_range(cls, v: int) -> int:
        if not (0 < v <= 100):
            raise ValueError("nudge.threshold_pct must be between 1 and 100")
        return v

    @field_validator("time")
    @classmethod
    def _time_is_hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError(f"nudge.time {v!r} must match HH:MM")
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


class GamificationConfig(BaseModel):
    """ROADMAP.md v0.10.0 "Streaks, Gentle Gamification & Daily Summary".

    `enabled` gates ONLY the milestone encouragement line appended to a
    confirmation (AC10.2/AC10.4) -- streak computation itself is always
    available (read-only, used unconditionally by the weekly review's
    duration streak, AC10.5) and is never suppressed by this flag.
    `daily_summary` is a separate, independent flag for the end-of-day
    recap job -- AC10.4's "no behavioral leakage" means turning one off
    must not silently affect the other."""

    enabled: bool = True
    milestones: list[int] = Field(default_factory=lambda: [3, 7, 30])
    daily_summary: bool = True
    daily_summary_time: str = "21:45"

    @field_validator("milestones")
    @classmethod
    def _milestones_are_positive(cls, v: list[int]) -> list[int]:
        if any(m <= 0 for m in v):
            raise ValueError("gamification.milestones entries must be positive integers")
        return sorted(set(v))

    @field_validator("daily_summary_time")
    @classmethod
    def _daily_summary_time_is_hhmm(cls, v: str) -> str:
        if not _HHMM_RE.match(v):
            raise ValueError(f"gamification.daily_summary_time {v!r} must match HH:MM")
        return v


class ChartsConfig(BaseModel):
    """ROADMAP.md v1.0.0 "Insights: Charts-as-Images". `enabled = true` by
    default -- the weekly review renders a chart PNG per chartable habit
    when `matplotlib` (the one optional dependency, `pip install -e
    ".[charts]"`) is importable. Missing matplotlib with `enabled = true`
    is NOT an error -- `core/charts.py` logs once and the review falls
    back to text-only, never crashing (AC1.0.1). Set `enabled = false` to
    skip chart rendering entirely regardless of what's installed."""

    enabled: bool = True


class AuditConfig(BaseModel):
    """SPEC-v1.3.md "Audit log" (§4 R-W3): how long `audit_log` rows are
    kept. `retention_days = 365` (default) -- long enough to be useful on
    a personal bot, bounded enough not to grow without limit; `0` means
    "keep forever, never prune". Pruning itself runs once at startup
    (`main.py`, not per-insert) and is not itself audited (housekeeping,
    R-W3's own explicit carve-out)."""

    retention_days: int = 365

    @field_validator("retention_days")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("audit.retention_days must be >= 0 (0 = keep forever)")
        return v


class GarminConfig(BaseModel):
    """ROADMAP.md v1.0.0 Garmin hydration import (closes SPEC.md §12's
    TODO). `csv_path = ""` (default) means the feature is off -- the
    weekly review appends no Garmin section at all. `column_map` maps the
    app's own field names to the CSV's header names, since export column
    naming varies by locale/device (ROADMAP's own risk note); default
    assumes a `Date, Hydration(ml)`-style export. `discrepancy_threshold_ml`
    is how far self-reported vs. Garmin can differ for a day before the
    review flags it."""

    csv_path: str = ""
    column_map: dict[str, str] = Field(
        default_factory=lambda: {"date": "Date", "hydration_ml": "Hydration(ml)"}
    )
    discrepancy_threshold_ml: float = 300


class NotificationsConfig(BaseModel):
    """SPEC-v1.8.md §2.5/R-D1 (module `riders`): `silent_proactive = true`
    (default) is the one deliberate v1.8.0 behavior change -- the three
    proactive-send sites (`reminders.send_reminder`, `checkins.
    run_due_checkins`, `nudge.run_due_nudges`) send with `Channel.send(...,
    disable_notification=True)` instead of the default notifying send.
    User-initiated confirmations/replies and the one-time dashboard pin are
    NOT gated by this flag -- module `riders`' own send-site edits are the
    only call sites that read it. `false` restores pre-v1.8 byte-identical
    proactive-send payloads (AC-D4)."""

    silent_proactive: bool = True


class QuicklogConfig(BaseModel):
    """SPEC-v1.8.md §2.5/R-Q1 (module `quicklog`): `enabled = true`
    (default) turns on the `/log`/`บันทึก` inline keyboard.
    `max_buttons_per_habit` caps how many amount buttons one goal-bearing/
    aliased numeric or duration habit can contribute (R-Q1's own ladder/
    alias-multiplier derivation is module `quicklog`'s concern; this class
    only carries the raw cap)."""

    enabled: bool = True
    max_buttons_per_habit: int = 3

    @field_validator("max_buttons_per_habit")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quicklog.max_buttons_per_habit must be a positive integer")
        return v


class ReactionsConfig(BaseModel):
    """SPEC-v1.8.md §2.5/R-Q4 (module `quicklog`): `enabled = true`
    (default) turns on the emoji reaction `core/reactions.py:react` fires
    after a successful typed-message log (R-Q4/R-Q5). `false` skips the
    `set_message_reaction` call entirely -- not just a silent no-op, so a
    disabled install makes zero extra Bot API calls."""

    enabled: bool = True


class BackfillConfig(BaseModel):
    """SPEC-v1.8.md §2.5/R-B5 (module `backfill`): how many days back a
    recognized relative-date phrase ("3 days ago", "3 วันที่แล้ว") may
    resolve to -- a date older than this (or in the future) is a bounds
    error, no write (AC-C4)."""

    max_days_back: int = 14

    @field_validator("max_days_back")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("backfill.max_days_back must be a positive integer")
        return v


class RoutinesConfig(BaseModel):
    """SPEC-v1.8.md §2.5/R-R1 (module `routines`): per-user cap on how many
    routines one user may create (`/routine <name> = ...` past the cap is
    a friendly error, no write, R-R1's own validation list)."""

    max_per_user: int = 20

    @field_validator("max_per_user")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("routines.max_per_user must be a positive integer")
        return v


class CadenceConfig(BaseModel):
    """SPEC-v1.9.md §5 (module `cadence`, R18): the upper bound on a
    weekly-cadence habit's `/cadence <habit> <N>`/`cadence=<N>w` value --
    ISO weeks only have 7 days, so `N` above 7 can never be met (R20's own
    "no cadence shapes other than N times per ISO week", §10)."""

    max_per_week: int = 7

    @field_validator("max_per_week")
    @classmethod
    def _in_range(cls, v: int) -> int:
        if not (1 <= v <= 7):
            raise ValueError("cadence.max_per_week must be between 1 and 7")
        return v


class GraceConfig(BaseModel):
    """SPEC-v1.9.md §5 (module `grace`, R8): `enabled = true` (default)
    turns on the fully-automatic, once-per-ISO-week-per-daily-habit grace
    bridge (`core/grace.py:evaluate_grace`, the nightly tick). `false`
    disables the ENTIRE mechanism -- no bridging, no message, no
    `grace_ledger` writes at all (R8's own "byte-identical to a world
    without grace"), not merely a suppressed message."""

    enabled: bool = True


class PauseConfig(BaseModel):
    """SPEC-v1.9.md §5 (module `pause`, R12): the cap on `/pause
    [<habit>] <duration>`'s span -- a duration (either `<N>d` or `until
    <date>`, resolved to a day count) exceeding this is rejected with
    `pause_too_long` (states the max), no row written."""

    max_days: int = 30

    @field_validator("max_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("pause.max_days must be a positive integer")
        return v


class WrappedConfig(BaseModel):
    """SPEC-v1.9.md §5 (module `wrapped`, R21/R25/R26): `auto_send` gates
    the optional, OFF-by-default (user decision 2026-08-26) month-end
    silent auto-send of the recap card (R26) -- opt-in, consistent with
    this codebase's proactive-send discipline (mirrors `[notifications]
    silent_proactive`'s own "deliberate default, documented" posture, just
    the opposite default direction here). `celebrate_burst` gates the
    emoji-burst appended to a milestone/record celebration line (R25) --
    independent of `auto_send`, same "each knob its own flag, no implicit
    coupling" convention `[gamification] enabled`/`daily_summary`
    established (AC10.4's "no behavioral leakage")."""

    auto_send: bool = False
    celebrate_burst: bool = True


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


class HabitsConfig(BaseModel):
    """SPEC-v1.7.md §5 R-V5: `/addhabit` per-user cap. NAMING NOTE: spec's
    own §6 file-list entry reads "config.py + config.toml -- `[habits]
    max_per_user`", but `Config.habits` (below, `list[HabitConfig]`, the
    `[[habits]]` array-of-tables base catalog) already owns that field
    name -- and TOML itself doesn't allow a `[[habits]]` array-of-tables
    and a sibling `[habits]` table under the *same* key in one document
    (a redefinition). This model therefore mounts on `Config.custom_habits`
    / TOML section `[custom_habits]` instead -- a deliberately DIFFERENT,
    disjoint field/section name (never `Config.habits`), documented here
    so `habitdef`/`main.py` reference `config.custom_habits.max_per_user`,
    not a `config.habits.max_per_user` that would collide with the
    existing list field."""

    max_per_user: int = 20


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
    checkin: CheckinConfig = CheckinConfig()
    nudge: NudgeConfig = NudgeConfig()
    snooze: SnoozeConfig = SnoozeConfig()
    gamification: GamificationConfig = GamificationConfig()
    charts: ChartsConfig = ChartsConfig()
    garmin: GarminConfig = GarminConfig()
    audit: AuditConfig = AuditConfig()
    custom_habits: HabitsConfig = HabitsConfig()
    # SPEC-v1.8.md §2.5/§6 (shared surface): five new sections, all
    # defaulted -- an absent section in config.toml uses the class
    # defaults above (AC-5), same "no existing key changes meaning"
    # posture as every prior config addition in this file.
    notifications: NotificationsConfig = NotificationsConfig()
    quicklog: QuicklogConfig = QuicklogConfig()
    reactions: ReactionsConfig = ReactionsConfig()
    backfill: BackfillConfig = BackfillConfig()
    routines: RoutinesConfig = RoutinesConfig()
    # SPEC-v1.9.md §5/§6 (shared surface): four new sections, all
    # defaulted -- an absent section in config.toml uses the class
    # defaults above (AC5), same posture as every prior config addition
    # in this file. User decisions confirmed 2026-08-26: `/wrapped` bare
    # = last 4 weeks (a `wrapped` module concern, not a config default);
    # `[wrapped] auto_send = false` (opt-in, baked in as the class default
    # above).
    cadence: CadenceConfig = CadenceConfig()
    grace: GraceConfig = GraceConfig()
    pause: PauseConfig = PauseConfig()
    wrapped: WrappedConfig = WrappedConfig()
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
