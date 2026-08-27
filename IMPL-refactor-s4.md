# Implementation — Refactor Stage 4: dead code + test health (v1.9.4)

## Files changed

| Path | Created/Modified | One-line description |
|---|---|---|
| `src/habit_assistant/core/i18n.py` | Modified | Removed the 3 dead catalog keys `dashboard_line_goal_weeks`/`dashboard_line_boolean_weeks`/`dashboard_line_count_weeks` (12 lines) — grep-confirmed zero references anywhere in `src/` or `tests/` before deletion. |
| `tests/conftest.py` | Modified | Added the shared `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler` trio (AC12) plus an autouse `_reset_shared_doubles` fixture that clears `FakeOllamaClient.responses`/`FakeScheduler.last_instance` after every test so the two class-level-state doubles can't leak across files. |
| `tests/test_checkins.py` | Modified | Local `FakeChannel(Channel)` replaced with `from conftest import RecordingChannel as FakeChannel`; dropped the now-unused `Channel`/`Awaitable`/`Callable` imports. |
| `tests/test_nudge.py` | Modified | Same swap as `test_checkins.py`. |
| `tests/test_reminders.py` | Modified | Same swap; kept the `Button` import (still used by a later `send_actionable` override in this file). |
| `tests/test_v16_integration.py` | Modified | Local `_FakeScheduler`/`_FakeOllamaClient` replaced with `from conftest import FakeScheduler as _FakeScheduler, FakeOllamaClient as _FakeOllamaClient`; updated the file's own "small local fakes" convention comment to note the shared-doubles carve-out. |
| `tests/test_v17_integration.py` | Modified | Same swap; dropped the now-unused `json` import (its only use was inside the deleted `_FakeOllamaClient.chat_json`). |
| `tests/test_announce.py` | Modified | 1 test (`test_ac21_db_read_error_on_the_gate_check_fails_open_and_still_sends`) converted from a manual `tempfile.TemporaryDirectory()` block to the `tmp_path` fixture (AC13). |
| `tests/test_discoverability.py` | Modified | 3 tests converted from `tempfile.TemporaryDirectory()` to `tmp_path` (AC13). |
| `tests/test_heatmap_gaps.py` | Modified | 1 test converted (AC13). |
| `tests/test_v07_m3_review_extra.py` | Modified | 1 test converted (AC13); also switched `Database(f"{td}/habits.db")` (string path) to `Database(tmp_path / "habits.db")` (Path object, matching every other `tmp_path`-based `Database(...)` call site in the suite). |
| `SPEC-REFACTOR.md` | Modified (doc-only, sanctioned by Archi's task item 4) | Corrected rule 14's "Order (27 branches)" prose to match the real, verified dispatch order (Vera's `TEST-refactor-s3.md` finding — see "Iteration log"). |

No production files besides `core/i18n.py` were touched (0 lines changed in `main.py`, `commands.py`, `db.py`, `routing.py`, or any other `core/`/`storage/`/`channels/` module) — matching the task's exit bar ("no production-code edits expected at all except the 3 i18n key deletions").

## How it works

`tests/conftest.py` now defines three plain classes — `RecordingChannel(Channel)`, `FakeOllamaClient`, `FakeScheduler` — each copied verbatim (byte-for-byte body) from the most common "vanilla" fake already used across the suite, so no test file's *observable* fake behavior changed, only where the class definition lives. Because `tests/` has no `__init__.py`, pytest's default import mode puts each test module's own directory on `sys.path`, which is the same mechanism that makes `conftest.py` itself auto-discoverable — so any test file can do `from conftest import RecordingChannel, FakeOllamaClient, FakeScheduler` (aliased to the file's existing local name, e.g. `as FakeChannel` / `as _FakeScheduler`) with zero other plumbing. A migrated file therefore changes in exactly two places: the import block gains one line, and the local `class Fake...:` block is deleted — every line that *uses* the fake (`FakeChannel()`, `_FakeScheduler.last_instance`, `_FakeOllamaClient.responses = [...]`, etc.) is untouched, because the alias makes the shared class answer to the same local name the file already used. `FakeScheduler`/`FakeOllamaClient` carry class-level mutable state (`last_instance`, `responses`), so a new autouse `_reset_shared_doubles` fixture in `conftest.py` clears both after every test — this is a no-op for every non-migrated file (which still has its own separate, untouched fake class) and only matters once two *different* migrated files share the same class object. The `tmp_path`-migrated tests dropped their local `tempfile.TemporaryDirectory()`/`Path` boilerplate and now take `tmp_path` as a normal pytest fixture parameter, same as every other test in the suite already does — same behavior (a fresh, auto-cleaned scratch directory per test), pytest-native lifecycle instead of a manual context manager.

## Smoke test done

- **Full suite, foreground** (`PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q`, no `-n`, no `uv`): **4484 passed, 1 skipped, 1 xfailed, 0 failed in 200.89s** — exact same total as the v1.9.3 baseline (4484/0/1/1xf), runtime not worse (baseline ~200s → 200.89s, within noise).
- **Per-file test-COUNT proof** (the task's own exit-bar requirement): ran `pytest --collect-only -q` on every touched test file, then `git stash`ed all Stage-4 changes, re-ran `--collect-only` against the pre-Stage-4 (HEAD) version of the same files, then `git stash pop`ped back. Every file's count is identical before/after — table below.
- **Targeted subset runs** after each edit batch (not just the final full-suite run): `tests/test_i18n.py tests/test_i18n_literals.py tests/test_dashboard.py tests/test_dashboard_gaps.py` (114 passed, after the i18n key deletion) → `tests/test_checkins.py tests/test_nudge.py tests/test_reminders.py` (122 passed, after the channel-double migration) → `tests/test_announce.py tests/test_discoverability.py tests/test_heatmap_gaps.py tests/test_v07_m3_review_extra.py` (178 passed, after the `tmp_path` migration) → `tests/test_v16_integration.py tests/test_v17_integration.py` (42 passed, after the scheduler/LLM-double migration).
- **Import-mechanism proof**: before touching any real file, wrote a throwaway `tests/_scratch_conftest_import_check.py` that did `from conftest import RecordingChannel, FakeOllamaClient, FakeScheduler` and exercised each class's basic shape; ran it standalone (3 passed), then deleted it — confirms the `sys.path` mechanism this migration depends on actually works in this repo before committing any of the 5 exemplar files to it.
- **Grep proof for AC11**: `grep -rn "dashboard_line_goal_weeks\|dashboard_line_boolean_weeks\|dashboard_line_count_weeks" src/ tests/` → zero hits anywhere (confirmed both before deletion, as the removal justification, and it's structurally impossible to have residual hits after — the only definition site is gone). `grep -rn "BUILTIN_IDS" src/` → only `core/habits.py:31` (definition) and `core/reminders.py:74,390` (legitimate use); **not** in `main.py` — confirms the other half of AC11 (dropping the unused `main.py:66` import) was already done in Stage 2's full `main.py` rewrite (2523 → 147 lines), as PROGRESS.md's Stage-3 note flagged ("BUILTIN_IDS gone").

## Doubles-migration table (AC12)

| File | Doubles replaced | Test count before → after |
|---|---|---|
| `tests/test_checkins.py` | `FakeChannel(Channel)` → `conftest.RecordingChannel` | 64 → 64 |
| `tests/test_nudge.py` | `FakeChannel(Channel)` → `conftest.RecordingChannel` | 34 → 34 |
| `tests/test_reminders.py` | `FakeChannel(Channel)` → `conftest.RecordingChannel` | 24 → 24 |
| `tests/test_v16_integration.py` | `_FakeScheduler` + `_FakeOllamaClient` → `conftest.FakeScheduler` + `conftest.FakeOllamaClient` | 33 → 33 |
| `tests/test_v17_integration.py` | `_FakeScheduler` + `_FakeOllamaClient` → `conftest.FakeScheduler` + `conftest.FakeOllamaClient` | 9 → 9 |

**tmp_path migration table (AC13):**

| File | `tempfile.TemporaryDirectory()` occurrences converted | Test count before → after |
|---|---|---|
| `tests/test_announce.py` | 1 | 21 → 21 |
| `tests/test_discoverability.py` | 3 | 83 → 83 |
| `tests/test_heatmap_gaps.py` | 1 | 66 → 66 |
| `tests/test_v07_m3_review_extra.py` | 1 | 8 → 8 |

No test's assertions were touched in any of the 9 migrated files — only fake-class boilerplate (doubles table) or temp-directory acquisition (tmp_path table) moved; every `assert` line is byte-identical to `HEAD`, confirmed by `git diff` (the diffs above are visible in each file's own diff and contain no `assert`/`Command(...)`/i18n-string line changes).

## Maps to acceptance criteria

- **AC11** (3 dead `dashboard_line_*_weeks` keys + unused `BUILTIN_IDS` import removed; catalog tests still pass) → `core/i18n.py` (keys removed, this stage); `main.py:66`'s `BUILTIN_IDS` import was already removed in Stage 2's rewrite (verified via grep, not re-done — nothing left to do for that half). `tests/test_i18n_literals.py`/`tests/test_i18n.py` (72+42=114 combined with dashboard tests, all pass) confirm the catalog stays internally consistent post-deletion.
- **AC12** (`tests/conftest.py` provides shared `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler`; migrated tests still assert on identical surfaces) → implemented in `tests/conftest.py`; migrated in 5 exemplar files per the doubles table above. Per SPEC-REFACTOR.md §10 ("Out of scope: ...beyond the three shared doubles named in AC12 — the exotic scripted/raising variants stay per-file"), this stage provides the shared trio and migrates a representative, non-exotic subset (plain recording channels + plain queueing scheduler/LLM fakes) — see "Known limitations" for what's deliberately deferred.
- **AC13** (4 fixed-Temp-path test files use `tmp_path`) → all 4 named files (`test_announce.py`, `test_discoverability.py`, `test_heatmap_gaps.py`, `test_v07_m3_review_extra.py`) converted, 6 total call sites. Zero `tempfile`/manual-`Path`-temp-dir usage remains in any of the 4 (grep-confirmed).

## Known limitations

- **AC12 migration scope is intentionally partial**, per SPEC-REFACTOR.md's own out-of-scope line. The audit counted 82 channel / 35 LLM / 29 scheduler+db hand-rolled doubles across ~66 files with a `*Channel` fake and ~22 files with a `_FakeScheduler`/`_FakeOllamaClient` pair; this stage migrated 5 exemplar files (3 channel, 2 scheduler+LLM) demonstrating the shared trio works end-to-end (byte-identical behavior, identical test counts, full suite green) and provides the shared classes for any future file to adopt. The remaining files were deliberately **not** touched in this stage: migrating the full census in one pass would mean editing dozens of large integration/release-gate files under a strict byte-identical/zero-test-count-drift gate with a 3-round loop cap — a much larger, separately-riskable body of work than "final stage, behavior-preserving dead code + test health" calls for. This is a documented follow-up, not an oversight: any file with a plain (non-scripted, non-raising) `FakeChannel`/`_FakeScheduler`/`_FakeOllamaClient` can be migrated later by the same mechanical two-step edit (add the `conftest` import, delete the local class) shown in the 5 exemplars here.
- **`db.py` god-object (OQ4)**: out of Stage 4's assigned scope. SPEC-REFACTOR.md §9 states "*Default:* document-and-defer (rule 13); not in any stage unless the user asks" and §11's Stage-4 row lists only `core/i18n.py`, large-file imports, `tests/conftest.py`, and the 4 fixed-path test files as owned files — rule 13's own prose in the spec already **is** the documentation of the defer decision, so no further doc note was added.
- The `RecordingChannel`/`FakeOllamaClient`/`FakeScheduler` reset fixture (`_reset_shared_doubles`) is a new autouse fixture that runs for every test in the suite (not just migrated files) — verified to be a cheap no-op (two attribute assignments) for the ~4470 non-migrated tests; the full-suite runtime (200.89s vs. ~200s baseline) confirms no measurable cost.

## Iteration log

**Round 1: none needed — all changes passed on first full-suite run.** No Vera round-trips occurred in this stage; the report below documents the one sanctioned pre-existing finding this task explicitly asked to be resolved:

1. **SPEC-REFACTOR.md rule 14 prose fix (Archi task item 4, doc-only).** Vera's `TEST-refactor-s3.md` ("Findings" #1) found that rule 14's "Order (27 branches)" list was an inaccurate paraphrase of the real dispatch order: it omitted `pause`/`resume` (added in v1.9.0, the spec's own stated target version) and placed `query` before `help`/`habits` instead of last — directly contradicting the spec's own invariant (iii) two sentences later ("`query`... must stay last"). Vera confirmed Luna's Stage-3 `_MATCHERS` table matches the **real, pre-Stage-3 code** exactly (not the spec's flawed summary) — so this was always a documentation bug, never a code defect. Fixed in `SPEC-REFACTOR.md` rule 14: the order now reads `...routine → cadence → pause → resume → help → habits → query`, with a dated note explaining the correction and citing Vera's finding.
