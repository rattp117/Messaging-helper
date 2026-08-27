"""SPEC-v1.10.md "Never lose a log" -- module M1 (functionals 1+2, R1-R12):
unparsed-row closure with a terminal state, and conservative, deterministic
tap-to-fix guess buttons.

Built droppable per SPEC-v1.10.md §5's exact signatures -- the integration
seam (`core/routing.py`, built AFTER this module, §11) wires
`reparse_pending_unparsed` to call `tier1_guesses` to decide close-vs-offer,
then `send_closure`/`offer_clarify` after winning the respective CAS
(`db.mark_unparsed_state`, shared surface); `handle_inbound_message`'s live
LLM-unknown branch the same way; `on_callback`'s `clarify:` prefix to
`handle_clarify_callback`. This module is exercised directly by its own
tests (`tests/test_clarify.py`, `tests/test_unparsed_closure.py`) -- no
`core/routing.py` wiring exists yet, by design (§11's "the parallel modules
never edit routing.py").

Integration-pass update (Archi's consolidation ruling): this module used to
carry a verbatim mirror of `core/routing.py`'s recovered-* confirmation
branching (`_send_recovered_confirmation` below), built as a mirror rather
than an import specifically because `core/clarify.py` cannot import
`core/routing.py` (the reverse import, added at integration, is what
creates the cycle). That branching now lives in `core/confirmation.py`
(`send_recovered_confirmation`) -- a leaf neither `routing.py` nor this
module themselves define, so both import it directly with no cycle at all.
`_send_recovered_confirmation` below is now a plain alias for
`confirmation.send_recovered_confirmation`, kept under its original name so
every existing call site in this module (and Vera's own byte-parity tests,
which now double as import-identity guards) needs no further change.

R5's tap-to-fix scope: only a numeric/duration/boolean habit can ever
receive a guess (`_GUESSABLE_TYPES` below) -- a text habit's real "value"
IS its raw free text, which the `clarify:<row>:<habit>:<value>` callback's
own NUMBER-only value grammar (R9) has no way to carry.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from habit_assistant.channels.base import Button, Channel
from habit_assistant.core import (
    confirmation,
    dashboard,
    i18n,
    quicklog,
    reactions,
    render_budget,
    targets,
    undo_ui,
    user_prefs,
)
from habit_assistant.core.units import VALUE_RE, build_unit_lookup, resolve_unit

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import Habit, HabitRegistry
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

# SPEC-v1.10.md §5/§2.1: the unparsed-state machine's three named states.
# `storage/db.py`'s own CAS methods take `from_states`/`to_state` as plain
# strings (or `None` for the legacy/awaiting_llm origin) -- these constants
# are this module's (and its callers') named handle onto those same literals,
# not a second, independently-defined vocabulary.
AWAITING_LLM = "awaiting_llm"
AWAITING_CLARIFY = "awaiting_clarify"
CLOSED = "closed"


# ---------------------------------------------------------------------------
# R5/§2.3: tier-1 guesses -- deterministic, zero-LLM, against the acting
# user's own per-user registry (already resolved by the caller, incl.
# customs -- mirrors every other registry-generic module in this app).
# ---------------------------------------------------------------------------

_NUMBER_ANYWHERE_RE = re.compile(r"\d+(?:\.\d+)?")

# Only a numeric/duration/boolean habit can receive a tap-to-fix guess --
# see this module's own docstring. A label/alias/unit match against a text
# habit therefore always has "no derivable value" by construction, the same
# outcome §2.3's own value-derivation rule already produces for a goal-less
# numeric/duration match with no number in the text -- this frozenset just
# makes the exclusion explicit instead of relying on it falling out of
# `effective_goal` returning `None` by accident.
_GUESSABLE_TYPES = frozenset({"numeric", "duration", "boolean"})


def _match_kind(token: str, field_value: str) -> str | None:
    """`None` / `"exact"` / `"prefix"` -- §2.3: `token` (already lowercased)
    exactly equals `field_value` (lowercased), or, when `token` is at least
    3 characters, `field_value` starts with it. `field_value` being the
    prefix of `token` (the reverse direction) does NOT count -- a typo like
    "Streaching" (10 chars) is longer than "stretch" (7 chars) and so can
    never be "a length->=3 prefix of" it."""
    field_lower = field_value.lower()
    if token == field_lower:
        return "exact"
    if len(token) >= 3 and field_lower.startswith(token):
        return "prefix"
    return None


def _best_match_kind(candidates: set[str], habit: "Habit") -> str | None:
    """The best (exact beats prefix) match kind across every candidate
    token/whole-text against this habit's label_en/label_th/unit_en/
    unit_th/alias keys (§2.3) -- `None` if nothing matched at all."""
    fields = [habit.label_en, habit.label_th, habit.unit_en, habit.unit_th, *habit.unit_aliases.keys()]
    best: str | None = None
    for token in candidates:
        for field_value in fields:
            if not field_value:
                continue
            kind = _match_kind(token, field_value)
            if kind == "exact":
                return "exact"  # can't beat this
            if kind == "prefix":
                best = "prefix"
    return best


def _number_in_text(text: str) -> float | None:
    """The first number appearing ANYWHERE in `text` (not whole-message-
    anchored, unlike `units.VALUE_RE`) -- §2.3's "the number in the text if
    present" for the label/alias/unit match value-derivation rule, e.g.
    "stretch 15" -> 15 even though the match itself was on "stretch"."""
    match = _NUMBER_ANYWHERE_RE.search(text)
    return float(match.group(0)) if match is not None else None


def _label_match_value(
    habit: "Habit", number_in_text: float | None, db: "Database", config: "Config", user_id: str
) -> float | None:
    """§2.3's value-derivation rule for a label/alias/unit match: the
    number in the text if present, else the habit's effective goal (a
    boolean habit has none, so it falls back to 1 instead); `None` (the
    match is dropped, §2.3's own "a match with no derivable value is
    dropped") if neither is available, OR the derived value is not usable
    (non-positive -- mirrors `core/quicklog.py:_goal_ladder`'s own "a
    non-positive amount can never be a real tap-to-fix target" guard, so a
    config-authored non-positive goal can never surface a dead guess
    button here either)."""
    if number_in_text is not None:
        value = number_in_text
    elif habit.type == "boolean":
        value = 1.0
    else:
        goal = targets.effective_goal(db, habit, config, user_id)
        if goal is None:
            return None
        value = goal
    return value if value > 0 else None


def _is_bare_number(text: str, registry: "HabitRegistry") -> float | None:
    """§2.3's bare-number condition: the whole stripped text is a positive
    number with no unit token, OR a unit token that doesn't resolve against
    `registry` (a resolvable unit means the deterministic pre-parser would
    already have placed it -- this is the tier-1 LAST-RESORT reading for
    text that reached here despite that)."""
    match = VALUE_RE.match(text.strip())
    if match is None:
        return None
    num = float(match.group("num"))
    if num <= 0:
        return None
    unit_raw = match.group("unit")
    if unit_raw is None:
        return num
    if resolve_unit(build_unit_lookup(registry), unit_raw.lower()) is not None:
        return None  # a resolvable unit is not a "bare" number
    return num


def tier1_guesses(
    text: str, registry: "HabitRegistry", db: "Database", config: "Config", user_id: str
) -> list[tuple[str, float]]:
    """SPEC-v1.10.md §2.3/R5: deterministic, zero-LLM tap-to-fix guesses
    against the acting user's own per-user registry (`registry`, already
    resolved by the caller -- incl. customs). Two independent sources,
    unioned then de-duplicated by `(habit_id, value)` -- exact label/
    alias/unit matches first, then prefix matches, then bare-number
    unit-plausibility guesses (R5's own ordering) -- capped at
    `config.clarify.max_guesses`.

    Worked examples against the shipped default registry (§2.3): "500" ->
    only water is unit-plausible (stretch's goal window excludes it) ->
    `[("water", 500.0)]`; "stretch"/"stre" -> one label/prefix guess at
    stretch's effective goal; "Streaching" (typo) -> no label/prefix match,
    no bare number -> `[]`."""
    stripped = text.strip()
    candidates = {t.lower() for t in stripped.split() if t}
    if stripped:
        candidates.add(stripped.lower())

    exact_guesses: list[tuple[str, float]] = []
    prefix_guesses: list[tuple[str, float]] = []

    number_in_text = _number_in_text(text)
    for habit in registry:
        if habit.type not in _GUESSABLE_TYPES:
            continue
        kind = _best_match_kind(candidates, habit)
        if kind is None:
            continue
        value = _label_match_value(habit, number_in_text, db, config, user_id)
        if value is None:
            continue
        (exact_guesses if kind == "exact" else prefix_guesses).append((habit.id, value))

    plausibility_guesses: list[tuple[str, float]] = []
    bare_n = _is_bare_number(text, registry)
    if bare_n is not None:
        lower = config.clarify.plausibility_lower
        upper = config.clarify.plausibility_upper
        for habit in registry:
            if habit.type not in ("numeric", "duration"):
                continue
            goal = targets.effective_goal(db, habit, config, user_id)
            if goal and goal > 0 and goal * lower <= bare_n <= goal * upper:
                plausibility_guesses.append((habit.id, bare_n))

    seen: set[tuple[str, float]] = set()
    deduped: list[tuple[str, float]] = []
    for guess in exact_guesses + prefix_guesses + plausibility_guesses:
        if guess in seen:
            continue
        seen.add(guess)
        deduped.append(guess)
    return deduped[: config.clarify.max_guesses]


# ---------------------------------------------------------------------------
# §3.2: the guess-offer buttons.
# ---------------------------------------------------------------------------


def _format_amount(value: float) -> str:
    """Compact numeric rendering for a button label/callback payload --
    "500" for a whole number, "0.5" for a fraction. A small, independent
    copy of `core/quicklog.py:_format_amount`'s own rounding contract
    (module-private there, so not imported across files) -- same
    "verbatim mirror instead of a cross-module import" posture as this
    module's own `_send_recovered_confirmation` below, and it must produce
    a value `_CLARIFY_CALLBACK_RE`'s grammar (<=6 fractional digits) can
    always re-parse."""
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def build_guess_buttons(
    guesses: list[tuple[str, float]], row_id: int, registry: "HabitRegistry", lang: i18n.Language
) -> list[Button]:
    """SPEC-v1.10.md §3.2: one button per guess -- `"<emoji> <label>
    <amount><unit>"` for a numeric/duration guess (e.g. "💧 water 500ml"),
    `"<emoji> <label>"` for a boolean guess (no unit to show) --
    `callback_data = "clarify:<row_id>:<habit_id>:<value>"` (R9's exact
    grammar). A guess naming a habit no longer in `registry` (should not
    happen -- `tier1_guesses` only ever guesses from the SAME registry
    passed to it -- but guarded rather than assumed) is silently skipped."""
    buttons: list[Button] = []
    for habit_id, value in guesses:
        habit = registry.get(habit_id)
        if habit is None:
            continue
        emoji = reactions.emoji_for_habit(habit)
        amount = _format_amount(value)
        if habit.type == "boolean":
            label = f"{emoji} {habit.label(lang)}"
        else:
            unit = habit.unit(lang) or ""
            label = f"{emoji} {habit.label(lang)} {amount}{unit}"
        buttons.append((label, f"clarify:{row_id}:{habit_id}:{amount}"))
    return buttons


# ---------------------------------------------------------------------------
# §3.1/§3.2: the two outbound notifications. Both assume the caller has
# already won the relevant CAS (`mark_unparsed_state`) before calling --
# same "row is assumed live, the caller already checked" contract
# `core/undo_ui.py:send_undo_confirmation` documents for itself.
# ---------------------------------------------------------------------------

# `raw_message` is arbitrary user input with no upstream length cap of its
# own (Telegram itself allows up to ~4096 chars per message) -- quoting it
# verbatim in full could push the closure/offer template past Telegram's
# OWN `sendMessage` length limit (`TelegramChannel.send`/`send_actionable`
# call `resp.raise_for_status()` unconditionally, no try/except, so an
# oversized send here would raise; in the recovery sweep, an unhandled raise
# on one row would abort the rest of that pass for every other pending
# user). `_QUOTE_MAX_CHARS` bounds just the QUOTED portion of the message --
# reuses `core/render_budget.py:truncate`, the same per-value truncation
# primitive `core/audit_view.py`/`core/history_view.py` already use, rather
# than reimplementing character-slicing here, just with a larger cap than
# its own 60-char `MAX_VALUE_CHARS` default: the quoted text IS the point of
# these two messages (not one row among many in a list), so it gets much
# more room. 200 chars leaves >3800 chars of headroom under Telegram's 4096
# budget even after the template's own (short, fixed) surrounding copy.
_QUOTE_MAX_CHARS = 200


def _quote(text: str) -> str:
    """Bounds the raw text quoted in §3.1/§3.2's messages -- never applied
    to the text passed into `tier1_guesses`/`offer_clarify`'s own guess
    recomputation, only to what actually gets embedded in the sent
    message, so truncation can never change which guesses are offered."""
    return render_budget.truncate(text, max_chars=_QUOTE_MAX_CHARS)


async def offer_clarify(
    channel: Channel,
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    *,
    row_id: int,
    text: str,
) -> None:
    """SPEC-v1.10.md §3.2 (R6/R7): the tap-to-fix guess offer. Callers (the
    `handle_inbound_message` live-LLM-unknown branch, and the recovery
    sweep after winning its own `mark_unparsed_state(to='awaiting_clarify')`
    CAS) are responsible for having already confirmed `tier1_guesses(text,
    ...)` is non-empty and for `row_id` already sitting in `awaiting_clarify`
    state -- this function recomputes the same deterministic guesses (pure,
    no DB write) purely to build the buttons; it performs no state
    transition of its own."""
    guesses = tier1_guesses(text, registry, db, config, user_id)
    buttons = build_guess_buttons(guesses, row_id, registry, lang)
    message = i18n.t("clarify_offer", lang, text=_quote(text))
    await channel.send_actionable(user_id, message, buttons)


async def send_closure(
    channel: Channel,
    db: "Database",
    config: "Config",
    registry: "HabitRegistry",
    lang: i18n.Language,
    user_id: str,
    *,
    text: str,
) -> None:
    """SPEC-v1.10.md §3.1 (R1): the ONE terminal closure notification --
    the caller has already won the `mark_unparsed_state(to='closed')` CAS
    before calling this (R1's exactly-once guarantee lives in that CAS,
    not here). Quotes `text` (bounded to `_QUOTE_MAX_CHARS`, see `_quote`),
    attaches the `/log` keyboard (R10) -- an empty keyboard (no loggable
    habit) falls back to the friendly hint appended to the same message,
    one send either way."""
    message = i18n.t("closure_notification", lang, text=_quote(text))
    buttons = quicklog.build_keyboard(registry, config, db, lang, user_id)
    if buttons:
        await channel.send_actionable(user_id, message, buttons)
    else:
        await channel.send(user_id, f"{message}\n\n{quicklog.empty_keyboard_hint(lang)}")


# ---------------------------------------------------------------------------
# R9: the `clarify:<row>:<habit>:<value>` callback_query handler.
# ---------------------------------------------------------------------------

# Mirrors `core/quicklog.py:_LOG_CALLBACK_RE`'s exact shape/rationale
# (habit id grammar, `re.ASCII` against forged non-ASCII digits) -- R9's
# own "mirrors quicklog" instruction.
_CLARIFY_CALLBACK_RE = re.compile(
    r"^clarify:(?P<row>\d+):(?P<habit>[a-z0-9_]{1,32}):(?P<value>-?\d{1,15}(?:\.\d{1,6})?)$", re.ASCII
)

# Mirrors `core/undo_ui.py:_SQLITE_MAX_INTEGER` / `core/quicklog.py:_MAX_LOG_VALUE`.
_SQLITE_MAX_INTEGER = 2**63 - 1
_MAX_CLARIFY_VALUE = 1_000_000_000.0


# Import-identity alias, not a new function -- see this module's own top
# docstring. `clarify._send_recovered_confirmation is confirmation.
# send_recovered_confirmation` holds by construction.
_send_recovered_confirmation = confirmation.send_recovered_confirmation


async def handle_clarify_callback(
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
    """SPEC-v1.10.md §5/R9: the `on_callback` body for a `clarify:<row>:
    <habit>:<value>` tap. `TelegramChannel.run` calls `answerCallbackQuery`
    itself right after awaiting this (mirrors `undo_ui`/`quicklog`'s own
    note) -- `callback_id` is accepted only to match the `on_callback`
    callable shape.

    Malformed payload, or an out-of-range row id/value -> logged and
    ignored (no read/write/send) -- same "no legitimate origin" bucket as
    `quicklog._LOG_CALLBACK_RE`'s own mismatch case. An unknown/foreign
    habit id (not in the TAPPING user's own `registry`) -> a friendly
    no-op reply (AC10). A text-type habit id, or a boolean/numeric/
    duration value outside this habit's valid range, is never a
    legitimate tap-to-fix target (§5's `_GUESSABLE_TYPES`/`build_guess_
    buttons` never encode one) -> silently ignored, mirroring `quicklog.
    handle_log_callback`'s identical guard.

    Only a genuinely-shaped payload naming an owned, guessable habit
    reaches the CAS: `db.resolve_unparsed(from_states=('awaiting_clarify',
    ), ...)`. Winning (rowcount 1) -> the recovered-* confirmation + Undo +
    dashboard refresh (R9), no audit row (R12 -- an ordinary log). Losing
    (rowcount 0 -- another tap already won this exact race, AC11, or the
    row was never/no-longer in `awaiting_clarify`, AC10) -> one friendly
    `clarify_already_handled` reply, no write, no further action."""
    match = _CLARIFY_CALLBACK_RE.match(data)
    if match is None:
        logger.info("Ignoring malformed clarify callback_query data: %r", data)
        return

    row_id = int(match.group("row"))
    habit_id = match.group("habit")
    value = float(match.group("value"))
    if row_id > _SQLITE_MAX_INTEGER or abs(value) > _MAX_CLARIFY_VALUE:
        logger.info("Ignoring clarify callback_query data with an out-of-range row id or value: %r", data)
        return

    lang = i18n.resolve_reply_language(source_text, config, user_pref=user_prefs.stored_language_pref(db, chat_id))

    # R9/AC10, SPEC-v1.2.md R-C3's own established multi-user-isolation
    # precedent -- mirrored line-for-line from `undo_ui.handle_undo_
    # callback`: a `clarify:` payload is a per-ROW capability token, not
    # just a per-HABIT one. `db.resolve_unparsed`'s own CAS predicate has
    # NO `user_id` term at all (it only narrows by primary key + state), so
    # without this check a stranger who merely guesses a small sequential
    # row id (habit ids like "water" need no special knowledge -- they're
    # in every base registry) could reclassify and confirm someone ELSE's
    # row. A missing row, an already-reclassified row (`category !=
    # 'unparsed'`), and a row owned by a different chat all collapse into
    # the SAME friendly no-op a stale/already-resolved tap already sends --
    # a stranger tapping a stolen/guessed callback_data learns nothing
    # about whether the row even exists, exactly like `undo_ui`'s own
    # `already_undone` reply.
    row = db.get_log(row_id)
    if row is None or row["category"] != "unparsed" or row["user_id"] != chat_id:
        await channel.send(chat_id, i18n.t("clarify_already_handled", lang))
        return

    # R9: resolved against the TAPPING user's own registry ONLY -- `registry`
    # is already scoped to `chat_id` by the caller (mirrors `undo_ui.
    # handle_undo_callback`/`quicklog.handle_log_callback`'s own `registry`
    # parameter), so a habit_id this chat doesn't own simply isn't present.
    habit = registry.get(habit_id)
    if habit is None:
        await channel.send(chat_id, i18n.t("quicklog_unknown_habit", lang))
        return

    if habit.type == "text":
        logger.info("Ignoring clarify callback_query data naming a non-guessable (text) habit: %r", data)
        return

    if habit.type == "boolean":
        if value != 1:
            logger.info("Ignoring clarify callback_query data with an invalid boolean value: %r", data)
            return
    elif value <= 0:
        logger.info("Ignoring clarify callback_query data with a non-positive value for %s: %r", habit_id, data)
        return

    value_num = 1.0 if habit.type == "boolean" else float(value)
    won = db.resolve_unparsed(
        row_id,
        from_states=(AWAITING_CLARIFY,),
        category=habit.id,
        value_num=value_num,
        value_text=None,
        habit_type=habit.type,
    )
    if not won:
        await channel.send(chat_id, i18n.t("clarify_already_handled", lang))
        return

    undo_buttons = undo_ui.undo_button(row_id, lang)
    await _send_recovered_confirmation(channel, chat_id, habit, value_num, lang, undo_buttons)
    await dashboard.refresh(db, channel, config, registry, chat_id, clock)
