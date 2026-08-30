# Test Report — LINE Module B (no-LLM mode)

## Summary

- Scope: SPEC-LINE.md §5.2 (No-LLM call-site table, rows 1-8) + §8 AC15-AC19, against IMPL-LINE-B.md.
- New test file: `tests/test_line_b_gaps.py` — **14 tests, 14 passed**, adversarial gap-fill for what Luna's own `tests/test_line_no_llm.py` (17 tests, 17 passed) didn't structurally prove.
- Regression subset (query/review/confirmation/i18n/resilience): **159 passed, 0 failed**.
- Full LINE gate `pytest tests/ -m "not telegram_only and not llm_only"`: **4896 passed, 4 skipped, 153 deselected, 1 xfailed, 2 failed**. Both failures are **outside Module B's scope** (detail in "Parallel-tree noise" below) — zero Module-B-owned failures.
- **Status: PASS** (Module B). No production-code edits made (test-only, per mandate).

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_line_no_llm.py` (Luna's, pre-existing) | 17 | AC15-AC19, §5.2 rows 1,2,3,4,5,6,8 (disabled + enabled pair each; row 1 also gets a dedicated guesses/no-guesses AC16 pair) |
| `tests/test_line_b_gaps.py` (this pass, new) | 14 | Adversarial gaps: legacy `awaiting_llm` row disposition, backfill×disabled interaction, reply-to-reminder independence from `ollama.enabled`, health-monitor multi-cycle stability, a standalone structural "zero forbidden rows" invariant, 4 byte-level enabled=true round-trip spot checks, disabled-by-default config-loader proof, `/checkin` non-interaction, and the Module C `/digest` interleave audit (2 tests) |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC15 (R-B1: no deferral row, ever) | `test_row1_disabled_preparse_miss_goes_straight_to_generic_clarify_no_llm`, `test_zero_awaiting_llm_rows_after_n_unparseable_messages_disabled` | PASS |
| AC16 (R-B2: guesses → tap-to-fix `awaiting_clarify`; no guesses → generic + `/log`) | `test_row1_disabled_preparse_miss_with_guesses_offers_tap_to_fix_no_llm`, `test_row1_disabled_preparse_miss_goes_straight_to_generic_clarify_no_llm`, `test_zero_awaiting_llm_rows_after_n_unparseable_messages_disabled` (proves `awaiting_clarify` rows ARE written and are the *only* state that ever appears) | PASS |
| AC17 (R-B3/R-B4: target-NL → `/target` pointer; NL query → command pointer) | `test_row3_disabled_target_phrasing_points_at_target_command_no_llm`, `test_row4_disabled_query_never_classifies_returns_command_pointer`, `test_row4_disabled_query_via_full_routing_path` | PASS |
| AC18 (R-B5/R-B6: confirmation + review force static fallback) | `test_row5_disabled_diary_confirmation_forces_static_fallback_no_llm`, `test_row5_disabled_generic_text_habit_confirmation_forces_static_fallback_no_llm`, `test_row6_disabled_weekly_review_forces_static_narrative_no_llm` | PASS |
| AC19 (R-B7/B8/B9: no probe, no `OllamaClient`, no health ping/alert/recovery) | `test_row8_disabled_health_monitor_never_pings_ollama_no_alert_no_recovery`, `test_disabled_health_monitor_never_drifts_across_multiple_cycles` (health half, fully covered). **R-B7 (no probe) and R-B9 (no `OllamaClient` construction) live in `core/app.py`, which SPEC-LINE.md §11 reserves for Integration — genuinely untestable from Module B's owned files.** | **PASS (health half). Probe/construction half — see "Outstanding" below, not a Module B defect.** |

Every AC15-AC19 has at least one passing test. No AC is untestable-as-written; AC19's split ownership is a structural fact of the module boundary (§11), not a spec ambiguity — no escalation needed, but Integration must close it (tracked below).

## §5.2 call-site table — verified

| # | Site | Disabled disposition | Verified | Enabled byte-identity | Verified |
|---|---|---|---|---|---|
| 1 | `routing.py:handle_inbound_message` → `parser.py:parse_message` | Skip → synthetic `ExtractionResult.unknown()` → existing clarify branch, zero LLM | PASS (Luna's 2 tests + my structural sweep test) | Preparse miss still calls `parse_message` | PASS |
| 2 | `routing.py:reparse_pending_unparsed` → `parse_message` | Dead code; guard returns before single-flight check | PASS — **and** legacy `NULL`/`'awaiting_llm'` rows left genuinely inert (still visible to `pending_unparsed()`, not silently closed) — my new `test_legacy_awaiting_llm_and_null_rows_survive_disabled_reparse_guard_untouched` | Leftover row still reparsed/resolved | PASS |
| 3 | `routing.py` → `target_nl.py:classify_target_intent` | Skip classify; zero-LLM `looks_like_target_phrasing` gate → `/target` pointer | PASS | Still classifies + sets target | PASS |
| 4 | `routing.py` (`kind=="query"`) → `query.py:answer_question` → `classify_query_intent` | Skip classify → `query_no_llm_pointer` | PASS | Still classifies + answers with real number | PASS |
| 5 | `confirmation.py` (text/diary) → `chat_text` | Force `diary_reflection_fallback` | PASS | `chat_text` still called; **prompt is byte-identical to `DIARY_REFLECTION_SYSTEM_PROMPT`/`_USER_TEMPLATE`** (my new byte-level test) | PASS |
| 6 | `jobs.py:weekly_review_job` → `review.py:run_weekly_review` | Force `weekly_review_fallback_narrative`; stats/charts unchanged | PASS | Narrative call **byte-identical to `WEEKLY_REVIEW_SYSTEM_PROMPT`** (my new byte-level test) | PASS |
| 7 | `app.py:async_main` → `probe_schema_support` | Not implemented in Module B (owned by Integration, §11) | **N/A — see Outstanding** | — | — |
| 8 | `health.py:run_once` → `check_ollama` | Skip Ollama half entirely: no HTTP call, no alert, no recovery, **stable across repeated cycles** (my new multi-cycle test) | PASS | Still pings; **bare `HealthMonitor()` with no `ollama_enabled` kwarg at all is behaviorally identical to explicit `ollama_enabled=True`** (my new test — this is the shape every real pre-LINE call site uses) | PASS |

## Additional gap coverage (beyond §5.2, per dispatch instructions)

| Probe | Finding | Test |
|---|---|---|
| Backfill date-phrase + preparse **miss** in disabled mode | No crash, no fabricated backdate, zero LLM calls, no row written (nonsense text → generic clarify) | `test_backfill_date_phrase_with_preparse_miss_disabled_no_crash_no_fabricated_date` — PASS |
| Backfill date-phrase + preparse **hit** ("500ml 3 days ago") | Byte-identical logged row (category/value/backdated ts) whether `ollama.enabled` is True or False, with a **poisoned** llm/`parse_message` in both configs — this path never reaches the `elif`/`else` split at all | `test_backfill_date_phrase_with_preparse_hit_byte_identical_disabled_vs_enabled` — PASS |
| Reply-to-reminder attribution (already zero-LLM pre-LINE, R13/R14) | Provably unaffected by `config.ollama.enabled` — poisoned llm/`parse_message` in **both** configs still resolves correctly | `test_reply_to_reminder_attribution_zero_llm_in_both_configs` — PASS |
| `/checkin` (no `llm` param at all, pre-LINE zero-LLM) | Unaffected in either config — confirms Module B had nothing to touch here, and nothing regressed | `test_checkin_command_unaffected_by_ollama_enabled_in_either_direction` — PASS |
| Disabled-by-default surface | Bare `Config()` and the repo's own unmodified `config.toml` → `ollama.enabled=True` (loaded through real `load_config`, not grepped); `config.toml.line` → `ollama.enabled=False` | `test_bare_config_and_repo_config_toml_default_ollama_enabled_true`, `test_config_toml_line_loads_with_ollama_enabled_false` — both PASS |
| Structural clarify-handoff invariant | After 6 unparseable messages (mixed guess/no-guess) in disabled mode: **zero** rows anywhere in `logs` with `unparsed_state IN (NULL,'awaiting_llm')`; `awaiting_clarify` rows *do* appear and are the only state observed (sanity-checked non-vacuous) | `test_zero_awaiting_llm_rows_after_n_unparseable_messages_disabled` — PASS |

## The C-interleave audit (Module C's `/digest` branch in `routing.py`)

**What C did:** added a `command.kind == "digest"` branch in `handle_inbound_message` (routing.py:414-424) plus a `_MatcherEntry("digest", _ignore_registry(_match_digest))` row in `commands.py`'s `_MATCHERS` table (grouped next to `dashboard`), dispatching to a new `core/digest.py:execute_digest_toggle`.

**Classification:** this is a **legitimate, disciplined table-driven-dispatch addition**, not an ad-hoc routing-level hack. It follows the exact same shape as every other settings-style command (`lang`, `quiet`, `dashboard`) already in that table, and both the matcher-order comment and `_EXPECTED_ROW_ORDER` in `tests/test_refactor_s3.py` were updated to document it as the additive 29th row (after `guide`'s 28th, SPEC-v1.10.md R-SS8). `_assert_dispatch_invariants` still passes at import.

**Does it break any B test or path?** No.
- Structurally: the `/digest` branch sits in the top-level `if command is not None:` dispatch block, which is entirely **above** the `elif config.ollama.enabled: ... else: ...` preparse-miss split B owns (that split only runs when `command is None`). It cannot reach or perturb B's LLM-gating logic by construction.
- Behaviorally, verified: `test_digest_dispatch_makes_zero_llm_calls_in_either_config` drives `/digest off` then `/digest on` through `handle_inbound_message` with a **poisoned** llm/`parse_message` in both `ollama.enabled` configs — zero LLM calls, correct `digest_opt_out` toggling, both PASS.
- `test_digest_matcher_registered_disjoint_from_every_b_owned_command_kind` confirms `kind=="digest"` is disjoint from every kind B's own branches inspect (`query`, `target`).

**Is the edit itself sane?** Yes, with one caveat already flagged in IMPL-LINE-B.md §"Known limitations" item 3 and independently confirmed here: it's a genuine **file-ownership violation** of SPEC-LINE.md §11 (routing.py is Module B's file, not C's) — but it's a forced one, since `routing.py`'s command-dispatch table is the only place any command kind's execution can live, and no cleaner seam exists today. Flagging for Archi: §11's module split has a real gap here that future digest-like additions will hit again. Not a defect in what was built, and it does not affect this report's verdict.

**Independent re-verification of Luna's two previously-reported failures caused by this interleave:** both are now **fixed** (re-run below) — `tests/test_refactor_s3.py::test_matchers_table_has_all_27_rows_in_the_exact_pre_conversion_order` and `tests/test_audit.py::test_actions_matches_the_spec_vocabulary_exactly` both PASS as of this pass. The tree has moved on since IMPL-LINE-B.md was written; C's golden-list updates caught up.

## Parallel-tree noise (confirmed, not Module B's)

Full LINE gate at time of this report: **2 failures**, both independently traced, neither touching a Module-B-owned file:

1. **`tests/test_line_d_gaps.py::test_line_templates_are_lf_only_on_disk[.env.example]`** — Module D's own file (`.env.example`) has CRLF on disk; Module D's own test docstring already documents this as a known hygiene gap (fixed by `.gitattributes` `eol=lf` at commit time, not a functional break). Not present in Luna's earlier 3-failure snapshot — a new Module D-owned test landed since; still nothing to do with Module B.
2. **`tests/test_v110_m3_gaps.py::test_run_due_reminders_actually_paused_user_suppressed_others_unaffected`** — confirmed genuine **pre-existing date-dependent flake**, unrelated to any branch work. Traced to source: `core/reminders.py:429` computes the pause-suppression date via `datetime.now(ZoneInfo(...))` (**real wall clock**), not the test's injected `clock=`. The test hardcodes a pause window `2026-08-27..2026-08-27`; against today's real date (2026-08-30 per system clock) that window has already expired, so the pause no-longer-applies and the reminder correctly fires — the test's own fixture, not the implementation, is stale. `git status` confirms `tests/test_v110_m3_gaps.py` is untouched (not in the worktree's modified-files list) — this predates the LINE branch entirely. Matches IMPL-LINE-B.md's own characterization exactly; independently re-derived here, not just trusted.

Both were already called out by Luna as out-of-scope; this pass re-confirms both from first principles (source trace + git status), and confirms the golden-list pair she'd also flagged is now resolved.

## Regressions detected

None. `tests/test_query.py`, `tests/test_review.py`, `tests/test_confirmations.py`, `tests/test_bilingual_confirmations.py`, `tests/test_v08_query_gaps.py`, `tests/test_resilience.py`, `tests/test_i18n.py`, `tests/test_i18n_literals.py` → **159 passed, 0 failed**.

## Outstanding (not a Module B defect — flagging for Archi/Integration)

Per IMPL-LINE-B.md's own "Known limitations" #1 (independently confirmed correct by this pass): R-B7 (skip startup probe) and R-B9 (no `OllamaClient` construction when disabled) are **not yet wired** — they require edits in `core/app.py`, which §11 reserves exclusively for the Integration pass. `core/health.py`'s `ollama_enabled` flag exists and is fully tested (this report), but nothing in the current tree passes `ollama_enabled=config.ollama.enabled` into `HealthMonitor(...)` from `app.py` yet, and no LINE-branch code skips `probe_schema_support`/`OllamaClient` construction yet. This is exactly the shape §11 anticipated ("droppable if integration owns the wiring") and does not block Module B's own verdict — it blocks AC28 (Integration), not AC19 (Module B, already fully covered on the health-monitor half). Recommend Archi confirm Integration's dispatch explicitly includes these three wiring steps before declaring AC28/AC19 fully closed end-to-end.

## Subset numbers (for Archi)

- `pytest tests/test_line_no_llm.py tests/test_line_b_gaps.py` → **31 passed, 0 failed**.
- `pytest tests/test_query.py tests/test_review.py tests/test_confirmations.py tests/test_bilingual_confirmations.py tests/test_v08_query_gaps.py tests/test_resilience.py tests/test_i18n.py tests/test_i18n_literals.py` → **159 passed, 0 failed**.
- `pytest tests/ -m "not telegram_only and not llm_only"` (full LINE gate) → **4896 passed, 4 skipped, 153 deselected, 1 xfailed, 2 failed** (both failures out of Module B's scope, detailed above; down from Luna's earlier 3 as the golden-list pair caught up, with one new Module-D-owned failure surfacing since).

## Recommendation

**Ready to ship** (Module B). All 5 owned ACs (AC15-AC19) pass; the health-monitor half of AC19 is fully tested, and its two remaining clauses are correctly scoped to Integration, not missing work. Zero regressions in touched or adjacent modules. The Module C `/digest` interleave in `routing.py` is sane, disciplined, and provably non-interfering with Module B's LLM-gating contract — flagged to Archi as a §11 file-ownership gap for future reference, not a blocker. The 2 remaining LINE-gate failures are both independently confirmed out of scope (Module D hygiene gap; pre-existing date-dependent flake in an untouched file).

Only action item: Archi should confirm Integration's `app.py` pass explicitly wires R-B7/R-B9 (see "Outstanding" above) before declaring AC19/AC28 fully closed end-to-end.
