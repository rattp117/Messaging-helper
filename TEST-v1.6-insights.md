# Test Report — v1.6.0 `insights` module (Personal bests & records, Deterministic trends)

## Summary
- Total (module-owned): 93 tests — 81 Luna (`test_records.py` 44 + `test_trends.py` 37) + 12 Vera (`test_insights_gaps.py`)
- Passed: 93
- Failed: 0
- Full suite: **3001 passed, 0 failed, 1 skipped, 1 xfailed** (two consecutive stable runs)
- Status: **PASS**

## Round 2 — Archi's ruling on the escalated judgment call, applied and re-verified

Round 1 (see below) escalated one judgment call instead of failing it: whether a habit's first-ever log should be celebrated as its own "beaten crossing." **Archi ruled: no — first-ever logs seed the record row silently (baseline stored via `upsert_record`), celebrations fire only when a value strictly exceeds an already-stored record.** Luna implemented this in `core/records.py:_maybe_break_record` (now: unconditional `upsert_record` on any genuine improvement, but returns `None`/no-celebration when `current is None`, i.e. the first observation) and updated her own suite (43→44 tests, one split into two: the renamed `test_first_log_seeds_records_silently_without_celebrating` + new `test_second_log_that_exceeds_the_silently_seeded_baseline_celebrates`).

**One of my own 12 gap tests encoded the overruled behavior** and broke as a direct, correct consequence: `test_boolean_habit_best_week_uses_count_true_across_the_whole_week` seeded 4 raw rows then called `update_on_log` once, asserting `("best_week", 3.0) in broken` — under the new rule that single call is now (correctly) a silent first observation, so `broken == []`. Per Luna's own report she left this test untouched and flagged it back, rather than editing my test herself — correct etiquette. **Fixed**: the test now asserts the seeded baseline directly via `db.get_record(...) == 3.0` (proving `count_true`, not raw `count`, produced the seed value) on call 1, then adds one more truthy day and calls `update_on_log` a second time, asserting a genuine celebration at `("best_week", 4.0)` — stronger than the original (proves the aggregate is correct on *both* the seed path and the comparison path, not just one).

**Independent re-verification of the ruling implementation** (not just trusting Luna's report — read `core/records.py:_maybe_break_record`/`update_on_log` directly, then ran a standalone script against a real on-disk `Database`, bypassing both test suites):
```
Day1 (first-ever log, streak=1) broken: []            # silent seed, all 3 types
Day1 stored best_day/best_week/longest_streak: 10.0 / 10.0 / 1.0   # correct baseline values
Day2 (streak=2, genuine improvement) broken: [('best_week', 20.0), ('longest_streak', 2.0)]
Day2 stored longest_streak: 2.0                         # celebrates exactly once, correct value
Day-far (isolated streak=1, 1 <= stored 2) broken: []   # equal-or-smaller never celebrates
stored longest_streak unchanged: 2.0
```
This confirms, independent of both Luna's and my own test suites: **silent seed writes the correct baseline for all three record types** (including `longest_streak` through the real `streaks.compute_streak` engine, not just the two aggregate types); **a second, genuinely larger log celebrates exactly once with the correct value**; **equal-to-stored does not celebrate**. All three of Archi's requested re-verification points hold.

**My other 11 gap tests**: re-ran unmodified — all 11 still pass (the ruling only touches first-observation behavior; my other tests either seed a pre-existing baseline via `db.upsert_record` directly, don't touch `update_on_log`'s celebration path at all — the `trends.py` tests, the render-budget tests, the collision-sweep tests — or use two-user/two-habit setups where the specific record type under test isn't a first observation).

## Test files
| Path | Tests | Covers |
|---|---|---|
| `tests/test_records.py` (Luna) | 44 | AC-R1, AC-R2, AC-R3, AC-X1, AC-X3 |
| `tests/test_trends.py` (Luna, untouched by Round 2 — trends.py has no first-log-celebration logic) | 37 | AC-T1, AC-T2, AC-T3, AC-X1, AC-X3 |
| `tests/test_insights_gaps.py` (Vera) | 12 (1 rewritten this round) | AC-T2 (run-length boundary), AC-T3 (empty current week), AC-T1 (trends tz boundary), AC-R1 (multi-habit independence, boolean best_week — now correctly exercising the silent-seed rule), AC-R3/AC-T1 (render-budget at scale), dispatch collision sweep |

No production code was modified by me at any point in this pass (both rounds). Only `tests/test_insights_gaps.py` was added/edited.

## AC coverage
| AC | Test(s) | Status |
|---|---|---|
| AC-R1 (stored + updated) | `TestUpdateOnLog` (12, Luna, incl. the new silent-seed split) + `test_two_different_habits_records_stay_independent_when_interleaved`, `test_boolean_habit_best_week_uses_count_true_across_the_whole_week` (Vera, rewritten) | **PASS** |
| AC-R2 (celebrate once, fail-open) | `test_second_log_that_exceeds_the_silently_seeded_baseline_celebrates`, `test_second_smaller_log_same_day_does_not_break_or_celebrate_again`, `test_equal_value_does_not_celebrate_strict_inequality`, `test_fail_open_never_raises_and_returns_empty_list`, `TestFormatCelebration` (6, Luna) | **PASS** |
| AC-R3 (`/records` view) | `TestRender` (12) + `TestDispatchShape` (9, Luna) + render-budget + collision-sweep tests (Vera) | **PASS** |
| AC-T1 (`/trends` week-over-week + delta/%) | `TestCompute::test_matches_spec_sample_numbers_exactly`, `test_negative_delta_and_pct` (Luna) + tz-boundary, empty-current-week, render-budget tests (Vera) | **PASS** |
| AC-T2 (review block + run-length ≥2 callout) | `TestReviewBlock` (3) + 4 run-length tests (Luna) + 2 run-length boundary tests (Vera) | **PASS** |
| AC-T3 (insufficient history, no ÷0 / misleading %) | 3 tests (Luna) + `test_empty_current_week_against_a_real_previous_week_no_crash_correct_delta` (Vera) | **PASS** |
| AC-X1 (registry-generic) | Luna, both modules | **PASS** |
| AC-X3 (per-user isolation) | Luna, both modules | **PASS** |

Every AC owned by this module (SPEC-v1.6.md §11: AC-R1, AC-R2, AC-R3, AC-T1, AC-T2, AC-T3) is covered and passing. AC-1/AC-2/AC-3/AC-X2 are shared-surface-owned (not this module's scope).

## Failures (if any)
None (the one flagged in Round 2 — `test_boolean_habit_best_week_uses_count_true_across_the_whole_week` — is fixed; see "Round 2" above).

## Regressions detected
None owned by this module. Round 1 saw one transient, unrelated flake in `tests/test_dashboard_gaps.py` on a concurrent full-suite run — self-resolved on rerun, traced to a sibling track's concurrent file edits, not a records/trends issue. Not reproduced in Round 2.

## Judgment-call audits (final status)

### 1. First-ever log celebrated as its own "beaten" crossing
**Verdict: RESOLVED by Archi's ruling (2026-08-24) — first-ever logs seed silently, no celebration.** See "Round 2" above for the implementation and independent re-verification. This matches the reasoning I raised in Round 1 (R-R2's "strictly exceeds the stored record" presupposes a stored record; the milestone precedent it claims to mirror never fires on a literal day-1 streak by default config) — Archi's ruling landed on the same side I flagged as the more spec-faithful reading, without me having pre-judged it as a hard FAIL in Round 1 (correctly routed as an escalation, not invented).

### 2. Records never revert on undo (the typo-record sharp edge)
**Verdict: CONFORMANT — unchanged by Round 2.** `test_undo_does_not_revert_an_already_celebrated_record` was restructured by Luna to seed a baseline on day 1 (silent) and genuinely break the record on day 2 (celebrated) before the undo — the underlying claim (records don't self-heal on undo, by explicit spec design: R-R1's "stored, not re-derived" + no `update_on_undo` in §5, contrasted with R-D5's explicit undo trigger for dashboard) is untouched by the Round 2 ruling and re-confirmed passing. Still a flagged product-risk note, not a bug.

### 3. ISO dates vs. spec's illustrative `"(12 Aug)"`/`"(5–11 Aug)"` sample format
**Verdict: CONFORMANT — unaffected by Round 2.** No change to date rendering in this round; original reasoning (§3 is illustrative, §4/§8 are normative, independently corroborated by the sibling `dashboard`/`nudge` Vera reports) stands.

## Integration status (informational — still not this module's scope)
Unchanged from Round 1: `main.py` still has no `records.`/`trends.` wiring — correctly deferred to Archi's integration pass per SPEC-v1.6.md §11's module-split plan.

## Recommendation
**Ready to ship.** All 6 owned ACs (AC-R1, AC-R2, AC-R3, AC-T1, AC-T2, AC-T3) pass; the one escalated judgment call from Round 1 has been ruled on by Archi, implemented by Luna, and independently re-verified by me (not just re-trusted) at both the test-suite and raw-behavior level; full suite green at **3001 passed, 0 failed, 1 skipped, 1 xfailed** (stable across two consecutive runs). Zero production-code changes needed from Vera at any point. No further Luna↔Vera round needed for the `insights` module. Note for Archi: the xfail count is expected to become 2 once the `dashboard` track's own round-2 flip lands (per that track, not this one) — current 1 xfailed reflects a snapshot before that lands, not a discrepancy in this module's work.
