"""Vera (tester) gap-pass for the living-streak display fix (line/v1.3.2,
IMPL-STREAK-DISPLAY.md): focused verification of `core/streaks.py:
display_streak` and the five switched call sites (`core/dashboard.py`,
`core/discoverability.py`, `core/streaks.py:compute_daily_summary`,
`core/wrapped.py`, `core/portal/users.py`), with special attention to the
KEEP/SWITCH boundary -- every exactness-sensitive caller (`crossed_
milestone`, `records.py`'s longest-streak tracking, `review.py`'s duration
streak, `grace.py`/`digest.py:_grace_bridged`'s own re-derivation) must stay
governed by `compute_streak`'s exact 0-for-not-yet-met-today contract,
completely unaffected by `display_streak`'s living/optimistic number used
elsewhere for the same day's same data.

`tests/test_streaks.py`'s own 8 new tests already cover the core truth
table (today-met, today-pending/live-scenario, real-gap, grace-bridged
yesterday, paused-today x2, cadence pass-through, cadence negative
control) directly against `display_streak`; `tests/test_dashboard.py`/
`test_digest.py`/`test_discoverability.py`/`test_wrapped.py`/
`test_portal_users_gaps.py` each add one regression test proving the fix
reaches their own switched surface -- 13 new tests total across the six
files, all read directly. This file does NOT duplicate any of those; it
probes what they don't:

1. The user's exact live scenario end-to-end through the REAL wired LINE
   webhook reply flow (not a direct function call) -- dashboard-in-reply
   included, with the milestone-crossing keep/switch boundary pinned in
   the SAME test.
2. Truth-table extremes not in the existing 8: first-ever log day, a
   grace+pending combo and a paused+met combo each re-verified through a
   real CALLER (`dashboard.render`), an honest-double-miss zero, and the
   cadence Monday negative/positive control re-verified independently
   through `dashboard.render`'s own cadence branch rather than the raw
   function.
3. Boundary audits: `records.py`'s stored `longest_streak` stays governed
   by `compute_streak`'s exact contract even when `display_streak` would
   report a higher living number for the same data; `crossed_milestone`
   is provably unaffected by any number of intervening `display_streak`
   calls; `review.py`'s duration streak stays exact, not living.
4. Timezone/clock discipline across every switched caller's own "today"
   resolution.

No production code is modified by this file. Live-environment rule:
every DB here is a scratch `tmp_path` SQLite file; the one end-to-end test
uses the same REAL wired `_running_line_app`/`_post_events` webhook harness
`tests/test_line_integration.py` already established (imported from there,
same convention `test_line_release_gate.py`/`test_portal_integration.py`
already use to reuse that harness rather than duplicating it) -- no mocks
for anything that doesn't involve a paid/external API."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from habit_assistant.config import Config
from habit_assistant.core import dashboard, discoverability, records, review, streaks, timeutil
from habit_assistant.core.grace import _period_key
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry
from test_line_integration import (
    MEMBER as LINE_MEMBER,
    OWNER as LINE_OWNER,
    _post_events,
    _running_line_app,
    _text_event,
    _wait_until,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_streaks.py's own copies -- each test
# file keeps its own per this codebase's convention, see that file's own
# docstring).
# ---------------------------------------------------------------------------

OWNER = "owner"


def _seed(db: Database, ts: str, category: str, value_num: float | None, raw: str = "x", user_id: str = OWNER) -> int:
    entry = LogEntry(None, user_id, ts, category, value_num, None, raw, "reply")
    return db.insert_log(entry)


def _synthetic_habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_,
        label_th=id_,
        unit_en="ml" if type_ in ("numeric", "duration") else None,
        unit_th="มล." if type_ in ("numeric", "duration") else None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.upsert_user(OWNER, role="owner", status="active")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    dashboard._last_rendered.clear()
    yield
    dashboard._last_rendered.clear()


def _config() -> Config:
    return Config()


# ===========================================================================
# 1. The user's exact live scenario, end to end, through the REAL wired
#    webhook -> routing -> confirmation+dashboard-in-reply flow -- the
#    keep/switch boundary (dashboard SWITCHED vs milestone-crossing KEPT)
#    proven in one test, not two.
# ===========================================================================


async def test_e2e_webhook_reply_shows_living_streak_then_milestone_fires_exactly_at_crossing(monkeypatch, tmp_path):
    """Goal 2500 (water's real default), met on D-2 and D-1, today logged
    but only 1250/2500 so far: the confirmation+dashboard-in-reply
    (SPEC-LINE-1.2.md R-A1, default ON) must show "streak 2d" for water,
    not "streak 0d" -- reproducing IMPL-STREAK-DISPLAY.md's own root-cause
    scenario through the REAL webhook, not a direct function call. A
    second log the same day crosses 2500 for real: the dashboard row must
    now read "streak 3d" AND -- the keep/switch boundary, in the SAME
    test -- the milestone-crossing suffix (KEPT on `compute_streak`,
    unaffected by this patch) must fire exactly on THIS log's confirmation
    (3 is a default milestone), never on the first, still-partial log."""
    async with _running_line_app(monkeypatch, tmp_path) as app:
        today = datetime.now().date()
        d2 = (today - timedelta(days=2)).isoformat()
        d1 = (today - timedelta(days=1)).isoformat()
        app.db.insert_log(LogEntry(None, LINE_MEMBER, f"{d2}T09:00:00", "water", 2500.0, None, "2500ml", "reply"))
        app.db.insert_log(LogEntry(None, LINE_MEMBER, f"{d1}T09:00:00", "water", 2500.0, None, "2500ml", "reply"))

        # First log today: partial, below goal. The reply's trailing
        # dashboard-in-reply board must show the LIVING streak (2), and
        # must carry NO milestone line -- today doesn't qualify yet, so
        # `crossed_milestone` (KEPT, `compute_streak`-backed) correctly
        # has nothing to report.
        resp = await _post_events(app.port, [_text_event(LINE_MEMBER, "1250ml", reply_token="rt-partial")])
        assert resp.status_code == 200
        replies = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-partial"] or None
        )
        (reply_body,) = replies
        messages = reply_body["messages"]
        assert len(messages) == 2, "confirmation + trailing dashboard board (dashboard-in-reply, default ON)"
        confirmation_text = messages[0]["text"]
        board_text = messages[-1]["text"]
        assert "-day water streak" not in confirmation_text, (
            f"milestone must NOT fire on a still-partial log, got: {confirmation_text!r}"
        )
        water_line = next(line for line in board_text.splitlines() if "water" in line.lower() or "น้ำ" in line)
        assert "streak 2d" in water_line, f"expected the living (fallback-to-yesterday) streak, got: {water_line!r}"

        # Second log today: crosses 2500 for real. Now `compute_streak
        # (today)` itself becomes 3 (D-2, D-1, today all qualify) -- the
        # dashboard board must show "streak 3d" (display_streak is a pure
        # pass-through once today itself qualifies), AND the milestone
        # suffix (3 is in the default [3, 7, 30] list) must fire on THIS
        # confirmation, not the previous one.
        resp2 = await _post_events(app.port, [_text_event(LINE_MEMBER, "1250ml", reply_token="rt-cross")])
        assert resp2.status_code == 200
        replies2 = await _wait_until(
            lambda: [b for b in app.api.calls_matching("/message/reply") if b["replyToken"] == "rt-cross"] or None
        )
        (reply_body2,) = replies2
        messages2 = reply_body2["messages"]
        assert len(messages2) == 2
        confirmation_text2 = messages2[0]["text"]
        board_text2 = messages2[-1]["text"]
        assert "3-day water streak" in confirmation_text2, (
            f"expected the milestone line to fire exactly at the crossing, got: {confirmation_text2!r}"
        )
        water_line2 = next(line for line in board_text2.splitlines() if "water" in line.lower() or "น้ำ" in line)
        assert "streak 3d" in water_line2

        assert app.db.sum_value(LINE_MEMBER, "water", today.isoformat()) == 2500.0
        assert app.api.calls_matching("/message/push") == []


# ===========================================================================
# 2. Truth-table extremes the existing 8 tests don't cover.
# ===========================================================================


def test_display_streak_first_ever_log_day_qualified_no_history_shows_one(db):
    """No prior history at all -- today's own first-ever log already meets
    goal. `compute_streak(today)` is naturally 1 (nothing before it to
    walk into), so `display_streak` is a trivial pass-through -- but this
    is the true empty-history boundary the existing 8 cases (which all
    seed at least one prior day) never exercise."""
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = _config()
    today = date(2026, 8, 24)
    _seed(db, f"{today.isoformat()}T09:00:00", "juice", 1000.0)

    assert streaks.compute_streak(db, config, juice, today, OWNER) == 1
    assert streaks.display_streak(db, config, juice, today, OWNER) == 1


def test_dashboard_render_yesterday_grace_bridged_today_pending_shows_unbroken_streak(db):
    """Re-verified through the REAL caller (`dashboard.render`), not the
    raw function directly (`tests/test_streaks.py`'s own grace test only
    calls `display_streak` itself): two real qualifying days, then a
    grace-bridged yesterday (held, not broken), then today still pending
    (no log yet). The live dashboard row must show the unbroken 2, not a
    false 0 -- and must NOT show any pause/grace marker of its own
    (grace's marker lives in `/habits`' `grace_status_line`, a separate
    surface from the dashboard row)."""
    juice = _synthetic_habit("juice", "numeric", goal=1000, label_en="juice", unit_en="ml")
    registry = HabitRegistry([juice])
    config = _config()
    today = date(2026, 8, 24)
    yesterday = today - timedelta(days=1)
    _seed(db, f"{(today - timedelta(days=3)).isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "juice", 1000.0)
    db.record_grace(OWNER, "juice", yesterday.isoformat(), _period_key(yesterday))
    # today: pending, no log yet.

    clock = lambda: datetime(today.year, today.month, today.day, 9, 0, 0)
    text = dashboard.render(db, config, registry, "en", OWNER, clock)
    line = next(ln for ln in text.splitlines() if "juice" in ln)
    assert "streak 2d" in line, f"expected the grace-held, unbroken streak, got: {line!r}"


def test_dashboard_render_paused_today_met_yesterday_pins_streak_and_pause_marker_coherently(db):
    """Re-verified through `dashboard.render` (her documented truth table
    says a paused today with a real streak behind it shows the HELD count
    directly, never reaching the fallback branch): the live row must show
    BOTH the true held streak (2, not 0) AND the pause marker on the same
    line -- a coherent pin, not a false "0 + paused" that would read as
    the streak having died."""
    juice = _synthetic_habit("juice", "numeric", goal=1000, label_en="juice", unit_en="ml")
    registry = HabitRegistry([juice])
    config = _config()
    today = date(2026, 8, 24)
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "juice", 1000.0)
    db.insert_pause(OWNER, "juice", today.isoformat(), today.isoformat())

    clock = lambda: datetime(today.year, today.month, today.day, 9, 0, 0)
    text = dashboard.render(db, config, registry, "en", OWNER, clock)
    line = next(ln for ln in text.splitlines() if "juice" in ln)
    assert "streak 2d" in line, f"expected the held streak shown directly (never reaches the fallback), got: {line!r}"
    assert "paused until" in line and today.isoformat() in line, f"expected a coherent pause marker too, got: {line!r}"


def test_display_streak_yesterday_and_today_both_genuinely_unqualified_shows_honest_zero(db):
    """Not every day with a LOG is a qualifying day: both yesterday and
    today have real entries, but both are below goal (no pause, no
    grace) -- a genuine double-miss must still show 0, distinguishing
    "logged but short" from "not logged yet" (the actual bug's own
    ambiguity)."""
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = _config()
    today = date(2026, 8, 24)
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "juice", 1000.0)  # an old, already-broken run
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "juice", 400.0)  # logged, but short
    _seed(db, f"{today.isoformat()}T09:00:00", "juice", 300.0)  # logged, but short

    assert streaks.display_streak(db, config, juice, today, OWNER) == 0


def test_dashboard_render_cadence_monday_last_week_failed_does_not_resurrect_streak(db):
    """Re-verified independently through `dashboard.render`'s own cadence
    branch (`tests/test_streaks.py`'s negative control only calls
    `display_streak` directly) -- per_week=2, last week genuinely missed
    its quota, today is the Monday that starts a brand-new week. The live
    dashboard row must show "weekly streak 0 week(s)", never a wrongly
    resurrected 1 from a naive yesterday-fallback re-anchoring the walk
    onto last week's own Sunday."""
    gym = _synthetic_habit("gym", "boolean", label_en="gym")
    registry = HabitRegistry([gym])
    config = _config()
    db.set_cadence(OWNER, "gym", 2)
    monday = date(2026, 8, 24)  # brand-new week, 0 days logged yet

    _seed(db, "2026-08-10T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-11T09:00:00", "gym", 1.0)
    # 2026-08-17..23 (last week): genuinely missed, 0 qualifying days.

    clock = lambda: datetime(monday.year, monday.month, monday.day, 9, 0, 0)
    text = dashboard.render(db, config, registry, "en", OWNER, clock)
    line = next(ln for ln in text.splitlines() if "gym" in ln)
    assert "weekly streak 0 week" in line, f"must not resurrect the already-broken streak, got: {line!r}"


def test_dashboard_render_cadence_monday_last_week_met_passes_through_living_value(db):
    """The positive contrast to the test above, same re-verification path:
    per_week=2, the two most recent COMPLETED weeks both met quota, today
    is Monday of a brand-new (0-logged) week. `display_streak` is a pure
    pass-through for cadence -- the live row must show the full 2-week
    running streak, unaffected by today's own still-open week."""
    gym = _synthetic_habit("gym", "boolean", label_en="gym")
    registry = HabitRegistry([gym])
    config = _config()
    db.set_cadence(OWNER, "gym", 2)
    monday = date(2026, 8, 24)

    _seed(db, "2026-08-10T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-11T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-17T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-18T09:00:00", "gym", 1.0)

    clock = lambda: datetime(monday.year, monday.month, monday.day, 9, 0, 0)
    text = dashboard.render(db, config, registry, "en", OWNER, clock)
    line = next(ln for ln in text.splitlines() if "gym" in ln)
    assert "weekly streak 2 week" in line, f"expected the living pass-through value, got: {line!r}"


# ===========================================================================
# 3. Boundary audits: exactness-sensitive callers must stay governed by
#    `compute_streak`, provably unaffected by `display_streak`.
# ===========================================================================


def test_records_longest_streak_stays_governed_by_compute_streak_not_display_streak(db):
    """The sharpest regression catcher for the keep/switch boundary: a
    stored `longest_streak` record of exactly 1 (an already-broken, seeded
    baseline), then a real 2-day run through yesterday with today still
    partial. `compute_streak(today)` is exactly 0 (today doesn't qualify
    yet) -- `records.update_on_log` must see that 0 and do NOTHING (no
    upsert, no celebration): the record must stay at 1. If `records.py`
    were ever accidentally wired to `display_streak` instead (which
    reports 2 for this same data), this would wrongly upsert to 2 and
    fire a false celebration -- this test fails loudly if that regression
    is ever introduced."""
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = _config()
    registry = HabitRegistry([juice])
    today = date(2026, 8, 24)
    db.upsert_record(OWNER, "juice", "longest_streak", 1.0, "2026-08-01")

    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{today.isoformat()}T09:00:00", "juice", 400.0)  # today: partial, below goal

    # The display-side number for this exact data IS living (2) --
    # confirms the scenario actually exercises the divergence, not a
    # vacuous case where both functions would agree anyway.
    assert streaks.display_streak(db, config, juice, today, OWNER) == 2
    assert streaks.compute_streak(db, config, juice, today, OWNER) == 0

    clock = lambda: datetime(today.year, today.month, today.day, 9, 0, 0)
    broken = records.update_on_log(db, config, registry, juice, OWNER, clock=clock)

    assert broken == [], "no celebration must fire off the living (display) number"
    assert db.get_record(OWNER, "juice", "longest_streak") == 1.0, (
        "the stored record must stay governed by compute_streak's exact 0, unaffected by display_streak's living 2"
    )


def test_crossed_milestone_unaffected_by_repeated_display_streak_calls_between(db):
    """`crossed_milestone` (KEPT) must be a pure function of its own
    `was_qualified_before` snapshot + the current DB state -- proven here
    by sandwiching it between many `display_streak` calls (simulating a
    dashboard/wrapped/habits-overview/portal row all rendering the same
    day repeatedly) and confirming neither the crossing detection nor the
    once-per-crossing suppression is disturbed by any number of them."""
    juice = _synthetic_habit("juice", "numeric", goal=1000)
    config = _config()
    today = date(2026, 8, 24)
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "juice", 1000.0)
    _seed(db, f"{today.isoformat()}T09:00:00", "juice", 1000.0)  # today qualifies too -- a 3-day streak

    for _ in range(25):
        streaks.display_streak(db, config, juice, today, OWNER)

    # was_qualified_before=False: this IS the crossing -- 3 is a default
    # milestone ([3, 7, 30]).
    assert streaks.crossed_milestone(db, config, juice, today, was_qualified_before=False, user_id=OWNER) == 3

    for _ in range(25):
        streaks.display_streak(db, config, juice, today, OWNER)

    # was_qualified_before=True: a second log the same, already-qualifying
    # day -- must not re-fire, even after another batch of display calls.
    assert streaks.crossed_milestone(db, config, juice, today, was_qualified_before=True, user_id=OWNER) is None


def test_review_duration_streak_stays_exact_not_living_regardless_of_display_streak_calls(db):
    """Review's own duration-habit streak (KEPT, `review.py:122`, per
    Archi's explicit instruction) must report the EXACT, non-living
    number -- 0, not 2 -- when today hasn't been logged yet, even though
    `display_streak` for the identical data reports 2. Interleaved
    `display_streak` calls (simulating the dashboard having already
    rendered this same day) must not leak into review's own independent
    recomputation."""
    stretch = _synthetic_habit("stretch", "duration", label_en="stretch")
    config = _config()
    today = date(2026, 8, 24)
    _seed(db, f"{(today - timedelta(days=2)).isoformat()}T09:00:00", "stretch", None)
    _seed(db, f"{(today - timedelta(days=1)).isoformat()}T09:00:00", "stretch", None)
    # today: no session logged yet -- pending, not a genuine miss in the
    # display sense, but review must still report the exact 0.

    for _ in range(10):
        streaks.display_streak(db, config, stretch, today, OWNER)
    assert streaks.display_streak(db, config, stretch, today, OWNER) == 2

    day_strs = timeutil.week_days(today)
    stats = review._compute_habit_stats(db, config, stretch, day_strs, today, OWNER)
    assert stats.streak == 0, "review's duration streak must stay exact (compute_streak), never the living number"


# ===========================================================================
# 4. Timezone / clock discipline across every switched caller's own "today".
# ===========================================================================


def test_timezone_anchor_agrees_across_switched_callers_under_the_apps_real_naive_clock(db):
    """Every real call site in this codebase threads an injectable but
    NAIVE `clock` (always `datetime.now`-shaped, confirmed by inspection
    of `core/routing.py`/`core/dashboard.py`/`core/wrapped.py`/`core/
    portal/users.py`/`core/digest.py` -- none of them ever construct a
    timezone-AWARE clock) -- under that real shape, `dashboard.render`
    (tz-normalized via `timeutil.today_in_timezone`) and `discoverability.
    build_habits_overview` (raw `clock().date()`, no explicit
    normalization) must still agree on "today" and therefore on the same
    cadence habit's living streak number, since a naive value is treated
    identically by both paths."""
    gym = _synthetic_habit("gym", "boolean", label_en="gym")
    registry = HabitRegistry([gym])
    config = _config()
    db.set_cadence(OWNER, "gym", 2)
    monday = date(2026, 8, 24)
    _seed(db, "2026-08-10T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-11T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-17T09:00:00", "gym", 1.0)
    _seed(db, "2026-08-18T09:00:00", "gym", 1.0)

    naive_clock = lambda: datetime(monday.year, monday.month, monday.day, 9, 0, 0)

    dash_text = dashboard.render(db, config, registry, "en", OWNER, naive_clock)
    dash_line = next(ln for ln in dash_text.splitlines() if "gym" in ln)

    overview_text = discoverability.build_habits_overview(db, config, registry, naive_clock, "en", OWNER)
    overview_line = next(ln for ln in overview_text.splitlines() if "gym" in ln)

    assert "weekly streak 2 week" in dash_line
    assert "weekly streak 2 week" in overview_line, (
        "dashboard.render and build_habits_overview must agree on today's cadence streak under the app's real "
        "(always-naive) clock shape"
    )

    # Informational (not a FAIL against this patch's own ACs): the two
    # functions resolve "today" through DIFFERENT mechanisms --
    # `dashboard.render`/`wrapped.render`/`portal/users._current_streak`
    # all call `timeutil.today_in_timezone(clock, tz)` (explicitly
    # astimezone()'s an AWARE clock to config.app.timezone before taking
    # .date()), while `discoverability.build_habits_overview` takes a bare
    # `clock().date()` with no such normalization -- see that function's
    # own docstring, which explicitly defers ALL tz correctness to
    # whatever the caller passes as `clock`. This is a PRE-EXISTING
    # characteristic of `build_habits_overview` (unchanged by this patch --
    # `display_streak` was substituted in at the same already-resolved
    # `today` variable, no different from `compute_streak` before it), and
    # is inert today because no real call site in this codebase ever
    # constructs an aware clock. It becomes a genuine bug ONLY if some
    # future caller ever passes an aware clock across a tz boundary --
    # demonstrated below directly against the two raw "today" resolvers,
    # not through the app (nothing wires an aware clock into either
    # today):
    aware_clock = lambda: datetime(2026, 8, 23, 20, 0, 0, tzinfo=ZoneInfo("UTC"))  # 2026-08-24 03:00 in Bangkok (+7h)
    tz_normalized_today = timeutil.today_in_timezone(aware_clock, config.app.timezone)
    raw_today = aware_clock().date()
    assert tz_normalized_today == date(2026, 8, 24)
    assert raw_today == date(2026, 8, 23)
    assert tz_normalized_today != raw_today, (
        "flagged, not asserted-fixed: build_habits_overview's raw clock().date() would disagree with every other "
        "switched surface's timeutil.today_in_timezone() at a UTC/Bangkok midnight-crossing boundary IF ever given "
        "an aware clock -- pre-existing, not introduced by v1.3.2, and currently unreachable in production"
    )
