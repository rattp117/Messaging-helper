"""SPEC-v1.10.md "Never lose a log" -- module M1 (`core/clarify.py`) unit
tests, functional 2 (conservative tap-to-fix clarify, R5-R12): AC7 (tier1_
guesses is deterministic), AC8 (guess-offer shape/state -- the offer/state
half; the sweep-composition half lives in tests/test_unparsed_closure.py),
AC10 (a clarify tap = an ordinary log, no audit row), AC11 (the sweep-vs-
tap race guard, tap-vs-tap flavor).

Mirrors tests/test_quicklog.py's/tests/test_undo_ui.py's own conventions:
real on-disk SQLite via `tmp_path` (no DB mocks), a small local `FakeChannel`
recording both plain sends and `send_actionable` (button) calls -- `conftest.
RecordingChannel` drops buttons via `Channel.send_actionable`'s own default,
so it can't be used here where the button *shape* is itself under test.

`core/routing.py` is never imported here (SPEC-v1.10.md §11: the parallel
modules never edit/depend on it) -- every scenario below drives `core/
clarify.py`'s own droppable functions directly, exactly as Archi's dispatch
instructed ("test them via direct calls")."""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import clarify, i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner"
OTHER = "other-user"


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

    def all_sent(self) -> list[tuple[str, str]]:
        return self.sent


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="member", status="active")
    database.upsert_user(OTHER, role="member", status="active")
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
    """SPEC-v1.10.md §2.3's own worked example is stated against "the
    default registry (water goal 2000 ml, stretch goal 30 min)" -- neither
    figure is this project's actual `config.toml` default (water=2500,
    stretch=no config goal at all), so tests reproduce the SPEC's own
    illustrative numbers via a per-user `/target` override (`db.set_target`,
    the exact same mechanism `targets.effective_goal` already prefers over
    the config default for any user)."""
    db_.set_target(user_id, "water", 2000)
    db_.set_target(user_id, "stretch", 30)


def _insert_unparsed(db_: Database, raw: str, unparsed_state: str | None, user_id: str = OWNER) -> int:
    return db_.insert_log(
        LogEntry(None, user_id, "2026-08-27T10:00:00", "unparsed", None, None, raw, "reply", unparsed_state=unparsed_state)
    )


def _custom_registry(config: Config, *habits: Habit) -> HabitRegistry:
    return HabitRegistry([*HabitRegistry.from_config(config), *habits])


# ===========================================================================
# AC7 -- tier1_guesses: deterministic, zero-LLM. SPEC-v1.10.md §2.3's exact
# worked examples.
# ===========================================================================


def test_worked_example_500_guesses_water_only(db, config, registry):
    _set_worked_example_goals(db)

    guesses = clarify.tier1_guesses("500", registry, db, config, OWNER)

    assert guesses == [("water", 500.0)]


def test_worked_example_stretch_label_exact_match_yields_effective_goal(db, config, registry):
    _set_worked_example_goals(db)

    guesses = clarify.tier1_guesses("stretch", registry, db, config, OWNER)

    assert guesses == [("stretch", 30.0)]


def test_worked_example_stre_prefix_match_yields_effective_goal(db, config, registry):
    _set_worked_example_goals(db)

    guesses = clarify.tier1_guesses("stre", registry, db, config, OWNER)

    assert guesses == [("stretch", 30.0)]


def test_worked_example_streaching_typo_yields_no_guess(db, config, registry):
    """"Streaching" (10 chars) is LONGER than "stretch" (7 chars), so it
    can never be "a length>=3 prefix of" it -- §2.3's own point: a typo is
    NOT tier-1 territory (that's the out-of-scope tier-2/fuzzy matching,
    §10)."""
    _set_worked_example_goals(db)

    guesses = clarify.tier1_guesses("Streaching", registry, db, config, OWNER)

    assert guesses == []


def test_bare_number_out_of_every_habits_plausibility_window_yields_nothing(db, config, registry):
    _set_worked_example_goals(db)

    # 2_000_000 is nowhere near plausible for either water (goal*5=10000)
    # or stretch (goal*5=150).
    assert clarify.tier1_guesses("2000000", registry, db, config, OWNER) == []


def test_bare_number_case_insensitive_unit_still_resolves_and_is_not_bare(db, config, registry):
    """"500ML" resolves to water via the normal unit lookup (case-
    insensitive) -- NOT a "bare number" case, so no unit-plausibility
    guess is generated for it (the deterministic pre-parser would already
    have placed this text; tier1_guesses is a last resort)."""
    assert clarify.tier1_guesses("500ML", registry, db, config, OWNER) == []


def test_unresolvable_unit_token_is_still_treated_as_a_bare_number(db, config, registry):
    """"500 bloop" -- "bloop" resolves to no configured unit, so §2.3's
    own "or the unit token didn't resolve" clause still treats 500 as a
    bare number."""
    _set_worked_example_goals(db)

    guesses = clarify.tier1_guesses("500 bloop", registry, db, config, OWNER)

    assert guesses == [("water", 500.0)]


def test_number_present_in_text_overrides_the_effective_goal(db, config, registry):
    """§2.3: "value = the number in the text if present" -- a label match
    with an explicit number wins over the habit's own goal."""
    _set_worked_example_goals(db)

    guesses = clarify.tier1_guesses("stretch 45", registry, db, config, OWNER)

    assert guesses == [("stretch", 45.0)]


def test_label_match_with_no_number_and_no_goal_is_dropped(db, config, registry):
    """A numeric habit with NO effective goal, matched by label, with no
    number in the text -- "no derivable value" (§2.3) -> dropped."""
    custom = Habit(
        id="pushups", type="numeric", label_en="pushups", label_th="วิดพื้น",
        unit_en="reps", unit_th="ครั้ง", goal=None, reminder_times=(), reminder_text_en=None,
        reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, custom)

    assert clarify.tier1_guesses("pushups", reg, db, config, OWNER) == []


def test_boolean_label_match_with_no_number_defaults_to_one(db, config, registry):
    custom = Habit(
        id="meds", type="boolean", label_en="meds", label_th="ยา", unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, custom)

    assert clarify.tier1_guesses("meds", reg, db, config, OWNER) == [("meds", 1.0)]


def test_text_habit_never_produces_a_guess_even_on_exact_label_match(db, config, registry):
    """diary (text) is a shipped built-in -- an exact label match on
    "diary" must never surface a guess (§5's own scope: only numeric/
    duration/boolean can carry a tap-to-fix value)."""
    assert clarify.tier1_guesses("diary", registry, db, config, OWNER) == []


def test_per_user_registry_includes_a_custom_habit(db, config, registry):
    """AC7: "against the acting per-user registry (incl. a custom habit)"."""
    pushups = Habit(
        id="pushups", type="numeric", label_en="pushups", label_th="วิดพื้น",
        unit_en="reps", unit_th="ครั้ง", goal=50, reminder_times=(), reminder_text_en=None,
        reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, pushups)

    assert clarify.tier1_guesses("pushups", reg, db, config, OWNER) == [("pushups", 50.0)]
    # A DIFFERENT user's registry, without the custom habit, gets nothing.
    assert clarify.tier1_guesses("pushups", registry, db, config, OTHER) == []


def _exact_and_prefix_habits() -> tuple[Habit, Habit]:
    """Two goal-less custom numeric habits: one whose label IS "500"
    (exact match against the text "500"), one whose label STARTS WITH
    "5000..." (a length->=3 prefix match) -- both derive their guessed
    value from `_number_in_text` (the literal "500" in the input), never
    from their own (nonexistent) goal, so neither is dropped."""
    exact_habit = Habit(
        id="zzz_exact", type="numeric", label_en="500", label_th="500", unit_en="u1", unit_th="u1", goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    prefix_habit = Habit(
        id="aaa_prefix", type="numeric", label_en="5000widgets", label_th="w", unit_en="u2", unit_th="u2", goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    return exact_habit, prefix_habit


def test_exact_match_ranks_before_prefix_before_plausibility(db, config, registry):
    """R5's own ordering: exact label/alias/unit matches first, then
    prefix matches, then bare-number-plausibility guesses -- verified by
    constructing text ("500") that hits all three buckets at once: an
    exact-labeled custom habit, a prefix-labeled custom habit, and the
    built-in water's own unit-plausibility window."""
    _set_worked_example_goals(db)
    exact_habit, prefix_habit = _exact_and_prefix_habits()
    reg = _custom_registry(config, exact_habit, prefix_habit)

    guesses = clarify.tier1_guesses("500", reg, db, config, OWNER)

    assert guesses == [("zzz_exact", 500.0), ("aaa_prefix", 500.0), ("water", 500.0)]


def test_tier1_guesses_deduplicates_when_label_and_plausibility_agree(db, config, registry):
    """A label match and a bare-number-plausibility match landing on the
    SAME (habit_id, value) pair collapse into one guess, not two: a custom
    habit labeled "500" with goal=100 is both an exact label match (value
    500, the number in the text) AND unit-plausible for its own goal
    (100 * upper(5.0) == 500, inclusive)."""
    dup_habit = Habit(
        id="dup_habit", type="numeric", label_en="500", label_th="500", unit_en="u", unit_th="u", goal=100,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, dup_habit)

    guesses = clarify.tier1_guesses("500", reg, db, config, OWNER)

    assert guesses.count(("dup_habit", 500.0)) == 1


def test_tier1_guesses_capped_at_max_guesses_keeps_highest_priority(db, config, registry):
    _set_worked_example_goals(db)
    exact_habit, prefix_habit = _exact_and_prefix_habits()
    reg = _custom_registry(config, exact_habit, prefix_habit)
    small_cap = Config(clarify={"max_guesses": 1})

    guesses = clarify.tier1_guesses("500", reg, db, small_cap, OWNER)

    assert guesses == [("zzz_exact", 500.0)]  # the exact match survives the cap, not the others


# ===========================================================================
# build_guess_buttons -- §3.2 shape.
# ===========================================================================


def test_build_guess_buttons_numeric_shape_and_callback_payload():
    reg = HabitRegistry.from_config(Config())
    buttons = clarify.build_guess_buttons([("water", 500.0)], 42, reg, "en")

    assert buttons == [("\U0001F4A7 water 500ml", "clarify:42:water:500")]


def test_build_guess_buttons_boolean_has_no_unit_suffix():
    meds = Habit(
        id="meds", type="boolean", label_en="meds", label_th="ยา", unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = HabitRegistry([*HabitRegistry.from_config(Config()), meds])

    buttons = clarify.build_guess_buttons([("meds", 1.0)], 7, reg, "en")

    label, callback = buttons[0]
    assert callback == "clarify:7:meds:1"
    assert label.endswith("meds")  # no trailing "1<unit>" amount suffix, unlike a numeric/duration guess


def test_build_guess_buttons_skips_a_habit_no_longer_in_registry():
    reg = HabitRegistry.from_config(Config())
    buttons = clarify.build_guess_buttons([("ghost_habit", 5.0)], 1, reg, "en")
    assert buttons == []


def test_build_guess_buttons_fractional_amount_renders_compactly():
    reg = HabitRegistry.from_config(Config())
    buttons = clarify.build_guess_buttons([("water", 0.5)], 1, reg, "en")
    assert buttons[0][1] == "clarify:1:water:0.5"


# ===========================================================================
# offer_clarify / send_closure -- §3.1/§3.2 message shape.
# ===========================================================================


async def test_offer_clarify_sends_bilingual_message_quoting_text_and_guess_buttons(db, config, registry, channel):
    _set_worked_example_goals(db)
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.offer_clarify(channel, db, config, registry, "en", OWNER, row_id=row_id, text="500")

    chat_id, text, buttons = channel.actionable[-1]
    assert chat_id == OWNER
    assert '"500"' in text
    assert buttons == [("\U0001F4A7 water 500ml", f"clarify:{row_id}:water:500")]


async def test_offer_clarify_thai(db, config, registry, channel):
    _set_worked_example_goals(db)
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.offer_clarify(channel, db, config, registry, "th", OWNER, row_id=row_id, text="500")

    _, text, _ = channel.actionable[-1]
    assert '"500"' in text
    assert i18n.detect_language(text) == "th"


async def test_send_closure_quotes_text_and_attaches_log_keyboard(db, config, registry, channel):
    await clarify.send_closure(channel, db, config, registry, "en", OWNER, text="Streaching")

    chat_id, text, buttons = channel.actionable[-1]
    assert chat_id == OWNER
    assert '"Streaching"' in text
    assert "Nothing was logged" in text
    assert len(buttons) > 0  # default registry has water/stretch -> non-empty /log keyboard


async def test_send_closure_falls_back_to_hint_when_keyboard_is_empty(db, config, channel):
    text_only_config = Config(habits=[{"id": "diary", "type": "text", "label": {"en": "diary", "th": "ไดอรี่"}}])
    reg = HabitRegistry.from_config(text_only_config)

    await clarify.send_closure(channel, db, text_only_config, reg, "en", OWNER, text="hmm")

    # No loggable (numeric/duration/boolean) habit -> no send_actionable at
    # all, a single plain send carrying both the closure body AND the
    # friendly "nothing to quick-log yet" hint.
    assert channel.actionable == []
    chat_id, text = channel.sent[-1]
    assert chat_id == OWNER
    assert "hmm" in text
    assert i18n.t("quicklog_empty", "en") in text


# ===========================================================================
# AC10 -- a clarify tap is an ordinary log: recovered-* confirmation +
# Undo + dashboard refresh, no audit row. Unknown/foreign habit or an
# already-resolved/closed row -> friendly no-op, no write.
# ===========================================================================


async def test_winning_tap_reclassifies_and_sends_recovered_confirmation(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-1", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["category"] == "water"
    assert row["value_num"] == 500.0
    assert row["unparsed_state"] is None
    chat_id, text, buttons = channel.actionable[-1]
    assert chat_id == OWNER
    assert text == i18n.t("recovered_water", "en", water_ml=500)
    assert buttons == [(i18n.t("undo_button_label", "en"), f"undo:{row_id}")]


async def test_winning_tap_writes_no_audit_row(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-1", db=db, channel=channel, config=config, registry=registry
    )

    assert db.recent_audit(100) == []


async def test_winning_tap_refreshes_the_pinned_dashboard(db, config, registry, channel):
    db.set_dashboard_msg_id(OWNER, "pinned-1")
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-1", db=db, channel=channel, config=config, registry=registry
    )

    assert channel.edits, "dashboard.refresh should have edited the pinned message"
    assert channel.edits[-1][0] == OWNER
    assert channel.edits[-1][1] == "pinned-1"


async def test_stretch_tap_uses_recovered_stretch_copy(db, config, registry, channel):
    row_id = _insert_unparsed(db, "15", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:stretch:15", "15", "cb-2", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["category"] == "stretch" and row["value_num"] == 15.0
    assert channel.actionable[-1][1] == i18n.t("recovered_stretch", "en", stretch_min=15)


async def test_custom_numeric_tap_uses_recovered_numeric_copy(db, config, registry, channel):
    pushups = Habit(
        id="pushups", type="numeric", label_en="pushups", label_th="วิดพื้น",
        unit_en="reps", unit_th="ครั้ง", goal=50, reminder_times=(), reminder_text_en=None,
        reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, pushups)
    row_id = _insert_unparsed(db, "pushups 20", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:pushups:20", "pushups 20", "cb-3", db=db, channel=channel, config=config, registry=reg
    )

    row = db.get_log(row_id)
    assert row["category"] == "pushups" and row["value_num"] == 20.0
    assert channel.actionable[-1][1] == i18n.t("recovered_numeric", "en", value=20.0, unit="reps", label="pushups")


async def test_boolean_tap_always_confirms_with_value_one(db, config, registry, channel):
    meds = Habit(
        id="meds", type="boolean", label_en="meds", label_th="ยา", unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, meds)
    row_id = _insert_unparsed(db, "meds", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:meds:1", "meds", "cb-4", db=db, channel=channel, config=config, registry=reg
    )

    row = db.get_log(row_id)
    assert row["category"] == "meds" and row["value_num"] == 1.0
    assert channel.actionable[-1][1] == i18n.t("recovered_boolean", "en", label="meds")


async def test_unknown_habit_id_is_a_friendly_noop_no_write(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:not_a_real_habit:5", "500", "cb-5", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed"  # untouched
    assert row["unparsed_state"] == "awaiting_clarify"
    assert channel.sent[-1] == (OWNER, i18n.t("quicklog_unknown_habit", "en"))
    assert channel.actionable == []


async def test_foreign_users_custom_habit_is_unknown_to_this_registry(db, config, registry, channel):
    """R9: resolved against the TAPPING user's OWN registry only -- a habit
    id belonging to a different user simply isn't present in `registry`
    (the caller scopes it), so it hits the exact same "unknown" no-op."""
    other_users_habit_id = "someone_elses_habit"
    row_id = _insert_unparsed(db, "5", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:{other_users_habit_id}:5", "5", "cb-6", db=db, channel=channel, config=config, registry=registry
    )

    assert channel.sent[-1] == (OWNER, i18n.t("quicklog_unknown_habit", "en"))


async def test_already_resolved_row_is_a_friendly_noop_no_second_write(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")
    won = db.resolve_unparsed(
        row_id, from_states=("awaiting_clarify",), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )
    assert won is True  # pre-resolved, simulating a stale button

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:999", "500", "cb-7", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["value_num"] == 500.0  # the stale tap's value never applied
    assert channel.sent[-1] == (OWNER, i18n.t("clarify_already_handled", "en"))
    assert channel.actionable == []


async def test_closed_row_is_a_friendly_noop_no_write(db, config, registry, channel):
    """A closed row has no `clarify:` button attached in the first place
    (§3.1's closure keyboard is `/log`, not a `clarify:` payload) -- this
    covers a forged/replayed payload naming one anyway."""
    row_id = _insert_unparsed(db, "Streaching", "closed")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-8", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed" and row["unparsed_state"] == "closed"
    assert channel.sent[-1] == (OWNER, i18n.t("clarify_already_handled", "en"))


async def test_text_habit_id_is_silently_ignored_not_a_friendly_noop(db, config, registry, channel):
    row_id = _insert_unparsed(db, "some diary text", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:diary:5", "some diary text", "cb-9", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed"  # untouched
    assert channel.sent == []  # no reply at all -- "no legitimate origin" bucket


@pytest.mark.parametrize(
    "data",
    [
        "clarify:1:water",  # missing value
        "clarify:1:water:500:extra",
        "clarify:x:water:500",  # non-numeric row id
        "clarify:1:WATER:500",  # uppercase habit id (grammar is lowercase-only)
        "clarify:1:water:abc",  # non-numeric value
        "clarify:1:water:๕๐๐",  # non-ASCII (Thai) digits
        "log:1:water:500",  # wrong prefix entirely
    ],
)
async def test_malformed_payload_is_silently_ignored(db, config, registry, channel, data):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, data, "500", "cb-10", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["unparsed_state"] == "awaiting_clarify"  # untouched
    assert channel.sent == []


async def test_out_of_range_row_id_is_silently_ignored(db, config, registry, channel):
    await clarify.handle_clarify_callback(
        OWNER, "clarify:99999999999999999999999999999:water:500", "500", "cb-11",
        db=db, channel=channel, config=config, registry=registry,
    )
    assert channel.sent == []


async def test_out_of_range_value_is_silently_ignored(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:9999999999", "500", "cb-12", db=db, channel=channel, config=config, registry=registry
    )

    assert db.get_log(row_id)["unparsed_state"] == "awaiting_clarify"
    assert channel.sent == []


async def test_nonpositive_numeric_value_is_silently_ignored(db, config, registry, channel):
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:0", "500", "cb-13", db=db, channel=channel, config=config, registry=registry
    )
    assert db.get_log(row_id)["unparsed_state"] == "awaiting_clarify"
    assert channel.sent == []


async def test_boolean_with_wrong_value_is_silently_ignored(db, config, registry, channel):
    meds = Habit(
        id="meds", type="boolean", label_en="meds", label_th="ยา", unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    reg = _custom_registry(config, meds)
    row_id = _insert_unparsed(db, "meds", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:meds:5", "meds", "cb-14", db=db, channel=channel, config=config, registry=reg
    )
    assert db.get_log(row_id)["unparsed_state"] == "awaiting_clarify"
    assert channel.sent == []


# ===========================================================================
# AC11 -- the sweep-vs-tap race guard (tap-vs-tap flavor; the sweep-vs-tap
# flavor and the single-flight-sweep flavor live in
# tests/test_unparsed_closure.py, alongside the sweep simulation itself).
# ===========================================================================


async def test_tap_vs_tap_same_row_exactly_one_winner(db, config, registry, channel):
    """Two taps racing the SAME row (e.g. two different buttons tapped in
    quick succession, or the same button tapped twice) -- the CAS
    (`resolve_unparsed(from_states=('awaiting_clarify',), ...)`) lets only
    the first through; the second observes rowcount 0 and sends only the
    friendly no-op, never a second log/confirmation/dashboard-refresh."""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-a", db=db, channel=channel, config=config, registry=registry
    )
    first_actionable_count = len(channel.actionable)
    row_after_first = db.get_log(row_id)

    # A second, different guess button on the SAME row (simulates a
    # near-simultaneous second tap on the original offer message).
    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:stretch:30", "500", "cb-b", db=db, channel=channel, config=config, registry=registry
    )

    assert row_after_first["category"] == "water"
    assert db.get_log(row_id)["category"] == "water"  # the second tap never applied
    assert len(channel.actionable) == first_actionable_count  # no second confirmation
    assert channel.sent[-1] == (OWNER, i18n.t("clarify_already_handled", "en"))


async def test_cas_from_state_discipline_tap_cannot_jump_ahead_of_the_sweep(db, config, registry, channel):
    """A tap payload naming a row that is still `awaiting_llm` (never
    offered a guess yet -- the sweep hasn't gotten to it) must not resolve
    it: the tap's CAS guards on `('awaiting_clarify',)` only, a disjoint
    origin from the sweep's own `(None, 'awaiting_llm')` (R11's own
    precondition)."""
    row_id = _insert_unparsed(db, "500", "awaiting_llm")

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-c", db=db, channel=channel, config=config, registry=registry
    )

    row = db.get_log(row_id)
    assert row["category"] == "unparsed"
    assert row["unparsed_state"] == "awaiting_llm"  # untouched
    assert channel.sent[-1] == (OWNER, i18n.t("clarify_already_handled", "en"))


async def test_tap_on_a_row_already_reclassified_by_the_sweep_is_a_noop(db, config, registry, channel):
    """Simulates R11's own "sweep-vs-tap" scenario the other way around:
    the row was `awaiting_clarify`, a genuine tap already reclassified it
    (mirrors what a real prior `handle_clarify_callback` call would have
    done) -- a second, stale tap on the same old message must not double-
    log."""
    row_id = _insert_unparsed(db, "500", "awaiting_clarify")
    won = db.resolve_unparsed(
        row_id, from_states=("awaiting_clarify",), category="water", value_num=500.0, value_text=None, habit_type="numeric"
    )
    assert won is True

    await clarify.handle_clarify_callback(
        OWNER, f"clarify:{row_id}:water:500", "500", "cb-d", db=db, channel=channel, config=config, registry=registry
    )

    assert channel.actionable == []  # no second recovered-style confirmation
    assert channel.sent[-1] == (OWNER, i18n.t("clarify_already_handled", "en"))


# ===========================================================================
# Structural zero-LLM proof (mirrors tests/test_backfill.py's/tests/
# test_preparse.py's own "structural zero-LLM proof" group): this module
# must never import or reference the LLM client -- every guess and every
# state transition here is deterministic.
# ===========================================================================


def test_module_imports_no_llm_client():
    source = inspect.getsource(clarify)
    for forbidden in ("ollama_client", "OllamaClient", "parse_message", "llm.parser"):
        assert forbidden not in source


def test_module_does_not_import_routing():
    """Would cycle -- `core/routing.py` is what imports `core/clarify.py`
    at the later integration pass (§11), never the reverse."""
    source = inspect.getsource(clarify)
    assert "core.routing" not in source
    assert "core import routing" not in source
    assert "from habit_assistant.core.routing" not in source
