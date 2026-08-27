# Test Report — Refactor Stage 1 (DB + tick performance), v1.9.1

## Summary

- Total: 4306 tests (baseline 4258 + db-track 22 + tick-track 12 + Vera's own adversarial file 14)
- Passed: 4303
- Failed: 1 (a NEW adversarial finding from Vera's own file, not a regression in the 4292-test baseline — see "Failures")
- Skipped: 1 (pre-existing, unrelated: `test_channels.py:238`, platform-gated)
- XFailed: 1 (pre-existing, unrelated: `test_announce_gaps.py`'s documented TOCTOU race, Archi-ruled out of scope 2026-08-23)
- Stable across two consecutive full foreground runs (197.41s, 198.05s; a third `-rs` run also matched) — identical counts, identical single failure, both times
- **Status: PASS on every numbered Stage 1 AC (AC1–AC5, AC-G1, AC-G2) — with one escalated finding outside the numbered ACs (see "Failures" and "Recommendation")**

## The 3 approved test-only fixes (done first, as instructed)

| # | File | Problem | Fix |
|---|---|---|---|
| 1+2 | `tests/test_pause.py` | `_resume()` helper never threaded a clock into `execute_resume` (which has had a `clock` param since the v1.9 round-2 fix) — real `datetime.now()` drifted past the file's hardcoded `TODAY = date(2026, 8, 26)` as soon as the system clock advanced, flipping `_resume_scope`'s truncate-vs-delete branch and breaking `test_resume_habit_deletes_row_and_confirms` / `test_resume_all_clears_every_row` | Added `clock=_fixed_clock` (the same fixture `_pause()` already uses) as a parameter on `_resume()`, threaded into `pause.execute_resume(..., clock=clock)`. Every one of the file's ~10 `_resume(...)` call sites now runs on the fixed clock, structurally, not just the two that were failing today — kills the whole drift class in this file rather than bumping the literal |
| 3 | `tests/test_refactor_stage1_tick.py::test_run_due_reminders_bulk_path_byte_identical_to_fallback` | Opened with `assert not hasattr(db, "all_reminder_times")`, written before the S1-A/db track's accessor landed; now that it's real, the precondition itself fails before the test's actual bulk-vs-fallback logic ever runs | Replaced the stale precondition with `assert hasattr(db, "all_reminder_times")`. The fallback path is now forced by temporarily shadowing the real method with an instance attribute set to `None` (`db.all_reminder_times = None`) — `_bulk_reminder_time_overrides`'s `getattr(db, "all_reminder_times", None)` sees exactly what "not available" looked like pre-landing, since a plain function is a non-data descriptor and an instance-`__dict__` entry takes precedence over it. `del db.all_reminder_times` restores the real accessor for the bulk-path half of the comparison. The test now exercises its real logic (byte-identical bulk vs fallback sends) instead of short-circuiting on a dead assumption |

All 3 fixes verified: `tests/test_pause.py tests/test_refactor_stage1_tick.py tests/test_refactor_s1_db.py` → **91 passed**. Confirmed the strict AC1 `<=3` branch is the one actually exercised now (`hasattr(db, "all_reminder_times")` is `True` on a real `Database`).

## Benchmark reproduction (`tools/bench_baseline.py`, run live, foreground, scratch DB — 3 users × 365 days × 6570 rows)

| Metric | Measured (this run) | Luna's claim (`IMPL-refactor-s1-db.md`) | Spec floor (§8) | Verdict |
|---|---|---|---|---|
| A. idle reminder tick, queries/call | **2** | 2 (once AC1's accessor consumer lands) | ≤ 3 | PASS, with margin |
| H. `sum_value` LIKE vs range-bound | 0.1602 ms vs 0.0081 ms → **19.8×** | ~22.8× | ≥ 14× (spec-measured) | PASS |
| I. `insert_log` commit, FULL vs NORMAL | 1.3964 ms vs 0.2564 ms → **5.45×** | ~5.3× | ≥ 3× (1.45ms→≤0.5ms; achieved 0.256ms) | PASS |
| EXPLAIN QUERY PLAN, range-bound `sum_value` | `SEARCH logs USING INDEX idx_logs_user (user_id=? AND category=? AND ts>? AND ts<?)` | same | genuine index range-seek, not a scan | PASS |

No regression anywhere in the benchmark's other sections (B–G unchanged in shape from the pre-refactor baseline; F's `handle_inbound_message '500ml'` stays at 33 queries, same count as before Stage 1, only faster per the NORMAL-pragma write-cost drop). Small numeric differences from Luna's own run (19.8× vs her 22.8×, 5.45× vs her 5.3×) are ordinary machine-to-machine timing variance — both floors clear with comfortable margin.

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_pause.py` | 0 added, 2 fixed (date-drift, structural) | (regression suite, not Stage-1-owned) |
| `tests/test_refactor_stage1_tick.py` | 0 added, 1 fixed (stale precondition) | AC1, AC4, AC5 (Luna's own 12 tests) |
| `tests/test_refactor_s1_db.py` | 0 (Luna's own 22, unmodified) | AC2, AC3, + the `all_reminder_times()` cross-track accessor |
| `tests/test_refactor_s1_gaps.py` (**new, this round**) | 14 | AC1 (larger-shape query bound), AC2 (5 hostile-shape tests), AC4 (ordering, scheduler coherence, **1 escalated finding**), AC-G2 (3 hand-pinned byte-identity spot-checks), reminder-parity beyond AC1/AC4's own numeric wording |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC-G1 (full suite green, only sanctioned mechanical literal changes) | Full suite run (below); `test_reminders.py`'s job-id assertion (`reminder_tick`→`minutely_tick`) is the only pre-existing-test literal change, sanctioned (internal wiring id, not an emitted string/DB row/PNG) | **PASS** |
| AC-G2 (byte-identical output probe) | `test_refactor_s1_gaps.py::test_typed_log_confirmation_is_byte_identical`, `::test_history_render_is_byte_identical`, `::test_checkin_message_is_byte_identical` (hand-pinned literals) + every pre-existing byte-identical corpus (`test_confirmations.py`, `test_ac17_v060_byte_identical_composite.py`, `test_reminders.py`, etc.) passing unmodified | **PASS** |
| AC1 (idle reminder tick ≤3 queries, baseline 13) | `test_refactor_stage1_tick.py::test_idle_reminder_tick_query_count_ac1` (strict branch now active) + `test_refactor_s1_gaps.py::test_idle_tick_query_bound_holds_at_a_larger_user_habit_shape` (4 users × 6 habits, still ≤3 — proves O(1), not just correct at the 3×3 baseline) + bench section A (2 queries live) | **PASS** |
| AC2 (byte-identical `sum_value`/`count`/`count_true` across boundaries) | Luna's 16 boundary/rollover/fuzz tests (unmodified) + `test_refactor_s1_gaps.py`'s 5 hostile-shape tests (cross-user/cross-category contamination at the boundary instant, a zero-row day sandwiched between boundary-adjacent days, timestamps written through the REAL production write path, a soft-deleted row at the single tightest last-second boundary, a 90-day seeded-random multi-month fuzz including a leap February) | **PASS** |
| AC3 (`synchronous=NORMAL` ≥3× write speedup, identical rows) | `test_refactor_s1_db.py`'s pragma tests + live benchmark (5.45×) + every `insert_log` round-trip test in `test_db.py` unmodified | **PASS** |
| AC4 (consolidated tick: `active_user_ids()` once, byte-identical sends) | `test_consolidated_minutely_tick_calls_active_user_ids_once_ac4` + `test_active_user_ids_param_short_circuits_internal_call_ac4` (Luna's) + `test_refactor_s1_gaps.py`'s explicit ordering proof and scheduler-coherence test (both new, both pass) | **PASS on AC4's literal wording** ("called once" + "byte-identical sends" both proven) — **see the escalated finding below**: rule 2's own broader claim ("the three fan-outs are independent") does not hold under a raising scenario, a dimension AC4 itself doesn't number |
| AC5 (`build_checkin_message` reads `active_pauses` once, identical message) | `test_build_checkin_message_reads_active_pauses_once_ac5`, `test_build_nudge_message_reads_active_pauses_once`, `test_pause_reuse_still_excludes_paused_habit_byte_identical` (Luna's), reinforced by `test_refactor_s1_gaps.py`'s own suppression-interplay test (pause suppression proven identical on both the bulk and forced-fallback reminder paths) | **PASS** |

Every AC from SPEC-REFACTOR.md §8's Stage 1 section appears above with a PASS verdict on its own literal wording.

## Failures

### `test_refactor_s1_gaps.py::test_one_ticks_exception_does_not_suppress_the_other_two_ticks_same_minute`

- **What was tested:** whether the pre-Stage-1 per-job failure isolation (three independent APScheduler jobs — `reminder_tick`/`checkin_tick`/`nudge_tick`, each on `CronTrigger(second=0)`) survived the Stage 1 rule-2 consolidation into one `minutely_tick` job.
- **AC violated:** none of AC1–AC5 by their literal wording. This is a gap in rule 2's own broader claim ("Byte-identical: the three fan-outs are independent and order-free," SPEC-REFACTOR.md §4 rule 2) and in §1's general "no change to any observable behavior" framing — neither is a numbered AC, and the specific invariant list in §3 (i18n strings / DB rows / migrations / config meaning / command grammar / PNGs) does not name scheduler-job failure isolation either. This is a genuine spec-completeness gap, not a violation of a written test.
- **Input:** `run_due_reminders` (monkeypatched to raise `RuntimeError`, simulating e.g. a real DB read error escaping `db.all_reminder_times()`/`db.get_reminder_times()`, which — unlike `_goal_already_met`/`effective_quiet_windows` — is not wrapped in its own try/except inside `run_due_reminders`) fired through the real `main.py:async_main` → `_minutely_tick` wiring, with `checkins.run_due_checkins`/`nudge.run_due_nudges` spied.
- **Expected:** matching the pre-Stage-1 semantics, `checkins.run_due_checkins`/`nudge.run_due_nudges` should still run the same minute even though `run_due_reminders` raised (APScheduler's default executor isolates one job's exception from any other independently-scheduled job due the same tick).
- **Actual:** neither ran. `calls == ["reminders"]` — the exception propagated straight out of `_minutely_tick`, skipping the two subsequent `await`s.
- **Stack trace / output:**
  ```
  AssertionError: REGRESSION vs pre-Stage-1 isolation: run_due_reminders raising suppressed
  checkins/nudge for this whole tick (only ['reminders'] ran) -- under the old
  3-independent-scheduler-jobs design, checkin_tick/nudge_tick would still have fired this
  same minute regardless of reminder_tick's own failure.
  assert ('checkins' in ['reminders'])
  ```
- **Suspected cause:** `src/habit_assistant/main.py:1963-1985` (`_minutely_tick`) — the three tick calls are plain sequential `await`s with no try/except between them:
  ```python
  async def _minutely_tick() -> None:
      active_ids = db.active_user_ids()
      await run_due_reminders(channel, config, registry, db, reminder_state, registry_for=provider.for_user, active_user_ids=active_ids)
      await checkins.run_due_checkins(channel, config, registry, db, registry_for=provider.for_user, active_user_ids=active_ids)
      await nudge.run_due_nudges(channel, config, registry, db, registry_for=provider.for_user, active_user_ids=active_ids)
  ```
  This is the one exception-propagation path in the three-function chain that isn't already covered by one of the pervasive internal fail-open wrappers this codebase otherwise uses everywhere else in `reminders.py`/`checkins.py`/`nudge.py` (every per-user loop in the other two files is individually try/except-wrapped; `run_due_reminders`'s own loop is not, and `_bulk_reminder_time_overrides`/`db.all_reminder_times()`/`effective_reminder_times` are called outside any try in that function).
  **Minimal fix** (not applied — production code is out of scope for this test-only pass): wrap each of the three `await`s in `_minutely_tick` in its own try/except (log + continue), restoring per-tick isolation. Mechanical, ~6 lines, no signature changes, no effect on any of AC1–AC5's own proven behavior.

## Regressions detected

None. All 4292 baseline+db-track+tick-track tests pass; the one failure above is a new test in Vera's own adversarial file, proving a gap neither IMPL doc addressed — not a previously-passing test that broke.

## Final suite numbers (both runs)

```
Run 1: 1 failed, 4303 passed, 1 skipped, 1 xfailed in 197.41s (0:03:17)
Run 2: 1 failed, 4303 passed, 1 skipped, 1 xfailed in 198.05s (0:03:18)
Run 3 (verification, -rs): 1 failed, 4303 passed, 1 skipped, 1 xfailed in 198.88s (0:03:18)
```
Identical counts and the identical single failure all three times — stable, not flaky. The pre-existing skip (`test_channels.py:238`) and xfail (`test_announce_gaps.py`'s Archi-ruled TOCTOU race) are both unrelated to Stage 1 and match SPEC-REFACTOR.md's own stated baseline.

## Recommendation

**Escalate to Archi — spec-completeness gap discovered (does not block AC1–AC5/AC-G1/AC-G2).**

Every numbered Stage 1 acceptance criterion (AC1–AC5) and both cross-cutting gates (AC-G1, AC-G2) is independently proven PASS, including under adversarial conditions beyond what either IMPL doc's own tests covered (hostile LIKE→range boundary shapes, full suppression-layer interplay + stored-language-pref resolution on the reminder bulk-vs-fallback parity, a larger-shape query-count proof, and hand-pinned byte-identity spot-checks). The benchmark's claimed deltas reproduce live with comfortable margin over both floors, and no regression was found anywhere in 4292 pre-existing tests across two stable, foreground, non-flaky full-suite runs.

The one failure is a **new finding**, not a violation of any written AC: the three-jobs-into-one consolidation (rule 2/AC4) silently changed cross-function failure isolation — a real DB error escaping `run_due_reminders` now also silently skips that minute's check-ins and nudges, where the pre-Stage-1 three-independent-scheduler-jobs design would not have. This contradicts rule 2's own "independent" framing and this codebase's own pervasive fail-open convention, but touches no i18n string, DB row, migration, config key, command grammar, or PNG — the literal invariant list SPEC-REFACTOR.md §3 names. Two reasonable dispositions, Archi's call:

1. **Fix now** (recommended, given how small it is): have the tick-track Luna wrap each of the three `_minutely_tick` awaits in `main.py:1963-1985` in its own try/except (log + continue) — mechanical, ~6 lines, doesn't touch any of the 5 already-proven ACs — then re-run this one test to confirm, and tag v1.9.1.
2. **Accept and document**, mirroring the `test_announce_gaps.py` TOCTOU precedent (Archi ruling recorded in `PROGRESS.md`, test converted to `xfail(strict=False)` citing that ruling) — reasonable if Archi judges a scheduler-job-level DB error escaping all the way out of `run_due_reminders` to be pathological enough in practice (every per-user/per-habit failure mode inside it already has its own fail-open guard; only a hard failure in the bulk reminder-time read itself would reach this path) that the fix can wait for a later stage.

Either way, this does not implicate the DB track (`storage/db.py`) at all, and does not require reopening AC1–AC5.
