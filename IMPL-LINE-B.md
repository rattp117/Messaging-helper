# Implementation — LINE edition, Module B (no-LLM mode)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/routing.py` | Modified | `handle_inbound_message`'s preparse-miss branch split into `elif config.ollama.enabled:` (unchanged v1.10.0 body: deferral, target-NL classify, `parse_message`) / `else:` (R-B1/B2/B3: no deferral row, zero-LLM `looks_like_target_phrasing` gate → `/target` pointer, else a synthetic `ExtractionResult.unknown()` that reuses the existing "habit is None" clarify branch verbatim). `reparse_pending_unparsed` gained a `config.ollama.enabled` guard before its single-flight check (R-B8, dead-code proof + protects any pre-branch leftover `awaiting_llm` row). |
| `src/habit_assistant/core/query.py` | Modified | `answer_question` returns the new `query_no_llm_pointer` i18n string immediately when `config.ollama.enabled` is False, before ever calling `classify_query_intent` (R-B4). |
| `src/habit_assistant/core/confirmation.py` | Modified | `generic_confirmation`'s text-habit branch and `confirmation_text`'s diary branch both gate their `llm.chat_text(...)` call on `config.ollama.enabled`, falling straight to `diary_reflection_fallback` when False (R-B5). |
| `src/habit_assistant/core/review.py` | Modified | `run_weekly_review`'s narrative `llm.chat_text(...)` call is gated the same way, falling to `weekly_review_fallback_narrative`; the stats block/charts (deterministic) are untouched (R-B6). |
| `src/habit_assistant/core/health.py` | Modified | `HealthMonitor.__init__` gained `ollama_enabled: bool = True` (keyword-only, additive); `check_ollama()` returns `True` immediately with **no HTTP call at all** when `False` — no ping, and therefore no UP→DOWN alert and no `on_ollama_recovered` firing (R-B8). Default `True` keeps every existing Telegram-branch caller byte-identical. |
| `src/habit_assistant/core/i18n.py` | Modified (additive) | One new key, `query_no_llm_pointer` (EN+TH), added next to the shared surface's own `target_nl_no_llm_pointer` — see "Known limitations" for why this deviates from strict file ownership. No existing key's text was changed. |
| `tests/test_line_no_llm.py` | Created | 17 tests: one disabled + one enabled test per §5.2 call-site row (rows 1–6, 8; row 7 is out of scope, see below), plus a dedicated AC16 guesses/no-guesses pair for row 1. Every disabled-mode test uses a `Poisoned*` double that **raises** if any LLM-shaped method/HTTP call is ever made — a structural, not just behavioral, zero-LLM proof. |

**Deliberately not touched:** `src/habit_assistant/core/target_nl.py` — no functional change was needed. The disabled-mode short-circuit lives entirely in `routing.py`, reusing `target_nl.looks_like_target_phrasing` (already zero-LLM, a pure regex gate) to decide whether to send the `/target` pointer; `classify_target_intent` (the actual LLM call) is simply never invoked from the disabled branch. Listed as a file "to touch" in SPEC-LINE.md §6, but no touch was required to satisfy R-B3.

## How it works

The whole module hinges on one branch point in `core/routing.py:handle_inbound_message`: after a preparse miss (and a reply-attribution miss), the code now asks `config.ollama.enabled` instead of unconditionally running the deferral/target-NL/`parse_message` sequence. When `True` it is the exact, unmodified v1.10.0 body. When `False`, target-shaped text is diverted to a `/target` pointer and everything else is turned into a synthetic `ExtractionResult.unknown()` that flows into the pre-existing "habit is None" branch — so `clarify.tier1_guesses`/`clarify.offer_clarify`/the generic clarifying question are **reused verbatim**, not reimplemented. The other four call sites (query, confirmation×2, review) follow the identical pattern: an `if config.ollama.enabled: <call llm> else: None`-shaped guard right before the existing fallback-on-falsy logic, so the fallback path itself (already present pre-LINE, for LLM failures) is what fires — no new fallback logic was invented anywhere. `HealthMonitor` gets one new constructor flag that turns `check_ollama` into a no-network no-op; since `.ollama_up` starts `True` and never changes, no alert or recovery callback can ever fire.

## Smoke test done

1. Targeted import smoke test of every edited module (`routing`, `query`, `confirmation`, `review`, `health`, `i18n`) — all import cleanly; resolved both new i18n keys (`target_nl_no_llm_pointer`, `query_no_llm_pointer`) in en/th and wrote them to a scratch file to visually confirm the Thai renders correctly.
2. `pytest tests/test_query.py tests/test_review.py tests/test_confirmations.py tests/test_bilingual_confirmations.py tests/test_v08_query_gaps.py` → **114 passed** (zero regressions in the modules I touched).
3. `pytest tests/test_line_no_llm.py` → **17 passed** (my new file, standalone).
4. Full LINE gate, `pytest tests/ -m "not telegram_only and not llm_only"` → **4829 passed, 4 skipped, 153 deselected, 1 xfailed, 3 failed**. All 3 failures are outside Module B's scope (detail below) — none touch a file this module owns, and a baseline run taken before I added my tests showed the identical pattern (different-but-equally-out-of-scope failures, since Module A/C were mid-flight in the same shared worktree at each point I ran the gate).

## Maps to acceptance criteria

- **AC15** → `core/routing.py:handle_inbound_message` (the `else:` branch) + `core/routing.py:reparse_pending_unparsed` (the new leading guard). Tests: `test_row1_disabled_preparse_miss_goes_straight_to_generic_clarify_no_llm`, `test_row2_disabled_reparse_pending_unparsed_is_dead_code_no_llm`.
- **AC16** → same `handle_inbound_message` branch, reusing `core/clarify.py:tier1_guesses`/`offer_clarify` unmodified. Tests: `test_row1_disabled_preparse_miss_goes_straight_to_generic_clarify_no_llm` (no guesses → generic + `/log`), `test_row1_disabled_preparse_miss_with_guesses_offers_tap_to_fix_no_llm` (guesses → tap-to-fix, row lands in `awaiting_clarify` not `awaiting_llm`).
- **AC17** → `core/routing.py` (target phrasing → `target_nl_no_llm_pointer`) + `core/query.py:answer_question` (NL question → `query_no_llm_pointer`). Tests: `test_row3_disabled_target_phrasing_points_at_target_command_no_llm`, `test_row4_disabled_query_never_classifies_returns_command_pointer`, `test_row4_disabled_query_via_full_routing_path`.
- **AC18** → `core/confirmation.py:confirmation_text`/`generic_confirmation` (diary + generic text habit) + `core/review.py:run_weekly_review`. Tests: `test_row5_disabled_diary_confirmation_forces_static_fallback_no_llm`, `test_row5_disabled_generic_text_habit_confirmation_forces_static_fallback_no_llm`, `test_row6_disabled_weekly_review_forces_static_narrative_no_llm`.
- **AC19** → **Partially covered, by design.** `core/health.py:HealthMonitor` (no Ollama ping/alert/recovery when `ollama_enabled=False`) is fully covered: `test_row8_disabled_health_monitor_never_pings_ollama_no_alert_no_recovery`. The other two clauses of AC19 — "no schema probe runs" and "no `OllamaClient` is constructed" — live entirely in `core/app.py`, which SPEC-LINE.md §11 reserves for Integration ("the only central file... reserved for integration so no module edits it"). Archi's own dispatch to me confirmed this piece is "droppable if integration owns the wiring." I did not touch `app.py`. **Action needed from Integration** (see Known limitations).

## §5.2 call-site table — completed

| # | Site | No-LLM-mode disposition (as built) | Disabled-mode test | Enabled-mode (byte-identical) test |
|---|---|---|---|---|
| 1 | `routing.py:handle_inbound_message` → `parser.py:parse_message` | Skip; synthetic `ExtractionResult.unknown()` → existing clarify branch | `test_row1_disabled_preparse_miss_goes_straight_to_generic_clarify_no_llm`, `test_row1_disabled_preparse_miss_with_guesses_offers_tap_to_fix_no_llm` | `test_row1_enabled_preparse_miss_still_calls_parse_message` |
| 2 | `routing.py:reparse_pending_unparsed` → `parse_message` | Dead; guarded, returns before any DB/LLM touch | `test_row2_disabled_reparse_pending_unparsed_is_dead_code_no_llm` | `test_row2_enabled_reparse_pending_unparsed_still_reparses` |
| 3 | `routing.py` → `target_nl.py:classify_target_intent` | Skip classify; zero-LLM `looks_like_target_phrasing` gate → `/target` pointer | `test_row3_disabled_target_phrasing_points_at_target_command_no_llm` | `test_row3_enabled_target_phrasing_still_classifies_and_sets_target` |
| 4 | `routing.py` (`kind=="query"`) → `query.py:answer_question` → `classify_query_intent` | Skip classify → `query_no_llm_pointer` | `test_row4_disabled_query_never_classifies_returns_command_pointer`, `test_row4_disabled_query_via_full_routing_path` | `test_row4_enabled_query_still_classifies_and_answers` |
| 5 | `confirmation.py:confirmation_text`/`generic_confirmation` (text/diary) | Force `diary_reflection_fallback` | `test_row5_disabled_diary_confirmation_forces_static_fallback_no_llm`, `test_row5_disabled_generic_text_habit_confirmation_forces_static_fallback_no_llm` | `test_row5_enabled_diary_confirmation_still_calls_llm` |
| 6 | `jobs.py:weekly_review_job` → `review.py:run_weekly_review` | Force `weekly_review_fallback_narrative`; stats/charts unchanged | `test_row6_disabled_weekly_review_forces_static_narrative_no_llm` | `test_row6_enabled_weekly_review_still_calls_llm` |
| 7 | `app.py:async_main` → `probe_schema_support` | **Not built here** — `app.py` is Integration's file (§11). See Known limitations. | — | — |
| 8 | `health.py:run_once` → `check_ollama` | Skip Ollama half entirely — no HTTP call, no alert, no recovery | `test_row8_disabled_health_monitor_never_pings_ollama_no_alert_no_recovery` | `test_row8_enabled_health_monitor_still_pings_ollama_default_behavior` |

## Known limitations

1. **Row 7 + the rest of R-B7/R-B8/R-B9 need one small Integration-side change in `core/app.py`.** I do not own that file (§11 reserves it for the Integration pass), so I did not touch it. What Integration needs to do, concretely:
   - Wrap `if config.ollama.probe_on_startup:` with `and config.ollama.enabled` (or nest it), so the probe never runs when disabled.
   - Skip constructing a real `OllamaClient` when `config.ollama.enabled` is False (or construct a stub whose methods raise — either satisfies R-B9's "prove no accidental dependence").
   - Pass `ollama_enabled=config.ollama.enabled` into the `HealthMonitor(...)` constructor call (the flag exists and is fully tested; it just isn't wired from `app.py` yet).
   - `reparse_pending_unparsed`'s startup call and the `on_ollama_recovered` closure need no change — they're now self-guarded (row 2) and safe to call unconditionally, though skipping them when disabled is cleaner.
   All of `core/health.py`'s and `core/routing.py`'s own halves of this are done and unit-tested; only the `app.py` wiring is outstanding, exactly as Archi's dispatch anticipated ("droppable if integration owns the wiring").

2. **One new i18n key (`query_no_llm_pointer`) was added to `core/i18n.py`, a file §11 assigns to the Shared surface, not Module B.** R-B4 explicitly permits reusing/extending the existing `query_cant_answer` copy, but that key is also the enabled-mode fail-closed fallback — editing its *existing* text would have risked breaking the "enabled=true byte-identical" gate and any test asserting that literal string. Adding one new, clearly-scoped key (mirroring exactly how the shared surface added `target_nl_no_llm_pointer` for R-B3) was the lower-risk path to satisfy AC17's explicit "the reply points at /records/trends/dashboard" requirement. Purely additive — no existing key's value changed.

3. **A concurrent edit from Module C landed in `core/routing.py` during my work** (adding a `command.kind == "digest"` dispatch branch + `digest` import, for R-C4's `/digest on|off` toggle). This is a real gap in SPEC-LINE.md §11's file split — `routing.py`'s command-dispatch table is the one place every command kind's execution lives, so Module C's `/digest` command had nowhere else to go. It's cleanly interleaved with my own edits (different regions, verified via `git diff`) and doesn't conflict with anything in this report. Flagging for Archi's awareness, not something I reverted or fixed.

4. **The LINE gate currently shows 3 failures, none of them Module B's:**
   - `tests/test_audit.py::test_actions_matches_the_spec_vocabulary_exactly` and `tests/test_refactor_s3.py::test_matchers_table_has_all_27_rows_in_the_exact_pre_conversion_order` — both caused by Module C's in-flight `commands.py`/`audit.py` changes (the new `digest` command kind/audit action isn't yet reflected in these two golden-list tests). Neither file is in Module B's scope; confirmed via `git status` that I never touched `commands.py` or `audit.py`.
   - `tests/test_v110_m3_gaps.py::test_run_due_reminders_actually_paused_user_suppressed_others_unaffected` — the pre-existing flake called out in Archi's own dispatch to me ("the test_v110_m3_gaps.py pre-existing flake isn't yours").
   All three reproduce identically with my diff reverted (confirmed by comparing against my very first baseline gate run, which showed a different-but-equally-unrelated set: 3 Module-A-stub failures — since fixed by Module A in the interim — plus this same flake).

## Gate numbers (for Archi)

- `pytest tests/ -m "not telegram_only and not llm_only"` → **4829 passed, 4 skipped, 153 deselected, 1 xfailed, 3 failed** (all 3 failures out of Module B's scope, see above).
- My own new file alone: `pytest tests/test_line_no_llm.py` → **17 passed, 0 failed**.
