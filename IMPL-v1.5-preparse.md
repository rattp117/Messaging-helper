# Implementation — v1.5.0 module `preparse` (deterministic pre-parser)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/preparse.py` | Created | `deterministic_parse(text, registry) -> ExtractionResult \| None` — the zero-LLM fast path for unambiguous whole-message `NUMBER UNIT` logs (R-L1). Reuses `core/units.py`'s `VALUE_RE`/`build_unit_lookup`/`resolve_unit` verbatim (R-L5); introduces no new unit-matching logic. |
| `tests/test_preparse.py` | Created | 166 tests: supported-shape + byte-identical-confirmation proof (AC-14), systematic unit-registry coverage, a 72-message zero-false-positive adversarial corpus run against two registries (AC-15), boundary values, and a structural zero-LLM proof. |

Neither file touches `main.py`, `core/commands.py`, `core/i18n.py`, or any `checkins`/`announce` file, per this module's explicit scope (SPEC-v1.5.md §11).

## How it works

`deterministic_parse` strips the input, matches it against `core/units.VALUE_RE` (`^NUMBER\s*UNIT?\s*$`, whole-message-anchored), and requires **both** a positive number **and** a unit token that `core/units.resolve_unit(core/units.build_unit_lookup(registry), unit.lower())` resolves to a `(habit_id, multiplier)` pair — `build_unit_lookup` already restricts itself to `numeric`/`duration` habits, so `text`/`boolean` habits (e.g. `diary`) can never be matched. A bare number with no unit at all deliberately returns `None` (unlike `commands._parse_edit_value`, which defaults an unadorned number to the first numeric habit) — SPEC-v1.5.md §9's own recorded decision to forgo that case for safety. On a match, it returns `ExtractionResult(habit_id, number * multiplier, 1.0)`; every failure path (regex miss, `num <= 0`, no unit, unresolved unit) returns `None`, meaning the caller should fall through to the existing pipeline unchanged.

## Smoke test done

- Direct script (`uv run python -c "..."`) against `HabitRegistry.from_config(Config())`: confirmed `"500ml"` → `water/500.0`, `"2 แก้ว"` → `water/500.0` (glass alias), `"10 min"` → `stretch/10.0`, `"2 bottle"`/`"2 ขวด"` → `water/1200.0`, `"2.5 ml"` → `water/2.5`, `"999999999 ml"` → `water/999999999.0`; and `None` for `"0 ml"`, `"-5 ml"`, `"500"` (bare number), `"did 10 min stretch"`, `"from now on 2.5L a day"`, `"น้ำ 500"`, `"how much water this week?"`, `"/undo"`, `"1,500 ml"`, `"1,500ml"`.
- `uv run pytest -q tests/test_preparse.py` → **166 passed** (run standalone twice, both clean, including after the final full-suite pass below).
- `uv run pytest -q` (full suite, current tree) → **1934 passed, 1 failed, 1 skipped**. The 1 failure is `tests/test_announce_gaps.py::test_concurrent_overlapping_calls_send_at_most_once_per_user` — the `announce` module's own concurrency test (not present in my earlier runs; it landed mid-session from that parallel track). It does not reference `preparse` at all (confirmed by grep) and is unrelated to this module's files (`core/preparse.py`, `tests/test_preparse.py`) — out of this module's scope per SPEC-v1.5.md §11 (owned by `announce`, not `preparse`).

Note on the baseline count: the dispatch cited a **1643 passed / 0 failed / 1 skipped** baseline (the shared-surface hand-off point). While this module's tests were being written, `tests/test_checkins.py`, `tests/test_dnd.py`, `tests/test_announce.py`, and later `tests/test_announce_gaps.py` all appeared in the tree — the other two parallel tracks (`checkins`, `announce`) landing concurrently, as SPEC-v1.5.md §11's PARALLEL mode intends — so the full-suite total and pass/fail mix moved independently of this module's own work. What this hand-off is accountable for: this module's own 166 tests are **166/166 green**, standalone and inside every combined run observed, and this module introduces zero regressions elsewhere (only 2 new files, nothing else touched — confirmed via `git status`).

## Maps to acceptance criteria

- **AC-14** (pre-parser skips LLM, byte-identical confirmation) → `core/preparse.py:deterministic_parse`.
  - Correctness: `tests/test_preparse.py::test_ac14_deterministic_parse_produces_the_expected_result` (8 supported shapes, EN+TH, both habit types) + `test_ac14_every_registered_unit_and_alias_resolves_via_deterministic_parse` (every unit/alias `build_unit_lookup` derives from the shipped default registry, not just the 3 spec-named examples).
  - Byte-identical confirmation: `test_ac14_confirmation_is_byte_identical_to_the_llm_path` — for each supported shape, drives the text through the REAL, unmodified `handle_inbound_message` confirmation pipeline (`main.py`, untouched by this module) on two fresh scratch on-disk SQLite DBs: once with `parse_message` monkeypatched to return a genuine-LLM-style `ExtractionResult` (same category/value, but a realistic non-1.0 confidence, e.g. 0.81), once with it returning `deterministic_parse`'s own result (confidence 1.0). Asserts the two `channel.sent` outputs are identical — proving confirmation shape depends only on `(category, value)`, never `confidence`, so the pre-parser is a safe substitute wherever it fires.
  - Zero-LLM: `test_ac14_deterministic_parse_never_touches_an_llm_even_with_a_raising_double_patched_in` (patches `OllamaClient.chat_json`/`chat_text` to raise, then runs every supported shape and the full adversarial corpus through `deterministic_parse` directly — never trips) + `test_ac14_deterministic_parse_signature_has_no_llm_channel_or_db_parameter` (structural: `inspect.signature` shows exactly `(text, registry)`).
- **AC-15** (zero false positive / adversarial) → same function.
  - `tests/test_preparse.py`'s 72-message `ADVERSARIAL_MESSAGES` corpus (bare numbers, unknown/unregistered units, boundary failures [0/negative/comma-separated/empty], multi-clause sentences wrapping a real NUMBER+UNIT, label-first phrasing ("น้ำ 500"), questions [reused from `test_commands.py`'s v0.8.0 `QUERY_MESSAGES`], full-NL target phrasings ["from now on 2.5L a day", "ตั้งเป้า น้ำ 2000"], slash commands, and the historical v1.2/v1.3 Thai-alias near-miss substrings from `test_commands.py`'s own AC5.5 corpus and `test_schedules.py`'s `เตือน` audit-fix commentary) — run twice, once against the default 3-habit registry and once against a purpose-built 4-habit-kind registry (`numeric`/`duration`/`text`/`boolean`), both `test_ac15_adversarial_corpus_never_produces_a_false_positive*` — all return `None`.
  - `test_ac15_boundary_huge_and_decimal_values_are_supported_not_false_positives` — the flip side: huge numbers and decimals ARE legitimate and must still resolve (only zero/negative/comma-separated fail).

**AC-16** (works Ollama-down) is explicitly `main.py`'s wiring responsibility, verified at the integration pass per SPEC-v1.5.md §11 — not owned by this module and not re-tested here beyond the structural zero-LLM proof above (which is the property AC-16 depends on).

## Known limitations

- `deterministic_parse` never defaults a bare number to a habit (unlike `commands._parse_edit_value`) — this is the spec's own deliberate scope limit (§9: "a bare-number win is deliberately forgone for safety"), not an oversight.
- Label-first phrasing ("น้ำ 500"), multi-habit messages, and full free-form NL target-setting are out of scope by design (§10) and remain on the LLM path.
- This module does not call, import from, or otherwise couple to `main.py`. Integration (the wiring below) is a separate, later step per SPEC-v1.5.md §11.

## Wiring instructions for integration (`main.py`, NOT done by this module)

Per **R-L2**, `deterministic_parse` must run in `handle_inbound_message` (`src/habit_assistant/main.py`) **after** `commands.dispatch` returns `None` and **before** both the health-monitor deferral check and the LLM path. Concretely, insert it immediately before the existing line:

```python
    if not dry_run and health_monitor is not None and not health_monitor.ollama_up:   # currently line 771
```

i.e. right after the `if command is not None: ... return` block ends (every branch inside it already returns, so control only reaches this point when `command is None`), and the new step should short-circuit past **both** the deferral check (line 771) **and** the target-NL gate (line 792) **and** the `parse_message` call (line 818) — reusing every line from `result = await parse_message(...)` onward (the shared confirmation-building code) unchanged. Suggested shape:

```python
    preparsed = preparse.deterministic_parse(text, registry)
    if preparsed is not None:
        result = preparsed
    else:
        if not dry_run and health_monitor is not None and not health_monitor.ollama_up:
            ...  # existing deferral block, unchanged
            return

        if health_monitor is None or health_monitor.ollama_up:
            ...  # existing target_nl gate, unchanged

        result = await parse_message(text, llm, registry, config.ollama.confidence_threshold)

    if dry_run:
        print(asdict(result))
        return
    ...  # everything from `now = clock()` onward is untouched — this is what
         # guarantees AC-14's byte-identical confirmation: both paths
         # converge on the exact same `result` variable feeding the exact
         # same downstream code.
```

This placement means: (1) a recognized command still short-circuits first (unchanged); (2) a `preparse` hit skips the deferral check entirely, so a number+unit log logs successfully even while Ollama is down (AC-16) and without spending a health-monitor-gated LLM call; (3) a `preparse` miss (`None`) falls through byte-for-byte to today's existing deferral → target-NL-gate → `parse_message` sequence, unchanged (AC-15's "falls through unchanged" half). Add `from habit_assistant.core import preparse` to `main.py`'s existing `from habit_assistant.core import (...)` import block alongside its siblings (`commands`, `target_nl`, etc.).

## Iteration log

No Vera round yet — first hand-off for this module.
