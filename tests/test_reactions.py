"""SPEC-v1.8.md §4 Feature "quicklog" (`core/reactions.py`) -- module tests
for the ACs this module owns (SPEC-v1.8.md §11): AC-A4 (reaction emoji
resolution + fail-open), AC-A5 (reaction scope is a call-site discipline,
documented/verified here as "this module reacts unconditionally whenever
called -- the caller decides WHEN to call it").
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.core import reactions
from habit_assistant.core.habits import Habit


def _habit(id: str, type: str = "numeric") -> Habit:
    return Habit(
        id=id,
        type=type,
        label_en=id,
        label_th=id,
        unit_en="ml" if type in ("numeric", "duration") else None,
        unit_th="มล." if type in ("numeric", "duration") else None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


class RecordingChannel(Channel):
    def __init__(self) -> None:
        self.reactions: list[tuple[str, str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        raise NotImplementedError("not exercised in these tests")

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))


class RaisingChannel(Channel):
    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        raise NotImplementedError("not exercised in these tests")

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        raise RuntimeError("simulated transport failure")


# ---------------------------------------------------------------------------
# emoji_for_habit -- R-Q4's resolution order: id -> type -> "✅" fallback.
# ---------------------------------------------------------------------------


def test_emoji_for_habit_base_ids():
    assert reactions.emoji_for_habit(_habit("water", "numeric")) == "💧"
    assert reactions.emoji_for_habit(_habit("stretch", "duration")) == "💪"
    assert reactions.emoji_for_habit(_habit("diary", "text")) == "✅"


def test_emoji_for_habit_custom_numeric_uses_type_fallback():
    """SPEC-v1.8.md AC-A1's own illustration: a custom NUMERIC habit
    ("pushups | alias=set:10") reacts/buttons with 💪 -- the same emoji
    `stretch` (a DURATION built-in) already uses for any quantifiable
    habit, not a distinct "numeric" glyph."""
    assert reactions.emoji_for_habit(_habit("pushups", "numeric")) == "💪"


def test_emoji_for_habit_custom_duration_uses_type_fallback():
    assert reactions.emoji_for_habit(_habit("workout", "duration")) == "💪"


def test_emoji_for_habit_custom_boolean_uses_ultimate_fallback():
    assert reactions.emoji_for_habit(_habit("meds", "boolean")) == "✅"


def test_emoji_for_habit_custom_text_uses_ultimate_fallback():
    assert reactions.emoji_for_habit(_habit("journal", "text")) == "✅"


def test_emoji_for_habit_unrecognized_type_falls_back_to_ultimate_default():
    """Defensive: a `Habit.type` this map has never heard of (shouldn't
    happen -- `HabitConfig`/`habitdef` both gate `type` to the four known
    values -- but `emoji_for_habit` must still never raise/return None)."""
    assert reactions.emoji_for_habit(_habit("mystery", "something_else")) == "✅"


# ---------------------------------------------------------------------------
# react -- posts the resolved emoji, fail-open on any exception.
# ---------------------------------------------------------------------------


async def test_react_posts_the_resolved_emoji():
    channel = RecordingChannel()
    habit = _habit("water", "numeric")

    await reactions.react(channel, "owner", "msg-1", habit)

    assert channel.reactions == [("owner", "msg-1", "💧")]


async def test_react_posts_type_fallback_emoji_for_a_custom_habit():
    channel = RecordingChannel()
    habit = _habit("pushups", "numeric")

    await reactions.react(channel, "owner", "msg-2", habit)

    assert channel.reactions == [("owner", "msg-2", "💪")]


async def test_react_never_raises_on_transport_error():
    channel = RaisingChannel()
    habit = _habit("stretch", "duration")

    # Must not raise -- AC-A4's "a reaction failure never affects the log
    # or its confirmation" contract.
    await reactions.react(channel, "owner", "msg-3", habit)


async def test_react_logs_the_swallowed_failure(caplog):
    channel = RaisingChannel()
    habit = _habit("stretch", "duration")

    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.reactions"):
        await reactions.react(channel, "owner", "msg-4", habit)

    assert any("reaction" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# AC-A5 (this module's own half of the contract, see the module docstring):
# `react` reacts unconditionally whenever the caller invokes it -- it has
# no notion of "was this a typed log", so there is nothing here for it to
# skip. Documented so a reader of this test file sees the split explicitly
# rather than assuming this module enforces scope it structurally cannot.
# ---------------------------------------------------------------------------


async def test_react_has_no_notion_of_scope_it_reacts_whenever_called():
    channel = RecordingChannel()
    habit = _habit("water", "numeric")

    # Calling it "for a tap" or "for undo" is exactly as valid a call, from
    # this module's own point of view, as calling it for a typed log --
    # R-Q5's restriction lives entirely in the (not-yet-wired) caller.
    await reactions.react(channel, "owner", "any-message-id", habit)

    assert len(channel.reactions) == 1
