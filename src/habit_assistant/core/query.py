"""Natural-language queries (ROADMAP.md v0.8.0 "Natural-Language Queries",
AC8.1-AC8.5): "how much water this week?" / "อาทิตย์นี้ยืดกี่ครั้ง" answered
on demand from the DB the app already writes.

`core/commands.py`'s `dispatch()` decides, cheaply and LLM-free, that a
message *looks like* a question (AC8.1-8.2's routing requirement: "explicit
commands -> query intent -> extractor", never the other way -- a plain log
like "500ml" must never reach here). Once it has, `main.py` calls
`answer_question` below, which:

1. Classifies the free-text question into a structured `{category, metric,
   timeframe}` via the LLM (`classify_query_intent`), reusing
   `OllamaClient.chat_json`'s existing model-fallback-chain machinery
   (`llm/ollama_client.py`) with a query-specific schema/prompt
   (`llm/prompts.build_query_intent_system_prompt`), exactly the same
   "structured JSON + strict code-side validation" shape `core/parser.py`
   already uses for extraction.
2. Validates the result strictly against the live `HabitRegistry` and the
   known `METRICS`/`TIMEFRAMES` vocabularies (`_validate_intent`) -- any
   habit id outside the registry, or a value outside those two closed
   vocabularies, is rejected. Combined with the LLM being explicitly told to
   answer category "unknown" when a question isn't about a tracked habit or
   it can't confidently classify one, this is how AC8.4 ("unconfigured habit
   or unparseable question -> friendly can't-answer, never a wrong number,
   never a crash") is enforced: every failure mode collapses to `None` here,
   and `answer_question` turns that into the `query_cant_answer` catalog
   message rather than ever guessing.
3. Maps the validated timeframe to a concrete, timezone-aware
   (`Asia/Bangkok` by default, `config.app.timezone`) list of 'YYYY-MM-DD'
   day strings (`date_range_for_timeframe`) and aggregates over them using
   the *existing* generic, read-only `Database.sum_value`/`count`/
   `count_true` helpers (`storage/db.py`, unchanged) -- summed in Python
   across the day range the same way `core/review.py`'s weekly stats already
   do. Nothing in this module ever calls `insert_log`/`soft_delete`/
   `update_value`/`reclassify_log` -- AC8.5's "strictly read-only" is true
   by construction, not by convention.
4. Formats a bilingual answer via `core/i18n.py`'s catalog, per-type
   (`_format_answer`): numeric -> a total with its unit (or a log count, if
   the caller asked "how many times"); duration -> count *and* total
   minutes together (matches how a duration habit's own weekly-review line
   already reads, `core/review.py`'s `stats_generic_duration_summary`);
   boolean -> done-day count; text -> entry count.

Timeframe semantics (AC8.3 -- documented here since it's not obvious from
the names alone): "today"/"yesterday" are single calendar days. "this_week"
and "last_7_days" are DELIBERATELY THE SAME range -- the 7 days ending today
inclusive (today-6 .. today) -- matching `core/review.py`'s own definition
of "this week" (a rolling 7-day window, not a Mon-Sun ISO calendar week).
The app has no calendar-week concept anywhere else, and AC8.1 itself
describes a "this week" water query as expecting "the correct 7-day sum" --
so query timeframes reuse the review module's existing convention rather
than inventing a second, incompatible definition of "week".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from habit_assistant.core import i18n
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import build_query_intent_system_prompt, build_query_intent_user_prompt

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# Closed vocabularies (AC8.3): anything the LLM returns outside these is
# rejected by `_validate_intent`, same fail-closed contract `core/parser.py`
# applies to a habit category outside the configured registry.
METRICS: tuple[str, ...] = ("sum", "count")
TIMEFRAMES: tuple[str, ...] = ("today", "yesterday", "this_week", "last_7_days")

_TIMEFRAME_LABEL_MSG_ID: dict[str, str] = {
    "today": "query_timeframe_today",
    "yesterday": "query_timeframe_yesterday",
    "this_week": "query_timeframe_this_week",
    "last_7_days": "query_timeframe_last_7_days",
}


@dataclass(slots=True)
class QueryIntent:
    """A validated `{habit_id, metric, timeframe}` triple -- every field is
    already known-good by the time this is constructed (see
    `_validate_intent`): `habit_id` is a real configured habit id, `metric`
    is one of `METRICS`, `timeframe` is one of `TIMEFRAMES`."""

    habit_id: str
    metric: str
    timeframe: str


def build_query_intent_schema(category_enum: list[str]) -> dict:
    """The query-intent JSON schema, generated from the live registry's
    `category_enum()` (habit ids + "unknown", same convention
    `build_extraction_schema` uses) so its `category` enum always matches
    what's actually configured. `metric`/`timeframe` are fixed, closed
    enums (`METRICS`/`TIMEFRAMES`) -- they don't grow with habit count."""
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(category_enum)},
            "metric": {"type": "string", "enum": list(METRICS)},
            "timeframe": {"type": "string", "enum": list(TIMEFRAMES)},
        },
        "required": ["category", "metric", "timeframe"],
    }


def _validate_intent(data: object, registry: "HabitRegistry") -> QueryIntent | None:
    """Strict validation (AC8.4): `category` must be a real configured
    habit id (NOT "unknown" -- that's the LLM's own "I can't tell" signal,
    and is rejected here exactly like any other unrecognized value);
    `metric`/`timeframe` must be members of the closed vocabularies. Any
    mismatch (including a malformed/non-dict payload) returns None, which
    the caller turns into the "can't answer" message -- never a guess."""
    if not isinstance(data, dict):
        return None
    habit_id = data.get("category")
    metric = data.get("metric")
    timeframe = data.get("timeframe")
    if habit_id not in registry.ids():
        return None
    if metric not in METRICS:
        return None
    if timeframe not in TIMEFRAMES:
        return None
    return QueryIntent(habit_id=habit_id, metric=metric, timeframe=timeframe)


async def classify_query_intent(text: str, llm: OllamaClient, registry: "HabitRegistry") -> QueryIntent | None:
    """Classify `text` into a validated `QueryIntent`, or None on any
    failure (transport/HTTP error, unparseable JSON, an off-schema/
    unrecognized category, or a category/metric/timeframe that fails
    `_validate_intent`) -- never raises, matching `core/parser.py.
    parse_message`'s fail-closed contract."""
    system_prompt = build_query_intent_system_prompt(registry)
    user_prompt = build_query_intent_user_prompt(text)
    schema = build_query_intent_schema(registry.category_enum())
    valid_categories = set(registry.category_enum())

    try:
        raw_json = await llm.chat_json(system_prompt, user_prompt, schema, valid_categories)
        if raw_json is None:
            return None
        data = json.loads(raw_json)
        return _validate_intent(data, registry)
    except Exception:
        logger.exception("Query intent classification failed; failing closed to can't-answer")
        return None


def _today_in_timezone(clock, tz_name: str) -> date:
    """AC8.3: timezone-aware regardless of the host's own local timezone --
    a naive `clock()` (the app's existing convention, e.g. `datetime.now`
    and every test's `fixed_clock` fixture) is treated as already being in
    `tz_name`; an aware one is converted to it. Only the resulting calendar
    *date* is used from here on, matching `core/review.py`'s day-string
    aggregation."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.date()


def date_range_for_timeframe(timeframe: str, today: date) -> list[str]:
    """AC8.3: map a validated timeframe id to its 'YYYY-MM-DD' day strings.
    "this_week"/"last_7_days" are intentionally identical -- see the module
    docstring's "Timeframe semantics" note."""
    if timeframe == "today":
        return [today.isoformat()]
    if timeframe == "yesterday":
        return [(today - timedelta(days=1)).isoformat()]
    if timeframe in ("this_week", "last_7_days"):
        return [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    raise ValueError(f"unknown timeframe: {timeframe!r}")  # unreachable past _validate_intent


def _aggregate(db: "Database", habit: "Habit", day_strs: list[str], user_id: str) -> tuple[float, int]:
    """(total, count) over `day_strs` for `user_id`, both computed
    generically from the existing read-only `Database` helpers (AC8.5: no
    write method is ever called anywhere in this module). `total` is
    meaningful for numeric/duration habits; `count` means "log rows" for
    numeric/duration/text, and "done days" (not raw rows) for boolean,
    matching `core/review.py._compute_habit_stats`'s own boolean
    semantics. SPEC-v1.2.md R-D3: scoped throughout (AC-U-ISO)."""
    if habit.type in ("numeric", "duration"):
        total = sum(db.sum_value(user_id, habit.id, d) for d in day_strs)
        count = sum(db.count(user_id, habit.id, d) for d in day_strs)
        return total, count
    if habit.type == "boolean":
        done_days = sum(1 for d in day_strs if db.count_true(user_id, habit.id, d) > 0)
        return 0.0, done_days
    # text
    count = sum(db.count(user_id, habit.id, d) for d in day_strs)
    return 0.0, count


def _format_answer(habit: "Habit", metric: str, total: float, count: int, lang: i18n.Language, timeframe: str) -> str:
    label = habit.label(lang)
    if habit.type == "numeric":
        if metric == "count":
            return i18n.t("query_answer_numeric_count", lang, label=label, count=count, timeframe=timeframe)
        return i18n.t(
            "query_answer_numeric_sum", lang, label=label, total=total, unit=habit.unit(lang) or "", timeframe=timeframe
        )
    if habit.type == "duration":
        return i18n.t(
            "query_answer_duration",
            lang,
            label=label,
            count=count,
            total=total,
            unit=habit.unit(lang) or "",
            timeframe=timeframe,
        )
    if habit.type == "boolean":
        return i18n.t("query_answer_boolean", lang, label=label, count=count, timeframe=timeframe)
    return i18n.t("query_answer_text", lang, label=label, count=count, timeframe=timeframe)


async def answer_question(
    text: str,
    *,
    db: "Database",
    llm: OllamaClient,
    registry: "HabitRegistry",
    config: "Config",
    lang: i18n.Language,
    user_id: str,
    clock=datetime.now,
) -> str:
    """Top-level entry point `main.py` calls once `core/commands.dispatch`
    has flagged a message as query-shaped. Always returns a string (the
    answer, or the bilingual `query_cant_answer` fallback) -- never raises,
    never writes to the DB (AC8.5). SPEC-v1.2.md R-D3: `user_id` scopes
    every aggregation -- a query only ever answers from the asking user's
    own data (AC-U-ISO)."""
    try:
        intent = await classify_query_intent(text, llm, registry)
        if intent is None:
            return i18n.t("query_cant_answer", lang)
        habit = registry.get(intent.habit_id)
        if habit is None:  # defensive; _validate_intent already checked registry.ids()
            return i18n.t("query_cant_answer", lang)

        today = _today_in_timezone(clock, config.app.timezone)
        day_strs = date_range_for_timeframe(intent.timeframe, today)
        total, count = _aggregate(db, habit, day_strs, user_id)
        timeframe_label = i18n.t(_TIMEFRAME_LABEL_MSG_ID[intent.timeframe], lang)
        return _format_answer(habit, intent.metric, total, count, lang, timeframe_label)
    except Exception:
        logger.exception("Query answer failed unexpectedly; failing closed to can't-answer")
        return i18n.t("query_cant_answer", lang)
