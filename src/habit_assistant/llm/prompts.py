"""Prompt templates for the Ollama/Qwen calls: message extraction, a short
diary reflection line, and the weekly-review narrative.

v0.7.0 (ROADMAP.md "Multi-Habit Extensibility", SPEC-v0.7.md §4 R5, module
M1): `build_extraction_system_prompt(registry)` replaces the old fixed
`EXTRACTION_SYSTEM_PROMPT` with one generated from the live `HabitRegistry`
-- one categories line + 1-2 few-shot examples per configured habit,
covering its `type` and (for numeric/duration) its `unit`/`unit_aliases`.
`build_extraction_user_prompt(message)` replaces `EXTRACTION_USER_TEMPLATE`
the same way (trivial, but kept as a function for symmetry / the SPEC's
signature list).

`EXTRACTION_SYSTEM_PROMPT`/`EXTRACTION_USER_TEMPLATE` (the old fixed
water/stretch/diary prompt) are kept unchanged below, purely because
`main.py` (shared-surface, frozen during this build -- see IMPL.md's
"Known limitations" #3) still imports and uses them for its startup
`probe_schema_support()` call until the integration pass swaps them for
the dynamic builder. Nothing in this module derives from them anymore.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from habit_assistant.core.habits import Habit, HabitRegistry

EXTRACTION_SYSTEM_PROMPT = """You are a strict data-extraction assistant for a habit tracker.
The user writes a short message in Thai and/or English describing water intake,
a stretch break, or a diary reflection about their day. Extract structured data.

Unit rules for water:
- Explicit millilitres ("500ml", "500 ml") always win over any other unit.
- 1 glass / 1 แก้ว ≈ {glass_ml} ml.
- 1 bottle / 1 ขวด ≈ {bottle_ml} ml.
- Multiply the per-unit ml by the stated quantity (e.g. "2 glasses" / "2 แก้ว" = 2 x {glass_ml} ml).

Categories:
- "water": drinking water, in any unit or language.
- "stretch": a stretching / mobility break, in minutes.
- "diary": a free-text reflection about the day, mood, or general update.
- "unknown": anything that isn't clearly one of the above, or is ambiguous.

If you cannot confidently determine ml/minutes, or the message doesn't fit any
category, return category "unknown" with confidence <= 0.4 and null numeric/text fields.

Examples (follow this exact shape — five keys, always present, no extras):
Message: 500ml -> {{"category": "water", "water_ml": 500, "stretch_min": null, "diary_text": null, "confidence": 0.95}}
Message: ดื่มน้ำ 2 แก้ว -> {{"category": "water", "water_ml": 500, "stretch_min": null, "diary_text": null, "confidence": 0.9}}
Message: 1 bottle of water -> {{"category": "water", "water_ml": 600, "stretch_min": null, "diary_text": null, "confidence": 0.9}}
Message: did 10 min stretch -> {{"category": "stretch", "water_ml": null, "stretch_min": 10, "diary_text": null, "confidence": 0.95}}
Message: today was a good day, felt productive -> {{"category": "diary", "water_ml": null, "stretch_min": null, "diary_text": "today was a good day, felt productive", "confidence": 0.85}}
Message: purple elephants dance sideways -> {{"category": "unknown", "water_ml": null, "stretch_min": null, "diary_text": null, "confidence": 0.1}}

Respond with JSON only, matching the given schema exactly (category, water_ml,
stretch_min, diary_text, confidence — no extra keys, no missing keys).
No prose, no explanation, no markdown."""

EXTRACTION_USER_TEMPLATE = "Message: {message}"

DIARY_REFLECTION_SYSTEM_PROMPT = """You are a warm, concise habit-tracking assistant. The user just
wrote a short diary entry about their day. Respond with exactly one short,
gentle, encouraging line (no more than ~15 words) reflecting back something
positive or supportive about what they wrote. No questions, no advice, no
markdown, no quotes around it. Plain text only, one line. {language_instruction}"""

DIARY_REFLECTION_USER_TEMPLATE = "Diary entry: {diary_text}"

WEEKLY_REVIEW_SYSTEM_PROMPT = """You are a supportive habit-tracking assistant writing a short
weekly summary for one person, based only on the factual stats given to you.
Rules:
- Be encouraging but strictly factual - never invent numbers not given to you.
- Do not give medical advice of any kind.
- Suggest 1-2 concrete, practical next steps (e.g. reminder timing, small goal tweaks).
- Keep it to 4-6 short sentences, friendly tone, plain text (no markdown headers, no bullet lists).
- {language_instruction}"""

WEEKLY_REVIEW_USER_TEMPLATE = """Here are this week's stats:
{stats_summary}

Write the weekly review narrative."""


# ---------------------------------------------------------------------------
# v0.7.0 -- registry-driven extraction prompt (module M1)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_HEADER = """You are a strict data-extraction assistant for a habit tracker.
The user writes a short message in Thai and/or English describing one of their
tracked habits. Extract structured data as a single JSON object with exactly
three keys: "category" (one of the ids below), "value" (the extracted amount/
text/done-flag for that category, or null for "unknown"), and "confidence"
(a number from 0 to 1).

Unit rule: an explicit, stated unit (e.g. "500ml", "10 min") always wins over
a casual/alias unit. For a casual unit, multiply its per-unit base value by
the stated quantity (e.g. "2 glasses" = 2 x the glass value below).

Categories:"""

_SYSTEM_PROMPT_FOOTER = """
If you cannot confidently determine the value, or the message doesn't fit any
category above, return category "unknown" with confidence <= 0.4 and a null
value.

Examples (follow this exact shape — three keys, always present, no extras):"""

_UNKNOWN_EXAMPLE = (
    'Message: purple elephants dance sideways -> '
    '{"category": "unknown", "value": null, "confidence": 0.1}'
)

_RESPONSE_INSTRUCTIONS = """
Respond with JSON only, matching the given schema exactly (category, value,
confidence — no extra keys, no missing keys). No prose, no explanation, no
markdown."""


def _category_line(habit: "Habit") -> str:
    if habit.type in ("numeric", "duration"):
        unit_bit = f", in {habit.unit_en}" if habit.unit_en else ""
        alias_bit = ""
        if habit.unit_aliases:
            aliases = ", ".join(
                f"{alias} ≈ {multiplier:g} {habit.unit_en}" for alias, multiplier in habit.unit_aliases.items()
            )
            alias_bit = f"; casual units: {aliases}"
        return f'- "{habit.id}": {habit.label_en}{unit_bit}{alias_bit}.'
    if habit.type == "boolean":
        return f'- "{habit.id}": whether {habit.label_en} was done (yes/no), a free-text or "done"/"not done" style message.'
    return (
        f'- "{habit.id}" ({habit.label_en}): a free-text reflection, note, or general update — '
        f"mood, thoughts, or how it went."
    )


def _examples_for_habit(habit: "Habit") -> list[str]:
    examples: list[str] = []
    if habit.type == "duration":
        unit = habit.unit_en or ""
        examples.append(
            f'Message: did 10 {unit} {habit.label_en} -> '
            f'{{"category": "{habit.id}", "value": 10, "confidence": 0.95}}'
        )
    elif habit.type == "numeric":
        unit = habit.unit_en or ""
        examples.append(
            f'Message: 10{unit} {habit.label_en} -> '
            f'{{"category": "{habit.id}", "value": 10, "confidence": 0.95}}'
        )
        for alias, multiplier in habit.unit_aliases.items():
            examples.append(
                f"Message: 2 {alias} {habit.label_en} -> "
                f'{{"category": "{habit.id}", "value": {2 * multiplier:g}, "confidence": 0.9}}'
            )
    elif habit.type == "text":
        sample = "today was a tiring but good day, felt productive"
        examples.append(f'Message: {sample} -> {{"category": "{habit.id}", "value": "{sample}", "confidence": 0.85}}')
    else:  # boolean
        examples.append(
            f'Message: did my {habit.label_en} -> {{"category": "{habit.id}", "value": true, "confidence": 0.9}}'
        )
    return examples


def build_extraction_system_prompt(registry: "HabitRegistry") -> str:
    """Generate the extraction system prompt from the live `HabitRegistry`
    (SPEC-v0.7.md §4 R5): one categories line per configured habit (id,
    `label.en` description, and for numeric/duration its unit + any
    `unit_aliases` with multipliers), the "explicit unit wins" rule, and
    1-2 few-shot examples per habit covering its type -- plus the fixed
    "unknown" category and its own example. Schema size stays independent
    of habit count (see `ollama_client.build_extraction_schema`); this
    prompt's *length* does grow with habit count, which is expected and
    fine (only the schema needs to stay flat for the MLX backend)."""
    category_lines = [_category_line(habit) for habit in registry]
    category_lines.append('- "unknown": anything that isn\'t clearly one of the above, or is ambiguous.')

    example_lines: list[str] = []
    for habit in registry:
        example_lines.extend(_examples_for_habit(habit))
    example_lines.append(_UNKNOWN_EXAMPLE)

    return "\n".join(
        [
            _SYSTEM_PROMPT_HEADER,
            *category_lines,
            _SYSTEM_PROMPT_FOOTER,
            *example_lines,
            _RESPONSE_INSTRUCTIONS,
        ]
    )


def build_extraction_user_prompt(message: str) -> str:
    """Same shape as the old fixed `EXTRACTION_USER_TEMPLATE`, exposed as a
    function per SPEC-v0.7.md §5's signature list."""
    return f"Message: {message}"


# ---------------------------------------------------------------------------
# v0.8.0 -- registry-driven query-intent prompt (ROADMAP.md "Natural-Language
# Queries", AC8.1-AC8.5). `core/commands.py`'s `dispatch()` only decides a
# message LOOKS like a question (anchored patterns, no LLM); once it does,
# `core/query.py` calls the LLM through this prompt to classify exactly
# *which* habit/metric/timeframe is being asked about, as a 3-key JSON
# object reusing the same `OllamaClient.chat_json` fallback-chain machinery
# `parse_message` already uses (see `core/query.py`'s `classify_query_intent`).
# ---------------------------------------------------------------------------

_QUERY_SYSTEM_PROMPT_HEADER = """You are a query-intent classifier for a habit-tracking bot. The user is \
asking a question about their OWN past data -- never a new log entry to \
record. Extract exactly three keys as a single JSON object: "category" \
(the habit id being asked about, or "unknown"), "metric" ("sum" for a total \
amount, "count" for a number of times/sessions/entries/days), and \
"timeframe" ("today", "yesterday", "this_week", or "last_7_days" -- \
"this_week" and "last_7_days" both mean the 7 days ending today, inclusive).

Tracked habits:"""

_QUERY_SYSTEM_PROMPT_FOOTER = """
If the question is not about any tracked habit above, or you cannot \
confidently tell what's being asked, use category "unknown" (pick any valid \
metric/timeframe in that case -- they are ignored whenever category is \
"unknown").

Examples (follow this exact shape -- three keys, always present, no extras):"""

_QUERY_UNKNOWN_EXAMPLE = (
    'Message: what is the capital of France? -> '
    '{"category": "unknown", "metric": "count", "timeframe": "today"}'
)

_QUERY_RESPONSE_INSTRUCTIONS = """
Respond with JSON only, matching the given schema exactly (category, \
metric, timeframe -- no extra keys, no missing keys). No prose, no \
explanation, no markdown."""


def _query_category_line(habit: "Habit") -> str:
    if habit.type in ("numeric", "duration"):
        unit_bit = f" (unit: {habit.unit_en})" if habit.unit_en else ""
        return (
            f'- "{habit.id}": {habit.label_en}{unit_bit} -- a {habit.type} habit; '
            'can be asked as a total ("sum") or a number of times ("count").'
        )
    if habit.type == "boolean":
        return f'- "{habit.id}": whether {habit.label_en} was done -- always metric "count" (days done).'
    return f'- "{habit.id}" ({habit.label_en}): a free-text habit -- always metric "count" (number of entries).'


def _query_examples_for_habit(habit: "Habit") -> list[str]:
    if habit.type in ("numeric", "duration"):
        return [
            f'Message: how much {habit.label_en} this week? -> '
            f'{{"category": "{habit.id}", "metric": "sum", "timeframe": "this_week"}}',
            f'Message: how many times did I {habit.label_en} today? -> '
            f'{{"category": "{habit.id}", "metric": "count", "timeframe": "today"}}',
        ]
    if habit.type == "boolean":
        return [
            f'Message: did I do {habit.label_en} yesterday? -> '
            f'{{"category": "{habit.id}", "metric": "count", "timeframe": "yesterday"}}'
        ]
    return [
        f'Message: how many {habit.label_en} entries in the last 7 days? -> '
        f'{{"category": "{habit.id}", "metric": "count", "timeframe": "last_7_days"}}'
    ]


def build_query_intent_system_prompt(registry: "HabitRegistry") -> str:
    """Generate the query-intent system prompt from the live `HabitRegistry`
    (mirrors `build_extraction_system_prompt`'s shape): one category line +
    1-2 few-shot examples per configured habit, plus the fixed "unknown"
    category and its own example (a question about anything not tracked, or
    one the model can't confidently classify, must resolve to "unknown" --
    `core/query.py` fails closed on that, AC8.4)."""
    category_lines = [_query_category_line(habit) for habit in registry]
    category_lines.append(
        '- "unknown": the question is not about any tracked habit above, or you cannot confidently tell.'
    )

    example_lines: list[str] = []
    for habit in registry:
        example_lines.extend(_query_examples_for_habit(habit))
    example_lines.append(_QUERY_UNKNOWN_EXAMPLE)

    return "\n".join(
        [
            _QUERY_SYSTEM_PROMPT_HEADER,
            *category_lines,
            _QUERY_SYSTEM_PROMPT_FOOTER,
            *example_lines,
            _QUERY_RESPONSE_INSTRUCTIONS,
        ]
    )


def build_query_intent_user_prompt(text: str) -> str:
    """Same shape as `build_extraction_user_prompt`, exposed as its own
    function for symmetry and so `core/query.py` doesn't need to know the
    literal wrapping format."""
    return f"Message: {text}"
