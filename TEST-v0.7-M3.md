# Test Report — v0.7.0 Multi-Habit Extensibility, Module M3 (Review)

Scope: `src/habit_assistant/core/review.py`, `tests/test_review.py`, plus a
NEW file `tests/test_v07_m3_review_extra.py`. Judged against SPEC-v0.7.md
AC15 and AC16 only. Per the task boundary, this report does **not** judge
the full suite, `TEST.md`, or any M1/M2-owned file — see "Audit of the 9
boundary failures" for how they were handled.

## Summary
- Total (M3 scope): 43 tests (19 in `test_review.py` + 8 new in
  `test_v07_m3_review_extra.py` + 16 in `test_habits.py` sanity)
- Passed: 43
- Failed: 0
- Status: **PASS**

## Test files

| Path | Tests added (by Vera) | Covers |
|---|---|---|
| `tests/test_review.py` | 0 (Luna's file — read/executed, not edited; Vera does not modify production or existing test files per role) | AC15 (7 narrative/delivery tests, 6 built-in math tests), AC16 (7 generic-type tests) |
| `tests/test_v07_m3_review_extra.py` (new) | 8 | AC15 (soft-delete exclusion for built-ins), AC16 (soft-delete exclusion for a generic habit, duration-streak edge cases, boolean done-days across a full week, two generic habits alongside all three built-ins at once) |
| `tests/test_habits.py` | 0 (shared-surface sanity check per task brief; not edited) | Sanity only — `HabitRegistry`/`Habit` construction, not an M3 AC |

## AC coverage

- **AC15** [→R16, AC7.1-review] → **PASS**. Default-config weekly review is byte-identical to v0.6.0 (stats math, labels, language).
  - `test_review.py::test_compute_weekly_stats_water_stretch_diary_math_matches_v060` → PASS
  - `test_review.py::test_compute_weekly_stats_stretch_streak_zero_when_last_day_has_no_stretch` → PASS
  - `test_review.py::test_compute_weekly_stats_empty_week_is_all_zero` → PASS
  - `test_review.py::test_compute_weekly_stats_water_goal_read_from_legacy_reminders_config` → PASS
  - `test_review.py::test_format_stats_summary_contains_expected_figures` → PASS
  - `test_review.py::test_run_weekly_review_*` (7 narrative/delivery tests) → PASS
  - `test_v07_m3_review_extra.py::test_soft_deleted_rows_excluded_from_weekly_review_stats` → PASS (independent re-verification of the scenario the M1-owned boundary test also covers, run against M3's correct signature — see audit below)
  - **Independent byte-identical reconstruction (see "AC15 verification detail" below)** → PASS

- **AC16** [→R16, AC7.5-review] → **PASS**. Generic per-habit aggregation — numeric total/avg/goal-adherence, duration count+streak, text count, boolean done-days; config-added habit needs zero code change; pre-v0.7 `habit_type IS NULL` rows aggregate correctly.
  - `test_review.py::test_generic_numeric_with_goal_gets_total_avg_and_goal_adherence` → PASS
  - `test_review.py::test_generic_numeric_without_goal_gets_total_avg_only_no_per_day_lines` → PASS
  - `test_review.py::test_generic_duration_gets_count_and_streak` → PASS
  - `test_review.py::test_generic_text_gets_entry_count` → PASS
  - `test_review.py::test_generic_boolean_gets_done_day_count_not_raw_row_count` → PASS
  - `test_review.py::test_config_added_habit_appears_alongside_builtins_with_no_code_change` → PASS
  - `test_review.py::test_pre_v070_rows_with_null_habit_type_aggregate_correctly` → PASS
  - `test_v07_m3_review_extra.py::test_soft_deleted_rows_excluded_for_a_generic_habit` → PASS
  - `test_v07_m3_review_extra.py::test_duration_streak_all_seven_days_active` → PASS
  - `test_v07_m3_review_extra.py::test_duration_streak_breaks_on_first_gap_from_end_date` → PASS
  - `test_v07_m3_review_extra.py::test_duration_streak_zero_when_end_date_itself_has_no_session` → PASS
  - `test_v07_m3_review_extra.py::test_duration_streak_multiple_sessions_same_day_counts_as_one_streak_day` → PASS
  - `test_v07_m3_review_extra.py::test_boolean_done_days_across_week_with_multiple_logs_on_several_days` → PASS
  - `test_v07_m3_review_extra.py::test_two_generic_habits_alongside_all_three_builtins_render_independently` → PASS

## AC15 verification detail — byte-identical, not construction-trusted

Per the task instructions, IMPL.md's claim that built-ins "reuse the v0.6.0
catalog entries verbatim" was **not** taken on trust. I pulled
`git show v0.6.0:src/habit_assistant/core/review.py` into an isolated temp
module (`review_v060.py`, loaded via `importlib.util`) and ran it side by
side with the current `core/review.py`, against two identically seeded
databases (water every day / stretch on the last 3 days / diary on days
-6 and 0 — same fixture plan as the seeded-week tests), for both the
default config (resolves Thai) and a forced-English config:

- `compute_weekly_stats` (old 3-arg vs new 4-arg) → `format_stats_summary`
  output compared **character-for-character**, `en` and `th`: **MATCH** in
  both cases.
- Full `run_weekly_review(...)` output (header + stats block + narrative,
  same `FakeLLM` narrative string fed to both) compared
  character-for-character, `en` and `th`: **MATCH** in both cases.

Script: `ac15_byte_identical.py` (scratchpad, not committed to the repo).
Result:
```
=== default config (resolves th) ===
  summary_en: MATCH
  summary_th: MATCH
  full: MATCH
=== forced-english config ===
  summary_en: MATCH
  summary_th: MATCH
  full: MATCH

ALL MATCH
```
This directly verifies the water per-day %/total math, the stretch
count+streak math, the diary count, and every label/unit string in both
languages are unchanged from v0.6.0 — not merely "the same catalog call is
made," but that the two implementations produce identical output text
given identical input.

## Audit of Luna's 9 attributed boundary failures

Luna's IMPL.md claims all 9 non-M3 failures are mechanical
"old 3-arg/1-arg call shape vs new registry-required shape" breaks, not M3
defects, caused by `compute_weekly_stats`/`format_stats_summary`/
`run_weekly_review`'s signature flip landing before the calling files
(owned by shared-surface/M1, frozen for her) were updated. I ran all 9
directly and confirmed the failure mode is **exactly** and **only**
`TypeError: missing 1 required positional argument` (`registry` or
`end_date`/`llm` shifting position) — no assertion failure, no wrong
value, no exception from inside `core/review.py`'s logic:

- `tests/test_db.py` (6): `test_compute_weekly_stats_totals_and_adherence`, `test_compute_weekly_stats_current_streak`, `test_compute_weekly_stats_streak_zero_when_last_day_has_no_stretch`, `test_compute_weekly_stats_empty_week_is_all_zero`, `test_format_stats_summary_contains_expected_figures`, `test_compute_weekly_stats_respects_custom_goal` — all `TypeError: compute_weekly_stats() missing 1 required positional argument: 'end_date'` (calling with the old 3-arg `(db, config, end_date)` shape; `registry` now sits where `end_date` used to be positionally, so the 3rd arg lands as `registry` and the call is short one arg).
- `tests/test_commands.py::test_soft_deleted_rows_excluded_from_weekly_review_stats` (1): same `TypeError`, old 3-arg call.
- `tests/test_v060_bilingual_gaps.py` (2): `test_weekly_review_system_prompt_carries_thai_directive_by_default`, `test_weekly_review_system_prompt_carries_english_directive_when_forced` — `TypeError: run_weekly_review() missing 1 required positional argument: 'llm'` (old 3-arg `(db, config, llm)` call; `registry` now occupies the `llm` position).

**Verdict: confirmed integration boundary breaks, not M3 defects.** The
new signatures (`compute_weekly_stats(db, config, registry, end_date)`,
`format_stats_summary(stats, registry, lang="en")`,
`run_weekly_review(db, config, registry, llm, today=None)`) are exactly
what SPEC-v0.7.md §5's frozen M3 contract requires; the failures are 100%
in caller files M3 was explicitly told not to touch
(`test_db.py`/`test_commands.py` are shared/M1-owned; `test_v060_bilingual_gaps.py`
is unowned-but-off-limits per the task's file-scope restriction). All 6
`test_db.py` cases and the `test_commands.py` case have equivalent,
passing, signature-correct coverage in this Vera pass
(`test_v07_m3_review_extra.py::test_soft_deleted_rows_excluded_from_weekly_review_stats`
covers the `test_commands.py` scenario directly; the `test_db.py` math
cases are superseded 1:1 by `test_review.py`'s own math tests per Luna's
IMPL.md rename table, which I independently re-verified above). I did not
edit any of the 9 broken tests — that is integration Vera's job once
`main.py`'s call site and the sibling test files are updated.

## Failures (if any)

None in M3 scope.

## Regressions detected

None. `test_review.py` (19/19) and `test_habits.py` (16/16, shared-surface
sanity) both pass cleanly; no test that was passing before is now failing
within M3's scope.

## Design-choice notes carried forward from IMPL.md (not defects)

Luna flagged three under-constrained rendering choices in IMPL.md's
"Known limitations." I checked each against SPEC-v0.7.md and consider all
three reasonable, spec-consistent interpretations, not gaps:

1. **Numeric-without-goal rendering** (total/avg line only, no per-day
   breakdown) — consistent with §3.2's `steps` example
   (`✅ 8000 steps logged today`, no percentage) and the only catalog
   entries the shared surface shipped (`stats_generic_numeric_total` has
   no goal/pct fields). Verified via
   `test_generic_numeric_without_goal_gets_total_avg_only_no_per_day_lines`.
2. **Boolean "done-days" reusing `stats_generic_count_summary`** — AC16
   only specifies the semantics (done-day count, not raw row count), not
   a dedicated template; no catalog entry exists for boolean specifically,
   and reusing the count-summary template is a faithful rendering of "a
   count." Verified via done-day math tests, including my own
   multi-day/multi-log extension.
3. **Water's goal sourced from `config.reminders.water.goal_ml`** rather
   than `registry.get("water").goal`** — this is required for AC15's
   byte-identical guarantee to hold when a deployment's legacy
   `[reminders.water]` config differs from `[[habits]]`'s `goal` field;
   confirmed correct by the byte-identical reconstruction above (which
   uses the real v0.6.0 code path reading the same legacy field).

None of these need escalation to Archi or Sophia; they are implementation
decisions consistent with the spec's own text and available catalog
surface.

## Items for the integration Vera

1. **Wire the 9 boundary-broken tests** once `main.py`'s `run_weekly_review`
   call site and `test_db.py`/`test_commands.py`/`test_v060_bilingual_gaps.py`
   are updated to the new signatures — this is SPEC-v0.7.md §11's own
   "integration order step 1." All 9 are one-line mechanical fixes
   (thread `registry` through); I independently confirmed the failure
   mode is purely the missing argument, nothing deeper.
2. **AC17** (composite byte-identical across confirmations + reminders +
   review) needs the real `parse_message`/`schedule_reminders`/
   `run_weekly_review` wired into `main.py` together — M3's piece of that
   (the review call) is verified ready (byte-identical, both languages,
   both stats-only and full-narrative forms).
3. **AC11** (end-to-end `sleep` habit via `"นอน 7 ชม."`) — M3's review
   rendering of a config-added numeric+goal habit is verified
   (`test_config_added_habit_appears_alongside_builtins_with_no_code_change`
   plus my own two-generic-habits test); integration still needs to drive
   it through the real parser end-to-end, which is out of M3's scope.
4. Consider also exercising a **boolean** habit in the AC11 integration
   pass (SPEC-v0.7.md §11 integration step 3 already calls for this) —
   review-side boolean rendering is verified here, but no shipped/default
   habit is boolean, so integration is where it first meets the real
   parser.
5. Live Ollama narrative spot-check is still outstanding for the review
   path (Ollama unreachable in this environment, matching Luna's finding)
   — recommend a live smoke check once Ollama is reachable, not blocking.

## Recommendation

**Ready to ship (M3 scope).** AC15 and AC16 both PASS, including an
independent byte-identical reconstruction against the real v0.6.0 code
(not a trust-the-claim check) and 8 additional Vera-authored edge-case
tests beyond Luna's own suite, all passing on first run with no code
changes needed. The only failures in the M3-relevant surface (9 tests in
files M3 does not own) are confirmed mechanical integration-boundary
breaks, not defects in `core/review.py`. Hand to Archi for the
integration Vera pass (SPEC-v0.7.md §11 integration order).
