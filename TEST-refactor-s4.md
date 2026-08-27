# Test Report — Refactor Stage 4: dead code + test health (v1.9.4)

## Summary

- Full suite RUN 1: 4484 passed, 1 skipped, 1 xfailed, 0 failed in 203.13s
- Full suite RUN 2 (consecutive): 4484 passed, 1 skipped, 1 xfailed, 0 failed in 199.58s
- Baseline: 4484 passed / 0 failed / 1 skipped / 1 xfailed, ~200s — **matched exactly, both runs**
- `git status` confirms the working tree touches exactly the files IMPL-refactor-s4.md claims (`core/i18n.py`, `tests/conftest.py`, 8 named test files, `SPEC-REFACTOR.md` prose) — no unmigrated file, no other production file, touched
- **Status: PASS** — this is the final refactor stage; recommend release of v1.9.4 and closing the refactor initiative.

## Test files

| Path | Tests added/modified | Covers |
|---|---|---|
| `tests/conftest.py` | +114/-1 lines: `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler` + autouse `_reset_shared_doubles` fixture (no new test functions) | AC12 |
| `tests/test_checkins.py`, `test_nudge.py`, `test_reminders.py` | Doubles swap only, 0 assertion changes | AC12 |
| `tests/test_v16_integration.py`, `test_v17_integration.py` | Doubles swap only, 0 assertion changes | AC12 |
| `tests/test_announce.py`, `test_discoverability.py`, `test_heatmap_gaps.py`, `test_v07_m3_review_extra.py` | `tempfile.TemporaryDirectory()` → `tmp_path` fixture, 0 assertion changes | AC13 |
| `src/habit_assistant/core/i18n.py` (production, sanctioned) | 3 dead catalog keys removed | AC11 |
| `SPEC-REFACTOR.md` (doc-only, sanctioned by Archi task item 4) | Rule 14 prose order corrected | Doc-fix verification |

No new test files were added in this stage (dead-code removal and test-plumbing consolidation don't need new test cases — they need proof the existing suite still passes byte-identically).

## AC coverage

| AC | Verification | Result |
|---|---|---|
| **AC11** — 3 dead `dashboard_line_*_weeks` keys + unused `BUILTIN_IDS` import removed; catalog tests still pass | Independent grep: `grep -rn "dashboard_line_goal_weeks\|dashboard_line_boolean_weeks\|dashboard_line_count_weeks" src/ tests/` → **zero hits** anywhere. Independent programmatic diff of the full `CATALOG` dict (AST-parsed) between v1.9.3 (`ed99314`) and the current working tree: `removed keys: ['dashboard_line_boolean_weeks', 'dashboard_line_count_weeks', 'dashboard_line_goal_weeks']`, `added keys: []`, `modified common keys: []`, `old count 387, cur count 384` — **exactly 3 removals, 0 modifications, 0 additions**, no other key touched. `git diff` on `i18n.py` confirms a single hunk, pure deletion, nothing else in the file changed. `BUILTIN_IDS` check: `grep -rn "BUILTIN_IDS" src/` → only `core/habits.py:31` (definition) and `core/reminders.py:74,390` (legitimate use) — **not present in `main.py` at all** (confirmed by reading `main.py`'s current header, which documents it as a Stage-2 thin shim); this half was already resolved by Stage 2's full rewrite, correctly not re-claimed as new work here. Targeted run: `test_i18n.py test_i18n_literals.py test_dashboard.py test_dashboard_gaps.py` → **114 passed**, matching IMPL's claim exactly. | **PASS** |
| **AC12** — shared `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler` in `conftest.py`; migrated tests assert on identical surfaces | Spot-diffed **all 5** migrated files' `git diff HEAD` (task asked for 3; did all 5 for full coverage): `test_checkins.py`, `test_nudge.py`, `test_reminders.py` (channel doubles) and `test_v16_integration.py`, `test_v17_integration.py` (scheduler+LLM doubles). Every deleted local class body is byte-identical to the shared `conftest.py` class it was replaced by, **except** `FakeScheduler.add_job`, which gains an explicit `kwargs=None` param + stores `job.kwargs` — a strict superset (old code silently swallowed any `kwargs=` a caller passed into its `**kwargs` catch-all without ever exposing it; nothing in the suite read `job.kwargs` before, so this is additive, not a behavior change, and it's the honest thing to have flagged as new capability rather than "byte-identical"). Zero assertion lines changed in any of the 5 diffs. **Isolation probe** (temporary `tests/test_zz_vera_isolation_probe.py`, written, run, then deleted): test A dirties `FakeOllamaClient.responses` and `FakeScheduler.last_instance` without cleaning up; test B (run immediately after) asserts both are clean — **2/2 passed**, confirming `_reset_shared_doubles` actually prevents cross-test leakage, not just a documented intent. **Test-count re-verification independent of Luna's claim**: `--collect-only` on the 5 files at current state → **164 collected**; `git stash push` on just those 5 files (reverting to HEAD/v1.9.3) → `--collect-only` again → **164 collected** — exact match, then `git stash pop` restored the working tree cleanly. **~55 unmigrated files** (independently counted via `grep -rl "class.*Fake.*Channel\|class _FakeScheduler\|class _FakeOllamaClient\|class FakeChannel" tests/*.py`, excluding `conftest.py` — close to IMPL's "~60" estimate) are confirmed untouched by `git status` and pass as part of the full-suite runs below. | **PASS** |
| **AC13** — 4 fixed-`Temp`-path files use `tmp_path`; no more shared-path race | `grep -n "tempfile\|TemporaryDirectory\|gettempdir"` across all 4 files → **zero hits**. `git diff` on all 4 files shows clean 1:1 conversions (`tempfile.TemporaryDirectory()` block → `tmp_path` fixture param), zero assertion-line changes, including the `test_v07_m3_review_extra.py` string-path→`Path`-object normalization IMPL called out. **Windows file-handle wobble check**: ran the 4 files twice, back-to-back, foreground — RUN 1: **178 passed** in 4.60s; RUN 2 (immediately after): **178 passed** in 4.96s — identical counts, no lingering-handle flake. | **PASS** |
| **Rule-14 doc fix** — SPEC-REFACTOR.md's corrected dispatch order matches `TEST-refactor-s3.md`'s ground truth exactly | Token-by-token comparison: `TEST-refactor-s3.md` "Findings #1" ground truth — `undo → edit → snooze → target → remind → access → audit → lang → quiet → checkin → dnd → dashboard → history → heatmap → records → trends → wrapped → addhabit → delhabit → log → routine → cadence → pause → resume → help → habits → query` (27 items, `query` last) — is **identical, in the same order**, to `SPEC-REFACTOR.md` rule 14's corrected "Order (27 branches)" line, and the accompanying note correctly cites "corrected 2026-08-27 per Vera's TEST-refactor-s3.md finding" with an accurate restatement of what was wrong in the prior draft (omitted `pause`/`resume`, misplaced `query`). | **PASS** |
| **AC-G1** — full suite green, byte-identical, zero unsanctioned edits | Two consecutive foreground full-suite runs (below) both **4484 passed, 1 skipped, 1 xfailed, 0 failed**. `git status --porcelain` shows only the files IMPL-refactor-s4.md claims — no test file outside the named 8 was touched. | **PASS** |
| **AC-G2** — byte-identical output probe | All migrated-file diffs contain zero `assert`/string/PNG-output line changes (verified directly, not just via IMPL's claim). i18n diff is pure key deletion with zero string edits to any surviving key. | **PASS** |

## Full-suite runs (both foreground, `PYTHONPATH=src`, no `-n`, no `uv`)

```
RUN 1: 4484 passed, 1 skipped, 1 xfailed in 203.13s (0:03:23)
RUN 2: 4484 passed, 1 skipped, 1 xfailed in 199.58s (0:03:19)
```

Both within noise of the ~200s baseline; no runtime regression.

## Closeout benchmark — `tools/bench_baseline.py` (scratch DB only, `data/habits.db` never touched — verified by reading the script: `SCRATCH = Path(tempfile.gettempdir()) / "habit_bench_scratch.db"`)

| Metric | Original audit baseline (SPEC-REFACTOR.md §4) | End-state (this run, post-Stage-4) | Floor / target | Verdict |
|---|---|---|---|---|
| **Idle reminder-tick DB queries** | 13 queries/tick (rule 1) | **2 queries/tick** (`A. run_due_reminders NOTHING DUE`) | ≤ 3 (AC1) | **PASS** — 6.5× reduction |
| **Consolidated 3-tick idle minute** | 3× redundant `active_user_ids()` (rule 2) | **2 queries total** for all 3 ticks combined (`E. ALL THREE TICKS`) — same as tick A alone, confirming `active_user_ids()` is not re-fetched per job | called once, not 3× (AC4) | **PASS** |
| **Day-filter query speed** (LIKE vs range, 1yr water rows) | 0.195 ms (LIKE) vs 0.0135 ms (range) — 14.4× (rule 3) | **0.1585 ms (LIKE) vs 0.0078 ms (range) — 20.3×** (`H.`) | range-bound faster, byte-identical results (AC2) | **PASS** — `db.sum_value` cross-checked to agree with both raw forms in the same run (assertion inside the benchmark itself) |
| **Write speed** (synchronous FULL vs NORMAL) | 1.45 ms/write (FULL) vs 0.31 ms/write (NORMAL) — 4.7× (rule 4) | **1.4221 ms/write (FULL) vs 0.2490 ms/write (NORMAL) — 5.71×** (`I.`) | NORMAL ≤ 0.5 ms, ≥ 3× faster (AC3) | **PASS** |
| Typed-log pipeline query count | 33 queries (rule 6, explicitly deferred/lower-priority, not a Stage target) | **33 queries** (`F.`) | unchanged — not in scope | as expected, unchanged |
| `provider.for_user` cold/warm | 1 query cold / 0 warm (rule 8, "healthy, do not touch") | **1 query cold** (`G.`) | unchanged — not in scope | as expected, unchanged |

All three floors named in the closeout ask (idle-tick queries, day-filter speed, write speed) hold at end-state, with margin beyond the original targets. Run-to-run variance vs. Stage-1's own numbers is normal measurement noise (same direction, same order of magnitude, both comfortably past their floors).

## Failures (if any)

None.

## Regressions detected

None.

## Recommendation

**Ready to ship — v1.9.4, and close the refactor initiative.**

All three Stage-4 ACs (AC11, AC12, AC13) pass under independent re-derivation, not just re-confirmation of Luna's claims — the i18n key-set diff, the doubles byte-diff, the collect-only count-preservation check, the isolation probe, and the double-run wobble check were all built and run fresh by Vera rather than trusted from IMPL-refactor-s4.md's tables. The one flagged nuance (`FakeScheduler.add_job` gaining an explicit `kwargs` capture) is additive, not a behavior change, and does not affect AC12's pass status. The rule-14 SPEC prose fix matches TEST-refactor-s3.md's ground truth exactly, token for token. Both full-suite runs are clean at the exact expected count and runtime. The closeout benchmark confirms every Stage-1-era performance floor still holds at the final structure — nothing regressed across Stages 2–4's restructuring. Clear to hand back to Archi for the v1.9.4 release (commit + tag + PROGRESS.md update) and to close SPEC-REFACTOR.md as fully delivered.
