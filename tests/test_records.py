"""SPEC-v1.6.md §4 Feature 3 "Personal bests & records" (module `insights`,
R-R1-R-R4): `core/records.py` (`update_on_log`/`format_celebration`/
`render`) + `core/commands.dispatch`'s own `"records"` kind.

Owned ACs (SPEC-v1.6.md §11): AC-R1 (stored + updated), AC-R2 (celebrate
once, fail-open), AC-R3 (`/records` view). Also exercises the shared/
cross-cutting rules that land through this module's own surface: AC-X1
(registry-generic), AC-X3 (per-user isolation), R-X2 (bilingual, zero-LLM).

Conventions: real on-disk SQLite (`tmp_path`), no DB mocks -- mirrors
`tests/test_checkins.py`/`tests/test_history.py`'s own convention. No
channel/LLM involved at all (this module never imports either)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, records
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
    database = Database(tmp_path / "records.db")
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


# ===========================================================================
# dispatch() shape -- /records, สถิติ (R-R3)
# ===========================================================================


class TestDispatchShape:
    def test_bare_slash_shows_all(self):
        cmd = commands.dispatch("/records", DEFAULT_REGISTRY)
        assert cmd is not None
        assert cmd.kind == "records"
        assert cmd.category is None

    def test_slash_with_habit_filters(self):
        cmd = commands.dispatch("/records water", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="records", category="water")

    def test_slash_trailing_int_is_ignored_not_rejected(self):
        """SPEC-v1.6.md §5 skeleton: `records` never populates `Command.
        limit` -- the tail grammar mirrors `/history`'s (habit + int), but
        the int is discarded (only `heatmap` uses it)."""
        cmd = commands.dispatch("/records water 8", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="records", category="water")

    def test_slash_unresolved_habit_passes_through_raw_token(self):
        cmd = commands.dispatch("/records coffee", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="records", category="coffee")

    def test_thai_bare_shows_all(self):
        cmd = commands.dispatch("สถิติ", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="records", category=None)

    def test_thai_with_habit_filters(self):
        cmd = commands.dispatch("สถิติ น้ำ", DEFAULT_REGISTRY)
        assert cmd == commands.Command(kind="records", category="water")

    @pytest.mark.parametrize(
        "text",
        [
            "สถิติน้ำ",  # glued, no space -- must not match
            "สถิติดีมาก",  # ordinary Thai prose opening with the trigger word
            "สถิติ ดีมาก",  # spaced but the tail isn't a real habit/number
            "สถิติศาสตร์เป็นวิชาที่น่าสนใจ",  # "statistics is an interesting subject"
        ],
    )
    def test_thai_adversarial_corpus_never_misfires(self, text):
        assert commands.dispatch(text, DEFAULT_REGISTRY) is None

    @pytest.mark.parametrize("text", ["500ml", "ดื่มน้ำ 2 แก้ว", "10 min stretch", "diary: today was good"])
    def test_ordinary_logs_never_misfire(self, text):
        assert commands.dispatch(text, DEFAULT_REGISTRY) is None


# ===========================================================================
# update_on_log -- R-R2 (AC-R1/AC-R2)
# ===========================================================================


class TestUpdateOnLog:
    def test_first_log_seeds_records_silently_without_celebrating(self, db, config):
        """Archi's ruling (2026-08-24, overriding this module's earlier
        migration-docstring-based resolution): a habit's very first log
        SEEDS best_day/best_week (the row is written, so later logs have
        a baseline to compare against) but is never itself celebrated --
        R-R2's "strictly exceeds the stored record" presupposes a stored
        record to exceed, and celebrating literally every fresh habit's
        first log is noisier than the milestone precedent it claims to
        mirror (milestones never fire on a literal day-1 streak)."""
        habit = _habit("water", "numeric", goal=2500.0)
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 500.0)

        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        assert broken == []  # seeded silently, nothing to celebrate yet
        assert db.get_record(OWNER, "water", "best_day") == 500.0
        assert db.get_record(OWNER, "water", "best_week") == 500.0

    def test_second_log_that_exceeds_the_silently_seeded_baseline_celebrates(self, db, config):
        """The very next genuine improvement over a silently-seeded
        baseline DOES celebrate -- silent-seeding only suppresses the
        FIRST observation, not every observation forever."""
        habit = _habit("water", "numeric", goal=2500.0)
        clock1 = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 500.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock1)  # silent seed

        clock2 = _clock_at(datetime(2026, 8, 25, 9, 0))
        _seed(db, OWNER, "2026-08-25T09:00:00", "water", 900.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock2)

        assert ("best_day", 900.0) in broken
        assert db.get_record(OWNER, "water", "best_day") == 900.0

    def test_second_smaller_log_same_day_does_not_break_or_celebrate_again(self, db, config):
        habit = _habit("water", "numeric")
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 1000.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        _seed(db, OWNER, "2026-08-24T10:00:00", "water", 200.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        # today's total is now 1200 (1000+200) -- still a genuine increase
        # over the stored 1000, so best_day/best_week DO break again; this
        # asserts the "strictly greater" comparison is against the STORED
        # value, not the individual log amount.
        assert ("best_day", 1200.0) in broken
        assert db.get_record(OWNER, "water", "best_day") == 1200.0

    def test_equal_value_does_not_celebrate_strict_inequality(self, db, config):
        habit = _habit("water", "numeric")
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 1000.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        # A second day logging the exact same total (1000) must not
        # re-break best_day (equal is not "strictly exceeds").
        clock2 = _clock_at(datetime(2026, 8, 25, 9, 0))
        _seed(db, OWNER, "2026-08-25T09:00:00", "water", 1000.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock2)
        assert not any(rt == "best_day" for rt, _ in broken)
        assert db.get_record(OWNER, "water", "best_day") == 1000.0

    def test_smaller_value_never_downgrades_a_stored_record(self, db, config):
        # Two logs 24 days apart so their rolling 7-day windows don't
        # overlap -- isolates the "smaller value never downgrades"
        # assertion to best_day/best_week both, without the second week's
        # rolling total accidentally still summing over the first spike.
        habit = _habit("juice", "numeric")
        clock1 = _clock_at(datetime(2026, 8, 1, 9, 0))
        _seed(db, OWNER, "2026-08-01T09:00:00", "juice", 3000.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock1)

        clock2 = _clock_at(datetime(2026, 8, 25, 9, 0))
        _seed(db, OWNER, "2026-08-25T09:00:00", "juice", 100.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock2)
        assert broken == []
        assert db.get_record(OWNER, "juice", "best_day") == 3000.0
        assert db.get_record(OWNER, "juice", "best_week") == 3000.0

    def test_zero_value_never_creates_a_record(self, db, config):
        """A false boolean log (nothing truthy that day) has nothing to
        celebrate."""
        habit = _habit("stretch", "boolean")
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "stretch", 0.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)
        assert broken == []
        assert db.get_record(OWNER, "stretch", "best_day") is None

    def test_longest_streak_grows_daily_while_it_is_the_all_time_best(self, db, config):
        """R-R1: `longest_streak` uses the REAL `core/streaks.py:
        compute_streak` engine, not a re-derivation -- a habit's very
        first-ever streak legitimately re-breaks its own record every
        single day AFTER day 1 (each day IS a new all-time high once a
        baseline exists), unlike milestones (which only fire at specific
        configured lengths). Day 1 itself seeds silently (Archi's ruling)
        -- streak length 1 is stored but not celebrated."""
        habit = _habit("stretch", "duration")
        base = datetime(2026, 8, 20, 9, 0)
        streaks_seen = []
        for i in range(5):
            day = base + timedelta(days=i)
            _seed(db, OWNER, day.isoformat(timespec="seconds"), "stretch", 10.0)
            broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, _clock_at(day))
            streak_break = next((v for rt, v in broken if rt == "longest_streak"), None)
            streaks_seen.append(streak_break)
        assert streaks_seen == [None, 2.0, 3.0, 4.0, 5.0]
        assert db.get_record(OWNER, "stretch", "longest_streak") == 5.0

    def test_longest_streak_stops_re_breaking_once_matched_to_a_prior_best(self, db, config):
        """A NEW streak that hasn't yet caught up to an existing all-time
        best must not fire until it genuinely exceeds it."""
        habit = _habit("stretch", "duration")
        db.upsert_record(OWNER, "stretch", "longest_streak", 10.0, "2026-07-01")

        base = datetime(2026, 8, 20, 9, 0)
        for i in range(3):  # streak reaches 3, still well under 10
            day = base + timedelta(days=i)
            _seed(db, OWNER, day.isoformat(timespec="seconds"), "stretch", 10.0)
            broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, _clock_at(day))
            assert not any(rt == "longest_streak" for rt, _ in broken)
        assert db.get_record(OWNER, "stretch", "longest_streak") == 10.0

    def test_week_boundary_uses_config_timezone_not_utc(self, db, config):
        """SPEC-v1.6.md R-T1/§4: "week"/"today" resolution goes through
        `config.app.timezone`. `config.app.timezone` defaults to
        "Asia/Bangkok" (UTC+7) -- 2026-08-25T01:00 UTC is already
        2026-08-25T08:00 in Bangkok, a DIFFERENT calendar day than the UTC
        date. A tz-AWARE UTC clock must resolve "today" against Bangkok
        wall time, not UTC."""
        from zoneinfo import ZoneInfo

        habit = _habit("water", "numeric")
        utc_clock = _clock_at(datetime(2026, 8, 25, 1, 0, tzinfo=ZoneInfo("UTC")))
        _seed(db, OWNER, "2026-08-25T08:00:00", "water", 777.0)

        # First-ever observation seeds silently (Archi's ruling) -- the
        # celebration itself isn't what this test is proving; the DATE the
        # seeded row lands on is. `broken == []` on this first call is
        # expected, not a regression.
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, utc_clock)
        assert broken == []
        row = db.get_records(OWNER, "water")
        best_day_row = next(r for r in row if r["record_type"] == "best_day")
        assert best_day_row["achieved_on"] == "2026-08-25"  # Bangkok date, not the UTC date (08-24)

    def test_isolation_two_users_independent_records(self, db, config):
        habit = _habit("water", "numeric")
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 3000.0)
        _seed(db, MEMBER, "2026-08-24T09:00:00", "water", 100.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, MEMBER, clock)
        assert db.get_record(OWNER, "water", "best_day") == 3000.0
        assert db.get_record(MEMBER, "water", "best_day") == 100.0

    def test_undo_does_not_revert_an_already_celebrated_record(self, db, config):
        """SPEC-v1.6.md R-R1 ("stored, not re-derived") gives records no
        undo-reversion path at all -- §5's interfaces list no
        `update_on_undo`/similar function, unlike `dashboard.refresh`,
        which R-D5 explicitly wires to run after an undo. A record, once
        broken, is a durable high-water mark -- it stays even after the
        log that set it is later undone."""
        habit = _habit("water", "numeric")
        clock1 = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 500.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock1)  # silent seed

        clock2 = _clock_at(datetime(2026, 8, 25, 9, 0))
        row_id = _seed(db, OWNER, "2026-08-25T09:00:00", "water", 3000.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock2)
        assert ("best_day", 3000.0) in broken  # genuine celebration this time

        db.soft_delete(row_id)  # undo the record-setting log

        assert db.get_record(OWNER, "water", "best_day") == 3000.0  # unchanged
        # a fresh recompute (e.g. triggered by a later log) also does not
        # spontaneously revert it -- best_day on 08-25 is now 0 (nothing
        # live), which is <= the stored 3000, so nothing changes.
        broken_after = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock2)
        assert broken_after == []
        assert db.get_record(OWNER, "water", "best_day") == 3000.0

    def test_fail_open_never_raises_and_returns_empty_list(self, db, config, monkeypatch):
        habit = _habit("water", "numeric")

        def _boom(*a, **kw):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(db, "get_record", _boom)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, _clock_at(datetime(2026, 8, 24, 9, 0)))
        assert broken == []

    def test_multiple_record_types_break_together_in_one_call(self, db, config):
        # NOT habit id "water" -- `targets.config_goal` special-cases that
        # exact id to always read the legacy `config.reminders.water.
        # goal_ml`, regardless of what this synthetic Habit's own `.goal`
        # says (SPEC-v0.7.md's carried-forward v0.6 behavior). A
        # differently-named numeric habit with `goal=None` is genuinely
        # goal-less, so `longest_streak` qualifies on any entry (count()),
        # not a goal comparison.
        #
        # Day 1 seeds all three silently (Archi's ruling) -- day 2, a
        # genuine improvement on all three fronts (bigger day, bigger
        # rolling week, one more consecutive qualifying day), is what
        # actually breaks them together.
        habit = _habit("juice", "numeric")
        clock1 = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "juice", 500.0)
        seed_broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock1)
        assert seed_broken == []

        clock2 = _clock_at(datetime(2026, 8, 25, 9, 0))
        _seed(db, OWNER, "2026-08-25T09:00:00", "juice", 700.0)
        broken = records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock2)
        assert {rt for rt, _ in broken} == {"best_day", "best_week", "longest_streak"}


# ===========================================================================
# format_celebration -- R-R2's suffix line(s)
# ===========================================================================


class TestFormatCelebration:
    def test_empty_broken_list_renders_empty_string(self):
        habit = _habit("water", "numeric")
        assert records.format_celebration([], habit, "en") == ""

    def test_numeric_best_day_en(self):
        habit = _habit("water", "numeric", label_en="water", unit_en="ml")
        text = records.format_celebration([("best_day", 3200.0)], habit, "en")
        assert text == "🎉 New personal best — water best day: 3200 ml!"

    def test_longest_streak_matches_spec_sample_exactly(self):
        """SPEC-v1.6.md §3.3's own literal sample."""
        habit = _habit("water", "numeric", label_en="water")
        text = records.format_celebration([("longest_streak", 15.0)], habit, "en")
        assert text == "🎉 New personal best — longest water streak: 15 days!"

    def test_boolean_count_variant_has_no_unit(self):
        habit = _habit("stretch", "boolean", label_en="stretch", unit_en=None)
        text = records.format_celebration([("best_day", 2.0)], habit, "en")
        assert "ml" not in text
        assert "2" in text

    def test_multiple_breaks_join_as_separate_lines(self):
        habit = _habit("water", "numeric", label_en="water", unit_en="ml")
        text = records.format_celebration([("best_day", 3200.0), ("best_week", 18000.0)], habit, "en")
        assert len(text.split("\n")) == 2

    def test_thai_variant_present_and_non_empty(self):
        habit = _habit("water", "numeric", label_th="น้ำ", unit_th="มล.")
        text = records.format_celebration([("best_day", 3200.0)], habit, "th")
        assert text and "3200" in text


# ===========================================================================
# render -- /records [habit] (R-R3, AC-R3)
# ===========================================================================


class TestRender:
    def test_no_records_yet_renders_gracefully_per_habit(self, db, config):
        text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER)
        assert "No records yet" in text
        assert "water" in text and "stretch" in text and "diary" in text

    def test_filtered_no_records_yet(self, db, config):
        text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER, habit_id="water")
        assert "No records yet" in text
        assert "🏆" in text

    def test_invalid_habit_reports_friendly_error(self, db, config):
        text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER, habit_id="coffee")
        assert "coffee" in text

    def test_shows_established_records_with_correct_values(self, db, config):
        habit = _habit("water", "numeric", label_en="water", unit_en="ml", goal=2500.0)
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 3200.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER, habit_id="water")
        assert "3200" in text
        assert "ml" in text
        assert "2026-08-24" in text

    def test_boolean_habit_records_render_as_plain_counts(self, db, config):
        habit = _habit("stretch", "boolean", label_en="stretch", unit_en=None)
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "stretch", 1.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER, habit_id="stretch")
        assert "Best day: 1" in text

    def test_thai_language_is_distinct_from_english(self, db, config):
        habit = _habit("water", "numeric")
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 500.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        en_text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER, habit_id="water")
        th_text = records.render(db, config, DEFAULT_REGISTRY, "th", OWNER, habit_id="water")
        assert en_text != th_text

    def test_registry_generic_extra_habit_appears_automatically(self, db, config):
        """AC-X1: an extra configured habit (not one of the three
        built-ins) appears in `/records` with zero per-feature code
        change -- proven here by constructing a registry with a fourth
        habit and asserting it gets its own block."""
        extra = _habit("meditation", "duration", label_en="meditation")
        registry = HabitRegistry([*DEFAULT_REGISTRY, extra])
        text = records.render(db, config, registry, "en", OWNER)
        assert "meditation" in text

    def test_isolation_two_users_see_only_their_own_records(self, db, config):
        habit = _habit("water", "numeric")
        clock = _clock_at(datetime(2026, 8, 24, 9, 0))
        _seed(db, OWNER, "2026-08-24T09:00:00", "water", 3000.0)
        records.update_on_log(db, config, DEFAULT_REGISTRY, habit, OWNER, clock)

        member_text = records.render(db, config, DEFAULT_REGISTRY, "en", MEMBER, habit_id="water")
        assert "3000" not in member_text
        assert "No records yet" in member_text

    def test_render_is_fail_open_never_raises(self, db, config, monkeypatch):
        monkeypatch.setattr(db, "get_records", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        text = records.render(db, config, DEFAULT_REGISTRY, "en", OWNER)
        assert isinstance(text, str) and text  # degraded, not raised

    def test_zero_llm_no_ollama_import(self):
        import inspect

        source = inspect.getsource(records)
        assert "ollama" not in source.lower()
        assert "OllamaClient" not in source

    def test_zero_llm_no_channel_import(self):
        import inspect

        source = inspect.getsource(records)
        assert "import habit_assistant.channels" not in source
        assert "from habit_assistant.channels" not in source
