"""Vera's adversarial gap suite for the v1.5.0 `checkins` module (SPEC-v1.5.md
§4 Feature 1 + R-D5), on top of Luna's own `tests/test_checkins.py` (62) and
`tests/test_dnd.py` (21). Scope: the checkins-owned ACs (AC-3, AC-4, AC-5,
AC-6, AC-7, AC-8, AC-9, AC-13 per SPEC-v1.5.md §11).

These probe angles Luna's own suite doesn't cover: opt-in holding absolutely
across a full simulated day with many users, window-boundary edge cases
(top-of-hour-only, midnight-crossing, degenerate start==end), a wider
`/checkin` garbage/adversarial corpus, a full collision sweep against every
other `CommandKind`, R-K8's "stored literal survives a later config default
change" guarantee, target-override vs config-default interaction for the
all-goals-met skip, live-total accuracy across ticks, check-in/reminder-tick
independence, `/dnd`'s audit-row indistinguishability from `/quiet`, and an
isolation stress test across several users in mixed states in one tick.

Same conventions as `tests/test_checkins.py`/`tests/test_dnd.py`: a real
on-disk SQLite `Database` (tmp_path, never `data\\habits.db`), a `FakeChannel`
in place of a real Telegram channel, an injectable `clock`, no LLM anywhere
in this module's own call graph (AC-4 is structural)."""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, i18n
from habit_assistant.core.audit import ACTIONS
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.preferences import execute_quiet
from habit_assistant.core.reminders import run_due_reminders
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError

    def sent_to(self, chat_id: str) -> list[str]:
        return [text for cid, text in self.sent if cid == chat_id]


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str = "test",
    label_th: str = "ทดสอบ",
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    goal: float | None = None,
    reminder_times: tuple[str, ...] = (),
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en,
        label_th=label_th,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=reminder_times,
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "checkins_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _fixed_clock(y=2026, m=8, d=23, hh=9, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


async def _enable(db, config, user_id: str, window: str | None = None) -> None:
    tail = "on" if window is None else window
    await checkins.execute_checkin(
        commands.dispatch(f"/checkin {tail}", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
    )


# ===========================================================================
# Opt-in holds absolutely (AC-8) -- full simulated day, many users, nobody
# enabled; explicit NULL-column (pre-v1.5-shaped) check.
# ===========================================================================


async def test_gap_opt_in_holds_absolutely_across_a_full_day_many_users_none_enabled(db, config):
    """AC-8: not one send, for any of five active users (owner included),
    at any hour/quarter-hour of a full simulated day, when nobody has ever
    run `/checkin on`. Each user's `checkin_window` is confirmed NULL first
    -- the exact shape migration 008 leaves every pre-v1.5 (and brand new)
    user in, by construction (no backfill)."""
    extra_users = ["u-extra-1", "u-extra-2", "u-extra-3"]
    for u in extra_users:
        db.upsert_user(u, role="member", status="active")
    all_users = [OWNER, MEMBER, *extra_users]
    for u in all_users:
        db.insert_log(LogEntry(None, u, "2026-08-23T07:00:00", "water", 100.0, None, "100ml", "reply"))
        assert db.get_checkin_window(u) is None  # pre-v1.5-shaped: never set, no backfill

    channel = FakeChannel()
    for hh in range(24):
        for mm in (0, 15, 30, 45):
            await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=hh, mm=mm))
    assert channel.sent == []


# ===========================================================================
# Window semantics -- top-of-hour-only, midnight-crossing, degenerate window.
# ===========================================================================


async def test_gap_no_fire_at_half_past_the_hour(db, config):
    """AC-3: on-the-hour is `:00` only -- `:30` (not just `:01`/`:59`,
    already covered by Luna) must also be silent."""
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 100.0, None, "100ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=30))
    assert channel.sent_to(OWNER) == []


async def test_gap_midnight_crossing_window_never_fires_any_hour_of_the_day(db, config):
    """R-K2's own explicit firing formula is a plain `start <= HH:00 <=
    end` string comparison with NO midnight-wraparound handling (unlike
    `/quiet`'s `_in_quiet_hours`) -- IMPL-v1.5-checkins.md's own "Known
    limitations" #3 documents this as a deliberate, spec-compliant gap (no
    AC requires check-in windows to cross midnight, and R-K2 gives no
    wraparound formula). This proves the documented behavior end-to-end:
    `execute_checkin`'s shape-only validation happily stores a
    start-after-end window, and it then LITERALLY NEVER FIRES, at any hour
    of the day -- not even the hours nominally "inside" 22:00-06:00."""
    command = commands.dispatch("/checkin 22:00-06:00", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=OWNER)
    assert db.get_checkin_window(OWNER) == "22:00-06:00"  # stored -- no ordering check at write time
    assert reply == i18n.t("checkin_set_window", "en", start="22:00", end="06:00")

    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 100.0, None, "100ml", "reply"))
    channel = FakeChannel()
    for hh in range(24):
        await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=hh, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_gap_reversed_default_style_window_20_00_to_08_00_never_fires(db, config):
    """Same gap as above, using the spec's own default-window numbers
    reversed (20:00-08:00, the natural "evening to morning" DND-style
    phrasing a user might reasonably try for check-ins) -- still start >
    end as a plain string compare, still never fires."""
    await _enable(db, config, OWNER, window="20:00-08:00")
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 100.0, None, "100ml", "reply"))

    channel = FakeChannel()
    for hh in range(24):
        await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=hh, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_gap_degenerate_window_start_equals_end_fires_only_at_that_single_hour(db, config):
    """R-K2's formula `start <= HH:00 <= end` is satisfied only when both
    bounds equal the current hour for a degenerate `08:00-08:00` window --
    fires exactly at 08:00, nowhere else."""
    await _enable(db, config, OWNER, window="08:00-08:00")
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T05:00:00", "water", 100.0, None, "100ml", "reply"))

    at_hour = FakeChannel()
    await checkins.run_due_checkins(at_hour, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=8, mm=0))
    assert at_hour.sent_to(OWNER) != []

    before = FakeChannel()
    await checkins.run_due_checkins(before, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=7, mm=0))
    assert before.sent_to(OWNER) == []

    after = FakeChannel()
    await checkins.run_due_checkins(after, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    assert after.sent_to(OWNER) == []


# ===========================================================================
# /checkin garbage/adversarial shapes beyond Luna's own corpus.
# ===========================================================================


CHECKIN_GARBAGE_WINDOWS = [
    "maybe",
    "8-20",
    "08:00–20:00",  # en dash (U+2013) instead of an ASCII hyphen
    "25:00-20:00",
]


@pytest.mark.parametrize("bad_tail", CHECKIN_GARBAGE_WINDOWS)
async def test_gap_checkin_garbage_shapes_report_usage_and_write_nothing(db, config, bad_tail):
    command = commands.dispatch(f"/checkin {bad_tail}", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)
    assert db.get_checkin_window(MEMBER) is None
    assert reply == i18n.t("checkin_usage", "en")


CHECKIN_TH_ADVERSARIAL_EXTRA = [
    "เช็คอินๆ",  # mai yamok (repetition mark), glued -- no space after the trigger
    "เช็คอิน ค่ะ",  # polite particle tail -- not on/off/default/window shape
    "เช็คอิน on ครับ",  # a valid prefix PLUS a trailing particle -- whole tail invalid
    "เช็คอิน on\nครับ",  # a newline mid-message breaks the whole-message anchor
]


@pytest.mark.parametrize("message", CHECKIN_TH_ADVERSARIAL_EXTRA)
def test_gap_checkin_thai_adversarial_extra_corpus_returns_none(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


DND_ADVERSARIAL_EXTRA = [
    "งดรบกวนก่อนนะ",  # glued continuation with a trailing particle, no space
    "อย่ารบกวนฉันเลย",  # ordinary Thai prose containing "รบกวน" but not the trigger word
]


@pytest.mark.parametrize("message", DND_ADVERSARIAL_EXTRA)
def test_gap_dnd_thai_adversarial_extra_corpus_returns_none(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


# ===========================================================================
# Collision sweep vs every other CommandKind, both directions.
# ===========================================================================

OTHER_KIND_TRIGGERS = [
    ("undo", "/undo"),
    ("snooze", "snooze"),
    ("query", "how much water today?"),
    ("target", "/target water 2000"),
    ("help", "/help"),
    ("habits", "/habits"),
    ("start", "/start"),
    ("approve", "/approve 123"),
    ("block", "/block 123"),
    ("users", "/users"),
    ("invite", "/invite 123"),
    ("lang", "/lang th"),
    ("quiet", "/quiet 22:00-07:00"),
    ("remind", "/remind water 09:00"),
    ("audit", "/audit"),
    ("history", "/history"),
]


@pytest.mark.parametrize("expected_kind,text", OTHER_KIND_TRIGGERS)
def test_gap_other_kinds_are_never_shadowed_by_checkin_or_dnd_matchers(expected_kind, text):
    """Direction 1: every other command kind's own canonical trigger still
    dispatches to ITS OWN kind -- `_match_checkin`/`_match_dnd` (wired in
    right after `_match_quiet` in `dispatch()`) never intercepts it."""
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == expected_kind


CHECKIN_DND_TRIGGERS = [
    ("checkin", "/checkin on"),
    ("checkin", "เช็คอิน off"),
    ("quiet", "/dnd 22:00-07:00"),
    ("quiet", "งดรบกวน off"),
]


@pytest.mark.parametrize("expected_kind,text", CHECKIN_DND_TRIGGERS)
def test_gap_checkin_and_dnd_triggers_are_never_shadowed_by_other_matchers(expected_kind, text):
    """Direction 2: `/checkin`/`เช็คอิน`/`/dnd`/`งดรบกวน` reliably reach
    their own kind and aren't accidentally caught by an earlier matcher in
    `dispatch()`'s own recognizer chain."""
    command = commands.dispatch(text, DEFAULT_REGISTRY)
    assert command is not None
    assert command.kind == expected_kind


# ===========================================================================
# R-K8: /checkin on's stored literal window is immune to a later config
# default change.
# ===========================================================================


async def test_gap_on_stores_literal_window_immune_to_later_config_default_change(db, config):
    await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=OWNER
    )
    assert db.get_checkin_window(OWNER) == "08:00-20:00"

    changed_config = Config.model_validate({"checkin": {"enabled": True, "window": "06:00-22:00"}})

    # effective_checkin still resolves the OLD literal, not the new default.
    enabled, window = checkins.effective_checkin(db, changed_config, OWNER)
    assert enabled is True
    assert window == ("08:00", "20:00")

    db.insert_log(LogEntry(None, OWNER, "2026-08-23T05:00:00", "water", 100.0, None, "100ml", "reply"))

    # 07:00 is INSIDE the new config default (06:00-22:00) but OUTSIDE the
    # user's own stored literal (08:00-20:00) -- must NOT fire.
    at_7 = FakeChannel()
    await checkins.run_due_checkins(at_7, changed_config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=7, mm=0))
    assert at_7.sent_to(OWNER) == []

    # 08:00 is inside both -- the stored literal's own start -- must fire.
    at_8 = FakeChannel()
    await checkins.run_due_checkins(at_8, changed_config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=8, mm=0))
    assert at_8.sent_to(OWNER) != []


# ===========================================================================
# Content correctness -- mixed goal/goal-less habits, target-override vs
# config-default, live totals across ticks.
# ===========================================================================


def test_gap_mixed_goal_and_goal_less_habits_skip_uses_only_the_goal_bearing_set(db, config):
    """R-K3, using the real default registry's own natural mix: `water`
    (numeric, goal-bearing) + `stretch` (duration, no config goal) +
    `diary` (text, never goal-able). Meeting `water` alone must skip the
    whole check-in -- `stretch`/`diary` being goal-less must not force a
    generic nudge instead, and must not block the skip."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 3000.0, None, "3000ml", "reply"))
    message = checkins.build_checkin_message(db, config, DEFAULT_REGISTRY, "en", OWNER, clock=_fixed_clock())
    assert message is None


def test_gap_target_override_lower_than_config_default_causes_skip_via_override_not_config(db, config):
    """R-K3 + R-T3: `targets.effective_goal` reads a user's DB override
    FIRST -- an override of 500ml is met by a 600ml log even though the
    config default (2500ml) would call it unmet. The skip must follow the
    override, not the config default."""
    db.set_target(OWNER, "water", 500.0)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 600.0, None, "600ml", "reply"))

    message = checkins.build_checkin_message(db, config, DEFAULT_REGISTRY, "en", OWNER, clock=_fixed_clock())
    assert message is None


def test_gap_target_override_higher_than_config_default_keeps_it_unmet_using_override_goal(db, config):
    """The inverse: an override of 5000ml is NOT met by 3000ml, even
    though the config default (2500ml) would already be satisfied -- the
    progress line must show the override's own goal (5000), not the
    config default's (2500)."""
    db.set_target(OWNER, "water", 5000.0)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 3000.0, None, "3000ml", "reply"))

    message = checkins.build_checkin_message(db, config, DEFAULT_REGISTRY, "en", OWNER, clock=_fixed_clock())
    assert message == "\n".join(
        [
            i18n.t("checkin_header", "en"),
            i18n.t("checkin_line_progress", "en", label="water", total=3000.0, goal=5000.0, unit="ml"),
            i18n.t("checkin_invite", "en"),
        ]
    )


async def test_gap_progress_numbers_reflect_live_totals_at_each_firing_moment(db, config):
    """AC-3/AC-4's own implied "at firing time" freshness: two ticks an
    hour apart, with a log inserted between them, must each report the
    CURRENT day-to-date total -- not a value cached from the first tick."""
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T08:30:00", "water", 500.0, None, "500ml", "reply"))

    first_tick = FakeChannel()
    await checkins.run_due_checkins(first_tick, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    first_sent = first_tick.sent_to(OWNER)
    assert len(first_sent) == 1
    assert "500" in first_sent[0]
    assert "1200" not in first_sent[0]

    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:30:00", "water", 700.0, None, "700ml", "reply"))  # total now 1200

    second_tick = FakeChannel()
    await checkins.run_due_checkins(second_tick, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=10, mm=0))
    second_sent = second_tick.sent_to(OWNER)
    assert len(second_sent) == 1
    assert "1200" in second_sent[0]


# ===========================================================================
# Suppression interplay -- check-in tick vs. per-habit reminder tick;
# /dnd's audit-row indistinguishability from /quiet.
# ===========================================================================


async def test_gap_checkin_and_reminder_tick_both_fire_independently_same_minute(db, config):
    """SPEC-v1.5.md §2.3 marks the per-habit reminder tick "unchanged" and
    R-K5 says check-ins have no suppression relationship with reminders
    (only snooze/DND are addressed). Prove the two ticks genuinely don't
    collide or dedupe when both are due for the same user at the same
    minute -- both fire, giving that user two separate messages."""
    registry = HabitRegistry(
        [_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.", reminder_times=("09:00",))]
    )
    await _enable(db, config, OWNER)

    channel = FakeChannel()
    await run_due_reminders(channel, config, registry, db, clock=_fixed_clock(hh=9, mm=0))
    await checkins.run_due_checkins(channel, config, registry, db, clock=_fixed_clock(hh=9, mm=0))

    assert len(channel.sent_to(OWNER)) == 2


async def test_gap_dnd_alias_audit_row_indistinguishable_from_quiet_set(db, config):
    """R-D5: `/dnd` must be a truly pure alias all the way down to the
    audit trail (SPEC-v1.5.md §1's own "no parallel storage/mechanism").
    There is no "dnd_set" in the closed `audit.Action` vocabulary at all
    -- a `/dnd` write records the exact same `quiet_set` action, same
    `source="command"`, same stringified `new_value` shape a `/quiet` call
    with the equivalent window would produce."""
    await execute_quiet(commands.dispatch("/dnd 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)
    dnd_row = db.recent_audit(1)[0]

    await execute_quiet(commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    quiet_row = db.recent_audit(1)[0]

    assert dnd_row["action"] == "quiet_set"
    assert dnd_row["action"] == quiet_row["action"]
    assert dnd_row["new_value"] == quiet_row["new_value"]
    assert dnd_row["source"] == quiet_row["source"] == "command"
    assert "dnd_set" not in ACTIONS
    assert "dnd_off" not in ACTIONS


async def test_gap_dnd_off_audit_row_indistinguishable_from_quiet_off(db, config):
    await execute_quiet(commands.dispatch("/dnd 22:00-07:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)
    await execute_quiet(commands.dispatch("/dnd off", DEFAULT_REGISTRY), db=db, lang="en", user_id=MEMBER)
    dnd_off_row = db.recent_audit(1)[0]

    await execute_quiet(commands.dispatch("/quiet 09:00-10:00", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    await execute_quiet(commands.dispatch("/quiet off", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    quiet_off_row = db.recent_audit(1)[0]

    assert dnd_off_row["action"] == "quiet_off"
    assert dnd_off_row["action"] == quiet_off_row["action"]
    assert dnd_off_row["new_value"] == quiet_off_row["new_value"]


# ===========================================================================
# Isolation stress -- several users in mixed enablement/window/DND states,
# one shared tick.
# ===========================================================================


async def test_gap_isolation_stress_many_users_mixed_states_single_tick(db, config):
    """AC-9, stress form: four extra users in four different states, plus
    OWNER/MEMBER (never enabled by this test) -- exactly ONE of the six
    receives a message from a single shared tick."""
    users = ["stress-a", "stress-b", "stress-c", "stress-d"]
    for u in users:
        db.upsert_user(u, role="member", status="active")

    await _enable(db, config, "stress-a")  # enabled, default window, no DND -> fires
    await _enable(db, config, "stress-b")  # enabled, default window, always-DND -> suppressed
    db.set_user_quiet_hours("stress-b", '[["00:00", "12:00"], ["12:00", "00:00"]]')
    # "stress-c" never opts in -> nothing.
    await _enable(db, config, "stress-d", window="14:00-16:00")  # enabled, but out of window at 09:00

    for u in [*users, OWNER, MEMBER]:
        db.insert_log(LogEntry(None, u, "2026-08-23T07:00:00", "water", 100.0, None, "100ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))

    assert channel.sent_to("stress-a") != []
    assert channel.sent_to("stress-b") == []
    assert channel.sent_to("stress-c") == []
    assert channel.sent_to("stress-d") == []
    assert channel.sent_to(OWNER) == []
    assert channel.sent_to(MEMBER) == []
    assert len(channel.sent) == 1  # exactly stress-a's own single send
