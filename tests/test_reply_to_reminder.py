"""SPEC-v1.10.md "Never lose a log" -- module M2, functional 3
(reply-to-reminder attribution, R13/R14, AC12/AC13). Owns:
`core/reply_attribution.py:resolve_reply_value` (the pure value-resolution
function) and its interaction with the shared-surface `ReminderState`
context map (`core/reminders.py`).

`core/routing.py`'s own `handle_inbound_message` branch that WIRES this
into the live inbound path (R13's "after backfill, before preparse" call
site) is the sequential integration seam's job, not this module's (Archi's
own dispatch note) -- so these tests exercise `resolve_reply_value`
directly against real `Habit`/`ReminderState` objects, plus a small
test-local `_attempt` helper that mirrors R13's documented decision chain
(`reply_to_reminder.enabled` -> `habit_for_reply` -> `resolve_reply_value`)
purely to prove the three PURE pieces compose the way R13 describes --
it is NOT a copy of `routing.py`'s real branch and makes no channel/DB
call.
"""

from __future__ import annotations

import pytest

from habit_assistant.config import Config
from habit_assistant.core import reply_attribution
from habit_assistant.core.habits import Habit
from habit_assistant.core.reminders import ReminderState


def _habit(id_: str, type_: str, *, unit_en: str | None = "ml", unit_th: str | None = "มล.") -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=id_,
        label_th=id_,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=2000 if type_ in ("numeric", "duration") else None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


WATER = _habit("water", "numeric", unit_en="ml", unit_th="มล.")
STRETCH = _habit("stretch", "duration", unit_en="min", unit_th="นาที")
MEDS = _habit("meds", "boolean")
DIARY = _habit("diary", "text")


# ---------------------------------------------------------------------------
# AC12 -- a bare positive number attributes to a numeric/duration habit;
# a boolean habit + an affirmative token logs 1.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["500", " 500 ", "500.0", "500.5"])
def test_bare_positive_number_resolves_to_numeric_habit_base_unit_value(text):
    assert reply_attribution.resolve_reply_value(text, WATER) == float(text.strip())


def test_bare_positive_number_resolves_to_duration_habit_base_unit_value():
    assert reply_attribution.resolve_reply_value("15", STRETCH) == 15.0


@pytest.mark.parametrize("text", ["yes", "Yes", "YES", "done", "Done", "true", "1", "ครบ", "แล้ว"])
def test_boolean_habit_affirmative_token_resolves_to_one(text):
    assert reply_attribution.resolve_reply_value(text, MEDS) == 1.0


def test_boolean_habit_affirmative_token_tolerates_surrounding_whitespace():
    assert reply_attribution.resolve_reply_value("  yes  ", MEDS) == 1.0


# ---------------------------------------------------------------------------
# AC13 -- conservatism: only a mapped reply AND a bare value fires; a
# number+unit, an unmapped reply, a check-in/nudge reply, non-value text,
# and a post-restart empty map all fall through with NO wrong attribution.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["500ml", "500 ml", "500mL", "10 min"])
def test_number_plus_unit_reply_does_not_resolve_falls_through(text):
    """A number+unit reply -- even one that would resolve to THIS SAME
    habit via preparse -- is deliberately left to the normal path (R14),
    not resolved here, so it goes through the exact same, already-tested
    preparse/deterministic_parse route a typed "500ml" message does."""
    assert reply_attribution.resolve_reply_value(text, WATER) is None


@pytest.mark.parametrize("text", ["no", "No", "ยัง", "nope", "not yet"])
def test_boolean_habit_non_affirmative_token_falls_through(text):
    """R14's own "everything else -> None" -- a negative/unknown token for
    a boolean habit is NOT guessed as 0.0, it falls through to the normal
    path (which may ask the LLM, or defer, exactly as an ordinary message
    would)."""
    assert reply_attribution.resolve_reply_value(text, MEDS) is None


@pytest.mark.parametrize("text", ["0", "-5", "-500ml"])
def test_zero_or_negative_number_falls_through(text):
    assert reply_attribution.resolve_reply_value(text, WATER) is None


def test_prose_falls_through():
    assert reply_attribution.resolve_reply_value("went for a run", WATER) is None
    assert reply_attribution.resolve_reply_value("went for a run", MEDS) is None


def test_text_habit_never_resolves_even_for_a_bare_number():
    """A text-type habit (e.g. diary) has no sensible bare "value" -- a
    reply is always left to the normal path (LLM reflection), regardless
    of what the text looks like."""
    assert reply_attribution.resolve_reply_value("500", DIARY) is None
    assert reply_attribution.resolve_reply_value("yes", DIARY) is None
    assert reply_attribution.resolve_reply_value("a quiet day", DIARY) is None


@pytest.mark.parametrize("text", ["๕๐๐", "５００"])
def test_thai_and_fullwidth_numerals_resolve_the_same_as_ascii(text):
    """No special-case normalization code needed (see the module's own
    docstring) -- Python's `\\d`/`float()` already treat these the same
    as ASCII digits, for free."""
    assert reply_attribution.resolve_reply_value(text, WATER) == 500.0


def test_returned_value_is_a_plain_float_not_a_string_or_int():
    value = reply_attribution.resolve_reply_value("500", WATER)
    assert isinstance(value, float)


# ---------------------------------------------------------------------------
# AC13 -- the ReminderState half of the conservatism story: unmapped,
# check-in/nudge (never mapped in the first place), and post-restart
# (fresh/empty map) all resolve to "no attribution" via `habit_for_reply`
# alone, before `resolve_reply_value` is ever reached.
# ---------------------------------------------------------------------------


def _attempt(state: ReminderState, config: Config, chat_id: str, message_id: str, text: str, get_habit) -> float | None:
    """Test-local mirror of R13's documented decision chain -- NOT
    `routing.py`'s real branch (that's integration's, unwritten as of this
    module's own scope). Exists only to prove the pure pieces this module
    owns (`ReminderState.habit_for_reply`, `resolve_reply_value`) compose
    into exactly the fall-through behavior AC13 describes."""
    if not config.reply_to_reminder.enabled:
        return None
    habit_id = state.habit_for_reply(chat_id, message_id)
    if habit_id is None:
        return None
    habit = get_habit(habit_id)
    if habit is None:
        return None
    return reply_attribution.resolve_reply_value(text, habit)


def test_mapped_reply_with_bare_value_attributes_correctly():
    state = ReminderState()
    state.remember_reminder("chat1", "8801", "water", cap=32)
    config = Config()

    result = _attempt(state, config, "chat1", "8801", "500", lambda hid: {"water": WATER}.get(hid))

    assert result == 500.0


def test_unmapped_reply_falls_through():
    state = ReminderState()  # never remembered anything
    config = Config()

    result = _attempt(state, config, "chat1", "9999", "500", lambda hid: {"water": WATER}.get(hid))

    assert result is None


def test_reply_to_a_message_id_from_a_different_chat_does_not_cross_chats():
    state = ReminderState()
    state.remember_reminder("chat1", "8801", "water", cap=32)
    config = Config()

    result = _attempt(state, config, "chat2", "8801", "500", lambda hid: {"water": WATER}.get(hid))

    assert result is None


def test_checkin_and_nudge_prompts_are_never_mapped_so_a_reply_falls_through():
    """SPEC-v1.10.md §4 R14: "only per-habit reminder messages are mapped
    ... check-in and nudge prompts are multi-habit and therefore never
    mapped". `core/checkins.py`/`core/nudge.py` never call
    `ReminderState.remember_reminder` (only `core/reminders.py:send_reminder`
    does) -- so a reply to whatever message id a check-in/nudge send
    returned finds nothing here by construction, same as any other
    unmapped id."""
    state = ReminderState()  # a check-in/nudge send never registers here
    config = Config()

    result = _attempt(state, config, "chat1", "7000", "500", lambda hid: {"water": WATER}.get(hid))

    assert result is None


def test_post_restart_empty_map_falls_through_no_wrong_attribution():
    """A fresh `ReminderState()` (as if the process just restarted) has an
    empty `reminder_context` -- a reply to a pre-restart reminder message
    id simply finds nothing, exactly like any other unmapped reply (R14's
    own "on restart the map is empty ... no wrong attribution, no data
    loss")."""
    state = ReminderState()

    assert state.habit_for_reply("chat1", "8801") is None


def test_reply_to_reminder_disabled_via_config_always_falls_through():
    state = ReminderState()
    state.remember_reminder("chat1", "8801", "water", cap=32)
    config = Config.model_validate({"reply_to_reminder": {"enabled": False}})

    result = _attempt(state, config, "chat1", "8801", "500", lambda hid: {"water": WATER}.get(hid))

    assert result is None
