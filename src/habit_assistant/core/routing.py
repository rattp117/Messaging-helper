"""SPEC-REFACTOR.md Stage 2: inbound routing, split out of `main.py`
(rule 9) -- `on_message`/`on_callback` (the Telegram-loop entry points),
`handle_inbound_message` (command dispatch -> act+confirm OR parse ->
validate -> write+confirm OR clarifying question), `reparse_pending_
unparsed` (the Ollama-recovery sweep), and the command-executor/formatter
helpers only these use.

`on_message`/`on_callback` used to be closures inside `main.py:async_main`,
capturing `db`/`llm`/`channel`/`config`/`secrets`/`provider`/`scheduler`/
`reminder_state`/`health_monitor` from that function's own locals (OQ3).
They are plain functions here, taking those same values as explicit
keyword parameters instead -- `core/app.py`'s `async_main` wires them up
with two small zero-arg forwarding closures at registration time (the only
closures left, and they carry no logic of their own, just argument
threading).

Rule 5/AC7 (dispatch-once): `handle_inbound_message` gained an optional
`command` parameter -- `on_message` dispatches once against the acting
user's registry and threads that same `Command` through, instead of
`handle_inbound_message` dispatching a second, redundant time. Every other
caller (CLI `--dry-run`, tests, `reparse_pending_unparsed`'s own sibling
callers) omits it and gets the original fall-back-and-dispatch behavior.

Back-compat note (`main.py` is the real point of this): `parse_message` is
an explicit, overridable parameter on both `handle_inbound_message` and
`reparse_pending_unparsed`, defaulting to the real parser -- `main.py`'s
own re-export wrappers always pass its OWN current module-level
`parse_message` name explicitly, which is what lets
`monkeypatch.setattr(main_module, "parse_message", fake)` keep working now
that the function bodies that call it no longer live in `main.py`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.core import (
    access,
    audit,
    audit_view,
    backfill,
    cadence,
    checkins,
    clarify,
    commands,
    confirmation,
    dashboard,
    discoverability,
    habitdef,
    heatmap,
    history_view,
    i18n,
    pause,
    preferences,
    preparse,
    query,
    quicklog,
    reactions,
    records,
    render_budget,
    reply_attribution,
    routines,
    schedules,
    streaks,
    target_nl,
    targets,
    targets_command,
    trends,
    undo_ui,
    user_prefs,
    wrapped,
)
from habit_assistant.core.habits import Habit, HabitRegistry, log_entry_from_result
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.parser import parse_message as _default_parse_message
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.reminders import ReminderState, send_reminder
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.storage.models import LogEntry

logger = logging.getLogger(__name__)

# Rule 5/AC7's own subtlety: `commands.dispatch` legitimately returns `None`
# for an ordinary habit message ("500ml" matches none of the 27 command
# matchers) -- so `None` cannot double as BOTH "no command was found" and
# "the caller didn't pass one, please dispatch yourself". A caller that
# dispatched and got `None` (e.g. `on_message`, threading its own result
# through) must be able to say "trust me, it's None" without triggering a
# second, redundant dispatch. This sentinel is the only way `command`'s
# default is distinguishable from an explicit `command=None`.
_NOT_DISPATCHED = object()

# SPEC-v1.10.md §4 R15, Archi-sanctioned integration extra (item 4): bounds
# just the QUOTED portion of the outage-honesty message, mirroring
# `core/clarify.py:_QUOTE_MAX_CHARS`'s identical rationale/value for the
# closure/clarify-offer messages -- a near-4096-char raw inbound message
# would otherwise push the composed outage message past Telegram's own
# `sendMessage` limit (measured by Vera in `tests/test_v110_m2_gaps.py`:
# +92..+188 chars over budget at 4000-char inputs). Never applied to the
# `text` written into the `LogEntry.raw_message` deferral row itself --
# only to what gets embedded in the sent message.
_OUTAGE_QUOTE_MAX_CHARS = 200

# SPEC-v1.10.md §4 R4 (single-flight sweep guard): if a sweep is already
# running, a new trigger (startup, and `HealthMonitor.on_ollama_recovered`)
# logs and returns immediately -- the running sweep's own `db.
# pending_unparsed()` snapshot already covers everything deferred up to the
# outage's end. Plain module-level bool, not a lock: this app is a single
# asyncio process (`storage/db.py`'s own documented "not thread-safe by
# design" posture) and the guard is set synchronously, with no `await`
# between the check and the set, so no interleaving window exists for two
# concurrent callers to both observe `False`.
_sweep_in_progress = False

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.llm.ollama_client import OllamaClient
    from habit_assistant.storage.db import Database


async def _execute_undo(
    db: "Database",
    channel: Channel,
    config: "Config",
    clock,
    registry: HabitRegistry,
    lang: i18n.Language,
    user_id: str,
) -> None:
    """Soft-delete the most recent non-deleted log and confirm what was
    removed; nothing logged -> friendly message, no write."""
    row = db.last_log(user_id)
    if row is None:
        await channel.send(user_id, i18n.t("nothing_to_undo", lang))
        return

    await undo_ui.send_undo_confirmation(db, channel, config, clock, registry, lang, row)
    await dashboard.refresh(db, channel, config, registry, user_id, clock)


async def _execute_edit(
    db: "Database",
    channel: Channel,
    config: "Config",
    clock,
    command: commands.Command,
    registry: HabitRegistry,
    lang: i18n.Language,
    user_id: str,
) -> None:
    """Update the last matching (same-category) entry's value and
    re-confirm the new daily total; no matching entry -> friendly message,
    no write. Water/stretch keep their byte-identical v0.6 confirmations;
    any other numeric/duration habit gets a type-generic edit confirmation;
    a numeric-without-goal/boolean/text/unrecognized category sends nothing
    (editing those is out of scope) and refreshes no dashboard."""
    row = db.last_log(user_id, category=command.category)
    if row is None:
        await channel.send(user_id, i18n.t("nothing_to_edit", lang))
        return

    db.update_value(row["id"], value_num=command.value_num)
    # `row` was fetched above BEFORE the write, so `row["value_num"]` is
    # still the pre-update value here.
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
        goal = (
            targets.effective_goal(db, habit, config, user_id) if habit is not None else config.reminders.water.goal_ml
        )
        pct = round(100 * total / goal) if goal else 0
        await channel.send(
            user_id,
            i18n.t("edit_updated_water", lang, value_num=command.value_num, total=int(total), goal=goal, pct=pct),
        )
        await dashboard.refresh(db, channel, config, registry, user_id, clock)
        return
    if command.category == "stretch":
        count = db.stretch_count(user_id, today_str)
        await channel.send(
            user_id,
            i18n.t("edit_updated_stretch", lang, value_num=command.value_num, ordinal=confirmation.ordinal(count), count=count),
        )
        await dashboard.refresh(db, channel, config, registry, user_id, clock)
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
            await dashboard.refresh(db, channel, config, registry, user_id, clock)
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
                ordinal=confirmation.ordinal(count),
                count=count,
            ),
        )
        await dashboard.refresh(db, channel, config, registry, user_id, clock)
        return
    # A numeric-without-goal/boolean/text habit, or an unrecognized
    # category: out of scope, no confirmation, no dashboard refresh.


async def _execute_snooze(
    db: "Database",
    channel: Channel | None,
    config: "Config",
    clock,
    command: commands.Command,
    registry: HabitRegistry,
    lang: i18n.Language,
    scheduler: AsyncIOScheduler | None,
    reminder_state: ReminderState | None,
    dry_run: bool,
    user_id: str,
) -> None:
    """Reschedule a single one-off reminder for the most recently reminded
    habit, N minutes from now; no habit has fired a reminder yet this
    process -> a friendly fallback, no job scheduled."""
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


async def _react_to_typed_log(
    channel: Channel, config: "Config", chat_id: str, inbound_message_id: str | None, habit: Habit
) -> None:
    """Fires `reactions.react` right after a successful TYPED inbound
    log confirmation, and ONLY then -- never for a quick-log button tap,
    undo, a command reply, a clarifying question, or a deferred/unparsed
    ack."""
    if inbound_message_id is None or not config.reactions.enabled:
        return
    await reactions.react(channel, chat_id, inbound_message_id, habit)


async def handle_inbound_message(
    text: str,
    *,
    db: "Database",
    llm: "OllamaClient",
    channel: Channel | None,
    config: "Config",
    user_id: str,
    source: str = "reply",
    clock=datetime.now,
    dry_run: bool = False,
    health_monitor: HealthMonitor | None = None,
    registry: HabitRegistry | None = None,
    scheduler: AsyncIOScheduler | None = None,
    reminder_state: ReminderState | None = None,
    provider: RegistryProvider | None = None,
    inbound_message_id: str | None = None,
    reply_to_message_id: str | None = None,
    command: commands.Command | None = _NOT_DISPATCHED,  # type: ignore[assignment]
    parse_message=_default_parse_message,
) -> None:
    """Command dispatch -> (act + confirm) OR Parse -> validate -> (write
    row + confirm) OR (clarifying question). Confirmation formats are
    verbatim per SPEC.md §6.

    `command`, when given (rule 5/AC7 -- `on_message` dispatches once and
    threads its result here, EVEN when that result is `None` -- an
    ordinary habit message matches none of the 27 command matchers), is
    used as-is instead of dispatching again. Every other caller (CLI
    `--dry-run`, `reparse_pending_unparsed`'s sibling callers, every
    existing test) omits it entirely and gets the original dispatch-here
    behavior, unchanged.

    SPEC-v1.10.md §5 R-SS7/R13 ("never lose a log"): `reply_to_message_id`
    is the `message.reply_to_message.message_id` (as `str`) when the
    inbound message is a Telegram reply, else `None` -- threaded here from
    `on_message` below, itself threaded from `TelegramChannel.run`. When
    set (and `config.reply_to_reminder.enabled`, and `reminder_state` maps
    it to a habit), the reply-attribution block below (after backfill,
    before preparse) resolves a bare-value reply zero-LLM, exactly like a
    preparse hit.
    """
    registry = registry or HabitRegistry.from_config(config)
    lang = i18n.resolve_reply_language(text, config, user_pref=user_prefs.stored_language_pref(db, user_id))
    if command is _NOT_DISPATCHED:
        command = commands.dispatch(text, registry)
    if command is not None:
        if command.kind in ("start", "approve", "block", "users", "invite"):
            # Meant to be intercepted by `on_message` BEFORE this function is
            # ever called (`access.execute_admin` needs `owner_chat_id`,
            # which isn't a parameter here). Reaching this point means some
            # caller bypassed that routing -- a safe no-op, not the generic
            # edit-fallthrough every unhandled kind used to silently become.
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
        if command.kind == "checkin":
            reply = await checkins.execute_checkin(command, db=db, config=config, lang=lang, user_id=user_id)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind in ("addhabit", "delhabit"):
            active_provider = provider if provider is not None else RegistryProvider(config, db)
            base_registry = HabitRegistry.from_config(config)
            if command.kind == "addhabit":
                reply = await habitdef.execute_addhabit(
                    command,
                    db=db,
                    provider=active_provider,
                    config=config,
                    base_registry=base_registry,
                    lang=lang,
                    user_id=user_id,
                )
            else:
                reply = await habitdef.execute_delhabit(
                    command, db=db, provider=active_provider, lang=lang, user_id=user_id
                )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "dashboard":
            reply = await dashboard.execute_dashboard(
                command, db=db, channel=channel, config=config, registry=registry, lang=lang, user_id=user_id, clock=clock
            )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "heatmap":
            # `execute_heatmap` sends the PNG itself when it succeeds,
            # returning "" to signal "already delivered"; a non-empty
            # return is the text fallback for this caller to send.
            if dry_run:
                print({"kind": "heatmap", "note": "requires a real channel; not supported in --dry-run"})
                return
            assert channel is not None, "channel is required outside dry-run"
            reply = await heatmap.execute_heatmap(
                command, db=db, channel=channel, config=config, registry=registry, lang=lang, user_id=user_id, clock=clock
            )
            if reply:
                await channel.send(user_id, reply)
            return
        if command.kind == "records":
            reply = records.render(db, config, registry, lang, user_id, habit_id=command.category)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "trends":
            reply = trends.render(db, config, registry, lang, user_id, habit_id=command.category, clock=clock)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "log":
            if dry_run:
                print({"kind": "log", "note": "requires a real channel; not supported in --dry-run"})
                return
            assert channel is not None, "channel is required outside dry-run"
            buttons = quicklog.build_keyboard(registry, config, db, lang, user_id)
            if buttons:
                await channel.send_actionable(user_id, quicklog.keyboard_prompt_text(lang), buttons)
            else:
                await channel.send(user_id, quicklog.empty_keyboard_hint(lang))
            return
        if command.kind == "routine":
            if dry_run:
                print(
                    {
                        "kind": "routine",
                        "routine_action": command.routine_action,
                        "routine_name": command.routine_name,
                        "note": "requires a real channel; not supported in --dry-run",
                    }
                )
                return
            assert channel is not None, "channel is required outside dry-run"
            active_provider = provider if provider is not None else RegistryProvider(config, db)
            reply = await routines.execute_routine(
                command,
                db=db,
                channel=channel,
                config=config,
                provider=active_provider,
                lang=lang,
                user_id=user_id,
                clock=clock,
            )
            if reply is not None:
                await channel.send(user_id, reply)
            return
        if command.kind == "cadence":
            reply = await cadence.execute_cadence(
                command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
            )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            await dashboard.refresh(db, channel, config, registry, user_id, clock)
            return
        if command.kind in ("pause", "resume"):
            if command.kind == "pause":
                reply = await pause.execute_pause(
                    command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
                )
            else:
                reply = await pause.execute_resume(
                    command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
                )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            await dashboard.refresh(db, channel, config, registry, user_id, clock)
            return
        if command.kind == "wrapped":
            # `execute_wrapped` sends the PNG itself when it succeeds,
            # returning "" to signal "already delivered".
            if dry_run:
                print({"kind": "wrapped", "note": "requires a real channel; not supported in --dry-run"})
                return
            assert channel is not None, "channel is required outside dry-run"
            reply = await wrapped.execute_wrapped(
                command, db=db, channel=channel, config=config, registry=registry, lang=lang, user_id=user_id, clock=clock
            )
            if reply:
                await channel.send(user_id, reply)
            return
        if command.kind == "query":
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
            reply = await targets_command.execute_target(
                command, db=db, config=config, registry=registry, lang=lang, user_id=user_id
            )
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            if command.target_action in ("set", "clear"):
                await dashboard.refresh(db, channel, config, registry, user_id, clock)
            return
        if command.kind == "help":
            reply = discoverability.build_help_text(config, lang)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "habits":
            reply = discoverability.build_habits_overview(db, config, registry, clock, lang, user_id)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "guide":
            # SPEC-v1.10.md §4 R16 (functional 5): a compact bilingual
            # getting-started card, one `channel.send` -- not budget-capped
            # (fixed size, R16's own precedent).
            reply = discoverability.build_guide_text(config, lang)
            if dry_run:
                print(reply)
                return
            assert channel is not None, "channel is required outside dry-run"
            await channel.send(user_id, reply)
            return
        if command.kind == "history":
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

    # A zero-LLM whole-message "NUMBER UNIT" hit skips the deferral check
    # AND the target-NL gate entirely (logs successfully even while Ollama
    # is down). A miss falls through byte-for-byte to the pre-existing
    # deferral -> target-NL-gate -> parse_message sequence below.
    #
    # A leading/trailing recognized date phrase is stripped from the raw
    # text BEFORE preparse -- the RESIDUAL is what actually gets parsed for
    # habit+value below, so "500ml yesterday" backdates to yesterday from
    # residual "500ml". `backfill_date` stays `None` (and `parse_text`
    # stays the ORIGINAL `text`) whenever no phrase was recognized OR it
    # resolved to exactly today. A phrase resolving outside
    # `[today - max_days_back, today)` is rejected immediately, before the
    # Ollama up/down check below.
    backfill_result = backfill.extract_date(text, clock, max_days_back=config.backfill.max_days_back)
    parse_text = text
    backfill_date: date | None = None
    if isinstance(backfill_result, backfill.OutOfRange):
        if dry_run:
            print({"kind": "backfill_out_of_range", "reason": backfill_result.reason})
            return
        assert channel is not None, "channel is required outside dry-run"
        await channel.send(user_id, backfill.bounds_error_text(backfill_result, lang, config.backfill.max_days_back))
        return
    if backfill_result is not None:
        parse_text, backfill_date = backfill_result

    # SPEC-v1.10.md §4 R13/R14 (module `reply_attribution`, functional 3):
    # a bare-value reply to one of the bot's own per-habit reminder
    # messages attributes zero-LLM -- placed here (after backfill, before
    # preparse) per R13's own exact ordering. `reply_attribution.
    # resolve_reply_value` is deliberately conservative (R14): non-`None`
    # only for a bare positive number (numeric/duration habit) or an
    # affirmative token (boolean habit); everything else (a number+unit,
    # an unmapped/check-in/nudge reply, non-value text, or the map simply
    # not knowing this `(chat_id, message_id)` pair) falls through
    # unchanged to the normal preparse/LLM path below -- no wrong
    # attribution, ever. A hit is treated EXACTLY like a preparse hit (the
    # shared write+confirm block below fires the reaction and refreshes
    # the dashboard the same way, R13's own "works offline" -- this needs
    # no Ollama-up check at all, unlike the LLM path just below).
    reply_result: ExtractionResult | None = None
    if reply_to_message_id is not None and config.reply_to_reminder.enabled and reminder_state is not None:
        reply_habit_id = reminder_state.habit_for_reply(user_id, reply_to_message_id)
        reply_habit = registry.get(reply_habit_id) if reply_habit_id is not None else None
        if reply_habit is not None:
            reply_value = reply_attribution.resolve_reply_value(text, reply_habit)
            if reply_value is not None:
                reply_result = ExtractionResult(reply_habit.id, reply_value, 1.0)

    preparsed = reply_result if reply_result is not None else preparse.deterministic_parse(parse_text, registry)
    if preparsed is not None:
        result = preparsed
    else:
        if not dry_run and health_monitor is not None and not health_monitor.ollama_up:
            assert channel is not None, "channel is required outside dry-run"
            now = clock()
            ts = now.isoformat(timespec="seconds")
            db.insert_log(LogEntry(None, user_id, ts, "unparsed", None, None, text, source))
            # SPEC-v1.10.md §4 R15 (functional 4, outage honesty): replaces
            # the bare `deferred_ack` with an immediate, honest message
            # naming what still works instantly -- gated by `config.
            # outage.honest_reply` (default True); `false` restores the
            # pre-1.10 `deferred_ack` byte-for-byte. The deferral row above
            # and the recovery machinery are unchanged either way.
            if config.outage.honest_reply:
                outage_text = i18n.t(
                    "outage_honest_reply", lang, text=render_budget.truncate(text, max_chars=_OUTAGE_QUOTE_MAX_CHARS)
                )
                outage_buttons = quicklog.build_keyboard(registry, config, db, lang, user_id)
                if outage_buttons:
                    await channel.send_actionable(user_id, outage_text, outage_buttons)
                else:
                    await channel.send(user_id, outage_text)
            else:
                await channel.send(user_id, i18n.t("deferred_ack", lang))
            return

        # The full-NL target-intent step. `command` is guaranteed None here
        # (every branch above returns), and the health-monitor deferral
        # check just above already returned for the Ollama-DOWN case, so
        # reaching this point already implies "Ollama up" whenever a
        # health_monitor is wired. `looks_like_target_phrasing` is a cheap
        # cost gate only -- `classify_target_intent` is independently
        # fail-closed, so a `None` result (gate miss, low confidence, or
        # any classifier failure) falls straight through to the normal
        # log-parsing path, unchanged. A backfill never reaches this gate
        # at all -- "500ml yesterday" residual "500ml" logging a habit, not
        # silently becoming a `/target` set.
        if backfill_date is None and (health_monitor is None or health_monitor.ollama_up):
            if target_nl.looks_like_target_phrasing(text):
                intent = await target_nl.classify_target_intent(text, llm, registry, config)
                if intent is not None:
                    set_command = commands.Command(
                        kind="target",
                        category=intent.habit_id,
                        value_num=intent.goal_base_unit,
                        target_action="set",
                    )
                    reply = await targets_command.execute_target(
                        set_command, db=db, config=config, registry=registry, lang=lang, user_id=user_id, source="nl"
                    )
                    if dry_run:
                        print(reply)
                        return
                    assert channel is not None, "channel is required outside dry-run"
                    await channel.send(user_id, reply)
                    await dashboard.refresh(db, channel, config, registry, user_id, clock)
                    return  # no `logs` row is written for a target-intent hit

        result = await parse_message(parse_text, llm, registry, config.ollama.confidence_threshold)

        # The LLM extraction path may additionally return an optional
        # integer `date_offset`, honored only when present and within
        # bounds, and only when the deterministic pass above did NOT
        # already resolve a date -- the deterministic parse wins whenever
        # both are present.
        if backfill_date is None and result.date_offset is not None:
            offset_result = backfill.resolve_days_back(
                clock, result.date_offset, max_days_back=config.backfill.max_days_back
            )
            if isinstance(offset_result, backfill.OutOfRange):
                if dry_run:
                    print({"kind": "backfill_out_of_range", "reason": offset_result.reason})
                    return
                assert channel is not None, "channel is required outside dry-run"
                await channel.send(
                    user_id, backfill.bounds_error_text(offset_result, lang, config.backfill.max_days_back)
                )
                return
            if offset_result is not None:
                backfill_date = offset_result

    if dry_run:
        print(asdict(result))
        return

    assert channel is not None, "channel is required outside dry-run"

    now = clock()
    # A backfilled row's `ts` is the resolved date at local noon, NOT
    # `now` -- but `today_str` (used for "today's totals" in every
    # confirmation branch, and for the milestone/streak checks) stays the
    # REAL today throughout, so a backfilled log's confirmation shows
    # today's totals UNCHANGED.
    ts = backfill.backdated_ts(backfill_date) if backfill_date is not None else now.isoformat(timespec="seconds")
    today_str = now.date().isoformat()

    habit = registry.get(result.category)
    if habit is None:
        # SPEC-v1.10.md §4 R6/R10 (module `clarify`, functional 2): reached
        # only when Ollama is UP and `parse_message` itself returned no
        # registry habit -- the Ollama-DOWN deferral above already
        # returned. `config.clarify.enabled=false` -> generic path always
        # (R6's own "false -> generic clarifying question only, no guess
        # buttons"). Guesses -> a fresh `awaiting_clarify` row (raw_message
        # = text) + the tap-to-fix offer (R6); no guesses -> the existing
        # bilingual clarifying question, now with the `/log` keyboard
        # attached too (R10), and no row is written.
        guesses = clarify.tier1_guesses(text, registry, db, config, user_id) if config.clarify.enabled else []
        if guesses:
            clarify_row_id = db.insert_log(
                LogEntry(None, user_id, ts, "unparsed", None, None, text, source, unparsed_state=clarify.AWAITING_CLARIFY)
            )
            await clarify.offer_clarify(channel, db, config, registry, lang, user_id, row_id=clarify_row_id, text=text)
        else:
            clarify_buttons = quicklog.build_keyboard(registry, config, db, lang, user_id)
            if clarify_buttons:
                await channel.send_actionable(user_id, i18n.t("clarifying_question", lang), clarify_buttons)
            else:
                await channel.send(user_id, i18n.t("clarifying_question", lang))
        return

    # Snapshot whether today already satisfied this habit's streak
    # condition BEFORE writing the new row -- comparing this to the
    # post-insert state is how a genuine milestone crossing is detected.
    # Skipped entirely when gamification is disabled or for a backfill (a
    # backdated row can never affect TODAY's own streak-qualification
    # state).
    was_qualified_before = (
        streaks.day_qualifies(db, config, habit, today_str, user_id)
        if config.gamification.enabled and backfill_date is None
        else False
    )

    entry = log_entry_from_result(habit, result, ts, text, source, user_id)
    # The inserted row's id drives the inline "Undo" button attached to
    # this confirmation -- unchanged for a backfilled row too (undo
    # operates by row id, never by `ts` ordering).
    row_id = db.insert_log(entry)
    undo_buttons = undo_ui.undo_button(row_id, lang)

    record_clock = clock if backfill_date is None else (lambda: datetime.combine(backfill_date, time(12, 0)))
    confirmation_suffix = confirmation.suffix(
        db,
        config,
        registry,
        habit,
        user_id,
        lang,
        now_date=now.date(),
        was_qualified_before=was_qualified_before,
        record_clock=record_clock,
        apply=backfill_date is None,
    )
    # Reuses whichever confirmation formatter below unchanged, just
    # prepended, for a backfilled log.
    confirmation_prefix = backfill.confirmation_prefix(backfill_date, lang) if backfill_date is not None else ""

    message = await confirmation.confirmation_text(db, llm, habit, result.value, today_str, lang, config, user_id)
    await channel.send_actionable(user_id, confirmation_prefix + message + confirmation_suffix, undo_buttons)
    await _react_to_typed_log(channel, config, user_id, inbound_message_id, habit)
    # Skipped entirely for a backfill -- a backdated row never changes
    # what today's live dashboard shows anyway.
    if backfill_date is None:
        await dashboard.refresh(db, channel, config, registry, user_id, clock)


async def reparse_pending_unparsed(
    db: "Database",
    llm: "OllamaClient",
    channel: Channel,
    config: "Config",
    registry: HabitRegistry | None = None,
    provider: RegistryProvider | None = None,
    *,
    parse_message=_default_parse_message,
) -> None:
    """Re-parse every row deferred while Ollama was DOWN (category=
    'unparsed'), convert it to its real category, and confirm. Rows come
    straight from `db.pending_unparsed()`, so this also picks up rows
    deferred by a *previous* process run.

    SPEC-v1.10.md §4 R1-R4/R7 (modules `clarify`, "never lose a log"): a
    row that's STILL unparseable after Ollama is back no longer sits in
    'unparsed' forever, re-parsed on every future recovery sweep. It's
    decided ONCE more, via `clarify.tier1_guesses` (deterministic,
    zero-LLM, against the row's OWN user's registry): guesses exist -> CAS
    `mark_unparsed_state(to='awaiting_clarify')`, winner sends the
    tap-to-fix offer (R7); no guesses -> CAS `mark_unparsed_state(to=
    'closed')`, winner sends the ONE closure notification (R1). Either way
    the row permanently leaves `pending_unparsed()` (R2/R-SS2) -- the LLM
    is never retried on it again. A row that DOES re-parse is reclassified
    via the guarded CAS `resolve_unparsed` (R3), not the old unconditional
    `reclassify_log` -- only the winner sends the recovered-* confirmation
    + dashboard refresh. Every CAS is guarded on `from_states=(None,
    clarify.AWAITING_LLM)` -- the sweep's own origin set, disjoint from the
    tap's `('awaiting_clarify',)` (R11's race-guard precondition) -- so a
    losing CAS (another concurrent sweep, or a tap that already resolved
    this exact row) is a silent no-op: no double log, no double
    notification.

    SPEC-v1.10.md §4 R4 (single-flight guard): a sweep already in progress
    makes any concurrent trigger a no-op (logged, returns immediately) --
    defense-in-depth on top of the per-row CAS above, since a second sweep
    reading the same `pending_unparsed()` snapshot would otherwise race
    every one of its rows against the first sweep for no benefit (the
    running sweep already covers everything deferred up to the outage's
    end).

    `provider`, when given, resolves EACH row's own per-user registry
    inside the loop -- a backlog spanning multiple users re-parses each
    row against its OWN user's custom habits."""
    global _sweep_in_progress
    if _sweep_in_progress:
        logger.info("Skipping reparse_pending_unparsed: a sweep is already in progress")
        return
    _sweep_in_progress = True
    try:
        registry = registry or HabitRegistry.from_config(config)
        pending = db.pending_unparsed()
        if not pending:
            return

        logger.info("Re-parsing %d deferred message(s)", len(pending))
        for row in pending:
            text = row["raw_message"]
            user_id = row["user_id"]
            row_registry = provider.for_user(user_id) if provider is not None else registry
            lang = i18n.resolve_reply_language(text, config, user_pref=user_prefs.stored_language_pref(db, user_id))
            result = await parse_message(text, llm, row_registry, config.ollama.confidence_threshold)

            habit = row_registry.get(result.category)
            if habit is None:
                guesses = clarify.tier1_guesses(text, row_registry, db, config, user_id)
                to_state = clarify.AWAITING_CLARIFY if guesses else clarify.CLOSED
                won = db.mark_unparsed_state(row["id"], from_states=(None, clarify.AWAITING_LLM), to_state=to_state)
                if won:
                    if guesses:
                        await clarify.offer_clarify(
                            channel, db, config, row_registry, lang, user_id, row_id=row["id"], text=text
                        )
                    else:
                        await clarify.send_closure(channel, db, config, row_registry, lang, user_id, text=text)
                continue

            recovered_entry = log_entry_from_result(habit, result, row["ts"], text, row["source"], user_id)
            won = db.resolve_unparsed(
                row["id"],
                from_states=(None, clarify.AWAITING_LLM),
                category=habit.id,
                value_num=recovered_entry.value_num,
                value_text=recovered_entry.value_text,
                habit_type=habit.type,
            )
            if not won:
                continue

            undo_buttons = undo_ui.undo_button(row["id"], lang)
            await confirmation.send_recovered_confirmation(channel, user_id, habit, result.value, lang, undo_buttons)

            # The "recovery" case -- a deferred row landing late is still a
            # real log, so the pinned board should reflect it too. No
            # `clock=` override -- this loop has no injectable clock of
            # its own, so it correctly falls back to the real wall clock.
            await dashboard.refresh(db, channel, config, row_registry, user_id)
    finally:
        _sweep_in_progress = False


async def on_message(
    chat_id: str,
    text: str,
    display_name: str | None = None,
    message_id: str | None = None,
    reply_to_message_id: str | None = None,
    *,
    db: "Database",
    llm: "OllamaClient",
    channel: Channel,
    config: "Config",
    owner_chat_id: str,
    provider: RegistryProvider,
    scheduler: AsyncIOScheduler,
    reminder_state: ReminderState,
    health_monitor: HealthMonitor,
) -> None:
    """The Telegram inbound-message entry point. In order:
    1. The access gate runs FIRST, before any logging/LLM/command work.
    2. The five owner-only/onboarding kinds are routed to
       `access.execute_admin` here, before `handle_inbound_message`.
    3. `/audit` (owner-only) is answered here too -- LLM-free, must work
       with Ollama down, and needs `owner_chat_id`.
    4. Everything else proceeds through `handle_inbound_message`, given
       the SAME already-dispatched `command` this function computed for
       its own steps 2/3 (rule 5/AC7 -- dispatch once, not twice)."""
    lang = i18n.resolve_reply_language(text, config)
    proceed = await access.handle_gate(db, channel, config, owner_chat_id, chat_id, display_name, text, lang=lang)
    if not proceed:
        return

    user_registry = provider.for_user(chat_id)
    command = commands.dispatch(text, user_registry)
    if command is not None and command.kind in ("start", "approve", "block", "users", "invite"):
        await access.execute_admin(
            command, db=db, channel=channel, config=config, owner_chat_id=owner_chat_id, chat_id=chat_id, lang=lang
        )
        return

    if command is not None and command.kind == "audit":
        if access.classify(db, chat_id) == "owner":
            audit_lang = i18n.resolve_reply_language(
                text, config, user_pref=user_prefs.stored_language_pref(db, chat_id)
            )
            reply = audit_view.render_recent(
                db, config, audit_lang, limit=command.limit, owner_chat_id=owner_chat_id
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
        registry=user_registry,
        scheduler=scheduler,
        reminder_state=reminder_state,
        provider=provider,
        inbound_message_id=message_id,
        reply_to_message_id=reply_to_message_id,
        command=command,
    )


async def on_callback(
    chat_id: str,
    data: str,
    source_text: str,
    callback_id: str,
    *,
    db: "Database",
    channel: Channel,
    config: "Config",
    provider: RegistryProvider,
) -> None:
    """Route an inline-button tap. A non-active chat's tap is a silent
    no-op (no onboarding reply -- a tap isn't itself a message to onboard
    from). Dispatch by payload prefix: `log:` -> `quicklog`, `routine:run:`
    -> `routines`, `clarify:` -> `clarify` (SPEC-v1.10.md §5 R9, module
    `clarify`), everything else (`undo:`) -> `undo_ui`, which this
    function refreshes the dashboard for afterward (the other three
    prefixes already refresh it themselves as part of their own "log +
    confirm" flow)."""
    if access.classify(db, chat_id) not in ("owner", "active"):
        return
    user_registry = provider.for_user(chat_id)

    if data.startswith("log:"):
        await quicklog.handle_log_callback(
            chat_id, data, source_text, callback_id, db=db, channel=channel, config=config, registry=user_registry, clock=datetime.now
        )
        return
    if data.startswith("routine:run:"):
        await routines.handle_routine_callback(
            chat_id, data, source_text, callback_id, db=db, channel=channel, config=config, provider=provider, clock=datetime.now
        )
        return
    if data.startswith("clarify:"):
        await clarify.handle_clarify_callback(
            chat_id, data, source_text, callback_id, db=db, channel=channel, config=config, registry=user_registry, clock=datetime.now
        )
        return

    await undo_ui.handle_undo_callback(
        chat_id, data, source_text, callback_id, db=db, channel=channel, config=config, clock=datetime.now, registry=user_registry
    )
    await dashboard.refresh(db, channel, config, user_registry, chat_id)
