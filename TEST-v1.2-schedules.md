# Test Report — v1.2.0 Multi-user support, module `schedules`

## Summary
- Total (`tests/test_schedules.py`): **94 tests** (56 from Luna + 38 new, Vera additions)
- Passed: 94
- Failed: 0
- Full repo suite: **1152 passed, 1 skipped** (baseline 1114 passed / 1 skipped + 38 new = 1152; the skip is the pre-existing `tests/test_channels.py:232` architectural-boundary check, unrelated to this pass)
- Status: **PASS**

No production code was modified. Only `tests/test_schedules.py` was edited (2 import-line additions + 38 new test functions appended). No test used `data/habits.db` or touched the live Task Scheduler service — every test runs against a `tmp_path`-only SQLite file (the `db` fixture). The LLM is never invoked anywhere in this module (deterministic path).

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_schedules.py` | 38 new (94 total) | AC-S2, AC-S3, AC-S5 (module-owned); sanity on AC-S4; supplementary AC-S6/AC-U-ISO interplay and cross-module dispatch-precedence checks (not owned by this module, exercised for confidence) |

## AC coverage

| AC | Test(s) | Status |
|---|---|---|
| **AC-S2** (per-user set: fires at new times, not old config times; other user/habit unaffected) | Luna: `test_ac_s2_set_writes_the_override_and_replies_remind_set`, `test_ac_s2_custom_time_fires_and_old_config_time_does_not`, `test_ac_s2_other_user_without_an_override_still_fires_at_config_times`, `test_ac_s2_other_habit_is_unaffected_by_waters_override`. Vera: `test_isolation_matrix_set_and_off_never_cross_user_or_habit_boundaries`, `test_setting_new_times_replaces_previous_override_entirely` | **PASS** |
| **AC-S3** (show reports effective times + source; default reverts; off suppresses only that user) | Luna: `test_ac_s3_show_with_no_override_reports_default_source_and_config_times`, `test_ac_s3_show_after_set_reports_custom_source_and_times`, `test_ac_s3_show_after_off_reports_off`, `test_ac_s3_default_clears_override_and_reverts_effective_times`, `test_ac_s3_default_synonyms_all_clear_the_override` (x3), `test_ac_s3_off_suppresses_reminders_for_that_user_only`. Vera: folded into `test_isolation_matrix_set_and_off_never_cross_user_or_habit_boundaries` (default-clear leg) | **PASS** |
| **AC-S5** (validation: non-`HH:MM` rejected with `remind_invalid_time`, no write; dedupe; ≤24 cap) | Luna: `test_ac_s5_invalid_time_token_rejected_with_no_write` (x7 tokens), `test_ac_s5_one_bad_token_rejects_the_whole_set_no_partial_write`, `test_ac_s5_duplicate_times_are_deduped`, `test_ac_s5_cap_boundary_24_times_is_accepted`, `test_ac_s5_cap_exceeded_25_times_is_rejected_with_no_write`. Vera: `test_ac_s5_additional_invalid_time_literals_rejected_with_no_write` (x3: `"25:00"`, `"7:5"`, `"07:60"`), `test_ac_s5_invalid_token_leading_still_rejects_whole_set`, `test_ac_s5_all_tokens_identical_dedupes_to_a_single_time`, `test_ac_s5_cap_applies_after_dedupe_not_to_raw_token_count` | **PASS** |
| **AC-S4** (no restart / scheduler rebuild — owned by shared-surface, sanity here) | Luna: `test_ac_s4_remind_write_is_picked_up_by_the_next_tick_with_no_scheduler_rebuild` (proven by never constructing a scheduler object; write path is entirely this module's, read path is the shared `run_due_reminders`) | **PASS (sanity)** |
| AC-S6 (custom time still honors quiet-hours/goal-met + snooze — owned by shared-surface/integration) | Vera (supplementary): `test_ac_s6_custom_time_reminder_is_suppressed_during_the_users_own_quiet_hours`, `test_ac_s6_custom_time_reminder_fires_outside_the_users_quiet_hours`, `test_ac_s6_custom_time_reminder_still_honors_goal_met_skip_per_user`, `test_ac_s6_snooze_after_a_custom_time_reminder_targets_the_asking_user_only` | **PASS (supplementary — not this module's gate)** |
| AC-U-ISO-adjacent isolation on `user_reminder_times` (owned by shared-surface, exercised here) | Vera: `test_isolation_matrix_set_and_off_never_cross_user_or_habit_boundaries` | **PASS (supplementary)** |

Every AC this module owns per `SPEC-v1.2.md` §11 (AC-S2, AC-S3, AC-S5) is covered and green. AC-S4 (shared-surface-owned, proven against the real tick) and AC-S6/isolation (shared-surface/integration-owned) are exercised for confidence but do not gate this module's PASS.

## Adversarial verification performed (beyond Luna's own tests)

1. **Isolation matrix.** `test_isolation_matrix_set_and_off_never_cross_user_or_habit_boundaries` seeds owner-water=custom, owner-stretch=off, user-b-water=custom(different time), user-b-stretch=untouched, then clears only owner-water back to default and re-checks the full 2×2 matrix plus a real tick at three separate clock times. Confirms `set`/`off`/`default` are scoped strictly to `(user_id, habit_id)` with zero cross-talk, and that `effective_reminder_times` for an entirely untouched habit still falls back to config for both users.
2. **Extra HH:MM validation literals.** The exact tokens named in the dispatch (`"25:00"`, `"7:5"`, `"07:60"`) beyond Luna's own set (`"25:99"`, `"12:60"`, `"8:00"`), plus leading-position (not just trailing) invalid-token whole-set rejection.
3. **Cap-vs-dedupe ordering.** `test_ac_s5_cap_applies_after_dedupe_not_to_raw_token_count` sends 30 raw tokens that collapse to 10 distinct times — confirms the ≤24 cap is enforced on the **deduped** set (matching R-S5's stated order "validate → de-dupe → cap"), not the raw token count. This was untested by Luna and is a real implementation-order question `_execute_set` could have gotten backwards.
4. **Replace-not-accumulate semantics.** `test_setting_new_times_replaces_previous_override_entirely` confirms a second `/remind` set fully replaces the first (delete-then-insert), not merges.
5. **Wider adversarial corpus.** Prefix look-alikes (`"/reminder water 08:00"`, `"/remindful"`) that share a "remind" substring but must not match because no whitespace immediately follows the literal trigger; bare `"remind"`/`"remind water 08:00"` (no slash, no Thai trigger — English has no bare-word alias); Thai glued/mid-sentence mentions. All 8 confirmed non-dispatching.
6. **Whitespace tolerance.** `เตือน   น้ำ    08:00` (multiple internal spaces) still dispatches correctly — the false-positive mitigation isn't over-fitted to exactly one space character.
7. **AC-S6 interplay through the real tick/`send_reminder`, not mocks.** Discovered along the way: `send_reminder`'s quiet-hours check reads the **real** `datetime.now(tz)` (`core/reminders.py`), not the `clock` callable passed to `run_due_reminders` (which only drives the "which HH:MM is due" check). A first draft of the quiet-hours interplay test failed for exactly this reason. Not a defect — it's the same pattern already established in `tests/test_v09_gaps.py`'s own `_FixedDatetime`/`_freeze_reminders_clock` monkeypatch technique, which I replicated locally in `test_schedules.py`. Flagging it here because it's a real testability gotcha worth knowing about, not a schedules-module bug (the shared surface owns `send_reminder`/`run_due_reminders`, unmodified by this module).
8. **Goal-met skip on a custom time, per user.** `test_ac_s6_custom_time_reminder_still_honors_goal_met_skip_per_user` — owner's custom-time water reminder is suppressed when today's total already meets the goal; user-b's own (unrelated) custom time for the same habit is unaffected.
9. **Snooze targeting after a custom-time fire.** `test_ac_s6_snooze_after_a_custom_time_reminder_targets_the_asking_user_only` mirrors `main.py:_execute_snooze`'s actual mechanism (re-invoking `send_reminder` for `state.last_habit_id[asking_user]`) and confirms it's addressed only to the asking user.
10. **Cross-module dispatch-table precedence.** `test_combined_dispatch_table_has_no_precedence_conflicts` (16-row parametrized table) and `test_remind_trigger_never_shadowed_by_any_other_v12_module` directly exercise `commands.dispatch()` against representative triggers from every landed v1.2 module (`schedules`, `access`, `preferences`) plus the pre-existing v1.1 kinds (`undo`, `target`, `help`, `habits`, `snooze`) in the single combined `core/commands.py`. No precedence conflicts found — every trigger dispatches as its own kind; `/remind`/`เตือน` is never shadowed by, and never shadows, any other module's trigger. This directly confirms the "disjoint trigger text" claim made throughout `IMPL-v1.2-schedules.md`/`core/commands.py`'s own docstrings, rather than taking it on faith.
11. **The `user_id` kwarg deviation and the mandatory-space Thai-alias mitigation.** Reviewed both by code-reading and by test: `execute_remind`'s `user_id` kwarg is required by every DB read/write inside it and is exercised on every single test in this file (there is no code path that could silently ignore it) — the same pattern as the already-landed `execute_target`, not a new precedent. The mandatory `\s+` after `เตือน` is what the wider adversarial corpus (point 5 above) and the original corpus both stress — confirmed sound, not papering over a defect: a normal Thai sentence beginning with "เตือน" glued to more text can never match, and an explicit "เตือน &lt;habit&gt; ..." with a space always does.

## Failures (if any)

None.

## Regressions detected

None. Full suite: 1152 passed, 1 skipped (pre-existing skip, unrelated). Baseline stated in the dispatch was 1114 passed / 1 skipped; the +38 delta is exactly the new tests added this pass.

## Recommendation

**Ready to ship** for the `schedules` module's own scope (AC-S2, AC-S3, AC-S5 — all PASS, no gaps found). AC-S4's shared-surface half and AC-S6/isolation (both owned by the shared-surface/integration pass per `SPEC-v1.2.md` §11) are exercised here for extra confidence and also PASS, but remain gated for final sign-off by the integration pass described in §11 step 2–3 (wiring `access.handle_gate` + `/remind` routing into `main.py`, per `IMPL-v1.2-schedules.md`'s "Known limitations" — `main.py` is not yet wired to call `schedules.execute_remind`, which is explicitly out of this module's scope and Luna's own stated deferral).
