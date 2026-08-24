"""Adversarial gap coverage for SPEC-v1.6.md §4 Feature 5 ("Almost there"
end-of-day nudge, module `nudge`, R-N1-R-N3) on top of `tests/test_nudge.py`
(Luna's own 32 tests). Owned ACs: AC-N1, AC-N2, AC-N3.

Scope, per Archi's dispatch: enablement gating (OQ2) precision, threshold
float/edge precision, mid-day target-override changes, clock/timezone
edges, DND boundary edges, fail-open fan-out, and the Thai-default-for-
unprompted-sends posture. Mirrors `tests/test_nudge.py`'s own fixtures/
conventions (`FakeChannel`, injectable `clock`, real on-disk SQLite
`Database`) plus `tests/test_announce_gaps.py`'s `FakeChannel(fail_for=...)`
pattern for the fail-open fan-out tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, nudge, i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.preferences import execute_lang
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"
THIRD = "third-chat-c"

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


class RaisingForChannel(Channel):
    """Mirrors `tests/test_announce_gaps.py::FakeChannel(fail_for=...)` --
    raises on `send` for a configured set of recipients, records every
    other send. Used to prove (or disprove) fail-open fan-out (SPEC-v1.6.md
    §3.4: "the nudge never raise[s]")."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_for = fail_for or set()

    async def send(self, chat_id: str, text: str) -> None:
        if chat_id in self._fail_for:
            raise RuntimeError(f"simulated send failure for {chat_id}")
        self.sent.append((chat_id, text))

    async def run(self, on_message, on_callback=None) -> None:
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
    database = Database(tmp_path / "nudge_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    database.upsert_user(THIRD, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _fixed_clock(y=2026, m=8, d=24, hh=20, mm=0, ss=0):
    return lambda: datetime(y, m, d, hh, mm, ss)


async def _enable_checkin(db, config, user_id: str, window: str | None = None) -> None:
    tail = "on" if window is None else window
    await checkins.execute_checkin(
        commands.dispatch(f"/checkin {tail}", DEFAULT_REGISTRY), db=db, config=config, lang="en", user_id=user_id
    )


def _log(db, user_id: str, habit_id: str, value: float, ts: str = "2026-08-24T09:00:00") -> None:
    db.insert_log(LogEntry(None, user_id, ts, habit_id, value, None, f"{value}", "reply"))


# Deliberately NOT id "water" -- see tests/test_nudge.py's own comment:
# `targets.config_goal` special-cases "water" to the legacy config default.
JUICE = HabitRegistry(
    [_habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล.")]
)


# ===========================================================================
# Enablement gating (OQ2) -- explicit "pre-v1.5 NULL" audit.
# ===========================================================================


async def test_pre_v15_null_checkin_state_never_nudges(db, config):
    """A user who has never called /checkin at all has a raw NULL
    `checkin_window` column (migration 008's own no-backfill default --
    same shape a pre-v1.5 row would carry forward as). `effective_checkin`
    resolves NULL -> inherit `config.checkin.enabled` (ships False) ->
    disabled. Explicitly asserts the raw NULL state first so this is
    provably the "never touched /checkin" case, not just "off by
    default"."""
    assert db.get_checkin_window(OWNER) is None  # raw pre-v1.5-shaped state
    _log(db, OWNER, "water", 2000.0)  # 80% of the default 2500ml goal -- otherwise squarely "close"

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock())
    assert channel.sent_to(OWNER) == []


# ===========================================================================
# Threshold precision -- float goals, threshold_pct edge values.
# ===========================================================================


def test_threshold_boundary_just_under_80_percent_with_fractional_goal_is_not_close(db, config):
    """goal=750.5 -> 80% = 600.4. total=600.3 is a hair under -- must NOT
    be close (cross-multiplied comparison, not a total/goal division, so
    this must resolve exactly, no float-precision wobble)."""
    registry = HabitRegistry([_habit("juice", "numeric", goal=750.5, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล.")])
    _log(db, OWNER, "juice", 600.3)
    assert nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=_fixed_clock()) is None


def test_threshold_boundary_exactly_80_percent_with_fractional_goal_is_close(db, config):
    """Same fractional goal, total=600.4 -- exactly the 80% boundary,
    inclusive per R-N1's own ">=" wording."""
    registry = HabitRegistry([_habit("juice", "numeric", goal=750.5, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล.")])
    _log(db, OWNER, "juice", 600.4)
    assert nudge.build_nudge_message(db, config, registry, "en", OWNER, clock=_fixed_clock()) is not None


def test_threshold_pct_100_config_means_nothing_is_ever_close(db):
    """threshold_pct=100 (the valid upper edge, per NudgeConfig's own 1-100
    validator) is a degenerate-but-legal config: "close" requires total*100
    >= 100*goal (i.e. total >= goal), but build_nudge_message's own earlier
    `total >= goal` branch already treats that as "met" and excludes it
    first. So threshold_pct=100 makes the nudge permanently silent for
    every user, even a habit logged to 99.99% of goal -- a real, testable
    behavioral consequence of the config value, not just a boundary
    number."""
    config = Config.model_validate({"nudge": {"threshold_pct": 100, "time": "20:00"}})
    _log(db, OWNER, "juice", 999.99)  # 99.999% -- as close as a habit can get without being "met"
    assert nudge.build_nudge_message(db, config, JUICE, "en", OWNER, clock=_fixed_clock()) is None


def test_threshold_pct_0_is_rejected_by_config_validation():
    """NudgeConfig's own validator requires `0 < v <= 100` -- 0 is invalid
    (not "everything is always close"), confirming the config edge doesn't
    silently produce a nag-everything mode."""
    with pytest.raises(Exception):
        Config.model_validate({"nudge": {"threshold_pct": 0, "time": "20:00"}})


# ===========================================================================
# Target override changed mid-day -- nudge must use the goal AT SEND TIME.
# ===========================================================================


async def test_target_override_changed_mid_day_uses_the_goal_at_nudge_time(db, config):
    """Set an early-morning override, log against it, then change the
    override again before the nudge fires. `db.get_target` reads live (no
    caching), and `build_nudge_message` calls `targets.effective_goal`
    fresh on every invocation -- so the 20:00 nudge must reflect the LATEST
    override, not whatever was in effect when the log was written. Proven
    by a total that would already be "met" under the stale goal (500) but
    is "close" (not yet met) under the current one (1000) -- the two goals
    produce opposite nudge outcomes, so this isn't just checking a number
    changed, it's checking behavior flips correctly."""
    db.set_target(OWNER, "water", 500.0)  # morning override
    _log(db, OWNER, "water", 850.0, ts="2026-08-24T09:00:00")  # 850 >= 500 -- "met" under the stale goal
    db.set_target(OWNER, "water", 1000.0)  # changed again before end of day

    message = nudge.build_nudge_message(db, config, DEFAULT_REGISTRY, "en", OWNER, clock=_fixed_clock())
    assert message is not None  # 850/1000 = 85% -- close under the CURRENT goal
    assert "150" in message  # remaining = 1000 - 850


# ===========================================================================
# Clock/timezone edges.
# ===========================================================================


async def test_custom_app_timezone_is_honored_not_hardcoded_bangkok(db):
    """config.app.timezone defaults to Asia/Bangkok, but the nudge tick
    must convert against WHATEVER timezone is configured, not a hardcoded
    one. America/New_York is UTC-4 in August (EDT, no DST transition on
    this date) -- 2026-08-25T00:00:00 UTC == 2026-08-24T20:00:00 EDT, the
    configured nudge time in that timezone."""
    config = Config.model_validate({"app": {"timezone": "America/New_York"}, "nudge": {"threshold_pct": 80, "time": "20:00"}})
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)  # 80% of the default 2500ml goal

    utc_clock = lambda: datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)
    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=utc_clock)
    assert channel.sent_to(OWNER) != []


async def test_custom_app_timezone_does_not_fire_at_the_bangkok_equivalent_minute(db):
    """Negative counterpart to the above -- 20:00 UTC (== 20:00 Bangkok, the
    DEFAULT timezone) must NOT fire once the app timezone is reconfigured
    to America/New_York, where 20:00 UTC is only 16:00 local."""
    config = Config.model_validate({"app": {"timezone": "America/New_York"}, "nudge": {"threshold_pct": 80, "time": "20:00"}})
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)

    utc_clock = lambda: datetime(2026, 8, 24, 20, 0, 0, tzinfo=timezone.utc)
    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=utc_clock)
    assert channel.sent_to(OWNER) == []


async def test_nonzero_seconds_within_the_due_minute_still_fires(db, config):
    """The real scheduler job fires on `CronTrigger(second=0, ...)`, but
    `run_due_nudges`'s own guard compares HH:MM only (`_now_hhmm` strftimes
    away the seconds) -- a clock with nonzero seconds inside the due minute
    (e.g. a slightly-delayed tick) must still be treated as due, not
    skipped by an accidental exact-second comparison."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0, ss=45))
    assert channel.sent_to(OWNER) != []


# ===========================================================================
# DND boundary edges -- [start, end) exact-boundary + midnight-crossing.
# ===========================================================================


async def test_dnd_window_ending_exactly_at_20_00_does_not_suppress(db, config):
    """`_in_quiet_hours` uses a half-open `[start, end)` window -- a DND
    window that ENDS at 20:00 must not cover 20:00 itself (end exclusive)."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)
    db.set_user_quiet_hours(OWNER, '[["19:00", "20:00"]]')

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []


async def test_dnd_window_starting_exactly_at_20_00_suppresses(db, config):
    """A DND window that STARTS at 20:00 covers 20:00 (start inclusive)."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)
    db.set_user_quiet_hours(OWNER, '[["20:00", "21:00"]]')

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_midnight_crossing_dnd_window_covering_20_00_suppresses(db, config):
    """A midnight-crossing window (start > end) is "now >= start OR now <
    end" -- ["18:00", "06:00"] spans the evening through the next morning
    and covers 20:00."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)
    db.set_user_quiet_hours(OWNER, '[["18:00", "06:00"]]')

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) == []


async def test_midnight_crossing_dnd_window_excluding_20_00_does_not_suppress(db, config):
    """A midnight-crossing window that does NOT reach back to 20:00 --
    ["22:00", "06:00"] -- must not suppress the 20:00 nudge."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)
    db.set_user_quiet_hours(OWNER, '[["22:00", "06:00"]]')

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert channel.sent_to(OWNER) != []


# ===========================================================================
# Fail-open fan-out (SPEC-v1.6.md §3.4: "the nudge never raise[s]").
# ===========================================================================


async def test_fail_open_fan_out_one_users_send_failure_does_not_block_the_others(db, config):
    """3 active users (OWNER, MEMBER, THIRD, in that creation/fan-out
    order), all check-in-enabled and all squarely close. The channel raises
    on `send` for MEMBER only. Per SPEC-v1.6.md §3.4 ("the nudge never
    raise[s]") and the fail-open-fan-out discipline every other minutely
    tick in this codebase follows (e.g. `announce.announce_release`),
    OWNER and THIRD -- on either side of the failing user in fan-out order
    -- must still be nudged, and `run_due_nudges` itself must not raise."""
    for uid in (OWNER, MEMBER, THIRD):
        await _enable_checkin(db, config, uid)
        _log(db, uid, "water", 2000.0)  # 80% of the default 2500ml goal for all three

    channel = RaisingForChannel(fail_for={MEMBER})
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))

    assert channel.sent_to(OWNER) != [], "OWNER (before the failing user) should still be nudged"
    assert channel.sent_to(THIRD) != [], "THIRD (after the failing user) should still be nudged"


async def test_a_failed_send_is_not_retried_later_the_same_day(db, config):
    """Post-fix residual check: `run_due_nudges` persists no per-user
    "already sent today" state anywhere (unlike e.g. `announce_release`,
    which leaves a failed user UNMARKED specifically so a later run can
    retry them) -- the ONLY thing that makes this "once/day" is R-N1's
    fixed-minute guard (`hhmm != config.nudge.time`). So a user whose
    send failed at the due minute must NOT receive a leftover/queued
    send at the very next minute (or any other minute) that same day --
    they simply get nothing further until tomorrow's fixed minute. This
    also re-confirms the fixed-minute guard itself still short-circuits
    BEFORE the per-user loop even runs (no stray retry-queue, no partial
    state to drain), so a same-day re-tick is a true no-op, not a delayed
    resend."""
    await _enable_checkin(db, config, OWNER)
    _log(db, OWNER, "water", 2000.0)  # 80% of the default 2500ml goal -- squarely close

    failing_channel = RaisingForChannel(fail_for={OWNER})
    await nudge.run_due_nudges(failing_channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    assert failing_channel.sent_to(OWNER) == []  # the send genuinely failed, not silently succeeded

    # Next tick, one minute later, same day, channel now healthy: the
    # fixed-minute guard alone (not any "already attempted" state) governs
    # whether this fires again -- 20:01 is no longer the due minute, so
    # nothing is sent, even though the earlier attempt for this exact
    # habit/day never actually succeeded.
    retry_channel = FakeChannel()
    await nudge.run_due_nudges(retry_channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=1))
    assert retry_channel.sent_to(OWNER) == []


# ===========================================================================
# Bilingual: Thai-default posture for a user who never set /lang at all.
# ===========================================================================


async def test_unprompted_send_defaults_to_thai_when_no_lang_pref_was_ever_set(db, config):
    """`i18n.resolve_unprompted_language` defaults to Thai for a user with
    no stored preference (this codebase's documented default for every
    unprompted send). Unlike `tests/test_nudge.py`'s own bilingual tests
    (which explicitly call `/lang en`/`/lang th` first, or pass a `lang`
    directly into `build_nudge_message`), this exercises `run_due_nudges`
    end-to-end for a user who never touched `/lang` at all."""
    await _enable_checkin(db, config, OWNER)  # never calls execute_lang
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=_fixed_clock(hh=20, mm=0))
    sent = channel.sent_to(OWNER)
    assert len(sent) == 1
    assert i18n.detect_language(sent[0]) == "th"


# ===========================================================================
# Interplay: both `run_due_nudges` and `run_due_checkins` fire at 20:00 for
# an enabled user -- explicit proof neither one is skipped/suppressed by
# the other via any shared per-tick state (they are independent calls).
# ===========================================================================


async def test_both_ticks_independently_reach_a_send_at_20_00_with_default_window(db, config):
    """Belt-and-suspenders re-confirmation of tests/test_nudge.py's own
    interplay test, but asserting message COUNT and CONTENT disjointness
    explicitly (no shared/leaked state between the two tick functions)."""
    await _enable_checkin(db, config, OWNER)  # default window 08:00-20:00, covers 20:00 inclusively
    await execute_lang(commands.dispatch("/lang en", DEFAULT_REGISTRY), db=db, lang="en", user_id=OWNER)
    _log(db, OWNER, "water", 2000.0)

    channel = FakeChannel()
    clock = _fixed_clock(hh=20, mm=0)
    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=clock)
    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=clock)

    sent = channel.sent_to(OWNER)
    assert len(sent) == 2
    nudge_msgs = [m for m in sent if i18n.t("nudge_header", "en") in m]
    checkin_msgs = [m for m in sent if i18n.t("checkin_header", "en") in m]
    assert len(nudge_msgs) == 1
    assert len(checkin_msgs) == 1
