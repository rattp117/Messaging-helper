"""Per-habit target/goal resolution (SPEC-v1.1.md §4 R-T3/R-T4). Consolidates
the three pre-v1.1 goal sources -- `config.reminders.water.goal_ml` (the
legacy water default), `habit.goal` (every other habit's config default),
and `core/streaks.py`'s own (now-removed) `effective_goal` helper -- into
one function every goal-consuming module reads through: the reminder
goal-met skip, streaks/milestones, the daily summary, the weekly review,
chart goal-lines, and `main.py`'s confirmation/undo/edit percentages
(SPEC-v1.1.md §4 R-T5).

A per-habit override lives in the DB (`habit_targets`, migration 005) and
takes priority over the config default when present -- read live on every
call, no caching, so a `/target` change (or its later clearing) takes
effect immediately (R-T11) with no restart and no config edit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from habit_assistant.core.habits import Habit

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database


def is_goalable(habit: Habit) -> bool:
    """R-T3: only `numeric`/`duration` habits can carry a goal -- mirrors
    `config.py`'s `HabitConfig._unit_and_goal_match_type` validator, which
    already forbids a goal on `text`/`boolean` at config-load time. This is
    the same rule applied to a *target override*, which isn't config-time
    validated (a target is set at runtime, from chat)."""
    return habit.type in ("numeric", "duration")


def config_goal(habit: Habit, config: "Config") -> float | None:
    """R-T4: the config-declared base goal, with no DB read -- used to
    report "(default ...)" in `/target` replies and to detect "override
    differs from default". The built-in `water` habit's base goal has
    always lived in the legacy `config.reminders.water.goal_ml` (carried
    forward byte-identical from the now-removed `core/streaks.py:
    effective_goal` -- SPEC-v0.7.md's "byte-identical to v0.6.0" contract);
    every other habit just uses its own configured `goal`."""
    if habit.id == "water":
        return config.reminders.water.goal_ml
    return habit.goal


def effective_goal(db: "Database", habit: Habit, config: "Config") -> float | None:
    """R-T3: the goal actually used everywhere a goal is consulted -- a
    DB-stored override (`habit_targets`, R-T1) for this habit if one
    exists, else its `config_goal()`. An override may exist for ANY
    goal-able habit, including one with no config goal (e.g. `stretch`) --
    R-T5b: that habit becomes goal-bearing going forward, purely because
    every caller here starts comparing against a non-None value. A
    non-goalable habit (text/boolean) never consults the override store at
    all (`is_goalable` gates the DB read), since none can exist for it by
    construction (R-T10's `set` op refuses text/boolean)."""
    if is_goalable(habit):
        override = db.get_target(habit.id)
        if override is not None:
            return override
    return config_goal(habit, config)
