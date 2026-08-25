"""Instant emoji reactions on a user's typed log message (SPEC-v1.8.md §4
Feature "quicklog", R-Q4/R-Q5) -- Bot API 7.0 `setMessageReaction`, via the
shared-surface `Channel.set_message_reaction` (SPEC-v1.8.md R-S2, already
built and, on `TelegramChannel`, already fail-open at the transport level).

This module adds one more layer of fail-open on top of that: `react` below
never lets ANY exception -- not just a transport error -- escape into the
log-confirmation flow that triggers it (R-Q4's own "a reaction failure
never affects the log or its confirmation" contract). A reaction is purely
decorative.

No channel import beyond the `Channel` ABC (SPEC.md §8's seam) -- mirrors
`core/undo_ui.py`'s/`core/reminders.py`'s own import shape.

R-Q5 (reaction scope, enforced by the CALLER, not this module): reactions
fire only for a successful TYPED inbound-message log -- never for a
quick-log button tap (the tap targets the bot's keyboard message, not a
user log), undo, a command reply, a clarifying question, or a
deferred/unparsed ack. This module has no way to enforce that itself (it
has no notion of "was this a typed log" -- it just reacts to whatever
`chat_id`/`message_id`/`habit` it's given) -- the integration step (R-Q4,
`main.py`'s own wiring, not this module's scope) is what only calls `react`
from the one call site right after a successful typed-log confirmation,
gated on `inbound_message_id is not None` and `[reactions] enabled`. Tests
below assert this module's OWN half of that contract instead: `react`
itself makes no distinction and simply reacts whenever called -- the
"never for taps/undo/commands" half of R-Q5 is a call-site discipline,
verified by inspection/integration tests, not something a unit test of
this module alone can observe.
"""

from __future__ import annotations

import logging

from habit_assistant.channels.base import Channel
from habit_assistant.core.habits import Habit

logger = logging.getLogger(__name__)

# R-Q4: "Emoji from REACTION_EMOJI (base ids: water->💧, stretch->💪,
# diary->✅; a small type map for the rest; ✅ ultimate fallback for any
# custom habit)". SPEC-v1.8.md's own AC-A1 illustration pins down what
# that "small type map" actually is: a custom NUMERIC habit ("pushups |
# alias=set:10") gets 💪, the SAME emoji `stretch` (a DURATION built-in)
# already uses -- i.e. any quantifiable (numeric/duration) habit, custom
# or not, reuses `stretch`'s own "effort/rep" emoji, while anything that
# is just DONE-or-not (boolean/text) reuses `diary`'s ✅. One flat
# namespace -- `emoji_for_habit` below tries the habit's own id first
# (the three built-ins), then its `type`, then "✅" as the ultimate,
# unconditional fallback (so it can never return `None`).
REACTION_EMOJI: dict[str, str] = {
    # base ids (built-ins)
    "water": "💧",
    "stretch": "💪",
    "diary": "✅",
    # type fallback, keyed by `Habit.type` -- used only when the habit's
    # own id isn't one of the three above (i.e. every custom habit).
    # numeric/duration (quantifiable) -> 💪 (AC-A1's own pushups example);
    # boolean/text (done-or-not) -> ✅ (same as `diary`'s built-in).
    "numeric": "💪",
    "duration": "💪",
    "boolean": "✅",
    "text": "✅",
}

# The ultimate fallback R-Q4 names explicitly -- kept as its own constant
# (rather than relying on REACTION_EMOJI["boolean"]/["text"] happening to
# already be "✅") so a future edit to either of those two entries can never
# silently change what "no match at all" falls back to.
_ULTIMATE_FALLBACK = "✅"


def emoji_for_habit(habit: Habit) -> str:
    """R-Q4's own resolution order: the habit's id, else its type, else
    the ultimate fallback. Also used by `core/quicklog.py`'s own amount-
    button labels (§3.1's "<emoji> <amount><unit>") so the SAME emoji a
    habit reacts with is the one its quick-log buttons show -- one source
    of truth, not two independently-maintained maps."""
    return REACTION_EMOJI.get(habit.id) or REACTION_EMOJI.get(habit.type) or _ULTIMATE_FALLBACK


async def react(channel: Channel, chat_id: str, message_id: str, habit: Habit) -> None:
    """R-Q4: set one emoji reaction (`emoji_for_habit(habit)`) on
    `message_id` in `chat_id`. Wrapped fail-open in its own right (on top
    of `TelegramChannel.set_message_reaction`'s own transport-level
    fail-open, SPEC-v1.8.md R-S2) -- ANY exception here is logged and
    swallowed, never raised to the caller, so a reaction can never break
    the log or its confirmation (AC-A4)."""
    try:
        await channel.set_message_reaction(chat_id, message_id, emoji_for_habit(habit))
    except Exception:
        logger.exception(
            "Setting a log reaction failed for chat_id=%s message_id=%s habit=%s (fail-open); "
            "the log and its confirmation are unaffected",
            chat_id,
            message_id,
            habit.id,
        )
