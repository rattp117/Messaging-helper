"""Wiring: load config, start the scheduler + Telegram inbound loop.

Also the CLI entry point: --test-reminder, --seed, --dry-run (SPEC.md §10),
plus --migrate, --backup, --restore (ROADMAP.md v0.3.0), plus the health
monitor task (ROADMAP.md v0.4.0).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from habit_assistant.channels.base import Button, Channel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config, ConfigError, load_config, load_secrets
from habit_assistant.core import (
    access,
    audit,
    audit_view,
    commands,
    discoverability,
    history_view,
    i18n,
    preferences,
    query,
    schedules,
    streaks,
    target_nl,
    targets,
    targets_command,
    undo_ui,
)
from habit_assistant.core.backup import BackupError
from habit_assistant.core.backup import backup as backup_db
from habit_assistant.core.backup import restore as restore_db
from habit_assistant.core.habits import BUILTIN_IDS, Habit, HabitRegistry, log_entry_from_result
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.parser import parse_message
from habit_assistant.core.reminders import ReminderState, is_quiet_hours_now, run_due_reminders, send_reminder
from habit_assistant.core.review import render_weekly_review_charts, run_weekly_review
from habit_assistant.llm.ollama_client import OllamaClient, build_extraction_schema
from habit_assistant.llm.prompts import (
    DIARY_REFLECTION_SYSTEM_PROMPT,
    DIARY_REFLECTION_USER_TEMPLATE,
    build_extraction_system_prompt,
    build_extraction_user_prompt,
)
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

logger = logging.getLogger("habit_assistant")

# ROADMAP.md v0.7.0 "Multi-Habit Extensibility": the default habit registry
# (water/stretch/diary, SPEC-v0.7.md §2.1), used wherever a registry is
# needed before `config.toml` has been loaded -- currently only
# `build_arg_parser()`'s `--test-reminder` choices (argparse runs before
# `async_main` loads config). Every other call site builds its own
# registry from the loaded `Config` (SPEC-v0.7.md §4 R3).
_DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

# ROADMAP.md v0.6.0: every user-facing string below now resolves through
# core/i18n.py's catalog (AC6.2). These module-level names are kept as the
# resolved *English* text (== CATALOG[id]["en"]) purely for backward-compat
# imports (existing tests, `--dry-run`/CLI tooling) -- the actual reply
# language is resolved per-message inside handle_inbound_message via
# `i18n.resolve_reply_language` (AC6.1/AC6.3) and is not a fixed constant.
CLARIFYING_QUESTION = i18n.t("clarifying_question", "en")

# ROADMAP.md v0.4.0 AC3.3: sent instead of the normal parse/confirm flow
# while the LLM is known DOWN (health_monitor.ollama_up is False) -- the
# message is persisted verbatim (category='unparsed') and re-parsed
# automatically once Ollama recovers (see reparse_pending_unparsed below).
DEFERRED_ACK_MESSAGE = i18n.t("deferred_ack", "en")

# ROADMAP.md v0.5.0 AC5.2 / edit-with-nothing-to-edit: friendly, no-write
# responses when a command has nothing to act on.
NOTHING_TO_UNDO_MESSAGE = i18n.t("nothing_to_undo", "en")
NOTHING_TO_EDIT_MESSAGE = i18n.t("nothing_to_edit", "en")

# SPEC-v1.1.md R-U1/AC1: this integration step's own contribution to the bot
# command menu, mirroring `core/undo_ui.py:UNDO_COMMAND_DESCRIPTIONS`'s shape
# and rationale -- there is no `core/i18n.py` catalog key for Bot API menu
# copy (short, static, not a user-facing reply template), and the `targets`
# module (IMPL-v1.1-targets.md) did not add a `command_menu_entries()` of its
# own, so this integration step supplies `/target`'s menu entry directly.
TARGET_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "View or set a habit's daily goal",
    "th": "ดูหรือตั้งเป้าหมายรายวันของกิจกรรม",
}

# SPEC-v1.1.md R-D4/AC40: this module's own contribution to the bot command
# menu for `/help` and `/habits`, same rationale as TARGET_COMMAND_
# DESCRIPTIONS above -- `core/discoverability.py` (module `discoverability`)
# has no `command_menu_entries()` of its own (its public surface is just the
# two formatter functions, per SPEC-v1.1.md §5), so this integration point
# supplies the menu copy directly.
DISCOVERABILITY_COMMAND_DESCRIPTIONS: dict[i18n.Language, list[tuple[str, str]]] = {
    "en": [("help", "Show what I can do"), ("habits", "List your habits and today's progress")],
    "th": [("help", "ดูสิ่งที่ฉันทำได้"), ("habits", "ดูรายการกิจกรรมและความคืบหน้าวันนี้")],
}

# SPEC-v1.2.md §11 integration step: this integration point's own
# contribution to the bot command menu for the three parallel modules'
# user-facing commands, same rationale as TARGET_COMMAND_DESCRIPTIONS/
# DISCOVERABILITY_COMMAND_DESCRIPTIONS above -- none of `access`/
# `preferences`/`schedules` added a `command_menu_entries()` of their own
# (their public surface per SPEC-v1.2.md §5 is just their execute_*/
# handle_* functions), so this integration step supplies the menu copy
# for all three directly, exactly like the v1.1 integration step already
# did for `/target`/`/help`/`/habits`.
#
# `/start` is included -- it's available to ANY user (R-A5), not
# owner-only, matching ordinary Telegram bot convention of always listing
# it. `/lang`/`/quiet`/`/remind` are likewise genuinely user-facing
# (R-P1/R-P2/R-S5). The four TRUE admin commands (`/approve`/`/block`/
# `/users`/`/invite`, R-A4, owner-only) are deliberately NOT added here:
# `set_my_commands`/Telegram's `setMyCommands` is GLOBAL (R-C1 -- one
# menu for every user of the bot, not per-chat), so listing owner-only
# admin commands in it would advertise them to every ordinary member, a
# needless information leak for a capability only one chat can actually
# use. SPEC-v1.2.md itself is silent on which commands belong in the
# menu (no §2.3/§5 mention of `setMyCommands` content) -- this is this
# integration step's own judgment call, recorded here per the
# coordinator's own instruction to "follow spec, document the call".
START_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Get started",
    "th": "เริ่มต้นใช้งาน",
}
LANG_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Set your reply language (en/th/auto)",
    "th": "ตั้งภาษาที่ใช้ตอบ (en/th/auto)",
}
QUIET_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Set your quiet hours (no reminders sent)",
    "th": "ตั้งช่วงเวลางดแจ้งเตือนของคุณ",
}
REMIND_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "View or set your reminder times for a habit",
    "th": "ดูหรือตั้งเวลาแจ้งเตือนของกิจกรรม",
}
# SPEC-v1.4.md §4 R-A2/§9 item 1: `/history` is the caller's OWN data
# (unlike owner-only, admin-hidden `/audit`) -- added to the PUBLIC menu.
HISTORY_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Show your recent entries (including undone ones)",
    "th": "ดูรายการล่าสุดของคุณ (รวมรายการที่ยกเลิกแล้ว)",
}


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def setup_logging(level: str) -> None:
    # Windows consoles / redirected files default to cp1252, which can't
    # encode the emoji used in reminders/confirmations. Force UTF-8 so
    # logging (and print()) never crashes on them, on any platform.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _stored_language_pref(db: Database, user_id: str) -> str:
    """SPEC-v1.2.md R-P1's read side (integration step, punch-list item 3):
    `user_id`'s stored `users.language_pref`, defaulting to `"auto"` on a
    missing row or a DB read error -- fail-open, mirrors `core/access.py:
    _resolve_unprompted_language_for`'s and `core/reminders.py:
    _user_language_pref`'s identical posture (each file keeps its own
    small copy, this codebase's established convention -- see e.g.
    `_seed` duplicated per test file). A preference lookup must never
    crash or block ordinary message handling."""
    try:
        row = db.get_user(user_id)
    except Exception:
        logger.exception("Failed to read language preference for user %r; defaulting to auto", user_id)
        return "auto"
    return row["language_pref"] if row is not None else "auto"


async def _execute_undo(
    db: Database,
    channel: Channel,
    config: Config,
    clock,
    registry: HabitRegistry,
    lang: i18n.Language,
    user_id: str,
) -> None:
    """ROADMAP.md v0.5.0 AC5.1/AC5.2: soft-delete the most recent
    non-deleted log and confirm what was removed, with today's running
    total reflecting the removal. Nothing logged -> friendly message, no
    write (AC5.2). `lang` is the reply language resolved from the inbound
    undo command itself (ROADMAP.md v0.6.0 AC6.1/AC6.3).

    SPEC-v1.1.md R-U8/AC11: the "soft-delete + describe + recompute" body
    delegates to `core/undo_ui.send_undo_confirmation` -- the single
    formatter this text/command `/undo` path and the inline-button
    `handle_undo_callback` path both use, so the two can never diverge by
    a future edit to only one of them.

    SPEC-v1.2.md R-D1: `user_id` scopes which row counts as "the most
    recent" -- a user's `/undo` can only ever remove their OWN last entry
    (AC-U-ISO)."""
    row = db.last_log(user_id)
    if row is None:
        await channel.send(user_id, i18n.t("nothing_to_undo", lang))
        return

    await undo_ui.send_undo_confirmation(db, channel, config, clock, registry, lang, row)


async def _execute_edit(
    db: Database,
    channel: Channel,
    config: Config,
    clock,
    command: commands.Command,
    registry: HabitRegistry,
    lang: i18n.Language,
    user_id: str,
) -> None:
    """ROADMAP.md v0.5.0 AC5.3: update the last matching (same-category)
    entry's value and re-confirm the new daily total. No matching entry ->
    friendly message, no write (mirrors AC5.2's undo-with-nothing shape).
    `lang` is resolved from the inbound edit command itself (v0.6.0
    AC6.1/AC6.3).

    ROADMAP.md v0.7.0: water/stretch keep their byte-identical v0.6
    confirmations (AC7.1); a non-built-in numeric/duration habit gets a
    type-generic edit confirmation (AC9). `core/commands.py`'s
    registry-aware `dispatch()` (module M1) can now classify an edit
    against any numeric/duration habit's own units/aliases, not just
    water/stretch, so this generic branch is live in production
    (SPEC-v0.7.md §4 R14).

    SPEC-v1.2.md R-D1: `user_id` scopes both the row lookup and every
    aggregation below -- a user can only edit their own last matching
    entry (AC-U-ISO)."""
    row = db.last_log(user_id, category=command.category)
    if row is None:
        await channel.send(user_id, i18n.t("nothing_to_edit", lang))
        return

    db.update_value(row["id"], value_num=command.value_num)
    # SPEC-v1.3.md R-C1/AC-C2 (integration step): the one capture site that
    # belongs to main.py itself rather than either parallel audit module --
    # `row` was fetched above BEFORE the write, so `row["value_num"]` is
    # still the pre-update value here, exactly like every other capture
    # site's own "read old, write, record" shape (core/audit.py's own
    # fail-open contract -- no try/except needed around this call).
    audit.record(
        db,
        actor=user_id,
        action="edit",
        source="command",
        entity=command.category,
        old_value=row["value_num"],
        new_value=command.value_num,
    )
    today_str = clock().date().isoformat()
    habit = registry.get(command.category)

    if command.category == "water":
        total = db.water_total_ml(user_id, today_str)
        # SPEC-v1.1.md R-T5/AC23: same override-aware goal resolution as
        # the water confirmation/undo paths (AC24: byte-identical with no
        # override set).
        goal = (
            targets.effective_goal(db, habit, config, user_id) if habit is not None else config.reminders.water.goal_ml
        )
        pct = round(100 * total / goal) if goal else 0
        await channel.send(
            user_id,
            i18n.t("edit_updated_water", lang, value_num=command.value_num, total=int(total), goal=goal, pct=pct),
        )
        return
    if command.category == "stretch":
        count = db.stretch_count(user_id, today_str)
        await channel.send(
            user_id,
            i18n.t("edit_updated_stretch", lang, value_num=command.value_num, ordinal=ordinal(count), count=count),
        )
        return

    if habit is not None and habit.type == "numeric":
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal:
            total = db.sum_value(user_id, habit.id, today_str)
            pct = round(100 * total / goal) if goal else 0
            await channel.send(
                user_id,
                i18n.t(
                    "edit_updated_numeric",
                    lang,
                    value=command.value_num,
                    total=total,
                    goal=goal,
                    unit=habit.unit(lang) or "",
                    pct=pct,
                ),
            )
            return
    if habit is not None and habit.type == "duration":
        count = db.count(user_id, habit.id, today_str)
        await channel.send(
            user_id,
            i18n.t(
                "edit_updated_duration",
                lang,
                value=command.value_num,
                unit=habit.unit(lang) or "",
                label=habit.label(lang),
                ordinal=ordinal(count),
                count=count,
            ),
        )
        return
    # A numeric-without-goal/boolean/text habit, or an unrecognized
    # category: editing those is out of scope (SPEC-v0.7.md §10) -- no
    # confirmation is sent, matching v0.6.0's behavior for any category
    # other than water/stretch (which could never occur pre-v0.7).


async def _execute_snooze(
    db: Database,
    channel: Channel | None,
    config: Config,
    clock,
    command: commands.Command,
    registry: HabitRegistry,
    lang: i18n.Language,
    scheduler: AsyncIOScheduler | None,
    reminder_state: ReminderState | None,
    dry_run: bool,
    user_id: str,
) -> None:
    """ROADMAP.md v0.9.0 AC9.3: reschedule a single one-off reminder for the
    most recently reminded habit (`reminder_state.last_habit_id`), N minutes
    from now (N = the command's explicit `minutes`, else `config.snooze.
    default_minutes`). No habit has fired a reminder yet this process ->
    a friendly fallback, no job scheduled (mirrors AC5.2's undo-with-nothing
    shape). The scheduled job is `send_reminder` itself, so it still
    respects quiet hours/goal-met at fire time (fail-open, same as every
    other reminder) and fires exactly once -- a `DateTrigger`, not a
    recurring `CronTrigger`; APScheduler drops a date-triggered job from the
    scheduler once it has fired, so it never recurs.

    SPEC-v1.2.md R-S2/AC-U-SNOOZE: `reminder_state.last_habit_id` is now a
    per-user map -- only the ASKING user's (`user_id`) most-recently-fired
    habit is looked up, and the rescheduled one-off is addressed to their
    chat only. A rescheduling never affects, or is affected by, another
    user's reminder history."""
    minutes = command.minutes if command.minutes is not None else config.snooze.default_minutes
    habit_id = reminder_state.last_habit_id.get(user_id) if reminder_state is not None else None
    habit = registry.get(habit_id) if habit_id is not None else None

    if dry_run:
        print({"kind": "snooze", "minutes": minutes, "habit": habit.id if habit is not None else None})
        return

    assert channel is not None, "channel is required outside dry-run"

    if habit is None:
        await channel.send(user_id, i18n.t("snooze_no_recent_reminder", lang))
        return

    if scheduler is not None:
        run_time = clock() + timedelta(minutes=minutes)
        reminder_language = i18n.resolve_unprompted_language(config)
        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=run_time, timezone=config.app.timezone),
            args=[channel, user_id, habit, reminder_language, db, config, reminder_state],
            id=f"snooze_{user_id}_{habit.id}_{run_time.strftime('%Y%m%dT%H%M%S%f')}",
            replace_existing=True,
        )

    await channel.send(user_id, i18n.t("snooze_confirmed", lang, minutes=minutes, label=habit.label(lang)))


async def _generic_confirmation(
    db: Database,
    llm: OllamaClient,
    habit: Habit,
    value,
    today_str: str,
    lang: i18n.Language,
    config: Config,
    user_id: str,
) -> str:
    """Type-generic confirmation for any habit that is NOT one of the
    three built-ins (SPEC-v0.7.md §3.2/§4 R13). Built-ins never reach
    here -- `handle_inbound_message` keeps their byte-identical v0.6
    catalog entries inline (AC7.1). SPEC-v1.2.md R-D2: scoped to
    `user_id`."""
    if habit.type == "numeric":
        total = db.sum_value(user_id, habit.id, today_str)
        unit = habit.unit(lang) or ""
        # SPEC-v1.1.md R-T5/AC23: a stored `/target <habit> …` override
        # now wins over the habit's config `goal` here too (AC24:
        # byte-identical when no override is set).
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


async def _send_recovered_generic(
    channel: Channel, chat_id: str, habit: Habit, value, lang: i18n.Language, buttons: list[Button]
) -> None:
    """The recovery-confirmation counterpart of `_generic_confirmation`,
    for `reparse_pending_unparsed` (SPEC-v0.7.md §4 R14). SPEC-v1.1.md
    R-U2: a recovery re-confirmation is itself an interactive log
    confirmation (the deferred message finally got logged), so it carries
    the undo button too, via `send_actionable`. SPEC-v1.2.md R-D3:
    `chat_id` is the deferred row's own `user_id` -- the recovery always
    addresses the ORIGINAL asking user, never a pinned/global chat."""
    if habit.type == "numeric":
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
    else:  # text -- not explicitly listed among SPEC-v0.7.md §5's catalog
        # ids; added for completeness (see IMPL.md "known limitations").
        await channel.send_actionable(chat_id, i18n.t("recovered_text", lang, label=habit.label(lang)), buttons)


async def handle_inbound_message(
    text: str,
    *,
    db: Database,
    llm: OllamaClient,
    channel: Channel | None,
    config: Config,
    user_id: str,
    source: str = "reply",
    clock=datetime.now,
    dry_run: bool = False,
    health_monitor: HealthMonitor | None = None,
    registry: HabitRegistry | None = None,
    scheduler: AsyncIOScheduler | None = None,
    reminder_state: ReminderState | None = None,
) -> None:
    """Command dispatch -> (act + confirm) OR Parse -> validate -> (write
    row + confirm) OR (clarifying question). Confirmation formats are
    verbatim per SPEC.md §6.

    ROADMAP.md v0.5.0: every inbound message is checked against
    `core/commands.dispatch()` first -- a conservative, LLM-free router for
    explicit undo/edit commands (AC5.1, AC5.3). It never needs the LLM and
    is unaffected by Ollama's up/down state, so it runs even while the
    deferral path below would otherwise kick in. A message that isn't a
    recognized command (AC5.5: normal habit messages like "500ml") falls
    through to the parser exactly as before.

    ROADMAP.md v0.4.0 AC3.3: if `health_monitor` says Ollama is currently
    DOWN, skip calling the LLM entirely -- acknowledge the message and
    persist it verbatim as category='unparsed' (raw text kept). It gets
    re-parsed and confirmed automatically once Ollama recovers (see
    `reparse_pending_unparsed`, wired as `health_monitor`'s
    `on_ollama_recovered` callback in `async_main`, and also run once at
    startup to catch up on anything deferred by a previous process run).

    ROADMAP.md v0.6.0 AC6.1/AC6.3: every reply below is a *response* to
    this inbound `text`, so its language is resolved once, up front, from
    the message itself (`i18n.resolve_reply_language`) -- "auto" mode
    matches whatever the user just wrote (Thai in -> Thai out, English in
    -> English out), and a forced `config.i18n.language` overrides that
    for every branch uniformly, commands included.

    ROADMAP.md v0.7.0 "Multi-Habit Extensibility": `registry` defaults to
    `HabitRegistry.from_config(config)` when not given, so every pre-v0.7
    caller (this file's own `async_main`, and every test that doesn't care
    about a custom habit set) keeps working unchanged. A result's
    `habit = registry.get(result.category)` decides confirmation shape:
    built-in ids (`water`/`stretch`/`diary`) keep their exact v0.6 catalog
    entries below (byte-identical, AC7.1); any other habit renders through
    `_generic_confirmation` (AC9).

    ROADMAP.md v0.7.0 integration: `commands.dispatch(text, registry)` is
    module M1's registry-aware contract (SPEC-v0.7.md §5) -- wired here
    now that `core/commands.py` has landed (was deferred during the
    shared-surface build; see `IMPL.md`'s v0.7.0 "Known limitations" #1
    and `IMPL-v0.7-M1.md`'s "READ THIS FIRST").

    ROADMAP.md v0.8.0 "Natural-Language Queries": a `command.kind ==
    "query"` (detected LLM-free by `dispatch`'s anchored interrogative
    patterns, e.g. "how much water this week?" / "อาทิตย์นี้ยืดกี่ครั้ง")
    is answered by `core/query.answer_question` and returned immediately --
    before the health-monitor deferral check and the extractor below, and
    without ever writing a `logs` row (AC8.5).

    ROADMAP.md v0.9.0 "Adaptive Reminders, Snooze & Quiet Hours": a
    `command.kind == "snooze"` (e.g. "snooze 30" / "เลื่อน 30 นาที") is
    handled by `_execute_snooze`, which schedules a single one-off
    `send_reminder` job `minutes` from now on `scheduler` for the habit
    whose reminder most recently actually fired (`reminder_state`, AC9.3)
    -- both new, optional, default-`None` params so every pre-v0.9 caller
    that doesn't care about snooze (tests, `--dry-run`) is unaffected.

    ROADMAP.md v0.10.0 "Streaks, Gentle Gamification & Daily Summary"
    (AC10.2/AC10.4): when `config.gamification.enabled`, a normal habit log
    (below, after the command/deferral branches) checks whether writing
    this entry just made *today* newly satisfy the habit's streak
    condition (`core/streaks.py:day_qualifies`, read once before the
    insert and once after) and, if the resulting streak lands on a
    configured milestone, appends one `milestone_reached` line to the
    confirmation that's about to be sent -- exactly once per crossing,
    never repeated for further logs the same day (see
    `streaks.crossed_milestone`'s docstring for why). `enabled = false`
    skips this entirely -- zero milestone lines, and the pre/post
    `day_qualifies` reads never happen (AC10.4).

    SPEC-v1.1.md "Undo menu + per-habit targets" integration (this file's
    own wiring, landed after both `undo-ui` and `targets` reported done):
    a `command.kind == "target"` (R-T7, e.g. "/target water 2000") is
    answered by `core/targets_command.execute_target` and returned
    immediately, plain `send` (a target reply is not a log confirmation).
    Every interactive log confirmation below (water/stretch/diary/generic,
    plus `reparse_pending_unparsed`'s recovery re-confirmations) now goes
    out via `channel.send_actionable(text, undo_ui.undo_button(row_id,
    lang))` instead of plain `send` (R-U2/AC3) -- unprompted sends
    (reminders, daily summary, weekly review, health alerts, the
    clarifying question, the deferred-ack) are untouched, still plain
    `send`, no button (R-U2's explicit exclusion list). Between the
    health-monitor deferral check and `parse_message`, a full-NL
    target-intent step (R-T12-R-T16) runs only when Ollama is up: a cheap
    gate (`target_nl.looks_like_target_phrasing`) followed by a fail-closed
    LLM classification (`target_nl.classify_target_intent`); a hit sets the
    target via the same `execute_target` "set" path and returns without
    ever writing a `logs` row (AC29/AC30); a miss (gate miss, low
    confidence, or any classifier failure) falls straight through to
    `parse_message` unchanged (R-C5).

    SPEC-v1.1.md "discoverability" module (sequential follow-on, landed
    after the above): `command.kind in ("help", "habits")` (R-D1) are
    answered by `core/discoverability.py`'s two formatters and returned
    immediately, plain `send` -- both deterministic and LLM-free, so both
    are dispatched here, before the health-monitor deferral check, and
    work with Ollama down (AC35/AC37).

    SPEC-v1.2.md "Multi-user support": `user_id` is the ACTING user (the
    chat id `main.py`'s `on_message(chat_id, text)` extracted from the
    inbound update, R-C2) -- threaded into every scoped db/core call
    below and every `channel.send*` call as the recipient, so this
    function's entire body operates on, and replies only to, that one
    user's own data (R-D4/AC-U-ISO). Required (no default): a caller
    cannot accidentally invoke this without stating who is acting.

    Integration step (SPEC-v1.2.md §11, landed after all three parallel
    modules reported done): `command.kind in ("lang", "quiet", "remind")`
    are now handled here, same recognize-shape/execute split and
    placement convention as `target`/`help`/`habits` -- routed to
    `core/preferences.execute_lang`/`execute_quiet` and `core/schedules.
    execute_remind` respectively, plain `send` (a settings reply is not a
    log confirmation, same posture as `target`). The five `access`-owned
    kinds (`start`/`approve`/`block`/`users`/`invite`) are deliberately
    NOT handled here -- `main.py`'s `on_message` closure intercepts them
    BEFORE ever calling this function (the access gate, `access.
    handle_gate`, has to run first regardless -- R-A1 -- and `execute_
    admin` needs `owner_chat_id`, which isn't one of this function's
    params); reaching this function with one of those five kinds anyway
    (a caller that bypassed `on_message`, e.g. `--dry-run`) is a safe
    no-op, not the generic edit-fallthrough every unhandled kind used to
    silently become. `lang`/`user_id` are also both now used to resolve
    the acting user's own stored `/lang` preference (`_stored_language_
    pref`) into this reply's own language, alongside the inbound text's
    detected language and the global config force -- so a user who ran
    `/lang th` gets Thai for every subsequent reply, not just the `/lang`
    confirmation itself (R-P1)."""
    registry = registry or HabitRegistry.from_config(config)
    # SPEC-v1.2.md R-P1 call site #1 of 5 (integration step): the acting
    # user's own stored /lang preference now participates in "auto"
    # resolution, alongside the still-undefeated global config force and
    # the inbound text's own detected language.
    lang = i18n.resolve_reply_language(text, config, user_pref=_stored_language_pref(db, user_id))
    command = commands.dispatch(text, registry)
    if command is not None:
        if command.kind in ("start", "approve", "block", "users", "invite"):
            # Integration step (IMPL-v1.2-access.md's own "load-bearing,
            # not stylistic" warning): these five kinds are meant to be
            # intercepted by main.py's `on_message` closure, BEFORE this
            # function is ever called -- `access.execute_admin` needs
            # `owner_chat_id`, which isn't a parameter here, and sends via
            # a different addressing convention than dry-run's print()
            # path. Reaching this point means some caller bypassed that
            # routing (e.g. a direct `handle_inbound_message` call,
            # `--dry-run`). Rather than silently falling through to the
            # generic `command.kind == "undo" else _execute_edit(...)`
            # branch below -- which would treat an access Command's empty
            # `category=None`/`value_num=None` as "edit the user's last
            # log entry of ANY category to value None", a genuine
            # data-corruption risk discovered while wiring this
            # integration step, not a hypothetical -- this is a safe
            # no-op instead.
            if dry_run:
                print({"kind": command.kind, "note": "handled by on_message's admin routing, not here"})
            return
        if command.kind == "remind":
            reply = await schedules.execute_remind(
                command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
            )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "lang":
            reply = await preferences.execute_lang(command, db=db, lang=lang, user_id=user_id)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "quiet":
            reply = await preferences.execute_quiet(command, db=db, lang=lang, user_id=user_id)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "query":
            # ROADMAP.md v0.8.0: read-only (AC8.5) -- never touches health_monitor
            # deferral or the extractor below; classify_query_intent fails closed
            # to the query_cant_answer catalog message on any error, including
            # Ollama being unreachable, so this branch never raises (AC8.4).
            answer = await query.answer_question(
                text, db=db, llm=llm, registry=registry, config=config, lang=lang, user_id=user_id, clock=clock
            )
            if dry_run:
                print(answer)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, answer)
            return
        if command.kind == "snooze":
            await _execute_snooze(
                db, channel, config, clock, command, registry, lang, scheduler, reminder_state, dry_run, user_id
            )
            return
        if command.kind == "target":
            # SPEC-v1.1.md R-T10: a target reply is not a log confirmation
            # (R-U2 scope) -- plain `send`, no undo button.
            reply = await targets_command.execute_target(
                command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
            )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "help":
            # SPEC-v1.1.md R-D2/AC35: deterministic, LLM-free, reads only
            # `config` -- runs here, before the health-monitor deferral
            # check below, so it works with Ollama down.
            reply = discoverability.build_help_text(config, lang)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "habits":
            # SPEC-v1.1.md R-D3/AC37: deterministic, LLM-free, read-only --
            # same "before the deferral check" placement as "help" above.
            reply = discoverability.build_habits_overview(db, config, registry, clock, lang, user_id)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "history":
            # SPEC-v1.4.md R-A1: deterministic, LLM-free, read-only, and --
            # unlike `/audit` -- available to ANY active user for their OWN
            # data (no owner check; the access gate in `on_message` already
            # established this chat is active before `handle_inbound_
            # message` was ever called). Same "before the deferral check"
            # placement as `help`/`habits`/`target` above, so it works with
            # Ollama down.
            reply = history_view.render_history(
                db, config, registry, lang, user_id=user_id, category=command.category, limit=command.limit
            )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if dry_run:
            print({"kind": command.kind, "category": command.category, "value_num": command.value_num})
            return
        assert channel is not None, "channel is required outside dry-run"
        if command.kind == "undo":
            await _execute_undo(db, channel, config, clock, registry, lang, user_id)
        else:
            await _execute_edit(db, channel, config, clock, command, registry, lang, user_id)
        return

    if not dry_run and health_monitor is not None and not health_monitor.ollama_up:
        assert channel is not None, "channel is required outside dry-run"
        now = clock()
        ts = now.isoformat(timespec="seconds")
        db.insert_log(LogEntry(None, user_id, ts, "unparsed", None, None, text, source))
        await channel.send(user_id, i18n.t("deferred_ack", lang))
        return

    # SPEC-v1.1.md R-T12-R-T16 (OQ3, module `targets`): the full-NL
    # target-intent step. `command` is guaranteed None here (every branch
    # above returns) -- i.e. the message matched neither an anchored
    # command nor query-shape -- and the health-monitor deferral check just
    # above already returned for the Ollama-DOWN case, so reaching this
    # point already implies "Ollama up" whenever a health_monitor is wired
    # (R-T16: no NL classification call is made while Ollama is down; the
    # message falls through unchanged to the deferred/parse path below).
    # `looks_like_target_phrasing` is a cheap cost gate only (R-T13.1) --
    # `classify_target_intent` is independently fail-closed (R-T14), so a
    # `None` result here (gate miss, low confidence, or any classifier
    # failure) falls straight through to the normal log-parsing path,
    # unchanged (R-C5).
    if health_monitor is None or health_monitor.ollama_up:
        if target_nl.looks_like_target_phrasing(text):
            intent = await target_nl.classify_target_intent(text, llm, registry, config)
            if intent is not None:
                set_command = commands.Command(
                    kind="target",
                    category=intent.habit_id,
                    value_num=intent.goal_base_unit,
                    target_action="set",
                )
                # SPEC-v1.3.md §2.1/R-C1 (integration step): the full-NL
                # path is the one `target_set` call site that must NOT
                # default to "command" -- distinguishing "/target water
                # 2000" from "from now on I want to drink 2.5L a day" in
                # the audit trail is the entire point of `source`'s
                # existence for this action.
                reply = await targets_command.execute_target(
                    set_command, db=db, config=config, registry=registry, lang=lang, user_id=user_id, source="nl"
                )
                if dry_run:
                    print(reply)
                    return
                assert channel is not None, "channel is required outside dry-run"
                await channel.send(user_id, reply)
                return  # AC29/AC30: no `logs` row is written for a target-intent hit

    result = await parse_message(text, llm, registry, config.ollama.confidence_threshold)

    if dry_run:
        print(asdict(result))
        return

    assert channel is not None, "channel is required outside dry-run"

    now = clock()
    ts = now.isoformat(timespec="seconds")
    today_str = now.date().isoformat()

    habit = registry.get(result.category)
    if habit is None:
        await channel.send(user_id, i18n.t("clarifying_question", lang))
        return

    # ROADMAP.md v0.10.0 AC10.2/AC10.4: snapshot whether today already
    # satisfied this habit's streak condition BEFORE writing the new row --
    # comparing this to the post-insert state is how `crossed_milestone`
    # detects a genuine crossing without any persisted "already announced"
    # flag (see its docstring). Skipped entirely when gamification is
    # disabled, so a disabled install pays no extra DB reads for this.
    was_qualified_before = (
        streaks.day_qualifies(db, config, habit, today_str, user_id) if config.gamification.enabled else False
    )

    entry = log_entry_from_result(habit, result, ts, text, source, user_id)
    # SPEC-v1.1.md R-U2: the inserted row's id drives the inline "Undo"
    # button attached to this confirmation below.
    row_id = db.insert_log(entry)
    undo_buttons = undo_ui.undo_button(row_id, lang)

    milestone_suffix = ""
    if config.gamification.enabled:
        crossed = streaks.crossed_milestone(db, config, habit, now.date(), was_qualified_before, user_id)
        if crossed is not None:
            milestone_suffix = "\n\n" + i18n.t("milestone_reached", lang, streak=crossed, label=habit.label(lang))

    if habit.id == "water":
        water_ml = int(result.value)  # type: ignore[arg-type]
        total = db.water_total_ml(user_id, today_str)
        # SPEC-v1.1.md R-T5/AC23: a stored `/target water …` override now
        # wins here too (AC24: byte-identical when no override is set).
        goal = targets.effective_goal(db, habit, config, user_id)
        pct = round(100 * total / goal) if goal else 0
        await channel.send_actionable(
            user_id,
            i18n.t("water_confirmation", lang, water_ml=water_ml, total=int(total), goal=goal, pct=pct)
            + milestone_suffix,
            undo_buttons,
        )
        return

    if habit.id == "stretch":
        stretch_min = int(result.value)  # type: ignore[arg-type]
        count = db.stretch_count(user_id, today_str)
        await channel.send_actionable(
            user_id,
            i18n.t("stretch_confirmation", lang, stretch_min=stretch_min, ordinal=ordinal(count), count=count)
            + milestone_suffix,
            undo_buttons,
        )
        return

    if habit.id == "diary":
        diary_text = str(result.value)
        reflection = await llm.chat_text(
            DIARY_REFLECTION_SYSTEM_PROMPT.format(language_instruction=i18n.language_instruction(lang)),
            DIARY_REFLECTION_USER_TEMPLATE.format(diary_text=diary_text),
        )
        if not reflection:
            reflection = i18n.t("diary_reflection_fallback", lang)
        await channel.send_actionable(
            user_id, i18n.t("diary_confirmation", lang, reflection=reflection) + milestone_suffix, undo_buttons
        )
        return

    # Any other configured habit: type-generic confirmation (AC9).
    message = await _generic_confirmation(db, llm, habit, result.value, today_str, lang, config, user_id)
    await channel.send_actionable(user_id, message + milestone_suffix, undo_buttons)


async def reparse_pending_unparsed(
    db: Database, llm: OllamaClient, channel: Channel, config: Config, registry: HabitRegistry | None = None
) -> None:
    """ROADMAP.md v0.4.0 AC3.3 recovery path: re-parse every row deferred
    while Ollama was DOWN (category='unparsed'), convert it to its real
    category, and confirm. Rows come straight from `db.pending_unparsed()`
    -- a plain query against persisted state, not an in-memory queue --
    so this also picks up rows deferred by a *previous* process run. Two
    call sites in `async_main`: once at startup (catches up any backlog
    left over from before a restart) and once per DOWN->UP transition via
    `health_monitor`'s `on_ollama_recovered` callback.

    A row that's still unparseable after Ollama is back (genuinely bad
    input, not an outage) is left as 'unparsed' and logged -- it is not
    retried again until the next DOWN->UP transition.

    ROADMAP.md v0.6.0 AC6.1/AC6.3: the recovery confirmation is still a
    reply to the *original* message the user sent (just delayed), so its
    language is resolved from that row's `raw_message`, same rule as a
    live confirmation.

    ROADMAP.md v0.7.0: `registry` defaults like `handle_inbound_message`'s;
    built-ins keep their byte-identical `recovered_*` catalog entries,
    any other habit renders via `_send_recovered_generic` (AC9), and
    `reclassify_log` is now also stamped with `habit_type` (SPEC-v0.7.md
    §4 R14).

    SPEC-v1.2.md R-D1/R-D3: `db.pending_unparsed()` stays GLOBAL (every
    deferred row across every user, in one pass), but each row carries its
    own `user_id` (stamped at deferral time in `handle_inbound_message`)
    -- the recovery confirmation is addressed to THAT row's own chat, not
    a pinned global one, so a backlog spanning multiple users resolves
    correctly to each of them."""
    registry = registry or HabitRegistry.from_config(config)
    pending = db.pending_unparsed()
    if not pending:
        return

    logger.info("Re-parsing %d deferred message(s)", len(pending))
    for row in pending:
        text = row["raw_message"]
        user_id = row["user_id"]
        # SPEC-v1.2.md R-P1 call site #2 of 5 (integration step): same
        # stored-preference threading as the live handle_inbound_message
        # path -- a recovered confirmation respects the row's own user's
        # /lang choice too, not just their detected input language.
        lang = i18n.resolve_reply_language(text, config, user_pref=_stored_language_pref(db, user_id))
        result = await parse_message(text, llm, registry, config.ollama.confidence_threshold)

        habit = registry.get(result.category)
        if habit is None:
            logger.warning(
                "Deferred message id=%s still unparseable after Ollama recovery; left as 'unparsed': %r",
                row["id"],
                text,
            )
            continue

        recovered_entry = log_entry_from_result(habit, result, row["ts"], text, row["source"], user_id)
        db.reclassify_log(
            row["id"], habit.id, recovered_entry.value_num, recovered_entry.value_text, habit_type=habit.type
        )
        # SPEC-v1.1.md R-U2: a recovery re-confirmation is itself an
        # interactive log confirmation -- the deferred row's id (unchanged
        # by reclassify_log) drives its own undo button.
        undo_buttons = undo_ui.undo_button(row["id"], lang)

        if habit.id == "water":
            await channel.send_actionable(
                user_id, i18n.t("recovered_water", lang, water_ml=int(result.value)), undo_buttons  # type: ignore[arg-type]
            )
        elif habit.id == "stretch":
            await channel.send_actionable(
                user_id, i18n.t("recovered_stretch", lang, stretch_min=int(result.value)), undo_buttons  # type: ignore[arg-type]
            )
        elif habit.id == "diary":
            await channel.send_actionable(user_id, i18n.t("recovered_diary", lang), undo_buttons)
        else:
            await _send_recovered_generic(channel, user_id, habit, result.value, lang, undo_buttons)


def seed_fake_data(db: Database, config: Config, user_id: str) -> None:
    """Insert a few days of plausible fake logs so --seed lets the weekly
    review be exercised fully offline. SPEC-v1.2.md's own risk note
    ("confirm --seed/CLI tools attribute to owner"): `user_id` is always
    the owner's chat id here -- `async_main` resolves it from `.env` via
    `load_secrets()` before calling this."""
    now = datetime.now()
    rng = random.Random(42)
    for offset in range(6, -1, -1):
        day = now - timedelta(days=offset)
        for _ in range(rng.randint(2, 6)):
            ml = rng.choice([250, 300, 500, 600])
            ts = day.replace(hour=rng.randint(8, 20), minute=rng.randint(0, 59), second=0, microsecond=0)
            db.insert_log(
                LogEntry(None, user_id, ts.isoformat(timespec="seconds"), "water", float(ml), None, f"seed {ml}ml water", "reply")
            )
        if rng.random() > 0.3:
            minutes = rng.choice([5, 10, 15])
            ts = day.replace(hour=rng.randint(11, 17), minute=rng.randint(0, 59), second=0, microsecond=0)
            db.insert_log(
                LogEntry(
                    None, user_id, ts.isoformat(timespec="seconds"), "stretch", float(minutes), None,
                    f"seed {minutes} min stretch", "reply",
                )
            )
        if rng.random() > 0.4:
            ts = day.replace(hour=21, minute=30, second=0, microsecond=0)
            db.insert_log(
                LogEntry(
                    None, user_id, ts.isoformat(timespec="seconds"), "diary", None, "seed diary entry",
                    "seed diary entry", "reply",
                )
            )
    logger.info("Seeded fake data for the last 7 days into %s", db.db_path)


async def async_main(args: argparse.Namespace) -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.app.log_level)

    # ROADMAP.md v0.7.0: built once from the loaded config and threaded
    # through every call site below (SPEC-v0.7.md §4 R3, §5 main.py note).
    registry = HabitRegistry.from_config(config)

    # --seed and --dry-run don't need Telegram credentials for the channel
    # itself, but SPEC-v1.2.md's own risk note requires them to attribute
    # to the OWNER (never leave a NULL-user_id row) -- the owner id only
    # exists in `.env`, so both branches now load secrets too, just to get
    # it. This is a new `.env` dependency for these two CLI paths (they
    # didn't need it pre-v1.2); everything else about them is unchanged.
    if args.seed:
        try:
            secrets = load_secrets()
        except ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        db = Database(config.app.db_path)
        db.attribute_legacy_to_owner(secrets.telegram_chat_id)
        seed_fake_data(db, config, secrets.telegram_chat_id)
        db.close()
        return

    if args.dry_run is not None:
        try:
            secrets = load_secrets()
        except ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        db = Database(config.app.db_path)
        db.attribute_legacy_to_owner(secrets.telegram_chat_id)
        llm = OllamaClient(config.ollama.base_url, config.ollama.model_chain, config.ollama.timeout_seconds)
        await handle_inbound_message(
            args.dry_run,
            db=db,
            llm=llm,
            channel=None,
            config=config,
            dry_run=True,
            registry=registry,
            user_id=secrets.telegram_chat_id,
        )
        await llm.aclose()
        db.close()
        return

    # --migrate / --backup / --restore are dev/ops affordances -- like
    # --seed and --dry-run, they never touch Telegram (ROADMAP v0.3.0).
    # getattr(...) defaults keep this compatible with hand-built
    # argparse.Namespace-alikes (pre-existing test fixtures) that predate
    # these flags and don't set them.
    if getattr(args, "migrate", False):
        db = Database(config.app.db_path)  # Database.__init__ runs the migration runner
        print(f"Migrated schema {db.schema_version_before} -> {db.schema_version}")
        db.close()
        return

    if getattr(args, "backup", False):
        try:
            dest = backup_db(config.app.db_path, config.backup.dir, retain=config.backup.retain)
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Backup written: {dest}")
        return

    restore_arg = getattr(args, "restore", None)
    if restore_arg:
        if not getattr(args, "yes", False):
            print(
                "ERROR: --restore is destructive (it replaces the live DB). "
                "Re-run with --yes to confirm.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            restored = restore_db(restore_arg, config.app.db_path, config.backup.dir, retain=config.backup.retain)
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Restored {restore_arg} -> {restored}")
        return

    try:
        secrets = load_secrets()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    db = Database(config.app.db_path)
    # SPEC-v1.2.md R-M2: startup attribution, called ONCE right after
    # `load_secrets` -- the owner id only lives in `.env`, unreachable
    # from the migration runner (§2.2). Idempotent: upserts the owner's
    # `users` row (role=owner, status=active) and backfills every
    # previously-NULL `logs`/`habit_targets` row to the owner. After the
    # first run this is a no-op (AC-M2), so it's safe to call on every
    # startup unconditionally.
    db.attribute_legacy_to_owner(secrets.telegram_chat_id)
    # SPEC-v1.3.md R-W3: retention, run ONCE at startup (cheap housekeeping,
    # not per-insert) -- `retention_days = 0` means "keep forever", so
    # skip the DELETE (and the cutoff computation) entirely rather than
    # passing a cutoff that would prune nothing anyway; pruning itself is
    # NOT audited (R-W3's own explicit carve-out -- no `audit.record` call
    # here). Placed right after `attribute_legacy_to_owner` -- both are
    # "once, at real-process startup" housekeeping over the same `db`.
    if config.audit.retention_days > 0:
        cutoff = (datetime.now() - timedelta(days=config.audit.retention_days)).isoformat(timespec="seconds")
        pruned = db.prune_audit(cutoff)
        if pruned:
            logger.info("Pruned %d audit_log row(s) older than %s (retention_days=%d)", pruned, cutoff, config.audit.retention_days)
    llm = OllamaClient(
        config.ollama.base_url,
        config.ollama.model_chain,
        config.ollama.timeout_seconds,
        retry_attempts=config.ollama.retry_attempts,
        retry_backoff_seconds=config.ollama.retry_backoff_seconds,
    )
    channel = TelegramChannel(
        secrets.telegram_bot_token,
        secrets.telegram_chat_id,
        config.telegram.poll_timeout,
        backoff_initial_seconds=config.telegram.backoff_initial_seconds,
        backoff_max_seconds=config.telegram.backoff_max_seconds,
    )

    # ROADMAP.md v0.9.0: one `ReminderState` for the process lifetime,
    # updated by every `send_reminder` that actually fires (not suppressed
    # by quiet hours/goal-met) and read by `_execute_snooze` to resolve a
    # bare "snooze"/"เลื่อน" command to a habit (AC9.3). SPEC-v1.2.md R-S2:
    # `last_habit_id` is now a per-user map.
    reminder_state = ReminderState()

    if args.test_reminder:
        # ROADMAP.md v0.7.0 integration: `send_reminder` takes a real
        # `Habit`, not a bare category string (SPEC-v0.7.md §5, module M2's
        # `core/reminders.py`) -- resolve it from the registry first.
        # `--test-reminder`'s argparse `choices` are already restricted to
        # `_DEFAULT_REGISTRY.ids()`, so `habit` should never be None for a
        # value that reached this branch at all; the check is defensive
        # belt-and-suspenders (e.g. a config.toml that dropped a habit
        # between process invocations) rather than an expected runtime path.
        habit = registry.get(args.test_reminder)
        if habit is None:
            print(f"ERROR: {args.test_reminder!r} is not a configured habit", file=sys.stderr)
            await channel.aclose()
            await llm.aclose()
            db.close()
            sys.exit(1)
        try:
            # ROADMAP.md v0.9.0: `--test-reminder` also honors quiet hours/
            # goal-met (db/config passed through) so a manual test reflects
            # real scheduled behavior; `state` lets a follow-up manual
            # "snooze" also work against a `--test-reminder`-fired habit.
            # SPEC-v1.2.md: addressed to the owner (this is a manual ops
            # tool, not a per-user fan-out).
            await send_reminder(
                channel, secrets.telegram_chat_id, habit, i18n.resolve_unprompted_language(config), db, config, reminder_state
            )
        except httpx.HTTPError as exc:
            print(f"ERROR: Failed to send test reminder: {exc}", file=sys.stderr)
            await channel.aclose()
            await llm.aclose()
            db.close()
            sys.exit(1)
        await channel.aclose()
        await llm.aclose()
        db.close()
        return

    # AC2.1 / SPEC-v0.7.md §4 R4/R5: build the generic extraction schema
    # and the registry-driven system/user prompt from the live registry
    # (module M1's `build_extraction_system_prompt`/
    # `build_extraction_user_prompt`, wired here now that M1 has landed --
    # was interim-wired to the fixed v0.6.0 prompt during the
    # shared-surface build, see `IMPL.md`'s v0.7.0 "Known limitations" #3),
    # then probe each configured model's schema conformance once at
    # startup, purely for operator visibility (logged inside the client).
    # Never allowed to crash startup -- probe_schema_support() itself never
    # raises, but this is belt-and-suspenders against a future change to it.
    extraction_schema = build_extraction_schema(registry.category_enum())
    probe_system_prompt = build_extraction_system_prompt(registry)
    probe_user_prompt = build_extraction_user_prompt("500ml")
    try:
        await llm.probe_schema_support(probe_system_prompt, probe_user_prompt, extraction_schema)
    except Exception:
        logger.exception("Ollama schema conformance probe failed unexpectedly; continuing startup anyway")

    # SPEC-v1.1.md R-U1/R-D4/AC1-AC2/AC40, extended by SPEC-v1.2.md §11 and
    # SPEC-v1.4.md R-A2 integration steps: register the bot command menu
    # once at startup -- `/start` + `/undo` (undo_ui's own contribution) +
    # `/target` (this integration step's, see TARGET_COMMAND_DESCRIPTIONS
    # above) + `/help` + `/habits` (discoverability) + `/remind` + `/lang`
    # + `/quiet` + `/history` (see START_/REMIND_/LANG_/QUIET_/HISTORY_
    # COMMAND_DESCRIPTIONS above for why the four true admin commands --
    # and owner-only `/audit` -- are deliberately excluded), English
    # default + a Thai set. 9 public commands total. A transport error
    # here is logged and never crashes startup (AC2) -- same belt-and-
    # suspenders posture as the schema-conformance probe just above.
    undo_command_menu = undo_ui.command_menu_entries()
    command_menu = {
        lang: (
            [("start", START_COMMAND_DESCRIPTIONS[lang])]
            + undo_command_menu[lang]
            + [("target", desc)]
            + DISCOVERABILITY_COMMAND_DESCRIPTIONS[lang]
            + [("remind", REMIND_COMMAND_DESCRIPTIONS[lang])]
            + [("lang", LANG_COMMAND_DESCRIPTIONS[lang])]
            + [("quiet", QUIET_COMMAND_DESCRIPTIONS[lang])]
            + [("history", HISTORY_COMMAND_DESCRIPTIONS[lang])]
        )
        for lang, desc in TARGET_COMMAND_DESCRIPTIONS.items()
    }
    try:
        await channel.set_my_commands(command_menu)
    except Exception:
        logger.exception("set_my_commands failed at startup; continuing")

    # ROADMAP.md v0.4.0 AC3.3: catch up on anything deferred by a
    # *previous* process run before entering the main loop. If Ollama is
    # still down right now, parse_message just fails closed per row (no
    # change, no confirmation) and they stay 'unparsed' for the health
    # monitor's own DOWN->UP callback to pick up later -- never raises.
    try:
        await reparse_pending_unparsed(db, llm, channel, config, registry)
    except Exception:
        logger.exception("Startup re-parse of deferred messages failed unexpectedly; continuing")

    async def on_ollama_recovered() -> None:
        await reparse_pending_unparsed(db, llm, channel, config, registry)

    health_monitor = HealthMonitor(
        config.ollama.base_url,
        secrets.telegram_bot_token,
        secrets.telegram_chat_id,
        interval_seconds=config.health.interval_seconds,
        channel=channel,
        on_ollama_recovered=on_ollama_recovered,
        language=i18n.resolve_unprompted_language(config),
    )

    # SPEC-v1.2.md R-S1: per-habit reminders now fire from a single
    # minutely tick job (`run_due_reminders`), replacing the removed
    # `schedule_reminders`'s one-cron-per-config-time fan-out. `coalesce`
    # + a small `misfire_grace_time` mean a paused/restarted process never
    # replays a missed minute (no double-send); `max_instances=1` means a
    # slow tick can't overlap the next one. With an empty
    # `user_reminder_times` store (fresh DB / no one has customized yet),
    # every user -- owner included -- falls back to the global config
    # times, so the owner's reminders fire at exactly the v1.1 minutes
    # (AC-M3/AC-S1).
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_due_reminders,
        trigger=CronTrigger(second=0, timezone=config.app.timezone),
        args=[channel, config, registry, db, reminder_state],
        id="reminder_tick",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )

    review_hour, review_minute = (int(x) for x in config.weekly_review.time.split(":"))

    async def weekly_review_job() -> None:
        # SPEC-v1.2.md R-S3: the weekly-review TIME stays global, but the
        # send fans out to every active user, each from their own data;
        # a user with no logs in the 7-day window is skipped entirely (no
        # empty review for a brand-new user).
        today = date.today()
        week_start = (today - timedelta(days=6)).isoformat() + "T00:00:00"
        week_end = today.isoformat() + "T23:59:59"
        for user_id in db.active_user_ids():
            if not db.logs_between(user_id, week_start, week_end):
                continue
            # SPEC-v1.2.md R-P1 call site #3 of 5 (integration step):
            # resolved per user now, not once globally -- each active
            # user's review/charts go out in THEIR own stored language.
            lang = i18n.resolve_unprompted_language(config, user_pref=_stored_language_pref(db, user_id))
            text = await run_weekly_review(db, config, registry, llm, lang, user_id, today=today)
            await channel.send(user_id, text)

            # ROADMAP.md v1.0.0 AC1.0.1: attach chart images when
            # `[charts] enabled` and matplotlib is present; `[]` (disabled,
            # matplotlib missing, or nothing chartable) leaves the review
            # exactly as sent above -- text-only, no crash. `today` is
            # shared with the text review call above so both cover the
            # same 7-day window even if this job happens to straddle
            # midnight.
            try:
                image_captions = render_weekly_review_charts(db, config, registry, lang, user_id, today=today)
            except Exception:
                logger.exception("Failed to render weekly review charts for %s; continuing with text-only review", user_id)
                image_captions = []
            for image, caption in image_captions:
                try:
                    await channel.send_image(user_id, image, caption)
                except Exception:
                    logger.exception("Failed to send a weekly review chart image to %s; continuing", user_id)

    scheduler.add_job(
        weekly_review_job,
        trigger=CronTrigger(
            day_of_week=config.weekly_review.day_of_week,
            hour=review_hour,
            minute=review_minute,
            timezone=config.app.timezone,
        ),
        id="weekly_review",
        replace_existing=True,
    )

    # ROADMAP.md v0.10.0 AC10.3/AC10.4: the end-of-day recap, gated by its
    # own `config.gamification.daily_summary` flag (independent of
    # `gamification.enabled`, which only affects milestone lines -- AC10.4
    # "no behavioral leakage") and, like every other unprompted send,
    # suppressed during a configured quiet-hours window (the GLOBAL
    # window -- R-S3 only makes per-*habit reminder* times per-user, not
    # the daily-summary send itself).
    summary_hour, summary_minute = (int(x) for x in config.gamification.daily_summary_time.split(":"))

    async def daily_summary_job() -> None:
        # SPEC-v1.2.md R-S3: fans out to every active user, each from
        # their own today's data; a user who logged nothing today is
        # skipped (no empty summary).
        if not config.gamification.daily_summary:
            return
        if is_quiet_hours_now(config):
            logger.info("Suppressing daily summary: inside a quiet-hours window")
            return
        today = date.today()
        today_str = today.isoformat()
        for user_id in db.active_user_ids():
            has_logs_today = any(db.count(user_id, habit.id, today_str) > 0 for habit in registry)
            if not has_logs_today:
                continue
            # SPEC-v1.2.md R-P1 call site #4 of 5 (integration step):
            # resolved per user now, not once globally.
            lang = i18n.resolve_unprompted_language(config, user_pref=_stored_language_pref(db, user_id))
            text = streaks.run_daily_summary(db, config, registry, lang, user_id, today=today)
            await channel.send(user_id, text)

    scheduler.add_job(
        daily_summary_job,
        trigger=CronTrigger(
            hour=summary_hour,
            minute=summary_minute,
            timezone=config.app.timezone,
        ),
        id="daily_summary",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started; entering Telegram long-poll loop")

    async def on_message(chat_id: str, text: str, display_name: str | None = None) -> None:
        # SPEC-v1.2.md R-C2: `chat_id` (the acting user) is threaded
        # straight into `handle_inbound_message`'s `user_id`. `display_name`
        # is TelegramChannel's own newly-added third argument (channels/
        # telegram.py's `_display_name_of`, IMPL-v1.2-access.md's "Known
        # limitations" #1) -- defaulted to `None` so any caller/fake that
        # still passes only 2 args (every pre-integration test) is
        # unaffected.
        #
        # SPEC-v1.2.md §11 integration step, in order:
        # 1. The access gate (R-A1) runs FIRST, before any logging/LLM/
        #    command work -- an unknown/pending/blocked chat gets
        #    onboarded/refused here and never reaches dispatch at all.
        # 2. The five owner-only/onboarding kinds (R-A4/R-A5) are routed
        #    to `access.execute_admin` here, BEFORE `handle_inbound_
        #    message` -- that function has no `owner_chat_id` param and
        #    would otherwise silently mis-handle them (see its own
        #    docstring's "load-bearing, not stylistic" note).
        # 3. Everything else (including the `lang`/`quiet`/`remind` kinds,
        #    now handled inside `handle_inbound_message` itself) proceeds
        #    exactly as before.
        lang = i18n.resolve_reply_language(text, config)
        proceed = await access.handle_gate(
            db, channel, config, secrets.telegram_chat_id, chat_id, display_name, text, lang=lang
        )
        if not proceed:
            return

        command = commands.dispatch(text, registry)
        if command is not None and command.kind in ("start", "approve", "block", "users", "invite"):
            await access.execute_admin(
                command,
                db=db,
                channel=channel,
                config=config,
                owner_chat_id=secrets.telegram_chat_id,
                chat_id=chat_id,
                lang=lang,
            )
            return

        # SPEC-v1.3.md R-V3 (integration step): `/audit` is owner-only,
        # same "intercept here, before handle_inbound_message" reasoning
        # as the admin block above -- LLM-free, must work with Ollama
        # down, and `audit_view.render_recent` needs `owner_chat_id`
        # (to render the owner's own rows as "you"), which `handle_
        # inbound_message` doesn't have. A non-owner gets NO reply at
        # all (reveals nothing, same posture as approve/block/users/
        # invite) -- `access.classify` re-checked here even though
        # `handle_gate` already let this chat proceed (active does not
        # imply owner, the same belt-and-suspenders `execute_admin`
        # itself already applies to its own four owner-only kinds).
        if command is not None and command.kind == "audit":
            if access.classify(db, chat_id) == "owner":
                reply = audit_view.render_recent(
                    db, config, lang, limit=command.limit, owner_chat_id=secrets.telegram_chat_id
                )
                await channel.send(chat_id, reply)
            return

        await handle_inbound_message(
            text,
            db=db,
            llm=llm,
            channel=channel,
            config=config,
            user_id=chat_id,
            health_monitor=health_monitor,
            registry=registry,
            scheduler=scheduler,
            reminder_state=reminder_state,
        )

    async def on_callback(chat_id: str, data: str, source_text: str, callback_id: str) -> None:
        # SPEC-v1.1.md R-U4/R-U5 / SPEC-v1.2.md R-C2/R-C3: route an
        # inline-button tap to the shared undo_ui handler, with the
        # tapping chat id first (for the ownership check). `TelegramChannel.
        # run` always calls `answer_callback_query` itself right after
        # awaiting this (R-U4), so this closure doesn't need to.
        #
        # SPEC-v1.2.md R-A1 ("every inbound update is gated before any ...
        # command work") + this integration step's own punch list: a
        # non-active chat's button tap must not execute -- even a stale
        # button from before the tapping chat was ever approved, or from
        # after they were blocked. Unlike on_message, no onboarding reply
        # is sent here: a tap isn't itself a message to onboard from (an
        # unknown chat has no button to tap in the first place unless the
        # bot itself sent them one, which only happens post-approval), and
        # `TelegramChannel.run` always dismisses the spinner via
        # `answer_callback_query` right after this returns regardless --
        # a silent no-op is the correct posture for a stale/spoofed tap
        # from a non-active chat.
        if access.classify(db, chat_id) not in ("owner", "active"):
            return
        await undo_ui.handle_undo_callback(
            chat_id, data, source_text, callback_id, db=db, channel=channel, config=config, clock=datetime.now, registry=registry
        )

    # ROADMAP.md v0.4.0 scope item 5: the health monitor runs as its own
    # asyncio task alongside the scheduler and the inbound loop.
    health_task = asyncio.create_task(health_monitor.run())
    try:
        await channel.run(on_message, on_callback=on_callback)
    finally:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        await health_monitor.aclose()
        scheduler.shutdown(wait=False)
        await channel.aclose()
        await llm.aclose()
        db.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="habit_assistant", description="Local habit-tracking assistant")
    parser.add_argument(
        "--test-reminder",
        metavar="CATEGORY",
        # ROADMAP.md v0.7.0 (SPEC-v0.7.md §4 R15): choices come from the
        # (default) habit registry's ids, not the fixed REMINDER_TEXTS
        # keys -- same set for the shipped three habits, but this is what
        # makes a newly configured habit show up here too.
        choices=sorted(_DEFAULT_REGISTRY.ids()),
        help="Fire one reminder immediately and exit",
    )
    parser.add_argument("--seed", action="store_true", help="Insert a few days of fake logs for weekly-review testing")
    parser.add_argument(
        "--dry-run",
        metavar="MESSAGE",
        default=None,
        help="Parse MESSAGE and print structured output without writing DB or sending a confirmation",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Apply pending schema migrations and exit (prints from -> to version)",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Back up the DB to [backup].dir and exit",
    )
    parser.add_argument(
        "--restore",
        metavar="FILE",
        default=None,
        help="Restore the DB from FILE (destructive; requires --yes)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm a destructive operation (required with --restore)",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
