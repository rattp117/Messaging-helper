# Test Report — v1.9.0 `cadence` module (M1, weekly-cadence goals)

## Summary
- Total (cadence-scope subset): **85 tests** — 49 (Luna's `tests/test_cadence.py`) + 36 (mine, `tests/test_v19_cadence_gaps.py`)
- Passed: 85 / Failed: 0
- Full suite at time of this report: **4181 passed, 3 failed, 1 skipped, 1 xfailed**
- The 3 failures are in `tests/test_v19_pause_gaps.py` — a file **not owned by `cadence`**, written by a concurrent pause-track Vera mid-session (see "Tree state" below). Not investigated further here; routing note included for Archi.
- **Status: PASS** for everything in `cadence`'s scope (AC7, AC8, AC11, AC12 fully; AC9/AC10 pass at the module level — see "Deferred slices").

---

## PRIORITY 1 — interaction root-cause (`test_pause.py` 2-failure report)

**Verdict: (c) something else — a transient parallel-file-edit snapshot artifact, not a real dispatch collision. Not reproducible in the current tree.**

**What I checked:**
1. `tests/test_pause.py` in isolation: **57/57 passed** (matches Luna-pause's own report).
2. Full suite in the current tree: **0 failures in `test_pause.py`**, `test_commands.py::test_reserved_trigger_words_covers_every_real_command_stem` also green. Cadence's IMPL.md reported 2 `test_pause.py` failures + the reserved-word failure at her checkpoint; pause's own IMPL.md (run later, after all four v1.9 modules had landed) already saw only the reserved-word failure (0 `test_pause.py` failures); my run now sees 0 failures of either kind. The failure count monotonically dropped across the three checkpoints as the parallel Lunas finished landing disjoint edits to the same shared files (`storage/db.py`, `core/commands.py`) — consistent with a filesystem race during concurrent edits, not a logic bug that was later "fixed."
3. **The two failing tests themselves are not dispatch-shape tests.** `test_re_pausing_the_same_habit_extends_replaces_not_stacks` and `test_pausing_all_then_a_different_habit_specifically_coexists` (`tests/test_pause.py:264,282`) exercise `/pause gym 5d`-style **slash-form** commands only — no Thai alias, no `_match_query`/`_match_cadence` interaction possible. They assert on `db.active_pauses()` row counts and `execute_pause`'s replace-not-stack semantics. Nothing about cadence's `commands.py` edits (which only touch `_match_cadence*`, inserted and returning `None` for any `/pause ...` input) can plausibly change that logic.
4. **Confirmed no lexical collision exists between cadence's and pause/resume's Thai triggers**, which was the only plausible mechanism the task raised:
   - `_match_cadence_nl`'s regex (`core/commands.py:1885`) is `^(?:กี่ครั้งต่อสัปดาห์|ต่อสัปดาห์)\s*(?P<habit>...)...` — an anchored **literal-string** match requiring `สัปดาห์` immediately (or after whitespace) following `ต่อ`.
   - `_match_resume_th`'s regex (`core/commands.py:1969`) is `^(?:กลับมา|ต่อ)\s+(?P<rest>\S.*)$` — requires **whitespace immediately after** `ต่อ`.
   - These are lexically disjoint by construction: `ต่อสัปดาห์...` (no space after ต่อ) can never satisfy `_RESUME_TH_RE`'s `\s+`, and `ต่อ <space> <token>` can never satisfy `_match_cadence_nl`'s literal `ต่อสัปดาห์` string. Proven directly in `tests/test_v19_cadence_gaps.py::test_tor_resume_alias_vs_tor_sapda_cadence_alias_never_collide`.
   - `dispatch()`'s call order (`core/commands.py:2306-2332`) checks `cadence` before `pause`/`resume`, but since the two never match the same string, order is moot here — it only matters for cadence-vs-`_match_query` (the documented, correctly-handled `กี่` collision).
5. `storage/db.py` region check: `active_pauses`(866) → `insert_pause`(891) → `clear_pauses`(904) [pause's write region] → `set_cadence`(933) → `clear_cadence`(946) [cadence's write region] — single definitions each, correctly ordered, no duplication or corruption in the current tree.

**Conclusion:** classify as **(c)** — the 2 `test_pause.py` failures cadence's Luna observed were a snapshot of pause's own write region (`db.insert_pause`/`clear_pauses`/`execute_pause`'s replace semantics) mid-edit at the exact moment she ran her full-suite check, an artifact of four Lunas concurrently editing the same shared files on disk in parallel (SPEC-v1.9.md §11's own parallel-module design). It resolved itself once pause's Luna finished; no code fix was needed and none should be attributed to cadence. **No action needed for cadence's Luna.** If Archi wants extra assurance, a single authoritative full-suite run after all four modules are frozen (which this report's run represents) is the right gate — not any individual Luna's mid-flight checkpoint.

---

## Tree state (parallel Veras)

Confirmed via `git status` that `tests/test_v19_pause_gaps.py` appeared as a new untracked file during this session (not present at the start) — a concurrent pause-track Vera writing her own adversarial gaps file. Its 3 current failures (`TestSemanticEdges::test_early_resume_before_natural_expiry_matches_natural_expiry_result`, `::test_resume_habit_reply_is_not_misleading_when_all_habits_pause_still_covers_it`, `::test_resume_habit_reply_th_is_not_misleading_when_all_habits_pause_still_covers_it`) are entirely about `/resume` reply wording semantics — no cadence code path involved, no overlap with any file `cadence` owns. Flagging for Archi to route to the pause track; **not investigated further here** (out of this dispatch's scope, and that Vera's own file is presumably still being iterated on).

`tests/test_v19_grace_gaps.py` and `tests/test_v19_shared_surface.py` were also present (grace track + the shared-surface Vera pass) and are fully green.

---

## Test files

| Path | Tests | Covers |
|---|---|---|
| `tests/test_cadence.py` (Luna's) | 49 | AC7, AC8, AC9 (numeric), AC10 (numeric), AC11, AC12 |
| `tests/test_v19_cadence_gaps.py` (mine, new) | 36 | AC7 (N=1/N=7 boundaries), AC8 (further malformed shapes, boundaries), AC11 (year-boundary weekly walk), per-user isolation, non-boolean cadence types, `weekly_progress` once-per-day + backfill ordering, real cross-module interop (pause NEUTRAL week, grace exclusion), wider Thai collision corpus, `ต่อ`-vs-`ต่อสัปดาห์` disjointness proof, AC3-gate sanity from cadence's own angle |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC7 (`/cadence` set/off/validation/audit) | `test_cadence.py`'s execute_cadence section; `test_v19_cadence_gaps.py::test_cadence_n_equals_1_is_the_minimum_accepted_value`, `::test_cadence_n_equals_7_is_the_maximum_accepted_value`, `::test_cadence_n_equals_8_rejected_by_default_max_per_week_7`, `::test_off_is_the_documented_clear_word_and_a_second_off_stays_idempotent` | **PASS** |
| AC8 (`/addhabit cadence=<N>w` atomic) | `test_cadence.py`'s addhabit section; `test_v19_cadence_gaps.py::test_addhabit_further_malformed_cadence_shapes_create_neither[3d/w3/3.5w/-3w/3.0w/3 w/3ww]`, `::test_addhabit_cadence_boundary_values_1w_and_7w_both_accepted` | **PASS** |
| AC9 (week-count streak + week wording everywhere) | `test_cadence.py::test_streak_unit_switches_on_cadence_presence`, `::test_records_stores_and_celebrates_a_week_count_for_a_cadence_habit`; `test_v19_cadence_gaps.py::test_weekly_walk_crosses_the_2026_w53_to_2027_w01_year_boundary`, `::test_cadence_on_a_duration_type_habit_streak_walk_uses_week_unit` | **PASS at module level** — numeric week-count + `streak_unit` switch fully verified across ordinary and year-boundary cases. Full AC text ("every renderer... uses week wording") requires wiring into `review.py`/`dashboard.py`/`main.py` milestone+daily-summary — **deferred to integration** (see below). |
| AC10 (`weekly_progress`/"X of N this week") | `test_cadence.py::test_weekly_progress_*`, `::test_cadence_status_line_*`; `test_v19_cadence_gaps.py::test_weekly_progress_counts_a_day_once_regardless_of_log_count`, `::test_weekly_progress_ignores_a_backfilled_log_landing_in_a_prior_week`, `::test_weekly_progress_counts_a_backfilled_log_that_lands_inside_the_current_week`, `::test_cadence_on_a_text_type_habit_is_accepted_and_counted` | **PASS at module level** — the pure computation is correct (once-per-day counting, backfill-order-independent, type-generic). Its actual appearance in `/habits`/`/dashboard` output — **deferred to integration** (see below). |
| AC11 (rest days don't break cadence streak) | `test_cadence.py::test_three_per_week_rest_days_do_not_break_the_streak`, `::test_a_genuinely_missed_week_breaks_the_cadence_streak`; `test_v19_cadence_gaps.py::test_weekly_walk_crosses_the_2026_w53_to_2027_w01_year_boundary`, `::test_cadence_week_made_unreachable_by_a_real_pause_row_is_neutral_not_missed` | **PASS** |
| AC12 (`records.update_on_log` week-count storage/celebration) | `test_cadence.py::test_records_stores_and_celebrates_a_week_count_for_a_cadence_habit`, `::test_records_best_day_best_week_unaffected_by_cadence` | **PASS** |

Cross-cutting checks not tied to a single owned AC but relevant to the interaction investigation and general robustness:
- `test_v19_cadence_gaps.py::test_cadence_on_another_users_custom_habit_is_unknown_habit_not_a_leak`, `::test_set_cadence_is_per_user_scoped_for_the_same_habit_id` — per-user isolation, PASS.
- `::test_grace_evaluate_grace_never_bridges_a_real_cadence_row` — cross-module confirmation of AC16 (grace-owned) from cadence's own write path, PASS.
- `::test_wider_thai_query_corpus_still_routes_to_query_not_cadence`, `::test_wider_thai_cadence_corpus_routes_correctly_for_other_habits`, `::test_tor_resume_alias_vs_tor_sapda_cadence_alias_never_collide`, `::test_zero_false_positive_cadence_aliases_on_ordinary_prose` — wider Thai adversarial corpus, zero false positives/negatives found, PASS.
- `::test_no_cadence_row_daily_walk_and_weekly_progress_are_byte_identical_gate`, `::test_dispatch_of_an_ordinary_log_message_is_unaffected_by_cadences_matchers` — AC3-gate sanity from cadence's own angle, PASS.

## Deferred slices (not gaps — documented scope boundary, confirmed by me independently)

I independently verified (not just trusting `IMPL-v1.9-cadence.md`'s claim) that `core/review.py`, `core/dashboard.py`, `core/discoverability.py`, and `src/habit_assistant/main.py` contain **zero** references to `streak_unit` or the cadence feature (`grep -l "streak_unit\|cadence"` on those four files returns only an unrelated English-word usage — "on the SAME minutely cadence" — in two `main.py` comments about the scheduler tick interval, not the feature). This confirms:

- **AC9's renderer-wording half** (milestone message, records celebration/view, dashboard row, weekly review, daily summary each selecting day/week wording via `streak_unit`) is genuinely not wired yet anywhere outside `core/cadence.py`'s own tested surface.
- **AC10's "/habits and dashboard render 'X of N this week'" half** is genuinely not wired into `discoverability.py`/`dashboard.py` yet.
- **AC30**'s `/help` + Telegram-menu listing of `/cadence` is also not yet present in `main.py`.

This matches SPEC-v1.9.md §11's own integration-order step 1 ("Append each module's pure formatter... into `review.py`/`dashboard.py`/`discoverability.py`") and §6's file-ownership table (those four files are listed only under "Integration (sequential, last)", never under module `cadence`'s owned files). **Not a defect in `cadence`'s work** — flagging so Archi schedules an integration-pass Vera to close AC9/AC10/AC30's full text once `main.py`/`review.py`/`dashboard.py`/`discoverability.py` are wired, and re-tests the milestone/daily-summary/dashboard/review wording end-to-end at that point.

## Failures (cadence scope)

None.

## Regressions detected (cadence scope)

None. Full suite run with `tests/test_v19_cadence_gaps.py` included: 4181 passed / 3 failed (all `tests/test_v19_pause_gaps.py`, not cadence-owned) / 1 skipped / 1 xfailed.

## Recommendation

**Ready to ship** for module `cadence`'s own scope (AC7, AC8, AC11, AC12 fully closed; AC9/AC10 numerically correct and closed at the module level, with the renderer-wiring half correctly and explicitly deferred to the integration pass per SPEC-v1.9.md §11 — not a `cadence`-owned gap).

**For Archi:**
1. PRIORITY 1 interaction closed: no action needed on `cadence`'s side; classify as a parallel-edit snapshot artifact, not a bug.
2. Route the 3 `tests/test_v19_pause_gaps.py` failures to the pause track (out of this dispatch's scope; that file is still being written/iterated by a concurrent Vera).
3. Schedule the integration pass (wiring `streak_unit`/`weekly_progress`/`cadence_status_line` into `review.py`/`dashboard.py`/`discoverability.py`/`main.py`) before declaring AC9/AC10/AC30 fully closed — cadence's own module-level work does not block that pass; it supplies everything the pass needs, already tested.
