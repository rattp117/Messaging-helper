"""SPEC-v1.10.md "Never lose a log" -- module M1, functional 1 (unparsed
closure with a terminal state, R1-R4): AC5 (the zombie re-parse loop is
killed), AC6 (the closure notification fires exactly once, ever), and AC8's
sweep-side half (a recovery-fail row WITH guesses moves to `awaiting_clarify`
and is excluded from every later sweep).

`core/routing.py:reparse_pending_unparsed` is the module that will actually
call into `core/clarify.py` (the sequential integration seam, SPEC-v1.10.md
§11) -- it isn't wired yet (M1 never touches `routing.py`, per the parallel-
module file-ownership split). Every test below instead plays the exact
sequence that seam will run, using ONLY the droppable building blocks this
module owns plus the already-built shared-surface CAS methods
(`db.mark_unparsed_state`/`db.resolve_unparsed`, `db.pending_unparsed`):

    guesses = clarify.tier1_guesses(text, registry, db, config, user_id)
    if not guesses:
        won = db.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state=clarify.CLOSED)
        if won:
            await clarify.send_closure(channel, db, config, registry, lang, user_id, text=text)
    else:
        won = db.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state=clarify.AWAITING_CLARIFY)
        if won:
            await clarify.offer_clarify(channel, db, config, registry, lang, user_id, row_id=row_id, text=text)

This is exactly Archi's dispatch instruction ("test them via direct calls")
and mirrors this module's own `offer_clarify`/`send_closure` docstrings
("the caller has already won the CAS before calling this").

No LLM anywhere in this file -- `db.pending_unparsed()`/the CAS methods and
`clarify.tier1_guesses` are all that's needed to prove a row never gets
re-parsed; there is nothing here for an LLM fake to even serve.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import clarify, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actionable: list[tuple[str, str, list[tuple[str, str]]]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        self.sent.append((chat_id, text))
        return None

    async def send_actionable(self, chat_id: str, text: str, buttons: list[tuple[str, str]]) -> None:
        self.actionable.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="member", status="active")
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


def _insert_unparsed(db_: Database, raw: str, unparsed_state: str | None = None, user_id: str = OWNER) -> int:
    return db_.insert_log(
        LogEntry(None, user_id, "2026-08-25T10:00:00", "unparsed", None, None, raw, "reply", unparsed_state=unparsed_state)
    )


async def _run_one_sweep_pass(channel, db_, config, registry, user_id, row_id: int, text: str, lang: i18n.Language = "en") -> str:
    """Plays exactly one "recovery sweep, row still can't parse" pass for
    ONE row -- see this file's own module docstring for why this, not a
    call into `core/routing.py`, is the right level to test at. Returns
    which branch fired: "closed", "offered", or "raced" (CAS lost, e.g. a
    second concurrent sweep on the same row)."""
    guesses = clarify.tier1_guesses(text, registry, db_, config, user_id)
    if not guesses:
        won = db_.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state=clarify.CLOSED)
        if not won:
            return "raced"
        await clarify.send_closure(channel, db_, config, registry, lang, user_id, text=text)
        return "closed"

    won = db_.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state=clarify.AWAITING_CLARIFY)
    if not won:
        return "raced"
    await clarify.offer_clarify(channel, db_, config, registry, lang, user_id, row_id=row_id, text=text)
    return "offered"


def _set_worked_example_goals(db_: Database, user_id: str = OWNER) -> None:
    db_.set_target(user_id, "water", 2000)
    db_.set_target(user_id, "stretch", 30)


# ===========================================================================
# AC5 -- the zombie re-parse loop is killed, for both "500" (has a guess ->
# awaiting_clarify) and "Streaching" (no guess -> closed). Either terminal
# outcome permanently removes the row from `pending_unparsed()`.
# ===========================================================================


async def test_streaching_zombie_row_is_closed_and_leaves_the_pending_pool(db, config, registry, channel):
    """The exact production shape SPEC-v1.10.md §1 describes: a legacy
    zombie row (id=14-style, `unparsed_state IS NULL`) whose text still
    can't be placed after a recovery attempt."""
    row_id = _insert_unparsed(db, "Streaching", unparsed_state=None)
    assert row_id in {r["id"] for r in db.pending_unparsed()}

    outcome = await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "Streaching")

    assert outcome == "closed"
    row = db.get_log(row_id)
    assert row["category"] == "unparsed"
    assert row["unparsed_state"] == "closed"
    assert row_id not in {r["id"] for r in db.pending_unparsed()}


async def test_500_zombie_row_with_a_guess_becomes_awaiting_clarify_and_leaves_the_pending_pool(
    db, config, registry, channel
):
    _set_worked_example_goals(db)
    row_id = _insert_unparsed(db, "500", unparsed_state="awaiting_llm")
    assert row_id in {r["id"] for r in db.pending_unparsed()}

    outcome = await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "500")

    assert outcome == "offered"
    row = db.get_log(row_id)
    assert row["category"] == "unparsed"  # not reclassified -- only OFFERED, R8
    assert row["unparsed_state"] == "awaiting_clarify"
    assert row_id not in {r["id"] for r in db.pending_unparsed()}


async def test_a_second_recovery_sweep_never_touches_either_terminal_row_no_llm_call(db, config, registry, channel):
    """R2/R8: once a row is `closed` or `awaiting_clarify`, `pending_
    unparsed()` never returns it again -- so a LATER sweep's own `for row
    in db.pending_unparsed(): ...` loop (the real `reparse_pending_
    unparsed`'s shape) would never even reach it a second time, let alone
    spend a second LLM call on it. Proven here at the DB layer directly
    (the strongest possible proof: it is IMPOSSIBLE to observe this row
    again through the one query the real sweep uses to find its work)."""
    _set_worked_example_goals(db)
    streaching_id = _insert_unparsed(db, "Streaching", unparsed_state=None)
    guess_id = _insert_unparsed(db, "500", unparsed_state="awaiting_llm")

    await _run_one_sweep_pass(channel, db, config, registry, OWNER, streaching_id, "Streaching")
    await _run_one_sweep_pass(channel, db, config, registry, OWNER, guess_id, "500")

    still_pending = {r["id"] for r in db.pending_unparsed()}
    assert streaching_id not in still_pending
    assert guess_id not in still_pending

    # A THIRD, later sweep queries pending_unparsed() first (exactly like
    # `reparse_pending_unparsed`'s own `for row in db.pending_unparsed()`)
    # -- neither row is even a candidate to re-parse.
    assert db.pending_unparsed() == []


# ===========================================================================
# AC6 -- the closure notification: exactly once, ever; quotes the raw text;
# carries the /log keyboard.
# ===========================================================================


async def test_closure_sent_exactly_once_even_across_repeated_sweep_attempts(db, config, registry, channel):
    row_id = _insert_unparsed(db, "Streaching", unparsed_state=None)

    first = await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "Streaching")
    assert first == "closed"
    assert len(channel.actionable) == 1

    # A second, later "sweep" re-checks pending_unparsed() first (the real
    # shape) and finds nothing -- but even a NAIVE direct re-attempt at the
    # CAS (defense in depth, simulating two overlapping sweeps that both
    # already had this row in their in-flight snapshot) is a pure no-op:
    # rowcount 0, no second send.
    raced = db.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state=clarify.CLOSED)
    assert raced is False
    assert len(channel.actionable) == 1  # still exactly one notification, ever


async def test_closure_notification_quotes_the_exact_raw_text(db, config, registry, channel):
    row_id = _insert_unparsed(db, "Streaching", unparsed_state=None)

    await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "Streaching")

    _, text, _ = channel.actionable[-1]
    assert '"Streaching"' in text


async def test_closure_notification_is_bilingual(db, config, registry, channel):
    en_row = _insert_unparsed(db, "Streaching", unparsed_state=None)
    await _run_one_sweep_pass(channel, db, config, registry, OWNER, en_row, "Streaching", lang="en")
    en_text = channel.actionable[-1][1]

    th_row = _insert_unparsed(db, "Streaching", unparsed_state=None)
    await _run_one_sweep_pass(channel, db, config, registry, OWNER, th_row, "Streaching", lang="th")
    th_text = channel.actionable[-1][1]

    assert en_text != th_text
    assert i18n.detect_language(en_text) == "en"
    assert i18n.detect_language(th_text) == "th"


async def test_closure_notification_carries_the_log_keyboard(db, config, registry, channel):
    row_id = _insert_unparsed(db, "Streaching", unparsed_state=None)

    await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "Streaching")

    _, _, buttons = channel.actionable[-1]
    assert len(buttons) > 0  # default registry (water/stretch) -> a non-empty /log keyboard


# ===========================================================================
# AC8 (sweep-side half) -- a recovery-fail row WITH guesses is offered, not
# closed, and the LLM is never retried on it thereafter.
# ===========================================================================


async def test_guess_offer_row_is_excluded_from_every_later_sweep_llm_never_retried(db, config, registry, channel):
    _set_worked_example_goals(db)
    row_id = _insert_unparsed(db, "500", unparsed_state="awaiting_llm")

    await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "500")
    assert db.get_log(row_id)["unparsed_state"] == "awaiting_clarify"

    # A later sweep's own query -- the row simply isn't there to re-parse.
    assert row_id not in {r["id"] for r in db.pending_unparsed()}
    # And even a direct, naive CAS attempt from the sweep's own origin set
    # fails outright -- the row is no longer `NULL`/`awaiting_llm`.
    assert db.mark_unparsed_state(row_id, from_states=(None, "awaiting_llm"), to_state=clarify.CLOSED) is False


async def test_offer_buttons_match_tier1_guesses_for_the_same_text(db, config, registry, channel):
    _set_worked_example_goals(db)
    row_id = _insert_unparsed(db, "500", unparsed_state="awaiting_llm")

    await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "500")

    expected = clarify.build_guess_buttons(
        clarify.tier1_guesses("500", registry, db, config, OWNER), row_id, registry, "en"
    )
    _, _, buttons = channel.actionable[-1]
    assert buttons == expected == [("\U0001F4A7 water 500ml", f"clarify:{row_id}:water:500")]


async def test_two_concurrently_triggered_sweeps_do_not_co_process_the_same_row(db, config, registry, channel):
    """AC11's own "two concurrently-triggered sweeps" clause, at the
    building-block level this module owns: two overlapping sweep passes
    both see the row in their (stale) snapshot and both attempt the SAME
    CAS transition -- only the first commits."""
    row_id = _insert_unparsed(db, "Streaching", unparsed_state=None)

    first = await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "Streaching")
    second = await _run_one_sweep_pass(channel, db, config, registry, OWNER, row_id, "Streaching")

    assert first == "closed"
    assert second == "raced"
    assert len(channel.actionable) == 1  # only the winner ever sent anything
