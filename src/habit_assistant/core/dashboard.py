"""Live pinned "Today" dashboard (SPEC-v1.6.md §4 Feature 1, module
`dashboard`, R-D1-R-D6): a per-user message pinned to the chat that edits
itself in place as the user logs/undoes/edits, an always-visible progress
board with zero extra pings. Mirrors `core/checkins.py`'s own "tick/command
split" shape where it applies, but this module has no tick of its own --
`refresh` is invoked by every state-changing action (integration, main.py)
plus a 00:00 day-rollover pass, not on a schedule.

Three public entry points (SPEC-v1.6.md §5):

- **`render`** (R-D2): the deterministic, LLM-free per-user board text --
  one line per configured habit for today, registry-generic (R-X1, no
  hardcoded habit ids): goal-bearing (an *effective* goal is configured,
  `core/targets.py:effective_goal`) -> `total/goal unit` + a block-bar +
  `pct%`; boolean -> `✓`/`–`; everything else (a goal-less numeric/duration
  habit, or `text`) -> a plain count of today's entries. Each line also
  carries that habit's current streak (`core/streaks.py:compute_streak`,
  the ONE streak algorithm in this app) -- not spelled out in R-D2's own
  three-way split, but consistent with it (still one line per habit) and
  matches this app's own established "Today" precedent,
  `core/streaks.py:format_daily_summary`'s per-line `· streak {n}d` suffix
  (the end-of-day recap this board is a live, editable sibling of).

  SPEC-v1.6.md §3.1's own illustration ("🧘 stretch  ✓ done", "📔 diary
  — not yet") shows a duration/text habit rendered done/not-done rather
  than as a count -- under the SHIPPED default config, `stretch` is
  `duration` (no goal) and `diary` is `text`, i.e. both fall into R-D2's
  own "count-only" bucket, not "boolean". That illustration doesn't match
  R-D2's literal three-way rule applied to the actual default habits.
  AC-D6 cites "(R-D2/R-X1)", not the illustration, as the acceptance
  authority, so this implementation follows R-D2's rule literally (a
  goal-less duration/text habit renders as a count) rather than the
  illustration -- flagged in IMPL-v1.6-dashboard.md's Known Limitations
  for Archi/Sophia to reconcile if the illustration was meant literally.

- **`refresh`** (R-D3/R-D4/R-D6): the fail-open, best-effort live editor.
  `NULL` `dashboard_msg_id` (db.get_dashboard_msg_id) -> disabled, return
  immediately. Otherwise render, and skip the edit entirely when the
  render is byte-identical to the last text this process actually wrote
  for this user (`_last_rendered`, an IN-PROCESS cache -- R-D3's own
  words -- not persisted; losing it on restart just costs one possibly-
  redundant edit, never a correctness issue). `edit_message` returning
  `False` (the user deleted the pinned message, or any other edit
  failure) triggers R-D4's self-heal: re-`send_and_pin` and store the new
  id, still enabled. R-D6: no DND check anywhere in this function --
  dashboard edits are silent by construction (Telegram's `editMessageText`
  sends no notification) and are explicitly exempt.

  **The entire body is one `try`** (mirrors `core/audit.py:record`'s own
  "structurally hard to misuse" shape): a DB read/write failure, a
  channel exception, anything -- logged and swallowed, never propagated.
  This is R-D4's own explicit contract ("any dashboard failure is logged
  and never blocks the triggering log/undo") and the reason `refresh`'s
  callers (main.py's integration step) need no try/except of their own
  around this call.

- **`execute_dashboard`** (R-D1): the `/dashboard on|off|<bare>` command
  handler `core/commands.dispatch`'s `"dashboard"` kind feeds -- same
  recognize-shape-in-commands.py/interpret-and-execute-here split as
  every other settings-style command in this app (`execute_checkin`,
  `execute_lang`, ...). `command.pref_value` is the lowercased trigger
  tail: `None` (bare "/dashboard"/"แดชบอร์ด") -> show the current
  effective state (R-D1's own "empty = show" grammar, not a usage error,
  mirroring `/checkin`'s identical convention); `"on"` -> idempotent
  enable (see the Vera gap-pass fixes below); `"off"` -> `channel.unpin`
  the currently-stored message (if any), clear the column, one fail-open
  `dashboard_off` audit row; anything else -> a usage reply, no write.
  Never raises -- mirrors `execute_checkin`'s identical "a DB/send
  failure is caught, logged, and reported via a friendly
  dashboard_save_failed reply" contract.

Per-user isolation (R-X2/AC-X3): every DB read/write and the in-process
cache below are keyed by `user_id` -- two users' boards can never leak
into each other.

**Vera gap-pass fixes (TEST-v1.6-dashboard.md, Archi-ruled 2026-08-24
against `tests/test_dashboard_gaps.py`'s five findings #1-#5; #6 accepted
as informational, no code change):**

1. **Idempotent `/dashboard on`** (finding #1): the "on" branch now checks
   for an existing pin FIRST. A live existing pin is refreshed in place
   (`edit_message`) with an "already on" acknowledgment -- no second,
   untracked pin. A dead existing pin (`edit_message` -> `False`) self-
   heals: a best-effort `unpin` of the dead one, then falls through to
   the same "create a fresh pin" path a first-time enable uses. No
   dangling pins, ever.
2. **One language resolution for board content** (finding #2): the pinned
   BOARD TEXT (not the confirmation reply) is now always resolved via
   `_board_language` (`i18n.resolve_unprompted_language`) -- the exact
   same function `refresh` already used -- both for a first-time enable
   and for a refresh-in-place re-confirmation, so a default-language user
   never sees the board silently flip language on the very next trigger
   with zero data change. The confirmation REPLY (`dashboard_set_on`/
   `dashboard_already_on`/...) still honors the caller-supplied `lang`
   (a genuine reply to that inbound command) -- only the board's own
   content is unified.
3. **Guarded on-branch render** (finding #3): the initial `render(...)`
   call in the "on" branch is now inside the same never-raises try/except
   discipline as every other write in this function -- a DB read failure
   inside `render()` is caught, logged, and reported via
   `dashboard_save_failed`, not propagated.
4. **Zero-goal classification** (finding #4): `render`'s goal-bearing
   gate is now `goal is not None` (not truthiness) -- an effective goal
   of exactly `0.0` (a legal config-time value) now renders through the
   goal-bearing branch instead of being misclassified as count-only.
   `pct` is defined as `100` when `goal == 0` (a zero target is trivially
   always met) rather than dividing by zero.
5. **Render budget** (finding #5): `render` now routes through
   `core/render_budget.fit_within_budget` -- the exact same structural
   guard `core/audit_view.py`/`core/history_view.py` already use -- so a
   large habit registry is truncated (oldest-shown-first, i.e. the LAST
   habits in registry order) with a bilingual `dashboard_more_rows`
   footer instead of producing a message Telegram's 4096-char
   `sendMessage`/`editMessageText` cap would reject outright. Directly
   relevant to v1.7 custom habits (R-X1's registry can grow arbitrarily)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from habit_assistant.core import audit, i18n, streaks, targets
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET, fit_within_budget

if TYPE_CHECKING:
    from habit_assistant.channels.base import Channel
    from habit_assistant.config import Config
    from habit_assistant.core.commands import Command
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

_BAR_WIDTH = 10
_BAR_FILLED = "▓"
_BAR_EMPTY = "░"
_STATUS_DONE = "✓"
_STATUS_NOT_DONE = "–"

# R-D3: "an in-process per-user cache" of the last text this process
# actually wrote to each user's pinned message -- `refresh`'s own spec'd
# signature (SPEC-v1.6.md §5) carries no extra state parameter (unlike
# `core/reminders.py`'s explicitly-threaded `ReminderState`), so this is a
# private module-level dict rather than an object a caller builds once and
# passes in. Pure optimization state, not business data: losing it (process
# restart, or a fresh test module) only costs one possibly-redundant edit
# the next time `refresh` runs for that user, never a correctness issue.
# Tests that call `refresh` more than once for the same `user_id` and need
# a fresh cache should clear this between cases (see
# tests/test_dashboard.py's own autouse fixture).
_last_rendered: dict[str, str] = {}


def _bar(pct: float) -> str:
    filled = max(0, min(_BAR_WIDTH, round(_BAR_WIDTH * pct / 100)))
    return _BAR_FILLED * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)


def _today_date(config: "Config", clock) -> date:
    """Mirrors `core/checkins.py:_today_str`'s own convention exactly (a
    naive `clock()` result is treated as already being in
    `config.app.timezone`; an aware one is converted to it) -- duplicated
    here rather than imported since, per that module's own documented
    precedent, this is a private, module-local "what's today, in the
    configured timezone" helper every call site in this codebase re-derives
    on its own, not a shared surface. R-D5's own day-rollover requirement
    ("the board shows TODAY per config tz") falls out of this for free: any
    `refresh` call after local midnight sees a new `.date()` here, so the
    render naturally reflects the new day with zero persisted rollover
    state of its own."""
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(config.app.timezone))
    else:
        now = now.astimezone(ZoneInfo(config.app.timezone))
    return now.date()


def _user_language_pref(db: "Database", chat_id: str) -> str:
    """Mirrors `core/checkins.py:_user_language_pref`'s own fail-open
    convention exactly -- duplicated per that module's documented
    precedent, not shared."""
    try:
        user = db.get_user(chat_id)
    except Exception:
        logger.exception("Reading language preference failed for %s; defaulting to auto (fail-open)", chat_id)
        return "auto"
    return user["language_pref"] if user is not None else "auto"


def _board_language(db: "Database", config: "Config", user_id: str) -> i18n.Language:
    """Gap-pass fix #2 (TEST-v1.6-dashboard.md finding #2): the ONE
    resolution the pinned BOARD CONTENT uses everywhere it's rendered --
    `refresh` (no inbound message to resolve from) and
    `execute_dashboard`'s "on" branch (which DOES have an inbound
    message, but must render the board the SAME way every future refresh
    will, not the caller-supplied reply-language) -- so a default-
    language user never sees the board silently flip language on the very
    next trigger with zero underlying data change. The CONFIRMATION reply
    text (`dashboard_set_on`/`dashboard_already_on`/`dashboard_set_off`/
    ...) is unaffected by this -- it still honors the caller-supplied
    `lang` (a genuine reply to that inbound command, `resolve_reply_
    language`'s job upstream in `main.py`), mirroring every other
    execute_* function's contract."""
    return i18n.resolve_unprompted_language(config, user_pref=_user_language_pref(db, user_id))


# ===========================================================================
# render -- R-D2: the deterministic, LLM-free, registry-generic board text.
# ===========================================================================


def render(db: "Database", config: "Config", registry: "HabitRegistry", lang: i18n.Language, user_id: str, clock) -> str:
    """R-D2/R-X1: one line per configured habit, in registry order, for
    `user_id`'s TODAY (per `config.app.timezone`, via `_today_date`). See
    this module's own docstring above for the full three-way rule and the
    streak-suffix rationale. R-X2: every DB read below is scoped to
    `user_id` (AC-X3).

    Gap-pass fix #5 (finding #5): the fully-rendered message is checked
    against `render_budget.TELEGRAM_MESSAGE_BUDGET`; an overflow (a large
    habit registry) is repaired by `render_budget.fit_within_budget`
    dropping the last-shown rows (registry order) and appending a
    bilingual `dashboard_more_rows` footer -- the exact same structural
    guarantee `core/audit_view.py:render_recent`/`core/history_view.py:
    render_history` already have, reusing the SAME shared helper rather
    than a third copy of the same fix."""
    today = _today_date(config, clock)
    today_str = today.isoformat()
    header = i18n.t("dashboard_header", lang, date=today.strftime("%a %d %b"))

    row_lines: list[str] = []
    for habit in registry:
        goal = targets.effective_goal(db, habit, config, user_id)
        streak = streaks.compute_streak(db, config, habit, today, user_id)
        label = habit.label(lang)

        if goal is not None:
            # Gap-pass fix #4 (finding #4): `is not None`, not truthiness
            # -- an effective goal of exactly 0.0 is a legal config-time
            # value and must render as goal-bearing, not count-only.
            total = db.sum_value(user_id, habit.id, today_str)
            # A zero goal is trivially always met (any total, including
            # 0, satisfies `>= 0`) -- pct is defined as 100 rather than
            # dividing by zero.
            pct = round(100 * total / goal) if goal else 100
            unit = habit.unit(lang) or ""
            row_lines.append(
                i18n.t(
                    "dashboard_line_goal",
                    lang,
                    label=label,
                    total=total,
                    goal=goal,
                    unit=unit,
                    bar=_bar(pct),
                    pct=pct,
                    streak=streak,
                )
            )
        elif habit.type == "boolean":
            done = db.count_true(user_id, habit.id, today_str) > 0
            status = _STATUS_DONE if done else _STATUS_NOT_DONE
            row_lines.append(i18n.t("dashboard_line_boolean", lang, label=label, status=status, streak=streak))
        else:
            count = db.count(user_id, habit.id, today_str)
            row_lines.append(i18n.t("dashboard_line_count", lang, label=label, count=count, streak=streak))

    full = "\n".join([header, *row_lines])
    if len(full) <= TELEGRAM_MESSAGE_BUDGET or not row_lines:
        return full
    return fit_within_budget(header, row_lines, render_footer=lambda dropped: i18n.t("dashboard_more_rows", lang, count=dropped))


# ===========================================================================
# refresh -- R-D3/R-D4/R-D6: the fail-open live editor.
# ===========================================================================


async def refresh(
    db: "Database", channel: "Channel", config: "Config", registry: "HabitRegistry", user_id: str, clock=datetime.now
) -> None:
    """R-D3/R-D4: see this module's own docstring above for the full
    contract -- disabled-skip, unchanged-skip, edit-in-place, self-heal on
    a `False` edit, and the "entire body is one try" fail-open shape
    (R-D4's own "never blocks the triggering log/undo"). R-D6: no DND
    check -- dashboard edits are silent by construction and explicitly
    exempt."""
    try:
        msg_id = db.get_dashboard_msg_id(user_id)
        if msg_id is None:
            return  # R-D3: disabled, nothing to do.

        lang = _board_language(db, config, user_id)
        text = render(db, config, registry, lang, user_id, clock)

        if _last_rendered.get(user_id) == text:
            return  # R-D3: unchanged -- skip the redundant edit.

        edited = await channel.edit_message(user_id, msg_id, text)
        if edited:
            _last_rendered[user_id] = text
            return

        # R-D4: self-heal -- the edit failed (message deleted/"not found",
        # or any other edit failure); recreate + re-pin and store the new
        # id, still enabled.
        new_id = await channel.send_and_pin(user_id, text)
        if new_id is None:
            # The channel has no pin capability at all (concrete-default
            # degradation, SPEC-v1.6.md §2.2) -- there is nothing to store
            # or later edit. Rather than leave a stale id that would keep
            # failing `edit_message` on every future trigger, honestly
            # fall back to disabled (mirrors `/dashboard off`'s own
            # cleared-column state) so this stops being retried forever.
            logger.warning(
                "Dashboard self-heal couldn't re-pin for %s (channel has no pin capability); disabling", user_id
            )
            db.set_dashboard_msg_id(user_id, None)
            _last_rendered.pop(user_id, None)
            return

        db.set_dashboard_msg_id(user_id, new_id)
        _last_rendered[user_id] = text
    except Exception:
        # R-D4: logged and swallowed, never re-raised -- a dashboard
        # problem must never break the log/undo/edit/target-change that
        # triggered this refresh (the confirmation was already sent before
        # this call, per the integration step's own required ordering).
        logger.exception("Dashboard refresh failed for %s (fail-open); continuing", user_id)


# ===========================================================================
# execute_dashboard -- R-D1: the /dashboard on|off|<bare> command handler.
# ===========================================================================


def _build_show_reply(db: "Database", lang: i18n.Language, user_id: str) -> str:
    try:
        msg_id = db.get_dashboard_msg_id(user_id)
    except Exception:
        logger.exception("Reading dashboard state failed for %s; showing as off (fail-open)", user_id)
        msg_id = None
    return i18n.t("dashboard_show_on", lang) if msg_id is not None else i18n.t("dashboard_show_off", lang)


async def execute_dashboard(
    command: "Command", *, db: "Database", channel: "Channel", config: "Config", registry: "HabitRegistry",
    lang: i18n.Language, user_id: str, clock=datetime.now,
) -> str:
    """R-D1: see this module's own docstring above for the full on/off/
    bare-show contract. Never raises -- mirrors `execute_checkin`'s
    identical "a DB/send failure is caught, logged, and reported via a
    friendly reply, not a traceback" contract."""
    raw = (command.pref_value or "").strip().lower()

    if not raw:
        return _build_show_reply(db, lang, user_id)

    if raw == "on":
        # Gap-pass fix #2: the board's own content always resolves
        # language via `_board_language` (same function `refresh` uses),
        # regardless of the caller-supplied `lang` this branch's
        # CONFIRMATION reply below still honors.
        board_lang = _board_language(db, config, user_id)
        try:
            # Gap-pass fix #3: guarded by the same never-raises discipline
            # as every write below -- a DB read failure inside render()
            # must report dashboard_save_failed, not propagate.
            text = render(db, config, registry, board_lang, user_id, clock)
        except Exception:
            logger.exception("Failed to render dashboard for user %r", user_id)
            return i18n.t("dashboard_save_failed", lang)

        try:
            previous = db.get_dashboard_msg_id(user_id)
        except Exception:
            logger.exception(
                "Reading dashboard state failed for %s; treating as not yet enabled (fail-open)", user_id
            )
            previous = None

        if previous is not None:
            # Gap-pass fix #1: idempotent "on" -- already enabled. Refresh
            # the existing pin in place instead of accumulating a second,
            # untracked pin.
            try:
                edited = await channel.edit_message(user_id, previous, text)
            except Exception:
                logger.exception("Failed to refresh the existing dashboard pin for user %r", user_id)
                edited = False

            if edited:
                _last_rendered[user_id] = text
                audit.record(
                    db, actor=user_id, action="dashboard_set", source="command",
                    old_value=previous, new_value=previous,
                )
                return i18n.t("dashboard_already_on", lang)

            # The stored pin is dead ("not found"/any other edit failure)
            # -- self-heal: best-effort unpin the dead one (its own
            # failure never blocks the re-pin below), then fall through
            # to the same "create a fresh pin" path a first-time enable
            # uses -- no dangling duplicate pin either way.
            try:
                await channel.unpin(user_id, previous)
            except Exception:
                logger.exception("Best-effort unpin of the dead dashboard pin failed for user %r", user_id)

        try:
            new_id = await channel.send_and_pin(user_id, text)
        except Exception:
            logger.exception("Failed to send/pin dashboard for user %r", user_id)
            return i18n.t("dashboard_save_failed", lang)

        if new_id is None:
            # Concrete-default degradation (SPEC-v1.6.md §2.2): this
            # channel can't pin at all -- honest reply, no write (there is
            # nothing `refresh` could ever edit later without an id).
            return i18n.t("dashboard_unsupported", lang)

        try:
            db.set_dashboard_msg_id(user_id, new_id)
        except Exception:
            logger.exception("Failed to persist dashboard id for user %r", user_id)
            return i18n.t("dashboard_save_failed", lang)

        _last_rendered[user_id] = text
        audit.record(db, actor=user_id, action="dashboard_set", source="command", old_value=previous, new_value=new_id)
        return i18n.t("dashboard_set_on", lang)

    if raw == "off":
        try:
            previous = db.get_dashboard_msg_id(user_id)
        except Exception:
            previous = None

        if previous is not None:
            try:
                await channel.unpin(user_id, previous)
            except Exception:
                # R-D4-style fail-open: an unpin failure must not block
                # clearing the column/confirming -- the user asked to turn
                # this off, and that must succeed even if Telegram can't
                # unpin (message already gone, permissions, etc.).
                logger.exception("Failed to unpin dashboard message for user %r", user_id)

        try:
            db.set_dashboard_msg_id(user_id, None)
        except Exception:
            logger.exception("Failed to clear dashboard state for user %r", user_id)
            return i18n.t("dashboard_save_failed", lang)

        _last_rendered.pop(user_id, None)
        audit.record(db, actor=user_id, action="dashboard_off", source="command", old_value=previous, new_value=None)
        return i18n.t("dashboard_set_off", lang)

    return i18n.t("dashboard_usage", lang)
