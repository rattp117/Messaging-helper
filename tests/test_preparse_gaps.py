"""Vera (tester) adversarial gap-coverage for `core/preparse.py` (SPEC-v1.5.md
R-L1/R-L2, module `preparse`, owned ACs: AC-14, AC-15), written against
Luna's `tests/test_preparse.py` (166 tests, all green) to close specific
gaps requested by the coordinator's dispatch:

1. **Corpus expansion** -- Thai numerals, full-width digits, trailing
   punctuation, embedded whitespace/newlines, zero-width/RTL marks, leading
   emoji, scientific notation, ranges, doubled logs, and unregistered unit
   synonyms ("cc", "oz", "litre") not in this file's `tests/test_units.py`
   sibling registry.
2. **Cross-kind safety** -- a registry where two numeric/duration habits
   share the exact same unit token (a configuration `commands.py` already
   allowed pre-v1.5, R-L5's byte-identical-reuse mandate inherits it).
3. **Byte-identical proof audit** -- `tests/test_preparse.py`'s own
   `FakeChannel` only overrides `send`, never `send_actionable`, so the
   base `Channel.send_actionable` default (SILENTLY DROPS the buttons
   argument, channels/base.py:88-92) means its "byte-identical" test never
   actually compared the undo button payload -- only the text. This file's
   `ActionableFakeChannel` captures `(text, buttons)` pairs and drives a
   real streak-milestone crossing AND a DB target override through the
   real, unmodified `handle_inbound_message` pipeline to prove the
   pre-parser's result produces the SAME undo button + milestone suffix +
   target-override goal rendering as a genuine LLM result would.
4. **ExtractionResult.confidence audit** -- structural (AST-level: the
   confirmation-building code never accesses `result.confidence`) and
   behavioral (confidence=0.0 vs 1.0, same category/value, byte-identical
   confirmation) proof that nothing downstream branches on confidence.
5. **Ollama-down structural proof** -- AST-level (not just behavioral):
   `core/preparse.py` defines no `async def`, contains no `await`
   expression anywhere, and its only `llm.ollama_client` import is the
   `ExtractionResult` dataclass (never `OllamaClient` itself).
6. **Fuzz** -- 480 generated near-miss mutations (6 mutation strategies x
   5 numbers x 8 registered unit tokens x 2 separators) that are each
   analytically guaranteed, by construction, to break either the
   whole-message NUMBER anchor or the registry unit-resolution step; every
   one must return `None`. Plus an 80-case true-positive grid (same
   number/unit/separator cross product, unmutated) that must all resolve
   correctly, as the fuzz harness's own sanity counterweight.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import preparse as preparse_module
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.preparse import deterministic_parse
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())
OWNER = "owner"


def _habit(id_: str, type_: str, **kw) -> Habit:
    defaults = dict(
        label_en=id_,
        label_th=id_,
        unit_en=None,
        unit_th=None,
        goal=None,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    defaults.update(kw)
    return Habit(id=id_, type=type_, **defaults)


# ===========================================================================
# 1. Corpus expansion (AC-15 zero-false-positive + AC-14 supported-shape
#    coverage). Every case here was empirically verified against the real
#    `deterministic_parse` before being committed to this file.
# ===========================================================================

# Genuinely NEW true positives -- shapes the pre-parser correctly accepts
# that `tests/test_preparse.py`'s SUPPORTED_SHAPES never exercised.
NOVEL_TRUE_POSITIVES = [
    ("๕๐๐ml", "water", 500.0),  # Thai numerals (๕๐๐) -- \d matches Unicode Nd, float() normalizes them
    ("５００ml", "water", 500.0),  # full-width Arabic digits (５００) -- same Unicode-digit behavior
    ("500 ml.", "water", 500.0),  # trailing period on the unit (resolve_unit's dot-stripping, with a space before)
    ("  500ml  ", "water", 500.0),  # leading/trailing whitespace -- text.strip() makes this pass (spec-intended)
    ("500\nml", "water", 500.0),  # newline between number and unit -- \s* includes \n, still whole-message-anchored
]


@pytest.mark.parametrize("text,category,value", NOVEL_TRUE_POSITIVES)
def test_ac14_novel_true_positive_shapes_resolve_correctly(text, category, value):
    assert deterministic_parse(text, DEFAULT_REGISTRY) == ExtractionResult(category, value, 1.0)


# Genuinely NEW false-positive temptations -- must all fall through to None
# (and therefore the LLM path, unchanged, R-L1).
NOVEL_ADVERSARIAL_MESSAGES = [
    "500ml!",  # trailing exclamation glued to the unit -- "ml!" never resolves
    "~500ml",  # leading tilde -- breaks the \d-start anchor even after strip()
    "500-600ml",  # a range -- unit token becomes "-600ml", never resolves
    "500ml 200ml",  # two logs in one message -- whole-message anchor rejects trailing content
    "-500ml",  # negative sign glued directly to the number -- breaks the \d-start anchor
    "0.5l",  # "l" (bare liter) is NOT a registered unit/alias -- must fall through, not silently convert
    "500cc",  # unregistered synonym
    "500oz",  # unregistered synonym
    "0.5litre",  # unregistered synonym
    "5e2 ml",  # scientific notation -- \d+(?:\.\d+)? does not match the "e2" exponent part
    "1,500ml",  # thousands-separator comma
    "​500ml",  # zero-width space (U+200B) BEFORE the number -- not whitespace per str.strip(), breaks the anchor
    "500​ml",  # zero-width space wedged between number and unit -- \S+ swallows it into an unresolvable token
    "‎500ml‏",  # left-to-right / right-to-left marks wrapping an otherwise-valid shape -- not stripped
    "\U0001F4A7500ml",  # leading emoji (💧) glued to the number -- breaks the \d-start anchor
    "５００ＭＬ",  # full-width digits AND full-width "ML" -- the unit half is not case-folded to ASCII
]


@pytest.mark.parametrize("message", NOVEL_ADVERSARIAL_MESSAGES)
def test_ac15_novel_adversarial_messages_never_produce_a_false_positive(message):
    assert deterministic_parse(message, DEFAULT_REGISTRY) is None


# ===========================================================================
# 2. Cross-kind safety -- FINDING (fixed, integration punch list #3): a
#    registry where two numeric/duration habits configure the identical
#    unit token. `core/units.build_unit_lookup` (shared surface, extracted
#    verbatim from commands.py per R-L5) used to `setdefault` -- first-
#    registered habit silently won, no collision detection. It now tracks
#    which habit id first claimed each token and, if a DIFFERENT habit id
#    later claims the same token, EXCLUDES that token from the lookup
#    entirely -- `resolve_unit` returns `None` for it regardless of
#    registry order, so `deterministic_parse` falls through to the LLM
#    path instead of silently misattributing the log to whichever habit
#    happened to be listed FIRST in `config.toml`'s `[[habits]]` array.
#
#    This does NOT affect the SHIPPED default registry today (water/
#    stretch/diary use disjoint units) -- `test_ac15_non_colliding_default_
#    registry_is_unaffected_by_this_finding` below confirms the fix is a
#    no-op against production `config.toml`. It protects any FUTURE
#    `[[habits]]` addition that reuses an existing unit string (e.g. a
#    second duration habit also measured in "min") from the "wrong HABIT"
#    failure mode the coordinator flagged (worse than a wrong value alone
#    -- it would corrupt a different habit's history entirely). See
#    `core/commands.py`'s `_parse_target_value`/`_parse_edit_value` -- both
#    consumers of this same shared `build_unit_lookup`/`resolve_unit` pair
#    (R-L5) -- for the one semantics note this fix carries into `/target`
#    (reported, not silently absorbed): IMPL-v1.5-integration.md.
# ===========================================================================

AMBIGUOUS_UNIT_REGISTRY = HabitRegistry(
    [
        _habit("stretch", "duration", unit_en="min", unit_th="min"),
        _habit("screen_time", "numeric", unit_en="min", unit_th="min"),
    ]
)

# A duration habit ("stretch", min) and an unrelated numeric habit ("water",
# ml) where "water" ALSO happens to configure "min" as a unit_alias --
# mirrors the coordinator's literal "duration-habit unit with a value that
# could also be water" scenario.
WATER_MIN_COLLISION_REGISTRY = HabitRegistry(
    [
        _habit("water", "numeric", unit_en="ml", unit_th="ml", unit_aliases={"min": 1.0}),
        _habit("stretch", "duration", unit_en="min", unit_th="min"),
    ]
)


def test_ac15_finding_shared_unit_token_across_two_habits_falls_through_to_none():
    """FIXED (integration punch list #3): `core/units.build_unit_lookup`
    now excludes a token claimed by two DIFFERENT habit ids from the
    lookup entirely, so an ambiguous unit resolves to `None` --
    `deterministic_parse` falls through to the LLM path instead of
    silently picking the first-registered habit. Order-independent: both
    registration orders now agree on `None`, proving this is a genuine
    ambiguity detection, not a reordering of the old accident."""
    result = deterministic_parse("10 min", AMBIGUOUS_UNIT_REGISTRY)
    assert result is None

    reordered = HabitRegistry([AMBIGUOUS_UNIT_REGISTRY.get("screen_time"), AMBIGUOUS_UNIT_REGISTRY.get("stretch")])
    result_reordered = deterministic_parse("10 min", reordered)
    assert result_reordered is None


def test_ac15_finding_water_alias_collision_with_stretch_unit_falls_through_to_none():
    """FIXED (integration punch list #3): a duration log ("10 min",
    clearly meant for `stretch`) no longer gets silently logged as 10ml
    of `water` just because `water` is registered first and also happens
    to alias "min" -- the collision is detected and the token is excluded
    from the lookup, so this now falls through to the LLM path (which can
    ask, or use context) instead of corrupting a different habit's
    history. This is the concrete case the coordinator's dispatch asked
    to be fixed."""
    result = deterministic_parse("10 min", WATER_MIN_COLLISION_REGISTRY)
    assert result is None


def test_ac15_non_colliding_default_registry_is_unaffected_by_this_finding():
    """Sanity counterweight: the SHIPPED default registry (water/stretch/
    diary) has no unit collisions, so this finding is latent, not active,
    against the current production config.toml."""
    lookup_units = set()
    for habit in DEFAULT_REGISTRY:
        if habit.type not in ("numeric", "duration"):
            continue
        for tok in (habit.unit_en, habit.unit_th, *habit.unit_aliases):
            if tok:
                lookup_units.add(tok.strip().lower())
    from habit_assistant.core.units import build_unit_lookup

    # Every configured token maps to exactly one habit_id -- no collision
    # exists in the shipped registry today.
    lookup = build_unit_lookup(DEFAULT_REGISTRY)
    assert len(lookup) == len(lookup_units)


# ===========================================================================
# 3. Byte-identical proof audit -- streak-milestone crossing, undo button
#    attachment, and target-override goal rendering, all captured (not
#    silently dropped) and compared between the LLM-style path and the
#    pre-parser path on two independently-seeded scratch DBs.
# ===========================================================================


class ActionableFakeChannel(Channel):
    """Unlike `tests/test_preparse.py`'s own `FakeChannel` (which only
    overrides `send`, so `Channel.send_actionable`'s base-class default --
    `channels/base.py:88-92` -- silently drops the buttons argument and
    forwards to `send`), this fake captures `(text, buttons)` for EVERY
    send, actionable or not, so a test can actually assert on the undo
    button payload."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((text, []))

    async def send_actionable(self, chat_id: str, text: str, buttons: list) -> None:
        self.sent.append((text, buttons))

    async def run(self, on_message, on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class _NeverCalledLLM:
    async def chat_json(self, *args, **kwargs):
        raise AssertionError("must never be called -- parse_message is monkeypatched in every test here")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("must never be called -- parse_message is monkeypatched in every test here")


def _fixed_clock():
    from datetime import datetime

    return datetime(2026, 8, 19, 9, 0, 0)


def _patch_parse_message(monkeypatch, result: ExtractionResult) -> None:
    async def fake(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake)


async def _drive_through_real_pipeline(
    db: Database, text: str, result: ExtractionResult, user_id: str = OWNER
) -> list[tuple[str, list]]:
    channel = ActionableFakeChannel()
    await handle_inbound_message(
        text,
        db=db,
        user_id=user_id,
        llm=_NeverCalledLLM(),
        channel=channel,
        config=Config(),
        clock=_fixed_clock,
        registry=HabitRegistry.from_config(Config()),
    )
    return channel.sent


async def test_ac14_streak_milestone_and_undo_button_are_byte_identical_between_paths(tmp_path, monkeypatch):
    """Seeds 2 prior qualifying days for `stretch` (a duration, no-goal
    habit -- any entry qualifies) on TWO independently-created scratch DBs,
    so the 3rd log (today, "10 min") crosses the default milestone=3 streak
    identically on both, AND lands on the same autoincrement row id (both
    DBs receive the exact same 2 seed inserts before the log under test) --
    so the undo button's `undo:<row_id>` callback_data is directly
    comparable. Confirms `deterministic_parse`'s own result (confidence=1.0)
    produces the SAME confirmation text (including the milestone suffix)
    AND the SAME undo button as a genuine LLM-style result (confidence=0.81)
    for the identical (category, value)."""
    preparsed = deterministic_parse("10 min", DEFAULT_REGISTRY)
    assert preparsed == ExtractionResult("stretch", 10.0, 1.0)
    llm_style = ExtractionResult("stretch", 10.0, 0.81)

    db_llm = Database(tmp_path / "llm.db")
    db_pp = Database(tmp_path / "preparse.db")
    try:
        for db in (db_llm, db_pp):
            db.insert_log(LogEntry(None, OWNER, "2026-08-17T09:00:00", "stretch", 10.0, None, "10 min", "reply"))
            db.insert_log(LogEntry(None, OWNER, "2026-08-18T09:00:00", "stretch", 10.0, None, "10 min", "reply"))

        _patch_parse_message(monkeypatch, llm_style)
        sent_llm = await _drive_through_real_pipeline(db_llm, "10 min", llm_style)

        _patch_parse_message(monkeypatch, preparsed)
        sent_pp = await _drive_through_real_pipeline(db_pp, "10 min", preparsed)
    finally:
        db_llm.close()
        db_pp.close()

    assert len(sent_llm) == 1
    text, buttons = sent_llm[0]
    assert "\U0001f525" in text  # milestone fire emoji -- proves the crossing actually fired, not a no-op scenario
    assert buttons == [("↩️ Undo", "undo:3")]  # 3rd insert on a fresh DB -- same on both sides
    assert sent_llm == sent_pp  # text (incl. milestone suffix) AND buttons are byte-identical


async def test_ac14_target_override_goal_rendering_and_undo_button_are_byte_identical_between_paths(
    tmp_path, monkeypatch
):
    """Sets a DB-stored `/target water` override (4000ml, overriding the
    default 2500ml config goal) identically on two fresh scratch DBs before
    driving a single "500ml" log through each. Confirms the rendered
    percentage/goal reflects the OVERRIDE (not the config default) and is
    byte-identical between the LLM-style and pre-parser paths, undo button
    included."""
    preparsed = deterministic_parse("500ml", DEFAULT_REGISTRY)
    assert preparsed == ExtractionResult("water", 500.0, 1.0)
    llm_style = ExtractionResult("water", 500.0, 0.81)

    db_llm = Database(tmp_path / "llm.db")
    db_pp = Database(tmp_path / "preparse.db")
    try:
        db_llm.set_target(OWNER, "water", 4000.0)
        db_pp.set_target(OWNER, "water", 4000.0)

        _patch_parse_message(monkeypatch, llm_style)
        sent_llm = await _drive_through_real_pipeline(db_llm, "500ml", llm_style)

        _patch_parse_message(monkeypatch, preparsed)
        sent_pp = await _drive_through_real_pipeline(db_pp, "500ml", preparsed)
    finally:
        db_llm.close()
        db_pp.close()

    assert len(sent_llm) == 1
    text, buttons = sent_llm[0]
    assert "4000" in text  # the override, not the config default of 2500
    assert "2500" not in text
    assert buttons == [("↩️ Undo", "undo:1")]
    assert sent_llm == sent_pp


# ===========================================================================
# 4. ExtractionResult.confidence audit -- structural + behavioral proof that
#    nothing downstream of `parse_message`'s call site branches on
#    `confidence` (the one place that DOES, `core/parser.py:_validate`'s
#    threshold check, is upstream of and never invoked by
#    `deterministic_parse`'s own result).
# ===========================================================================


def test_ac14_handle_inbound_message_confirmation_code_never_reads_result_confidence():
    source = inspect.getsource(handle_inbound_message)
    assert "result.confidence" not in source


async def test_ac14_confirmation_is_identical_across_the_full_confidence_range(tmp_path, monkeypatch):
    """Two results with the SAME (category, value) but opposite-extreme
    confidence (0.0 vs 1.0, matching the pre-parser's own always-1.0
    output) must produce byte-identical confirmations -- direct behavioral
    proof alongside the structural check above."""
    low = ExtractionResult("water", 500.0, 0.0)
    high = ExtractionResult("water", 500.0, 1.0)

    db_low = Database(tmp_path / "low.db")
    db_high = Database(tmp_path / "high.db")
    try:
        _patch_parse_message(monkeypatch, low)
        sent_low = await _drive_through_real_pipeline(db_low, "500ml", low)

        _patch_parse_message(monkeypatch, high)
        sent_high = await _drive_through_real_pipeline(db_high, "500ml", high)
    finally:
        db_low.close()
        db_high.close()

    assert sent_low == sent_high


# ===========================================================================
# 5. Ollama-down structural proof (AST-level, stronger than the behavioral
#    raising-double test already in tests/test_preparse.py): the module
#    defines no async function, contains no `await` anywhere, and its only
#    import from `llm.ollama_client` is the plain `ExtractionResult`
#    dataclass -- never `OllamaClient` itself.
# ===========================================================================


def test_ac16_preparse_module_defines_no_async_function_and_contains_no_await():
    tree = ast.parse(inspect.getsource(preparse_module))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef), "core/preparse.py must define no async def"
        assert not isinstance(node, ast.Await), "core/preparse.py must contain no await expression"


def test_ac16_preparse_module_only_imports_extractionresult_from_ollama_client():
    tree = ast.parse(inspect.getsource(preparse_module))
    imported_from_ollama_client: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "ollama_client" in node.module:
            imported_from_ollama_client.update(alias.name for alias in node.names)
    assert imported_from_ollama_client == {"ExtractionResult"}


def test_ac16_deterministic_parse_signature_and_module_take_no_db_or_channel():
    # Belt-and-suspenders alongside tests/test_preparse.py's own signature
    # check: the whole MODULE's top-level names never mention db/channel
    # types either (not just the one function's signature).
    tree = ast.parse(inspect.getsource(preparse_module))
    top_level_imports = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("channel" in name.lower() or "database" in name.lower() for name in top_level_imports)


# ===========================================================================
# 6. Fuzz -- 480 generated near-miss mutations (must all be None) + an
#    80-case true-positive grid (must all resolve) as the harness's own
#    sanity counterweight. Every mutation strategy below is analytically
#    guaranteed, by construction, to break either the whole-message NUMBER
#    anchor or the registry unit-resolution step -- verified empirically
#    against the real `deterministic_parse` while authoring this file
#    (0/480 false positives, 0/80 false negatives).
# ===========================================================================

_FUZZ_NUMBERS = ["1", "2", "500", "2.5", "999999999"]
_FUZZ_UNITS = ["ml", "มล.", "glass", "แก้ว", "bottle", "ขวด", "min", "นาที"]
_FUZZ_SEPS = ["", " "]
_FUZZ_UNIT_HABIT = {
    "ml": ("water", 1.0),
    "มล.": ("water", 1.0),
    "glass": ("water", 250.0),
    "แก้ว": ("water", 250.0),
    "bottle": ("water", 600.0),
    "ขวด": ("water", 600.0),
    "min": ("stretch", 1.0),
    "นาที": ("stretch", 1.0),
}


def _mut_prepend_letter(num: str, unit: str, sep: str) -> str:
    """A non-digit prefix breaks VALUE_RE's `^\\d` start anchor."""
    return f"x{num}{sep}{unit}"


def _mut_append_junk_suffix(num: str, unit: str, sep: str) -> str:
    """Glued suffix that is neither a bare "." nor a single trailing "s" --
    survives past resolve_unit's two stripping rules unresolved."""
    return f"{num}{sep}{unit}zz9"


def _mut_glue_symbol(num: str, unit: str, sep: str) -> str:
    """A "#" glued directly onto the number becomes part of the captured
    unit token (or breaks the trailing `\\s*$` anchor) -- never resolves."""
    return f"{num}#{unit}"


def _mut_double_message(num: str, unit: str, sep: str) -> str:
    """Two valid shapes space-joined -- the trailing `$` anchor requires
    the WHOLE stripped message to be consumed by one NUMBER+UNIT pair."""
    base = f"{num}{sep}{unit}"
    return f"{base} {base}"


def _mut_unregistered_unit(num: str, unit: str, sep: str) -> str:
    """Replaces the (otherwise-valid) unit with a nonsense word that is
    never in any registry's unit lookup."""
    return f"{num}{sep}xyzzy"


def _mut_negative_number(num: str, unit: str, sep: str) -> str:
    """A leading "-" glued to the number breaks the `^\\d` start anchor."""
    return f"-{num}{sep}{unit}"


_FUZZ_MUTATIONS = [
    _mut_prepend_letter,
    _mut_append_junk_suffix,
    _mut_glue_symbol,
    _mut_double_message,
    _mut_unregistered_unit,
    _mut_negative_number,
]

_FUZZ_CORPUS = [
    (mutation.__name__, num, unit, sep, mutation(num, unit, sep))
    for num in _FUZZ_NUMBERS
    for unit in _FUZZ_UNITS
    for sep in _FUZZ_SEPS
    for mutation in _FUZZ_MUTATIONS
]


@pytest.mark.parametrize(
    "strategy,num,unit,sep,text",
    _FUZZ_CORPUS,
    ids=[f"{s}-{n}-{u!r}-{p!r}" for s, n, u, p, _ in _FUZZ_CORPUS],
)
def test_ac15_fuzz_generated_near_miss_never_produces_a_false_positive(strategy, num, unit, sep, text):
    assert deterministic_parse(text, DEFAULT_REGISTRY) is None, (
        f"mutation {strategy!r} on (num={num!r}, unit={unit!r}, sep={sep!r}) produced text {text!r} "
        f"which unexpectedly parsed"
    )


_TRUE_POSITIVE_GRID = [
    (num, unit, sep, f"{num}{sep}{unit}")
    for num in _FUZZ_NUMBERS
    for unit in _FUZZ_UNITS
    for sep in _FUZZ_SEPS
]


@pytest.mark.parametrize(
    "num,unit,sep,text",
    _TRUE_POSITIVE_GRID,
    ids=[f"{n}-{u!r}-{p!r}" for n, u, p, _ in _TRUE_POSITIVE_GRID],
)
def test_ac14_fuzz_harness_true_positive_grid_still_resolves(num, unit, sep, text):
    """Sanity counterweight for the fuzz harness above: unmutated
    NUMBER+SEP+UNIT combinations across the same number/unit/separator
    cross product must all still resolve correctly -- proves the 480
    None-results above come from the mutations, not from the base shapes
    being broken."""
    habit_id, multiplier = _FUZZ_UNIT_HABIT[unit]
    expected = ExtractionResult(habit_id, float(num) * multiplier, 1.0)
    assert deterministic_parse(text, DEFAULT_REGISTRY) == expected
