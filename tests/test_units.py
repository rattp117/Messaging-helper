"""SPEC-v1.5.md §4 R-L5 -- unit tests for `core/units.py`, extracted from
`core/commands.py` so `core/preparse.py` (v1.5.0's deterministic
pre-parser, module `preparse`) can reuse the exact same unit-lookup/
-resolution logic instead of copy-pasting it.

`tests/test_commands.py`/`tests/test_targets.py`/`tests/test_target_nl.py`/
`tests/test_core_targets.py` (unmodified, all still passing) are the
byte-identical regression guard for AC-2 -- this file instead pins the
extracted helpers' own contract in isolation, so `core/preparse.py`'s own
test file can trust this module's behavior without re-deriving it.
"""

from __future__ import annotations

from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.units import VALUE_RE, build_unit_lookup, resolve_unit


def _habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_, label_th=id_, unit_en=None, unit_th=None, goal=None,
        reminder_times=(), reminder_text_en=None, reminder_text_th=None, unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


# ---------------------------------------------------------------------------
# VALUE_RE -- NUMBER, optionally followed by a unit token.
# ---------------------------------------------------------------------------


def test_value_re_matches_a_bare_integer():
    match = VALUE_RE.match("500")
    assert match is not None
    assert match.group("num") == "500"
    assert match.group("unit") is None


def test_value_re_matches_a_decimal_with_a_unit():
    match = VALUE_RE.match("2.5 L")
    assert match is not None
    assert match.group("num") == "2.5"
    assert match.group("unit") == "L"


def test_value_re_matches_a_number_glued_directly_to_a_unit():
    match = VALUE_RE.match("500ml")
    assert match is not None
    assert match.group("num") == "500"
    assert match.group("unit") == "ml"


def test_value_re_matches_a_thai_unit_token():
    match = VALUE_RE.match("2 แก้ว")
    assert match is not None
    assert match.group("num") == "2"
    assert match.group("unit") == "แก้ว"


def test_value_re_does_not_match_a_leading_negative_sign():
    """Unlike commands.py's own separate `_TARGET_VALUE_RE` (which DOES
    accept a leading '-' for target values, AC15's own carve-out), the
    shared `VALUE_RE` stays positive-only -- a physical quantity actually
    logged is never negative. `_TARGET_VALUE_RE` was deliberately NOT
    extracted here (SPEC-v1.5.md's own interface list names only
    `VALUE_RE`, not a second target-specific regex)."""
    assert VALUE_RE.match("-5") is None


def test_value_re_does_not_match_a_sentence():
    assert VALUE_RE.match("from now on I want to drink 2.5L a day") is None


def test_value_re_does_not_match_empty_string():
    assert VALUE_RE.match("") is None


# ---------------------------------------------------------------------------
# build_unit_lookup / resolve_unit
# ---------------------------------------------------------------------------


def _registry() -> HabitRegistry:
    return HabitRegistry(
        [
            _habit("water", "numeric", unit_en="ml", unit_th="มล.", unit_aliases={"l": 1000.0, "liter": 1000.0}),
            _habit("stretch", "duration", unit_en="min", unit_th="นาที"),
            _habit("diary", "text"),  # non-numeric/duration -- never in the lookup
        ]
    )


def test_build_unit_lookup_includes_every_numeric_and_duration_units_and_aliases():
    lookup = build_unit_lookup(_registry())
    assert lookup["ml"] == ("water", 1.0)
    assert lookup["มล."] == ("water", 1.0)
    assert lookup["l"] == ("water", 1000.0)
    assert lookup["liter"] == ("water", 1000.0)
    assert lookup["min"] == ("stretch", 1.0)
    assert lookup["นาที"] == ("stretch", 1.0)


def test_build_unit_lookup_excludes_text_and_boolean_habits():
    lookup = build_unit_lookup(_registry())
    assert not any(habit_id == "diary" for habit_id, _ in lookup.values())


def test_build_unit_lookup_excludes_a_token_shared_by_two_different_habits():
    """SPEC-v1.5.md integration punch list #3 (TEST-v1.5-preparse.md
    Finding 1): a token claimed by two DIFFERENT habit ids is a genuine
    ambiguity, not a first-registered-wins ordering accident -- excluded
    from the lookup entirely (order-independent), so `resolve_unit`
    returns `None` for it rather than silently picking either habit."""
    registry = HabitRegistry(
        [
            _habit("a", "numeric", unit_en="unit"),
            _habit("b", "numeric", unit_en="unit"),  # same token, different habit
        ]
    )
    lookup = build_unit_lookup(registry)
    assert "unit" not in lookup

    reordered = HabitRegistry([_habit("b", "numeric", unit_en="unit"), _habit("a", "numeric", unit_en="unit")])
    assert "unit" not in build_unit_lookup(reordered)


def test_build_unit_lookup_same_habit_reclaiming_its_own_token_is_not_a_collision():
    """A habit whose `unit_en` and an alias happen to coincide (or any
    other way the SAME habit id registers a token twice) is not
    ambiguous -- that habit's first value for the token stands, exactly
    as the old first-match-wins behavior did for the non-colliding case."""
    registry = HabitRegistry(
        [
            _habit("a", "numeric", unit_en="unit", unit_aliases={"unit": 2.0}),
        ]
    )
    lookup = build_unit_lookup(registry)
    assert lookup["unit"] == ("a", 1.0)  # unit_en registered first, alias's 2.0 is a no-op


def test_resolve_unit_exact_match():
    lookup = build_unit_lookup(_registry())
    assert resolve_unit(lookup, "ml") == ("water", 1.0)


def test_resolve_unit_strips_a_trailing_dot():
    lookup = build_unit_lookup(_registry())
    # "มล." is already in the lookup verbatim; also confirm a token like
    # "min." (not itself configured, but ending in the same way) resolves
    # via the dot-strip fallback against "min".
    assert resolve_unit(lookup, "min.") == ("stretch", 1.0)


def test_resolve_unit_singularizes_a_trailing_s():
    lookup = build_unit_lookup(_registry())
    assert resolve_unit(lookup, "mins") == ("stretch", 1.0)
    assert resolve_unit(lookup, "liters") == ("water", 1000.0)


def test_resolve_unit_returns_none_for_an_unknown_unit():
    lookup = build_unit_lookup(_registry())
    assert resolve_unit(lookup, "furlongs") is None


def test_resolve_unit_does_not_guess_irregular_plurals():
    """`unit_aliases` is the escape hatch for irregular plurals (e.g.
    "glasses") -- a bare trailing-"s" strip alone doesn't cover them, and
    `resolve_unit` deliberately doesn't try to."""
    registry = HabitRegistry([_habit("water", "numeric", unit_aliases={"glass": 250.0})])
    lookup = build_unit_lookup(registry)
    assert resolve_unit(lookup, "glass") == ("water", 250.0)
    assert resolve_unit(lookup, "glasses") is None  # "glasse" isn't in the lookup either
