"""SPEC-v1.6.md §4 Feature 4 "Deterministic trends" (module `insights`,
R-T1-R-T3): `core/trends.py` (`compute`/`render`/`review_block`) + `core/
commands.dispatch`'s own `"trends"` kind.

Owned ACs (SPEC-v1.6.md §11): AC-T1 (`/trends` week-over-week + delta/%),
AC-T2 (review block + run-length callout), AC-T3 (insufficient-history
graceful degrade, no divide-by-zero/misleading %). Also exercises the
shared/cross-cutting rules that land through this module's own surface:
AC-X1 (registry-generic), AC-X3 (per-user isolation), R-X2 (bilingual,
zero-LLM).

Conventions: real on-disk SQLite (`tmp_path`), no DB mocks -- mirrors
`tests/test_records.py`'s own convention (the module this one is
structurally closest to -- both are the `insights` module)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, trends
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str = "test",
    label_th: str = "ทดสอบ",
    unit_en: str | None = "ml",
    unit_th: str | None = "มล.",
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
    database = Database(tmp_path / "trends.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def _seed(db: Database, user_id: str, ts: str, category: str, value_num: float, raw: str = "x") -> int:
    return db.insert_log(LogEntry(None, user_id, ts, category, value_num, None, raw, "reply"))


def _clock_at(dt: datetime):
    return lambda: dt


def _seed_weekly_totals(db: Database, user_id: str, habit_id: str, today: datetime, totals_oldest_first: list[float]):
    """Seeds one log per week (7-day-spaced), `totals_oldest_first[-1]`
    landing on `today`'s own week."""
    n = len(totals_oldest_first)
    for i, total in enumerate(totals_oldest_first):
        week_start = today - timedelta(days=(n - 1 - i) * 7)
        _seed(db, user_id, week_start.isoformat(timespec="seconds"), habit_id, float(total))


# ===========================================================================
# dispatch() shape -- /trends, แนวโน้ม (R-T2)
# ===========================================================================


class TestDispatchShape:
    def test_bare_slash_shows_all(self):
        cmd = commands.dispatch("/trends", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="trends", category=None)

    def test_slash_with_habit_filters(self):
        cmd = commands.dispatch("/trends water", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="trends", category="water")

    def test_slash_trailing_int_is_ignored_not_rejected(self):
        cmd = commands.dispatch("/trends water 8", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="trends", category="water")

    def test_thai_bare_shows_all(self):
        cmd = commands.dispatch("แนวโน้ม", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="trends", category=None)

    def test_thai_with_habit_filters(self):
        cmd = commands.dispatch("แนวโน้ม น้ำ", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="trends", category="water")

    @pytest.mark.parametrize(
        "text",
        [
            "แนวโน้มเศรษฐกิจแย่ลง",  # ordinary prose about economic trends, glued
            "แนวโน้ม น่าสนใจ",  # spaced but not a real habit/number
            "แนวโน้มของตลาดหุ้น",
        ],
    )
    def test_thai_adversarial_corpus_never_misfires(self, text):
        assert commands.dispatch(text, DEFAULT_REGISTRY) is None

    @pytest.mark.parametrize("text", ["500ml", "ดื่มน้ำ 2 แก้ว", "10 min stretch"])
    def test_ordinary_logs_never_misfire(self, text):
        assert commands.dispatch(text, DEFAULT_REGISTRY) is None


# ===========================================================================
# compute -- R-T1 (AC-T1)
# ===========================================================================


class TestCompute:
    def test_matches_spec_sample_numbers_exactly(self, db, config):
        """SPEC-v1.6.md §3.3's own literal sample: 2450 -> 2780 ml, +13%."""
        habit_id = "water"
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, habit_id, today, [2450.0, 2780.0])
        registry = HabitRegistry([_habit("water", "numeric")])
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.previous_total == 2450.0
        assert t.current_total == 2780.0
        assert t.delta == 330.0
        assert t.pct_change == 13

    def test_negative_delta_and_pct(self, db, config):
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 800.0])
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.delta == -200.0
        assert t.pct_change == -20

    def test_no_history_at_all(self, db, config):
        """R-T3: a brand-new habit with only this week's data -> no
        previous-week baseline -> `has_history=False`, `pct_change=None`,
        never a crash."""
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed(db, OWNER, today.isoformat(timespec="seconds"), "water", 500.0)
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.has_history is False
        assert t.pct_change is None
        assert t.rising_weeks == 0 and t.falling_weeks == 0

    def test_previous_week_zero_total_but_real_history_no_divide_by_zero(self, db, config):
        """A boolean-false-only (or literal "0") previous week genuinely
        HAS history (real log rows exist), distinct from no data at all
        -- `pct_change` must stay `None` (can't divide by zero) but
        `has_history` must be `True` (R-T3's own "no LAST-WEEK data"
        wording, not "no data ever")."""
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed(db, OWNER, (today - timedelta(days=7)).isoformat(timespec="seconds"), "water", 0.0)
        _seed(db, OWNER, today.isoformat(timespec="seconds"), "water", 500.0)
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.has_history is True
        assert t.previous_total == 0.0
        assert t.pct_change is None  # no exception, no misleading %
        assert t.delta == 500.0

    def test_gap_in_history_treated_as_no_last_week_data(self, db, config):
        """A habit with real data 3 weeks ago but nothing at all in the
        immediately preceding week (a break/gap) -- R-T3's own literal
        "no LAST-WEEK data" -- must degrade gracefully, not treat the
        older data as if it were last week's."""
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed(db, OWNER, (today - timedelta(days=21)).isoformat(timespec="seconds"), "water", 3000.0)
        _seed(db, OWNER, today.isoformat(timespec="seconds"), "water", 500.0)
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.has_history is False

    def test_rising_run_length_counts_weeks_in_the_monotonic_run(self, db, config):
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 1500.0, 2000.0, 2500.0])
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.rising_weeks == 4
        assert t.falling_weeks == 0

    def test_falling_run_length(self, db, config):
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed_weekly_totals(db, OWNER, "water", today, [500.0, 400.0, 300.0, 200.0])
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.falling_weeks == 4
        assert t.rising_weeks == 0

    def test_run_breaks_on_a_flat_or_reversed_week(self, db, config):
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        # oldest -> newest: 500, 1000 (up), 800 (down) -- current week (800)
        # is LOWER than last week (1000): a falling run of exactly 2, not
        # contaminated by the earlier (irrelevant, opposite-direction) leg.
        _seed_weekly_totals(db, OWNER, "water", today, [500.0, 1000.0, 800.0])
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.falling_weeks == 2
        assert t.rising_weeks == 0

    def test_single_up_week_run_length_is_two_not_one(self, db, config):
        """R-T2's callout gate is `run_length >= 2`; a lone week-over-week
        increase (2 total data points) must itself already satisfy that
        gate -- "weeks in the trend" counts the current week too."""
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed_weekly_totals(db, OWNER, "water", today, [100.0, 200.0])
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.rising_weeks == 2

    def test_partial_current_week_still_computes_a_valid_rolling_window(self, db, config):
        """The "week" here is a rolling 7-day window ending today, not a
        calendar week -- calling `compute` mid-week (only a couple of
        real logged days into the CURRENT window) must not truncate or
        error; it's still a full, well-defined 7-day sum."""
        today = datetime(2026, 8, 24, 9, 0)  # arbitrary weekday
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed(db, OWNER, (today - timedelta(days=7)).isoformat(timespec="seconds"), "water", 1000.0)
        _seed(db, OWNER, (today - timedelta(days=2)).isoformat(timespec="seconds"), "water", 300.0)
        _seed(db, OWNER, today.isoformat(timespec="seconds"), "water", 200.0)
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.current_total == 500.0  # 300 + 200, both inside the trailing 7 days
        assert t.previous_total == 1000.0

    def test_boolean_habit_uses_count_true_aggregate(self, db, config):
        registry = HabitRegistry([_habit("stretch", "boolean", unit_en=None)])
        today = datetime(2026, 8, 24, 9, 0)
        _seed(db, OWNER, (today - timedelta(days=7)).isoformat(timespec="seconds"), "stretch", 1.0)
        _seed(db, OWNER, (today - timedelta(days=7)).isoformat(timespec="seconds"), "stretch", 0.0)  # false, excluded
        _seed(db, OWNER, today.isoformat(timespec="seconds"), "stretch", 1.0)
        _seed(db, OWNER, today.isoformat(timespec="seconds"), "stretch", 1.0)
        t = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        assert t.previous_total == 1.0
        assert t.current_total == 2.0

    def test_isolation_two_users_independent_trends(self, db, config):
        today = datetime(2026, 8, 24, 9, 0)
        registry = HabitRegistry([_habit("water", "numeric")])
        _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 2000.0])
        _seed_weekly_totals(db, MEMBER, "water", today, [500.0, 500.0])
        t_owner = trends.compute(db, config, registry, OWNER, _clock_at(today))[0]
        t_member = trends.compute(db, config, registry, MEMBER, _clock_at(today))[0]
        assert t_owner.current_total == 2000.0
        assert t_member.current_total == 500.0
        assert t_member.pct_change == 0

    def test_registry_generic_iterates_every_configured_habit(self, db, config):
        """AC-X1: no per-feature code change needed for an extra habit."""
        registry = HabitRegistry([_habit("water", "numeric"), _habit("meditation", "duration", label_en="meditation")])
        results = trends.compute(db, config, registry, OWNER, _clock_at(datetime(2026, 8, 24, 9, 0)))
        assert {t.habit.id for t in results} == {"water", "meditation"}


# ===========================================================================
# render -- /trends [habit] (R-T2, AC-T1/AC-T3)
# ===========================================================================


class TestRender:
    def test_matches_spec_sample_text_shape(self, db, config):
        """SPEC-v1.6.md §3.3's own literal sample: "2450 -> 2780 ml
        (+13%) ... 3 weeks rising". With only these two weeks seeded, the
        rising run is (correctly) 2 weeks, not 3 -- the "N weeks rising"
        callout scales with how much real history exists, matching this
        module's own literal run-length definition (see `core/trends.py:
        _run_lengths`'s docstring)."""
        registry = HabitRegistry([_habit("water", "numeric", label_en="water", unit_en="ml")])
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, "water", today, [2450.0, 2780.0])
        text = trends.render(db, config, registry, "en", OWNER, habit_id="water", clock=_clock_at(today))
        assert text == "📊 water — this week vs last: 2450 → 2780 ml (+13%) · 2 weeks rising 📈"

    def test_rising_suffix_appended_when_run_length_at_least_two(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric", label_en="water", unit_en="ml")])
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 1500.0, 2000.0])
        text = trends.render(db, config, registry, "en", OWNER, habit_id="water", clock=_clock_at(today))
        assert "weeks rising" in text
        assert "📈" in text

    def test_falling_suffix_appended(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric", label_en="water", unit_en="ml")])
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, "water", today, [2000.0, 1500.0, 1000.0])
        text = trends.render(db, config, registry, "en", OWNER, habit_id="water", clock=_clock_at(today))
        assert "weeks falling" in text
        assert "📉" in text

    def test_no_history_renders_friendly_message_not_an_error(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric")])
        text = trends.render(db, config, registry, "en", OWNER, habit_id="water")
        assert "not enough history" in text.lower()

    def test_invalid_habit_reports_friendly_error(self, db, config):
        text = trends.render(db, config, DEFAULT_REGISTRY, "en", OWNER, habit_id="coffee")
        assert "coffee" in text

    def test_unfiltered_shows_one_line_per_habit(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric"), _habit("stretch", "boolean", unit_en=None)])
        text = trends.render(db, config, registry, "en", OWNER)
        assert len(text.split("\n")) == 2

    def test_thai_language_is_distinct_from_english(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric", label_th="น้ำ")])
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 1200.0])
        en_text = trends.render(db, config, registry, "en", OWNER, habit_id="water", clock=_clock_at(today))
        th_text = trends.render(db, config, registry, "th", OWNER, habit_id="water", clock=_clock_at(today))
        assert en_text != th_text

    def test_render_is_fail_open_never_raises(self, db, config, monkeypatch):
        monkeypatch.setattr(db, "count", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        text = trends.render(db, config, DEFAULT_REGISTRY, "en", OWNER)
        assert isinstance(text, str) and text

    def test_zero_llm_no_ollama_import(self):
        import inspect

        source = inspect.getsource(trends)
        assert "ollama" not in source.lower()
        assert "OllamaClient" not in source

    def test_zero_llm_no_channel_import(self):
        import inspect

        source = inspect.getsource(trends)
        assert "import habit_assistant.channels" not in source
        assert "from habit_assistant.channels" not in source


# ===========================================================================
# review_block -- weekly-review integration surface (R-T2, AC-T2)
# ===========================================================================


class TestReviewBlock:
    def test_includes_header_and_one_line_per_habit(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric"), _habit("stretch", "boolean", unit_en=None)])
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, "water", today, [1000.0, 1200.0])
        block = trends.review_block(db, config, registry, "en", OWNER, clock=_clock_at(today))
        lines = block.split("\n")
        assert lines[0] == "📊 Trends"
        assert len(lines) == 3  # header + 2 habits

    def test_agrees_with_compute_numbers(self, db, config):
        registry = HabitRegistry([_habit("water", "numeric", label_en="water", unit_en="ml")])
        today = datetime(2026, 8, 24, 9, 0)
        _seed_weekly_totals(db, OWNER, "water", today, [2450.0, 2780.0])
        block = trends.review_block(db, config, registry, "en", OWNER, clock=_clock_at(today))
        assert "2450" in block and "2780" in block and "13%" in block

    def test_is_fail_open(self, db, config, monkeypatch):
        monkeypatch.setattr(db, "count", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        block = trends.review_block(db, config, DEFAULT_REGISTRY, "en", OWNER)
        assert isinstance(block, str) and block
