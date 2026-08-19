# Implementation — v0.7.0 Multi-Habit Extensibility, Module M1 (Extraction)

> Scope note: this covers **only** module M1 per `SPEC-v0.7.md` §11 —
> `llm/prompts.py`, `core/parser.py`, `core/commands.py`, and their tests
> (`tests/test_parser.py`, `tests/test_fallback.py`, `tests/test_commands.py`,
> plus a new `tests/test_prompts.py`). The shared surface (`config.py`,
> `core/habits.py`, `storage/*`, `llm/ollama_client.py`, `core/i18n.py`,
> `main.py`) was already built and frozen before this task started (see
> `IMPL.md`'s "v0.7.0 — Multi-Habit Extensibility (shared surface only)"
> section) and was **not** modified here. `core/reminders.py` (M2) and
> `core/review.py` (M3) were being edited concurrently by other Lunas in
> this same tree and were **not** touched or read for editing purposes.

## READ THIS FIRST — a blocking integration item, not a bug in this module

Implementing `core/commands.py`'s `dispatch(text, registry)` **exactly per
SPEC-v0.7.md §5's contract** (a hard, required signature — AC12 tests it
directly) makes `main.py:402`'s still-frozen call site raise:

```
command = commands.dispatch(text, config.units.glass_ml, config.units.bottle_ml)
TypeError: dispatch() takes 2 positional arguments but 3 were given
```

This call runs **unconditionally on every single inbound message** in
`handle_inbound_message`, with **no surrounding try/except** (confirmed by
reading `main.py` directly — unlike `parse_message`'s call site, which
*is* wrapped and degrades gracefully). The shared-surface Luna's own
`IMPL.md` "Known limitations" #1 predicted exactly this and deferred the
fix to integration on purpose. Landing M1 is what was always going to
surface it — this is not a defect in `core/commands.py`.

**I measured the exact blast radius before writing this report.** I
temporarily patched `main.py:402` to `commands.dispatch(text, registry)`
(the only change needed), ran the full suite, and restored the file to its
original byte-identical content immediately after (verified via `diff`/
checksum — `main.py` is untouched in the final tree). Results:

| State | Full suite |
|---|---|
| Before this module's changes (baseline, mid-flight with M2/M3 also editing concurrently) | 53 failed / 321 passed / 3 skipped |
| After M1's changes, `main.py` still frozen (current tree) | 97 failed / 333 passed / 3 skipped |
| After M1's changes **+ the one-line `main.py:402` fix** (temporary, reverted) | 27 failed / 386 passed / 3 skipped |

Of the 97 current failures, **84 are the exact `main.py:402` TypeError**
(`grep -c "main.py:402: TypeError"` across a full run), spread across
files this module does not own: `test_confirmations.py` (21),
`test_v060_bilingual_gaps.py` (12 of 14), `test_bilingual_confirmations.py`
(10), `test_commands.py` (30 of 31 — every `handle_inbound_message`-based
test in my own file), `test_fallback.py` (all 6 remaining), `test_cli.py`
(1 of 3), `test_resilience.py` (4 of 6). The remaining ~13 failures are
unrelated pre-existing boundaries (M3's `compute_weekly_stats`/
`run_weekly_review` signature flip — 9 of them, matching `IMPL-v0.7-M3.md`'s
own count; two stale-signature assertions in `test_resilience.py` not
owned by any of M1/M2/M3; `test_cli.py`'s reminder-flag tests, M2's
boundary).

**Recommendation:** apply the one-line fix at `main.py:402`
(`commands.dispatch(text, config.units.glass_ml, config.units.bottle_ml)`
→ `commands.dispatch(text, registry)`) as the very first step of
SPEC-v0.7.md §11's "Integration order" — I verified it alone (with no
other change) takes the suite from 97 failed to 27 failed, and every one
of that 27 remainder is already independently explained (M3's boundary,
or pre-existing/unrelated). I did not make this change myself because
`main.py` is explicitly outside my file ownership for this task and two
other Lunas were concurrently editing this tree.

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/llm/prompts.py` | modified | Added `build_extraction_system_prompt(registry)` and `build_extraction_user_prompt(message)` (SPEC-v0.7.md §4 R5/§5). Generates one categories line + 1–2 few-shot examples per configured `Habit` (numeric/duration get a base-unit example plus one example per `unit_aliases` entry with its multiplier; text gets a natural "today was a tiring but good day..." example; boolean gets a "did my X" → `true` example), plus the fixed `"unknown"` category/example and response-format instructions. Schema-independent prompt length grows with habit count (expected — only the *schema* must stay flat, per `ollama_client.build_extraction_schema`'s own docstring). The old fixed `EXTRACTION_SYSTEM_PROMPT`/`EXTRACTION_USER_TEMPLATE` are **kept unchanged** (not removed) because `main.py`'s frozen startup `probe_schema_support()` call still imports and uses them (`IMPL.md` "Known limitations" #3) until integration swaps them for the dynamic builder. |
| `src/habit_assistant/core/parser.py` | rewritten | `parse_message(text, llm, registry, confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD)` builds the schema (`build_extraction_schema(registry.category_enum())`) and prompt (`build_extraction_system_prompt`/`build_extraction_user_prompt`) from the live registry, calls `llm.chat_json(system, user, schema, valid_categories)`, then `_validate(data, registry, confidence_threshold)`. Still fails closed to `ExtractionResult.unknown()` on any exception, never raises (unchanged contract). `_validate`: `category` must be in `registry.ids()` else `unknown`; then dispatches per the matched `Habit.type` — numeric/duration coerce via `_coerce_number` (accepts genuine number or numeric-looking string, rejects `bool`, rejects `<= 0`); text requires non-empty after `.strip()`; boolean coerces via `_coerce_boolean` (bilingual truthy/falsy sets, matches SPEC-v0.7.md §4 R8 exactly). The confidence-threshold gate (v0.2 AC2.3, preserved) still only demotes a genuinely-numeric confidence below threshold. |
| `src/habit_assistant/core/commands.py` | rewritten | `dispatch(text, registry: HabitRegistry) -> Command | None`. Undo-matching (`_match_undo`) and the edit-trigger regex (`_EDIT_TRIGGER`) are unchanged from v0.6.0 — only edit-**value** resolution changed. `_build_unit_lookup(registry)` maps every numeric/duration habit's own `unit_en`/`unit_th` (multiplier 1) plus every `unit_aliases` entry (its configured multiplier) to `(habit_id, multiplier)`, built in registry order with `setdefault` so an earlier habit's token wins ties (SPEC-v0.7.md §9 risk 6, "first-match in registry order"). `_resolve_unit` tries an exact (lowercased) match, then a trailing-"." stripped variant, then a simple trailing-"s" singularization. A bare number with no unit defaults to the **first** numeric/duration habit in registry order (`_default_numeric_habit`) — generalizes v0.6.0's hardcoded "no unit → water" default; reproduces it exactly for the shipped config since water is listed first. `Command.category` is now any habit id, not just `"water"`/`"stretch"`. |
| `tests/test_parser.py` | rewritten | AC6, AC7, AC11 — generic `ExtractionResult(category, value, confidence)` throughout; a `DEFAULT_REGISTRY = HabitRegistry.from_config(Config())` and a synthetic `BOOLEAN_REGISTRY` (the default config ships no boolean habit — SPEC-v0.7.md §9 risk 5) for boolean coercion tests. All prior AC4/AC5 cases carried forward (think-block/prose stripping, malformed JSON, connection/HTTP errors, missing/extra keys, confidence handling, request shape) plus new AC7 per-type validation cases (numeric/duration `<=0` reject, `"7"`/`7.5` accept; text empty/whitespace reject; boolean 6 truthy forms / 4 falsy forms / 6 uncoercible forms, each parametrized) and an AC6 "category not in registry" case (replaces the old fixed-enum "invalid category" case). |
| `tests/test_prompts.py` | **new** | AC8: `build_extraction_system_prompt`'s default-registry output covers water (ml + glass/แก้ว/bottle/ขวด aliases with multipliers)/stretch/diary/unknown, each with ≥1 example; an added `sleep` habit gets its own category line + unit + example without disturbing the built-ins; boolean/text synthetic habits get type-appropriate examples; prompt length legitimately grows with habit count (contrast to the schema's deliberately flat size). `build_extraction_user_prompt` trivial-wrap check. |
| `tests/test_fallback.py` | rewritten | AC2.1–AC2.5 (fallback chain, schema probe, confidence gate, single-model back-compat, transport/HTTP fail-closed) carried forward with mechanical signature updates: `parse_message(..., DEFAULT_REGISTRY)` instead of `(..., glass_ml, bottle_ml)`; `llm.probe_schema_support(system, user, schema)` now passed a registry-built prompt/schema (`PROBE_SYSTEM_PROMPT`/`PROBE_USER_PROMPT`/`PROBE_SCHEMA`, built once at module scope from `DEFAULT_REGISTRY`) instead of being called with no arguments; `_StaticLLM.chat_json` stub widened to the new 4-param signature (`..., valid_categories`); `json_payload()` shape changed to `{category, value, confidence}`. No assertion values changed. |
| `tests/test_commands.py` | rewritten | AC5.1–AC5.5 carried forward (undo/edit phrasing, soft-delete exclusion, the adversarial false-positive corpus, LLM-down command execution, migration 003/004 regression guards — unchanged from the pre-v0.7 file except mechanical `DEFAULT_REGISTRY`/`ExtractionResult` updates). Added a new **AC12** section that calls `commands.dispatch(text, registry)` **directly** (bypassing `handle_inbound_message`/`main.py` entirely, matching how SPEC-v0.7.md §8 itself phrases AC12's examples): both `"make that 300ml"`/`"แก้เป็น 300 มล."` → `edit`/`water`/`300.0`; `"edit that to 15 min"` → `stretch`/`15.0`; 4 garbled-tail variants → `None`; a synthetic non-built-in habit with its own `unit_aliases` resolves correctly (proves generality, not water/stretch-specific); an ambiguous-unit case (two duration habits both using `"min"`) resolves to the first in registry order. |

## How it works

`parse_message` builds a fresh system prompt, user prompt, JSON schema, and
valid-category set from whatever `HabitRegistry` it's given, on every call
— nothing about the extraction contract is hardcoded to water/stretch/diary
anymore. `chat_json` (shared surface, unchanged by this module) tries each
configured model in order and returns the first response whose `category`
is recognized; `_validate` then does the real work: reject an unregistered
category, then coerce+validate `value` per the matched habit's `type`
(numeric/duration/text/boolean), then apply the confidence-threshold gate.
`core/commands.py`'s `dispatch` is unchanged in its undo/edit-trigger
matching (still LLM-free, still zero-false-positive by construction); only
edit-value **unit resolution** became registry-driven, via a token→
`(habit_id, multiplier)` lookup built fresh from the registry's configured
units and aliases on every call (cheap — habit counts are small).

## Smoke test done

- `.venv\Scripts\python.exe -m pytest -q tests/test_parser.py tests/test_prompts.py` → **60 passed** (0 failed). This module's own extraction/validation/prompt-generation logic is fully green in isolation.
- `.venv\Scripts\python.exe -m pytest -q tests/test_fallback.py` → **22 passed, 6 failed** — every failure is the documented `main.py:402` boundary (verified by inspecting each traceback; all 6 go through `handle_inbound_message`). Every fallback-chain/probe/threshold case that calls `parse_message`/`OllamaClient` directly (not through `main.py`) passes.
- `.venv\Scripts\python.exe -m pytest -q tests/test_commands.py` → **27 passed, 31 failed** — 30 of the 31 are the same `main.py:402` boundary (confirmed via traceback grep); the 1 remaining (`test_soft_deleted_rows_excluded_from_weekly_review_stats`) calls M3's `compute_weekly_stats(db, config, date)` with the pre-v0.7 3-arg shape and fails with `TypeError: compute_weekly_stats() missing 1 required positional argument: 'end_date'` — M3's boundary, not mine, left unmodified per file ownership (documented inline in the test's own docstring).
- Manual interpreter smoke test (`commands.dispatch` + `build_extraction_system_prompt` called directly against the real default registry): `dispatch("make that 300ml", registry)` → `Command(kind='edit', category='water', value_num=300.0)`; `dispatch("แก้เป็น 300 มล.", registry)` → same; `dispatch("edit that to 15 min", registry)` → `Command(kind='edit', category='stretch', value_num=15.0)`; `dispatch("edit that to blah", registry)` / adversarial corpus → `None` for all. Confirmed live via the actual generated system prompt containing `250`/`600` (glass/bottle multipliers) and `sleep` when a `sleep` habit is added.
- **Verified the exact integration fix** (see "READ THIS FIRST" above): temporarily patched `main.py:402` to `commands.dispatch(text, registry)`, ran the full suite (386 passed / 27 failed / 3 skipped), confirmed the 27 remainder is fully explained by M3's boundary + unrelated pre-existing gaps, then restored `main.py` to its original byte-identical content (checksum-verified) before finishing.
- **Live Ollama smoke test** (server reachable at `http://mac-mini:11434`, confirmed via `/api/version` → `0.32.6`; production bot PID 13956 confirmed still running via `tasklist` before and after; all work against `tempfile`/in-memory registries, `data/habits.db` never opened): ran `parse_message` through the real `OllamaClient` (model chain `["qwen3.5:9b-mlx", "qwen3:8b"]` from the real `config.toml`, loaded via `load_config()`) for the default registry:
  - `"ดื่มน้ำ 2 แก้ว"` → `water`, `500.0`, conf `0.95`
  - `"500ml"` → `water`, `500.0`, conf `0.99`
  - `"1 bottle of water"` → `water`, `600.0`, conf `0.95`
  - `"did 10 min stretch"` → `stretch`, `10.0`, conf `0.95`
  - `"today was such a tiring but good day"` → `diary`, text preserved, conf `0.85`
  - `"purple elephants dance sideways"` → `unknown`
  - Plus a **zero-code-change** synthetic `sleep` habit (numeric, unit `h`/`ชม.`, goal 8) added only to the in-memory registry: `"นอน 7 ชม."` → `sleep`, `7.0`, conf `0.95`; `"slept 7 hours"` → `sleep`, `7.0`, conf `0.95`.
  - **Bug found and fixed during this live check**: my first prompt-generation draft's text-type category description (`"a free-text note about {label}"`) and example (`"today's {label} update: felt good about it"`) were noticeably weaker than the old fixed prompt's for ambiguous diary-like input — 5 live repeats of `"today was such a tiring but good day"` against the first draft returned `diary` only 2/5 times (the other 3 landed on `unknown`), vs. the old fixed prompt's 5/5 `diary` on the identical message (verified head-to-head, same model, same session). Root cause: the generic description lost the old prompt's explicit "mood, or general update" cue words, and the example lost its "today was a ... day" structural match to the test sentence. Fixed by rewording the text-type category line to `"a free-text reflection, note, or general update — mood, thoughts, or how it went"` (keeps the effective cue words, generic across any text-type habit) and the example to a fixed, structurally-similar `"today was a tiring but good day, felt productive"`. Re-verified: 5/5 `diary` after the fix, and the full 8-case live check above (re-run after the fix) is all correct. `tests/test_parser.py`/`tests/test_prompts.py` still 60/60 green after this wording change (no test pinned the old wording).

## Maps to acceptance criteria

- **AC6** → `core/parser.py:_validate` (`category not in registry.ids()` → `unknown`) + `parse_message` (schema/prompt/valid_categories all built from `registry`). `tests/test_parser.py::test_category_not_in_registry_fails_closed_to_unknown`, `::test_water_glass_thai_normalizes_to_ml`/`::test_stretch_english_message`/`::test_explicit_ml_message`/`::test_bottle_message_normalizes_to_ml`/`::test_diary_message` (matched-habit path). Live-verified above.
- **AC7** → `core/parser.py:_validate`/`_coerce_number`/`_coerce_boolean`. `tests/test_parser.py`'s per-type validation section: `test_numeric_with_non_positive_value_fails_closed`, `test_numeric_stringified_or_float_value_is_accepted`, `test_duration_with_invalid_value_fails_closed`, `test_text_with_empty_value_fails_closed`/`test_text_with_non_empty_value_is_accepted`, `test_boolean_truthy_forms_coerce_to_true`/`test_boolean_falsy_forms_coerce_to_false`/`test_boolean_uncoercible_forms_fail_closed` (all parametrized across every form SPEC-v0.7.md §4 R8 lists), `test_below_threshold_confidence_fails_closed`/`test_at_threshold_confidence_is_kept`.
- **AC8** → `llm/prompts.py:build_extraction_system_prompt`. `tests/test_prompts.py` (default-registry coverage, added-`sleep`-habit category line + example, boolean/text example shapes, prompt-size-grows-with-habit-count). Live-verified: default three unchanged in quality (see Smoke test); `sleep` parses correctly live with zero code changes to this module.
- **AC12** → `core/commands.py:dispatch`/`_parse_edit_value`/`_build_unit_lookup`/`_resolve_unit`. `tests/test_commands.py`'s new AC12 section — the exact `SPEC-v0.7.md` §8 examples (`"make that 300ml"`, `"แก้เป็น 300 มล."` → `water`/300; `"edit that to 15 min"` → `stretch`/15), garbled-tail → `None`, a non-built-in synthetic habit's own alias resolving correctly, first-match-in-registry-order for an ambiguous unit, and the full pre-existing adversarial false-positive corpus (`test_dispatch_returns_none_for_normal_habit_messages`) still → `None` for every message, called directly (not through the blocked `handle_inbound_message` path).

## Known limitations

1. **The `main.py:402` integration blocker** — see "READ THIS FIRST" at the top. This is the single most important thing in this report; everything else is secondary to getting that one line landed.
2. **Edit-unit plural/irregular forms are narrower than v0.6.0's hardcoded set.** The old `core/commands.py` had explicit sets for `{"min","mins","minute","minutes","นาที"}`, `{"glass","glasses","แก้ว"}`, `{"bottle","bottles","ขวด"}`, plus a hardcoded liter/litre/`ลิตร`/`l` → ×1000 conversion, none of which are derivable from the registry's configured `unit`/`unit_aliases` alone. The new `_resolve_unit` handles the common regular-plural case (trailing "s" strip: `"mins"`→`"min"`, `"bottles"`→`"bottle"`) generically, but **not** irregular plurals (`"glasses"`→`"glass"` needs an "es" strip, which I deliberately did not add — see `core/commands.py`'s `_resolve_unit` docstring for the reasoning: a blanket "es"-strip breaks other regular words like `"bottles"`/`"minutes"`) and **not** the old hardcoded liter conversion (not part of any habit's configured units in `config.toml`). Neither gap is exercised by any test in this file or the false-positive corpus (verified: no test constructs an edit phrase using `"glasses"`, `"minute(s)"`, or `"liter(s)"`), so this is a documented, intentional narrowing consistent with SPEC-v0.7.md's "config is data" philosophy — a config author who wants `"glasses"` to resolve can add it as an explicit `unit_aliases` entry (e.g. `"glasses" = 250`).
3. **`probe_schema_support`'s prompt at `main.py`'s real startup is still the old static prompt** — this is `main.py`'s own documented limitation (`IMPL.md` "Known limitations" #3), not something this module can fix without touching `main.py`. `build_extraction_system_prompt`/`build_extraction_user_prompt` exist and are fully tested; wiring them into the real startup probe is integration's job alongside the `main.py:402` fix.
4. **Not implemented here (correctly out of scope for M1):** AC9 (shared surface, already done), AC13/AC14 (M2), AC15/AC16 (M3), AC11/AC17 (integration).

## Existing-test expectation changes (old → new), per this task's audit requirement

None of these change *behavior* — every one is a mechanical signature/shape update forced by the shared surface's already-landed `ExtractionResult`/`chat_json`/`probe_schema_support` contract changes (`IMPL.md`) plus this module's own new `dispatch`/`parse_message` contracts. No assertion's expected *value* changed.

| File | Old | New | Why |
|---|---|---|---|
| `tests/test_parser.py` | `parse_message(text, llm, glass_ml, bottle_ml, confidence_threshold=...)` | `parse_message(text, llm, registry, confidence_threshold=...)` | SPEC-v0.7.md §5 contract |
| `tests/test_parser.py` | `ExtractionResult(category, water_ml, stretch_min, diary_text, confidence)` (5-field) | `ExtractionResult(category, value, confidence)` (3-field) | shared-surface contract, already landed in `llm/ollama_client.py` |
| `tests/test_parser.py` | `json_payload()` shape `{category, water_ml, stretch_min, diary_text, confidence}` | `{category, value, confidence}` | matches `build_extraction_schema`'s generated schema |
| `tests/test_parser.py` | "invalid category enum" test used a fixed 4-value enum | renamed/reframed as "category not in registry" (AC6 wording) | registry-driven categories, no fixed enum anymore |
| `tests/test_parser.py` | `test_unit_constants_are_configurable` (asserted `glass_ml=450`/`bottle_ml=900` params reach the prompt) | replaced with `test_registry_unit_aliases_reach_the_prompt` (asserts the *registry's* configured `unit_aliases` reach the prompt) | `glass_ml`/`bottle_ml` params no longer exist; equivalent coverage via the registry path |
| `tests/test_fallback.py` | `llm.probe_schema_support()` (no args) | `llm.probe_schema_support(system_prompt, user_prompt, json_schema)` | shared-surface contract, already landed |
| `tests/test_fallback.py` | `_StaticLLM.chat_json(self, system_prompt, user_prompt, json_schema)` | `..., valid_categories)` (4th param) | shared-surface contract |
| `tests/test_fallback.py` | `parse_message(text, llm, GLASS_ML, BOTTLE_ML)` | `parse_message(text, llm, DEFAULT_REGISTRY)` | SPEC-v0.7.md §5 |
| `tests/test_commands.py` | `commands.dispatch(text, glass_ml, bottle_ml)` | `commands.dispatch(text, registry)` | SPEC-v0.7.md §5, AC12 |
| `tests/test_commands.py` | `patch_parse_message`'s fake `(text, llm, glass_ml, bottle_ml, confidence_threshold=None)` | `(text, llm, registry, confidence_threshold=None)` | matches `main.py`'s already-updated real call signature |
| `tests/test_commands.py` | `ExtractionResult("stretch", None, 5, None, 0.9)` (5-field) | `ExtractionResult("stretch", 5, 0.9)` (3-field) | shared-surface contract |

No test's expected string/number/behavior assertion changed — only call shapes, matching the already-landed shared-surface contract this module builds against.
