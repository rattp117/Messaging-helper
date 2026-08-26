"""Vera's own adversarial probes for SPEC-v1.9.md module `cadence` (M1,
AC7-AC12), independent of Luna's own `tests/test_cadence.py` (49 tests,
already earning solid 1:1 AC coverage -- read that file first). This file
exists to poke at edges her own suite doesn't exercise: N boundary values
(1 and 7, not just the out-of-range side), per-user registry isolation of
`/cadence` (another user's own custom habit), cadence on non-boolean habit
types (text/duration -- the spec never restricts cadence to boolean),
`/addhabit cadence=<N>w` malformed SHAPES beyond Luna's own corpus ("3d",
"w3", "3.5w"), a year-boundary weekly walk (2026's genuine ISO week 53 ->
2027-W01, mirroring `tests/test_v19_grace_gaps.py`'s own week-53 rationale
-- the shared-surface suite proved the DAILY walk crosses a year boundary
correctly but never exercised the WEEKLY walk across one), `weekly_progress`
counting-once-per-day (not per-log) and correctly excluding a backfilled
log that lands in a PRIOR ISO week, a real cross-module interop check
(`db.set_cadence` + `db.insert_pause`, both real write paths, not raw SQL)
for the "cadence week made unreachable by a pause is NEUTRAL, not MISSED"
rule, a real cross-module grace-exclusion check via `grace.evaluate_grace`
itself (not just `streaks.py` with a hand-seeded stray row), and a wider
Thai collision corpus than Luna's own four-shape proof -- including the
specific `ต่อ` (resume) vs `ต่อสัปดาห์` (cadence) boundary the interaction
investigation above was worried about.

Same conventions as `tests/test_cadence.py`: real on-disk SQLite via
`tmp_path`, no DB mocks, `commands.dispatch` for the recognize-shape layer,
`execute_cadence`/`habitdef.execute_addhabit` against the real functions.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, grace, habitdef, i18n
from habit_assistant.core.cadence import execute_cadence, weekly_progress
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.streaks import compute_streak
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
OTHER = "other-chat"
DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def _seed(db: Database, ts: str, category: str, value_num: float | None, user_id: str = OWNER, raw: str = "x") -> int:
    entry = LogEntry(None, user_id, ts, category, value_num, None, raw, "reply")
    return db.insert_log(entry)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "cadence_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(OTHER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def provider(db, config):
    return RegistryProvider(config, db)


async def _execute(text: str, db: Database, config: Config, registry: HabitRegistry, user_id: str, lang: str = "en") -> str:
    command = commands.dispatch(text, registry)
    assert command is not None and command.kind == "cadence"
    return await execute_cadence(command, db=db, config=config, registry=registry, lang=lang, user_id=user_id)


# ===========================================================================
# /cadence N bounds -- 0? 1? 7? 8? (Luna's own suite proves 0/-1/8/100
# rejected; this fills in the two ACCEPT boundaries, 1 and 7).
# ===========================================================================


async def test_cadence_n_equals_1_is_the_minimum_accepted_value(db, config):
    reply = await _execute("/cadence stretch 1", db, config, DEFAULT_REGISTRY, OWNER)
    assert "1×/week" in reply
    assert db.get_cadence(OWNER, "stretch") == 1


async def test_cadence_n_equals_7_is_the_maximum_accepted_value(db, config):
    reply = await _execute("/cadence stretch 7", db, config, DEFAULT_REGISTRY, OWNER)
    assert "7×/week" in reply
    assert db.get_cadence(OWNER, "stretch") == 7


async def test_cadence_n_equals_8_rejected_by_default_max_per_week_7(db, config):
    cmd = Command(kind="cadence", category="stretch", value_num=8.0)
    reply = await execute_cadence(cmd, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER)
    assert db.get_cadence(OWNER, "stretch") is None
    assert reply == i18n.t("cadence_invalid_value", "en", habit_id="stretch", max=7)


async def test_off_is_the_documented_clear_word_and_a_second_off_stays_idempotent(db, config):
    await _execute("/cadence stretch 3", db, config, DEFAULT_REGISTRY, OWNER)
    await _execute("/cadence stretch off", db, config, DEFAULT_REGISTRY, OWNER)
    assert db.get_cadence(OWNER, "stretch") is None
    # A second "off" with nothing to clear must not raise or write a stray row.
    reply2 = await _execute("/cadence stretch off", db, config, DEFAULT_REGISTRY, OWNER)
    assert reply2 and db.get_cadence(OWNER, "stretch") is None


# ===========================================================================
# Per-user isolation -- another user's own custom habit is invisible to a
# caller whose own registry never included it (R20's "per-user scoped").
# ===========================================================================


async def test_cadence_on_another_users_custom_habit_is_unknown_habit_not_a_leak(db, config, provider):
    base_registry = HabitRegistry.from_config(config)
    owner_cmd = commands.dispatch("/addhabit id=gym|type=boolean|en=gym|th=ยิม", provider.for_user(OWNER))
    await habitdef.execute_addhabit(
        owner_cmd, db=db, provider=provider, config=config, base_registry=base_registry, lang="en", user_id=OWNER
    )
    assert "gym" in provider.for_user(OWNER).ids()
    assert "gym" not in provider.for_user(OTHER).ids()

    # OTHER's own registry doesn't resolve "gym" -> dispatch still produces
    # a raw-token Command (mirrors /target's AC16 posture), execute_cadence
    # reports the friendly unknown-habit error and writes NOTHING for OTHER.
    other_registry = provider.for_user(OTHER)
    reply = await _execute("/cadence gym 3", db, config, other_registry, OTHER)
    assert "gym" in reply
    assert db.get_cadence(OTHER, "gym") is None

    # OWNER can still set cadence on their own habit -- the isolation check
    # above didn't accidentally poison the habit for its real owner.
    owner_registry = provider.for_user(OWNER)
    await _execute("/cadence gym 3", db, config, owner_registry, OWNER)
    assert db.get_cadence(OWNER, "gym") == 3
    assert db.get_cadence(OTHER, "gym") is None  # still isolated


async def test_set_cadence_is_per_user_scoped_for_the_same_habit_id(db, config):
    # Both users have "stretch" (base catalog) -- each user's own cadence
    # row must be fully independent (mirrors test_v19_shared_surface.py's
    # own `get_cadence` scoping proof, exercised here through the real
    # execute_cadence write path instead of raw SQL).
    await _execute("/cadence stretch 3", db, config, DEFAULT_REGISTRY, OWNER)
    await _execute("/cadence stretch 5", db, config, DEFAULT_REGISTRY, OTHER)
    assert db.get_cadence(OWNER, "stretch") == 3
    assert db.get_cadence(OTHER, "stretch") == 5

    await _execute("/cadence stretch off", db, config, DEFAULT_REGISTRY, OWNER)
    assert db.get_cadence(OWNER, "stretch") is None
    assert db.get_cadence(OTHER, "stretch") == 5  # OTHER's own row untouched by OWNER's clear


# ===========================================================================
# Cadence applies to any habit TYPE, not just boolean -- R18-R20 never
# restrict it, and `day_qualifies`/the weekly walk are already type-generic.
# ===========================================================================


async def test_cadence_on_a_text_type_habit_is_accepted_and_counted(db, config):
    # "diary" (config.toml) is type="text", no goal -- day_qualifies falls
    # to db.count(...) > 0 for it (unchanged qualification rule).
    reply = await _execute("/cadence diary 3", db, config, DEFAULT_REGISTRY, OWNER)
    assert "3×/week" in reply
    assert db.get_cadence(OWNER, "diary") == 3

    diary = DEFAULT_REGISTRY.get("diary")
    _seed(db, "2026-08-24T09:00:00", "diary", None, raw="Monday entry")  # Mon
    _seed(db, "2026-08-26T09:00:00", "diary", None, raw="Wednesday entry")  # Wed
    done, n = weekly_progress(db, config, diary, OWNER, date(2026, 8, 26))
    assert (done, n) == (2, 3)


async def test_cadence_on_a_duration_type_habit_streak_walk_uses_week_unit(db, config):
    reply = await _execute("/cadence stretch 3", db, config, DEFAULT_REGISTRY, OWNER)
    assert "3×/week" in reply
    stretch = DEFAULT_REGISTRY.get("stretch")
    for d_str in ("2026-08-24", "2026-08-26", "2026-08-28"):  # Mon/Wed/Fri, all MET
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)
    assert compute_streak(db, config, stretch, date(2026, 8, 28), OWNER) == 1


# ===========================================================================
# /addhabit cadence=<N>w -- malformed SHAPES beyond Luna's own corpus
# ("abc"/"0w"/"8w"/"3" without the "w"). Atomicity re-verified for each:
# a bad shape must create NEITHER the habit NOR the cadence row.
# ===========================================================================


@pytest.mark.parametrize("bad", ["3d", "w3", "3.5w", "-3w", "3.0w", "3 w", "3ww"])
# NOTE: a LEADING/TRAILING-space variant like " 3w" is deliberately NOT
# included here -- `core/commands.py`'s own addhabit field parser already
# strips each pipe-delimited value before `validate_and_normalize` ever
# sees it (habitdef.py's own docstring: "stripped-but-verbatim string
# values"), so " 3w" arrives as the perfectly valid "3w" and correctly
# creates the habit -- confirmed by hand before writing this list, not a
# gap in `_CADENCE_VALUE_RE`.
async def test_addhabit_further_malformed_cadence_shapes_create_neither(db, config, provider, bad):
    base_registry = HabitRegistry.from_config(config)
    habit_id = "gymx" + str(abs(hash(bad)) % 10000)
    cmd = commands.dispatch(f"/addhabit id={habit_id}|type=boolean|en={habit_id}|cadence={bad}", provider.for_user(OWNER))
    reply = await habitdef.execute_addhabit(
        cmd, db=db, provider=provider, config=config, base_registry=base_registry, lang="en", user_id=OWNER
    )
    assert habit_id not in provider.for_user(OWNER).ids(), f"{bad!r} unexpectedly created the habit"
    assert db.get_cadence(OWNER, habit_id) is None
    assert reply == i18n.t("addhabit_invalid_cadence", "en", max=config.cadence.max_per_week)


async def test_addhabit_cadence_boundary_values_1w_and_7w_both_accepted(db, config, provider):
    base_registry = HabitRegistry.from_config(config)
    for n, habit_id in ((1, "gymlow"), (7, "gymhigh")):
        cmd = commands.dispatch(f"/addhabit id={habit_id}|type=boolean|en={habit_id}|cadence={n}w", provider.for_user(OWNER))
        await habitdef.execute_addhabit(
            cmd, db=db, provider=provider, config=config, base_registry=base_registry, lang="en", user_id=OWNER
        )
        assert habit_id in provider.for_user(OWNER).ids()
        assert db.get_cadence(OWNER, habit_id) == n


# ===========================================================================
# Weekly-walk edge: year boundary (2026 genuinely has ISO week 53, per
# `date.isocalendar()` -- Dec 28 2026 .. Jan 3 2027 -- then week 1 of 2027
# starts Jan 4). The shared-surface suite proved the DAILY walk crosses a
# year boundary correctly; this proves the WEEKLY (cadence) walk does too.
# ===========================================================================


def test_weekly_walk_crosses_the_2026_w53_to_2027_w01_year_boundary(db, config):
    gym = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    # Week 52 (2026-12-21 Mon .. 2026-12-27 Sun): MET.
    for d_str in ("2026-12-21", "2026-12-23", "2026-12-25"):
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)
    # ISO week 53 (2026-12-28 Mon .. 2027-01-03 Sun): MET, straddles the
    # calendar year boundary.
    for d_str in ("2026-12-28", "2026-12-30", "2027-01-01"):
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)
    # 2027-W01 (2027-01-04 Mon .. 01-10 Sun): current, MET by Friday.
    for d_str in ("2027-01-04", "2027-01-06", "2027-01-08"):
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)

    streak = compute_streak(db, config, gym, date(2027, 1, 8), OWNER)
    assert streak == 3, "three consecutive MET ISO weeks, unbroken across the calendar/ISO-week-53 boundary"


# ===========================================================================
# weekly_progress -- counts once per DAY (not per log), and a backfilled
# log landing in a PRIOR ISO week must not leak into the current week's
# count.
# ===========================================================================


def test_weekly_progress_counts_a_day_once_regardless_of_log_count(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    # THREE separate logs on the same Monday -- still exactly one qualifying day.
    _seed(db, "2026-08-24T07:00:00", "stretch", 5.0)
    _seed(db, "2026-08-24T12:00:00", "stretch", 5.0)
    _seed(db, "2026-08-24T18:00:00", "stretch", 5.0)
    done, n = weekly_progress(db, config, stretch, OWNER, date(2026, 8, 24))
    assert (done, n) == (1, 3)


def test_weekly_progress_ignores_a_backfilled_log_landing_in_a_prior_week(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-24T09:00:00", "stretch", 10.0)  # this week's Monday
    # A backfilled entry for the PRIOR week (inserted out of chronological
    # order -- insertion order must not matter, only the log's own `ts`).
    _seed(db, "2026-08-14T09:00:00", "stretch", 10.0)  # Friday of the prior ISO week

    done, n = weekly_progress(db, config, stretch, OWNER, date(2026, 8, 26))
    assert (done, n) == (1, 3), "the backfilled prior-week log must not count toward THIS week's progress"


def test_weekly_progress_counts_a_backfilled_log_that_lands_inside_the_current_week(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    _seed(db, "2026-08-26T09:00:00", "stretch", 10.0)  # Wednesday, logged "live"
    # Backfilled Monday of the SAME week, inserted after the Wednesday row.
    _seed(db, "2026-08-24T09:00:00", "stretch", 10.0)

    done, n = weekly_progress(db, config, stretch, OWNER, date(2026, 8, 27))
    assert (done, n) == (2, 3), "a backfilled log landing inside the current week counts normally"


# ===========================================================================
# Cross-module interop -- real write paths (not raw SQL) for both sides.
# ===========================================================================


def test_cadence_week_made_unreachable_by_a_real_pause_row_is_neutral_not_missed(db, config):
    """R4's "NEUTRAL if paused enough that fewer than N non-paused days
    remain" + R14 ("a paused date is NEUTRAL"), exercised through the REAL
    write paths of both modules: `db.set_cadence` (module `cadence`) and
    `db.insert_pause` (module `pause`) -- not the shared-surface suite's
    own raw-SQL seeding. A week entirely covered by a real pause row can
    never reach N=3, so it must be held (streak bridges across it), never
    treated as a genuine miss."""
    gym = DEFAULT_REGISTRY.get("stretch")
    db.set_cadence(OWNER, "stretch", 3)
    for d_str in ("2026-08-17", "2026-08-19", "2026-08-21"):  # week 1 MET
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)
    db.insert_pause(OWNER, "stretch", "2026-08-24", "2026-08-30")  # week 2 fully paused (real pause write)
    for d_str in ("2026-08-31", "2026-09-02", "2026-09-04"):  # week 3 MET
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)

    streak = compute_streak(db, config, gym, date(2026, 9, 4), OWNER)
    assert streak == 2, "the fully-paused week is held (NEUTRAL), bridging the two genuinely-MET weeks around it"


def test_grace_evaluate_grace_never_bridges_a_real_cadence_row(db, config):
    """R6/AC16, exercised through the REAL cross-module call
    (`grace.evaluate_grace`, module `grace`'s own entry point) against a
    cadence row written by module `cadence`'s own `db.set_cadence` -- not
    a hand-seeded stray `grace_ledger` row on the streaks module directly.
    A cadence habit with a genuine miss must never be bridged."""
    registry = HabitRegistry([DEFAULT_REGISTRY.get("stretch")])
    db.set_cadence(OWNER, "stretch", 3)
    for d_str in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22"):
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)
    # 2026-08-23 (Sun) is a genuine miss for a plain daily read, but this
    # habit carries a cadence row -- grace must never even consider it.
    result = grace.evaluate_grace(db, config, registry, OWNER, date(2026, 8, 24))
    assert result == []
    assert db.grace_protected_dates(OWNER, "stretch", "2026-08-01", "2026-08-31") == set()


# ===========================================================================
# Thai collision corpus -- beyond Luna's own four-shape proof. Two-way:
# genuine cadence phrases route to "cadence"; ordinary "กี่"/other-trigger
# prose stays "query"/its own kind, never stolen by cadence's matcher.
# ===========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "สัปดาห์นี้กี่ครั้ง",  # "this week how many times" -- reversed word order, not the cadence trigger shape
        "กี่ครั้งต่อวัน",  # "how many times per day" -- "ต่อวัน" not "ต่อสัปดาห์"
        "น้ำเท่าไหร่ต่อสัปดาห์นี้",  # เท่าไหร่ opens it, "ต่อสัปดาห์" appears mid-string, not at ^
        "ยืดเส้นกี่ครั้งต่อสัปดาห์",  # habit token BEFORE the trigger word -- doesn't match ^-anchored cadence shape
        "วิ่งไปกี่ครั้งต่อสัปดาห์แล้วนะ",  # trigger appears mid-sentence with trailing prose after the "value" position
    ],
)
def test_wider_thai_query_corpus_still_routes_to_query_not_cadence(text):
    cmd = commands.dispatch(text, DEFAULT_REGISTRY)
    assert cmd is not None and cmd.kind == "query", f"{text!r} dispatched as {cmd!r}, expected kind='query'"


@pytest.mark.parametrize(
    "text,expected_category,expected_value",
    [
        ("ต่อสัปดาห์น้ำ4", "water", 4.0),
        ("กี่ครั้งต่อสัปดาห์ น้ำ 5", "water", 5.0),
        ("ต่อสัปดาห์ไดอารี่2", "diary", 2.0),
    ],
)
def test_wider_thai_cadence_corpus_routes_correctly_for_other_habits(text, expected_category, expected_value):
    cmd = commands.dispatch(text, DEFAULT_REGISTRY)
    assert cmd == Command(kind="cadence", category=expected_category, value_num=expected_value)


def test_tor_resume_alias_vs_tor_sapda_cadence_alias_never_collide():
    """The specific boundary the cross-module interaction investigation
    flagged: `ต่อ` (resume's own Thai alias, R13) is a literal PREFIX of
    `ต่อสัปดาห์` (cadence's own Thai alias, R18). Both matchers are
    ^-anchored full-literal-string matches, not prefix tests, so they can
    never both claim the same input -- proven here directly against
    `commands.dispatch`, both directions."""
    resume_cmd = commands.dispatch("ต่อ น้ำ", DEFAULT_REGISTRY)
    assert resume_cmd is not None and resume_cmd.kind == "resume"

    cadence_cmd = commands.dispatch("ต่อสัปดาห์ น้ำ 3", DEFAULT_REGISTRY)
    assert cadence_cmd is not None and cadence_cmd.kind == "cadence"

    # A bare "ต่อ" glued directly to "สัปดาห์..." with NO space (the actual
    # cadence trigger shape) must never be misrouted to resume -- resume's
    # own Thai regex requires `\s+` immediately after "ต่อ", which "ต่อสัปดาห์"
    # (no space) can never satisfy.
    assert commands.dispatch("ต่อสัปดาห์น้ำ4", DEFAULT_REGISTRY) == Command(kind="cadence", category="water", value_num=4.0)


@pytest.mark.parametrize(
    "text",
    [
        "ต่อสัปดาห์นี้ฉันยุ่งมาก",  # ordinary prose opening with the trigger word, no resolvable habit token
        "สัปดาห์หน้าฉันจะไปเที่ยว",  # doesn't even start with the trigger
        "ทำงานต่อสัปดาห์หน้า",  # "ต่อสัปดาห์" appears mid-string, not at ^
    ],
)
def test_zero_false_positive_cadence_aliases_on_ordinary_prose(text):
    cmd = commands.dispatch(text, DEFAULT_REGISTRY)
    assert cmd is None or cmd.kind != "cadence", f"{text!r} incorrectly dispatched as cadence: {cmd!r}"


# ===========================================================================
# AC3 gate, cadence's own angle: with cadence.py fully imported/wired and
# NO habit_cadence row for a user, the daily walk is untouched -- get_cadence
# reads None, compute_streak takes the pre-v1.9 daily path, weekly_progress
# degrades to (0, 0).
# ===========================================================================


def test_no_cadence_row_daily_walk_and_weekly_progress_are_byte_identical_gate(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    for d_str in ("2026-08-17", "2026-08-18", "2026-08-19"):
        _seed(db, f"{d_str}T09:00:00", "stretch", 10.0)

    assert db.get_cadence(OWNER, "stretch") is None
    assert compute_streak(db, config, stretch, date(2026, 8, 19), OWNER) == 3  # plain daily count, unaffected
    assert weekly_progress(db, config, stretch, OWNER, date(2026, 8, 19)) == (0, 0)


async def test_dispatch_of_an_ordinary_log_message_is_unaffected_by_cadences_matchers(db, config):
    for text in ("500ml", "10 min stretch", "น้ำ 500", "ยืดเส้น 20 นาที", "ไดอารี่ วันนี้ดีมาก"):
        cmd = commands.dispatch(text, DEFAULT_REGISTRY)
        assert cmd is None or cmd.kind != "cadence", f"{text!r} incorrectly dispatched as cadence"
