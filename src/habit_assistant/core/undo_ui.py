"""Undo discoverability (SPEC-v1.1.md §4 Feature 1 "undo-ui"): the inline
"Undo" button attached to every interactive log confirmation, the
`callback_query` handler it triggers, and the `/undo` bot-command-menu
entry -- all built on the existing v0.5.0 soft-delete capability
(`storage/db.py:soft_delete`/`get_log`), never a second delete
implementation.

This module is self-contained (SPEC-v1.1.md §11 module split): it owns no
part of `main.py`, which still has its own pre-v1.1 `_execute_undo`/
`_describe_log` for the *text* `/undo` command path. Per R-U8, the two
paths must produce byte-identical confirmation copy for the same removed
row and language -- `send_undo_confirmation`/`describe_log` below are that
one shared formatter, ready for `main.py`'s integration step (not part of
this module's scope, see IMPL-v1.1-undo-ui.md) to delegate to instead of
its own inline copy.

No channel import beyond the `Channel` ABC (SPEC.md §8's seam) -- mirrors
`core/reminders.py`'s own import shape.
"""

from __future__ import annotations

import logging
import re

from habit_assistant.channels.base import Button, Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n, targets
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-U5/R-U6: callback_data is "undo:<log_id>" (SPEC-v1.1.md §2.2/§5) --
# anything else is malformed and must not be treated as an undo request.
_UNDO_CALLBACK_RE = re.compile(r"^undo:(\d+)$")

# SQLite's `INTEGER` column binds a Python int via C's signed 64-bit range
# (sqlite3 raises `OverflowError` outside it, rather than truncating or
# erroring at the SQL level). `_UNDO_CALLBACK_RE` alone accepts a digit
# string of any length, so a hostile/malformed `data` like
# "undo:999999999999999999999999999999999" would otherwise reach
# `db.get_log(log_id)` and raise uncaught (Vera's TEST-v1.1-undo-ui.md
# finding, AC9) -- bounds-checked here instead, before any DB call, and
# treated identically to a regex-malformed payload (log + ignore, no
# read, no write).
_SQLITE_MAX_INTEGER = 2**63 - 1


# ---------------------------------------------------------------------------
# R-U1: this module's contribution to the bot command menu (`setMyCommands`).
# `main.py`'s integration step merges this per-language list with the
# `targets` module's own `/target` entries before calling
# `channel.set_my_commands(...)` -- see "Wiring for the integrator" in
# IMPL-v1.1-undo-ui.md for the exact merge call. There is no i18n.CATALOG
# key for this copy (the shared-surface i18n.py build did not add one, and
# this module must not touch that frozen file) -- it is short, static Bot
# API menu copy, not a user-facing reply template.
# ---------------------------------------------------------------------------

UNDO_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Undo your most recent log",
    "th": "ยกเลิกรายการล่าสุดของคุณ",
}


def command_menu_entries() -> dict[i18n.Language, list[tuple[str, str]]]:
    """R-U1: `{lang: [(command, description), ...]}` for just `/undo` --
    the shape `Channel.set_my_commands` expects, per language. `main.py`
    merges this with `targets_command`'s equivalent before the one
    `set_my_commands` call at startup."""
    return {lang: [("undo", desc)] for lang, desc in UNDO_COMMAND_DESCRIPTIONS.items()}


# ---------------------------------------------------------------------------
# R-U2/R-U3: the inline button attached to every interactive confirmation.
# ---------------------------------------------------------------------------


def undo_button(log_id: int, lang: i18n.Language) -> list[Button]:
    """The one-button row for a log confirmation (SPEC-v1.1.md §3.2):
    label resolves through the shared `undo_button_label` catalog entry,
    `callback_data` is `undo:<log_id>` (R-U5 parses this back out)."""
    return [(i18n.t("undo_button_label", lang), f"undo:{log_id}")]


# ---------------------------------------------------------------------------
# R-U8: the one shared "describe what was removed" + "soft-delete and
# confirm" implementation, used by both the button-callback path
# (`handle_undo_callback` below) and, once `main.py`'s integration step
# delegates to it, the text/command `/undo` path. Mirrors
# `main.py:_describe_log`/`_execute_undo`'s body verbatim (byte-identical
# output is the point of R-U8/AC11) -- see IMPL-v1.1-undo-ui.md for the
# smoke test that diffs the two against each other.
# ---------------------------------------------------------------------------


def describe_log(row, registry: HabitRegistry, lang: i18n.Language) -> str:
    """Human-readable one-line summary of a log row (mirrors
    `main.py:_describe_log`). Built-ins keep their dedicated catalog
    entries; any other configured habit resolves through `registry` to a
    type-generic description."""
    category = row["category"]
    if category == "water":
        return i18n.t("describe_log_water", lang, value_num=row["value_num"])
    if category == "stretch":
        return i18n.t("describe_log_stretch", lang, value_num=row["value_num"])
    if category == "diary":
        text = row["value_text"] or ""
        snippet = text if len(text) <= 40 else text[:37] + "..."
        return i18n.t("describe_log_diary", lang, snippet=snippet)

    habit = registry.get(category)
    if habit is not None:
        if habit.type in ("numeric", "duration"):
            msg_id = "describe_log_numeric" if habit.type == "numeric" else "describe_log_duration"
            return i18n.t(
                msg_id, lang, value_num=row["value_num"], unit=habit.unit(lang) or "", label=habit.label(lang)
            )
        if habit.type == "boolean":
            return i18n.t("describe_log_boolean", lang, label=habit.label(lang))
    return i18n.t("describe_log_generic", lang, category=category)


async def send_undo_confirmation(
    db: Database, channel: Channel, config: Config, clock, registry: HabitRegistry, lang: i18n.Language, row
) -> None:
    """R-U5/R-U8: soft-delete `row` and send the "removed + recomputed
    today total" confirmation -- the single formatter shared by the
    button-undo path (below) and the text/command `/undo` path (once
    `main.py`'s integration step delegates to this instead of its own
    inline copy, per IMPL-v1.1-undo-ui.md's wiring notes).

    `row` is assumed live (not yet soft-deleted) -- callers that don't
    already know that (`handle_undo_callback` below) check first via
    `db.get_log`."""
    db.soft_delete(row["id"])
    description = describe_log(row, registry, lang)
    today_str = clock().date().isoformat()
    category = row["category"]
    habit = registry.get(category)

    if category == "water":
        total = db.water_total_ml(today_str)
        # SPEC-v1.1.md R-T5/AC23: a stored `/target water …` override wins
        # over the legacy config default (byte-identical when none is set).
        goal = targets.effective_goal(db, habit, config) if habit is not None else config.reminders.water.goal_ml
        pct = round(100 * total / goal) if goal else 0
        await channel.send(
            i18n.t("undo_removed_water", lang, description=description, total=int(total), goal=goal, pct=pct)
        )
        return
    if category == "stretch":
        count = db.stretch_count(today_str)
        await channel.send(i18n.t("undo_removed_stretch", lang, description=description, count=count))
        return

    if habit is not None and habit.type == "numeric":
        goal = targets.effective_goal(db, habit, config)
        if goal:
            total = db.sum_value(habit.id, today_str)
            pct = round(100 * total / goal) if goal else 0
            await channel.send(
                i18n.t(
                    "undo_removed_numeric",
                    lang,
                    description=description,
                    total=total,
                    goal=goal,
                    unit=habit.unit(lang) or "",
                    pct=pct,
                )
            )
            return
    if habit is not None and habit.type == "duration":
        count = db.count(habit.id, today_str)
        await channel.send(
            i18n.t("undo_removed_duration", lang, description=description, count=count, label=habit.label(lang))
        )
        return
    if habit is not None and habit.type == "boolean":
        await channel.send(i18n.t("undo_removed_boolean", lang, description=description))
        return

    await channel.send(i18n.t("undo_removed_generic", lang, description=description))


# ---------------------------------------------------------------------------
# R-U5/R-U6: the callback_query handler itself.
# ---------------------------------------------------------------------------


async def handle_undo_callback(
    data: str,
    source_text: str,
    callback_id: str,
    *,
    db: Database,
    channel: Channel,
    config: Config,
    clock,
    registry: HabitRegistry,
) -> None:
    """The `on_callback` body for a Telegram inline-button tap
    (SPEC-v1.1.md §5). `TelegramChannel.run` (shared surface, already
    built) always calls `answerCallbackQuery(callback_id)` itself right
    after awaiting this -- R-U4 -- so this function never needs to call it
    (kept as a parameter only to match the `on_callback` callable shape
    `Callable[[str, str, str], Awaitable[None]]` main.py wires it as).

    R-U6: `data` that isn't `undo:<int>` is logged and ignored -- no DB
    read, no DB write, no send.

    R-U5: a valid id that's missing or already soft-deleted gets the
    friendly `already_undone` reply (idempotent re-tap); otherwise the row
    is soft-deleted and confirmed via `send_undo_confirmation` (R-U8, same
    formatter the command-path `/undo` uses)."""
    match = _UNDO_CALLBACK_RE.match(data)
    if match is None:
        logger.info("Ignoring malformed undo callback_query data: %r", data)
        return

    log_id = int(match.group(1))
    if log_id > _SQLITE_MAX_INTEGER:
        logger.info("Ignoring undo callback_query data with an out-of-range log id: %r", data)
        return

    lang = i18n.resolve_reply_language(source_text, config)

    row = db.get_log(log_id)
    if row is None or row["deleted_at"] is not None:
        await channel.send(i18n.t("already_undone", lang))
        return

    await send_undo_confirmation(db, channel, config, clock, registry, lang, row)
