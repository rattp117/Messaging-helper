"""SPEC-v1.1.md "Undo menu + per-habit targets" -- shared-surface /
integration ACs owned by the shared-surface build itself (SPEC-v1.1.md §11:
"ACs verified during the shared-surface/integration step"): AC21, AC22,
AC23, AC25, AC26 (effective-goal unification across every consumer) and the
non-confirmation half of AC31 (a target on a previously goal-less habit).
AC24 (byte-identical with no override) is the existing ~700-test suite
passing unmodified, not re-derived here. AC1-AC11 (undo-ui) and AC13-AC20/
AC27-AC30/AC32/AC34 (targets) belong to their own parallel modules.

No mocks for the DB (real on-disk SQLite via tmp_path, mirroring
tests/test_streaks.py/test_migrations.py's own convention); only
Telegram/Ollama are faked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import charts, reminders, streaks, targets
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.review import compute_weekly_stats
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry


def _seed(db: Database, ts: str, category: str, value_num: float | None) -> int:
    return db.insert_log(LogEntry(None, ts, category, value_num, None, "x", "reply"))


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return "noted"


def patch_parse_message(monkeypatch, result: ExtractionResult) -> None:
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


@pytest.fixture
def registry():
    return HabitRegistry.from_config(Config())


# ---------------------------------------------------------------------------
# AC21: reminders._goal_already_met compares against a stored override, not
# the config default.
# ---------------------------------------------------------------------------


def _today_ts(config: Config, hour: int = 9) -> str:
    """`_goal_already_met` compares against `_today_str(config)` (the REAL
    current date in `config.app.timezone`) -- seed at that same date, not a
    hardcoded past date, so this test doesn't rot as real time passes."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    today = _dt.now(ZoneInfo(config.app.timezone)).date()
    return f"{today.isoformat()}T{hour:02d}:00:00"


async def test_goal_already_met_uses_db_override_not_config_default(db):
    config = Config()  # water config default 2500
    water = HabitRegistry.from_config(config).get("water")
    _seed(db, _today_ts(config), "water", 1000.0)  # under 2500, over an override of 500

    assert reminders._goal_already_met(db, water, config) is False  # under both

    db.set_target("water", 500.0)
    assert reminders._goal_already_met(db, water, config) is True  # 1000 >= override 500


async def test_goal_already_met_applies_to_previously_goalless_duration_habit(db):
    """R-T5b/AC31 (reminder-skip half): stretch has no config goal, so the
    skip never applied pre-v1.1; once a target is set it does, and clearing
    it reverts to "never skip"."""
    config = Config()
    stretch = HabitRegistry.from_config(config).get("stretch")
    _seed(db, _today_ts(config), "stretch", 20.0)

    assert reminders._goal_already_met(db, stretch, config) is False  # no goal at all yet

    db.set_target("stretch", 20.0)
    assert reminders._goal_already_met(db, stretch, config) is True  # 20 >= target 20

    db.clear_target("stretch")
    assert reminders._goal_already_met(db, stretch, config) is False  # back to "no goal"


# ---------------------------------------------------------------------------
# AC22: day_qualifies / compute_streak / daily summary / weekly review /
# chart goal-line all reflect a stored override immediately.
# ---------------------------------------------------------------------------


def test_day_qualifies_and_compute_streak_use_override(db, registry):
    config = Config()
    water = registry.get("water")
    _seed(db, "2026-08-19T09:00:00", "water", 1000.0)

    assert streaks.day_qualifies(db, config, water, "2026-08-19") is False  # under config default 2500

    db.set_target("water", 1000.0)
    assert streaks.day_qualifies(db, config, water, "2026-08-19") is True  # meets override
    assert streaks.compute_streak(db, config, water, date(2026, 8, 19)) == 1


def test_daily_summary_reflects_override(db, registry):
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 1000.0)
    db.set_target("water", 1000.0)

    lines = streaks.compute_daily_summary(db, config, registry, date(2026, 8, 19))
    water_line = next(line for line in lines if line.habit.id == "water")
    assert water_line.goal == 1000.0
    assert water_line.total == 1000.0


def test_weekly_review_stats_reflect_override(db, registry):
    config = Config()
    for offset in range(7):
        _seed(db, f"2026-08-{13 + offset:02d}T09:00:00", "water", 1000.0)
    db.set_target("water", 1000.0)

    stats = compute_weekly_stats(db, config, registry, date(2026, 8, 19))
    water_stats = stats.get("water")
    assert all(day.goal == 1000.0 for day in water_stats.days)
    assert all(day.pct == 100.0 for day in water_stats.days)


def test_chart_goal_line_reflects_override(db, registry, monkeypatch):
    config = Config()
    _seed(db, "2026-08-19T09:00:00", "water", 1000.0)
    db.set_target("water", 1000.0)
    water = registry.get("water")

    captured_goals: list[float | None] = []

    def fake_render_bar_chart(labels, values, title, ylabel, goal):
        captured_goals.append(goal)
        return b"fake-png-bytes"

    monkeypatch.setattr(charts, "_render_bar_chart", fake_render_bar_chart)
    if not charts.MATPLOTLIB_AVAILABLE:
        pytest.skip("matplotlib not installed; chart rendering short-circuits before the goal line is computed")

    charts.render_habit_chart(db, config, water, date(2026, 8, 19), "en")
    assert captured_goals == [1000.0]


# ---------------------------------------------------------------------------
# AC23: confirmation / undo / edit percentages read against a stored
# override (through the real handle_inbound_message path).
# ---------------------------------------------------------------------------


async def test_water_confirmation_percentage_uses_override(db, registry, monkeypatch):
    config = Config()
    db.set_target("water", 2000.0)
    patch_parse_message(monkeypatch, ExtractionResult(category="water", value=500.0, confidence=0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "500ml",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=config,
        registry=registry,
        clock=lambda: datetime(2026, 8, 19, 9, 0, 0),
    )

    assert len(channel.sent) == 1
    assert "500 / 2000 ml (25%)" in channel.sent[0]


async def test_generic_numeric_confirmation_percentage_uses_override(db, monkeypatch):
    config = Config.model_validate(
        {
            "habits": [
                {
                    "id": "juice",
                    "type": "numeric",
                    "goal": 1000,
                    "label": {"en": "juice", "th": "juice"},
                    "unit": {"en": "ml", "th": "ml"},
                }
            ]
        }
    )
    registry = HabitRegistry.from_config(config)
    db.set_target("juice", 500.0)
    patch_parse_message(monkeypatch, ExtractionResult(category="juice", value=250.0, confidence=0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "250ml juice",
        db=db,
        llm=FakeLLM(),
        channel=channel,
        config=config,
        registry=registry,
        clock=lambda: datetime(2026, 8, 19, 9, 0, 0),
    )

    assert len(channel.sent) == 1
    assert "250 / 500 ml (50%)" in channel.sent[0]


# ---------------------------------------------------------------------------
# AC25: an override persists across a process restart (a fresh Database
# instance against the same file).
# ---------------------------------------------------------------------------


def test_override_persists_across_a_fresh_database_instance(tmp_path, registry):
    config = Config()
    db_path = tmp_path / "restart.db"

    first = Database(db_path)
    first.set_target("water", 1800.0)
    first.close()

    second = Database(db_path)
    try:
        water = registry.get("water")
        assert targets.effective_goal(second, water, config) == 1800.0
    finally:
        second.close()


# ---------------------------------------------------------------------------
# AC26: compute_streak's backward walk resolves the goal at most once per
# call, not once per day.
# ---------------------------------------------------------------------------


def test_compute_streak_reads_get_target_at_most_once_per_call(db, registry):
    config = Config()
    water = registry.get("water")
    for offset in range(5):
        _seed(db, f"2026-08-{15 + offset:02d}T09:00:00", "water", 2500.0)
    db.set_target("water", 2500.0)

    calls = {"n": 0}
    real_get_target = Database.get_target

    def counting_get_target(self, habit_id):
        calls["n"] += 1
        return real_get_target(self, habit_id)

    import habit_assistant.storage.db as db_module

    original = db_module.Database.get_target
    db_module.Database.get_target = counting_get_target
    try:
        streak = streaks.compute_streak(db, config, water, date(2026, 8, 19))
    finally:
        db_module.Database.get_target = original

    assert streak == 5
    assert calls["n"] == 1
