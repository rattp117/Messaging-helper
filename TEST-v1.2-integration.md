# Test Report — v1.2.0 integration (final release gate)

## Summary
- Scope: integration-owned ACs per SPEC-v1.2.md §11's table — **AC-M1, AC-M2, AC-M3, AC-C1, AC-C2, AC-U-ISO, AC-S1, AC-S4, AC-S6, AC-O1, AC-X1** — plus re-confirmation of every module-owned AC (`access`/`preferences`/`schedules`) through the REAL, wired `on_message`/`on_callback`/`async_main` path, not each module's own direct-call unit tests.
- Total (`tests/test_v12_integration.py`): **31 tests** (13 Luna + **18 new, this pass**).
- Passed: 31 / 31 (file scope).
- Full repo suite: **1325 passed, 0 failed, 1 skipped** (baseline going in: 1307 passed / 0 failed / 1 skipped; delta = +18, exactly this pass's additions — no other file regressed). The 1 skip is the same pre-existing conditional architectural-boundary check every prior pass has carried (`tests/test_channels.py:232`).
- Status: **PASS**.
- **RELEASE GATE VERDICT: PASS.** v1.2.0 is ready to ship.

## Test files
| Path | Tests added (this pass) | Covers which ACs |
|---|---|---|
| `tests/test_v12_integration.py` | 18 new (of 31 total) | AC-M1, AC-M2, AC-M3, AC-C1, AC-C2, AC-U-ISO, AC-S1, AC-S4, AC-S6, AC-A1, AC-A3, AC-P1, AC-P2, AC-U3, plus the coordinator's punch-list items 1–8 below |

No production code was modified. All changes are additive to the existing `tests/test_v12_integration.py` (new imports, two small test-only helper additions to its shared fakes — `_CountingOllamaClient`, `_ScriptedChannel.run_jobs_before_stop` — and 18 new test functions/parametrizations appended). No test opens `data/habits.db` or touches the live Task Scheduler service; every DB is a scratch `tmp_path` SQLite file; the LLM is always the file's existing `_FakeOllamaClient` double.

## Coordinator punch-list — per item

### 1. Gate security
- **Stranger → pending + exactly one owner notification, nothing logged, no LLM call** → `test_stranger_gate_never_logs_or_calls_the_llm_and_notifies_owner_exactly_once`. Three messages (a log attempt, a question, and `/start`) from the same still-pending stranger: owner notified exactly once, `SELECT COUNT(*) FROM logs WHERE user_id = stranger` stays 0, and a call-counting `OllamaClient` double proves `chat_json` is never invoked (not "invoked and ignored" — never called). **PASS.**
- **Pending/blocked message → polite denial only** → the above test's repeat messages (pending case) all get `access_pending`; `test_blocked_users_message_gets_denial_only_nothing_logged_no_llm_call` (blocked case) gets exactly `[access_denied]`, zero owner side-channel, zero logs, zero LLM calls. **PASS.**
- **Blocked user's stale button tap refused; forged `undo:<owner's log_id>` from a blocked/pending/unknown attacker chat** → `test_forged_undo_callback_for_owners_log_from_a_non_active_attacker_is_refused` (parametrized ×3: unknown/pending/blocked attacker). The attacker forges the OWNER's own real log id, not their own. In every case: the owner's row survives (`deleted_at is None`), the attacker gets no reply, and — checked explicitly — the owner gets no side-channel notification either. This directly probes the LIGHTER `on_callback` gate (`access.classify`, not the full `handle_gate`) described in `main.py`'s own comment, confirming it refuses BEFORE `undo_ui.handle_undo_callback`'s row-ownership check is ever reached. **PASS.**

### 2. Two-user isolation end-to-end
Building on the pre-existing life-cycle test (logs, `/habits`, `/target`, `/undo` text, `/lang`), this pass adds the remaining named surfaces:
- **`/undo` via BUTTON, two active (non-owner) members** → `test_two_active_members_undo_via_button_only_affects_the_tapping_members_own_row`: A's tap deletes only A's row; B's row and B's inbox are untouched. **PASS.**
- **`/remind`, two active members** → `test_remind_isolation_between_two_active_members`: A sets a custom water time (07:00); at 07:00 only A fires; at 08:00 (the config default) only B fires (A's override fully replaced the default for A alone). **PASS.**
- **`/quiet`, two active members** → `test_quiet_isolation_between_two_active_members`: A's window is stored and effective; B (untouched) still inherits the global (empty) default. **PASS.**
- **Queries** → `test_query_answers_are_scoped_to_the_asking_users_own_data`: A's "how much water today?" answer contains A's total and not B's; B's answer is the reverse. **PASS.**
- **Daily-summary content** → `test_daily_summary_fan_out_shows_each_users_own_totals_and_skips_a_user_with_no_logs_today`, driven through the REAL scheduled-job closure (captured off `_FakeScheduler`, not a direct `streaks.run_daily_summary` call): A's and B's summaries reflect only their own habit, and a third user (C) who logged nothing today is skipped entirely — no empty recap sent. **PASS.**

Cross-visibility is zero across every one of these surfaces, matching AC-U-ISO's own requirement.

### 3. AC-M3 owner-unchanged
Pre-existing `test_ac_m3_owner_confirmation_is_byte_identical_through_the_gated_wiring` pins one exact confirmation string. This pass adds `test_ac_m3_owner_habits_and_undo_stay_byte_identical_with_zero_other_users`, extending the same "owner is the ONLY user in the system" scenario to `/habits` and `/undo`: exact confirmation string, `/habits` total reflects the log, `/undo` reply starts with `↩️ Undone` and shows the post-removal `0 / 2500 ml (0%)` total — plus a direct DB check that exactly one `users` row (the owner) ever existed, proving the gate never needed to create or consult anyone else. **PASS.**

### 4. The safety-net deviation
`test_admin_kind_reaching_handle_inbound_message_directly_does_not_corrupt_the_last_log`: seeds one log row (`value_num=500.0`), then calls `handle_inbound_message` DIRECTLY (bypassing `on_message`'s routing entirely) with all five access-owned command texts (`/approve 123`, `/block 123`, `/users`, `/invite 123`, `/start`), both with `dry_run=False` (channel is a double that raises `AssertionError` if `send`/`send_actionable` is ever called — structural proof, not just "nothing observed") and with `dry_run=True, channel=None`. After all ten calls, the seeded row's `value_num` is still exactly `500.0` — proving the exact data-corruption edge Luna's own deviation note describes (`_execute_edit(category=None, value_num=None)` nulling the user's most recent log of ANY category) cannot occur. **PASS — deviation verified fixed, not just documented.**

### 5. `user_pref` threading
- **Reminders** → `test_lang_th_propagates_to_reminder_text`: after `/lang th`, a live `run_due_reminders` tick sends the Thai `reminder_water` catalog string, not English. **PASS.**
- **Daily summary + weekly review** → `test_lang_th_propagates_to_daily_summary_and_weekly_review`: both job closures, invoked for real (via `_ScriptedChannel.run_jobs_before_stop`, added this pass so the jobs run INSIDE `async_main`'s live DB connection rather than after it's closed), produce the Thai `daily_summary_header`/`weekly_review_header` — and explicitly NOT the English ones. **PASS.**
- **Owner unset (auto) → auto-detect unchanged** → `test_owner_autodetect_unaffected_when_no_lang_pref_ever_set`: with NO `/lang` ever run by anyone, a Thai-text message from the owner still gets a Thai reply and an English one still gets English — proving `_stored_language_pref`'s new threading is a true no-op for an unset preference. **PASS.**

### 6. `display_name`
- **Owner notification** (`access_request`) shows the captured `first_name`, falling back to the bare chat id when absent — already proven by the pre-existing life-cycle test and the 3 `_display_name_of` unit tests; re-confirmed via `test_two_arg_on_message_call_still_works_and_falls_back_to_chat_id_when_no_display_name`. **PASS.**
- **`/users` — FINDING, not a defect:** the coordinator's assumption that `/users` also shows the display name does not match the actual (and spec-conformant) implementation. `core/access.py:_render_users_list` has never rendered `display_name` — only chat id / role / status / lang, matching SPEC-v1.2.md §3.3's own illustrative example verbatim (chat-id-only rows). `test_users_listing_never_includes_display_name` locks this in explicitly (approves a stranger with a captured name "Charlie", confirms `/users` shows `7777` but never `"Charlie"`, while the earlier `access_request` notification to the same owner DID carry it). **Recorded so this doesn't silently drift into release notes as a claimed feature that isn't there — not a blocker, and arguably correct per spec.**
- **2-arg channel fakes still work** → `test_two_arg_on_message_call_still_works_and_falls_back_to_chat_id_when_no_display_name`: a channel whose `run()` calls `on_message(chat_id, text)` with exactly 2 positional args (no `display_name`) does not crash — the closure's default parameter absorbs it cleanly, and the resulting `access_request` correctly falls back to the bare chat id. **PASS.**

### 7. Command menu
Pre-existing `test_command_menu_public_set_excludes_the_four_admin_only_commands` already asserts, for BOTH registered languages, the exact 8-command public set (`start, undo, target, help, habits, remind, lang, quiet`) with none of the four admin commands (`approve, block, users, invite`) present. Independently re-read against `main.py`'s own `command_menu` construction and `START_/LANG_/QUIET_/REMIND_COMMAND_DESCRIPTIONS` docstrings — the exclusion rationale (global `setMyCommands`, would leak owner-only capability) holds. No new test needed; re-verified. **PASS.**

### 8. Migration + attribution on a scratch copy
`test_migration_and_attribution_rehearsal_on_a_v1_1_shaped_scratch_db`: hand-builds a raw sqlite3 DB matching the exact v1.1 schema (through migration 005, `user_version=5`, no `users` table, no `logs.user_id`), with two real pre-existing owner water logs (dated "today" so they land in `/habits`' own today-window) and a pre-existing `habit_targets` override (3000 ml), at the exact path `async_main` will open. Then runs the REAL `async_main` startup (`_run`) — the same migration-006-then-`attribute_legacy_to_owner` sequence production will execute on upgrade day — and drives one real `/habits` message plus a second message from a brand-new, never-before-seen chat.
Verified: schema lands at version 6; zero NULL `user_id` rows remain in either `logs` or `habit_targets`; the owner's `users` row is `role=owner, status=active`; the owner's `/habits` overview correctly shows the migrated total (`today 800 ml` = 500 + 300, both legacy rows now attributed to them) THROUGH the real production code path (not a raw SQL assertion); the pre-existing target override (3000) carried over and is readable via `db.get_target`; and the brand-new chat is correctly gated off as unknown, seeing none of the migrated legacy data. **PASS — this is the closest rehearsal of the actual production upgrade this suite can give without a real deployment.**

## AC coverage (integration-owned, SPEC-v1.2.md §11)
| AC | Test(s) | Status |
|---|---|---|
| **AC-M1** (migration 006, idempotent) | `tests/test_migrations.py::test_v5_shaped_db_migrates_to_v6_multiuser` (pre-existing) + `test_migration_and_attribution_rehearsal_on_a_v1_1_shaped_scratch_db` (this pass, through the real app) | **PASS** |
| **AC-M2** (owner attribution, idempotent) | `tests/test_migrations.py::test_attribute_legacy_to_owner_*` (pre-existing) + the same rehearsal test above | **PASS** |
| **AC-M3** (owner byte-identical) | `test_ac_m3_owner_confirmation_is_byte_identical_through_the_gated_wiring` (Luna) + `test_ac_m3_owner_habits_and_undo_stay_byte_identical_with_zero_other_users` (Vera) + full 1325-test suite green | **PASS** |
| **AC-C1** (per-chat delivery + ownership) | `test_full_two_user_lifecycle_onboarding_through_isolated_use` (Luna) + `test_two_active_members_undo_via_button_only_affects_the_tapping_members_own_row`, forged-callback tests (Vera) | **PASS** |
| **AC-C2** (callback ownership incl. the gate layer) | `test_on_callback_gate_blocks_a_blocked_chats_button_tap`, `test_on_callback_gate_lets_an_active_chats_button_tap_through` (Luna) + `test_forged_undo_callback_for_owners_log_from_a_non_active_attacker_is_refused` ×3, `test_two_active_members_undo_via_button_only_affects_the_tapping_members_own_row` (Vera) | **PASS** |
| **AC-U-ISO** (isolation invariant) | Life-cycle test (Luna) + undo-via-button, `/remind`, `/quiet`, queries, daily-summary fan-out isolation tests (Vera, all §11 punch-list item 2) | **PASS** |
| **AC-S1** (single tick, owner unchanged) | `tests/test_reminders.py`/`test_multi_habit_integration.py` (shared-surface) + `test_remind_isolation_between_two_active_members`'s own config-default-still-fires-for-b half (Vera) | **PASS** |
| **AC-S4** (no restart on `/remind` write) | `test_ac_s4_remind_write_through_real_on_message_is_picked_up_by_the_next_tick` (Luna, owner) + `test_remind_isolation_between_two_active_members` (Vera, two ordinary members) | **PASS** |
| **AC-S6** (custom time honors quiet-hours/goal-met) | `test_ac_s6_custom_time_reminder_still_honors_that_users_quiet_hours` (Luna, owner) + `test_quiet_isolation_between_two_active_members` (Vera, two ordinary members) | **PASS** |
| **AC-O1** (health alerts owner-only) | `test_ac_o1_health_alert_reaches_only_the_owner_even_with_other_active_users` (Luna) | **PASS** |
| **AC-X1** (sequential inbound processing) | `test_ac_x1_inbound_messages_from_two_users_are_processed_sequentially` (Luna) | **PASS** |

Plus, not §11-AC-numbered but explicit in this pass's own mandate: AC-A1 (Vera: exactly-once-notification/zero-log/zero-LLM), AC-A3 (Vera: blocked denial through real wiring), AC-A7-adjacent (Luna: gate fails safe through real wiring), AC-P1 (Luna: reply threading; Vera: reminder/summary/review threading + auto-detect-unaffected), AC-P2 (Vera: two-member quiet isolation), AC-U3 (Vera: daily-summary fan-out + skip, through the real job), `display_name` (Luna + Vera), command menu (Luna).

## Failures
None. 31/31 in `tests/test_v12_integration.py`; 1325/1326 in the full repo suite (the 1 non-pass is the pre-existing, unrelated architectural-boundary *skip*, not a failure).

## Regressions detected
None. Full repo suite: 1325 passed / 0 failed / 1 skipped, up from the stated 1307/0/1 baseline by exactly this pass's 18 new tests. Every pre-existing test file, including every prior v1.2 module's own suite (`test_access.py`, `test_v12_access_gaps.py`, `test_preferences.py`, `test_schedules.py`) and the full v1.1-era suite, stays green.

## Notable findings during this pass (both resolved, documented for the record)
1. **My own initial test bug (not a product bug):** `_query_intent`'s JSON originally used the key `"habit_id"`; `core/query.py:_validate_intent` actually reads it under `"category"` (matching the extraction schema's field name, distinct from `QueryIntent`'s own dataclass field name). Fixed in the test helper before this report; noted here only because it's exactly the kind of key-name mismatch worth flagging if it recurs elsewhere.
2. **Default language for unprompted sends is Thai, not English**, for any user (owner included) who has never run `/lang` — `config.i18n.primary_language` defaults to `"th"` (`core/i18n.py`'s own documented, intentional resolution for "auto" with no inbound message to detect from). This surprised my first draft of the daily-summary isolation test (which assumed English) — not a defect, just documented here so nobody else re-discovers it as a false alarm.
3. **`/users` never shows `display_name`** — see punch-list item 6 above. Matches SPEC-v1.2.md §3.3's own example; recorded as a clarification, not a gap.

## Recommendation
**PASS — ready to ship. This gates the v1.2.0 release.**

All 11 integration-owned ACs (M1–M3, C1, C2, U-ISO, S1, S4, S6, O1, X1) pass through the REAL wired `on_message`/`on_callback`/`async_main` path — not module-level direct calls. Every item on the coordinator's 8-point adversarial punch list was independently tested and passes, including the two highest-severity security probes (forged cross-user callback from a non-active attacker; the safety-net no-op for a data-corruption edge Luna found and fixed). One assumption in the punch list (display_name in `/users`) was checked and found not to match the actual/spec-conformant behavior — documented above, not a blocker. Zero regressions across the full 1325-test suite. No production code was touched by this pass.
