# Test Report — v1.7.0 release gate (integration ACs + whole-spec sign-off)

## Summary
- Total new tests this pass: 12 (all new, `tests/test_v17_release_gate.py`)
- Passed: 12
- Failed: 0
- Housekeeping: renamed `tests/test_v16_integration.py::test_menu_has_exactly_14_public_commands_both_languages` → `..._16_...` (Archi-approved, body/count already said 16; no other production or test edits)
- Full project suite (all tracks + this pass): **3344 passed, 1 skipped, 1 xfailed, 0 failed** (was 3332 at Luna's integration hand-off; delta is exactly this file's 12 tests; rename touched no assertion, same skip/xfail set)
- **Status: PASS — this is the release gate. Recommendation: ship v1.7.0.**

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_v17_release_gate.py` | 12 (new) | Integration-scope probes not covered by any of the three prior tracks (see below), plus a final AC-5/AC-6 re-lock on the finished tree |

Sections: (1) provider fallback-path safety, 3 tests · (2) menu/help release-gate precision, 2 tests · (3) full continuous lifecycle through real dispatch, 1 test (19-step script, ~25 assertions) · (4) `RELEASE_NOTES["1.7.0"]` readiness, 3 tests · (5) AC-5/AC-6 final re-check, 3 tests.

## AC coverage — all 16 ACs, union of all four tracks (no hole found)

| AC | Owner track | Test(s) | Result |
|---|---|---|---|
| AC-1 (migration 010) | shared | `test_migrations.py::test_v9_shaped_db_migrates_to_v10_user_habits_touching_nothing_existing` + `test_fresh_db_has_user_habits_shape`; full-suite green (3344/0) | **PASS** |
| AC-2 (per-user registry) | shared | `test_habits.py::test_for_user_with_no_rows_is_byte_identical_to_from_config`, `test_for_user_appends_active_custom_habits_after_the_base_catalog`, `test_for_user_excludes_archived_habits_from_the_registry` | **PASS** |
| AC-3 (rebuild w/o restart) | shared | `test_registry_provider.py::test_for_user_caches_across_calls_even_after_a_direct_db_write`, `test_invalidate_is_scoped_to_exactly_one_user`; real-dispatch re-check `test_v17_integration.py::test_addhabit_end_to_end_no_restart_dashboard_records_habits_help_pick_it_up`; **this pass** adds `test_v17_release_gate.py::test_two_sequential_no_provider_calls_still_see_each_others_writes` (proves correctness survives even with ZERO shared cache — the fallback path) | **PASS** |
| AC-4 (per-user rewiring, all consumers) | shared+sweep | `test_{reminders,checkins,nudge}.py` (`registry_for` spy-proof) + `test_v17_isolation_sweep.py` (all 17 consumers) + `test_v17_integration.py` (real dispatch); **this pass** adds `test_full_custom_habit_lifecycle_through_real_dispatch` — target/dashboard/heatmap/records/trends/habits/history all in ONE continuous real-dispatch flow (not just per-surface in isolation) | **PASS** |
| AC-5 (owner byte-identical / regression gate) | shared+sweep+integration | Full suite 3344/0 failed; `test_v17_integration.py::test_ac5_owner_with_no_custom_habits_is_still_byte_identical_through_real_dispatch`; `test_v17_isolation_sweep.py::test_ac5_member_stays_byte_identical_to_v16_while_owner_has_a_custom_habit`; **this pass** re-checks at a call shape nobody had exercised yet — the `--dry-run`-style call with NO provider at all (`test_ac5_owner_water_confirmation_byte_identical_via_the_no_provider_fallback_path`) | **PASS** |
| AC-6 (Thai-numeral/full-width lock) | shared+integration | Pre-existing `units.py`/`preparse.py` suite, unmodified, still green; `test_v17_integration.py::test_thai_numeral_log_preparses_with_no_llm_through_the_real_wired_path`; **this pass** adds the full-width-digit case (`５００ml`) alongside the Thai-numeral case in one final real-dispatch re-check, `test_ac6_thai_numeral_and_full_width_digit_lock_final_recheck` | **PASS** |
| AC-7 (audit vocab) | shared+habitdef | `test_audit.py::test_actions_matches_the_spec_vocabulary_exactly`; `test_habitdef.py`/`test_v17_habitdef_gaps.py` audit-content tests | **PASS** |
| AC-8 (release notes + menu/help, R-A2) | shared+integration | **Gap found and closed this pass** — see "Coverage gap closed" below. Menu precision: `test_v17_release_gate.py::test_public_menu_is_exactly_16_commands_addhabit_delhabit_last_both_languages`; bilingual help: `test_help_text_lists_addhabit_and_delhabit_bilingually`; release-notes catalog + announce pickup: `test_v170_ships_as_a_release_notes_catalog_entry`, `test_get_release_note_returns_both_languages_for_v170`, `test_announce_release_picks_up_v170_for_an_active_user_and_is_idempotent` | **PASS** |
| AC-H1 (create) | habitdef | `test_habitdef.py::test_execute_addhabit_creates_row_and_confirms_bilingually` + `test_execute_addhabit_appears_in_the_users_registry_immediately_ac3`; real-dispatch in `test_v17_integration.py` and this pass's lifecycle test | **PASS** |
| AC-H2 (validation) | habitdef | `test_habitdef.py` (~30 parametrized) + `test_v17_habitdef_gaps.py` (123 adversarial, incl. exhaustive grammar edge cases); this pass's `test_two_sequential_no_provider_calls_still_see_each_others_writes` re-proves duplicate-id rejection through the fallback path | **PASS** |
| AC-H3 (label/id collision safety) | habitdef | `test_habitdef.py` + `test_v17_habitdef_gaps.py`'s exhaustive 51-reserved-word sweep; `test_v17_integration.py`'s "help"/"เตือน" rejection through real dispatch | **PASS** |
| AC-H4 (unit collision degrades) | habitdef | `test_habitdef.py::test_addhabit_colliding_unit_is_excluded_from_preparse_lookup_ac_h4` + `test_v17_habitdef_gaps.py`'s two-way cross-effect test | **PASS** |
| AC-H5 (delete semantics) | habitdef+integration | `test_habitdef.py` archive/hard-delete/already-archived tests; `test_v17_integration.py`'s real-dispatch archive+hard-delete tests; **this pass** closes a real gap — see "Coverage gap closed" below | **PASS** |
| AC-H6 (`/habits`) | habitdef | `test_habitdef.py`'s owner-vs-member + archived-omitted tests; this pass's lifecycle test exercises `/habits` pre- and post-archive through real dispatch | **PASS** |
| AC-S1 (17-surface two-user isolation) | sweep | `test_v17_isolation_sweep.py`, all 17/17 surfaces, direct-row-driven | **PASS** |
| AC-S2 (per-user LLM prompt) | sweep | `test_v17_isolation_sweep.py::test_ac_s2_extraction_prompt_and_schema_are_per_user` | **PASS** |

**Union check: all 16 ACs from SPEC-v1.7.md §8 are covered by at least one passing test; no hole found.**

## Specific probes requested by Archi

**1. Provider fallback path (`handle_inbound_message` with no `provider`, e.g. `--dry-run`).**
Confirmed safe and correct at `main.py:786-816`: when `provider` is `None`, the branch builds a fresh one-off `RegistryProvider(config, db)` per call. `execute_addhabit`/`execute_delhabit` validate against that provider's `for_user()` (a real, fresh DB read every time — never stale) and call `provider.invalidate(user_id)` on a cache that never held an entry, which is a safe `dict.pop(user_id, None)` no-op. Two SEPARATE direct calls with no provider at all still see each other's writes correctly (proven in `test_two_sequential_no_provider_calls_still_see_each_others_writes` — a second `/addhabit` for the same id is correctly rejected as a duplicate) — correctness never depended on the shared cache, only the AC-3 *performance* benefit does, and that's exactly the tradeoff a one-shot `--dry-run` process makes. No crash, no unsafe state anywhere in this path.

One incidental discovery while probing this (documented, **not a v1.7 regression**): `--dry-run` for a PLAIN LOG message (free text, not a recognized command) prints the raw `asdict(result)` and returns *before* the DB insert (`main.py:1025-1027`) — it never writes a `logs` row and never renders the formatted confirmation string. This is pre-existing CLI behavior (present since well before v1.7); commands like `/addhabit`/`/target` behave differently (`dry_run=True` still writes, only send-vs-print differs). Not in scope to fix; flagged for awareness only.

**2. Menu regression.** Exactly 16 public commands, in both `en`/`th`, `/addhabit`+`/delhabit` last (matching the established "each release appends its own commands at the end" convention — confirmed the 3rd-from-last is `trends`, v1.6's own newest addition). `/help` lists both bilingually (both language variants literally contain `/addhabit`/`/delhabit`, matching the pre-existing "slash-commands are untranslated" convention this suite already established for the v1.6 commands).

**3. End-to-end lifecycle.** One continuous 19-step real-dispatch script: `/addhabit` → preparse-instant log (zero LLM) → `/target` on the custom habit → picked up by `/habits`, `/records`, `/trends`, `/heatmap`, `/history` (filtered and plain) → `/delhabit` archives it (has history) → `/habits`/`/records` drop it from active surfaces → id stays reserved (re-add rejected) → a separate log-free habit's `/delhabit` hard-deletes and frees its id for immediate reuse. All PASS.

Two genuine, previously-untested findings surfaced by pushing this further than any prior track had (both **spec-consistent, not bugs**, but worth Archi/Luna knowing about explicitly):
- **Filtered `/history <archived-id>` stops resolving.** After archiving, `registry.get("pages")` is `None` (archived rows are excluded from the active registry by construction, R-G1/AC-2), so `/history pages` returns the same `history_invalid_habit` reply an id that was *never* created would get — even though plain (unfiltered) `/history` still lists the row. R-C2's text only promises entries "remain visible in /history," not that the filter argument keeps resolving, so this is not a spec violation — but it is a real UX rough edge (a user who remembers the id and tries to filter by it gets "I don't track that" instead of their own history).
- **Plain `/history`'s description degrades post-archive.** The row survives (still listed), but `undo_ui.describe_log` also resolves through the archived-excluding registry, so the rich "20 pages" formatting falls back to the generic `describe_log_generic` template ("pages entry") instead. The raw quoted original message (`"20 pages"`) is still shown alongside it, so the value isn't truly lost to the user, just no longer type-formatted.

**4. Announce/release-notes readiness.** `RELEASE_NOTES["1.7.0"]` (EN+TH) was previously verified **only by Luna's manual smoke script** (IMPL-v1.7-shared.md) — no committed test asserted it. Closed this gap with three tests exercising the REAL `core/announce.py:announce_release` function against the literal string `"1.7.0"` (never touching `__init__.py:__version__`): the catalog entry exists, both languages render and differ, and `announce_release` sends exactly one message to an active user, stamps `last_announced_version`, and is idempotent on a second call for the same version. This proves the announce machinery would fire correctly the instant Phase 6.5 bumps the real version — without this test suite performing that bump itself.

**5. AC-5/AC-6 final lock.** Re-checked one more time on the fully-integrated tree, at a second call shape (the `--dry-run`-style no-provider path) neither prior track had exercised for these two specific gates. Both hold exactly, byte-for-byte.

## Coverage gaps found and closed this pass

1. **AC-8 (release notes) had zero committed tests** — only a manual smoke script. Closed (see probe 4 above).
2. **AC-H5's "still in /history" claim was never actually exercised** by any prior track — `habitdef`'s own tests check the DB row state (archived_at set) but never render `/history` afterward; `sweep`'s own scope is two-user isolation, not single-user post-archive behavior. Closed by this pass's lifecycle test, which also surfaced the two findings documented above.
3. Neither `/target`, `/heatmap`, nor `/trends` had been exercised on a custom habit through **real dispatch** in one continuous flow before (each was previously proven only via direct registry/render-function calls in `sweep`, or not at all for `/target`/`/trends` in the integration track's own file). Closed by the same lifecycle test.

No other holes found. Every other AC already had solid, real-dispatch-level coverage from the three prior tracks.

## Failures (if any)

None in the final state. (Three test-authoring mistakes were found and fixed while drafting this pass's own new file — never production bugs: two tests wrongly assumed `--dry-run` renders a full confirmation string for a plain log message, when production code actually prints the raw parse result and returns before the insert; one test's script embedded a Thai `th=` field inside an `/addhabit` command line, which flipped that script's own auto-detected reply language to Thai for every subsequent step via `i18n.resolve_reply_language`. All three were test-file-only fixes, made before this report was finalized — no production code or other tracks' test files were touched.)

## Regressions detected

None. Full suite: 3344 passed / 0 failed / 1 skipped / 1 xfailed (up from 3332 at hand-off; delta is exactly this pass's 12 new tests; same skip/xfail set).

## Recommendation

**Ready to ship — PASS.** All 16 acceptance criteria in SPEC-v1.7.md §8 are covered by at least one passing test, with no hole in the union across all four tracks (shared surface, `habitdef`, `sweep`, integration) plus this release-gate pass. Two non-blocking, spec-consistent UX observations are documented above for optional follow-up (post-archive `/history` filter and description degradation) — neither violates any AC and neither should block v1.7.0. Archi may proceed to Phase 6.5 (version bump, changelog, commit/tag, bounce, verify migration 010 + announcement).
