"""Hourly check-ins + `/checkin` setter (SPEC-v1.5.md §4 Feature 1, module
`checkins`, R-K1-R-K8). Two independent halves, mirroring `core/reminders.
py`'s own "tick job + settings command" split:

- **The tick** (`run_due_checkins`, R-K1): a sibling of `core/reminders.
  run_due_reminders` on the SAME minutely job -- fires at most once per hour
  (`:00` only), per active user, when that user's effective check-in
  setting (`effective_checkin`, R-K2) is enabled and the current hour falls
  inside their window. Strictly LLM-free: `build_checkin_message` (R-K6) is
  a deterministic bilingual template built purely from `targets.
  effective_goal` + `db.sum_value`, the exact same aggregation every other
  goal-consuming module in this app already reads through (mirrors `core/
  streaks.py:compute_daily_summary`'s own read pattern). Honors DND via the
  shared `reminders.in_dnd_now` primitive (R-K4/R-D1) and skips entirely
  when every goal-bearing habit is already met today (R-K3, non-nagging).

- **`/checkin`** (`execute_checkin`, R-K8): the deterministic, LLM-free
  per-user setter `core/commands.dispatch`'s own `"checkin"` kind feeds --
  same "structured op in, formatted string out, never raises" contract as
  `core/preferences.execute_quiet`/`execute_lang`. Writes the raw
  `users.checkin_window` column via the shared surface's `db.get/
  set_checkin_window` (IMPL-v1.5-shared.md) -- this module is the ONLY
  place that interprets that raw value's three-way meaning (NULL =
  inherit config, "off" = disabled, "HH:MM-HH:MM" = enabled with that
  window), mirroring `core/schedules.py:effective_reminder_times`'s own
  "storage returns raw, the owning module interprets" split.

OQ1 RESOLVED (b): `config.checkin.enabled` defaults to `false` -- opt-in
for everyone, owner included (AC-8). Migration 008 adds `checkin_window`
as an all-`NULL` column with no backfill, so every existing user inherits
that disabled default by construction until they run `/checkin on`.

Wired into `main.py` at the SPEC-v1.5.md §11 integration step (IMPL-v1.5-
integration.md): `run_due_checkins` runs on its own `checkin_tick` job
(same minutely cadence as `reminder_tick`), and `Command.kind == "checkin"`
is routed to `execute_checkin` in `handle_inbound_message`, alongside the
pre-existing `"quiet"` branch.

Per-user isolation (R-K7/U-ISO): every DB read/write below is scoped to a
single `user_id` -- `run_due_checkins`' own fan-out (`db.active_user_ids()`)
is the only place this module iterates across users at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from habit_assistant.config import _HHMM_RE
from habit_assistant.core import audit, i18n, targets
from habit_assistant.core.reminders import in_dnd_now

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)


# ===========================================================================
# effective_checkin -- R-K2: resolve users.checkin_window's raw three-way
# storage into (enabled, window). The ONE place this interpretation happens
# -- run_due_checkins, execute_checkin's own "show" reply, and any future
# caller all read through this, exactly as core/reminders.py:
# effective_reminder_times is the one place a stored reminder-times row is
# interpreted.
# ===========================================================================


def _parse_window(raw: str) -> tuple[str, str] | None:
    """"HH:MM-HH:MM" -> `(start, end)`, or `None` if the shape doesn't
    parse cleanly (a single window, unlike `/quiet`'s comma-separated
    list -- SPEC-v1.5.md §2.2 only ever describes ONE window per user).
    Reuses `config._HHMM_RE`, the exact pattern `core/preferences.py:
    _parse_quiet_windows` already validates HH:MM tokens with -- no drift
    between what this function accepts and what `QuietHoursConfig`/
    `CheckinConfig.window` themselves accept."""
    parts = raw.split("-")
    if len(parts) != 2:
        return None
    start, end = parts[0].strip(), parts[1].strip()
    if not (_HHMM_RE.match(start) and _HHMM_RE.match(end)):
        return None
    return start, end


def effective_checkin(db: "Database", config: "Config", user_id: str) -> tuple[bool, tuple[str, str] | None]:
    """R-K2: `user_id`'s effective check-in setting, from the raw
    `users.checkin_window` value `db.get_checkin_window` returns: `None`
    (never set, or migration 008's own no-backfill default) -> inherit
    `config.checkin.enabled`/`config.checkin.window` (OQ1 resolved (b):
    that inherited default is itself OFF, AC-8); `"off"` -> explicitly
    disabled; any other string -> enabled with that stored window
    (`/checkin on` stores the CONFIG default window explicitly, per
    R-K8's own "stays enabled regardless of the config default"
    requirement, so it resolves through this exact same branch).

    Fail-open on a DB read error (R-K2's own explicit contract): treated
    the same as "no override" -- inherit the config default, which ships
    disabled, so a DB hiccup can never turn INTO extra check-in spam,
    mirroring `core/reminders.py:effective_quiet_windows`'s own fail-open
    posture (falls back to the global config default, never raises)."""
    try:
        raw = db.get_checkin_window(user_id)
    except Exception:
        logger.exception("Reading check-in setting failed for %s; falling back to the config default (fail-open)", user_id)
        raw = None

    if raw is None:
        if not config.checkin.enabled:
            return False, None
        return True, _parse_window(config.checkin.window)

    if raw == "off":
        return False, None

    window = _parse_window(raw)
    if window is None:
        # Defensive only -- execute_checkin never writes a malformed
        # window -- but a corrupt/unexpected stored value should fail
        # safe as disabled, not raise or silently mis-fire.
        logger.warning("Stored checkin_window %r for %s doesn't parse; treating as disabled", raw, user_id)
        return False, None
    return True, window


# ===========================================================================
# build_checkin_message -- R-K3/R-K6: the deterministic bilingual body.
# ===========================================================================


def _today_str(config: "Config", clock) -> str:
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(config.app.timezone))
    else:
        now = now.astimezone(ZoneInfo(config.app.timezone))
    return now.date().isoformat()


def build_checkin_message(
    db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language, user_id: str, clock=datetime.now
) -> str | None:
    """R-K6: a compact progress line per unmet goal-bearing habit
    (`label: today / goal unit`, or a localized "not yet today" when the
    day's total is still zero), plus one gentle invite line. R-K3
    (non-nagging): `goal_bearing` = habits with a non-`None` `targets.
    effective_goal` for `user_id`; if it's non-empty and EVERY one is
    already met today, returns `None` (the caller skips the send
    entirely -- no "well done, nothing to log" message, that would be
    its own kind of nag). If `goal_bearing` is empty (e.g. a diary-only
    user with no numeric/duration goals at all), this is NOT the skip
    rule -- a generic one-line nudge is returned instead, so such users
    still get check-ins. R-K7: every read is scoped to `user_id`."""
    today_str = _today_str(config, clock)

    goal_bearing: list[tuple[object, float, float]] = []
    for habit in registry:
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal is None:
            continue
        total = db.sum_value(user_id, habit.id, today_str)
        goal_bearing.append((habit, total, goal))

    if not goal_bearing:
        return i18n.t("checkin_generic_nudge", lang)

    unmet = [(habit, total, goal) for habit, total, goal in goal_bearing if total < goal]
    if not unmet:
        return None  # R-K3: every goal-bearing habit already met today -- skip.

    lines = [i18n.t("checkin_header", lang)]
    for habit, total, goal in unmet:
        label = habit.label(lang)
        unit = habit.unit(lang) or ""
        if total <= 0:
            lines.append(i18n.t("checkin_line_not_yet", lang, label=label))
        else:
            lines.append(i18n.t("checkin_line_progress", lang, label=label, total=total, goal=goal, unit=unit))
    lines.append(i18n.t("checkin_invite", lang))
    return "\n".join(lines)


# ===========================================================================
# run_due_checkins -- R-K1: the minutely-tick sibling of
# core/reminders.run_due_reminders.
# ===========================================================================


def _now_hhmm(clock, tz_name: str) -> str:
    """Mirrors `core/reminders.py:_now_hhmm`'s own convention exactly (a
    naive `clock()` result is treated as already being in `tz_name`; an
    aware one is converted to it) -- duplicated here rather than imported
    since that helper is a private, module-local convention every "what's
    the current local HH:MM" call site in this codebase re-derives on its
    own (`core/reminders.py`, `core/query.py`), not a shared surface."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(tz_name))
    else:
        now = now.astimezone(ZoneInfo(tz_name))
    return now.strftime("%H:%M")


def _user_language_pref(db: "Database", chat_id: str) -> str:
    """Mirrors `core/reminders.py:_user_language_pref`'s own fail-open
    convention exactly -- a preference lookup must never block or crash
    the minutely tick for every OTHER user in the same fan-out."""
    try:
        user = db.get_user(chat_id)
    except Exception:
        logger.exception("Reading language preference failed for %s; defaulting to auto (fail-open)", chat_id)
        return "auto"
    return user["language_pref"] if user is not None else "auto"


async def run_due_checkins(
    channel: "Channel",
    config: "Config",
    registry: "HabitRegistry",
    db: "Database",
    clock=datetime.now,
    registry_for: "Callable[[str], HabitRegistry] | None" = None,
) -> None:
    """R-K1: called from the SAME minutely job that runs `core/reminders.
    run_due_reminders` (integration step, main.py). Returns immediately
    unless the current minute is `:00` -- check-ins fire on the hour only.
    On the hour, for each `db.active_user_ids()`: resolve the effective
    setting (R-K2); skip unless enabled AND the current hour is within the
    window (`start <= HH:00 <= end`, BOTH ends inclusive -- unlike quiet-
    hours' `[start, end)`, this is R-K2's own explicit formula, so the
    shipped default 08:00-20:00 fires 13 times/day, 08:00..20:00
    inclusive); skip if inside DND (R-K4/R-D1); skip if `build_checkin_
    message` returns `None` (R-K3's all-goals-met case). Strictly
    LLM-free throughout -- no Ollama call anywhere in this path (AC-4).

    SPEC-v1.7.md R-G3: `registry_for`, when given, resolves the PER-USER
    registry inside the fan-out loop below -- same additive, optional,
    backward-compatible convention as `core/reminders.py:run_due_
    reminders`'s own `registry_for` (see its docstring): omitted, every
    existing caller/test keeps using `registry` for every user, byte-
    identical to pre-v1.7 (AC-5)."""
    hhmm = _now_hhmm(clock, config.app.timezone)
    if not hhmm.endswith(":00"):
        return

    for user_id in db.active_user_ids():
        enabled, window = effective_checkin(db, config, user_id)
        if not enabled or window is None:
            continue
        start, end = window
        if not (start <= hhmm <= end):
            continue
        if in_dnd_now(db, config, user_id, clock):
            logger.info("Suppressing check-in for %s: inside their own DND window", user_id)
            continue

        lang = i18n.resolve_unprompted_language(config, user_pref=_user_language_pref(db, user_id))
        user_registry = registry_for(user_id) if registry_for is not None else registry
        message = build_checkin_message(db, config, user_registry, lang, user_id, clock)
        if message is None:
            continue
        await channel.send(user_id, message)


# ===========================================================================
# execute_checkin -- R-K8: the /checkin setter `core/commands.dispatch`'s
# own "checkin" kind feeds.
# ===========================================================================


def _build_show_reply(db: "Database", config: "Config", lang: i18n.Language, user_id: str) -> str:
    enabled, window = effective_checkin(db, config, user_id)
    if not enabled or window is None:
        return i18n.t("checkin_show_off", lang)
    start, end = window
    return i18n.t("checkin_show", lang, start=start, end=end)


async def execute_checkin(command: "Command", *, db: "Database", config: "Config", lang: i18n.Language, user_id: str) -> str:
    """R-K8: `command.pref_value` is the lowercased trigger tail `core/
    commands.dispatch` captured -- `None` (a bare "/checkin"/"เช็คอิน") ->
    show the current effective state (NOT a usage error, unlike bare
    "/lang"/"/quiet" -- R-K8's own grammar gives the empty tail a distinct
    meaning); `"off"` -> disable; `"default"` -> clear the override back
    to inheriting the config default, then reply with the resulting
    effective state (reuses `_build_show_reply` -- SPEC-v1.5.md §3.2 names
    only four reply ids, so "default" doesn't get a fifth of its own; the
    honest answer to "what did that do" IS the current state); `"on"` ->
    enable at the CONFIG default window, stored as an explicit window
    string (not the literal "on") so it stays enabled regardless of a
    later config change (R-K8's own explicit requirement); any other
    string is parsed as an explicit "HH:MM-HH:MM" window -- a shape that
    doesn't parse gets the usage reply, no write.

    Never raises (mirrors `execute_lang`/`execute_quiet`/`execute_remind`'s
    identical contract): a DB write failure is caught, logged, and
    reported via `checkin_save_failed` rather than propagating.

    Audit (v1.5.0 integration punch list #2 -- v1.3's own promise that
    every settings change is audited): each successful write records one
    `core/audit.py` entry, mirroring `execute_remind`'s three-way
    `remind_set`/`remind_off`/`remind_default` split -- `"on"` and an
    explicit window both record `checkin_set` (both mean "enable with
    this window", differing only in where the window string came from),
    `"off"` records `checkin_off`, `"default"` records `checkin_default`.
    `record` is itself fail-open (never raises, R-W2), called AFTER the
    write succeeds, `source="command"` (no full-NL `/checkin` intent
    exists, mirroring `execute_quiet`'s own default)."""
    raw = (command.pref_value or "").strip().lower()

    if not raw:
        return _build_show_reply(db, config, lang, user_id)

    try:
        previous = db.get_checkin_window(user_id)
    except Exception:
        previous = None

    if raw == "off":
        try:
            db.set_checkin_window(user_id, "off")
        except Exception:
            logger.exception("Failed to disable check-ins for user %r", user_id)
            return i18n.t("checkin_save_failed", lang)
        audit.record(db, actor=user_id, action="checkin_off", source="command", old_value=previous, new_value="off")
        return i18n.t("checkin_set_off", lang)

    if raw == "default":
        try:
            db.set_checkin_window(user_id, None)
        except Exception:
            logger.exception("Failed to reset check-in setting for user %r", user_id)
            return i18n.t("checkin_save_failed", lang)
        audit.record(db, actor=user_id, action="checkin_default", source="command", old_value=previous, new_value=None)
        return _build_show_reply(db, config, lang, user_id)

    if raw == "on":
        window_str = config.checkin.window
        try:
            db.set_checkin_window(user_id, window_str)
        except Exception:
            logger.exception("Failed to enable check-ins for user %r", user_id)
            return i18n.t("checkin_save_failed", lang)
        audit.record(db, actor=user_id, action="checkin_set", source="command", old_value=previous, new_value=window_str)
        start, end = window_str.split("-", 1)
        return i18n.t("checkin_set_on", lang, start=start, end=end)

    window = _parse_window(raw)
    if window is None:
        return i18n.t("checkin_usage", lang)

    start, end = window
    try:
        db.set_checkin_window(user_id, f"{start}-{end}")
    except Exception:
        logger.exception("Failed to set check-in window for user %r", user_id)
        return i18n.t("checkin_save_failed", lang)
    audit.record(
        db, actor=user_id, action="checkin_set", source="command", old_value=previous, new_value=f"{start}-{end}"
    )
    return i18n.t("checkin_set_window", lang, start=start, end=end)
