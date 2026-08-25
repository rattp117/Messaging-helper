"""Routines / habit stacks (SPEC-v1.8.md §4 "Feature -- routines / habit
stacks (module `routines`)", R-R1-R-R6): a named bundle of habit+value
items a user can log with one command or one tap -- `/routine <name> =
<habit> <val>[, ...]` (create), bare `/routine` (list, with a run-button
per routine), `/routine <name>` or the run-button (run: logs every valid
item for today in one compact summary), `/routine delete <name>` (delete).

Two entry points, mirroring `core/habitdef.py`'s own
"execute_*"/"handle_*_callback" split and every other settings-style module
in this codebase (`checkins`, `schedules`, `undo_ui`):

- **`execute_routine`** -- the `core/commands.py` `"routine"`-kind dispatch
  target for the TEXT command path (create/list/run/delete all funnel
  through here, selected by `command.routine_action`). Returns the reply
  text for the caller (`main.py`'s integration step) to send via
  `channel.send`, EXCEPT for "list", which sends its own message directly
  (via `channel.send_actionable`, so it can attach one run-button per
  routine -- `str | None`'s own signature carries `None` for exactly that
  case) and for "run", which also refreshes the dashboard itself as a side
  effect before returning its summary text (R-R3's own "refresh the
  dashboard once" -- done here, not by the caller, since this function
  already has `channel`/`config`/`provider` in hand).
- **`handle_routine_callback`** -- the `on_callback` body for a
  `routine:run:<name>` inline-button tap (R-R5). Builds a synthetic
  `Command(kind="routine", routine_action="run", ...)` and delegates
  straight to `execute_routine`'s own "run" branch -- R-Q2's own "reuses
  the shared confirmation path, no second confirmation formatter" pattern,
  reused here for routines' run path. Isolation (R-R5) falls out of the
  storage layer for free: `db.get_routine(user_id, name)` is scoped to
  `user_id` by construction, so a `routine:run:<name>` tapped by a chat
  that doesn't own that name resolves to the SAME `routine_run_not_found`
  friendly no-op a genuinely nonexistent name gets -- no separate
  ownership check is needed here, mirroring `core/undo_ui.py`'s own
  `row["user_id"] != chat_id` comparison, just expressed structurally
  through the scoped query instead of an explicit equality check.

Per-user isolation throughout (mirrors every scoped module in this
codebase): every DB read/write below is scoped to a single `user_id`.
Zero-LLM (R-R6/R-B7): no Ollama call anywhere in this module.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from habit_assistant.channels.base import Button
from habit_assistant.core import audit, i18n, records, units
from habit_assistant.core import dashboard as dashboard_module
from habit_assistant.core.commands import Command
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET
from habit_assistant.storage.models import LogEntry

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-R1: name normalized (trim, lowercase, <=32, ^[a-z0-9_]+$) -- the SAME
# shape a `routine:run:<name>` callback payload's own name segment is
# anchored to below, so a normalized name always round-trips through the
# 64-byte Telegram callback-data limit ("routine:run:" is 12 bytes + at
# most 32 bytes of name = 44 bytes, per SPEC-v1.8.md §2.3's own note).
_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_NAME_MAX_LEN = 32

# SPEC-v1.8.md §2.3: the run-button's own callback payload shape. The name
# segment is anchored to the exact id-shape/length R-R1 already enforces at
# creation time -- a malformed/oversized payload (a hostile or corrupted
# `callback_data`) is rejected here, before any DB call, mirroring
# `core/undo_ui.py`'s own `_UNDO_CALLBACK_RE` + bounds-check discipline
# (R-Q3's "malformed/out-of-range payload -> logged and ignored, no
# read/write" posture, reused verbatim for routines).
_RUN_CALLBACK_RE = re.compile(r"^routine:run:(?P<name>[a-z0-9_]{1,32})$")


def _normalize_name(raw: str) -> str:
    return raw.strip().lower()


def _name_valid(name: str) -> bool:
    return bool(name) and len(name) <= _NAME_MAX_LEN and _NAME_RE.match(name) is not None


def _build_habit_token_lookup(registry: "HabitRegistry") -> dict[str, str]:
    """Maps a lowercased habit id / English label / Thai label -> habit
    id, so a routine item's habit token can be given as the raw id or its
    configured label, in either language -- mirrors `core/commands.py:
    _build_habit_token_lookup` exactly (duplicated here rather than
    imported, since that one is a private helper of a different module;
    this codebase's own established convention for a small, module-local
    "resolve a habit token against a registry" shim, same as `core/
    records.py`'s/`core/trends.py`'s independently-duplicated `_today`)."""
    lookup: dict[str, str] = {}
    for habit in registry:
        lookup.setdefault(habit.id.strip().lower(), habit.id)
        if habit.label_en:
            lookup.setdefault(habit.label_en.strip().lower(), habit.id)
        if habit.label_th:
            lookup.setdefault(habit.label_th.strip().lower(), habit.id)
    return lookup


def _resolve_habit_token(token: str, registry: "HabitRegistry") -> str | None:
    return _build_habit_token_lookup(registry).get(token.strip().lower())


def _item_display(habit: "Habit", value: float | None, lang: i18n.Language) -> str:
    """One item's own display phrase for a create-success/list/run-summary
    line -- registry-generic (R-X1's own convention, reused here): numeric/
    duration shows "<value> <unit> <label>"; boolean/text just shows the
    label (a boolean item's stored `value` is never meaningful -- R-R3
    always logs it `true` -- and a text item is never actually logged by
    a routine at all, R-R3's own "text item -> skipped" rule)."""
    if habit.type in ("numeric", "duration"):
        unit = habit.unit(lang) or ""
        value_str = f"{value:g}" if value is not None else "0"
        return f"{value_str} {unit} {habit.label(lang)}".strip()
    return habit.label(lang)


# ===========================================================================
# create -- R-R1.
# ===========================================================================


def _resolve_item(
    habit_token: str, value_str: str, registry: "HabitRegistry"
) -> tuple[tuple[str, float | None] | None, str | None, dict[str, object]]:
    """One create-time item -> `(habit_id, value)` on success, or `(None,
    msg_id, kwargs)` on failure -- mirrors `core/habitdef.py:validate_and_
    normalize`'s own "structured result out, caller formats" split
    (`execute_routine`'s own caller then does `i18n.t(msg_id, lang,
    **kwargs)`, keeping this function lang-agnostic).

    The habit token must resolve against `registry` (the ACTING user's own
    per-user registry, R-R1); an unresolved token fails with
    `routine_invalid_habit`, echoing the raw token the user typed (SPEC-
    v1.8.md §3.3's own sample: `"coffee" isn't one of your habits`), not a
    resolved label.

    Type-specific value handling: numeric/duration -- the value tail must
    parse as a positive NUMBER [+ UNIT] belonging to THIS exact habit (an
    explicit unit resolving to a DIFFERENT habit fails, mirroring
    `core/commands.py:_parse_target_value`'s own R-T9 rule verbatim; no
    unit at all defaults to the habit's own base unit at multiplier 1).
    Boolean/text -- any non-empty value tail is accepted and the item is
    stored with `value=None` (R-R3: a boolean item always logs `true`
    regardless of what was typed here, and a text item is never logged at
    all -- always skipped, "can't carry free text" -- so neither type has
    a meaningful numeric value worth storing or re-validating)."""
    habit_id = _resolve_habit_token(habit_token, registry)
    if habit_id is None:
        return None, "routine_invalid_habit", {"token": habit_token}
    habit = registry.get(habit_id)
    assert habit is not None  # resolved via this same registry -- must exist

    if habit.type in ("numeric", "duration"):
        match = units.VALUE_RE.match(value_str.strip())
        if not match:
            return None, "routine_invalid_value", {"habit": habit_token, "value": value_str}
        num = float(match.group("num"))
        if num <= 0:
            return None, "routine_invalid_value", {"habit": habit_token, "value": value_str}
        unit_raw = match.group("unit")
        if unit_raw is None:
            return (habit_id, num), None, {}
        resolved = units.resolve_unit(units.build_unit_lookup(registry), unit_raw.lower())
        if resolved is None:
            return None, "routine_invalid_value", {"habit": habit_token, "value": value_str}
        unit_habit_id, multiplier = resolved
        if unit_habit_id != habit_id:
            return None, "routine_invalid_value", {"habit": habit_token, "value": value_str}
        return (habit_id, num * multiplier), None, {}

    if not value_str.strip():
        return None, "routine_invalid_value", {"habit": habit_token, "value": value_str}
    return (habit_id, None), None, {}


async def _create(
    command: "Command", *, db: "Database", config: "Config", provider: "RegistryProvider", lang: i18n.Language,
    user_id: str, name: str,
) -> str:
    """R-R1: checked in the same "cap first, then shape, then collision,
    then per-item semantics" order `core/habitdef.py:validate_and_
    normalize` uses for `/addhabit` -- a well-formed request still can't
    land at the limit, reported up front. Any failure -> a friendly
    error, no write (every return before the `db.add_routine` call is a
    read-only path)."""
    if db.count_routines(user_id) >= config.routines.max_per_user:
        return i18n.t("routine_cap_reached", lang, cap=config.routines.max_per_user)
    if not _name_valid(name):
        return i18n.t("routine_invalid_name", lang)
    if command.routine_items is None:
        return i18n.t("routine_create_usage", lang)
    if db.get_routine(user_id, name) is not None:
        return i18n.t("routine_name_taken", lang, name=name)

    registry = provider.for_user(user_id)
    resolved_items: list[tuple[str, float | None]] = []
    for habit_token, value_str in command.routine_items:
        item, msg_id, kwargs = _resolve_item(habit_token, value_str, registry)
        if item is None:
            return i18n.t(msg_id, lang, **kwargs)  # type: ignore[arg-type]
        resolved_items.append(item)

    try:
        db.add_routine(user_id, name, resolved_items)
    except Exception:
        logger.exception("Failed to add routine %r for user %r", name, user_id)
        return i18n.t("routine_save_failed", lang)

    audit.record(
        db, actor=user_id, action="routine_create", source="command", entity=name, new_value=len(resolved_items)
    )

    items_display = ", ".join(
        _item_display(registry.get(habit_id), value, lang) for habit_id, value in resolved_items
    )
    return i18n.t("routine_create_success", lang, name=name, items=items_display)


# ===========================================================================
# list -- R-R2.
# ===========================================================================


def _render_list(
    db: "Database", registry: "HabitRegistry", lang: i18n.Language, user_id: str
) -> tuple[str, list[Button]]:
    """R-R2: `user_id`'s own routines, each with its items and a run-button
    -- render-budget disciplined (mirrors `core/render_budget.py:
    fit_within_budget`'s own "drop the oldest shown rows one at a time
    until it fits" contract, reimplemented locally rather than calling
    that helper directly so the dropped buttons stay in lockstep with the
    dropped text lines -- the shared helper has no notion of a per-row
    button to keep in sync). Newest-first (mirrors every OTHER caller of
    that budget contract, e.g. `core/audit_view.py`/`core/history_view.
    py`), so an overflow drops the LEAST recently created routines first,
    not the most recent one a user just made."""
    rows = list(reversed(db.list_routines(user_id)))
    if not rows:
        return i18n.t("routine_list_empty", lang), []

    lines: list[str] = []
    buttons: list[Button] = []
    for name, items in rows:
        items_display = ", ".join(
            _item_display(registry.get(habit_id), value, lang)
            if registry.get(habit_id) is not None
            else i18n.t("routine_skip_removed", lang, habit=habit_id)
            for habit_id, value in items
        )
        lines.append(i18n.t("routine_list_item", lang, name=name, items=items_display))
        buttons.append((i18n.t("routine_run_button_label", lang, name=name), f"routine:run:{name}"))

    header = i18n.t("routine_list_header", lang)
    full = "\n".join([header, *lines])
    if len(full) <= TELEGRAM_MESSAGE_BUDGET:
        return full, buttons

    kept_lines = list(lines)
    kept_buttons = list(buttons)
    while True:
        dropped = len(lines) - len(kept_lines)
        parts = [header, *kept_lines]
        if dropped:
            parts.append(i18n.t("routine_list_more", lang, count=dropped))
        candidate = "\n".join(parts)
        if len(candidate) <= TELEGRAM_MESSAGE_BUDGET or not kept_lines:
            return candidate, kept_buttons
        kept_lines.pop()
        kept_buttons.pop()


# ===========================================================================
# run -- R-R3.
# ===========================================================================


async def _run(
    *, db: "Database", channel: "Channel", config: "Config", provider: "RegistryProvider", lang: i18n.Language,
    user_id: str, name: str, clock,
) -> str:
    """R-R3: logs every VALID item for TODAY, sends one compact summary,
    refreshes the dashboard ONCE (skipped entirely for an all-invalid
    routine -- "no dashboard churn"), and records one fail-open
    `routine_run` audit row. Milestone/record celebration lines are
    suppressed by construction: this calls `records.update_on_log` per
    item and discards its return (mirrors `main.py`'s own call site, minus
    the celebration-suffix step) and never calls `streaks.crossed_
    milestone` at all."""
    if not name:
        return i18n.t("routine_run_usage", lang)

    items = db.get_routine(user_id, name)
    if items is None:
        return i18n.t("routine_run_not_found", lang, name=name)

    registry = provider.for_user(user_id)
    now = clock()
    ts = now.isoformat(timespec="seconds")

    logged_phrases: list[str] = []
    skipped_phrases: list[str] = []

    for habit_id, value in items:
        habit = registry.get(habit_id)
        if habit is None:
            skipped_phrases.append(i18n.t("routine_skip_removed", lang, habit=habit_id))
            continue
        if habit.type == "text":
            skipped_phrases.append(i18n.t("routine_skip_text", lang, habit=habit.label(lang)))
            continue

        value_num = value if habit.type in ("numeric", "duration") else 1.0

        entry = LogEntry(
            id=None,
            user_id=user_id,
            ts=ts,
            category=habit.id,
            value_num=value_num,
            value_text=None,
            raw_message=f"/routine {name}",
            source="reply",
            habit_type=habit.type,
        )
        db.insert_log(entry)
        # R-R3: "records.update_on_log is still called per item and its
        # return discarded, so stored records stay accurate" -- no
        # celebration suffix is ever built from this call's result.
        records.update_on_log(db, config, registry, habit, user_id, clock=clock)

        logged_phrases.append(_item_display(habit, value_num, lang))

    logged_count = len(logged_phrases)
    total = len(items)

    audit.record(db, actor=user_id, action="routine_run", source="command", entity=name, new_value=logged_count)

    if logged_count == 0:
        if skipped_phrases:
            return i18n.t("routine_run_nothing_skipped", lang, name=name, skipped=", ".join(skipped_phrases))
        return i18n.t("routine_run_nothing", lang, name=name)

    # R-R3: refresh AFTER every item is logged, exactly once -- never for
    # an all-invalid run (the branch above already returned).
    await dashboard_module.refresh(db, channel, config, registry, user_id, clock)

    if skipped_phrases:
        return i18n.t(
            "routine_run_summary_partial",
            lang,
            name=name,
            items=", ".join(logged_phrases),
            count=logged_count,
            total=total,
            skipped=", ".join(skipped_phrases),
        )
    return i18n.t(
        "routine_run_summary_full", lang, name=name, items=", ".join(logged_phrases), count=logged_count, total=total
    )


# ===========================================================================
# delete -- R-R4.
# ===========================================================================


def _delete(db: "Database", *, lang: i18n.Language, user_id: str, name: str) -> str:
    if not name:
        return i18n.t("routine_delete_usage", lang)

    try:
        existed = db.delete_routine(user_id, name)
    except Exception:
        logger.exception("Failed to delete routine %r for user %r", name, user_id)
        return i18n.t("routine_save_failed", lang)

    if not existed:
        return i18n.t("routine_delete_not_found", lang, name=name)

    audit.record(db, actor=user_id, action="routine_delete", source="command", entity=name)
    return i18n.t("routine_delete_success", lang, name=name)


# ===========================================================================
# execute_routine -- the core/commands.py "routine"-kind dispatch target.
# ===========================================================================


async def execute_routine(
    command: "Command",
    *,
    db: "Database",
    channel: "Channel",
    config: "Config",
    provider: "RegistryProvider",
    lang: i18n.Language,
    user_id: str,
    clock=datetime.now,
) -> str | None:
    """SPEC-v1.8.md §5: dispatches on `command.routine_action` ("create"/
    "list"/"run"/"delete", set by `core/commands.py:_match_routine`).
    Returns the reply text for the caller to `channel.send`, EXCEPT for
    "list" -- which sends its own message directly (so it can attach one
    run-button per routine via `channel.send_actionable`) and returns
    `None`, since there is nothing left for the caller to send."""
    if command.routine_action == "list" or command.routine_action is None:
        registry = provider.for_user(user_id)
        text, buttons = _render_list(db, registry, lang, user_id)
        if buttons:
            await channel.send_actionable(user_id, text, buttons)
        else:
            await channel.send(user_id, text)
        return None

    name = _normalize_name(command.routine_name or "")

    if command.routine_action == "create":
        return await _create(command, db=db, config=config, provider=provider, lang=lang, user_id=user_id, name=name)
    if command.routine_action == "delete":
        return _delete(db, lang=lang, user_id=user_id, name=name)
    return await _run(
        db=db, channel=channel, config=config, provider=provider, lang=lang, user_id=user_id, name=name, clock=clock
    )


# ===========================================================================
# handle_routine_callback -- R-R3/R-R5: the routine:run:<name> tap.
# ===========================================================================


async def handle_routine_callback(
    chat_id: str,
    data: str,
    source_text: str,
    callback_id: str,
    *,
    db: "Database",
    channel: "Channel",
    config: "Config",
    provider: "RegistryProvider",
    clock=datetime.now,
) -> None:
    """The `on_callback` body for a `routine:run:<name>` tap (SPEC-v1.8.md
    §5). `TelegramChannel.run` always calls `answerCallbackQuery(
    callback_id)` itself right after awaiting this (mirrors `core/undo_ui.
    py:handle_undo_callback`'s own identical note) -- `callback_id` is kept
    as a parameter only to match the `on_callback` callable shape.

    `data` that isn't `routine:run:<name-shaped-id>` is logged and
    ignored -- no DB read, no DB write, no send (R-Q3's own safety
    discipline, mirrored here for routines).

    R-R5 (isolation): delegates straight to `execute_routine`'s own "run"
    branch with `user_id=chat_id` (the TAPPING chat) -- `db.get_routine`
    is scoped to that chat by construction, so a name owned by a DIFFERENT
    user resolves to the same friendly `routine_run_not_found` no-op a
    genuinely nonexistent name gets. No separate ownership check is
    needed here (unlike `undo_ui`'s explicit `row["user_id"] != chat_id`
    comparison, since a routine's row is never read cross-user in the
    first place)."""
    match = _RUN_CALLBACK_RE.match(data)
    if match is None:
        logger.info("Ignoring malformed routine callback_query data: %r", data)
        return

    name = match.group("name")
    lang = i18n.resolve_reply_language(source_text, config)

    command = Command(kind="routine", routine_action="run", routine_name=name)
    reply = await execute_routine(
        command, db=db, channel=channel, config=config, provider=provider, lang=lang, user_id=chat_id, clock=clock
    )
    if reply is not None:
        await channel.send(chat_id, reply)
