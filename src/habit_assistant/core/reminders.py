"""Reminder definitions + APScheduler wiring (ROADMAP.md v0.7.0 "Multi-Habit
Extensibility", SPEC-v0.7.md §4 R15 / §5, module M2). Depends only on the
Channel ABC (SPEC.md §8) and the HabitRegistry (core/habits.py, shared
surface) -- never a concrete channel, never `Config.habits` directly.

ROADMAP.md v0.9.0 "Adaptive Reminders, Snooze & Quiet Hours" (AC9.1/AC9.2/
AC9.4/AC9.5): `send_reminder` gained two opt-in, additive checks run right
before actually sending -- both no-ops (byte-identical to v0.7.0/v0.8.0
behavior) unless a caller passes `db`/`config`:

- **Quiet hours** (AC9.2): if the current wall-clock time (`config.app.
  timezone`) falls inside any effective quiet-hours window -- including a
  window that crosses midnight -- the reminder is suppressed and logged.
- **Goal-met skip** (AC9.1/AC9.4): for a goal-bearing habit with
  `habit.skip_if_goal_met` true (the default), today's progress is read
  from `db` and compared to the effective goal; already-met -> suppressed
  and logged. **Fail-open** (AC9.5): if the DB read itself raises, the
  error is logged and the reminder is sent anyway -- a DB hiccup must
  never silently swallow every reminder.

SPEC-v1.2.md "Multi-user support" (R-S1-R-S6): reminders are now per-user
in two independent ways -- (1) WHO gets one (`Database.active_user_ids()`,
the fan-out set) and (2) WHEN (`effective_reminder_times`, per-user custom
times with a config fallback). The single fixed cron-per-config-time
scheduling of `schedule_reminders` (v0.7.0-v1.1.0) is REPLACED by a single
minutely tick job (`run_due_reminders`, R-S1) that reads the live
`user_reminder_times` store on every tick -- no scheduler rebuild is ever
needed when a user runs `/remind` (R-S6), and no per-user job lifecycle is
needed on approve/block. `ReminderState.last_habit_id` becomes a per-user
map (chat_id -> habit id) so "snooze" (no habit named) resolves correctly
per asking user (R-S2/AC-U-SNOOZE) and quiet-hours become per-user via
`effective_quiet_windows` (R-P2).

`ReminderState` (below) is a tiny, in-memory, single-process "which habit's
reminder last actually fired, per user" tracker. It's plain data, not a
channel/DB import, so it stays inside this "no channel imports" module
without breaking the seam."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n, pause, targets, user_prefs
from habit_assistant.core.habits import BUILTIN_IDS, Habit, HabitRegistry
from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class ReminderState:
    """SPEC-v1.2.md R-S2: `last_habit_id` is now a per-user map (chat_id ->
    habit id), updated by `send_reminder` every time a reminder actually
    fires for that user (i.e. survives the quiet-hours/goal-met checks
    below) -- read by `main.py`'s snooze handler to resolve a bare
    "snooze"/"เลื่อน" command to a habit FOR THE ASKING USER, never
    another user's (AC-U-SNOOZE). One instance lives for the lifetime of
    the process (built once in `async_main`); lost on restart, which is
    fine -- there is nothing to snooze immediately after a fresh start
    anyway."""

    last_habit_id: dict[str, str] = field(default_factory=dict)


# Built-in habits reuse their v0.6.0 catalog entries verbatim (SPEC-v0.7.md
# §4 R15: "built-in id -> its reminder_water/stretch/diary catalog entry
# (byte-identical)") instead of the type-generic `reminder_generic` template
# every other habit falls back to.
BUILTIN_REMINDER_MESSAGE_IDS = {
    "water": "reminder_water",
    "stretch": "reminder_stretch",
    "diary": "reminder_diary",
}

# Back-compat only: kept so `from habit_assistant.core.reminders import
# REMINDER_TEXTS` (a few older tests) keeps working; nothing in this
# module's own code reads it anymore.
REMINDER_TEXTS = {category: i18n.t(msg_id, "en") for category, msg_id in BUILTIN_REMINDER_MESSAGE_IDS.items()}


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def _in_quiet_hours(now: time, windows: list[tuple[str, str]]) -> bool:
    """ROADMAP.md v0.9.0 AC9.2: does wall-clock `now` fall inside ANY
    configured `[start, end)` window? `start <= end` is a normal same-day
    window; `start > end` crosses midnight (e.g. `("23:00", "07:00")` spans
    the night -- true for `now` >= 23:00 OR `now` < 07:00). The project is
    fixed to `Asia/Bangkok` with no DST, so a plain time-of-day comparison
    is correct with no date arithmetic needed."""
    for start_s, end_s in windows:
        start_h, start_m = _parse_hhmm(start_s)
        end_h, end_m = _parse_hhmm(end_s)
        start, end = time(start_h, start_m), time(end_h, end_m)
        if start <= end:
            if start <= now < end:
                return True
        elif now >= start or now < end:  # crosses midnight
            return True
    return False


def _today_str(config: Config) -> str:
    return datetime.now(ZoneInfo(config.app.timezone)).date().isoformat()


def _now_hhmm(clock, tz_name: str) -> str:
    """SPEC-v1.2.md R-S1: the current wall-clock `HH:MM` in `tz_name`.
    Mirrors `core/query.py:_today_in_timezone`'s own convention -- a naive
    `clock()` (this app's usual injectable-clock shape, e.g. `datetime.now`
    or a test's fixed callable) is treated as already being in `tz_name`;
    an aware one is converted to it."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.strftime("%H:%M")


def effective_quiet_windows(db: Database | None, config: Config, chat_id: str) -> list[tuple[str, str]]:
    """SPEC-v1.2.md R-P2: `chat_id`'s effective quiet-hours windows --
    `users.quiet_hours_json` if set (an explicit, possibly EMPTY, list --
    `/quiet off` stores `"[]"`, distinct from "never set"), else inherit
    `config.quiet_hours.windows` (the pre-v1.2 global default, so an
    un-customized user -- the owner included -- is byte-identical to v1.1,
    AC-M3). `db=None` (a caller with no DB handle, e.g. a bare unit test
    calling `send_reminder` directly) also falls back to the global
    config, matching v1.1's own no-DB behavior. The whole lookup is inside
    one fail-open `try/except` (mirrors AC9.5's own posture for the
    goal-met read, `_goal_already_met` below) -- a DB hiccup reading the
    user row must fall back to the global config default, not crash
    `send_reminder` (and therefore the scheduler job) outright."""
    if db is None:
        return config.quiet_hours.windows
    try:
        user = db.get_user(chat_id)
        if user is None or user["quiet_hours_json"] is None:
            return config.quiet_hours.windows
        raw = json.loads(user["quiet_hours_json"])
        return [(w[0], w[1]) for w in raw]
    except Exception:
        logger.exception(
            "Reading effective quiet hours failed for %s; falling back to global config windows (fail-open)", chat_id
        )
        return config.quiet_hours.windows


def in_dnd_now(db: Database, config: Config, chat_id: str, clock=datetime.now) -> bool:
    """SPEC-v1.5.md R-D1: the ONE shared per-user Do-Not-Disturb check --
    "is `chat_id` inside their own effective quiet-hours window right
    now?" Built directly on the existing `effective_quiet_windows`
    (per-user override, falling back to `config.quiet_hours.windows`) +
    `_in_quiet_hours` (midnight-crossing-aware) -- v1.5 does NOT build a
    parallel DND mechanism (§4's own explicit decision); this is simply
    the one call every unprompted per-user send site now shares
    (check-ins R-K4, the daily-summary job R-D2, the weekly-review job
    R-D3), instead of each reimplementing the "windows lookup -> now ->
    inside?" sequence inline the way `main.py`'s old GLOBAL
    `is_quiet_hours_now` call sites used to.

    `clock` is the same injectable-clock shape every other testable
    "what time is it" call site in this module already uses (`_now_hhmm`
    above) -- a naive result is treated as already being in
    `config.app.timezone`; an aware one is converted to it. Fail-open by
    construction: `effective_quiet_windows` itself already falls back to
    the global config default on ANY DB read error (its own documented
    posture, a few lines up) -- a DB hiccup here simply resolves to
    whatever the global config's own windows say, never a raised
    exception.

    Deliberately NOT used by `send_reminder`'s own existing inline
    quiet-hours check (SPEC-v1.5.md §2.3's suppression matrix marks the
    per-habit reminder tick "unchanged") -- that call site keeps its own
    real-clock `datetime.now(ZoneInfo(...))` read rather than being
    refactored to take a `clock` param, so as to introduce zero behavior
    risk to an already-correct, already-tested path; this function is
    for the THREE sites the matrix marks as actually changing (or, for
    check-ins, brand new)."""
    windows = effective_quiet_windows(db, config, chat_id)
    if not windows:
        return False
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(config.app.timezone))
    else:
        now = now.astimezone(ZoneInfo(config.app.timezone))
    return _in_quiet_hours(now.time(), windows)


def _user_language_pref(db: Database, chat_id: str) -> str:
    """SPEC-v1.8.md integration step: now a thin alias for the shared
    `core/user_prefs.stored_language_pref` (Archi-approved consolidation of
    what used to be four independent per-file copies of this exact lookup;
    see that module's own docstring). Kept as a same-named wrapper here
    (rather than rewriting every call site to `user_prefs.stored_language_
    pref` directly) purely so this file's own docstrings/comments citing
    `_user_language_pref` by name stay accurate -- behavior is unchanged."""
    return user_prefs.stored_language_pref(db, chat_id)


def effective_reminder_times(db: Database, config: Config, habit: Habit, user_id: str) -> list[str]:
    """SPEC-v1.2.md R-S4: `user_id`'s effective reminder times for `habit`
    -- the ONE resolver both the minutely tick (`run_due_reminders`) and
    the `/remind` show/set paths (module `schedules`) consult, so they can
    never diverge. No stored rows -> the habit's global config
    `reminder_times` (fallback/default, AC-S1's "owner byte-identical
    until they customize"); rows equal to the single sentinel `["off"]` ->
    `[]` (explicitly no reminders for this user+habit); otherwise -> the
    stored custom `HH:MM` list, sorted and de-duplicated (read-side
    safety net; the write side, R-S5, is also expected to de-dupe)."""
    rows = db.get_reminder_times(user_id, habit.id)
    if not rows:
        return list(habit.reminder_times)
    if rows == ["off"]:
        return []
    return sorted(set(rows))


def _goal_already_met(db: Database, habit: Habit, config: Config, user_id: str) -> bool:
    """ROADMAP.md v0.9.0 AC9.1/AC9.4/AC9.5, extended by SPEC-v1.1.md R-T5/
    R-T5b: True only for a goal-bearing habit -- its effective goal
    (`targets.effective_goal`, a DB override if one is set, else the
    config default) is not `None` -- with adaptive skipping enabled
    (`habit.skip_if_goal_met`, default True) whose today's total already
    meets that goal. SPEC-v1.2.md R-D2/R-S2: scoped to `user_id` -- A's
    goal-met state never suppresses B's reminder (AC-U5).

    Fail-open (AC9.5): a DB read error is logged and treated as "not met"
    -- the reminder always sends rather than a scheduler job ever crashing
    or silently going dark on a DB hiccup. The goal lookup itself
    (`targets.effective_goal`) is included in this fail-open try/except --
    a `habit_targets` read failure must not behave differently from a
    `sum_value` read failure."""
    if not habit.skip_if_goal_met:
        return False
    try:
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal is None:
            return False
        total = db.sum_value(user_id, habit.id, _today_str(config))
    except Exception:
        logger.exception(
            "Adaptive-reminder goal read failed for %s/%s; sending reminder anyway (fail-open)", user_id, habit.id
        )
        return False
    if total >= goal:
        logger.info("Skipping %s/%s reminder: goal already met (%s/%s)", user_id, habit.id, total, goal)
        return True
    return False


async def send_reminder(
    channel: Channel,
    chat_id: str,
    habit: Habit,
    language: i18n.Language = "en",
    db: Database | None = None,
    config: Config | None = None,
    state: ReminderState | None = None,
) -> None:
    """Unprompted send (SPEC.md §7/§8) to `chat_id` -- `language` is
    resolved by the caller (`run_due_reminders` passes
    `i18n.resolve_unprompted_language(config)`, which defaults to Thai per
    ROADMAP.md v0.6.0 AC6.3).

    Copy resolution (SPEC-v0.7.md §4 R15): a built-in id reuses its
    existing v0.6.0 catalog entry byte-for-byte (AC13); else the habit's
    own `reminder_text` if the config set one; else the type-generic
    `reminder_generic` template parameterized by `label` (AC14).

    ROADMAP.md v0.9.0: `db`/`config` are additive and default to `None` --
    every pre-v0.9 caller (a test calling `send_reminder(channel, chat_id,
    habit, lang)` directly) is unaffected, byte-identical output, no
    adaptive checks run. `run_due_reminders` below is the real production
    caller and always binds both. Order: quiet hours first (cheap, no
    I/O), then the goal-met DB read -- either suppression short-circuits
    before the send and before `state` is updated (a suppressed reminder
    never counts as "the most recently reminded habit" for snooze
    purposes, ROADMAP.md v0.9.0's own AC9.3 target-habit rule).
    SPEC-v1.2.md R-S2: quiet-hours now uses `chat_id`'s OWN effective
    windows (`effective_quiet_windows`), not just the global config, so a
    custom-time reminder is suppressed under the same per-user rules as a
    config-time one (AC-S6).

    SPEC-v1.8.md R-D1 (module `riders`): the actual send below passes
    `disable_notification=config.notifications.silent_proactive` when
    `config` is given (the real `run_due_reminders` caller always binds
    it), else `False` -- a bare `send_reminder(channel, chat_id, habit,
    lang)` call with no `config` (pre-v1.8 tests, direct callers) stays
    byte-identical (AC-D4)."""
    if config is not None:
        windows = effective_quiet_windows(db, config, chat_id)
        if windows:
            now_local = datetime.now(ZoneInfo(config.app.timezone)).time()
            if _in_quiet_hours(now_local, windows):
                logger.info(
                    "Suppressing %s/%s reminder: inside a quiet-hours window (now=%s)", chat_id, habit.id, now_local
                )
                return

    # SPEC-v1.9.md R15 (v1.9 integration pass): a paused habit's reminder is
    # suppressed the exact same way a quiet-hours-suppressed one is -- an
    # early return before the send, right beside that check. `db is not
    # None` mirrors the goal-met check's own guard just below (a bare
    # `send_reminder(channel, chat_id, habit, lang)` call with no `db`/
    # `config`, e.g. a pre-v1.9 test or direct caller, is unaffected --
    # byte-identical, no pause lookup happens at all). Fail-open (mirrors
    # `_goal_already_met`'s own AC9.5 posture just below): a DB read error
    # is logged and treated as "not paused" -- a hiccup reading the
    # `pauses` table must never silently swallow a reminder.
    if db is not None and config is not None:
        try:
            today_local = datetime.now(ZoneInfo(config.app.timezone)).date()
            habit_is_paused = pause.is_paused(db, config, chat_id, habit.id, today_local)
        except Exception:
            logger.exception(
                "Pause read failed for %s/%s; sending reminder anyway (fail-open)", chat_id, habit.id
            )
            habit_is_paused = False
        if habit_is_paused:
            logger.info("Suppressing %s/%s reminder: habit is paused", chat_id, habit.id)
            return

    if db is not None and config is not None and _goal_already_met(db, habit, config, chat_id):
        return

    if habit.id in BUILTIN_IDS:
        text = i18n.t(BUILTIN_REMINDER_MESSAGE_IDS[habit.id], language)
    else:
        custom = habit.reminder_text(language)
        text = custom if custom is not None else i18n.t("reminder_generic", language, label=habit.label(language))
    silent = config is not None and config.notifications.silent_proactive
    await channel.send(chat_id, text, disable_notification=silent)
    if state is not None:
        state.last_habit_id[chat_id] = habit.id


async def run_due_reminders(
    channel: Channel,
    config: Config,
    registry: HabitRegistry,
    db: Database,
    state: ReminderState | None = None,
    clock=datetime.now,
    registry_for: Callable[[str], HabitRegistry] | None = None,
) -> None:
    """SPEC-v1.2.md R-S1: the minutely tick, replacing the per-config-time
    cron fan-out of the removed `schedule_reminders`. Computes the current
    wall-clock `HH:MM` (`config.app.timezone`) once, then for every active
    user × every registered habit, sends that habit's reminder to that
    user iff the current minute is in the user's effective reminder times
    for that habit (`effective_reminder_times`, R-S4). `send_reminder`
    itself re-applies quiet-hours/goal-met suppression per user (R-S2), so
    a custom-time reminder is suppressed under the exact same rules as a
    config-time one (AC-S6). `main.py` schedules this on a single
    `CronTrigger(second=0, ...)` job with `coalesce=True, max_instances=1`
    -- a paused/restarted process never replays a missed minute or
    double-sends (R-S1's own stated mitigation).

    SPEC-v1.7.md R-G3: `registry_for`, when given, resolves the PER-USER
    registry inside the fan-out loop below (`registry_for(user_id)`,
    typically `RegistryProvider.for_user` bound by `main.py`) -- each
    active user's own custom habits are then reminded exactly like a base
    habit (AC-4/AC-S1 surface #6). Additive and optional, mirroring
    `send_reminder`'s own `db`/`config` convention just above: omitted
    (`None`, the default), every existing caller/test that passes only
    `registry` keeps using that SAME registry for every user, byte-
    identical to pre-v1.7 behavior (AC-5) -- `registry` itself is
    unchanged/still required, now serving as the base/fallback registry."""
    current_hhmm = _now_hhmm(clock, config.app.timezone)
    for user_id in db.active_user_ids():
        # Integration step (SPEC-v1.2.md R-P1 call site #5 of 5): resolved
        # per user now, not once globally -- so a user who ran `/lang th`
        # gets their reminders in Thai regardless of what any other
        # active user (or the global config default) resolves to.
        language = i18n.resolve_unprompted_language(config, user_pref=_user_language_pref(db, user_id))
        user_registry = registry_for(user_id) if registry_for is not None else registry
        for habit in user_registry:
            times = effective_reminder_times(db, config, habit, user_id)
            if current_hhmm in times:
                await send_reminder(channel, user_id, habit, language, db, config, state)
