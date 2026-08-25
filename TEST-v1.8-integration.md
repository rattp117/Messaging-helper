# Test Report — v1.8.0 Release Gate (integration ACs + whole-spec sign-off)

## Summary
- Total (full repo suite, foreground, `.venv\Scripts\python.exe -m pytest -q`, `PYTHONPATH=src`): **3793 passed, 0 failed, 1 skipped, 1 xfailed**
- Baseline handed off by Archi (`IMPL-v1.8-integration.md`): 3774 passed / 0 failed / 1 skipped / 1 xfailed
- Delta: **+19 tests**, all new, all passing — `tests/test_v18_release_gate.py` (this pass's own file, probing beyond Luna's 14 integration tests per Archi's dispatch)
- **Status: PASS**
- `data/habits.db` (live DB): confirmed untouched — file mtime `Aug 21 08:58`, unchanged before/after this entire pass; every test in this report and in the existing suite uses a scratch `tmp_path` SQLite file only.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_v18_release_gate.py` (new, mine) | 19 | AC-9 (hard gate, re-proved independently); AC-C1/C2 (backfill × unit-collision fall-through, backfill × custom-habit alias); AC-A5/B3/B5 (routine run never reacts); AC-A2/A6/Q6 (quick-log during an Ollama outage); AC-A1 (20-custom-habit keyboard row-chunking + callback-data budget); AC-D2 (exact menu counts, non-owner isolation, both-registrations-fail resilience); the shared `user_prefs` helper (3 call sites in one run); AC-C4/date_offset backward-compat and fail-closed schema behavior; AC-7 (release notes); housekeeping (no stale single-row keyboard assertion) |
| `tests/test_v18_integration.py` (Archi's, reviewed not modified) | 14 | SPEC-v1.8.md §11.3's own named end-to-end scenarios — already PASS, part of the 3774 baseline |
| Four modules' own files (`test_quicklog.py`/`test_v18_quicklog_gaps.py`, `test_routines.py`/`test_v18_routines_gaps.py`, `test_backfill.py`/`test_v18_backfill_gaps.py`, `test_riders.py`/`test_v18_riders_gaps.py`) (reviewed, not modified) | 265+93+135+25 | Module-level AC-A/B/C/D slices — already PASS per their own TEST-v1.8-*.md reports |

## AC coverage map — all 32 ACs

Every AC maps to at least one PASSING test; the previously-deferred slices Archi flagged are called out explicitly with the test that closes them.

### Shared / integration (AC-1–AC-9)

| AC | Description | Test(s) | Result |
|---|---|---|---|
| AC-1 | Silent send param, payload byte-identical when `False` | `test_v18_shared_surface.py` (module); **`test_v18_release_gate.py::test_ac9_proactive_wire_payload_delta_is_only_disable_notification`** (re-verified at the real `TelegramChannel.build_send_request` wire-payload level, not just the fake's `.sent` text list — confirms `False` omits the field entirely, `True` adds exactly one field, nothing else differs) | **PASS** |
| AC-2 | `set_message_reaction` no-op default / fail-open Telegram impl | `test_v18_shared_surface.py` | **PASS** |
| AC-3 | Scoped menu (`scope_chat_id`) | `test_v18_shared_surface.py`; `test_v18_integration.py::test_owner_scoped_menu_has_admin_commands_public_menu_does_not` | **PASS** |
| AC-4 | `message_id` plumbing `run → on_message → handle_inbound_message` | `test_v18_shared_surface.py`; `test_v18_integration.py::test_ac9_ordinary_log_confirmation_text_unaffected_by_the_reaction_side_channel` | **PASS** |
| AC-5 | Config defaults, 5 new sections | `test_v18_shared_surface.py` | **PASS** |
| AC-6 | Audit vocab (`routine_create`/`routine_delete`/`routine_run`) | `test_v18_shared_surface.py`, `test_audit.py`; exercised live by `test_v18_integration.py::test_routine_two_user_isolation_end_to_end` | **PASS** |
| AC-7 | Release notes `RELEASE_NOTES["1.8.0"]` EN+TH | **`test_v18_release_gate.py::test_release_notes_1_8_0_present_both_languages_and_mentions_every_shipped_feature`**, **`::test_release_notes_1_8_0_actually_announces_via_the_real_announce_path`** (drives the real `announce.announce_release`, not just a dict lookup) | **PASS** |
| AC-8 | Reserved words (`log`/`บันทึก`/`routine`/`กิจวัตร`) rejected by `habitdef` | `test_v18_shared_surface.py` | **PASS** |
| AC-9 | Inert until invoked — byte-identical to v1.7, only proactive delta is `disable_notification` | Full v1.7 suite green (structural); `test_v18_integration.py::test_ac9_ordinary_log_confirmation_text_unaffected_by_the_reaction_side_channel`; **`test_v18_release_gate.py::test_ac9_water_stretch_diary_confirmations_are_exactly_the_plain_template`** (exact byte match against the plain `i18n.t(...)` template, no stray backfill prefix/suffix, for both water and stretch); **`::test_ac9_proactive_wire_payload_delta_is_only_disable_notification`** | **PASS** |

### Quick-log + reactions (`quicklog`, AC-A1–A6)

| AC | Test(s) | Result |
|---|---|---|
| AC-A1 | `test_quicklog.py`/`test_v18_quicklog_gaps.py` (module); `test_v18_integration.py::test_quicklog_button_prompt_and_empty_hint`; **`test_v18_release_gate.py::test_quicklog_keyboard_for_20_custom_habits_chunks_rows_and_fits_callback_budget`** (20 custom habits at the exact `custom_habits.max_per_user` cap — every row ≤3 buttons, every `callback_data` ≤64 bytes, payload JSON-serializable) | **PASS** |
| AC-A2 | Module tests (byte-identical confirmations, incl. language parity fix); `test_v18_integration.py::test_quicklog_tap_is_byte_identical_to_typing_and_reaction_is_typed_log_only`; **`test_v18_release_gate.py::test_quicklog_tap_logs_successfully_while_ollama_is_down`** | **PASS** |
| AC-A3 | Module tests (malformed/oversized/Unicode-digit/foreign-habit payloads) | **PASS** |
| AC-A4 | Module tests + `test_v18_integration.py`'s reaction-fires test | **PASS** |
| AC-A5 | Module tests + `test_v18_integration.py` (tap gets none); **`test_v18_release_gate.py::test_routine_run_never_fires_a_reaction`**, **`::test_routine_run_button_tap_never_fires_a_reaction_either`** (routines never react, a cross-module exclusion no single module's own Vera could probe) | **PASS** |
| AC-A6 | Module tests (bilingual, zero-Ollama structural proof); **`test_v18_release_gate.py::test_quicklog_tap_logs_successfully_while_ollama_is_down`**, **`::test_typed_log_defers_while_ollama_is_down_but_quicklog_tap_in_the_same_run_still_works`** (real end-to-end proof: a typed log defers with Ollama down in the SAME run where a quick-log tap for the same habit still logs immediately) | **PASS** |

### Routines (`routines`, AC-B1–B7)

| AC | Test(s) | Result |
|---|---|---|
| AC-B1 | `test_routines.py`/`test_v18_routines_gaps.py`; the bare-`=`-with-nothing-after regex gap (Finding 1) fixed per `IMPL-v1.8-integration.md` item 8, re-verified by the full suite staying green | **PASS** |
| AC-B2 | `test_routines.py`/`test_v18_routines_gaps.py` | **PASS** |
| AC-B3 | `test_routines.py`/`test_v18_routines_gaps.py`; **`test_v18_release_gate.py::test_routine_run_never_fires_a_reaction`** (run-in-context, no reaction leak) | **PASS** |
| AC-B4 | `test_routines.py`/`test_v18_routines_gaps.py` | **PASS** |
| AC-B5 | `test_routines.py`/`test_v18_routines_gaps.py`; `test_v18_integration.py::test_routine_two_user_isolation_end_to_end`, `::test_routine_run_button_tap_is_isolated_per_tapping_user` (spoofed-tap ownership) | **PASS** |
| AC-B6 | `test_routines.py`; full suite green at schema version 11 throughout | **PASS** |
| AC-B7 | `test_routines.py` (static AST import check + dynamic poison-and-drive check) | **PASS** |

### Backfill (`backfill`, AC-C1–C6)

| AC | Test(s) | Result |
|---|---|---|
| AC-C1 | Module tests (extraction slice, all 6 documented EN+TH phrases + 65-case negative corpus); `test_v18_integration.py::test_backfill_yesterday_lands_correctly_no_dashboard_edit_no_milestone` (residual→zero-LLM-preparse path), `::test_llm_date_offset_backdates_when_deterministic_parser_misses` (residual→LLM path); **`test_v18_release_gate.py::test_backfill_residual_with_colliding_unit_falls_through_to_llm_but_deterministic_date_still_wins`** (closes the previously-untested cross-feature slice: a residual whose unit token COLLIDES between two habits forces the LLM branch even though the phrase itself parsed deterministically — and the deterministic date still wins over the LLM's own contrived `date_offset`), **`::test_backfill_resolves_through_a_custom_habits_own_unit_alias`** (a CUSTOM habit's own `unit_aliases` entry resolves through backfill's residual zero-LLM, "2 set yesterday" → pushups × 10 = 20, landed on yesterday) | **PASS** |
| AC-C2 | `test_v18_integration.py::test_backfill_yesterday_lands_correctly_no_dashboard_edit_no_milestone` (direct `ts`-prefix row check + `heatmap._day_intensity` for both the resolved day and today + `/history`) | **PASS** |
| AC-C3 | Same test (no milestone/record text, `channel.edits` count unchanged) | **PASS** |
| AC-C4 | `test_backfill.py`/gaps (EN+TH bounds, non-default cap, exactly-at-cap/one-past-cap); `test_v18_integration.py::test_backfill_future_and_too_old_are_rejected_no_write`, `::test_llm_date_offset_out_of_range_is_rejected_no_write`; **`test_v18_release_gate.py::test_llm_date_offset_exactly_at_the_cap_is_honored_one_past_is_rejected`** (full-pipeline boundary via the real LLM branch, not just the pure `backfill.py` helper) | **PASS** |
| AC-C5 | `test_backfill.py`/gaps — 65-case combined EN+TH adversarial corpus | **PASS** |
| AC-C6 | `test_v18_integration.py::test_backfill_yesterday_lands_correctly_no_dashboard_edit_no_milestone`'s own undo continuation (removes exactly the backfilled row by id, leaving today's row) | **PASS** |

### Riders (`riders`, AC-D1–D4)

| AC | Test(s) | Result |
|---|---|---|
| AC-D1 | `test_riders.py`/gaps; **`test_v18_release_gate.py::test_ac9_proactive_wire_payload_delta_is_only_disable_notification`** (re-verified at the real wire-payload level) | **PASS** |
| AC-D2 | `test_v18_integration.py::test_owner_scoped_menu_has_admin_commands_public_menu_does_not`, `::test_owner_scoped_menu_registration_failure_never_crashes_startup`; **`test_v18_release_gate.py::test_owner_menu_is_public_18_plus_5_admin_public_menu_is_exactly_18`** (exact counts: public 18, owner 23 = 18+5, both languages), **`::test_non_owner_chat_never_receives_any_scoped_menu_registration`** (exactly 2 `set_my_commands` calls ever, MEMBER's chat id never appears as a scope), **`::test_startup_survives_both_public_and_owner_menu_registration_failing`** (BOTH registrations raise — Archi's own test only broke the owner-scoped call; this closes the "genuinely both fail" belt-and-suspenders gap) | **PASS** |
| AC-D3 | `test_v18_integration.py::test_audit_renders_in_the_owners_stored_language_even_via_ascii_trigger`, `::test_audit_non_owner_gets_no_reply`; `test_v15_integration.py` (updated, the pre-existing bug's own regression test flipped to the fixed behavior); **`test_v18_release_gate.py::test_user_prefs_helper_agrees_across_access_audit_and_reminders_call_sites`** (the SAME run exercises `/audit`'s call site AND `access.py`'s `access_granted` call site AND `reminders.py`'s tick call site — proving the 3 now-consolidated `core/user_prefs.py` callers agree with each other in one continuous scenario, not just each independently; the 4th, `core/quicklog.py`, is proven at module level by `TEST-v1.8-quicklog.md`'s own re-verification) | **PASS** |
| AC-D4 | `test_riders.py` (silent_proactive=false byte-identical); `test_v15_integration.py` (audit row content/order unchanged) | **PASS** |

## Additional probes (per Archi's specific dispatch items, beyond the 32 ACs)

- **`date_offset` schema/prompt/parser backward compatibility**: `test_v18_release_gate.py::test_llm_response_missing_date_offset_key_still_parses_as_a_normal_log` (an old-style, pre-v1.8 3-key LLM response — no `date_offset` at all — still parses and logs completely normally); `::test_llm_response_with_malformed_date_offset_fails_closed_to_no_date_not_a_crash` (negative / fractional / non-numeric-string `date_offset` values each fail closed to "no date," not a crash, not an aborted extraction, across 3 distinct malformed shapes). **PASS.**
- **Housekeeping — no stale pre-chunking keyboard assertion**: `test_v18_release_gate.py::test_no_test_file_still_asserts_the_pre_chunking_single_row_keyboard_shape` (automated repo-wide scan) + manual `grep` cross-check of every `inline_keyboard`-referencing assertion in `tests/` — the one remaining single-row assertion (`test_channels.py:342`, one button) is legitimate (≤3 buttons is still correctly one row). **PASS, no stale assertion found.**
- **Version-pin test** (`tests/test_v15_integration.py::test_current_pinned_version_announces_to_active_users_today`): confirmed `src/habit_assistant/__init__.py:__version__` is still `"1.7.0"` and the test still expects `"1.7.0"` — consistent with each other right now. Per the dispatch note, this is expected to be bumped to `"1.8.0"` at the release step (Archi's Phase 6.5), not by this pass. **Not touched, not a failure.**
- **Known accepted incident** (live `data/habits.db` migrated to schema 011 by an earlier integration smoke command): re-confirmed out of scope and harmless — file mtime (`Aug 21 08:58`) predates this entire testing pass and was never touched by any test here (all scratch `tmp_path` DBs). **Not re-litigated, not scored.**

## Failures (if any)

None.

## Regressions detected

None. Full-suite delta from Archi's 3774-test baseline is exactly +19 (this pass's own new file) — every pre-existing test, including all four modules' own suites and `test_v18_integration.py`, is unchanged and still green.

## Recommendation

**Ready to ship.** All 32 acceptance criteria (shared/integration AC-1–9, quicklog A1–A6, routines B1–B7, backfill C1–C6, riders D1–D4) show PASS, including every previously-deferred slice named in the dispatch (AC-C1's full EN+TH path through the real `handle_inbound_message`, AC-C2 aggregations, AC-C3 no-retro-celebration/no-dashboard-edit, AC-C4 bilingual bounds, AC-C6 undo, AC-D2 two-scope menu, AC-D3 `/audit` Thai, AC-9 inert-until-invoked). No production-code defects found in this pass; no changes needed from Luna. Full suite: **3793 passed, 0 failed, 1 skipped, 1 xfailed**. `data/habits.db` confirmed untouched throughout.

**PASS — recommend Archi proceed to Phase 6.5** (version bump to `1.8.0`, `PROGRESS.md` changelog, commit + tag).
