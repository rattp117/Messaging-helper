"""SPEC-REFACTOR.md Stage 2 (rule 11/AC8): the cycle-free confirmation LEAF
that `core/routing.py` (the typed-log path, `handle_inbound_message`) and
`core/quicklog.py` (the button-tap path, `handle_log_callback`) both import,
replacing the byte-identical mirror the two used to carry independently
(`main.py:_generic_confirmation`/its water-stretch-diary-generic send block
<-> `quicklog.py`'s own private copies -- see that module's pre-Stage-2
docstring for why a mirror, not an import, was the original choice: main.py
couldn't be imported from here without risking a cycle back through
quicklog's own future integration into main.py's routing. Now that this
formatting logic lives in a leaf neither `main.py` nor `quicklog.py`
themselves define, both can import it directly with no cycle at all.

Both callers already resolved `habit`/`value`/`today_str`/`lang` before
calling in -- this module only builds the confirmation TEXT (and its
milestone/record/celebration-burst suffix); it never sends, reacts, or
refreshes a dashboard -- those side effects differ between the two callers
(e.g. only the typed-log path supports backfill or fires a reaction) and
stay owned by each caller.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Callable

from habit_assistant.core import i18n, records, streaks, targets, wrapped
from habit_assistant.llm.prompts import DIARY_REFLECTION_SYSTEM_PROMPT, DIARY_REFLECTION_USER_TEMPLATE

if TYPE_CHECKING:
    from habit_assistant.channels.base import Button, Channel
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.llm.ollama_client import OllamaClient
    from habit_assistant.storage.db import Database


def ordinal(n: int) -> str:
    """"1st"/"2nd"/"3rd"/"Nth" -- shared by every duration-habit confirmation
    (was independently duplicated as `main.py:ordinal` and
    `quicklog.py:_ordinal`, byte-identical copies of each other)."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def generic_confirmation(
    db: "Database",
    llm: "OllamaClient | None",
    habit: "Habit",
    value,
    today_str: str,
    lang: i18n.Language,
    config: "Config",
    user_id: str,
) -> str:
    """Type-generic confirmation for any habit that is NOT one of the three
    built-ins (SPEC-v0.7.md §3.2/§4 R13) -- reached from `confirmation_text`
    below for a non-water/stretch/diary habit. `llm` may be `None` for a
    caller that has already proven its own value can never be a `text`-type
    habit (quicklog's `handle_log_callback` rejects `habit.type == "text"`
    before ever reaching this function, per R-Q6's "no Ollama call anywhere
    in this path")."""
    if habit.type == "numeric":
        total = db.sum_value(user_id, habit.id, today_str)
        unit = habit.unit(lang) or ""
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal:
            pct = round(100 * total / goal) if goal else 0
            return i18n.t(
                "confirm_numeric_goal",
                lang,
                label=habit.label(lang),
                value=value,
                unit=unit,
                total=total,
                goal=goal,
                pct=pct,
            )
        return i18n.t("confirm_numeric_nogoal", lang, label=habit.label(lang), value=value, unit=unit)

    if habit.type == "duration":
        count = db.count(user_id, habit.id, today_str)
        unit = habit.unit(lang) or ""
        return i18n.t(
            "confirm_duration",
            lang,
            label=habit.label(lang),
            value=value,
            unit=unit,
            ordinal=ordinal(count),
            count=count,
        )

    if habit.type == "text":
        reflection = await llm.chat_text(
            DIARY_REFLECTION_SYSTEM_PROMPT.format(language_instruction=i18n.language_instruction(lang)),
            DIARY_REFLECTION_USER_TEMPLATE.format(diary_text=value),
        )
        if not reflection:
            reflection = i18n.t("diary_reflection_fallback", lang)
        return i18n.t("confirm_text", lang, label=habit.label(lang), reflection=reflection)

    # boolean
    status = i18n.t("bool_status_done" if value else "bool_status_not_done", lang)
    return i18n.t("confirm_boolean", lang, label=habit.label(lang), status=status)


async def confirmation_text(
    db: "Database",
    llm: "OllamaClient | None",
    habit: "Habit",
    value,
    today_str: str,
    lang: i18n.Language,
    config: "Config",
    user_id: str,
) -> str:
    """The exact per-habit confirmation TEXT (no prefix/suffix) for a
    successful log -- water/stretch/diary keep their byte-identical v0.6
    catalog entries; any other habit renders via `generic_confirmation`
    (AC9). Byte-identical to the water/stretch/diary/else branches both
    `main.py:handle_inbound_message` and `quicklog.py:_log_and_confirm`
    used to carry inline (rule 11's own mirror pair)."""
    if habit.id == "water":
        water_ml = int(value)
        total = db.water_total_ml(user_id, today_str)
        goal = targets.effective_goal(db, habit, config, user_id)
        pct = round(100 * total / goal) if goal else 0
        return i18n.t("water_confirmation", lang, water_ml=water_ml, total=int(total), goal=goal, pct=pct)

    if habit.id == "stretch":
        stretch_min = int(value)
        count = db.stretch_count(user_id, today_str)
        return i18n.t("stretch_confirmation", lang, stretch_min=stretch_min, ordinal=ordinal(count), count=count)

    if habit.id == "diary":
        diary_text = str(value)
        reflection = await llm.chat_text(
            DIARY_REFLECTION_SYSTEM_PROMPT.format(language_instruction=i18n.language_instruction(lang)),
            DIARY_REFLECTION_USER_TEMPLATE.format(diary_text=diary_text),
        )
        if not reflection:
            reflection = i18n.t("diary_reflection_fallback", lang)
        return i18n.t("diary_confirmation", lang, reflection=reflection)

    return await generic_confirmation(db, llm, habit, value, today_str, lang, config, user_id)


async def send_recovered_confirmation(
    channel: "Channel", chat_id: str, habit: "Habit", value, lang: i18n.Language, buttons: "list[Button]"
) -> None:
    """SPEC-v1.10.md §5 R9 / integration-pass consolidation: the ONE
    recovered-* confirmation sender both `core/routing.py:reparse_pending_
    unparsed` (an ordinary sweep recovery, R3) and `core/clarify.py:
    handle_clarify_callback` (a tap-to-fix reclassify, R9) call. Both used
    to carry an independent, byte-identical mirror of this exact branching
    (`routing.py`'s own inline water/stretch/diary special-cases +
    `_send_recovered_generic`, and `clarify.py`'s own `_send_recovered_
    confirmation`) -- built that way because `core/clarify.py` cannot
    import `core/routing.py` (the reverse import, `routing.py` -> `clarify.
    py`, is what the integration pass adds) without a cycle. Consolidated
    here at integration per Archi's ruling, mirroring this module's own
    SPEC-REFACTOR.md Stage 2 rule-11 rationale for `confirmation_text`/
    `generic_confirmation` above: a leaf neither caller itself defines, so
    both can import it directly with no cycle at all -- one implementation
    to keep in sync, not two."""
    if habit.id == "water":
        await channel.send_actionable(chat_id, i18n.t("recovered_water", lang, water_ml=int(value)), buttons)
    elif habit.id == "stretch":
        await channel.send_actionable(chat_id, i18n.t("recovered_stretch", lang, stretch_min=int(value)), buttons)
    elif habit.id == "diary":
        await channel.send_actionable(chat_id, i18n.t("recovered_diary", lang), buttons)
    elif habit.type == "numeric":
        await channel.send_actionable(
            chat_id,
            i18n.t("recovered_numeric", lang, value=value, unit=habit.unit(lang) or "", label=habit.label(lang)),
            buttons,
        )
    elif habit.type == "duration":
        await channel.send_actionable(
            chat_id,
            i18n.t("recovered_duration", lang, value=value, unit=habit.unit(lang) or "", label=habit.label(lang)),
            buttons,
        )
    elif habit.type == "boolean":
        await channel.send_actionable(chat_id, i18n.t("recovered_boolean", lang, label=habit.label(lang)), buttons)
    else:  # text
        await channel.send_actionable(chat_id, i18n.t("recovered_text", lang, label=habit.label(lang)), buttons)


def suffix(
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    habit: "Habit",
    user_id: str,
    lang: i18n.Language,
    *,
    now_date: date,
    was_qualified_before: bool,
    record_clock: Callable,
    apply: bool = True,
) -> str:
    """Milestone-crossing + broken-record + celebration-burst suffix
    appended to a successful log confirmation -- the other half of rule
    11's mirror pair. `apply=False` reproduces `main.py`'s own backfill
    posture: `records.update_on_log` still runs (stored records/streaks
    stay accurate for the backdated day), but its result is discarded and
    no milestone check happens at all, so the suffix is always empty --
    quicklog has no backfill concept and always passes `apply=True`."""
    milestone_suffix = ""
    if apply and config.gamification.enabled:
        crossed = streaks.crossed_milestone(db, config, habit, now_date, was_qualified_before, user_id)
        if crossed is not None:
            milestone_msg_id = (
                "milestone_reached_weeks" if streaks.streak_unit(db, habit, user_id) == "week" else "milestone_reached"
            )
            milestone_suffix = "\n\n" + i18n.t(milestone_msg_id, lang, streak=crossed, label=habit.label(lang))

    broken_records = records.update_on_log(db, config, registry, habit, user_id, clock=record_clock)
    record_suffix = ""
    if apply and broken_records:
        record_unit = streaks.streak_unit(db, habit, user_id)
        record_suffix = "\n\n" + records.format_celebration(broken_records, habit, lang, record_unit)

    result = milestone_suffix + record_suffix
    if result:
        burst = wrapped.celebration_burst(config, lang)
        if burst:
            result += "\n" + burst
    return result
