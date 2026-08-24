"""Vera gap-audit for the v1.6.0 `insights` module (SPEC-v1.6.md Feature 3
"Personal bests & records" + Feature 4 "Deterministic trends"), on top of
Luna's own `tests/test_records.py` (43 tests) + `tests/test_trends.py` (37
tests). These fill adversarial angles the coordinator specifically flagged
that were not already locked in by Luna's own suite:

- flat (non-monotonic) week run-length off-by-one,
- an empty CURRENT week against a real previous week (the mirror image of
  Luna's own "empty previous week" coverage),
- `trends.compute`'s own timezone-boundary resolution (records.py already
  has this; trends.py did not),
- two different habits' records staying independent within one call
  sequence (not just two different USERS),
- a boolean habit's `best_week` record (Luna covered `best_day` only),
- render-budget behavior for `/records` and `/trends` under a large
  registry (matches the shared `core/render_budget.py` contract, but
  wasn't exercised through `insights.render` itself with enough habits to
  actually cross 4096 chars),
- a full Thai-trigger collision sweep against every OTHER module's own
  alias word in the same `dispatch()` table.

Same conventions as `tests/test_records.py`/`tests/test_trends.py`: real
on-disk SQLite (`tmp_path`), no DB mocks, no channel/LLM involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from habit_assistant.config import Config
from habit_assistant.core import commands, records, trends
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str | None = None,
    label_th: str = "ทดสอบ",
    unit_en: str | None = "ml",
    unit_th: str | None = "มล.",
    goal: float | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en or id_,
        label_th=label_th,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


def _db(tmp_path, name="gaps.db"):
    database = Database(tmp_path / name)
    database.upsert_user(OWNER, role="owner", status="active")
    return database


def _seed(db: Database, user_id: str, ts: str, category: str, value_num: float, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _clock_at(dt: datetime):
    return lambda: dt


def _seed_weekly_totals(db: Database, user_id: str, habit_id: str, today: datetime, totals_oldest_first: list[float]):
    n = len(totals_oldest_first)
    for i, total in enumerate(totals_oldest_first):
        week_start = today - timedelta(days=(n - 1 - i) * 7)
        _seed(db, user_id, week_start.isoformat(timespec="seconds"), habit_id, float(total))


# ===========================================================================
# trends.compute -- run-length off-by-one at exactly-equal (flat) weeks.
# ===========================================================================


def test_flat_run_of_equal_weeks_is_not_a_rising_or_falling_run(tmp_path):
    """R-T2's callout only fires on a STRICTLY monotonic run. Three
    identical weekly totals in a row must report (0, 0) for both --
    equal is neither rising nor falling, and must not be miscounted as a
    1-week or 2-week run by an off-by-one in the backward walk."""
    db = _db(tmp_path)
    config = Config()
    today = datetime(2026, 8, 24, 9, 0)
    registry = HabitRegistry([_habit("water", "numeric")])
    _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 1000.0, 1000.0])
    t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
    assert t.rising_weeks == 0
    assert t.falling_weeks == 0
    db.close()


def test_rising_run_stops_exactly_at_the_first_tie_walking_backward(tmp_path):
    """oldest -> newest: 300, 500, 1000, 1000, 2000. Walking backward from
    the current week: 2000 > 1000 (week -7) extends the run to 2; but
    week -7 (1000) TIES week -14 (1000), so the walk must stop exactly
    there -- even though the still-older data (500, 300) would, on its
    own, also look "generally rising." An off-by-one that peeks past the
    tie (or stops one step early, at 1 instead of 2) would misreport
    this. Expected: exactly 2."""
    db = _db(tmp_path)
    config = Config()
    today = datetime(2026, 8, 24, 9, 0)
    registry = HabitRegistry([_habit("water", "numeric")])
    _seed_weekly_totals(db, OWNER, "water", today, [300.0, 500.0, 1000.0, 1000.0, 2000.0])
    t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
    assert t.rising_weeks == 2
    assert t.falling_weeks == 0
    db.close()


# ===========================================================================
# trends.compute -- empty CURRENT week against a real previous week (the
# mirror image of Luna's own "empty previous week" coverage).
# ===========================================================================


def test_empty_current_week_against_a_real_previous_week_no_crash_correct_delta(tmp_path):
    """A user who tracked diligently last week and has logged NOTHING at
    all this week: `has_history` must still be True (there IS real
    previous-week data), `current_total` must be 0.0 (not an error), and
    `pct_change` must be a real, non-crashing negative 100%."""
    db = _db(tmp_path)
    config = Config()
    today = datetime(2026, 8, 24, 9, 0)
    registry = HabitRegistry([_habit("water", "numeric")])
    _seed(db, OWNER, (today - timedelta(days=7)).isoformat(timespec="seconds"), "water", 2000.0)
    t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
    assert t.has_history is True
    assert t.current_total == 0.0
    assert t.previous_total == 2000.0
    assert t.delta == -2000.0
    assert t.pct_change == -100
    db.close()


# ===========================================================================
# trends.compute -- timezone boundary (records.py already tests its own
# `_today`; trends.py's independent copy of the same shim did not).
# ===========================================================================


def test_trends_week_boundary_uses_config_timezone_not_utc(tmp_path):
    """Mirrors `tests/test_records.py::
    test_week_boundary_uses_config_timezone_not_utc` for the `trends`
    module's own independent `_today` helper: a tz-aware UTC clock just
    after UTC midnight (still yesterday in `Asia/Bangkok`... or already
    tomorrow, depending on direction) must resolve against Bangkok wall
    time, not the UTC calendar date, for the CURRENT week's own boundary."""
    from zoneinfo import ZoneInfo

    db = _db(tmp_path)
    config = Config()  # config.app.timezone defaults to Asia/Bangkok (UTC+7)
    # 2026-08-25T01:00 UTC == 2026-08-25T08:00 Bangkok -- a log placed at
    # the Bangkok wall-clock date must land in "this week"'s total when
    # `compute` is called with the tz-aware UTC clock for that same instant.
    utc_clock = _clock_at(datetime(2026, 8, 25, 1, 0, tzinfo=ZoneInfo("UTC")))
    registry = HabitRegistry([_habit("water", "numeric")])
    _seed(db, OWNER, "2026-08-25T08:00:00", "water", 900.0)
    t = trends.compute(db, config, registry, OWNER, utc_clock)[0]
    assert t.current_total == 900.0
    db.close()


# ===========================================================================
# records.update_on_log -- multi-habit independence within ONE call
# sequence (Luna's own isolation tests cover two USERS; this covers two
# HABITS for the same user, interleaved).
# ===========================================================================


def test_two_different_habits_records_stay_independent_when_interleaved(tmp_path):
    db = _db(tmp_path)
    config = Config()
    clock = _clock_at(datetime(2026, 8, 24, 9, 0))
    water = _habit("water", "numeric", goal=2500.0)
    stretch = _habit("stretch", "duration", unit_en="min", unit_th="นาที")
    registry = HabitRegistry([water, stretch])

    _seed(db, OWNER, "2026-08-24T09:00:00", "water", 3000.0)
    records.update_on_log(db, config, registry, water, OWNER, clock)
    _seed(db, OWNER, "2026-08-24T09:05:00", "stretch", 15.0)
    records.update_on_log(db, config, registry, stretch, OWNER, clock)

    assert db.get_record(OWNER, "water", "best_day") == 3000.0
    assert db.get_record(OWNER, "stretch", "best_day") == 15.0
    # a second, larger stretch log must not touch water's own record
    _seed(db, OWNER, "2026-08-24T09:10:00", "stretch", 45.0)
    broken = records.update_on_log(db, config, registry, stretch, OWNER, clock)
    assert {rt for rt, _ in broken} <= {"best_day", "best_week", "longest_streak"}
    assert db.get_record(OWNER, "water", "best_day") == 3000.0  # unchanged
    assert db.get_record(OWNER, "stretch", "best_day") == 60.0  # 15 + 45
    db.close()


# ===========================================================================
# records.update_on_log -- boolean habit's best_week (Luna's suite only
# exercises the boolean best_day path).
# ===========================================================================


def test_boolean_habit_best_week_uses_count_true_across_the_whole_week(tmp_path):
    """Best_week for a boolean habit must aggregate via `count_true`
    (only truthy entries), not raw `count`. Proven twice, per Archi's
    2026-08-24 silent-seed ruling (`core/records.py:_maybe_break_record`):
    the FIRST `update_on_log` call for a habit is now a silent seed
    (`broken == []`), so the count_true value is checked directly via
    `db.get_record` on that call; a SECOND call, with one more truthy day
    added, must then genuinely celebrate a strict-exceed over that seeded
    baseline -- proving the comparison itself (not just the initial seed)
    uses the correct aggregate too."""
    db = _db(tmp_path)
    config = Config()
    habit = _habit("stretch", "boolean", unit_en=None, unit_th=None)
    base = datetime(2026, 8, 24, 9, 0)
    registry = HabitRegistry([habit])
    # 3 truthy days + 1 explicit false day inside the same rolling week --
    # best_week must count only the truthy entries (count_true), matching
    # `period_total`'s own documented boolean rule.
    _seed(db, OWNER, base.isoformat(timespec="seconds"), "stretch", 1.0)
    _seed(db, OWNER, (base - timedelta(days=1)).isoformat(timespec="seconds"), "stretch", 0.0)
    _seed(db, OWNER, (base - timedelta(days=2)).isoformat(timespec="seconds"), "stretch", 1.0)
    _seed(db, OWNER, (base - timedelta(days=3)).isoformat(timespec="seconds"), "stretch", 1.0)

    # Call 1: first-ever observation for (OWNER, "stretch", "best_week")
    # -- silent seed, no celebration -- but the SEEDED VALUE itself must
    # be the count_true aggregate (3, not 4 -- the false day excluded).
    broken = records.update_on_log(db, config, registry, habit, OWNER, _clock_at(base))
    assert broken == []
    assert db.get_record(OWNER, "stretch", "best_week") == 3.0

    # Call 2: one more truthy day inside the same rolling window pushes
    # the real count_true total to 4 -- a genuine strict-exceed over the
    # seeded baseline, so this call MUST celebrate, with the celebrated
    # value itself the count_true aggregate (4, not 5).
    _seed(db, OWNER, (base - timedelta(days=4)).isoformat(timespec="seconds"), "stretch", 1.0)
    broken_again = records.update_on_log(db, config, registry, habit, OWNER, _clock_at(base))
    assert ("best_week", 4.0) in broken_again
    assert db.get_record(OWNER, "stretch", "best_week") == 4.0
    db.close()


# ===========================================================================
# Render-budget -- /records and /trends actually stay <= 4096 chars with a
# large registry (fit_within_budget itself is proven generically in
# tests/test_render_budget.py; this proves records.render/trends.render
# actually invoke it correctly at realistic scale).
# ===========================================================================


def _many_habits(n: int, type_: str = "numeric") -> HabitRegistry:
    return HabitRegistry([_habit(f"habit_{i:03d}", type_, label_en=f"habit {i:03d}") for i in range(n)])


def test_records_render_stays_within_budget_for_a_large_registry(tmp_path):
    db = _db(tmp_path)
    config = Config()
    registry = _many_habits(150)
    # A large registry with NO established records yet is already
    # realistic (a fresh install with many configured habits) and, at
    # 150 "no records yet" blocks, comfortably exceeds 4096 unfitted.
    text = records.render(db, config, registry, "en", OWNER)
    assert len(text) <= 4096
    db.close()


def test_trends_render_stays_within_budget_for_a_large_registry(tmp_path):
    db = _db(tmp_path)
    config = Config()
    registry = _many_habits(150)
    text = trends.render(db, config, registry, "en", OWNER)
    assert len(text) <= 4096
    db.close()


def test_records_render_over_budget_actually_drops_rows_not_just_luck(tmp_path):
    """Distinguishes "happens to already fit" from "budget machinery
    engaged": build a registry big enough that the UNFITTED text would
    exceed 4096, and confirm at least one habit's block is genuinely
    absent from the final (fitted) output."""
    db = _db(tmp_path)
    config = Config()
    registry = _many_habits(200)
    unfitted = "\n\n".join(records._habit_block(db, h, "en", OWNER) for h in registry)
    assert len(unfitted) > 4096  # confirms the test setup actually stresses the budget path
    text = records.render(db, config, registry, "en", OWNER)
    assert len(text) <= 4096
    assert "habit_199" not in text  # the oldest (last, per registry order) rows are the ones dropped
    db.close()


# ===========================================================================
# Matchers -- full Thai-trigger collision sweep against every OTHER
# module's own alias word registered in the same dispatch() table.
# ===========================================================================


_OTHER_MODULES_TH_TRIGGERS = {
    "remind": "เตือน",
    "lang": "ภาษา",
    "quiet": "เงียบ",
    "audit": "ประวัติ",
    "checkin": "เช็คอิน",
    "dnd": "งดรบกวน",
    "dashboard": "แดชบอร์ด",
    "history": "ย้อนหลัง",
    "heatmap": "ปฏิทิน",
}


def test_other_modules_thai_triggers_never_resolve_to_records_or_trends():
    for owner, trigger in _OTHER_MODULES_TH_TRIGGERS.items():
        cmd = commands.dispatch(trigger, DEFAULT_REGISTRY)
        if cmd is not None:
            assert cmd.kind not in ("records", "trends"), (
                f"{owner}'s own trigger {trigger!r} unexpectedly matched insights kind {cmd.kind!r}"
            )


def test_records_and_trends_thai_triggers_never_resolve_to_another_module():
    records_cmd = commands.dispatch("สถิติ", DEFAULT_REGISTRY)
    trends_cmd = commands.dispatch("แนวโน้ม", DEFAULT_REGISTRY)
    assert records_cmd is not None and records_cmd.kind == "records"
    assert trends_cmd is not None and trends_cmd.kind == "trends"


def test_records_and_trends_bare_triggers_do_not_collide_with_each_other():
    """สถิติ (records) and แนวโน้ม (trends) share no substring relationship
    -- each other's bare trigger must resolve to its OWN kind only, never
    accidentally fall through to the other's matcher."""
    assert commands.dispatch("สถิติ", DEFAULT_REGISTRY).kind == "records"
    assert commands.dispatch("แนวโน้ม", DEFAULT_REGISTRY).kind == "trends"
