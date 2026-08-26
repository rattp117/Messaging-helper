"""SPEC-v1.9.md "Life happens" §4 R18-R20 (module `cadence`, Theme A.1):
`core/commands.py`'s `"cadence"` kind (`_match_cadence`/`_match_cadence_
slash`/`_match_cadence_nl`), `core/cadence.py`'s `execute_cadence`/
`weekly_progress`/`cadence_status_line`, and `core/habitdef.py`'s
`cadence=<N>w` pipe-key wiring into `validate_and_normalize`/
`execute_addhabit`.

Owned ACs (SPEC-v1.9.md §11): AC7 (/cadence set/off + validation), AC8
(/addhabit cadence=<N>w, atomic), AC9 (week-count streak + streak_unit),
AC10 (weekly_progress "X of N"), AC11 (rest days don't break a cadence
streak), AC12 (records.update_on_log stores/celebrates a week count).

Mirrors `tests/test_schedules.py`'s/`tests/test_targets.py`'s own
convention: `commands.dispatch(text, registry)` directly for the
recognize-shape layer, `execute_cadence`/`validate_and_normalize`/
`execute_addhabit` against a real on-disk SQLite `Database` (no DB
mocks), an `_execute` helper mirroring `test_schedules.py`'s own shape.

CRITICAL (per the shared-surface Luna's flagged collision,
IMPL-v1.9-shared.md's "Known limitations", and the parent dispatch's own
explicit instruction): the Thai reserved stem `กี่ครั้งต่อสัปดาห์` CONTAINS
`กี่` -- one of `core/commands.py:_QUERY_PATTERNS`'s own substring anchors
-- so `tests/test_commands.py::test_dispatch_query_*`-style adversarial
proof in BOTH directions lives here too, in "Adversarial dispatch corpus"
below: a genuine cadence phrase must route to `"cadence"`, and an
ordinary "how many" query must still route to `"query"`, unaffected by
this module's own matcher being wired in ahead of `_match_query`."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, habitdef, i18n
from habit_assistant.core.cadence import cadence_status_line, execute_cadence, weekly_progress
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.records import update_on_log
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.streaks import compute_streak, streak_unit
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def _seed(db: Database, ts: str, category: str, value_num: float | None, raw: str = "x", user_id: str = OWNER) -> int:
    entry = LogEntry(None, user_id, ts, category, value_num, None, raw, "reply")
    return db.insert_log(entry)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "cadence.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def provider(db, config):
    return RegistryProvider(config, db)


async def _execute(
    text: str,
    db: Database,
    config: Config,
    registry: HabitRegistry = DEFAULT_REGISTRY,
    lang: str = "en",
    user_id: str = OWNER,
) -> str:
    command = commands.dispatch(text, registry)
    assert command is not None and command.kind == "cadence"
    return await execute_cadence(command, db=db, config=config, registry=registry, lang=lang, user_id=user_id)


# ===========================================================================
# commands.dispatch shape -- slash form, Thai aliases
# ===========================================================================


def test_dispatch_slash_set_shape():
    assert commands.dispatch("/cadence stretch 3", DEFAULT_REGISTRY) == Command(
        kind="cadence", category="stretch", value_num=3.0
    )


def test_dispatch_slash_off_shape():
    assert commands.dispatch("/cadence stretch off", DEFAULT_REGISTRY) == Command(
        kind="cadence", category="stretch", pref_value="off"
    )


def test_dispatch_slash_off_thai_tail_shape():
    # SPEC-v1.9.md §2's own "Thai tail ปิด/ค่าเริ่มต้น" for the English slash
    # form's off-word argument.
    assert commands.dispatch("/cadence stretch ปิด", DEFAULT_REGISTRY) == Command(
        kind="cadence", category="stretch", pref_value="off"
    )
    assert commands.dispatch("/cadence stretch ค่าเริ่มต้น", DEFAULT_REGISTRY) == Command(
        kind="cadence", category="stretch", pref_value="off"
    )


def test_dispatch_slash_habit_only_no_tail_is_usage_shape():
    cmd = commands.dispatch("/cadence stretch", DEFAULT_REGISTRY)
    assert cmd == Command(kind="cadence", category="stretch")


def test_dispatch_slash_bare_is_usage_shape():
    assert commands.dispatch("/cadence", DEFAULT_REGISTRY) == Command(kind="cadence")


def test_dispatch_slash_malformed_value_falls_to_usage_shape():
    cmd = commands.dispatch("/cadence stretch three", DEFAULT_REGISTRY)
    assert cmd == Command(kind="cadence", category="stretch")  # value_num/pref_value both None


def test_dispatch_slash_unresolved_habit_still_dispatches():
    # Mirrors `/target`'s own AC16 posture: an unrecognized habit token
    # still produces a Command (the raw lowercased token), letting
    # `execute_cadence` report `cadence_invalid_habit` rather than the
    # message silently falling through as an unrecognized log.
    assert commands.dispatch("/cadence bogus 3", DEFAULT_REGISTRY) == Command(
        kind="cadence", category="bogus", value_num=3.0
    )


@pytest.mark.parametrize(
    "text",
    [
        "กี่ครั้งต่อสัปดาห์ยืดเส้น3",  # compact, no spaces (real Thai writing)
        "กี่ครั้งต่อสัปดาห์ ยืดเส้น 3",  # spaced, matching §2's own illustrative example
        "ต่อสัปดาห์ยืดเส้น3",
        "ต่อสัปดาห์ ยืดเส้น 3",
    ],
)
def test_dispatch_thai_nl_set_shape(text):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="cadence", category="stretch", value_num=3.0)


@pytest.mark.parametrize(
    "text",
    [
        "กี่ครั้งต่อสัปดาห์ยืดเส้นปิด",
        "ต่อสัปดาห์ยืดเส้นปิด",
        "ต่อสัปดาห์ ยืดเส้น off",
    ],
)
def test_dispatch_thai_nl_off_shape(text):
    assert commands.dispatch(text, DEFAULT_REGISTRY) == Command(kind="cadence", category="stretch", pref_value="off")


# ===========================================================================
# Adversarial dispatch corpus -- the CRITICAL two-way collision proof
# (กี่ครั้งต่อสัปดาห์ contains กี่, a query-intent substring anchor).
# ===========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "กี่ครั้งแล้ว",  # "how many times already" -- no habit/value tail, must stay a query
        "น้ำกี่ครั้งวันนี้",  # "water how many times today" -- contains กี่, no cadence shape
        "ดื่มน้ำไปกี่ครั้งแล้ว",
        "วันนี้ทำอะไรไปบ้าง กี่ครั้ง",
    ],
)
def test_adversarial_ordinary_how_many_queries_still_route_to_query(text):
    cmd = commands.dispatch(text, DEFAULT_REGISTRY)
    assert cmd is not None and cmd.kind == "query", f"{text!r} dispatched as {cmd!r}, expected kind='query'"


def test_adversarial_bare_reserved_thai_cadence_stem_still_routes_to_query():
    # Pre-existing, DOCUMENTED collision (IMPL-v1.9-shared.md): the bare
    # reserved stem alone (no habit/value following it) has never been a
    # genuine cadence command shape -- this module's own matcher requires
    # a resolvable habit + a valid value tail, so a bare trigger still
    # falls through to `_match_query`'s own "กี่" substring anchor exactly
    # as it did before this module's matcher existed. Must NOT regress.
    cmd = commands.dispatch("กี่ครั้งต่อสัปดาห์", DEFAULT_REGISTRY)
    assert cmd is not None and cmd.kind == "query"


def test_adversarial_bare_reserved_stem_tor_sapda_still_dispatches_nothing():
    assert commands.dispatch("ต่อสัปดาห์", DEFAULT_REGISTRY) is None


@pytest.mark.parametrize(
    "text",
    [
        "ต่อสัปดาห์นี้ฉันยุ่งมาก",  # "this week I'm very busy" -- ordinary prose, not a habit token
        "ต่อสัปดาห์นี้ฉันดื่มน้ำเยอะมาก",  # "this week I drank a lot of water" -- habit substring appears LATER, not immediately
        "สัปดาห์หน้าฉันจะไปเที่ยว",  # unrelated prose, doesn't even start with the trigger
    ],
)
def test_adversarial_ordinary_prose_opening_with_the_trigger_word_does_not_dispatch_as_cadence(text):
    cmd = commands.dispatch(text, DEFAULT_REGISTRY)
    assert cmd is None or cmd.kind != "cadence", f"{text!r} incorrectly dispatched as cadence: {cmd!r}"


def test_adversarial_ordinary_habit_log_never_dispatches_as_cadence():
    # A completely normal log message must never be swallowed.
    for text in ("ยืดเส้น 3", "500ml", "10 min stretch", "น้ำ 500"):
        cmd = commands.dispatch(text, DEFAULT_REGISTRY)
        assert cmd is None or cmd.kind != "cadence"


# ===========================================================================
# execute_cadence -- AC7 (set/off + validation, audit, no-write-on-error).
# ===========================================================================


async def test_set_cadence_writes_row_confirms_and_audits(db, config):
    reply = await _execute("/cadence stretch 3", db, config)
    assert "3×/week" in reply
    assert db.get_cadence(OWNER, "stretch") == 3

    rows = db.recent_audit(5)
    matching = [r for r in rows if r["action"] == "cadence_set" and r["entity"] == "stretch"]
    assert len(matching) == 1
    assert matching[0]["new_value"] == "3"


async def test_set_cadence_twice_upserts_not_stacks(db, config):
    await _execute("/cadence stretch 3", db, config)
    await _execute("/cadence stretch 5", db, config)
    assert db.get_cadence(OWNER, "stretch") == 5


async def test_off_clears_cadence_and_audits(db, config):
    await _execute("/cadence stretch 3", db, config)
    reply = await _execute("/cadence stretch off", db, config)
    assert db.get_cadence(OWNER, "stretch") is None

    rows = db.recent_audit(5)
    matching = [r for r in rows if r["action"] == "cadence_clear" and r["entity"] == "stretch"]
    assert len(matching) == 1
    assert reply  # a friendly confirmation, not empty


async def test_off_when_never_set_is_idempotent_no_crash(db, config):
    reply = await _execute("/cadence stretch off", db, config)
    assert db.get_cadence(OWNER, "stretch") is None
    assert reply


async def test_unknown_habit_returns_friendly_error_and_writes_nothing(db, config):
    reply = await _execute("/cadence bogus 3", db, config)
    assert "bogus" in reply
    assert db.get_cadence(OWNER, "bogus") is None
    assert all(r["entity"] != "bogus" for r in db.recent_audit(10))


@pytest.mark.parametrize("n", [0, -1, 8, 100])
async def test_out_of_range_n_returns_friendly_error_and_writes_nothing(db, config, n):
    # N=0/negative never even reaches here as a digit-tail (dispatch would
    # produce value_num=None for a negative sign) -- exercise execute_
    # cadence directly for the numeric-but-out-of-range cases (0, 8, 100)
    # AC7's own "N∉[1,7]" wording covers, plus a direct Command for a
    # value dispatch can't produce (negative) to prove the bounds check
    # itself, not just the matcher's own digit-only shape gate.
    cmd = Command(kind="cadence", category="stretch", value_num=float(n))
    reply = await execute_cadence(cmd, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER)
    assert db.get_cadence(OWNER, "stretch") is None
    assert reply
    assert all(r["action"] == "cadence_set" for r in db.recent_audit(10)) is False or db.recent_audit(10) == []


async def test_bare_cadence_is_usage_reply(db, config):
    reply = await execute_cadence(Command(kind="cadence"), db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER)
    assert reply == i18n.t("cadence_usage", "en")


async def test_habit_only_no_value_is_usage_reply(db, config):
    reply = await _execute("/cadence stretch", db, config)
    assert reply == i18n.t("cadence_usage", "en")


async def test_bilingual_reply(db, config):
    reply_th = await _execute("/cadence stretch 3", db, config, lang="th")
    assert "3" in reply_th and "ยืดเส้น" in reply_th


# ===========================================================================
# /addhabit cadence=<N>w -- AC8 (atomic creation, malformed -> creates
# neither).
# ===========================================================================


async def test_addhabit_cadence_key_creates_habit_and_cadence_row_atomically(db, config, provider):
    base_registry = HabitRegistry.from_config(config)
    cmd = commands.dispatch("/addhabit id=gym|type=boolean|en=gym|th=ยิม|cadence=3w", provider.for_user(OWNER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=base_registry, lang="en", user_id=OWNER
    )
    assert "gym" in provider.for_user(OWNER).ids()
    assert db.get_cadence(OWNER, "gym") == 3

    rows = db.recent_audit(10)
    assert any(r["action"] == "cadence_set" and r["entity"] == "gym" for r in rows)
    assert any(r["action"] == "habit_create" and r["entity"] == "gym" for r in rows)


async def test_addhabit_malformed_cadence_creates_neither(db, config, provider):
    base_registry = HabitRegistry.from_config(config)
    for bad in ("cadence=abc", "cadence=0w", "cadence=8w", "cadence=3"):
        cmd = commands.dispatch(f"/addhabit id=gym2|type=boolean|en=gym2|th=ยิมสอง|{bad}", provider.for_user(OWNER))
        reply = await habitdef.execute_addhabit(
            cmd, db=db, provider=provider, config=config, base_registry=base_registry, lang="en", user_id=OWNER
        )
        assert "gym2" not in provider.for_user(OWNER).ids(), f"{bad!r} unexpectedly created the habit"
        assert db.get_cadence(OWNER, "gym2") is None
        assert reply == i18n.t("addhabit_invalid_cadence", "en", max=config.cadence.max_per_week)


async def test_addhabit_without_cadence_key_unaffected(db, config, provider):
    base_registry = HabitRegistry.from_config(config)
    cmd = commands.dispatch("/addhabit id=plain|type=boolean|en=plain", provider.for_user(OWNER))
    await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=base_registry, lang="en", user_id=OWNER
    )
    assert "plain" in provider.for_user(OWNER).ids()
    assert db.get_cadence(OWNER, "plain") is None


def test_validate_and_normalize_cadence_shape_directly():
    base_registry = HabitRegistry.from_config(Config())
    fields = {"id": "gym3", "type": "boolean", "en": "gym3", "cadence": "5w"}
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, base_registry, base_registry, frozenset(), 20, cadence_max=7)
    assert msg_id is None
    assert row is not None and row["cadence"] == 5


def test_validate_and_normalize_cadence_out_of_range_directly():
    base_registry = HabitRegistry.from_config(Config())
    fields = {"id": "gym4", "type": "boolean", "en": "gym4", "cadence": "9w"}
    row, msg_id, kwargs = habitdef.validate_and_normalize(fields, base_registry, base_registry, frozenset(), 20, cadence_max=7)
    assert row is None
    assert msg_id == "addhabit_invalid_cadence"
    assert kwargs == {"max": 7}


# ===========================================================================
# Weekly-engine wiring -- AC9 (streak_unit / week-count), AC11 (rest days
# don't break the streak).
# ===========================================================================


def test_streak_unit_switches_on_cadence_presence(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    assert streak_unit(db, stretch, OWNER) == "day"
    db.set_cadence(OWNER, "stretch", 3)
    assert streak_unit(db, stretch, OWNER) == "week"
    db.clear_cadence(OWNER, "stretch")
    assert streak_unit(db, stretch, OWNER) == "day"


def test_three_per_week_rest_days_do_not_break_the_streak(db, config):
    # ISO week 34 of 2026: Mon 2026-08-17 .. Sun 2026-08-23.
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-17T07:00:00", "stretch", 10.0)  # Mon
    _seed(db, "2026-08-19T07:00:00", "stretch", 10.0)  # Wed
    _seed(db, "2026-08-21T07:00:00", "stretch", 10.0)  # Fri
    # Tue/Thu/Sat/Sun left empty (rest days).

    streak_friday = compute_streak(db, config, stretch, date(2026, 8, 21), OWNER)
    assert streak_friday == 1, "current week already MET (3 of 3) counts immediately, rest days don't break it"

    # A day BEFORE the 3rd qualifying log (Thursday) -- only 2 of 3 so
    # far, current partial week is NOT YET met -> never over-reported
    # mid-week (Rule 4's own "never evaluated as failed while partial").
    streak_thursday = compute_streak(db, config, stretch, date(2026, 8, 20), OWNER)
    assert streak_thursday == 0

    # Week 2: Mon 2026-08-24 .. Sun 2026-08-30. Same Mon/Wed/Fri pattern.
    _seed(db, "2026-08-24T07:00:00", "stretch", 10.0)  # Mon
    _seed(db, "2026-08-26T07:00:00", "stretch", 10.0)  # Wed
    _seed(db, "2026-08-28T07:00:00", "stretch", 10.0)  # Fri

    streak_week2 = compute_streak(db, config, stretch, date(2026, 8, 28), OWNER)
    assert streak_week2 == 2, "week 1 (completed, MET) + week 2 (current, MET) -- unbroken across the rest days"


def test_a_genuinely_missed_week_breaks_the_cadence_streak(db, config):
    # Rule 4's own "current (partial) week never breaks, only ever adds"
    # posture means a MISSED week only actually breaks the walk once it
    # is no longer the week CONTAINING `end_date` -- so `end_date` here is
    # placed in week 3 (2026-08-31, Monday), making week 2 a genuinely
    # PRIOR, fully-elapsed week the walk-back logic can evaluate as
    # MET/NEUTRAL/MISSED (unlike `end_date` landing inside week 2 itself,
    # which the engine always treats leniently as "current", per
    # `test_three_per_week_rest_days_do_not_break_the_streak`'s own
    # `streak_thursday == 0` case just above).
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    # Week 1 (Aug 17-23): MET (3 logs).
    _seed(db, "2026-08-17T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-19T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-21T07:00:00", "stretch", 10.0)
    # Week 2 (Aug 24-30): only 1 log -- genuinely MISSED (no pause to make
    # 3 unreachable).
    _seed(db, "2026-08-24T07:00:00", "stretch", 10.0)

    streak = compute_streak(db, config, stretch, date(2026, 8, 31), OWNER)  # Monday of week 3 -- an empty "current" week
    assert streak == 0, "week 2 MISSED (1 of 3, fully reachable) breaks the walk before week 1 is ever counted"


# ===========================================================================
# weekly_progress / cadence_status_line -- AC10 ("X of N this week").
# ===========================================================================


def test_weekly_progress_counts_this_iso_week_only(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-17T07:00:00", "stretch", 10.0)  # Mon
    _seed(db, "2026-08-19T07:00:00", "stretch", 10.0)  # Wed

    done, n = weekly_progress(db, config, stretch, OWNER, date(2026, 8, 20))  # Thursday, same week
    assert (done, n) == (2, 3)


def test_weekly_progress_non_cadence_habit_returns_zero_zero(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    done, n = weekly_progress(db, config, stretch, OWNER, date(2026, 8, 20))
    assert (done, n) == (0, 0)


def test_cadence_status_line_shows_checkmark_only_once_met(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-17T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-19T07:00:00", "stretch", 10.0)

    not_met_line = cadence_status_line(db, config, stretch, OWNER, date(2026, 8, 20), "en")
    assert "2 of 3" in not_met_line and "✅" not in not_met_line

    _seed(db, "2026-08-21T07:00:00", "stretch", 10.0)
    met_line = cadence_status_line(db, config, stretch, OWNER, date(2026, 8, 21), "en")
    assert "3 of 3" in met_line and "✅" in met_line


# ===========================================================================
# records.update_on_log integration -- AC12 (stores/celebrates a WEEK
# count, best_day/best_week unaffected).
# ===========================================================================


async def test_records_stores_and_celebrates_a_week_count_for_a_cadence_habit(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-17T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-19T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-21T07:00:00", "stretch", 10.0)

    def _clock_week1():
        return datetime(2026, 8, 21, 12, 0, 0)

    broken1 = update_on_log(db, config, DEFAULT_REGISTRY, stretch, OWNER, clock=_clock_week1)
    # First-ever observation: seeded silently, nothing to have "strictly
    # exceeded" yet (records.py's own R-R2 "seed" posture) -- but the
    # stored value must already be a WEEK count (1), not a day count.
    assert broken1 == []
    assert db.get_record(OWNER, "stretch", "longest_streak") == 1.0

    # Week 2: extend the pattern -> a genuine new best (2 weeks).
    _seed(db, "2026-08-24T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-26T07:00:00", "stretch", 10.0)
    _seed(db, "2026-08-28T07:00:00", "stretch", 10.0)

    def _clock_week2():
        return datetime(2026, 8, 28, 12, 0, 0)

    broken2 = update_on_log(db, config, DEFAULT_REGISTRY, stretch, OWNER, clock=_clock_week2)
    assert ("longest_streak", 2.0) in broken2
    assert db.get_record(OWNER, "stretch", "longest_streak") == 2.0


async def test_records_best_day_best_week_unaffected_by_cadence(db, config):
    # AC12's own "best_day/best_week are unaffected" -- those two record
    # types are plain aggregation totals (unrelated to the streak walk),
    # so setting a cadence must not change how they're computed at all.
    # Smoke-check: update_on_log doesn't raise and both record types still
    # get seeded normally for a cadence habit.
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-17T07:00:00", "stretch", 15.0)

    def _clock():
        return datetime(2026, 8, 17, 12, 0, 0)

    update_on_log(db, config, DEFAULT_REGISTRY, stretch, OWNER, clock=_clock)
    assert db.get_record(OWNER, "stretch", "best_day") == 15.0
