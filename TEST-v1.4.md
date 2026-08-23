# Test Report — v1.4.0 `/history [N]` (personal entry statement)

## Summary
- Total (full suite): **1599 passed, 0 failed, 1 skipped**
- New this pass: **23 tests** (`tests/test_history_gaps.py`), all passing, added on top of Luna's own 65 (`tests/test_render_budget.py` 13 + `tests/test_history.py` 52)
- Regression baseline confirmed: **1576 passed / 0 failed / 1 skipped**, matching Luna's stated number exactly (own independent full run, no discrepancy)
- `tests/test_audit_view.py` run in isolation: **82 passed, unmodified** — byte-identical extraction confirmed (AC-3)
- **Status: PASS**

## Baseline verification
Ran the full suite myself before writing anything (per instructions, my observed number is baseline of record):
```
.venv\Scripts\python.exe -m pytest -q
1576 passed, 1 skipped, 40 warnings in 111.87s
```
Matches Luna's IMPL.md claim exactly (1511 + 65 = 1576). No discrepancy to investigate. Then added `tests/test_history_gaps.py` (23 tests) and reran the full suite:
```
1599 passed, 1 skipped, 40 warnings in 112.16s
```
1576 + 23 = 1599. Zero regressions, zero new failures. The one skip is pre-existing and unrelated to this feature (unchanged across both runs).

## Test files

| Path | Tests | Covers which ACs |
|---|---|---|
| `tests/test_render_budget.py` (Luna) | 13 | AC-3 |
| `tests/test_history.py` (Luna) | 52 | AC-1, AC-2, AC-4 – AC-14 |
| `tests/test_audit_view.py` (pre-existing, unmodified) | 82 | AC-3 (byte-identical regression guard) |
| `tests/test_history_gaps.py` (Vera, new) | 23 | AC-3, AC-6, AC-7, AC-9, AC-10, AC-11, AC-12, AC-14 (adversarial/edge supplements) |
| `tests/test_discoverability.py` (mechanical 8→9 fix, pre-existing) | — | AC-14 (exact 9-command menu set, both languages) |
| `tests/test_v12_integration.py` (mechanical 8→9 fix, pre-existing) | — | AC-14 |

### `tests/test_history_gaps.py` breakdown (23 new tests)

| Area | Tests | Notes |
|---|---|---|
| Extraction/footer contract (AC-3) | 2 | Footer containing `{}`/`{0}` renders literally; a pathologically huge footer renderer doesn't hang/crash (characterization note below) |
| U-ISO combined scenarios (AC-9/AC-10) | 2 | No fill-from-other-user when requester is exhausted; isolation holds with category filter + soft-delete combined |
| Matcher discipline (AC-4/AC-5/AC-6) | 8 | `/historys` no-match, trailing-garbage-ignored, negative-number-as-habit-token, `0` is a valid limit, huge-N doesn't overflow, 3 Thai-alias false-positive shapes (unregistered habit word, glued prefix, unanchored) |
| Rendering edge cases (AC-7/AC-12) | 4 | limit=0 renders empty; huge limit capped without crash; emoji/ZWJ/RTL-override doesn't crash; midnight-boundary formatting |
| Removed-habit handling (spec §9 risk note) | 2 | Unfiltered view falls back to generic description; filtering BY a removed habit reports friendly invalid-habit (not the historical rows) |
| Ordering/exclusion correctness (AC-2/AC-11) | 2 | Same-timestamp tie-break is deterministic; unparsed rows sandwiched between real ones leave the real ones intact |
| Integration/routing (AC-14) | 3 | Pending user gets the access gate, not a history reply; blocked user gets denied, not a history reply; `/history` produces a real reply through `handle_inbound_message` directly with a health-monitor double reporting Ollama down and a raising-LLM double |

## AC coverage

| AC | Description | Test(s) | Status |
|---|---|---|---|
| AC-1 | Additive/regression: no migration, 1511-baseline suite stays green | `test_no_migration_was_added_for_history`; full-suite run (1576→1599, 0 failed) | PASS |
| AC-2 | `recent_logs`: newest-first, includes soft-deleted, excludes unparsed, category filter, limit | `test_recent_logs_newest_first`, `::includes_soft_deleted_rows`, `::excludes_unparsed_even_without_a_category_filter`, `::honors_category_filter`, `::respects_limit`, `::scoped_to_user_id` (Luna) + `test_recent_logs_newest_first_stable_for_identical_timestamps`, `test_history_unparsed_rows_sandwiched_between_real_entries_leave_them_intact` (Vera) | PASS |
| AC-3 | Extract not copy: `audit_view` byte-identical, `history_view` reuses same helpers | `tests/test_audit_view.py` (82, unmodified) + `tests/test_render_budget.py` (13) + `test_fit_within_budget_footer_containing_format_braces_renders_literally`, `test_fit_within_budget_with_a_footer_renderer_that_itself_exceeds_budget` (Vera) | PASS (see characterization note below) |
| AC-4 | Dispatch: `/history`, `/history 10`, `/history water`, `/history water 10`, `ย้อนหลัง …` | `test_dispatch_recognizes_history_shape` (12 cases, Luna) + `test_dispatch_history_water_abc_ignores_unrecognized_trailing_token`, `test_dispatch_history_zero_is_a_well_formed_limit_not_dropped`, `test_dispatch_history_huge_n_parses_without_overflow` (Vera) | PASS |
| AC-5 | Adversarial corpus never matches; `ย้อนหลัง` doesn't collide with `ประวัติ` | `test_dispatch_adversarial_corpus_never_matches_history` (13 cases), `test_history_thai_alias_does_not_collide_with_audits_thai_alias` (Luna) + `test_dispatch_history_with_trailing_letters_does_not_match_history`, `test_dispatch_thai_alias_with_an_unregistered_habit_word_falls_through`, `test_dispatch_thai_alias_glued_prefix_does_not_match`, `test_dispatch_thai_alias_not_anchored_at_start_does_not_match` (Vera) | PASS |
| AC-6 | Invalid habit → friendly reply, no rows touched | `test_ac6_invalid_habit_returns_friendly_reply_and_touches_no_rows` (Luna) + `test_dispatch_history_negative_number_is_treated_as_an_unknown_habit_token`, `test_history_filter_by_a_habit_removed_from_the_registry_is_reported_invalid_not_crashed` (Vera) | PASS |
| AC-7 | Content + limits: ts/habit+value/quoted message, newest-first; explicit N; cap at 50 | `test_ac7_*` (3 tests, Luna) + `test_render_history_limit_zero_returns_the_empty_message_even_with_rows_present`, `test_render_history_huge_limit_is_capped_and_does_not_crash`, `test_format_ts_across_the_midnight_boundary` (Vera) | PASS |
| AC-8 | Undone entries included + marked; live entries carry no marker | `test_ac8_undone_entry_is_included_and_marked` (Luna) | PASS |
| AC-9 | U-ISO: A and B never see each other's entries | `test_recent_logs_scoped_to_user_id`, `test_ac9_u_iso_two_users_never_see_each_others_entries` (Luna) + `test_recent_logs_does_not_fill_from_other_users_when_requester_is_exhausted`, `test_recent_logs_isolation_combined_with_category_filter_and_soft_delete` (Vera) | PASS |
| AC-10 | Filter shows only the requested habit | `test_ac10_filter_shows_only_the_requested_habit` (Luna) + `test_recent_logs_isolation_combined_with_category_filter_and_soft_delete` (Vera, filter+isolation+soft-delete combined) | PASS |
| AC-11 | Unparsed rows never appear | `test_recent_logs_excludes_unparsed_even_without_a_category_filter`, `test_ac11_unparsed_rows_never_appear` (Luna) + `test_history_unparsed_rows_sandwiched_between_real_entries_leave_them_intact` (Vera) | PASS |
| AC-12 | Raw-text safety (control chars, braces) + budget/footer | `test_ac12_*` (4 tests, Luna) + `test_history_line_with_emoji_zero_width_and_rtl_override_does_not_crash` (Vera) | PASS |
| AC-13 | Empty history → `history_empty` | `test_ac13_empty_history_returns_friendly_message` (Luna) | PASS |
| AC-14 | LLM-free/Ollama-down availability; public menu registration (both languages); bilingual output | `test_ac14_history_available_to_any_active_member_no_owner_check`, `test_ac14_history_registered_in_the_public_command_menu_both_languages`, `test_bilingual_thai_output_localizes_header_and_undone_marker` (Luna) + `test_command_menu_registers_exactly_the_expected_commands_no_extras` (9-command exact-set, pre-existing, updated) + `test_pending_user_sending_history_gets_the_access_gate_not_a_history_reply`, `test_blocked_user_sending_history_gets_denied_not_a_history_reply`, `test_history_works_directly_through_handle_inbound_message_with_ollama_reported_down` (Vera) | PASS |

Every AC in SPEC-v1.4.md §8 is covered and green. 14/14 PASS.

## Failures (if any)

None. 0 failed across the full suite (1599 passed, 1 skipped).

## Characterization notes (not failures — flagged for the record)

These surfaced during adversarial testing. Neither is an AC violation; both are documented here so they aren't mistaken for regressions later, and so a future "fix" doesn't accidentally break parity with `/audit` or introduce an unreachable-in-practice edge case as a real one.

1. **`fit_within_budget`'s "always fits" guarantee has one true edge**: if `render_footer`'s own output (with zero rows kept) still exceeds `TELEGRAM_MESSAGE_BUDGET`, the function's `not kept` floor returns that over-budget string rather than looping forever — correct defensively (no hang/crash), but the *length* guarantee does not hold in that one pathological shape. Not reachable by either real caller today: `audit_view` and `history_view` both pass a short, fixed-shape catalog string (`audit_more_rows` / `history_more_rows`) as the footer, never anything close to this size. Pinned by `test_fit_within_budget_with_a_footer_renderer_that_itself_exceeds_budget`.
2. **`history_header`'s `{limit}` is the requested/capped LIMIT, not the actual row COUNT returned** — e.g. requesting the default 20 with only 1 real entry still prints "Your last 20 entries:". Verified this is **not** a v1.4.0-specific behavior: `core/audit_view.py:render_recent` has the identical, pre-existing pattern (confirmed against `tests/test_audit_view.py`'s own header assertions, which pin `audit_header(limit=N)` the same way regardless of actual row count). SPEC-v1.4.md explicitly says defaults/caps "mirror `/audit`", so `history_view` faithfully reproducing this is correct parity, not a new defect. Pinned by `test_render_history_huge_limit_is_capped_and_does_not_crash`.
3. **Thai-alias matcher is intentionally stricter than the slash form for unknown habits**: `/history coffee` gets the friendly `history_invalid_habit` reply (AC-6), but `ย้อนหลัง กาแฟ` (an unregistered Thai habit word) doesn't match the `"history"` kind at all and falls through to the normal pipeline — because the Thai regex's habit group is registry-anchored (AC-5's own anti-false-positive requirement), so an unrecognized trailing word leaves the `$` anchor unsatisfied and the whole match fails, rather than partially matching. This is the documented, deliberate design (R-D2's own code comment), not an inconsistency to fix. Pinned by `test_dispatch_thai_alias_with_an_unregistered_habit_word_falls_through`.

## Regressions detected

None.

## Recommendation

**Ready to ship.** All 14 acceptance criteria in SPEC-v1.4.md §8 pass. The full suite is green at 1599 passed / 0 failed / 1 skipped (baseline 1576 confirmed independently, +23 new adversarial tests added this pass, zero regressions). The AC-3 extraction is byte-identical (82 pre-existing audit-view tests untouched and passing). U-ISO holds under every combination tried (habit filter + soft-delete + limit-exhaustion). The matcher has no false positives across 21 adversarial/edge shapes (13 from Luna + 8 new). The renderer never crashes on hostile input (control chars, braces, 4000-char text, emoji, zero-width/RTL characters, a removed habit). Routing is correct: pending/blocked users get the v1.2 access gate (never a history reply), and `/history` works with Ollama down, proven at the real `handle_inbound_message` seam with a raising-LLM double. This verdict gates the v1.4.0 release.
