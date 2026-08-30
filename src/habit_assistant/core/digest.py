"""SPEC-LINE.md §4 R-C1-R-C8 (module C, branch `line-version`): the trimmed
daily digest -- the ONE proactive push a LINE user can receive per day,
batching every other proactive surface this app has (reminders, the
end-of-day recap, the almost-there nudge, a grace notification, a pending
release announcement) into one message, plus the push-quota bookkeeping
that makes "never exceed one push/user/day" actually auditable.

Two public entry points (SPEC-LINE.md §5.3), mirroring `core/announce.py`'s
own "one async fan-out, one sync/pure composer" split:

- `compose_digest` (sync, pure): for ONE user, builds the bilingual digest
  body from data already in the DB -- no channel, no writes. `None` means
  "nothing worth saying today" (R-C1's own qualifier) -> the caller sends
  nothing, spending no quota.
- `run_daily_digest` (async): `db.active_user_ids()` fan-out, skips a
  user with `users.digest_opt_out=1` (R-C4), composes, and -- only for a
  non-`None` result -- sends ONE `channel.send(...)` call. Outside any
  reply context (this is always a scheduled call, never a reply), that
  send goes out as a LINE Push and increments `push_ledger` for the
  current month -- but that bookkeeping is the CHANNEL's own job (R-A6/
  R-C6, module A), not this function's; `run_daily_digest` never calls
  `db.increment_push` itself, so the count stays authoritative regardless
  of caller (R-C6's own stated rationale).

Timing (R-C1: "once/day, at `[digest].time`"): this module does NOT gate
on wall-clock time itself. Mirrors `core/jobs.py:weekly_review_job`/
`daily_summary_job`/`wrapped_auto_job` -- none of which re-check "is it my
time" internally either -- trusting the caller's own `CronTrigger(hour=H,
minute=M, ...)` registration (Integration's R-I2) to fire this exactly
once/day. This is also the "no double-push on restart" mechanism: a
process restart doesn't replay an already-passed cron slot (APScheduler
recomputes the NEXT fire time from wall-clock, not from persisted state),
so no `users`-table "already sent today" flag is needed here, the same
"fires at most once/day BY CONSTRUCTION" posture `core/nudge.py`'s own
docstring states for its own single-fixed-minute gate.

R-C2 (suppression of every OTHER proactive send on LINE) is `core/jobs.py`'s
own responsibility, gated there on `config.channel.type == "line"` -- not
duplicated here.

`execute_digest_toggle` is the `/digest on|off` setter (SPEC-LINE.md §9
OQ4's own default resolution: a new matcher, Thai alias `สรุปรายวัน`,
audited like `/quiet`/`/checkin` -- R-C4's "must be LLM-free and audited
like other preference writes"), `core/commands.dispatch`'s `"digest"` kind
feeds it. Same "structured op in, formatted string out, never raises"
contract as `core/checkins.execute_checkin`."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.triggers.date import DateTrigger

from habit_assistant import __version__
from habit_assistant.core import access, audit, grace, i18n, nudge, streaks, user_prefs
from habit_assistant.core.release_notes import RELEASE_NOTES, get_release_note
from habit_assistant.core.reminders import effective_quiet_windows

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.core.registry_provider import RegistryProvider
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# R-C5/§4: "on the weekly-review weekday" -- `config.weekly_review.
# day_of_week` is a simple 3-letter cron day-of-week token (the shipped
# default "sun"), used verbatim as an APScheduler `CronTrigger` field
# elsewhere (`core/app.py`); this module only needs "does today match that
# token", not full cron-expression parsing, so a small literal lookup is
# proportionate rather than pulling in a cron parser for one comparison.
_WEEKDAY_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# TEST-LINE-C.md Finding 1 (Vera, round 2): SPEC-LINE.md §7's LINE text-
# object hard limit is 5,000 chars; a large, mostly-grace-bridged registry
# (each bridged habit contributing its own full `grace_message_line`
# sentence) can blow past it. `_LINE_TEXT_BUDGET` is the target ceiling
# `compose_digest`'s compaction/truncation fallbacks below aim to land
# under, with margin, so the final `assert len(text) < 5000` at the bottom
# of `compose_digest` never actually fires in practice -- it's a structural
# guarantee, not the primary mechanism. This fix is confined to this file
# (Archi's round-2 instruction) -- `core/render_budget.py`'s own
# `fit_within_budget` hardcodes Telegram's 4096-char budget, not
# parameterized for a different ceiling, so its exact drop-from-the-tail
# SHAPE is mirrored here (`_truncate_to_budget` below) rather than reused.
_LINE_HARD_LIMIT = 5000
_LINE_TEXT_BUDGET = 4900

# Deliberately NOT registered in `core/i18n.py` (also per the "confined to
# this file" instruction) -- a small, private, module-local pair of
# bilingual strings, same `{"en": ..., "th": ...}` shape as every catalog
# entry, just not shared. Promote to `core/i18n.py` in a later pass if
# Archi wants them in the shared catalog.
_GRACE_COMPACT_TEXT = {
    "en": "🛟 Grace day used for {count} habit(s) today — every one of those streaks is safe. (one grace per habit per week)",
    "th": "🛟 ใช้สิทธิ์ผ่อนผันให้ {count} กิจกรรมวันนี้ — สตรีคของกิจกรรมเหล่านั้นยังปลอดภัยทุกอันนะ (ผ่อนผันได้กิจกรรมละครั้งต่อสัปดาห์)",
}
_TRUNCATED_FOOTER_TEXT = {
    "en": "… and {count} more line(s) omitted to fit LINE's message limit.",
    "th": "… และอีก {count} บรรทัดที่ถูกตัดออกเพื่อให้พอดีกับขีดจำกัดข้อความของ LINE",
}


def _local_now(config: "Config", clock) -> datetime:
    """Same clock-normalization every other injectable-clock call site in
    this codebase uses (`core/reminders.py:in_dnd_now`, `core/timeutil.py`):
    a naive `clock()` result is treated as already being in
    `config.app.timezone`; an aware one is converted to it."""
    now = clock()
    tz = ZoneInfo(config.app.timezone)
    return now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)


def _is_weekly_review_day(config: "Config", today) -> bool:
    target = _WEEKDAY_ABBR.get(config.weekly_review.day_of_week.strip().lower())
    return target is not None and today.weekday() == target


def _format_lines_only(lines: list["streaks.DailySummaryLine"], lang: i18n.Language) -> list[str]:
    """The per-habit body `streaks.format_daily_summary` renders, minus its
    own leading `daily_summary_header` line -- reused here for the (a)
    due-reminders section, which wants the SAME per-type formatting
    (`streaks.py` is the one place that branching lives, R-C1's own "reuses
    existing deterministic progress helpers") under a DIFFERENT header."""
    return streaks.format_daily_summary(lines, lang).split("\n")[1:]


def _due_lines(lines: list["streaks.DailySummaryLine"]) -> list["streaks.DailySummaryLine"]:
    """R-C1(a): "habits still short of goal / not yet logged today" -- a
    goal-bearing habit is due while its total hasn't reached goal yet; a
    goal-less habit (duration/text/boolean, or a numeric habit with no
    configured goal) is due until it has at least one entry today."""
    return [line for line in lines if (line.total < line.goal if line.goal else line.total <= 0)]


def _due_reminders_section(lines: list["streaks.DailySummaryLine"], lang: i18n.Language) -> str:
    header = i18n.t("digest_due_reminders_header", lang)
    due = _due_lines(lines)
    body = "\n".join(_format_lines_only(due, lang)) if due else i18n.t("digest_all_caught_up", lang)
    return f"{header}\n{body}"


def _grace_bridged(
    db: "Database", config: "Config", registry: "HabitRegistry", user_id: str, today
) -> list[tuple[object, int]]:
    """R-C1(d): the habits (and the streak each one protected) grace
    "consumed for that user that day". The nightly `core/jobs.py:grace_tick`
    (00:05, still runs unsuppressed on LINE -- only ITS OWN send is
    suppressed, R-C2) already wrote the `grace_ledger` row for YESTERDAY
    relative to when it ran, which is today's own `yesterday` here too
    (grace_tick and the digest both key off the same calendar day, just at
    different times of it) -- so "was grace consumed today" is exactly "is
    yesterday grace-protected", true only on the one digest run right after
    the bridge happened (the day after, `yesterday` shifts to a date that
    was never protected, so this naturally stops firing on its own, no flag
    needed).

    `protected_streak` (the number `grace_message_line`/the compact
    aggregate line reports) is RE-DERIVED via the identical `streaks.
    compute_streak(..., day_before_yesterday, ...)` call `core/grace.py:
    evaluate_grace` itself used to decide the bridge -- a backward walk
    from a FIXED past end_date is unaffected by anything that happened on
    later dates, so recomputing it a day later returns the exact same
    number without `grace_ledger` needing a column to cache it.

    Returns the raw `(habit, streak)` list -- NOT formatted -- so
    `compose_digest` can decide, based on the assembled digest's total
    length, whether to render it via `grace.format_grace_message` (one full
    sentence per habit) or `_grace_compact_line` (one aggregate line,
    TEST-LINE-C.md Finding 1)."""
    if not config.grace.enabled:
        return []
    yesterday = today - timedelta(days=1)
    day_before_yesterday = today - timedelta(days=2)
    yesterday_str = yesterday.isoformat()

    bridged: list[tuple[object, int]] = []
    for habit in registry:
        if db.get_cadence(user_id, habit.id) is not None:
            continue  # R6: grace never applies to a cadence habit
        protected = db.grace_protected_dates(user_id, habit.id, yesterday_str, yesterday_str)
        if yesterday_str not in protected:
            continue
        streak = streaks.compute_streak(db, config, habit, day_before_yesterday, user_id)
        bridged.append((habit, streak))
    return bridged


def _grace_compact_line(lang: i18n.Language, count: int) -> str:
    return _GRACE_COMPACT_TEXT[lang].format(count=count)


def _assemble(header: str, sections: list[str]) -> str:
    return header + "\n\n" + "\n\n".join(sections)


def _truncate_to_budget(header: str, sections: list[str], lang: i18n.Language) -> str:
    """The pathological fallback (TEST-LINE-C.md Finding 1): reached only
    when grace-compaction alone isn't enough -- e.g. an enormous registry
    where even the due-reminders/daily-summary sections alone are large.
    Mirrors `core/render_budget.py:fit_within_budget`'s own shape (drop
    from the tail one line at a time, append a "N more" footer once
    anything was dropped, floor at `header` + footer) -- not imported
    verbatim since that helper's budget constant is hardcoded to
    Telegram's 4096, not LINE's `_LINE_TEXT_BUDGET`, and this fix is
    confined to `core/digest.py` (Archi's round-2 instruction). Sections
    are flattened to individual physical lines (not dropped whole) so a
    single oversized section can still be trimmed down rather than
    all-or-nothing removed."""
    lines: list[str] = []
    for section in sections:
        lines.extend(section.split("\n"))

    kept = list(lines)
    while True:
        dropped = len(lines) - len(kept)
        parts = [header, *kept]
        if dropped:
            parts.append(_TRUNCATED_FOOTER_TEXT[lang].format(count=dropped))
        candidate = "\n".join(parts)
        if len(candidate) <= _LINE_TEXT_BUDGET or not kept:
            return candidate
        kept.pop()


def _pending_announcement_version(db: "Database", user_id: str) -> str | None:
    """R-C1(e): "release announcement line, if a new version hasn't been
    announced to that user" -- mirrors `core/announce.py:announce_release`'s
    own eligibility check (a version with no `RELEASE_NOTES` entry
    announces nothing; a DB read failure is fail-open, treated as
    not-yet-announced) but does NOT send/mark anything itself -- pure read,
    consistent with `compose_digest` being a pure function. `run_daily_
    digest` re-derives this same result AFTER a successful send to decide
    whether to call `db.set_last_announced_version` (mirrors `announce_
    release`'s own "only mark after a successful send" discipline)."""
    version = __version__
    if version not in RELEASE_NOTES:
        return None
    try:
        if db.get_last_announced_version(user_id) == version:
            return None
    except Exception:
        logger.exception(
            "Reading last_announced_version failed for %s while composing the digest; "
            "treating as not-yet-announced (fail-open)",
            user_id,
        )
    return version


def compose_digest(
    db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language, user_id: str, *, now: datetime
) -> str | None:
    """R-C1: the ONE bilingual digest body for `user_id`, batching (a)
    due-reminders, (b) the full daily summary, (c) the almost-there nudge,
    (e) a pending release announcement, (per R-C7) the owner-only quota
    warning, (per R-C5) an optional weekly-review-ready line, and (d) a
    grace notification -- in that priority order (highest first): (a)/(b)/
    (c)/(e)/quota-warning are kept at full fidelity for as long as
    possible; the grace section is deliberately LAST and the first thing
    compacted/dropped under length pressure (TEST-LINE-C.md Finding 1 --
    see `_grace_bridged`'s own docstring and the budget handling below).
    `now` is expected already resolved to `config.app.timezone` by the
    caller (`run_daily_digest`, once per run, mirrors `core/jobs.py:
    weekly_review_job`/`daily_summary_job`'s own "caller resolves `today`
    once" convention).

    Returns `None` ("nothing to say" -> the caller sends nothing, R-C1's
    own qualifier "with something worth saying") only when EVERY section
    is empty -- in practice this needs an empty/fully-paused registry AND
    no nudge/grace/announcement/review-day/quota-warning; the ordinary
    case (any configured, non-paused habit) always has at least the
    due-reminders + daily-summary sections.

    LINE's hard text-object limit is 5,000 chars (SPEC-LINE.md §7). A
    large, mostly-grace-bridged registry can blow past it -- `grace.
    format_grace_message` contributes one full sentence per bridged habit,
    and grace's own "one per week" cap is per-HABIT, not per-user, so a
    user having one bad day across many habits at once can legitimately
    bridge most/all of them the same night. If the full composition is
    over `_LINE_TEXT_BUDGET`, the grace section is compacted to a single
    aggregate line first (everything else stays full fidelity); if STILL
    over (a pathological, very large registry), a hard, order-preserving,
    drop-from-the-tail truncation with a "N more" footer guarantees the
    result stays under budget regardless of registry size -- the trailing
    `assert` is the structural, always-checked proof of that guarantee."""
    today = now.date()
    yyyymm = today.strftime("%Y-%m")

    sections: list[str] = []

    lines = streaks.compute_daily_summary(db, config, registry, today, user_id)
    if lines:
        sections.append(_due_reminders_section(lines, lang))
        sections.append(streaks.format_daily_summary(lines, lang))

    nudge_message = nudge.build_nudge_message(db, config, registry, lang, user_id, clock=lambda: now)
    if nudge_message:
        sections.append(nudge_message)

    pending_version = _pending_announcement_version(db, user_id)
    if pending_version is not None:
        note = get_release_note(pending_version, lang)
        if note:
            sections.append(note)

    if access.classify(db, user_id) == "owner":
        total = db.monthly_push_total(yyyymm)
        if total >= config.digest.warn_cap:
            sections.append(i18n.t("digest_quota_warning", lang, total=total, cap=config.digest.warn_cap))

    if config.digest.include_weekly_review_day and _is_weekly_review_day(config, today):
        sections.append(i18n.t("digest_review_ready_line", lang))

    bridged = _grace_bridged(db, config, registry, user_id, today)
    grace_full = grace.format_grace_message(bridged, lang) if bridged else ""
    grace_index: int | None = None
    if grace_full:
        grace_index = len(sections)
        sections.append(grace_full)

    if not sections:
        return None

    header = i18n.t("digest_header", lang)
    text = _assemble(header, sections)

    if len(text) > _LINE_TEXT_BUDGET and grace_index is not None:
        sections[grace_index] = _grace_compact_line(lang, len(bridged))
        text = _assemble(header, sections)

    if len(text) > _LINE_TEXT_BUDGET:
        text = _truncate_to_budget(header, sections, lang)

    assert len(text) < _LINE_HARD_LIMIT, (
        f"compose_digest produced a {len(text)}-char digest, at or over LINE's "
        f"{_LINE_HARD_LIMIT}-char hard text-object limit"
    )
    return text


async def _send_one_user_digest(
    db: "Database", channel: "Channel", config: "Config", provider: "RegistryProvider", user_id: str, *, now: datetime | None = None
) -> bool:
    """Composes and sends ONE user's digest RIGHT NOW -- no DND check of
    its own; the caller (`run_daily_digest`, immediately, or the deferred
    one-off job the ARCHI RULING below schedules) has already decided this
    is the right moment. Extracted from `run_daily_digest`'s own former
    single-loop body (Integration pass, closing TEST-LINE-C.md's own
    "digest does not check DND/quiet-hours" gap) so both the immediate and
    the deferred send path share byte-identical compose/send/mark logic.
    Returns True iff a push actually went out, so the caller's once-per-day
    bookkeeping is only set for a real send, never for a silent
    opted-out/nothing-to-say/failed no-op."""
    if now is None:
        now = _local_now(config, datetime.now)
    try:
        lang = i18n.resolve_unprompted_language(config, user_pref=user_prefs.stored_language_pref(db, user_id))
        user_registry = provider.for_user(user_id)
        text = compose_digest(db, config, user_registry, lang, user_id, now=now)
    except Exception:
        logger.exception("Composing the daily digest failed for %s; skipping (fail-open)", user_id)
        return False

    if text is None:
        return False

    try:
        await channel.send(user_id, text)
    except Exception:
        logger.exception("Sending the daily digest failed for %s; skipping (fail-open)", user_id)
        return False

    pending_version = _pending_announcement_version(db, user_id)
    if pending_version is not None:
        try:
            db.set_last_announced_version(user_id, pending_version)
        except Exception:
            logger.exception(
                "Marking %s as announced for v%s failed after a successful digest send; "
                "will resend the note in tomorrow's digest",
                user_id,
                pending_version,
            )
    return True


# ===========================================================================
# ARCHI RULING (Integration pass, closing TEST-LINE-C.md's own "digest does
# not check DND/quiet-hours" Known-Limitations flag): a digest that would
# otherwise fire INSIDE the user's own effective quiet-hours window
# (`core/reminders.py:effective_quiet_windows`) is deferred to that
# window's own END instead of firing anyway or being silently dropped --
# same day for a normal (non-midnight-crossing) window; for one that
# crosses midnight, still "at the window's end", even when that end falls
# on the NEXT calendar day (`_dnd_deferred_datetime`'s own two-branch
# logic mirrors `core/reminders.py:_in_quiet_hours`'s identical midnight
# handling, just returning the boundary instead of a bare bool).
#
# `_DIGEST_DEFERRED_DATES` is a `user_id -> "YYYY-MM-DD"` once-per-day
# guard, process-lifetime, in-memory only -- the SAME "no distributed
# lock" posture `core/routing.py`'s own `_sweep_in_progress` module-level
# guard already takes in this codebase. It exists ONLY to make repeated
# deferral scheduling idempotent (a user already deferred for today is
# never re-scheduled/re-sent by a later call in the same run, or by a
# defensive extra invocation of `run_daily_digest` on the same calendar
# day) -- it does NOT gate the ordinary immediate-send path at all (see
# `tests/test_digest.py::test_run_daily_digest_has_no_internal_dedup_the_
# scheduler_owns_that`, which calls `run_daily_digest` twice with an
# IDENTICAL fixed clock and asserts TWO pushes -- that test's own config
# has no quiet-hours windows configured, so the deferred branch below is
# never entered and its documented "no internal dedup, the scheduler owns
# that" contract is completely unaffected by this guard).
#
# This is safe under the SAME single-instance assumption `deploy/
# habit-assistant-line.service` already requires of the whole process (no
# `Type=forking`, no multi-worker supervisor -- see that unit file's own
# comment): a second concurrent instance could still double-schedule/
# double-send, exactly like a second concurrent instance could double-fire
# the base `CronTrigger` itself -- explicitly out of scope (TEST-LINE-C.md
# Finding 2's own "restart-safe, not concurrency-safe" verdict, unchanged
# by this pass). The COMPLEMENTARY half of "once per day" -- a process
# restart never replaying an already-passed cron slot -- is APScheduler's
# own `CronTrigger` property (it always searches FORWARD from `now`, never
# replays), not anything this guard does; the two together are what R-I2's
# "once-per-day protection" actually rests on.
# ===========================================================================

_DIGEST_DEFERRED_DATES: dict[str, str] = {}


def _dnd_deferred_datetime(db: "Database", config: "Config", user_id: str, now: datetime) -> datetime | None:
    """`None` when `user_id` is NOT currently inside their own effective
    quiet-hours window at `now` -- send immediately, no deferral. Otherwise
    the `datetime` (same `tzinfo` as `now`) of that window's own END: a
    normal (`start <= end`) window ends later the SAME calendar day; a
    midnight-crossing window (`start > end`) ends the NEXT calendar day
    when `now` is on the pre-midnight side, or later the SAME day when
    `now` is already on the post-midnight side -- mirrors `core/
    reminders.py:_in_quiet_hours`'s own two-branch midnight logic exactly,
    just returning the boundary instead of a bare bool. The first
    containing window wins (mirrors `_in_quiet_hours`'s own first-match
    iteration -- overlapping windows are a config-authoring concern, not
    this function's)."""
    windows = effective_quiet_windows(db, config, user_id)
    now_time = now.time()
    for start_s, end_s in windows:
        start_h, start_m = (int(x) for x in start_s.split(":"))
        end_h, end_m = (int(x) for x in end_s.split(":"))
        start_t, end_t = time(start_h, start_m), time(end_h, end_m)
        if start_t <= end_t:
            if start_t <= now_time < end_t:
                return datetime.combine(now.date(), end_t, tzinfo=now.tzinfo)
        else:  # crosses midnight
            if now_time >= start_t:
                return datetime.combine(now.date() + timedelta(days=1), end_t, tzinfo=now.tzinfo)
            if now_time < end_t:
                return datetime.combine(now.date(), end_t, tzinfo=now.tzinfo)
    return None


async def run_daily_digest(
    db: "Database",
    channel: "Channel",
    config: "Config",
    provider: "RegistryProvider",
    *,
    clock=datetime.now,
    scheduler=None,
) -> None:
    """R-C1/R-C4/R-C6: for each `db.active_user_ids()`, skip a user with
    `digest_opt_out=1`, compose (fail-open: a composition error skips just
    that user, logged, retried next run -- mirrors every other per-user
    fan-out job's `try/except` posture in `core/jobs.py`), and -- only for
    a non-`None` result -- one `channel.send(...)`. `config.digest.enabled`
    is the job's own master switch (R-C1's own header default `true`);
    `false` makes this whole fan-out a no-op, no reads/writes of any kind
    (mirrors `core/grace.py:evaluate_grace`'s identical `[grace] enabled`
    short-circuit).

    `scheduler`, additive/keyword-only/defaulted `None` (Integration's own
    ARCHI RULING, see the block just above this function): when given (the
    real production `core/app.py` wiring always does, passing its own live
    `AsyncIOScheduler`), a user currently inside their own quiet-hours
    window has their send DEFERRED to that window's end via a one-off
    `DateTrigger` job instead of sent right now. `None` (every existing
    test/caller that predates this ruling) is byte-identical to the
    pre-ruling behavior -- always sends immediately, no DND check at all.

    A `digest_opt_out` read failure fails CLOSED (skip, don't send) rather
    than open, unlike most reads in this codebase -- deliberately, since
    the one thing being protected here is a scarce, quota-metered send:
    better to miss one user's digest on a transient DB hiccup (retried
    next day) than to push to someone who explicitly opted out because a
    read raised. TEST-LINE-C.md Finding 3: this is its OWN `try/except`,
    separate from the composition one just below -- the two failure
    classes have genuinely different dispositions (fail-CLOSED here,
    fail-open below) and now log distinctly labeled messages, not the one
    shared "(fail-open)" line both used to share."""
    if not config.digest.enabled:
        return

    now = _local_now(config, clock)
    today_str = now.date().isoformat()
    for user_id in db.active_user_ids():
        try:
            opted_out = db.digest_opt_out(user_id)
        except Exception:
            logger.exception(
                "digest_opt_out read failed for %s; skipping this user (fail-closed -- unlike "
                "an ordinary composition error below, which is fail-open, we can't confirm this "
                "user still wants the digest today, so we don't guess and don't spend quota)",
                user_id,
            )
            continue
        if opted_out:
            continue

        if scheduler is not None:
            deferred_at = _dnd_deferred_datetime(db, config, user_id, now)
            if deferred_at is not None:
                if _DIGEST_DEFERRED_DATES.get(user_id) == today_str:
                    continue  # already deferred (or sent) for today -- once-per-day guard
                _DIGEST_DEFERRED_DATES[user_id] = today_str
                job_id = f"digest_deferred_{user_id}_{today_str}"
                scheduler.add_job(
                    _send_one_user_digest,
                    trigger=DateTrigger(run_date=deferred_at),
                    args=[db, channel, config, provider, user_id],
                    id=job_id,
                    replace_existing=True,
                )
                logger.info(
                    "Digest for %s deferred to %s (inside their own quiet-hours window at digest time)",
                    user_id,
                    deferred_at,
                )
                continue

        await _send_one_user_digest(db, channel, config, provider, user_id, now=now)


# ===========================================================================
# execute_digest_toggle -- SPEC-LINE.md §9 OQ4 (default resolution): the
# `/digest on|off` setter `core/commands.dispatch`'s own "digest" kind
# feeds, Thai alias `สรุปรายวัน`.
# ===========================================================================


def _build_show_reply(db: "Database", config: "Config", lang: i18n.Language, user_id: str) -> str:
    if db.digest_opt_out(user_id):
        return i18n.t("digest_toggle_show_off", lang)
    return i18n.t("digest_toggle_show", lang, time=config.digest.time)


async def execute_digest_toggle(
    command: "Command", *, db: "Database", config: "Config", lang: i18n.Language, user_id: str
) -> str:
    """`command.pref_value` is the lowercased trigger tail `core/commands.
    dispatch` captured -- `None` (a bare "/digest"/"สรุปรายวัน") -> show the
    current effective state (R-K8's "empty = show" convention, not a usage
    error); `"on"`/`"off"` -> write `users.digest_opt_out` (off means
    opted OUT, i.e. `True`); anything else -> the usage reply, no write.
    Never raises (mirrors `execute_checkin`'s identical contract).

    Audit (R-C4's own "must be ... audited like other preference writes"):
    one fail-open `core/audit.py` row per successful write, `source=
    "command"` (no full-NL `/digest` intent exists, mirrors `execute_quiet`/
    `execute_checkin`'s own default)."""
    raw = (command.pref_value or "").strip().lower()

    if not raw:
        return _build_show_reply(db, config, lang, user_id)

    if raw not in ("on", "off"):
        return i18n.t("digest_toggle_usage", lang)

    opted_out = raw == "off"
    try:
        previous = db.digest_opt_out(user_id)
    except Exception:
        previous = None

    try:
        db.set_digest_opt_out(user_id, opted_out)
    except Exception:
        logger.exception("Failed to set digest_opt_out=%s for user %r", opted_out, user_id)
        return i18n.t("digest_toggle_save_failed", lang)

    audit.record(
        db,
        actor=user_id,
        action="digest_off" if opted_out else "digest_set",
        source="command",
        old_value=previous,
        new_value=opted_out,
    )
    return i18n.t("digest_toggle_set_off" if opted_out else "digest_toggle_set_on", lang)
