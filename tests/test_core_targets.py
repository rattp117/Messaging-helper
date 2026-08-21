"""SPEC-v1.1.md "Undo menu + per-habit targets" -- shared-surface unit
tests for `core/targets.py` (`effective_goal`/`config_goal`/`is_goalable`),
built by the shared-surface pass ahead of the two parallel modules
(`undo-ui`, `targets`). The `targets` module's own `tests/test_targets.py`
exercises these through `/target ...` end-to-end; this file pins the
resolver's own contract in isolation, against a real on-disk SQLite DB
(no mocks -- same convention as `tests/test_migrations.py`).
"""

from __future__ import annotations

import pytest

from habit_assistant.config import Config
from habit_assistant.core import targets
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.storage.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "targets.db")
    yield database
    database.close()


def _habit(id_: str, type_: str, goal: float | None, **kw) -> Habit:
    defaults = dict(
        label_en=id_,
        label_th=id_,
        unit_en="u",
        unit_th="u",
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, goal=goal, **defaults)


# ---------------------------------------------------------------------------
# is_goalable (R-T3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_", ["numeric", "duration"])
def test_is_goalable_true_for_numeric_and_duration(type_):
    assert targets.is_goalable(_habit("x", type_, None)) is True


@pytest.mark.parametrize("type_", ["text", "boolean"])
def test_is_goalable_false_for_text_and_boolean(type_):
    assert targets.is_goalable(_habit("x", type_, None)) is False


# ---------------------------------------------------------------------------
# config_goal (R-T4): no DB read, water reads the legacy config location
# ---------------------------------------------------------------------------


def test_config_goal_water_reads_legacy_reminders_config():
    config = Config.model_validate({"reminders": {"water": {"goal_ml": 3000}}})
    water = _habit("water", "numeric", goal=2500)  # habit.goal deliberately differs
    assert targets.config_goal(water, config) == 3000


def test_config_goal_non_water_reads_habit_goal():
    config = Config()
    stretch = _habit("stretch", "duration", goal=None)
    assert targets.config_goal(stretch, config) is None
    juice = _habit("juice", "numeric", goal=1000)
    assert targets.config_goal(juice, config) == 1000


# ---------------------------------------------------------------------------
# effective_goal (R-T3): override wins when present; falls back to
# config_goal; never consults the DB for a non-goalable habit.
# ---------------------------------------------------------------------------


def test_effective_goal_falls_back_to_config_goal_with_no_override(db):
    config = Config()  # water default 2500
    water = _habit("water", "numeric", goal=2500)
    assert targets.effective_goal(db, water, config) == 2500


def test_effective_goal_prefers_db_override_over_config_default(db):
    config = Config()
    water = _habit("water", "numeric", goal=2500)
    db.set_target("water", 2000.0)
    assert targets.effective_goal(db, water, config) == 2000.0


def test_effective_goal_clearing_override_reverts_to_config_default(db):
    config = Config()
    water = _habit("water", "numeric", goal=2500)
    db.set_target("water", 2000.0)
    assert targets.effective_goal(db, water, config) == 2000.0
    db.clear_target("water")
    assert targets.effective_goal(db, water, config) == 2500


def test_effective_goal_override_on_previously_goalless_duration_habit(db):
    """R-T5b (OQ2): a target on a goal-able habit with NO config goal
    (e.g. stretch) makes effective_goal return the override -- the habit
    becomes goal-bearing purely because callers now see a non-None value."""
    config = Config()
    stretch = _habit("stretch", "duration", goal=None)
    assert targets.effective_goal(db, stretch, config) is None
    db.set_target("stretch", 20.0)
    assert targets.effective_goal(db, stretch, config) == 20.0
    db.clear_target("stretch")
    assert targets.effective_goal(db, stretch, config) is None


def test_effective_goal_never_consults_db_for_non_goalable_habit(db):
    """A text/boolean habit has no goal by construction (config.py's own
    validator forbids one); effective_goal must not even attempt a DB
    read for it, let alone honor a stray override row."""
    config = Config()
    diary = _habit("diary", "text", goal=None)
    assert targets.effective_goal(db, diary, config) is None

    # Even if a row somehow existed under this id, is_goalable must gate
    # the read -- prove the DB is never consulted by using a Database
    # stand-in whose get_target would raise if ever called.
    class _ExplodingGetTarget(Database):
        def get_target(self, habit_id: str) -> float | None:
            raise AssertionError("effective_goal must not read the DB for a non-goalable habit")

    exploding = _ExplodingGetTarget(db.db_path)
    try:
        assert targets.effective_goal(exploding, diary, config) is None
    finally:
        exploding.close()
