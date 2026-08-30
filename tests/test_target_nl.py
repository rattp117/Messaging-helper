"""SPEC-v1.1.md "Undo menu + per-habit targets", Feature 2 full-NL
target-setting (OQ3, R-T12-R-T16): `core/target_nl.py`'s cheap bilingual
gate (`looks_like_target_phrasing`) and fail-closed LLM intent classifier
(`classify_target_intent`), mirroring `tests/test_query.py`'s own
`OllamaClient` + `httpx.MockTransport` pattern so the classifier's actual
`chat_json` call, fallback chain, and off-schema handling genuinely run,
not just a hand-rolled stub.

`main.py`'s NL-target routing seam (the gate -> classify -> `execute_target`
-> return-with-no-log-row chain, plus the Ollama-down skip) is explicitly
deferred to the integration step (SPEC-v1.1.md §11) -- not implemented by
this module (see IMPL-v1.1-targets.md's "Known limitations" / "wiring
instructions"). This file proves the two functions `main.py` will call are
independently correct: AC29/AC30 (a valid EN/TH intent classifies and,
combined with `execute_target`, sets the right target) and AC31/AC32/AC34
(goal-less-habit intent, and every fail-closed path) are exercised end to
end EXCEPT for the "no `logs` row is written" / "outage skips the LLM call
entirely" halves, which require `main.py`'s routing and are the shared/
integration Vera pass's job per SPEC-v1.1.md §11's AC ownership table.
"""

from __future__ import annotations

import json

import httpx
import pytest

from habit_assistant.config import Config
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.target_nl import (
    TargetIntent,
    build_target_intent_schema,
    classify_target_intent,
    looks_like_target_phrasing,
)
from habit_assistant.core.targets import effective_goal
from habit_assistant.core.targets_command import execute_target
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import build_target_intent_system_prompt, build_target_intent_user_prompt
from habit_assistant.storage.db import Database

# SPEC-LINE.md §4 R-S7/R-B3 (shared surface): `classify_target_intent` is
# the whole-block LLM classifier R-B3 skips entirely in no-LLM mode (NL
# target phrasing has no deterministic fallback; the reply points at the
# explicit /target command instead). Branch-N/A, not deleted.
pytestmark = pytest.mark.llm_only

DEFAULT_REGISTRY = HabitRegistry.from_config(Config())
OWNER = "owner"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "target_nl.db")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


def make_llm(response_json: dict | None, *, status: int = 200) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if response_json is None:
            return httpx.Response(status)
        return httpx.Response(200, json={"message": {"content": json.dumps(response_json)}})

    transport = httpx.MockTransport(handler)
    return OllamaClient(
        "http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=httpx.AsyncClient(transport=transport)
    )


# ===========================================================================
# R-T13.1 -- looks_like_target_phrasing: the cheap bilingual cost gate.
# ===========================================================================

GATE_HITS = [
    "from now on I want to drink 2.5L a day",
    "let's aim for 3 liters of water each day",
    "change my water target to 2 bottles a day",
    "from now on I want to stretch 20 minutes a day",
    "I want to hit my goal every day",
    "ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร",
    "ตั้งแต่นี้ขอตั้งเป้ายืดเส้นวันละ 20 นาที",
]

GATE_MISSES = [
    "I drank 2.5L",
    "500ml",
    "did 10 min stretch",
    "ดื่มน้ำ 2 แก้ว",
    "today was a good day",
]


@pytest.mark.parametrize("text", GATE_HITS)
def test_gate_matches_forward_looking_daily_goal_phrasing(text):
    assert looks_like_target_phrasing(text) is True


@pytest.mark.parametrize("text", GATE_MISSES)
def test_gate_does_not_match_plain_logs(text):
    assert looks_like_target_phrasing(text) is False


# ===========================================================================
# AC29 -- EN free-form "from now on I want to drink 2.5L a day" ->
# classify_target_intent returns (water, 2500); combined with
# execute_target, the target actually gets set (main.py's own "return with
# no log row" half is integration-owned, not tested here).
# ===========================================================================


async def test_ac29_english_free_form_classifies_water_2500():
    llm = make_llm({"category": "water", "goal": 2500, "confidence": 0.9})
    intent = await classify_target_intent(
        "from now on I want to drink 2.5L a day", llm, DEFAULT_REGISTRY, Config()
    )
    assert intent == TargetIntent(habit_id="water", goal_base_unit=2500.0)


async def test_ac29_end_to_end_through_execute_target_sets_the_db_override(db, config):
    llm = make_llm({"category": "water", "goal": 2500, "confidence": 0.9})
    intent = await classify_target_intent("from now on I want to drink 2.5L a day", llm, DEFAULT_REGISTRY, config)
    assert intent is not None

    command = Command(kind="target", category=intent.habit_id, value_num=intent.goal_base_unit, target_action="set")
    reply = await execute_target(command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER)

    assert db.get_target(OWNER, "water") == 2500.0
    assert effective_goal(db, DEFAULT_REGISTRY.get("water"), config, OWNER) == 2500.0
    assert "2500" in reply


# ===========================================================================
# AC30 -- TH free-form "ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร" -> water target 2500.
# ===========================================================================


async def test_ac30_thai_free_form_classifies_water_2500():
    llm = make_llm({"category": "water", "goal": 2500, "confidence": 0.9})
    intent = await classify_target_intent("ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร", llm, DEFAULT_REGISTRY, Config())
    assert intent == TargetIntent(habit_id="water", goal_base_unit=2500.0)


async def test_ac30_end_to_end_sets_water_target_no_log(db, config):
    llm = make_llm({"category": "water", "goal": 2500, "confidence": 0.9})
    intent = await classify_target_intent("ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร", llm, DEFAULT_REGISTRY, config)
    command = Command(kind="target", category=intent.habit_id, value_num=intent.goal_base_unit, target_action="set")

    await execute_target(command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="th", user_id=OWNER)

    assert db.get_target(OWNER, "water") == 2500.0
    assert db._conn.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"] == 0  # no log row from this path


# ===========================================================================
# AC31 -- goal-less habit (stretch, duration, no config goal): a valid NL
# intent introduces a goal; subsequent effective_goal/day_qualifies/etc all
# see it (R-T5b, verified at the effective_goal layer -- the streak/
# summary/reminder-skip integration itself is core/targets.py's shared
# surface, already covered by tests/test_v11_shared_surface.py).
# ===========================================================================


async def test_ac31_stretch_goalless_habit_gets_a_goal_from_nl_intent(db, config):
    stretch = DEFAULT_REGISTRY.get("stretch")
    assert effective_goal(db, stretch, config, OWNER) is None  # no config goal, no override yet

    llm = make_llm({"category": "stretch", "goal": 20, "confidence": 0.9})
    intent = await classify_target_intent(
        "from now on I want to stretch 20 minutes a day", llm, DEFAULT_REGISTRY, config
    )
    assert intent == TargetIntent(habit_id="stretch", goal_base_unit=20.0)

    command = Command(kind="target", category=intent.habit_id, value_num=intent.goal_base_unit, target_action="set")
    await execute_target(command, db=db, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER)

    assert effective_goal(db, stretch, config, OWNER) == 20.0

    # Clearing reverts to "no goal" (R-T5b's own reversibility clause).
    db.clear_target(OWNER, "stretch")
    assert effective_goal(db, stretch, config, OWNER) is None


# ===========================================================================
# AC32 -- ambiguity fail-closed: a log ("I drank 2.5L", "500ml") or any
# below-threshold-confidence response -> classify_target_intent returns
# None, no habit_targets write occurs.
# ===========================================================================


async def test_ac32_a_log_classified_as_unknown_returns_none(db, config):
    llm = make_llm({"category": "unknown", "goal": None, "confidence": 0.2})
    intent = await classify_target_intent("I drank 2.5L", llm, DEFAULT_REGISTRY, config)

    assert intent is None
    assert db.all_targets(OWNER) == {}  # nothing was ever written -- caller never calls execute_target on None


async def test_ac32_low_confidence_valid_shaped_response_still_returns_none(config):
    """Even a schema-valid, business-valid-looking category/goal is
    rejected outright if confidence is below `config.ollama.
    confidence_threshold` (default 0.55) -- the model "guessing" at a
    plausible-looking target must not be enough on its own."""
    llm = make_llm({"category": "water", "goal": 2500, "confidence": 0.4})
    intent = await classify_target_intent("maybe water 2.5L a day?", llm, DEFAULT_REGISTRY, config)
    assert intent is None


async def test_ac32_confidence_threshold_is_read_from_config():
    custom_config = Config.model_validate({"ollama": {"confidence_threshold": 0.9}})
    llm = make_llm({"category": "water", "goal": 2500, "confidence": 0.8})  # would pass the 0.55 default
    intent = await classify_target_intent("water 2.5L a day", llm, DEFAULT_REGISTRY, custom_config)
    assert intent is None


async def test_ac32_500ml_bare_log_classified_unknown_returns_none():
    llm = make_llm({"category": "unknown", "goal": None, "confidence": 0.1})
    intent = await classify_target_intent("500ml", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


# ===========================================================================
# AC34 -- classifier robustness: malformed JSON, unknown/unconfigured
# category, non-goal-able habit, goal <= 0, transport error -> None, never
# raises, no write.
# ===========================================================================


async def test_ac34_malformed_json_returns_none_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not json at all {{{"}})

    llm = OllamaClient(
        "http://mac-mini:11434", "qwen3.5:9b-mlx", timeout_seconds=5.0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    intent = await classify_target_intent("whatever", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac34_unconfigured_habit_category_returns_none():
    """The schema's category enum is generated from the live registry --
    a model hallucinating a habit id that isn't actually configured is
    off-schema, so chat_json's own fallback chain already returns None
    (mirrors test_query.py's equivalent AC8.4 case)."""
    llm = make_llm({"category": "sleep", "goal": 8, "confidence": 0.9})
    intent = await classify_target_intent("sleep 8 hours a day", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac34_non_goalable_habit_category_returns_none():
    """`diary` IS a configured habit id (so it passes the schema's enum
    and chat_json's off-schema gate) but is `text`-typed -- not
    goal-able -- so `_validate_intent`'s own `is_goalable` check rejects
    it, independent of `chat_json`'s gate."""
    llm = make_llm({"category": "diary", "goal": 5, "confidence": 0.9})
    intent = await classify_target_intent("write more diary a day", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


@pytest.mark.parametrize("goal", [0, -5, -0.5])
async def test_ac34_non_positive_goal_returns_none(goal):
    llm = make_llm({"category": "water", "goal": goal, "confidence": 0.9})
    intent = await classify_target_intent("water goal", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac34_transport_error_returns_none_not_a_crash():
    llm = make_llm(None, status=503)  # every model attempt gets a reachable-but-bad-status response
    intent = await classify_target_intent("from now on 2.5L a day", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac34_missing_confidence_field_defaults_closed_not_open():
    """A response missing `confidence` entirely must fail closed (treated
    as 0.0, below any real threshold), not be treated as implicitly
    confident."""
    llm = make_llm({"category": "water", "goal": 2500})
    intent = await classify_target_intent("water 2.5L a day", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac34_null_goal_with_unknown_category_returns_none():
    llm = make_llm({"category": "unknown", "goal": None, "confidence": 0.1})
    intent = await classify_target_intent("what is the capital of France?", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


# ---------------------------------------------------------------------------
# Vera adversarial additions -- failure modes beyond Luna's own coverage.
# ---------------------------------------------------------------------------


class _RaisingLLM:
    """Unlike `make_llm(None, status=503)` (a reachable-but-bad-status HTTP
    response, already handled by `OllamaClient.chat_json`'s own fallback
    chain), this simulates the client itself raising -- a connection
    refused / DNS failure / timeout, i.e. `httpx.ConnectError`/
    `httpx.TimeoutException` never reaching the HTTP layer at all.
    `classify_target_intent`'s bare `except Exception` (R-T14) must catch
    this too, not just HTTP-shaped failures."""

    async def chat_json(self, *args, **kwargs):
        raise ConnectionError("connection refused")

    async def chat_text(self, *args, **kwargs):
        raise AssertionError("not exercised by target classification")


async def test_ac34_llm_client_raising_directly_returns_none_not_a_crash():
    intent = await classify_target_intent("from now on 2.5L a day", _RaisingLLM(), DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac34_non_numeric_goal_string_returns_none():
    """A model that answers `goal` as a non-numeric string (e.g. echoing
    "a lot more" instead of a number) must fail closed -- `float("a lot
    more")` raises `ValueError`, which `_validate_intent` must catch, not
    propagate."""
    llm = make_llm({"category": "water", "goal": "a lot more", "confidence": 0.9})
    intent = await classify_target_intent("water goal", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


async def test_ac32_a_log_with_a_number_and_unit_but_no_daily_marker_is_never_set_by_the_classifier():
    """Belt-and-suspenders on AC32: even if a plain log somehow reached
    the classifier (bypassing the cost gate, e.g. a caller bug), a
    well-behaved model still answers "unknown" for it -- and even if the
    model mis-answered a real category with low confidence, the threshold
    check still fails it closed. Simulates the mis-answered-but-low-
    confidence case for a bare "500ml"-shaped log."""
    llm = make_llm({"category": "water", "goal": 500, "confidence": 0.3})
    intent = await classify_target_intent("500ml", llm, DEFAULT_REGISTRY, Config())
    assert intent is None


def test_validate_schema_shape():
    schema = build_target_intent_schema(DEFAULT_REGISTRY.category_enum())
    assert schema["properties"]["category"]["enum"] == DEFAULT_REGISTRY.category_enum()
    assert schema["required"] == ["category", "goal", "confidence"]


# ---------------------------------------------------------------------------
# Prompt builder sanity (mirrors tests/test_query.py's own coverage).
# ---------------------------------------------------------------------------


def test_target_user_prompt_wraps_the_message():
    assert build_target_intent_user_prompt("2.5L a day") == "Message: 2.5L a day"


def test_target_system_prompt_covers_goalable_habits_and_unknown_but_not_diary():
    prompt = build_target_intent_system_prompt(DEFAULT_REGISTRY)
    assert '"water"' in prompt
    assert '"stretch"' in prompt
    assert '"unknown"' in prompt
    # diary is text-typed (not goal-able) -- must not be offered as a
    # settable target category in the prompt's own habit list.
    assert '"diary":' not in prompt


def test_target_system_prompt_distinguishes_goal_setting_from_logging():
    """R-T14's core safety instruction must actually be present in the
    generated prompt text, not just asserted in this module's docstring."""
    prompt = build_target_intent_system_prompt(DEFAULT_REGISTRY)
    assert "FUTURE DAILY GOAL" in prompt
    assert "LOGGING AN AMOUNT" in prompt or "LOG" in prompt


def test_target_system_prompt_grows_with_habit_count():
    small = build_target_intent_system_prompt(HabitRegistry([DEFAULT_REGISTRY.get("water")]))
    big = build_target_intent_system_prompt(DEFAULT_REGISTRY)
    assert len(big) > len(small)


def test_target_system_prompt_includes_a_non_default_goalable_habit():
    sleep = Habit(
        id="sleep",
        type="numeric",
        label_en="sleep",
        label_th="นอน",
        unit_en="h",
        unit_th="ชม.",
        goal=8,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )
    registry = HabitRegistry([*DEFAULT_REGISTRY, sleep])
    prompt = build_target_intent_system_prompt(registry)
    assert '"sleep"' in prompt
