"""The habit registry (ROADMAP.md v0.7.0 "Multi-Habit Extensibility",
SPEC-v0.7.md §5): turns `Config.habits` (data) into the `Habit`/
`HabitRegistry` objects the rest of the app is generated from -- the
extraction schema/prompt, per-type validation, storage, reminders,
confirmations, and review.

No channel imports here (SPEC.md §8's seam) -- this module only knows
about config in, `Habit`/`HabitRegistry` out.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from habit_assistant.core import i18n
from habit_assistant.storage.models import LogEntry

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.llm.ollama_client import ExtractionResult

# The three habits shipped through v0.6.0, generalized (SPEC-v0.7.md §3.1):
# these ids reuse their existing v0.6 i18n catalog entries verbatim for
# confirmations/reminders/stats, guaranteeing byte-identical output (AC7.1/
# AC9), rather than reconstructing that copy from the type-generic templates
# every other habit uses.
BUILTIN_IDS: frozenset[str] = frozenset({"water", "stretch", "diary"})


@dataclass(frozen=True, slots=True)
class Habit:
    """A single configured habit, flattened out of `HabitConfig` (which
    carries `HabitLabel` sub-models) into plain fields -- the shape every
    other module (parser, reminders, review, main) is generated against."""

    id: str
    type: str  # 'numeric' | 'duration' | 'text' | 'boolean'
    label_en: str
    label_th: str
    unit_en: str | None
    unit_th: str | None
    goal: float | None
    reminder_times: tuple[str, ...]
    reminder_text_en: str | None
    reminder_text_th: str | None
    unit_aliases: dict[str, float]
    # ROADMAP.md v0.9.0 AC9.1/AC9.4: defaults True so every pre-v0.9 direct
    # `Habit(...)` construction (test helpers, etc.) keeps working unchanged.
    skip_if_goal_met: bool = True

    def label(self, lang: i18n.Language) -> str:
        return self.label_th if lang == "th" else self.label_en

    def unit(self, lang: i18n.Language) -> str | None:
        return self.unit_th if lang == "th" else self.unit_en

    def reminder_text(self, lang: i18n.Language) -> str | None:
        return self.reminder_text_th if lang == "th" else self.reminder_text_en


class HabitRegistry:
    """Ordered collection of `Habit`s, built once from `Config.habits` at
    startup (config is not hot-reloaded, SPEC-v0.7.md §10). Threaded
    through the parser, reminders, review, and main.py's dispatch instead
    of each of those modules reading `Config.habits` directly."""

    def __init__(self, habits: Sequence[Habit]) -> None:
        self._habits: list[Habit] = list(habits)
        self._by_id: dict[str, Habit] = {h.id: h for h in self._habits}

    @classmethod
    def from_config(cls, config: "Config") -> "HabitRegistry":
        habits = [
            Habit(
                id=h.id,
                type=h.type,
                label_en=h.label.en,
                label_th=h.label.th,
                unit_en=h.unit.en if h.unit is not None else None,
                unit_th=h.unit.th if h.unit is not None else None,
                goal=h.goal,
                reminder_times=tuple(h.reminder_times),
                reminder_text_en=h.reminder_text.en if h.reminder_text is not None else None,
                reminder_text_th=h.reminder_text.th if h.reminder_text is not None else None,
                unit_aliases=dict(h.unit_aliases),
                skip_if_goal_met=h.skip_if_goal_met,
            )
            for h in config.habits
        ]
        return cls(habits)

    def get(self, habit_id: str) -> Habit | None:
        return self._by_id.get(habit_id)

    def ids(self) -> list[str]:
        return [h.id for h in self._habits]

    def category_enum(self) -> list[str]:
        """Every configured habit id, plus the reserved `"unknown"`
        category -- the extraction schema's `category` enum
        (SPEC-v0.7.md §4 R4)."""
        return self.ids() + ["unknown"]

    def __iter__(self) -> Iterator[Habit]:
        return iter(self._habits)

    def __len__(self) -> int:
        return len(self._habits)


def log_entry_from_result(
    habit: Habit, result: "ExtractionResult", ts: str, raw_message: str, source: str, user_id: str
) -> LogEntry:
    """Map a validated `ExtractionResult` to a `LogEntry` per the matched
    habit's type (SPEC-v0.7.md §4 R10): numeric/duration -> `value_num`;
    text -> `value_text`; boolean -> `value_num` of 1.0/0.0. `habit_type`
    is always stamped from the habit's own config, not inferred.
    SPEC-v1.2.md R-D1: `user_id` is the owning chat id, stamped onto every
    new row -- the caller (`main.py`) is the acting user for a live log,
    or the deferred row's own `user_id` for a recovery re-parse."""
    if habit.type in ("numeric", "duration"):
        value_num: float | None = float(result.value)  # type: ignore[arg-type]
        value_text: str | None = None
    elif habit.type == "text":
        value_num = None
        value_text = str(result.value)
    else:  # boolean
        value_num = 1.0 if result.value else 0.0
        value_text = None

    return LogEntry(
        id=None,
        user_id=user_id,
        ts=ts,
        category=habit.id,
        value_num=value_num,
        value_text=value_text,
        raw_message=raw_message,
        source=source,
        habit_type=habit.type,
    )
