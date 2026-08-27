"""SPEC-REFACTOR.md Stage 2 (rule 9): the scheduler job bodies, split out of
`main.py:async_main` (where they used to be closures over that function's
own locals -- db/channel/config/registry/provider/reminder_state/llm).
Each job here is a plain function taking those as explicit parameters;
`core/app.py`'s `async_main` registers a small zero-arg forwarding closure
per job (APScheduler calls a registered job with no arguments), the only
closures left, carrying no logic of their own.

`run_due_reminders`/`render_weekly_review_charts` are explicit, overridable
keyword parameters on the two jobs that call them, defaulting to the real
functions -- `main.py`'s own re-export of `async_main` always passes its
OWN current module-level names explicitly, which is what lets
`monkeypatch.setattr(main_module, "run_due_reminders", fake)` /
`monkeypatch.setattr(main_module, "render_weekly_review_charts", fake)`
keep working now that the call sites no longer live in `main.py`.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from habit_assistant.core import checkins, commands, dashboard, grace, i18n, nudge, pause, streaks, user_prefs, wrapped
from habit_assistant.core.reminders import ReminderState, in_dnd_now
from habit_assistant.core.review import run_weekly_review

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.llm.ollama_client import OllamaClient
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


async def minutely_tick(
    channel: "Channel",
    config: "Config",
    registry: "HabitRegistry",
    db: "Database",
    reminder_state: ReminderState,
    provider: "RegistryProvider",
    *,
    run_due_reminders,
) -> None:
    """SPEC-REFACTOR.md Stage 1 rule 2/AC4: one consolidated minutely tick
    (replacing three independent `reminder_tick`/`checkin_tick`/
    `nudge_tick` jobs) that fetches the fan-out set once and threads it
    into all three, in the exact order the three jobs used to register in.
    Each call is wrapped in its own try/except (log + continue), restoring
    the pre-consolidation per-tick isolation an earlier round of testing
    found this merge had dropped."""
    active_ids = db.active_user_ids()
    try:
        await run_due_reminders(
            channel, config, registry, db, reminder_state, registry_for=provider.for_user, active_user_ids=active_ids
        )
    except Exception:
        logger.exception("run_due_reminders failed this tick; continuing with check-ins/nudge")

    try:
        await checkins.run_due_checkins(
            channel, config, registry, db, registry_for=provider.for_user, active_user_ids=active_ids
        )
    except Exception:
        logger.exception("run_due_checkins failed this tick; continuing with nudge")

    try:
        await nudge.run_due_nudges(
            channel, config, registry, db, registry_for=provider.for_user, active_user_ids=active_ids
        )
    except Exception:
        logger.exception("run_due_nudges failed this tick")


async def dashboard_day_rollover_job(
    db: "Database", channel: "Channel", config: "Config", provider: "RegistryProvider"
) -> None:
    """A day-rollover refresh so an enabled user's board resets to the new
    day without waiting for their next log. `refresh` already no-ops for a
    disabled user, so this iterates every active user unconditionally."""
    for user_id in db.active_user_ids():
        await dashboard.refresh(db, channel, config, provider.for_user(user_id), user_id)


async def weekly_review_job(
    db: "Database",
    channel: "Channel",
    config: "Config",
    provider: "RegistryProvider",
    llm: "OllamaClient",
    *,
    render_weekly_review_charts,
) -> None:
    """The weekly-review TIME stays global, but the send fans out to every
    active user, each from their own data; a user with no logs in the
    7-day window, or inside their own effective DND window, is skipped."""
    today = date.today()
    week_start = (today - timedelta(days=6)).isoformat() + "T00:00:00"
    week_end = today.isoformat() + "T23:59:59"
    for user_id in db.active_user_ids():
        if not db.logs_between(user_id, week_start, week_end):
            continue
        if in_dnd_now(db, config, user_id):
            continue
        lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
        user_registry = provider.for_user(user_id)
        text = await run_weekly_review(db, config, user_registry, llm, lang, user_id, today=today)
        await channel.send(user_id, text)

        try:
            image_captions = render_weekly_review_charts(db, config, user_registry, lang, user_id, today=today)
        except Exception:
            logger.exception("Failed to render weekly review charts for %s; continuing with text-only review", user_id)
            image_captions = []
        for image, caption in image_captions:
            try:
                await channel.send_image(user_id, image, caption)
            except Exception:
                logger.exception("Failed to send a weekly review chart image to %s; continuing", user_id)


async def daily_summary_job(db: "Database", channel: "Channel", config: "Config", provider: "RegistryProvider") -> None:
    """The end-of-day recap, gated by `config.gamification.daily_summary`
    (independent of `gamification.enabled`, which only affects milestone
    lines). Fans out to every active user, each from their own today's
    data; a user who logged nothing today, or is inside their own DND
    window, is skipped."""
    if not config.gamification.daily_summary:
        return
    today = date.today()
    today_str = today.isoformat()
    for user_id in db.active_user_ids():
        user_registry = provider.for_user(user_id)
        has_logs_today = any(db.count(user_id, habit.id, today_str) > 0 for habit in user_registry)
        if not has_logs_today:
            continue
        if in_dnd_now(db, config, user_id):
            logger.info("Suppressing daily summary for %s: inside their own DND window", user_id)
            continue
        lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
        text = streaks.run_daily_summary(db, config, user_registry, lang, user_id, today=today)
        await channel.send(user_id, text)


async def grace_tick(db: "Database", channel: "Channel", config: "Config", provider: "RegistryProvider") -> None:
    """The nightly 00:05 tick: fans out over every active user and, for
    whichever habits `evaluate_grace` just bridged, sends the one kind
    message -- always silent, and deliberately bypassing quiet-hours/DND
    (it reports a decision that has ALREADY been made)."""
    today = date.today()
    for user_id in db.active_user_ids():
        user_registry = provider.for_user(user_id)
        try:
            bridged = grace.evaluate_grace(db, config, user_registry, user_id, today)
        except Exception:
            logger.exception("evaluate_grace failed for %s; skipping (fail-open)", user_id)
            continue
        if not bridged:
            continue
        lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
        message = grace.format_grace_message(bridged, lang)
        if not message:
            continue
        try:
            await channel.send(user_id, message, disable_notification=True)
        except Exception:
            logger.exception("Sending the grace message failed for %s; skipping (fail-open)", user_id)


async def wrapped_auto_job(db: "Database", channel: "Channel", config: "Config", provider: "RegistryProvider") -> None:
    """The optional month-end auto-send, gated by `config.wrapped.
    auto_send` (default `false`). One silent card per active user,
    pause-aware (skipped for a user whose ENTIRE registry is currently
    paused) and DND-aware."""
    if not config.wrapped.auto_send:
        return
    today = date.today()
    for user_id in db.active_user_ids():
        try:
            if in_dnd_now(db, config, user_id):
                continue
            user_registry = provider.for_user(user_id)
            habit_ids = [h.id for h in user_registry]
            if not habit_ids or all(pause.is_paused(db, config, user_id, hid, today) for hid in habit_ids):
                continue
            lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
            auto_command = commands.Command(kind="wrapped", pref_value="month")
            reply = await wrapped.execute_wrapped(
                auto_command,
                db=db,
                channel=channel,
                config=config,
                registry=user_registry,
                lang=lang,
                user_id=user_id,
                disable_notification=True,
            )
            if reply:
                await channel.send(user_id, reply, disable_notification=True)
        except Exception:
            logger.exception("Month-end wrapped auto-send failed for %s; skipping (fail-open)", user_id)
