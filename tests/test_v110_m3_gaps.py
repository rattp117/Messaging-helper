"""tests/test_v110_m3_gaps.py -- Vera's adversarial gap-fill pass for
SPEC-v1.10.md §4 R18/R-SS9 (module M3, pause fail-open unification),
reviewing Luna's tests/test_pause_failopen.py (IMPL-v1.10-m3.md; 12 tests
proving the FAILURE path at all 5 R18 sites, each at both the direct
builder level and the real fan-out level).

This file targets three things that suite -- and the pre-existing
tests/test_v19_release_gate.py `test_pause_gating_*_site_excludes_only_
the_paused_habit` tests (single-user, direct-builder level, all green) --
do not themselves cover:

1. The POSITIVE control at the FAN-OUT level, with a genuine multi-user
   mix in the SAME run: an ACTUALLY-paused habit for user A must still be
   suppressed after the `_safe` swap (R18's fix must not have broken
   normal gating), while users B/C (no pause) get their full,
   un-suppressed content in that same tick/job -- i.e. the fail-open
   default doesn't leak into "nothing gets suppressed for anyone".
   `checkins`/`nudge`/`reminders` are exercised through their real
   `run_due_*` fan-out; `streaks`/`review`'s fan-out lives in
   `core/jobs.py` (not an M3-owned file per SPEC-v1.10.md §11) so those
   two mirror that file's own uncaught `for user_id in ...: <single-user
   call>` loop shape, exactly matching test_pause_failopen.py's own
   documented rationale for doing the same.

2. Operator visibility (R-SS9's own "logged" contract): a pauses-read
   failure must produce an actual log record (not pass silently) at both
   `pause.is_paused_safe` and `pause.active_pauses_safe`.

3. The review site's chart-render path (`render_weekly_review_charts`):
   static grep confirms it has no separate pause-read call site of its
   own -- it inherits R18's fix purely by calling the now-fixed
   `compute_weekly_stats` internally and filtering chart pairs against
   that result. This adds the BEHAVIORAL half of that claim: a
   pauses-read failure must not break chart rendering, and the affected
   (fail-open, "not paused") habit's chart must still be produced.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest

from conftest import FakeOllamaClient
from conftest import RecordingChannel as FakeChannel

from habit_assistant.config import Config
from habit_assistant.core import checkins, commands, nudge, pause, review, streaks
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.reminders import run_due_reminders
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

USER_A = "user-a"  # the user who is ACTUALLY paused / whose pauses read fails, per test
USER_B = "user-b"
USER_C = "user-c"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())
WATER_LABEL_EN = DEFAULT_REGISTRY.get("water").label("en")


class _RaisingActivePausesDb(Database):
    """`Database`, except `active_pauses(user_id)` unconditionally raises
    for every user -- used only by the logging-visibility and
    chart-render tests below, which don't need per-user selectivity (that
    is already Luna's `_PartlyBrokenDb`'s own concern in
    tests/test_pause_failopen.py)."""

    def active_pauses(self, user_id: str):
        raise RuntimeError("simulated pauses-table read failure")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    for user_id in (USER_A, USER_B, USER_C):
        database.upsert_user(user_id, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def raising_db(tmp_path):
    database = _RaisingActivePausesDb(tmp_path / "habits.db")
    database.upsert_user("owner", role="owner", status="active")
    yield database
    database.close()


def _log(db_: Database, user_id: str, habit_id: str, value: float, ts: str = "2026-08-27T09:00:00") -> None:
    db_.insert_log(LogEntry(None, user_id, ts, habit_id, value, None, f"{value}", "reply"))


async def _enable_checkin(db_: Database, config: Config, user_id: str) -> None:
    await checkins.execute_checkin(
        commands.dispatch("/checkin on", DEFAULT_REGISTRY), db=db_, config=config, lang="en", user_id=user_id
    )


# ===========================================================================
# 1. Positive control at the FAN-OUT level, multi-user mix in one run.
# ===========================================================================


async def test_run_due_reminders_actually_paused_user_suppressed_others_unaffected(db):
    config = Config()
    water = DEFAULT_REGISTRY.get("water")
    hour, minute = (int(x) for x in water.reminder_times[0].split(":"))
    clock = lambda: datetime(2026, 8, 27, hour, minute, 0)  # noqa: E731
    db.insert_pause(USER_A, "water", "2026-08-27", "2026-08-27")
    channel = FakeChannel()

    await run_due_reminders(channel, config, DEFAULT_REGISTRY, db, clock=clock)

    assert channel.sent_to(USER_A) == [], "an ACTUALLY-paused habit's reminder must still be suppressed (R18 must not have broken normal gating)"
    assert channel.sent_to(USER_B) != [], "an unrelated, non-paused user must still get their reminder in the same tick"
    assert channel.sent_to(USER_C) != []


async def test_run_due_checkins_actually_paused_habit_excluded_others_full(db):
    # `run_due_checkins` resolves each user's language via
    # `i18n.resolve_unprompted_language` (default Thai, ROADMAP.md v0.6.0
    # AC6.3) -- force English so the content assertions below (which check
    # for the English habit label) are meaningful regardless of that
    # per-user resolution.
    config = Config.model_validate({"i18n": {"language": "en"}})
    for user_id in (USER_A, USER_B, USER_C):
        await _enable_checkin(db, config, user_id)
    db.insert_pause(USER_A, "water", "2026-08-27", "2026-08-27")
    channel = FakeChannel()

    await checkins.run_due_checkins(channel, config, DEFAULT_REGISTRY, db, clock=lambda: datetime(2026, 8, 27, 9, 0, 0))

    a_msgs = channel.sent_to(USER_A)
    assert a_msgs, "A must still get a check-in for their other, non-paused habits"
    assert all(WATER_LABEL_EN.lower() not in m.lower() for m in a_msgs), "the paused habit must not appear in A's check-in"
    assert any(WATER_LABEL_EN.lower() in m.lower() for m in channel.sent_to(USER_B)), "B (not paused) must still see water"
    assert any(WATER_LABEL_EN.lower() in m.lower() for m in channel.sent_to(USER_C)), "C (not paused) must still see water"


async def test_run_due_nudges_actually_paused_habit_excluded_others_full(db):
    config = Config()
    for user_id in (USER_A, USER_B, USER_C):
        await _enable_checkin(db, config, user_id)
        db.set_checkin_window(user_id, "08:00-20:00")  # nudge rides check-in enablement
        _log(db, user_id, "water", 2400.0, ts="2026-08-27T08:00:00")  # 2400/2500 = 96% -- close
    db.insert_pause(USER_A, "water", "2026-08-27", "2026-08-27")
    channel = FakeChannel()

    await nudge.run_due_nudges(channel, config, DEFAULT_REGISTRY, db, clock=lambda: datetime(2026, 8, 27, 20, 0, 0))

    assert channel.sent_to(USER_A) == [], "A's only close habit is the paused one -- must get no nudge at all"
    assert channel.sent_to(USER_B) != [], "B (not paused, same close habit) must still get their nudge"
    assert channel.sent_to(USER_C) != []


def test_compute_daily_summary_actually_paused_habit_excluded_others_full_fanout(db):
    """Mirrors core/jobs.py:daily_summary_job's own uncaught
    `for user_id in db.active_user_ids(): streaks.run_daily_summary(...)`
    loop shape, same rationale as test_pause_failopen.py's own site-4
    fan-out test."""
    config = Config()
    today = date(2026, 8, 27)
    db.insert_pause(USER_A, "water", today.isoformat(), today.isoformat())
    for user_id in (USER_A, USER_B, USER_C):
        _log(db, user_id, "water", 500.0, ts=f"{today.isoformat()}T09:00:00")

    results = {}
    for user_id in (USER_A, USER_B, USER_C):
        results[user_id] = streaks.compute_daily_summary(db, config, DEFAULT_REGISTRY, today, user_id)

    assert "water" not in [line.habit.id for line in results[USER_A]], "A's actually-paused habit must not get a line"
    assert "water" in [line.habit.id for line in results[USER_B]], "B (not paused) must still get their line"
    assert "water" in [line.habit.id for line in results[USER_C]]


def test_compute_weekly_stats_actually_paused_habit_excluded_others_full_fanout(db):
    """Mirrors core/jobs.py:weekly_review_job's own uncaught fan-out loop
    shape, same rationale as test_pause_failopen.py's own site-5 test."""
    config = Config()
    end_date = date(2026, 8, 27)
    db.insert_pause(USER_A, "water", end_date.isoformat(), end_date.isoformat())
    for user_id in (USER_A, USER_B, USER_C):
        _log(db, user_id, "water", 500.0, ts=f"{end_date.isoformat()}T09:00:00")

    results = {}
    for user_id in (USER_A, USER_B, USER_C):
        results[user_id] = review.compute_weekly_stats(db, config, DEFAULT_REGISTRY, end_date, user_id)

    assert "water" not in [hs.habit.id for hs in results[USER_A].habits], "A's actually-paused habit must be excluded"
    assert "water" in [hs.habit.id for hs in results[USER_B].habits]
    assert "water" in [hs.habit.id for hs in results[USER_C].habits]


# ===========================================================================
# 2. Operator visibility -- a pauses-read failure must be logged, not
#    silent (SPEC-v1.10.md R-SS9's own "logged" contract).
# ===========================================================================


def test_is_paused_safe_logs_on_read_failure(caplog):
    class _RaisingDb:
        def active_pauses(self, user_id):
            raise RuntimeError("simulated pauses-table read failure")

    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.pause"):
        result = pause.is_paused_safe(_RaisingDb(), Config(), "owner", "water", date(2026, 8, 27))

    assert result is False
    assert any(record.levelno >= logging.ERROR for record in caplog.records), "a pauses-read failure must be logged (R-SS9), not pass silently"
    assert any(record.exc_info for record in caplog.records), "the log record should carry the exception (logger.exception / traceback), for operator visibility"


def test_active_pauses_safe_logs_on_read_failure(caplog):
    class _RaisingDb:
        def active_pauses(self, user_id):
            raise RuntimeError("simulated pauses-table read failure")

    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.pause"):
        result = pause.active_pauses_safe(_RaisingDb(), "owner")

    assert result == []
    assert any(record.levelno >= logging.ERROR for record in caplog.records), "a pauses-read failure must be logged (R-SS9), not pass silently"
    assert any(record.exc_info for record in caplog.records)


def test_checkins_site_pause_read_failure_is_logged_end_to_end(raising_db, caplog):
    """The logging behavior isn't just a `pause.py`-unit-test claim -- it
    must actually fire when reached through the real M3-owned call site
    (`checkins.build_checkin_message` -> `pause.active_pauses_safe`)."""
    config = Config()
    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.pause"):
        message = checkins.build_checkin_message(
            raising_db, config, DEFAULT_REGISTRY, "en", "owner", clock=lambda: datetime(2026, 8, 27, 9, 0, 0)
        )

    assert message is not None
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_review_site_pause_read_failure_is_logged_end_to_end(raising_db, caplog):
    """Same end-to-end check for the `review.compute_weekly_stats` ->
    `pause.is_paused_safe` call site."""
    config = Config()
    with caplog.at_level(logging.ERROR, logger="habit_assistant.core.pause"):
        stats = review.compute_weekly_stats(raising_db, config, DEFAULT_REGISTRY, date(2026, 8, 27), "owner")

    assert stats.habits
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


# ===========================================================================
# 3. Review site subtlety -- the chart-render path
#    (`review.render_weekly_review_charts`) must survive a pauses-read
#    failure too, even though it has no pause-read call site of its own
#    (grep-confirmed: it inherits the fix by calling the now-fixed
#    `compute_weekly_stats` internally).
# ===========================================================================


def test_render_weekly_review_charts_survives_pause_read_failure(raising_db):
    config = Config()
    end_date = date(2026, 8, 27)
    for offset in range(6, -1, -1):
        d = (end_date - timedelta(days=offset)).isoformat()
        _log(raising_db, "owner", "water", 500.0, ts=f"{d}T09:00:00")

    pairs = review.render_weekly_review_charts(raising_db, config, DEFAULT_REGISTRY, "en", "owner", today=end_date)

    assert pairs, "a pauses-read failure must not prevent the (fail-open, not-paused) habit's chart from rendering"
    assert any(b"PNG" in image[:16] or image[:8] == b"\x89PNG\r\n\x1a\n" for image, _caption in pairs)
