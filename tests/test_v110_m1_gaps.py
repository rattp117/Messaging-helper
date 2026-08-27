"""SPEC-v1.10.md "Never lose a log" -- module M1 (`core/clarify.py`)
ADVERSARIAL gap suite. Written by Vera against Luna's `IMPL-v1.10-m1.md`,
on top of her own `tests/test_clarify.py` (50) + `tests/test_unparsed_
closure.py` (10), which already cover the straightforward AC7/AC8/AC10/AC11
cases well. This file probes the corners Archi's dispatch specifically
flagged: tier1_guesses edge semantics (Thai prefix/unit matches, plausibility
boundaries, Thai/full-width numerals), the malformed-callback corpus, the
callback's ROW-OWNERSHIP guard (the critical probe), render-budget behavior
for the closure/offer copy, a zero-LLM behavioral (not just structural)
proof, and a byte-parity drift guard between `clarify._send_recovered_
confirmation` and `core/routing.py`'s own real `recovered_*` branches.

Same conventions as her two files: real on-disk SQLite via `tmp_path`, a
local `FakeChannel` recording both plain and actionable sends, no DB mocks.
`core/routing.py` IS imported in the byte-parity section below (deliberately
-- that section's entire point is to drive routing.py's OWN real code, not a
description of it) but every other section stays within `core/clarify.py`'s
own droppable functions, exactly like her two files.

ROUND 2 (post-fix re-verification, Archi's dispatch): Luna landed two fixes
in `core/clarify.py` only -- (1) a row-ownership pre-check in
`handle_clarify_callback` mirroring `undo_ui.handle_undo_callback` line-for-
line (`row = db.get_log(row_id); if row is None or row["category"] !=
'unparsed' or row["user_id"] != chat_id: <friendly no-op>; return`), argued
to need no CAS-level coverage because `logs.user_id` is write-once for any
row this check ever applies to; (2) `_QUOTE_MAX_CHARS = 200` via
`render_budget.truncate` at both `send_closure`/`offer_clarify`'s message-
construction sites (never applied to the text passed into
`tier1_guesses`/`offer_clarify`'s own guess recomputation, so truncation
cannot change which guesses are offered). Independently confirmed (see
TEST-v1.10-m1.md's own "Round 2" section): the immutability argument holds
-- `grep`-level audit of `storage/db.py` shows exactly ONE `UPDATE logs SET
user_id = ...` site (`attribute_legacy_to_owner`), called only once at
process startup (`core/app.py`, never from a request-handling path) and
scoped to `WHERE user_id IS NULL` (pre-v1.2 legacy rows) -- a row that could
ever reach `awaiting_clarify` always has a concrete, non-NULL `user_id` from
its very first `insert_log` (SPEC-v1.2.md R-D1: never `None` for a new
write), so it is categorically outside that one mutation's scope; and this
app's own single-asyncio-process model (`storage/db.py`'s own docstring)
means there is additionally no `await` between the pre-check read and the
CAS write within one `handle_clarify_callback` call, so even a hypothetically
mutable field would have no interleaving window there. `unparsed_state`
(genuinely mutable/racy) correctly stays CAS-only -- only the ownership
check moved to a pre-read, which is what needed justifying.

Two round-1 tests were WRITTEN to fail once the fix landed (each one's own
docstring pre-authorized this): `test_a_second_forged_tap_from_a_third_
user_is_ALSO_accepted_first_come_first_served` (documented the vulnerability
succeeding for a second attacker) and
`test_closure_notification_for_a_near_max_length_raw_message_may_exceed_the_
telegram_budget` (documented the render-budget overflow). Both are now
REPLACED (not deleted -- see git history if the old bodies are wanted) with
positive regression-guard forms:
`test_multiple_forged_taps_from_different_strangers_are_all_refused_owners_
own_tap_still_wins` and
`test_closure_notification_for_a_near_max_length_raw_message_fits_the_
telegram_budget`. The original two CRITICAL tests
(`test_CRITICAL_forged_tap_reclassifies_another_users_row_no_ownership_
check`, `test_CRITICAL_forged_tap_sends_confirmation_to_the_attacker_not_
the_owner`) needed NO changes -- they always asserted the CORRECT behavior
and simply flip from failing (bug present) to passing (bug fixed), which is
exactly their point as permanent regression guards.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import clarify, i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
OTHER = "other-user"
MALLORY = "mallory"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.edits: list[tuple[str, str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        self.sent.append((chat_id, text))
        return None

    async def send_actionable(self, chat_id: str, text: str, buttons: list[tuple[str, str]]) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return True

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="member", status="active")
    database.upsert_user(OTHER, role="member", status="active")
    database.upsert_user(MALLORY, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def registry(config: Config) -> HabitRegistry:
    return HabitRegistry.from_config(config)


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


def _set_worked_example_goals(db_: Database, user_id: str = OWNER) -> None:
    db_.set_target(user_id, "water", 2000)
    db_.set_target(user_id, "stretch", 30)


def _insert_unparsed(db_: Database, raw: str, unparsed_state: str | None, user_id: str = OWNER) -> int:
    return db_.insert_log(
        LogEntry(None, user_id, "2026-08-27T10:00:00", "unparsed", None, None, raw, "reply", unparsed_state=unparsed_state)
    )


def _custom_registry(config: Config, *habits: Habit) -> HabitRegistry:
    return HabitRegistry([*HabitRegistry.from_config(config), *habits])


# ===========================================================================
# CRITICAL PROBE -- row ownership. R9's own text: "resolves the habit
# against the tapping user's registry (unknown/foreign id -> friendly
# no-op, no write)" -- that's a HABIT-id check only. `undo_ui.
# handle_undo_callback` (SPEC-v1.2.md R-C3/AC-C2, the pattern R9's own
# docstring claims to mirror) additionally requires
# `row["user_id"] == chat_id` before acting on an EXISTING row -- a
# stranger tapping a stolen/guessed callback_data must learn nothing and
# change nothing. `db.resolve_unparsed`'s own CAS predicate
# (`storage/db.py`) is `id = ? AND category = 'unparsed' AND
# (unparsed_state predicate)` -- NO `user_id` filter at all -- so this
# guard, if it exists, has to live in `handle_clarify_callback` itself.
# It does not.
# ===========================================================================


async def test_CRITICAL_forged_tap_reclassifies_another_users_row_no_ownership_check(db, config, registry, channel):
    """Alice (OWNER) has a genuine `awaiting_clarify` row. Mallory -- a
    different, unrelated active user who never received this row's offer
    message -- taps `clarify:<alice's row_id>:water:999` (water is in
    every base registry, so this needs no knowledge of Alice's account,
    only her row id, which is a small sequential integer). Per R-C3's own
    precedent this MUST be a friendly no-op (row not owned by the tapping
    chat) -- instead it silently reclassifies Alice's row to a value
    Mallory chose."""
    row_id = _insert_unparsed(db, "my private diary-ish note 500", "awaiting_clarify", user_id=OWNER)

    await clarify.handle_clarify_callback(
        MALLORY, f"clarify:{row_id}:water:999", "999", "cb-mallory",
        db=db, channel=channel, config=config, registry=registry,
    )

    row = db.get_log(row_id)
    # FAIL: this row belongs to OWNER, not MALLORY -- it must be untouched.
    assert row["category"] == "unparsed", (
        f"cross-user hijack: MALLORY's forged tap reclassified OWNER's row to "
        f"{row['category']!r} (expected it to stay 'unparsed', untouched, since "
        f"MALLORY does not own row {row_id})"
    )
    assert row["unparsed_state"] == "awaiting_clarify"
    assert row["user_id"] == OWNER


async def test_CRITICAL_forged_tap_sends_confirmation_to_the_attacker_not_the_owner(db, config, registry, channel):
    """ROUND 2: same attack as above, from the outbound-message side.
    Note this assertion is TIGHTENED from round 1 -- a bare "recipients ==
    {MALLORY}" would pass under BOTH the old buggy behavior (Mallory got
    the real "Recovered" confirmation) and the fixed one (Mallory gets
    only the friendly refusal), so it no longer distinguishes correct
    from incorrect on its own; the real assertion is on WHAT was sent, not
    just who to. Fixed behavior: MALLORY receives exactly the friendly
    `clarify_already_handled` no-op (never a "Recovered"/actionable
    confirmation, never an Undo button for a row she doesn't own), and
    OWNER receives NOTHING at all -- she never tapped anything, so she
    gets no message either way; this fix's job is to make sure a stranger
    can't manufacture one on her behalf, not to notify her of the
    attempt."""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify", user_id=OWNER)

    await clarify.handle_clarify_callback(
        MALLORY, f"clarify:{row_id}:water:500", "500", "cb-mallory-2",
        db=db, channel=channel, config=config, registry=registry,
    )

    assert channel.actionable == []  # never a real (button-carrying) confirmation
    assert channel.sent == [(MALLORY, i18n.t("clarify_already_handled", "en"))]
    recipients = {chat_id for chat_id, _ in channel.sent}
    assert OWNER not in recipients  # OWNER is never contacted by a tap she didn't make


async def test_multiple_forged_taps_from_different_strangers_are_all_refused_owners_own_tap_still_wins(
    db, config, registry, channel
):
    """ROUND 2 (regression-guard form; was
    `test_a_second_forged_tap_from_a_third_user_is_ALSO_accepted_first_
    come_first_served`, retired per its own docstring's pre-authorization
    now that the CRITICAL fix landed -- see this file's module docstring
    for the round-2 note). Underlines the fix's strength: it isn't merely
    "the row's real owner still wins A race" against one attacker -- ANY
    number of unrelated strangers (OTHER, then MALLORY) can forge a tap
    on the SAME row and EVERY one of them is refused, with zero writes,
    before the row's real owner's own later, genuine tap still succeeds
    normally."""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify", user_id=OWNER)

    await clarify.handle_clarify_callback(
        OTHER, f"clarify:{row_id}:water:111", "111", "cb-other",
        db=db, channel=channel, config=config, registry=registry,
    )
    row = db.get_log(row_id)
    assert row["category"] == "unparsed" and row["unparsed_state"] == "awaiting_clarify"  # OTHER's forgery refused
    assert channel.sent[-1] == (OTHER, i18n.t("clarify_already_handled", "en"))

    await clarify.handle_clarify_callback(
        MALLORY, f"clarify:{row_id}:water:999", "999", "cb-mallory-3",
        db=db, channel=channel, config=config, registry=registry,
    )
    row = db.get_log(row_id)
    assert row["category"] == "unparsed" and row["unparsed_state"] == "awaiting_clarify"  # MALLORY's forgery refused too
    assert channel.sent[-1] == (MALLORY, i18n.t("clarify_already_handled", "en"))

    # The row's real owner's own genuine tap, AFTER two strangers already
    # tried and failed, still works normally -- the fix doesn't collaterally
    # lock the row's real owner out of their own row.
    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-real-owner",
        db=db, channel=channel, config=config, registry=registry,
    )
    row = db.get_log(row_id)
    assert row["category"] == "water" and row["value_num"] == 500.0
    assert row["unparsed_state"] is None
    chat_id, text, _ = channel.actionable[-1]
    assert chat_id == OWNER
    assert text == i18n.t("recovered_water", "en", water_ml=500)


# ===========================================================================
# ROUND 2 fresh probes (Archi's dispatch after Luna's fix landed): a forged
# tap on a foreign row in states OTHER than `awaiting_clarify` (closed,
# never-offered `awaiting_llm`), confirmation the fix doesn't collaterally
# break the row's OWN owner's legitimate tap, and the `_QUOTE_MAX_CHARS=200`
# truncation boundary (exact/off-by-one, and a Thai combining-mark case).
# ===========================================================================


async def test_forged_tap_on_a_foreign_CLOSED_row_is_refused(db, config, registry, channel):
    """A row already terminally `closed` (no tier-1 guess existed, R1) has
    no `clarify:` button in the first place -- this is a forged/replayed
    payload naming one anyway, owned by a different chat. The row-
    ownership pre-check (added in the fix) rejects it before the CAS is
    even attempted -- same friendly no-op a same-owner closed-row tap
    already got in round 1 (`test_clarify.py::
    test_closed_row_is_a_friendly_noop_no_write`), now ALSO covered for a
    non-owning tapper."""
    row_id = _insert_unparsed(db, "Streaching", "closed", user_id=OWNER)

    await clarify.handle_clarify_callback(
        MALLORY, f"clarify:{row_id}:water:500", "500", "cb-foreign-closed",
        db=db, channel=channel, config=config, registry=registry,
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed" and row["unparsed_state"] == "closed"  # untouched
    assert channel.sent == [(MALLORY, i18n.t("clarify_already_handled", "en"))]


async def test_forged_tap_on_a_foreign_AWAITING_LLM_row_is_refused(db, config, registry, channel):
    """A row that hasn't even been offered yet (`awaiting_llm` -- the
    sweep hasn't reached it) owned by someone else. Even before the fix
    this would have failed the CAS's own `from_states=('awaiting_clarify',)`
    guard (a disjoint origin state) -- but the ownership pre-check now
    catches it FIRST (defense in depth: two independent reasons this must
    be refused, not one)."""
    row_id = _insert_unparsed(db, "500", "awaiting_llm", user_id=OWNER)

    await clarify.handle_clarify_callback(
        MALLORY, f"clarify:{row_id}:water:500", "500", "cb-foreign-awaiting-llm",
        db=db, channel=channel, config=config, registry=registry,
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed" and row["unparsed_state"] == "awaiting_llm"  # untouched
    assert channel.sent == [(MALLORY, i18n.t("clarify_already_handled", "en"))]


async def test_owners_own_legitimate_tap_still_works_after_the_ownership_fix(db, config, registry, channel):
    """Regression check on the happy path itself: the row's REAL owner
    tapping their OWN genuine offer must still work exactly as it did in
    round 1 -- the fix must refuse only cross-user forgeries, never a
    legitimate same-user tap. (Her own `test_clarify.py::
    test_winning_tap_reclassifies_and_sends_recovered_confirmation`
    already covers this; repeated here as this file's own explicit
    round-2 regression gate on the exact code path the fix touched.)"""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify", user_id=OWNER)

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-legit",
        db=db, channel=channel, config=config, registry=registry,
    )

    row = db.get_log(row_id)
    assert row["category"] == "water" and row["value_num"] == 500.0
    assert row["unparsed_state"] is None
    chat_id, text, buttons = channel.actionable[-1]
    assert chat_id == OWNER
    assert text == i18n.t("recovered_water", "en", water_ml=500)
    assert buttons == [(i18n.t("undo_button_label", "en"), f"undo:{row_id}")]


async def test_quote_truncation_exactly_200_chars_is_not_truncated(db, config, registry, channel):
    """`clarify._QUOTE_MAX_CHARS == 200`; `render_budget.truncate`'s own
    contract is `len(text) <= max_chars` -> returned AS-IS (no ellipsis).
    A 200-char raw message must therefore be quoted byte-for-byte."""
    exactly_200 = "y" * 200
    await clarify.send_closure(channel, db, config, registry, "en", OWNER, text=exactly_200)
    _, text, _ = channel.actionable[-1]
    assert f'"{exactly_200}"' in text  # quoted verbatim, no "…"
    assert "…" not in text


async def test_quote_truncation_201_chars_is_truncated_to_199_plus_ellipsis(db, config, registry, channel):
    """One character past the boundary: `text[:199] + "…"` -- exactly 200
    quoted characters total (199 kept + 1 ellipsis), never 200 kept
    characters plus an ellipsis (which would put the quote at 201)."""
    exactly_201 = "y" * 201
    expected_quote = ("y" * 199) + "…"
    await clarify.send_closure(channel, db, config, registry, "en", OWNER, text=exactly_201)
    _, text, _ = channel.actionable[-1]
    assert f'"{expected_quote}"' in text
    assert len(expected_quote) == 200


async def test_truncated_thai_quote_at_a_combining_mark_boundary_renders_without_error(db, config, registry, channel):
    """Thai script uses COMBINING tone/vowel marks (separate Unicode
    codepoints logically attached to a preceding base consonant, e.g. the
    mai-ek tone mark). `render_budget.truncate` is a flat Python string
    slice (`text[:max_chars-1]`) -- Python `str` indexing is always
    codepoint-safe (can never split a single codepoint, unlike a raw byte
    slice of UTF-8), so this can never raise or produce invalid Unicode.
    The only OPEN question was whether landing the cut between a base
    consonant and its own combining mark produces something visually
    broken or an exception -- neither happens: worst case is the very
    last kept character loses its trailing tone mark before the ellipsis,
    which is a cosmetic non-issue, not a bug. This test pins that down
    with an actual base+combining-mark sequence straddling the 200-char
    boundary, through the REAL `i18n.t`/`send_closure` path (not just
    `render_budget.truncate` in isolation)."""
    base_and_tone = "ก่"  # base consonant U+0E01 + mai-ek tone mark U+0E48 (2 codepoints)
    thai_text = base_and_tone * 150  # 300 codepoints, well past the 200-char boundary
    assert len(thai_text) == 300

    await clarify.send_closure(channel, db, config, registry, "en", OWNER, text=thai_text)  # must not raise

    _, text, _ = channel.actionable[-1]
    expected_quote = thai_text[:199] + "…"
    assert f'"{expected_quote}"' in text
    assert len(expected_quote) == 200
    # sanity: still valid, round-trippable Unicode text (would raise on a
    # genuinely corrupt surrogate/byte sequence -- it doesn't, by construction
    # of Python str slicing, but this is the empirical confirmation).
    text.encode("utf-8")


# ===========================================================================
# tier1_guesses edges -- Thai label/alias/unit matching semantics, the
# plausibility window's own inclusive boundaries, zero/negative/Thai-numeral
# bare numbers, and per-user registry isolation in both directions.
# ===========================================================================


def test_thai_label_exact_match(db, config, registry):
    """water's own label_th is "น้ำ" -- an exact match, zero-LLM, no
    number in the text -> falls back to the habit's effective goal."""
    _set_worked_example_goals(db)
    guesses = clarify.tier1_guesses("น้ำ", registry, db, config, OWNER)
    assert guesses == [("water", 2000.0)]


def test_thai_label_prefix_match_length_ge_3(db, config, registry):
    """stretch's label_th "ยืดเส้น" (7 chars) -- a length->=3 Thai PREFIX
    "ยืด" (the first 3 characters) must match, exactly the same
    "len(token) >= 3 and field startswith token" rule already proven for
    English "stre"/"stretch"."""
    _set_worked_example_goals(db)
    guesses = clarify.tier1_guesses("ยืด", registry, db, config, OWNER)
    assert guesses == [("stretch", 30.0)]


def test_thai_label_prefix_too_short_below_3_chars_does_not_match(db, config, registry):
    """A 2-character Thai prefix must NOT match -- same length floor as
    the English case, just confirmed on the Thai side too."""
    _set_worked_example_goals(db)
    guesses = clarify.tier1_guesses("ยื", registry, db, config, OWNER)
    assert guesses == []


def test_unit_token_alone_matches_exactly_regardless_of_length(db, config, registry):
    """Typing just "ml" (water's own unit_en, only 2 characters) is an
    EXACT match (`token == field_lower`), so the <3-char prefix floor
    does not apply to it -- exact matches have no length floor at all."""
    _set_worked_example_goals(db)
    guesses = clarify.tier1_guesses("ml", registry, db, config, OWNER)
    assert guesses == [("water", 2000.0)]


def test_thai_unit_token_alone_matches_exactly(db, config, registry):
    """water's unit_th is "มล." -- exact match on the Thai unit token
    alone, same as the English "ml" case above."""
    _set_worked_example_goals(db)
    guesses = clarify.tier1_guesses("มล.", registry, db, config, OWNER)
    assert guesses == [("water", 2000.0)]


def test_alias_key_match_derives_the_HABITS_GOAL_not_the_aliass_own_multiplier(db, config, registry):
    """OBSERVATION (not a bug -- matches SPEC-v1.10.md §2.3's literal
    value-derivation rule verbatim: "the number in the text if present,
    ELSE the habit's effective_goal" -- there is no third branch for "the
    matched alias's own configured multiplier"): water's unit_aliases
    includes "glass"->250. Typing just "glass" DOES match (alias keys are
    in `_best_match_kind`'s own field list), but the guessed VALUE is
    water's full 2000ml goal, NOT the 250ml a "glass" alias actually
    means everywhere else in this app (`core/quicklog.py`'s own alias
    buttons DO render 250ml for the same alias). A tapped button here
    would read "💧 water 2000ml" for someone who typed "glass" -- worth
    flagging to Archi/Sophia as a UX surprise even though it is not a
    spec violation."""
    _set_worked_example_goals(db)
    guesses = clarify.tier1_guesses("glass", registry, db, config, OWNER)
    assert guesses == [("water", 2000.0)]  # NOT 250.0 -- pinned exactly per spec's literal text


def _isolated_numeric_habit(goal: float) -> HabitRegistry:
    """A registry with exactly ONE goal-bearing habit -- avoids the
    default water(2000)/stretch(30) registry's own OVERLAPPING
    plausibility windows around small N (e.g. N=100 is inside BOTH
    water's [100, 10000] and stretch's [1.5, 150] windows), which would
    make a single-habit boundary assertion ambiguous."""
    habit = Habit(
        id="widget", type="numeric", label_en="widget", label_th="a", unit_en="w", unit_th="a", goal=goal,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    return HabitRegistry([habit])


def test_plausibility_window_lower_boundary_is_inclusive(db, config):
    """§2.3: "G*lower <= N <= G*upper", shipped defaults lower=0.05/
    upper=5.0. For goal=2000, the lower boundary N = 2000*0.05 = 100 must
    be INCLUDED (99 must be excluded). Uses an isolated single-habit
    registry -- see `_isolated_numeric_habit`'s own docstring for why."""
    reg = _isolated_numeric_habit(2000)
    assert clarify.tier1_guesses("100", reg, db, config, OWNER) == [("widget", 100.0)]
    assert clarify.tier1_guesses("99", reg, db, config, OWNER) == []


def test_plausibility_window_upper_boundary_is_inclusive(db, config):
    """Same, at the top: N = 2000*5.0 = 10000 must be INCLUDED (10001
    excluded)."""
    reg = _isolated_numeric_habit(2000)
    assert clarify.tier1_guesses("10000", reg, db, config, OWNER) == [("widget", 10000.0)]
    assert clarify.tier1_guesses("10001", reg, db, config, OWNER) == []


def test_zero_bare_number_yields_no_guess(db, config, registry):
    _set_worked_example_goals(db)
    assert clarify.tier1_guesses("0", registry, db, config, OWNER) == []


def test_negative_bare_number_does_not_even_match_the_value_grammar(db, config, registry):
    """`VALUE_RE`'s own `\\d+` has no sign -- "-500" fails the whole-string
    anchored match entirely (not "matches but is filtered as
    non-positive") -- and "-500" is not a label/alias/unit token for
    anything either, so this is a clean `[]`, not an exception."""
    _set_worked_example_goals(db)
    assert clarify.tier1_guesses("-500", registry, db, config, OWNER) == []


def test_thai_numeral_bare_number_is_recognized(db, config, registry):
    """SPEC intent check: `core/backfill.py`'s own module docstring
    claims "no [Thai/full-width numeral] normalizer exists elsewhere in
    this codebase yet (checked: core/units.py:VALUE_RE matches ASCII \\d
    only)" -- that claim is actually FALSE for `VALUE_RE` (Python's bare
    `\\d` matches the Unicode Nd category, not ASCII-only, without
    `re.ASCII`; `float()` also natively parses Thai-digit and full-width
    digit strings). `tier1_guesses` inherits this "for free" via
    `units.VALUE_RE`/its own `_NUMBER_ANYWHERE_RE` -- Thai digit "๕๐๐"
    (500) IS recognized as a bare-number plausibility guess. This is a
    working, positive behavior; pinned here as a regression guard since
    it is currently untested and easy to break by someone "fixing"
    `_NUMBER_ANYWHERE_RE`/`VALUE_RE` to `re.ASCII` while believing (per
    the backfill.py comment above) that Thai digits were never
    supported anywhere else."""
    _set_worked_example_goals(db)
    thai_500 = "๕๐๐"  # ๕๐๐
    fullwidth_500 = "５００"  # ５００
    assert clarify.tier1_guesses(thai_500, registry, db, config, OWNER) == [("water", 500.0)]
    assert clarify.tier1_guesses(fullwidth_500, registry, db, config, OWNER) == [("water", 500.0)]


def test_thai_numeral_inside_a_label_match_also_overrides_the_goal(db, config, registry):
    """§2.3's "the number in the text if present" value-derivation rule,
    with a Thai-digit number, via the label/unit-match path (not the
    bare-number-plausibility path -- "stretch" is a label, not a unit)."""
    _set_worked_example_goals(db)
    thai_15 = "stretch ๑๕"  # "stretch ๑๕"
    assert clarify.tier1_guesses(thai_15, registry, db, config, OWNER) == [("stretch", 15.0)]


def test_habit_with_no_goal_and_no_aliases_and_no_number_is_dropped_via_unit_match_too(db, config, registry):
    """Same "no derivable value -> dropped" rule as her own label-match
    test, exercised via a UNIT-token match instead of a label match (a
    different `_best_match_kind` field, same value-derivation code path)."""
    goalless = Habit(
        id="jumps", type="numeric", label_en="jumping jacks", label_th="กระโดด",
        unit_en="reps", unit_th="ครั้ง", goal=None, reminder_times=(), reminder_text_en=None,
        reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, goalless)
    assert clarify.tier1_guesses("reps", reg, db, config, OWNER) == []


def test_a_custom_habit_never_appears_in_a_different_users_guesses_bidirectional(db, config, registry):
    """Extends her own `test_per_user_registry_includes_a_custom_habit`
    (which checks one direction) to both directions with two DIFFERENT
    users each owning their own conflicting custom habit -- since
    `tier1_guesses` only ever sees whatever `registry` the caller already
    resolved for `user_id` (per-user, via `provider.for_user` upstream),
    this is really a proof that the function has no hidden global-habit
    lookup of its own, not a DB isolation test per se."""
    owner_only = Habit(
        id="pushups", type="numeric", label_en="pushups", label_th="a", unit_en="reps", unit_th="a",
        goal=50, reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    other_only = Habit(
        id="squats", type="numeric", label_en="squats", label_th="b", unit_en="reps2", unit_th="b",
        goal=40, reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    owner_registry = _custom_registry(config, owner_only)
    other_registry = _custom_registry(config, other_only)

    assert clarify.tier1_guesses("pushups", owner_registry, db, config, OWNER) == [("pushups", 50.0)]
    assert clarify.tier1_guesses("pushups", other_registry, db, config, OTHER) == []  # OTHER never sees it
    assert clarify.tier1_guesses("squats", other_registry, db, config, OTHER) == [("squats", 40.0)]
    assert clarify.tier1_guesses("squats", owner_registry, db, config, OWNER) == []  # OWNER never sees it


def test_cap_enforced_with_genuinely_more_candidates_than_default_max_guesses(db, config, registry):
    """Default `config.clarify.max_guesses == 4`. Five distinct exact
    label matches for the SAME text ("x") -- verifies the cap actually
    bites against real over-supply, not just the 2-vs-1 case her own
    suite exercises."""
    _set_worked_example_goals(db)
    habits = [
        Habit(
            id=f"habit_{i}", type="numeric", label_en="x", label_th=f"label{i}", unit_en=f"u{i}", unit_th=f"u{i}",
            goal=10 + i, reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
        )
        for i in range(5)
    ]
    reg = _custom_registry(config, *habits)
    guesses = clarify.tier1_guesses("x", reg, db, config, OWNER)
    assert len(guesses) == 4  # config.clarify.max_guesses default


# ===========================================================================
# Callback malformed-payload corpus -- extends her own parametrized list
# with a few more adversarial shapes: SQL-ish content, and the EXACT digit-
# count boundary the regex itself encodes (15 digits passes the shape check
# but still must be bounds-rejected; 16 digits fails the shape check
# outright).
# ===========================================================================


@pytest.mark.parametrize(
    "data",
    [
        "clarify:1:water'; DROP TABLE logs;--:500",  # SQL-ish -- not a legal habit-id charset anyway
        "clarify:1:water:1e309",  # float-overflow-shaped, not digit-grammar-shaped -- rejected by regex
        "clarify:1:water: 500",  # embedded space in the value field
        "clarify: 1:water:500",  # embedded space in the row field
        "",  # empty payload entirely
    ],
)
async def test_more_malformed_payloads_are_silently_ignored(db, config, registry, channel, data):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")
    await clarify.handle_clarify_callback(
        OWNER, data, "500", "cb-extra", db=db, channel=channel, config=config, registry=registry
    )
    row = db.get_log(row_id)
    assert row["unparsed_state"] == "awaiting_clarify"
    assert channel.sent == []


async def test_value_with_exactly_15_digits_passes_the_shape_regex_but_is_bounds_rejected(db, config, registry, channel):
    """`_CLARIFY_CALLBACK_RE`'s own value grammar is `\\d{1,15}` -- exactly
    15 digits is shape-VALID (unlike 16, which the parametrized corpus
    above/in test_clarify.py never actually reaches, since it fails the
    regex). This value must still be rejected by the separate numeric
    bounds check (`_MAX_CLARIFY_VALUE = 1e9`) before any DB write."""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")
    fifteen_nines = "9" * 15
    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:{fifteen_nines}", "500", "cb-15",
        db=db, channel=channel, config=config, registry=registry,
    )
    assert db.get_log(row_id)["unparsed_state"] == "awaiting_clarify"
    assert channel.sent == []


async def test_value_with_16_digits_fails_the_shape_regex_outright(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")
    sixteen_nines = "9" * 16
    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:{sixteen_nines}", "500", "cb-16",
        db=db, channel=channel, config=config, registry=registry,
    )
    assert db.get_log(row_id)["unparsed_state"] == "awaiting_clarify"
    assert channel.sent == []


async def test_habit_id_exactly_32_chars_is_shape_valid_33_is_not(db, config, registry, channel):
    """`_CLARIFY_CALLBACK_RE`'s habit-id grammar is `[a-z0-9_]{1,32}` --
    confirms the boundary is exactly where the docstring says (mirrors
    `quicklog._LOG_CALLBACK_RE`'s own 32-char habit-id bound)."""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")
    habit_33 = "a" * 33
    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:{habit_33}:5", "500", "cb-33",
        db=db, channel=channel, config=config, registry=registry,
    )
    # 33 chars doesn't even match the regex -- silently ignored, not even
    # the "unknown habit" friendly no-op (that bucket requires a SHAPE-valid
    # payload naming a habit that just isn't in the registry).
    assert channel.sent == []


# ===========================================================================
# Closure/offer copy quality -- format-brace safety in a quoted raw
# message, and render-budget behavior for a maximal-length raw message.
# ===========================================================================


async def test_raw_text_containing_format_braces_is_quoted_literally_not_reinterpreted(db, config, registry, channel):
    """`i18n.t` uses `str.format(**kwargs)` on the TEMPLATE, substituting
    `text` as a plain value -- Python's `.format()` does not recursively
    re-parse a substituted value for further `{}` placeholders, so a raw
    message containing brace-like content must render byte-for-byte, not
    raise a `KeyError`/`IndexError` and not get "double-formatted"."""
    evil_text = "hello {0} {something_undefined} }} {{nested}}"
    await clarify.send_closure(channel, db, config, registry, "en", OWNER, text=evil_text)
    _, text, _ = channel.actionable[-1]
    assert evil_text in text  # quoted verbatim, not reinterpreted or truncated


async def test_closure_notification_for_a_near_max_length_raw_message_fits_the_telegram_budget(db, config, registry, channel):
    """ROUND 2 (regression-guard form; was
    `test_closure_notification_for_a_near_max_length_raw_message_may_
    exceed_the_telegram_budget`, retired per its own docstring's pre-
    authorization now that the fix landed). `send_closure`/`offer_clarify`
    now quote `text` through `clarify._quote` (`render_budget.truncate`,
    `_QUOTE_MAX_CHARS = 200`) before embedding it -- confirms a
    near-Telegram's-own-limit (~4000-char) raw message no longer blows the
    composed closure notification past `TELEGRAM_MESSAGE_BUDGET`, and with
    plenty of headroom to spare (the quote itself is capped at 200 chars,
    so total length is bounded by the template's own short fixed overhead
    + 200, independent of how long the original message was)."""
    near_max_message = "x" * 4000
    await clarify.send_closure(channel, db, config, registry, "en", OWNER, text=near_max_message)
    _, text, _ = channel.actionable[-1]
    assert len(text) <= TELEGRAM_MESSAGE_BUDGET
    assert len(text) < 500  # sanity: the fixed template overhead is small; this isn't just barely fitting


# ===========================================================================
# Zero-LLM: a BEHAVIORAL proof (not just the structural source-grep her own
# `test_module_imports_no_llm_client` already does) -- every LLM client
# entry point is poisoned to raise on any use, and both `tier1_guesses` and
# a full `handle_clarify_callback` tap are exercised successfully anyway.
# ===========================================================================


class _PoisonedOllamaClient:
    def __getattr__(self, name):
        raise AssertionError(f"clarify.py must never touch the LLM client (accessed .{name})")


async def test_tier1_guesses_and_handle_clarify_callback_never_touch_a_poisoned_llm_client(
    db, config, registry, channel, monkeypatch
):
    """Swap the real `parse_message`/`OllamaClient` module attribute for a
    stand-in that raises on ANY attribute access, run a full guess +
    tap sequence, and confirm nothing broke -- the strongest available
    proof (short of literally severing network access) that this module's
    guess derivation and callback handling are 100% deterministic."""
    import habit_assistant.llm.ollama_client as ollama_module

    monkeypatch.setattr(ollama_module, "OllamaClient", _PoisonedOllamaClient())
    # also poison the module-level parse_message function itself, in case
    # anything were ever to import and call it directly
    async def _poisoned_parse_message(*args, **kwargs):
        raise AssertionError("clarify.py must never call parse_message")

    monkeypatch.setattr(ollama_module, "parse_message", _poisoned_parse_message, raising=False)

    _set_worked_example_goals(db)
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    guesses = clarify.tier1_guesses("500", registry, db, config, OWNER)
    assert guesses == [("water", 500.0)]

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-poison",
        db=db, channel=channel, config=config, registry=registry,
    )
    assert db.get_log(row_id)["category"] == "water"


# ===========================================================================
# Byte-parity drift guard -- Luna's own IMPL.md flags
# `clarify._send_recovered_confirmation` as a VERBATIM MIRROR of
# `core/routing.py`'s recovered_* branching (both the inline water/
# stretch/diary special-cases in `reparse_pending_unparsed` and
# `_send_recovered_generic` for everything else), kept as a mirror rather
# than an import to avoid a clarify.py<->routing.py cycle. Archi has
# already ruled the two get consolidated into `core/confirmation.py` at
# integration -- these tests are NOT about failing the mirror itself, only
# about catching drift BEFORE that consolidation happens: both code paths
# are driven for real (routing.py's own `reparse_pending_unparsed`, not a
# hand-copied restatement of it) with identical habit/value/language
# inputs, and their outbound text is diffed.
# ===========================================================================

from habit_assistant.core import routing  # noqa: E402  (deliberately imported only in this section)
from habit_assistant.llm.ollama_client import ExtractionResult  # noqa: E402


def _make_fake_parse_message(category: str, value):
    async def _fake(text, llm, registry, threshold):
        return ExtractionResult(category=category, value=value, confidence=1.0)

    return _fake


async def _routing_recovered_text(db_, config_, registry_, user_id: str, raw: str, category: str, value) -> str:
    """Drives the REAL `core/routing.py:reparse_pending_unparsed` against
    one pending row, via a fixed fake `parse_message` (so no live Ollama
    is needed) -- returns the exact outbound confirmation text routing.py
    itself produced."""
    row_channel = FakeChannel()
    row_id = _insert_unparsed(db_, raw, unparsed_state="awaiting_llm", user_id=user_id)
    await routing.reparse_pending_unparsed(
        db_, None, row_channel, config_, registry=registry_, parse_message=_make_fake_parse_message(category, value)
    )
    assert row_id not in {r["id"] for r in db_.pending_unparsed()}  # sanity: it really got processed
    return row_channel.actionable[-1][1]


async def _clarify_recovered_text(db_, config_, registry_, user_id: str, category: str, value_str: str) -> str:
    """Drives the REAL `core/clarify.py:handle_clarify_callback` against
    one `awaiting_clarify` row for the SAME habit/value -- returns the
    exact outbound confirmation text clarify.py itself produced."""
    tap_channel = FakeChannel()
    row_id = _insert_unparsed(db_, "irrelevant for this comparison", "awaiting_clarify", user_id=user_id)
    await clarify.handle_clarify_callback(
        user_id, f"clarify:{row_id}:{category}:{value_str}", "irrelevant", "cb-parity",
        db=db_, channel=tap_channel, config=config_, registry=registry_,
    )
    return tap_channel.actionable[-1][1]


async def test_byte_parity_water(db, config, registry):
    routing_text = await _routing_recovered_text(db, config, registry, OWNER, "500", "water", 500.0)
    clarify_text = await _clarify_recovered_text(db, config, registry, OTHER, "water", "500")
    assert routing_text == clarify_text


async def test_byte_parity_stretch(db, config, registry):
    routing_text = await _routing_recovered_text(db, config, registry, OWNER, "15", "stretch", 15.0)
    clarify_text = await _clarify_recovered_text(db, config, registry, OTHER, "stretch", "15")
    assert routing_text == clarify_text


async def test_byte_parity_diary(db, config, registry, channel):
    """diary is text-typed and UNREACHABLE via a real tap
    (`handle_clarify_callback` rejects a text-habit payload outright and
    sends nothing at all -- her own `test_clarify.py::
    test_text_habit_id_is_silently_ignored_not_a_friendly_noop` already
    covers that half). So this compares `_send_recovered_confirmation`'s
    diary branch directly (calling it exactly as `handle_clarify_callback`
    would if that guard weren't in the way) against `routing.py`'s own
    real output for the same row, rather than driving the (intentionally
    unreachable) callback path."""
    routing_text = await _routing_recovered_text(db, config, registry, OWNER, "had a good day", "diary", "had a good day")

    habit = registry.get("diary")
    await clarify._send_recovered_confirmation(channel, OTHER, habit, 1.0, "en", [])
    clarify_text = channel.actionable[-1][1]

    assert routing_text == clarify_text == i18n.t("recovered_diary", "en")


async def test_byte_parity_generic_numeric_custom_habit(db, config):
    pushups = Habit(
        id="pushups", type="numeric", label_en="pushups", label_th="วิดพื้น", unit_en="reps", unit_th="ครั้ง",
        goal=50, reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, pushups)
    routing_text = await _routing_recovered_text(db, config, reg, OWNER, "20 pushups", "pushups", 20.0)
    clarify_text = await _clarify_recovered_text(db, config, reg, OTHER, "pushups", "20")
    assert routing_text == clarify_text


async def test_byte_parity_generic_duration_custom_habit(db, config):
    yoga = Habit(
        id="yoga", type="duration", label_en="yoga", label_th="โยคะ", unit_en="min", unit_th="นาที",
        goal=20, reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, yoga)
    routing_text = await _routing_recovered_text(db, config, reg, OWNER, "20 min yoga", "yoga", 20.0)
    clarify_text = await _clarify_recovered_text(db, config, reg, OTHER, "yoga", "20")
    assert routing_text == clarify_text


async def test_byte_parity_generic_boolean_custom_habit(db, config):
    meds = Habit(
        id="meds", type="boolean", label_en="meds", label_th="ยา", unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, meds)
    routing_text = await _routing_recovered_text(db, config, reg, OWNER, "took meds", "meds", True)
    clarify_text = await _clarify_recovered_text(db, config, reg, OTHER, "meds", "1")
    assert routing_text == clarify_text


async def test_byte_parity_thai_language_water(db, config, registry):
    """Same drift guard, Thai variant -- both paths must resolve the SAME
    language for the SAME (Thai) raw/source text and produce identical
    copy."""
    routing_text = await _routing_recovered_text(db, config, registry, OWNER, "น้ำ 500", "water", 500.0)
    tap_channel = FakeChannel()
    row_id = _insert_unparsed(db, "น้ำ 500", "awaiting_clarify", user_id=OTHER)
    await clarify.handle_clarify_callback(
        OTHER, f"clarify:{row_id}:water:500", "น้ำ 500", "cb-parity-th",
        db=db, channel=tap_channel, config=config, registry=registry,
    )
    clarify_text = tap_channel.actionable[-1][1]
    assert routing_text == clarify_text
    assert i18n.detect_language(routing_text) == "th"
