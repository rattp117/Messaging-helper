"""SPEC-v1.5.md §4 "Feature 1 -- Hourly check-ins" (module `checkins`, R-K1-
R-K8): `core/commands.dispatch`'s `"checkin"` kind, `core/checkins.py`'s
`effective_checkin`/`build_checkin_message`/`run_due_checkins`/
`execute_checkin`.

Owned ACs (SPEC-v1.5.md §11): AC-3 (hourly firing), AC-4 (LLM-free content),
AC-5 (all-goals-met skip), AC-6 (DND honored), AC-7 (`/checkin` setter),
AC-8 (opt-in default, owner included), AC-9 (isolation). `/dnd`'s own alias
behavior + the `/help` mention (AC-13) live in `tests/test_dnd.py`.

Mirrors `tests/test_preferences.py`'s own convention (`commands.dispatch`
directly, `execute_checkin` against a real on-disk SQLite `Database`, no
mocks for the DB) and `tests/test_reminders.py`/`tests/test_dnd_matrix.py`'s
own convention for the tick job (a `FakeChannel`, an injectable `clock`)."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, i18n
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
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
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en,
        label_th=label_th,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "checkins.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _fixed_clock(y=2026, m=8, d=23, hh=9, mm=0):
    return lambda: datetime(y, m, d, hh, mm, 0)


# ===========================================================================
# dispatch() shape -- /checkin (R-K8)
# ===========================================================================


@pytest.mark.parametrize(
    "text,expected_value",
    [
        ("/checkin on", "on"),
        ("/checkin off", "off"),
        ("/checkin default", "default"),
        ("/checkin 09:00-18:00", "09:00-18:00"),
        ("/CHECKIN ON", "on"),  # case-insensitive slash trigger
        ("เช็คอิน on", "on"),
        ("เช็คอิน off", "off"),
        ("เช็คอิน default", "default"),
        ("เช็คอิน 09:00-18:00", "09:00-18:00"),
    ],
)
def test_dispatch_recognizes_checkin_shape(text, expected_value):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="checkin", pref_value=expected_value)


def test_dispatch_bare_slash_checkin_means_show_not_usage():
    """R-K8: unlike bare "/lang"/"/quiet", a bare "/checkin" IS a
    recognized shape with its own meaning (show), not a usage error."""
    assert commands.dispatch("/checkin", DEFAULT_REGISTRY) == Command(kind="checkin", pref_value=None)


def test_dispatch_bare_thai_checkin_also_means_show():
    assert commands.dispatch("เช็คอิน", DEFAULT_REGISTRY) == Command(kind="checkin", pref_value=None)


def test_dispatch_checkin_slash_carries_through_an_invalid_window_unvalidated():
    """Shape-only layer, mirrors `/quiet`'s own permissive slash form --
    `execute_checkin` is where an invalid window becomes `checkin_usage`."""
    assert commands.dispatch("/checkin 25:99", DEFAULT_REGISTRY) == Command(kind="checkin", pref_value="25:99")


def test_dispatch_checkin_thai_rejects_an_invalid_window_shape_entirely():
    """The Thai alias, unlike the slash form, gates on valid-argument
    SHAPE (R-K8's own "whole-message-anchored, valid-argument-shape tail
    only" discipline) -- an out-of-shape tail falls through to None rather
    than reaching execute_checkin at all."""
    assert commands.dispatch("เช็คอิน 25:99", DEFAULT_REGISTRY) is None


# ===========================================================================
# adversarial corpus -- เช็คอิน is a common transliterated loanword (hotel/
# flight/social-media "check-in") that can open ordinary prose; a glued
# continuation or an out-of-shape spaced tail must fall through to None,
# same AC5.5/R-K8 conservatism every other command in this router honors.
# ===========================================================================

CHECKIN_ADVERSARIAL_CORPUS = [
    "เช็คอินโรงแรมแล้ว",  # "already checked into the hotel" -- glued, no space after the trigger
    "เช็คอินเที่ยวบินเรียบร้อย",  # "flight check-in done" -- glued
    "เช็คอิน ร้านอาหารอร่อยมาก",  # "checked in [at] a really good restaurant" -- spaced but not a valid tail shape
    "เช็คอิน แล้วนะ",  # "checked in already" -- spaced, ordinary trailing particle, not on/off/default/window
    "I checked in at the airport",  # English prose containing "checked in", not the Thai trigger at all
    "check-in time is 3pm",  # English, unrelated
]


@pytest.mark.parametrize("message", CHECKIN_ADVERSARIAL_CORPUS)
def test_dispatch_returns_none_for_checkin_adversarial_corpus(message):
    assert commands.dispatch(message, DEFAULT_REGISTRY) is None


def test_ordinary_habit_logs_still_fall_through_to_the_parser():
    assert commands.dispatch("500ml", DEFAULT_REGISTRY) is None
    assert commands.dispatch("10 min stretch", DEFAULT_REGISTRY) is None


def test_checkin_does_not_shadow_other_command_kinds():
    assert commands.dispatch("/quiet 22:00-07:00", DEFAULT_REGISTRY).kind == "quiet"
    assert commands.dispatch("/lang th", DEFAULT_REGISTRY).kind == "lang"
    assert commands.dispatch("/target water 2000", DEFAULT_REGISTRY).kind == "target"
    assert commands.dispatch("/undo", DEFAULT_REGISTRY).kind == "undo"


# ===========================================================================
# execute_checkin -- AC-7: on/off/default/window/show/invalid/db-failure.
# ===========================================================================


async def test_execute_checkin_on_stores_the_config_default_window_and_confirms(db, config):
    command = commands.dispatch("/checkin on", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)

    assert db.get_checkin_window(MEMBER) == "08:00-20:00"  # the literal window, not "on" (R-K8)
    assert reply == i18n.t("checkin_set_on", "en", start="08:00", end="20:00")


async def test_execute_checkin_off_stores_off_and_confirms(db, config):
    command = commands.dispatch("/checkin off", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)

    assert db.get_checkin_window(MEMBER) == "off"
    assert reply == i18n.t("checkin_set_off", "en")


async def test_execute_checkin_window_stores_and_confirms(db, config):
    command = commands.dispatch("/checkin 09:00-18:00", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)

    assert db.get_checkin_window(MEMBER) == "09:00-18:00"
    assert reply == i18n.t("checkin_set_window", "en", start="09:00", end="18:00")


async def test_execute_checkin_default_clears_the_override_and_shows_resulting_state(db, config):
    on_command = commands.dispatch("/checkin on", DEFAULT_REGISTRY)
    await checkins.execute_checkin(on_command, db=db, config=config, lang="en", user_id=MEMBER)
    assert db.get_checkin_window(MEMBER) == "08:00-20:00"

    default_command = commands.dispatch("/checkin default", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(default_command, db=db, config=config, lang="en", user_id=MEMBER)

    assert db.get_checkin_window(MEMBER) is None  # cleared -> inherits config (disabled by default)
    assert reply == i18n.t("checkin_show_off", "en")


async def test_execute_checkin_bare_shows_off_when_never_set(db, config):
    command = commands.dispatch("/checkin", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)

    assert db.get_checkin_window(MEMBER) is None  # read-only, no write
    assert reply == i18n.t("checkin_show_off", "en")


async def test_execute_checkin_bare_shows_on_and_window_after_enabling(db, config):
    await checkins.execute_checkin(
        commands.dispatch("/checkin 09:00-18:00", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    reply = await checkins.execute_checkin(
        commands.dispatch("/checkin", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("checkin_show", "en", start="09:00", end="18:00")


@pytest.mark.parametrize(
    "bad_value",
    ["25:99-07:00", "09:00", "09:00-18:00-bad", "09:00-", "-18:00", "9:00-18:00", "09:00-18:0"],
)
async def test_execute_checkin_malformed_window_writes_nothing_and_replies_usage(db, config, bad_value):
    command = commands.dispatch(f"/checkin {bad_value}", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)

    assert db.get_checkin_window(MEMBER) is None  # never written
    assert reply == i18n.t("checkin_usage", "en")


async def test_execute_checkin_db_failure_reports_save_failed_not_a_traceback(db, config, monkeypatch):
    def _boom(self, chat_id, value):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "set_checkin_window", _boom)
    command = commands.dispatch("/checkin on", DEFAULT_REGISTRY)
    reply = await checkins.execute_checkin(command, db=db, config=config, lang="en", user_id=MEMBER)

    assert reply == i18n.t("checkin_save_failed", "en")


async def test_execute_checkin_bilingual_replies(db, config):
    on_reply_th = await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db, config=config, lang="th", user_id=MEMBER
    )
    assert on_reply_th == i18n.t("checkin_set_on", "th", start="08:00", end="20:00")


# ===========================================================================
# effective_checkin / AC-8 -- opt-in default (owner included), fail-open.
# ===========================================================================


def test_effective_checkin_disabled_by_default_for_everyone_including_owner(db, config):
    """AC-8: the shipped config ([checkin] enabled=false) + no per-user
    override -> disabled for EVERY user, owner included -- OQ1 resolved (b)."""
    assert checkins.effective_checkin(db, config, OWNER) == (False, None)
    assert checkins.effective_checkin(db, config, MEMBER) == (False, None)


async def test_effective_checkin_after_checkin_on_is_enabled_at_the_default_window(db, config):
    await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=OWNER
    )
    assert checkins.effective_checkin(db, config, OWNER) == (True, ("08:00", "20:00"))
    # MEMBER is unaffected by OWNER's own change.
    assert checkins.effective_checkin(db, config, MEMBER) == (False, None)


def test_effective_checkin_off_override(db, config):
    db.set_checkin_window(OWNER, "off")
    assert checkins.effective_checkin(db, config, OWNER) == (False, None)


def test_effective_checkin_explicit_window_override(db, config):
    db.set_checkin_window(OWNER, "07:00-19:00")
    assert checkins.effective_checkin(db, config, OWNER) == (True, ("07:00", "19:00"))


def test_effective_checkin_would_inherit_an_enabled_config_default_if_operator_opts_in_globally(db):
    """Not the shipped default, but proves the inherit-branch: an operator
    who explicitly flips [checkin] enabled=true globally is honored for a
    user with no override of their own."""
    config = Config.model_validate({"checkin": {"enabled": True, "window": "06:00-22:00"}})
    assert checkins.effective_checkin(db, config, OWNER) == (True, ("06:00", "22:00"))


def test_effective_checkin_fails_open_on_a_db_read_error(config):
    class _RaisingDatabase:
        def get_user(self, chat_id):
            raise RuntimeError("simulated DB failure")

        def get_checkin_window(self, chat_id):
            raise RuntimeError("simulated DB failure")

    # Fail-open -> treated as "no override" -> inherits the config default,
    # which ships disabled (AC-8's own "never spam on a DB hiccup" posture).
    assert checkins.effective_checkin(_RaisingDatabase(), config, OWNER) == (False, None)


# ===========================================================================
# build_checkin_message -- AC-4 (LLM-free, deterministic, bilingual),
# AC-5 (all-goals-met skip / generic nudge for no-goal-bearing users).
# ===========================================================================


def test_checkins_module_never_imports_or_calls_an_llm():
    """AC-4's "zero Ollama calls" -- structural: this module never imports
    the LLM client, and none of its public functions take an `llm`
    parameter at all, so there is no code path through which one could be
    invoked (mirrors test_dnd_matrix.py's own AC-12 structural technique)."""
    assert "habit_assistant.llm" not in inspect.getsource(checkins)
    for fn in (checkins.effective_checkin, checkins.build_checkin_message, checkins.run_due_checkins, checkins.execute_checkin):
        assert "llm" not in inspect.signature(fn).parameters


def test_build_checkin_message_shows_unmet_goal_bearing_habits_only(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 1200.0, None, "1200ml", "reply"))

    message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert message == "\n".join(
        [
            i18n.t("checkin_header", "en"),
            i18n.t("checkin_line_progress", "en", label="water", total=1200.0, goal=2500.0, unit="ml"),
            i18n.t("checkin_invite", "en"),
        ]
    )


def test_build_checkin_message_shows_not_yet_today_when_zero(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])

    message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert i18n.t("checkin_line_not_yet", "en", label="water") in message


def test_build_checkin_message_skips_when_all_goal_bearing_habits_already_met(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 3000.0, None, "3000ml", "reply"))

    message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert message is None  # R-K3: non-nagging skip


def test_build_checkin_message_generic_nudge_for_a_user_with_no_goal_bearing_habits(db, config):
    """R-K3: an empty goal_bearing set is NOT the skip rule -- a
    diary-only user (no numeric/duration goal) still gets a generic
    nudge."""
    registry = HabitRegistry([_habit("diary", "text", label_en="diary", label_th="ไดอารี่", unit_en=None, unit_th=None)])

    message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert message == i18n.t("checkin_generic_nudge", "en")


def test_build_checkin_message_partial_progress_lists_only_the_unmet_habit(db, config):
    registry = HabitRegistry(
        [
            _habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล."),
            _habit("pushups", "numeric", goal=50.0, label_en="pushups", label_th="วิดพื้น", unit_en="reps", unit_th="ครั้ง"),
        ]
    )
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 3000.0, None, "3000ml", "reply"))  # met
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "pushups", 10.0, None, "10 reps", "reply"))  # not met

    message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    assert "water" not in message
    assert "pushups" in message


def test_build_checkin_message_bilingual(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])
    en_message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    th_message = checkins.build_checkin_message(db, config, registry, "th", OWNER, clock=_fixed_clock())
    assert en_message != th_message
    assert i18n.detect_language(th_message) == "th"


def test_build_checkin_message_isolated_per_user(db, config):
    registry = HabitRegistry([_habit("water", "numeric", goal=2500.0, label_en="water", label_th="น้ำ", unit_en="ml", unit_th="มล.")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T09:00:00", "water", 1200.0, None, "1200ml", "reply"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-23T09:00:00", "water", 2500.0, None, "2500ml", "reply"))

    owner_message = checkins.build_checkin_message(db, config, registry, "en", OWNER, clock=_fixed_clock())
    member_message = checkins.build_checkin_message(db, config, registry, "en", MEMBER, clock=_fixed_clock())

    assert owner_message is not None and "1200" in owner_message
    assert member_message is None  # MEMBER already met their own goal


# ===========================================================================
# run_due_checkins -- AC-3 (hourly firing + boundaries), AC-6 (DND),
# AC-9 (isolation).
# ===========================================================================


async def _enable(db, config, user_id: str, window: str | None = None) -> None:
    if window is None:
        await checkins.execute_checkin(
            commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
        )
    else:
        await checkins.execute_checkin(
            commands.dispatch(f"/checkin {window}", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
        )


async def test_ac3_fires_on_the_hour_inside_the_default_window(db, config):
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=8, mm=0))
    assert channel.sent_to(OWNER) != []


async def test_ac3_fires_at_the_inclusive_end_boundary_20_00(db, config):
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []  # R-K2: start <= HH:00 <= end, END inclusive


async def test_ac3_does_not_fire_before_the_window_starts(db, config):
    await _enable(db, config, OWNER)
    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=7, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_ac3_does_not_fire_after_the_window_ends(db, config):
    await _enable(db, config, OWNER)
    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=21, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_ac3_does_not_fire_off_the_hour(db, config):
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=1))
    assert channel.sent_to(OWNER) == []

    channel2 = FakeChannel()
    await checkins.run_due_checkins(channel2, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=59))
    assert channel2.sent_to(OWNER) == []


async def test_ac3_a_custom_window_is_honored(db, config):
    await _enable(db, config, OWNER, window="12:00-14:00")
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    inside = FakeChannel()
    await checkins.run_due_checkins(inside, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=13, mm=0))
    assert inside.sent_to(OWNER) != []

    outside = FakeChannel()
    await checkins.run_due_checkins(outside, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=15, mm=0))
    assert outside.sent_to(OWNER) == []


async def test_ac6_dnd_suppresses_a_due_checkin(db, config):
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))
    db.set_user_quiet_hours(OWNER, '[["00:00", "12:00"], ["12:00", "00:00"]]')  # always-DND, full-day coverage

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_ac6_dnd_is_scoped_to_the_user_in_it(db, config):
    await _enable(db, config, OWNER)
    await _enable(db, config, MEMBER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))
    db.set_user_quiet_hours(OWNER, '[["00:00", "12:00"], ["12:00", "00:00"]]')  # OWNER always-DND

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    assert channel.sent_to(OWNER) == []
    assert channel.sent_to(MEMBER) != []


async def test_ac8_disabled_by_default_no_one_gets_a_checkin_owner_included(db, config):
    """AC-8: nobody has run /checkin on -- both users, owner included,
    receive nothing even squarely inside the would-be default window."""
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    assert channel.sent == []


async def test_ac9_isolation_a_disabled_off_hour_or_dnd_user_never_leaks_into_an_enabled_users_send(db, config):
    """AC-9: A (enabled, in-window, not-DND) gets exactly A's own message;
    B (never opted in) gets nothing at all -- and A's content reflects
    only A's own data."""
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 400.0, None, "400ml", "reply"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-23T07:00:00", "water", 9999.0, None, "9999ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))

    assert channel.sent_to(MEMBER) == []
    owner_sent = channel.sent_to(OWNER)
    assert len(owner_sent) == 1
    assert "400" in owner_sent[0]
    assert "9999" not in owner_sent[0]


async def test_run_due_checkins_never_calls_ollama_end_to_end(db, config):
    """AC-4, end-to-end: a full tick with an eligible user produces a send
    with no LLM in the call path at all (the FakeChannel/db/registry used
    throughout this file have no llm client to begin with)."""
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    assert channel.sent_to(OWNER) != []


async def test_run_due_checkins_respects_each_users_own_language_preference(db, config, monkeypatch):
    from habit_assistant.core.preferences import execute_lang

    await _enable(db, config, OWNER)
    await execute_lang(commands.dispatch("/lang th", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0))
    sent = channel.sent_to(OWNER)
    assert len(sent) == 1
    assert i18n.detect_language(sent[0]) == "th"


# ---------------------------------------------------------------------------
# SPEC-v1.7.md R-G3: `registry_for` -- additive, optional per-user registry
# resolution. Omitted (every test above), byte-identical to pre-v1.7 (AC-5).
# ---------------------------------------------------------------------------


async def test_run_due_checkins_registry_for_resolves_per_user(db, config, monkeypatch):
    """The registry `build_checkin_message` actually receives is the one
    `registry_for(user_id)` returns for THAT user, not the fallback
    `registry` positional -- proven by capturing what each call received."""
    await _enable(db, config, OWNER)
    await _enable(db, config, MEMBER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))
    db.insert_log(LogEntry(None, MEMBER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    owner_registry = DEFAULT_REGISTRY
    member_registry = HabitRegistry.from_config(Config())  # a distinct object, same content
    received: dict[str, "HabitRegistry"] = {}
    real_build = checkins.build_checkin_message

    def spy(db_, config_, registry_, lang_, user_id_, clock_=datetime.now):
        received[user_id_] = registry_
        return real_build(db_, config_, registry_, lang_, user_id_, clock_)

    monkeypatch.setattr(checkins, "build_checkin_message", spy)

    def registry_for(user_id: str) -> "HabitRegistry":
        return owner_registry if user_id == OWNER else member_registry

    channel = FakeChannel()
    await checkins.run_due_checkins(
        channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0), registry_for=registry_for
    )

    assert received[OWNER] is owner_registry
    assert received[MEMBER] is member_registry


async def test_run_due_checkins_registry_for_none_falls_back_to_registry(db, config):
    await _enable(db, config, OWNER)
    db.insert_log(LogEntry(None, OWNER, "2026-08-23T07:00:00", "water", 1000.0, None, "1000ml", "reply"))

    channel = FakeChannel()
    await checkins.run_due_checkins(
        channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=9, mm=0), registry_for=None
    )
    assert channel.sent_to(OWNER) != []
