"""One-tap quick-log inline keyboard (SPEC-v1.8.md §4 Feature "quicklog",
R-Q1-R-Q3/R-Q6): `/log` (+ Thai alias `บันทึก`) builds a bilingual inline
keyboard from the TAPPING user's own per-user registry
(`RegistryProvider.for_user`, resolved by the caller -- `build_keyboard`
below takes the already-resolved `registry`, mirroring every other
registry-generic module in this app), and `handle_log_callback` is the
`on_callback` body for a `log:<habit>:<value>` tap.

This module is self-contained (does not touch `main.py`, SPEC-v1.8.md's own
"integration seam" note) -- it owns no part of the `/log` routing or the
`log:` prefix dispatch in `on_callback`, both of which are `main.py`'s
later, sequential integration step (SPEC-v1.8.md §11 "Integration order").
`build_keyboard`/`handle_log_callback` are written to be droppable into
that seam per SPEC-v1.8.md §5's exact signatures.

R-Q2's "reuses the shared confirmation path -- no second confirmation
formatter": `_log_and_confirm`/`_generic_confirmation` below MIRROR
`main.py:handle_inbound_message`'s own water/stretch/generic confirmation
branches (and `main.py:_generic_confirmation`'s numeric/duration/boolean
cases) line-for-line -- the same "byte-identical copy, not an import"
precedent `core/undo_ui.py`'s own docstring already established for this
codebase (a new module importing a PRIVATE main.py function would risk a
circular import once main.py's integration step later imports THIS module
too; a public, independently-tested mirror is the established pattern
instead). `tests/test_quicklog.py`'s own byte-identical assertions (mirrors
`tests/test_undo_ui.py`'s AC11 suite) are what guarantee the two never
silently drift apart.

No channel import beyond the `Channel` ABC (SPEC.md §8's seam) -- mirrors
`core/undo_ui.py`'s/`core/reminders.py`'s own import shape.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from habit_assistant.channels.base import Button, Channel
from habit_assistant.core import dashboard, i18n, reactions, records, streaks, targets, undo_ui, user_prefs
from habit_assistant.storage.models import LogEntry

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Small pure helpers (no DB/channel access) shared by the keyboard builder
# and the confirmation formatter below.
# ---------------------------------------------------------------------------


def _ordinal(n: int) -> str:
    """Byte-identical copy of `main.py:ordinal` (see this module's own
    docstring for why a copy, not an import) -- "1st"/"2nd"/"3rd"/"Nth"."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_amount(value: float) -> str:
    """Compact numeric rendering for a button label/callback payload --
    "500" for a whole number, "0.5" for a fraction (never "500.0").

    Fixed-point, not `%g`/`{:g}`: `%g`'s default 6-significant-digit
    precision switches to scientific notation for a large non-integer
    (e.g. "1.23457e+08" for a huge fractional goal's exact-G rung,
    TEST-v1.8-quicklog.md finding #4) -- a payload `_LOG_CALLBACK_RE`
    cannot match, rendering a dead button. `_LOG_CALLBACK_RE`'s own value
    grammar caps the fractional part at 6 digits, so `.6f` (trimmed of
    trailing zeros) is the matching format on the write side too."""
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# R-Q1: keyboard construction, from an already-resolved per-user registry.
# ---------------------------------------------------------------------------


def _round_ladder_step(x: float) -> float:
    """One rung of the goal-derived ladder (R-Q1's "[round¼G, round½G,
    G]"): nearest integer, floored at 1 so a small POSITIVE goal (e.g.
    G=2) can never produce a useless 0-amount button.

    The floor only applies when `x` itself is positive but rounds down to
    0 -- it must NOT also swallow a negative `x` (a config-authored
    negative goal, TEST-v1.8-quicklog.md finding #3) into a spurious
    positive rung. A non-positive `x` is returned as-is (still
    non-positive), so `_goal_ladder`'s own `value > 0` guard filters it
    out -- the same "no usable goal -> no button" outcome as a goal-less
    habit, per R-Q1's "if neither, the habit is skipped"."""
    rounded = round(x)
    if rounded > 0:
        return float(rounded)
    if x > 0:
        return 1.0
    return float(rounded)


def _goal_ladder(goal: float) -> list[float]:
    """R-Q1: "a derived ladder [round¼G, round½G, G]" for a goal-bearing
    habit with no unit aliases. `goal` itself is kept exact (a fractional
    configured/target goal is a legitimate tap amount) -- only the two
    intermediate rungs are rounded. De-duplicated (a small goal can make
    ¼G/½G round to the same rung, or to G itself) and returned ascending."""
    candidates = [_round_ladder_step(goal / 4), _round_ladder_step(goal / 2), float(goal)]
    seen: set[float] = set()
    ladder: list[float] = []
    for value in candidates:
        if value > 0 and value not in seen:
            seen.add(value)
            ladder.append(value)
    return sorted(ladder)


def _amount_candidates(habit: "Habit", config: "Config", db: "Database", user_id: str) -> list[float]:
    """R-Q1's own precedence: aliases first (a habit that has BOTH aliases
    and a goal uses its aliases, not the goal ladder), else a goal-derived
    ladder, else no buttons at all (the habit is skipped). Both shapes are
    sorted ascending and capped to `config.quicklog.max_buttons_per_habit`
    (the smallest/most-granular amounts win a cap, matching R-Q1's own
    "sorted-unique ... capped at" reading)."""
    cap = config.quicklog.max_buttons_per_habit
    if habit.unit_aliases:
        amounts = sorted({float(v) for v in habit.unit_aliases.values() if v > 0})
        return amounts[:cap]

    # R-Q1: "an effective goal G" -- resolves a stored `/target` override
    # the same way every other confirmation/streak check in this app does
    # (`targets.effective_goal`), not the raw config default alone.
    goal = targets.effective_goal(db, habit, config, user_id)
    if goal:
        return _goal_ladder(goal)[:cap]
    return []


def _buttons_for_habit(habit: "Habit", config: "Config", db: "Database", lang: i18n.Language, user_id: str) -> list[Button]:
    if habit.type == "boolean":
        return [(i18n.t("quicklog_done_button", lang), f"log:{habit.id}:1")]
    if habit.type != "numeric" and habit.type != "duration":
        return []  # R-Q1: text habits are omitted -- a tap can't carry free text.

    amounts = _amount_candidates(habit, config, db, user_id)
    if not amounts:
        return []  # R-Q1: "if neither, the habit is skipped".

    emoji = reactions.emoji_for_habit(habit)
    unit = habit.unit(lang) or ""
    return [
        (f"{emoji} {_format_amount(amount)}{unit}", f"log:{habit.id}:{_format_amount(amount)}") for amount in amounts
    ]


def build_keyboard(
    registry: "HabitRegistry", config: "Config", db: "Database", lang: i18n.Language, user_id: str
) -> list[Button]:
    """R-Q1: the `/log`/`บันทึก` inline keyboard, built from `registry`
    (the caller's already-resolved `provider.for_user(chat_id)` -- SPEC-
    v1.8.md §5's exact interface). Registry order in, button order out.
    An empty result (no habit contributed a button -- every configured
    habit is text-typed, or a numeric/duration habit has neither aliases
    nor a goal) means the caller should send `empty_keyboard_hint(lang)`
    instead of an inline keyboard (R-Q1's own "friendly hint" contract)."""
    buttons: list[Button] = []
    for habit in registry:
        buttons.extend(_buttons_for_habit(habit, config, db, lang, user_id))
    return buttons


def empty_keyboard_hint(lang: i18n.Language) -> str:
    """R-Q1: "Empty registry of loggable habits -> a friendly hint reply
    (referencing /addhabit)"."""
    return i18n.t("quicklog_empty", lang)


def keyboard_prompt_text(lang: i18n.Language) -> str:
    """The message text `/log`'s keyboard is attached to (SPEC-v1.8.md
    §3.1: "/log -> an inline keyboard (one send)") -- kept here so the
    integration step never has to invent this copy inline."""
    return i18n.t("quicklog_prompt", lang)


# ---------------------------------------------------------------------------
# R-Q2/R-Q3: the `log:<habit>:<value>` callback_query handler.
# ---------------------------------------------------------------------------

# SPEC-v1.8.md §2.1: "log:<habit_id>:<value_base_unit>". Habit ids are
# `^[a-z0-9_]+$`, <=32 chars (v1.7 R-V1, `config._HABIT_ID_RE`/habitdef's
# own bound) -- mirrored here as a literal char class + length bound
# rather than importing that private regex, same "shape-only, no cross-
# module coupling for a regex" posture `commands.py`'s own matchers take.
# The value carries an optional sign/decimal so a malformed/negative
# payload still reaches the bounds check below (and is rejected there,
# not silently un-matched) -- mirrors `undo_ui`'s own two-stage "regex
# shape, then a numeric bounds check" discipline.
# `re.ASCII` (TEST-v1.8-quicklog.md finding #2): a bare `\d` matches any
# Unicode decimal-digit character (category Nd), not just ASCII 0-9 --
# without this flag, a forged callback_data using Arabic-Indic/Thai/
# fullwidth digits (e.g. "log:water:๕๐๐") would slip past this "shape-
# only" check AND `float()`, producing a real, unvalidated log write.
_LOG_CALLBACK_RE = re.compile(r"^log:(?P<habit>[a-z0-9_]{1,32}):(?P<value>-?\d{1,15}(?:\.\d{1,6})?)$", re.ASCII)

# Defensive upper bound on the parsed value -- no legitimate button this
# module ever generates can produce anything close to this (aliases/goal
# ladders are ordinary habit amounts), so a payload beyond it can only be
# a forged/hostile callback_data, treated the same as a regex mismatch
# (R-Q3's "malformed / out-of-range payload -> logged and ignored, no
# read/write"). Mirrors `undo_ui._SQLITE_MAX_INTEGER`'s own "bound
# hostile-but-syntactically-valid input before any DB call" rationale.
_MAX_LOG_VALUE = 1_000_000_000.0


def _stored_language_pref(db: "Database", user_id: str) -> str:
    """SPEC-v1.8.md integration step: thin alias for the shared
    `core/user_prefs.stored_language_pref` -- see that module's own
    docstring (Archi-approved consolidation of what was, as of
    TEST-v1.8-quicklog.md's round-2 note, a FOURTH independent per-file
    copy of this exact lookup). Fixed TEST-v1.8-quicklog.md finding #1:
    without this, `handle_log_callback` could never honor a tapping
    user's stored `/lang` preference, unlike every other reply path."""
    return user_prefs.stored_language_pref(db, user_id)


async def handle_log_callback(
    chat_id: str,
    data: str,
    source_text: str,
    callback_id: str,
    *,
    db: "Database",
    channel: Channel,
    config: "Config",
    registry: "HabitRegistry",
    clock=datetime.now,
) -> None:
    """The `on_callback` body for a `log:<habit>:<value>` tap (SPEC-v1.8.md
    §5). `TelegramChannel.run` always calls `answerCallbackQuery(
    callback_id)` itself right after awaiting this (mirrors `undo_ui.
    handle_undo_callback`'s own note) -- this function never needs to call
    it (kept as a parameter only to match the `on_callback` callable
    shape).

    R-Q3: `data` that isn't `log:<habit>:<value>`-shaped, or whose value is
    out of bounds for the resolved habit's type, is logged and ignored --
    no DB read beyond the regex/registry lookup, no DB write, no send. A
    `habit_id` that IS shaped correctly but isn't in `registry` (i.e. not
    one of the TAPPING user's own habits -- e.g. another user's custom
    habit, or a habit since deleted) gets a friendly no-op reply instead,
    no write."""
    match = _LOG_CALLBACK_RE.match(data)
    if match is None:
        logger.info("Ignoring malformed log callback_query data: %r", data)
        return

    habit_id = match.group("habit")
    value = float(match.group("value"))
    if abs(value) > _MAX_LOG_VALUE:
        logger.info("Ignoring log callback_query data with an out-of-range value: %r", data)
        return

    lang = i18n.resolve_reply_language(source_text, config, user_pref=_stored_language_pref(db, chat_id))

    # R-Q3: resolved against the TAPPING user's own registry ONLY --
    # `registry` is already scoped to `chat_id` by the caller (mirrors
    # `undo_ui.handle_undo_callback`'s own `registry` parameter), so a
    # habit_id this chat doesn't own simply isn't present here.
    habit = registry.get(habit_id)
    if habit is None:
        await channel.send(chat_id, i18n.t("quicklog_unknown_habit", lang))
        return

    if habit.type == "text":
        # R-Q1: text habits never get a quick-log button in the first
        # place, so a payload naming one cannot come from a legitimate
        # tap -- same "no legitimate origin -> silent ignore" bucket as a
        # regex mismatch, not the "friendly no-op" bucket (that one is
        # reserved for an otherwise-valid habit id this user doesn't own).
        logger.info("Ignoring log callback_query data naming a non-quick-loggable (text) habit: %r", data)
        return

    if habit.type == "boolean":
        if value != 1:
            logger.info("Ignoring log callback_query data with an invalid boolean value: %r", data)
            return
    elif value <= 0:
        logger.info("Ignoring log callback_query data with a non-positive value for %s: %r", habit_id, data)
        return

    await _log_and_confirm(db, channel, config, clock, registry, lang, habit, value, chat_id, data)


# ---------------------------------------------------------------------------
# R-Q2: the shared "write the log + send the exact typed-path confirmation"
# implementation. See this module's own docstring for why this MIRRORS
# `main.py:handle_inbound_message`'s water/stretch/generic branches rather
# than importing them.
# ---------------------------------------------------------------------------


def _generic_confirmation(db: "Database", habit: "Habit", value: float, today_str: str, lang: i18n.Language, config: "Config", user_id: str) -> str:
    """Mirrors `main.py:_generic_confirmation`'s numeric/duration/boolean
    branches exactly (its `text` branch is unreachable from quick-log --
    R-Q1 omits text habits, and `handle_log_callback` above rejects a
    text-habit payload before this is ever called, so R-Q6's "no Ollama
    call anywhere in this path" holds by construction, not by omission)."""
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
            "confirm_duration", lang, label=habit.label(lang), value=value, unit=unit, ordinal=_ordinal(count), count=count
        )

    # boolean (the only remaining reachable type -- see the docstring above)
    status = i18n.t("bool_status_done" if value else "bool_status_not_done", lang)
    return i18n.t("confirm_boolean", lang, label=habit.label(lang), status=status)


async def _log_and_confirm(
    db: "Database",
    channel: Channel,
    config: "Config",
    clock,
    registry: "HabitRegistry",
    lang: i18n.Language,
    habit: "Habit",
    value: float,
    user_id: str,
    raw_data: str,
) -> None:
    now = clock()
    ts = now.isoformat(timespec="seconds")
    today_str = now.date().isoformat()

    was_qualified_before = (
        streaks.day_qualifies(db, config, habit, today_str, user_id) if config.gamification.enabled else False
    )

    value_num = 1.0 if habit.type == "boolean" else float(value)
    entry = LogEntry(
        id=None,
        user_id=user_id,
        ts=ts,
        category=habit.id,
        value_num=value_num,
        value_text=None,
        raw_message=raw_data,
        source="reply",
        habit_type=habit.type,
    )
    # R-Q2/AC-A2: the tapping user's own row -- the SAME `undo_ui.
    # undo_button` every typed confirmation attaches.
    row_id = db.insert_log(entry)
    undo_buttons = undo_ui.undo_button(row_id, lang)

    milestone_suffix = ""
    if config.gamification.enabled:
        crossed = streaks.crossed_milestone(db, config, habit, now.date(), was_qualified_before, user_id)
        if crossed is not None:
            milestone_suffix = "\n\n" + i18n.t("milestone_reached", lang, streak=crossed, label=habit.label(lang))

    record_suffix = ""
    broken_records = records.update_on_log(db, config, registry, habit, user_id, clock=clock)
    if broken_records:
        record_suffix = "\n\n" + records.format_celebration(broken_records, habit, lang)

    confirmation_suffix = milestone_suffix + record_suffix

    if habit.id == "water":
        water_ml = int(value_num)
        total = db.water_total_ml(user_id, today_str)
        goal = targets.effective_goal(db, habit, config, user_id)
        pct = round(100 * total / goal) if goal else 0
        await channel.send_actionable(
            user_id,
            i18n.t("water_confirmation", lang, water_ml=water_ml, total=int(total), goal=goal, pct=pct)
            + confirmation_suffix,
            undo_buttons,
        )
    elif habit.id == "stretch":
        stretch_min = int(value_num)
        count = db.stretch_count(user_id, today_str)
        await channel.send_actionable(
            user_id,
            i18n.t("stretch_confirmation", lang, stretch_min=stretch_min, ordinal=_ordinal(count), count=count)
            + confirmation_suffix,
            undo_buttons,
        )
    else:
        message = _generic_confirmation(db, habit, value_num, today_str, lang, config, user_id)
        await channel.send_actionable(user_id, message + confirmation_suffix, undo_buttons)

    # SPEC-v1.6.md R-D5 (module `dashboard`): refresh AFTER the
    # confirmation is sent, never before -- mirrors `main.py`'s own
    # placement exactly.
    await dashboard.refresh(db, channel, config, registry, user_id, clock)
