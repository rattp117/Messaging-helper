"""Weekly review tests (module M3, SPEC-v0.7.md §11): AC15 (default config
review is byte-identical to v0.6.0 -- same stats math, labels, language)
and AC16 (generic per-habit aggregation -- numeric total/avg/goal-adherence,
duration count+streak, text count, boolean done-days; a config-added habit
appears with zero code change; pre-v0.7 rows aggregate correctly through the
same generic db methods). The narrative + delivery layer (LLM call, fallback,
"no medical advice") is also covered here per Archi's original file split;
the aggregation math for the *built-in* three was originally covered in
test_db.py (shared-owned) -- this file now also owns `compute_weekly_stats`/
`format_stats_summary`'s own behavior directly, per SPEC-v0.7.md §11's file
split (`core/review.py` + `tests/test_review.py` are M3-exclusive)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.review import compute_weekly_stats, format_stats_summary, run_weekly_review
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

# ROADMAP.md v0.6.0 AC6.4: the weekly review is an unprompted send, so
# `run_weekly_review(db, Config(), registry, ...)` -- the default `Config()`
# used throughout this file (i18n.language="auto", i18n.primary_language="th")
# -- resolves to Thai, not English.
LANG = "th"


def default_registry() -> HabitRegistry:
    return HabitRegistry.from_config(Config())


def _synthetic_habit(
    id_: str,
    type_: str,
    *,
    goal: float | None = None,
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    label_en: str | None = None,
    label_th: str | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en or id_,
        label_th=label_th or id_,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


class FakeLLM:
    def __init__(self, text: str | None):
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        self.calls.append((system_prompt, user_prompt))
        return self._text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 2500.0, None, "seed", "reply"))
    database.insert_log(LogEntry(None, "owner", "2026-08-19T11:00:00", "stretch", 10.0, None, "seed", "reply"))
    database.insert_log(LogEntry(None, "owner", "2026-08-19T21:30:00", "diary", None, "seed", "seed", "reply"))
    yield database
    database.close()


# ---------------------------------------------------------------------------
# AC15 -- default config review is byte-identical to v0.6.0.
# ---------------------------------------------------------------------------


async def test_run_weekly_review_includes_narrative_and_stats(db):
    """Default `Config()`/registry resolves to Thai for this unprompted
    send -- header/labels are the Thai catalog equivalents of v0.6.0's
    English strings ("📊 Weekly Review", "Water total: 2500 ml", "Stretch
    sessions this week: 1", "Diary entries this week: 1"). The LLM-supplied
    narrative always passes through verbatim regardless of language."""
    llm = FakeLLM("Great week! Keep up the water habit.")
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    assert i18n.t("weekly_review_header", LANG) in text
    assert "Great week! Keep up the water habit." in text
    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in text
    assert i18n.t("stats_stretch_summary", LANG, stretch_total=1, stretch_streak=1) in text
    assert i18n.t("stats_diary_summary", LANG, diary_count=1) in text


async def test_run_weekly_review_falls_back_when_llm_returns_none(db):
    llm = FakeLLM(None)
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    assert i18n.t("weekly_review_fallback_narrative", LANG) in text
    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in text  # stats block still present


async def test_run_weekly_review_falls_back_when_llm_returns_empty_string(db):
    llm = FakeLLM("")
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    assert i18n.t("weekly_review_fallback_narrative", LANG) in text


async def test_run_weekly_review_passes_stats_summary_to_llm_prompt(db):
    llm = FakeLLM("narrative")
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    await run_weekly_review(db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    assert len(llm.calls) == 1
    _system_prompt, user_prompt = llm.calls[0]
    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in user_prompt


async def test_run_weekly_review_system_prompt_forbids_medical_advice(db):
    """Sanity check the narrative system prompt actually encodes SPEC.md
    §6's "no medical advice" constraint -- a prompt-engineering contract,
    not model behavior we can unit test directly."""
    llm = FakeLLM("narrative")
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    await run_weekly_review(db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    system_prompt, _user_prompt = llm.calls[0]
    assert "medical advice" in system_prompt.lower()


async def test_run_weekly_review_defaults_to_today_when_not_given(db, monkeypatch):
    """today=None should default to date.today() -- patch the module's
    `date` so this is deterministic."""
    import habit_assistant.core.review as review_module

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 19)

    monkeypatch.setattr(review_module, "date", FixedDate)
    llm = FakeLLM("narrative")
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=None)

    assert i18n.t("stats_water_total", LANG, water_total_ml=2500, water_avg_ml=357.1) in text


async def test_weekly_review_job_sends_result_over_channel(db):
    """Mirrors main.py's weekly_review_job closure: run_weekly_review's
    output must be exactly what gets sent through the Channel."""
    from typing import Awaitable, Callable

    from habit_assistant.channels.base import Channel

    class FakeChannel(Channel):
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, chat_id: str, text: str) -> None:
            self.sent.append(text)

        async def run(
            self,
            on_message: Callable[[str, str], Awaitable[None]],
            on_callback: Callable[[str, str, str, str], Awaitable[None]] | None = None,
        ) -> None:
            raise NotImplementedError

    channel = FakeChannel()
    llm = FakeLLM("Solid week overall.")
    registry = default_registry()
    config = Config()
    lang = i18n.resolve_unprompted_language(config)

    text = await run_weekly_review(db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))
    await channel.send("owner", text)

    assert channel.sent == [text]
    assert "Solid week overall." in channel.sent[0]


def _seed_known_week(db: Database, end_date: date) -> None:
    """Same fixture plan as the pre-v0.7 test_db.py `_seed_known_week`:
    water 500ml every day, stretch on days -2,-1,0 only (so the streak is
    3), diary on days -6 and 0 (2 entries)."""
    for offset in range(6, -1, -1):
        d = end_date - timedelta(days=offset)
        day_str = d.isoformat()
        db.insert_log(LogEntry(None, "owner", f"{day_str}T09:00:00", "water", 500.0, None, "seed water", "reply"))
        if offset <= 2:
            db.insert_log(LogEntry(None, "owner", f"{day_str}T11:00:00", "stretch", 10.0, None, "seed stretch", "reply"))
    diary_days = [end_date - timedelta(days=6), end_date]
    for d in diary_days:
        db.insert_log(LogEntry(None, "owner", f"{d.isoformat()}T21:30:00", "diary", None, "seed diary", "seed diary", "reply"))


def test_compute_weekly_stats_water_stretch_diary_math_matches_v060(tmp_path):
    """AC15: the built-in three's aggregation math is unchanged from
    v0.6.0 -- water per-day %/total, stretch count+streak, diary count."""
    db = Database(tmp_path / "habits.db")
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    registry = default_registry()

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    water = stats.get("water")
    stretch = stats.get("stretch")
    diary = stats.get("diary")
    assert water is not None and stretch is not None and diary is not None

    assert len(water.days) == 7
    assert water.days[0].day == "2026-08-13"
    assert water.days[-1].day == "2026-08-19"
    assert water.total == 3500.0
    assert water.avg == 500.0
    assert all(d.goal == 2500 for d in water.days)
    assert water.days[0].pct == 20.0

    assert stretch.total == 3
    assert stretch.streak == 3

    assert diary.total == 2
    db.close()


def test_compute_weekly_stats_stretch_streak_zero_when_last_day_has_no_stretch(tmp_path):
    db = Database(tmp_path / "habits.db")
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))
    registry = default_registry()

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("stretch").streak == 0
    db.close()


def test_compute_weekly_stats_empty_week_is_all_zero(tmp_path):
    db = Database(tmp_path / "habits.db")
    end_date = date(2026, 8, 19)
    registry = default_registry()

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    water = stats.get("water")
    assert water.total == 0
    assert water.avg == 0.0
    assert all(d.pct == 0.0 for d in water.days)
    assert stats.get("stretch").total == 0
    assert stats.get("stretch").streak == 0
    assert stats.get("diary").total == 0
    db.close()


def test_compute_weekly_stats_water_goal_read_from_legacy_reminders_config(tmp_path):
    """The built-in `water` habit's goal keeps coming from the legacy
    `config.reminders.water.goal_ml` field, mirroring main.py's water
    confirmation branch (both reuse v0.6.0's code verbatim) -- not
    `registry.get("water").goal`. This is a deliberate design choice
    (see core/review.py:_water_goal_ml), not an AC by itself, but it is
    the mechanism AC15's byte-identical guarantee rests on when a deployment
    still configures the goal through `[reminders.water]`."""
    db = Database(tmp_path / "habits.db")
    config = Config.model_validate({"reminders": {"water": {"goal_ml": 1000}}})
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))
    registry = default_registry()

    stats = compute_weekly_stats(db, config, registry, end_date, user_id="owner")

    assert stats.get("water").days[-1].goal == 1000
    assert stats.get("water").days[-1].pct == 50.0
    db.close()


def test_format_stats_summary_contains_expected_figures(tmp_path):
    db = Database(tmp_path / "habits.db")
    end_date = date(2026, 8, 19)
    _seed_known_week(db, end_date)
    registry = default_registry()
    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    summary = format_stats_summary(stats, registry)  # default lang="en"

    water = stats.get("water")
    stretch = stats.get("stretch")
    diary = stats.get("diary")
    assert f"Water total: {int(water.total)} ml" in summary
    assert f"Stretch sessions this week: {stretch.total}" in summary
    assert f"current streak: {stretch.streak} day(s)" in summary
    assert f"Diary entries this week: {diary.total}" in summary
    db.close()


# ---------------------------------------------------------------------------
# AC16 -- generic per-habit aggregation for a configured habit outside the
# built-in three.
# ---------------------------------------------------------------------------


def test_generic_numeric_with_goal_gets_total_avg_and_goal_adherence(tmp_path):
    """SPEC-v0.7.md §3.2's `sleep` example, extended to the review: numeric
    + goal gets the per-day header/lines (goal-adherence) plus a total/avg
    line, via the type-generic templates."""
    db = Database(tmp_path / "habits.db")
    sleep = _synthetic_habit("sleep", "numeric", goal=8, unit_en="h", unit_th="ชม.", label_en="sleep", label_th="นอน")
    registry = HabitRegistry([sleep])
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T22:00:00", "sleep", 7.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")
    sleep_stats = stats.get("sleep")

    assert len(sleep_stats.days) == 7
    assert sleep_stats.days[-1].value == 7.0
    assert sleep_stats.days[-1].pct == 87.5
    assert sleep_stats.total == 7.0
    assert round(sleep_stats.avg, 3) == round(7.0 / 7, 3)

    summary_en = format_stats_summary(stats, registry, "en")
    assert i18n.t("stats_generic_numeric_header", "en", label="sleep", unit="h") in summary_en
    assert (
        i18n.t("stats_generic_numeric_line", "en", day=end_date.isoformat(), value=7.0, goal=8, pct=87.5)
        in summary_en
    )
    assert i18n.t("stats_generic_numeric_total", "en", label="sleep", unit="h", total=7.0, avg=sleep_stats.avg) in summary_en

    summary_th = format_stats_summary(stats, registry, "th")
    assert i18n.t("stats_generic_numeric_header", "th", label="นอน", unit="ชม.") in summary_th
    db.close()


def test_generic_numeric_without_goal_gets_total_avg_only_no_per_day_lines(tmp_path):
    """SPEC-v0.7.md §3.2's `steps` example: a numeric habit with no goal
    has no goal-adherence to show, so the review renders only the
    total/avg line -- no per-day header/lines."""
    db = Database(tmp_path / "habits.db")
    steps = _synthetic_habit("steps", "numeric", goal=None, unit_en="steps", unit_th="ก้าว")
    registry = HabitRegistry([steps])
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T18:00:00", "steps", 8000.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")
    steps_stats = stats.get("steps")

    assert steps_stats.days == []
    assert steps_stats.total == 8000.0

    summary = format_stats_summary(stats, registry, "en")
    assert i18n.t("stats_generic_numeric_header", "en", label="steps", unit="steps") not in summary  # no per-day header
    assert (
        i18n.t("stats_generic_numeric_total", "en", label="steps", unit="steps", total=8000.0, avg=steps_stats.avg)
        in summary
    )
    db.close()


def test_generic_duration_gets_count_and_streak(tmp_path):
    db = Database(tmp_path / "habits.db")
    yoga = _synthetic_habit("yoga", "duration", unit_en="min", unit_th="นาที", label_en="yoga", label_th="โยคะ")
    registry = HabitRegistry([yoga])
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{(end_date - timedelta(days=1)).isoformat()}T09:00:00", "yoga", 20.0, None, "x", "reply"))
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "yoga", 15.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")
    yoga_stats = stats.get("yoga")

    assert yoga_stats.total == 2  # 2 sessions logged
    assert yoga_stats.streak == 2  # both of the last two days

    summary = format_stats_summary(stats, registry, "en")
    assert i18n.t("stats_generic_duration_summary", "en", label="yoga", total=2, streak=2) in summary
    db.close()


def test_generic_text_gets_entry_count(tmp_path):
    db = Database(tmp_path / "habits.db")
    gratitude = _synthetic_habit("gratitude", "text", label_en="gratitude", label_th="ขอบคุณ")
    registry = HabitRegistry([gratitude])
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T21:00:00", "gratitude", None, "grateful", "grateful", "reply"))

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")
    gratitude_stats = stats.get("gratitude")

    assert gratitude_stats.total == 1

    summary = format_stats_summary(stats, registry, "en")
    assert i18n.t("stats_generic_count_summary", "en", label="gratitude", count=1) in summary
    db.close()


def test_generic_boolean_gets_done_day_count_not_raw_row_count(tmp_path):
    """AC16: boolean gets "done-days" -- logging `meds` twice on the same
    day still counts as one done day, not two."""
    db = Database(tmp_path / "habits.db")
    meds = _synthetic_habit("meds", "boolean", label_en="meds", label_th="ยา")
    registry = HabitRegistry([meds])
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "meds", 1.0, None, "x", "reply"))
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T20:00:00", "meds", 1.0, None, "x", "reply"))
    db.insert_log(LogEntry(None, "owner", f"{(end_date - timedelta(days=1)).isoformat()}T09:00:00", "meds", 0.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")
    meds_stats = stats.get("meds")

    assert meds_stats.total == 1  # one done day, despite 2 truthy rows + 1 falsy row

    summary = format_stats_summary(stats, registry, "en")
    assert i18n.t("stats_generic_count_summary", "en", label="meds", count=1) in summary
    db.close()


def test_config_added_habit_appears_alongside_builtins_with_no_code_change(tmp_path):
    """AC16 / AC7.2 groundwork: a registry built from a config that adds a
    `sleep` habit renders `sleep`'s block alongside the three built-ins,
    purely from `HabitRegistry.from_config` + `core/review.py`'s existing
    id-branch/type-branch logic -- no new code path for `sleep` itself."""
    from habit_assistant.config import HabitConfig, HabitLabel

    db = Database(tmp_path / "habits.db")
    config = Config()
    config.habits = [
        *config.habits,
        HabitConfig(
            id="sleep",
            type="numeric",
            goal=8,
            unit=HabitLabel(en="h", th="ชม."),
            label=HabitLabel(en="sleep", th="นอน"),
        ),
    ]
    registry = HabitRegistry.from_config(config)
    end_date = date(2026, 8, 19)
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T09:00:00", "water", 500.0, None, "x", "reply"))
    db.insert_log(LogEntry(None, "owner", f"{end_date.isoformat()}T22:00:00", "sleep", 7.0, None, "x", "reply"))

    stats = compute_weekly_stats(db, config, registry, end_date, user_id="owner")
    summary = format_stats_summary(stats, registry, "en")

    assert i18n.t("stats_water_header", "en") in summary  # built-in unaffected
    assert i18n.t("stats_generic_numeric_header", "en", label="sleep", unit="h") in summary  # new habit, generic path
    db.close()


def test_pre_v070_rows_with_null_habit_type_aggregate_correctly(tmp_path):
    """AC7.5-review: rows from before migration 004 (i.e. `habit_type` is
    NULL, as backfill leaves any category it doesn't recognize) still
    aggregate correctly, because `sum_value`/`count`/`count_true` filter by
    `category`, not `habit_type` -- `habit_type` is metadata, not part of
    the aggregation key. Simulates a migrated-but-not-yet-typed row by
    inserting through raw SQL rather than `insert_log` (which always
    stamps `habit_type` for a new row)."""
    db = Database(tmp_path / "habits.db")
    db._conn.execute(
        "INSERT INTO logs (user_id, ts, category, value_num, value_text, raw_message, source, habit_type) "
        "VALUES ('owner', ?, 'water', 500.0, NULL, 'legacy row', 'reply', NULL)",
        ("2026-08-19T09:00:00",),
    )
    db._conn.commit()
    registry = default_registry()
    end_date = date(2026, 8, 19)

    stats = compute_weekly_stats(db, Config(), registry, end_date, user_id="owner")

    assert stats.get("water").total == 500.0
    db.close()
