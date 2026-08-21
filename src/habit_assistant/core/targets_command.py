"""Per-habit target set/show/clear execution (SPEC-v1.1.md §4 R-T10, module
`targets`). `core/commands.dispatch` (R-T7) only recognizes the *shape* of
a `/target ...` / anchored-NL command and produces a `Command`; this module
is where that `Command` actually gets validated against the live
`HabitRegistry`/DB and turned into a bilingual reply -- and, for `set`/
`clear`, where the DB write happens.

Every branch below returns a plain string (the reply) and never raises --
same "structured op in, formatted string out, no traceback to the user"
contract as `core/query.py.answer_question`. A DB write failure is caught,
logged, and reported via the generic `target_save_failed` catalog message
(§3.5/AC28), never a stack trace.

`execute_target` is also the landing spot for a *validated* full-NL target
intent (SPEC-v1.1.md R-T13.3): `core/target_nl.classify_target_intent`
produces a `TargetIntent`, and `main.py`'s integration wiring converts that
into a `Command(kind="target", target_action="set", category=..., value_num=...)`
and calls this same function -- there is exactly one `set` code path for
both the deterministic and full-NL entry points (no divergent second
implementation).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from habit_assistant.core import i18n
from habit_assistant.core import targets

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


def _example_goal_value(habit: "Habit") -> str:
    """A plausible positive example for the `target_invalid_value` reply's
    "e.g. ..." clause -- the habit's own configured goal if it has one
    (formatted without a trailing ".0"), else a type-appropriate generic
    (20 for a duration habit's minutes, 2000 otherwise)."""
    if habit.goal:
        return f"{habit.goal:g}"
    return "20" if habit.type == "duration" else "2000"


def _format_previous(previous_goal: float | None, unit: str, lang: i18n.Language) -> str:
    """The `{previous}` clause in `target_set`'s "(was ...)" -- a plain,
    already-formatted string (the catalog template interpolates it
    verbatim, with no `:g`/unit formatting of its own), so both the
    "had a goal" and "had no goal at all" (a previously goal-less habit,
    R-T5b) cases are handled here."""
    if previous_goal is None:
        return "none" if lang == "en" else "ไม่มี"
    return f"{previous_goal:g} {unit}".strip()


def _default_note(effective_goal: float, default_goal: float | None, unit: str, lang: i18n.Language) -> str:
    """R-T10: the "(default ...)" clause on `show`/`show_all`, shown only
    when an override is active and actually differs from the config
    default. Deliberately empty (not a "(default: none)" clause) when the
    habit has no config default at all -- the shared-surface `i18n.py`
    catalog's `target_show_default_note` template only supports a numeric
    `{default:g}`, so a habit with no config base (e.g. a duration habit
    that had no goal until a target was set on it, R-T5b) simply gets no
    default clause rather than risking a `.format()` crash on `None`. See
    IMPL-v1.1-targets.md's "Known limitations" for the (cosmetic-only)
    deviation from SPEC-v1.1.md §3.4's illustrative "(default: none)" text."""
    if default_goal is not None and effective_goal != default_goal:
        return i18n.t("target_show_default_note", lang, default=default_goal, unit=unit)
    return ""


def _render_show(db: "Database", config: "Config", habit: "Habit", lang: i18n.Language) -> str:
    goal = targets.effective_goal(db, habit, config)
    if goal is None:
        # A goal-able habit with no override AND no config default (e.g.
        # `stretch` before any target has ever been set on it). No
        # dedicated "show, single habit, no goal" catalog key exists
        # (SPEC-v1.1.md §3.4 only illustrates this for `show_all`) -- reuse
        # `target_show_all_line_nogoal` rather than adding a new key to the
        # frozen shared-surface i18n catalog (see "Known limitations").
        return i18n.t("target_show_all_line_nogoal", lang, label=habit.label(lang))
    unit = habit.unit(lang) or ""
    default_goal = targets.config_goal(habit, config)
    default_note = _default_note(goal, default_goal, unit, lang)
    return i18n.t("target_show", lang, label=habit.label(lang), goal=goal, unit=unit, default_note=default_note)


def _render_show_all(db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language) -> str:
    lines = [i18n.t("target_show_all_header", lang)]
    for habit in registry:
        goal = targets.effective_goal(db, habit, config)
        if goal is None:
            lines.append(i18n.t("target_show_all_line_nogoal", lang, label=habit.label(lang)))
            continue
        unit = habit.unit(lang) or ""
        default_goal = targets.config_goal(habit, config)
        default_note = _default_note(goal, default_goal, unit, lang)
        lines.append(
            i18n.t(
                "target_show_all_line", lang, label=habit.label(lang), goal=goal, unit=unit, default_note=default_note
            )
        )
    return "\n".join(lines)


def _execute_clear(db: "Database", config: "Config", habit: "Habit", lang: i18n.Language) -> str:
    default_goal = targets.config_goal(habit, config)
    try:
        db.clear_target(habit.id)
    except Exception:
        logger.exception("Failed to clear target for habit %r", habit.id)
        return i18n.t("target_save_failed", lang)

    if default_goal is None:
        return i18n.t("target_cleared_nogoal", lang, label=habit.label(lang))
    unit = habit.unit(lang) or ""
    return i18n.t("target_cleared", lang, label=habit.label(lang), default=default_goal, unit=unit)


def _execute_set(db: "Database", config: "Config", habit: "Habit", value_num: float | None, lang: i18n.Language) -> str:
    if value_num is None or value_num <= 0:
        return i18n.t(
            "target_invalid_value", lang, habit_id=habit.id, example=_example_goal_value(habit)
        )

    previous_goal = targets.effective_goal(db, habit, config)
    unit = habit.unit(lang) or ""
    previous_text = _format_previous(previous_goal, unit, lang)

    try:
        db.set_target(habit.id, value_num)
    except Exception:
        logger.exception("Failed to set target for habit %r", habit.id)
        return i18n.t("target_save_failed", lang)

    return i18n.t("target_set", lang, label=habit.label(lang), goal=value_num, unit=unit, previous=previous_text)


async def execute_target(
    command: "Command", *, db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language
) -> str:
    """Validate and perform the target op described by `command`
    (`core/commands.dispatch`'s `kind="target"` output, or an equivalent
    `Command` built from a validated `core/target_nl.TargetIntent`), and
    return the bilingual reply. Never raises (R-T10/§3.5): every failure
    mode -- unknown habit, non-goalable habit, invalid value, a DB write
    error -- resolves to a friendly catalog message."""
    if command.target_action is None or command.target_action == "usage":
        return i18n.t("target_usage", lang)

    if command.target_action == "show_all":
        return _render_show_all(db, config, registry, lang)

    habit_id = command.category or ""
    habit = registry.get(habit_id)
    if habit is None:
        return i18n.t("target_invalid_habit", lang, habit_id=habit_id, habit_list=", ".join(registry.ids()))

    if not targets.is_goalable(habit):
        return i18n.t("target_not_goalable", lang, label=habit.label(lang))

    if command.target_action == "show":
        return _render_show(db, config, habit, lang)
    if command.target_action == "clear":
        return _execute_clear(db, config, habit, lang)
    if command.target_action == "set":
        return _execute_set(db, config, habit, command.value_num, lang)

    return i18n.t("target_usage", lang)  # defensive, unreachable given Command's Literal type
