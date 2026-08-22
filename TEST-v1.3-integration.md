# Test Report — v1.3.0 integration (audit log — final release gate)

## Summary
- Scope: integration-owned ACs per SPEC-v1.3.md §11 — **AC-A1, AC-A2, AC-A3, AC-R1, AC-C2, AC-C7, AC-V3** — plus re-confirmation of both parallel modules (`audit-capture`, `audit-view`) through the REAL wired `on_message`/`on_callback`/`async_main` path.
- Total (`tests/test_v13_integration.py`): **22 tests** (8 Luna + **14 new, this pass**).
- Passed: 22 / 22 (file scope).
- Full repo suite: **1511 passed, 0 failed, 1 skipped** (baseline going in: 1497 passed / 0 failed / 1 skipped — independently re-confirmed before starting; delta = +14, exactly this pass's additions, twice in a row on re-run). The 1 skip is the same pre-existing conditional architectural-boundary check every prior pass has carried (`tests/test_channels.py:232`).
- Status: **PASS**.
- **RELEASE GATE VERDICT: PASS.** v1.3.0 is ready to ship.

## Test files
| Path | Tests added (this pass) | Covers |
|---|---|---|
| `tests/test_v13_integration.py` | 14 new (of 22 total) | AC-A2, AC-A3, AC-R1, AC-V3 (owner-gate hardening), plus the coordinator's 7-point punch list below |

No production code was modified. All changes are additive to the existing `tests/test_v13_integration.py` (three new imports — `logging`, `sqlite3`, `LogEntry` — and 14 new test functions/parametrizations appended; no change to the file's shared harness). No test opens `data/habits.db` or touches the live Task Scheduler service; every DB is a scratch `tmp_path` SQLite file; the LLM is always the file's existing `_FakeOllamaClient` double, and `target_nl.classify_target_intent` is monkeypatched directly where an NL path is exercised (this file's own established precedent).

## Punch-list — per item

### 1. Owner gate security
- **Non-owner/pending/blocked/unknown chats sending `/audit`** → `test_audit_from_every_non_owner_state_never_reads_audit_data` (parametrized ×4: unknown, pending, blocked, active-member). **Important precision finding, not a defect:** because `access.handle_gate` (R-A1) gates every inbound update BEFORE dispatch, only an **active non-owner member** ever reaches `/audit`'s own owner re-check and gets the true "silent no-op" (zero reply). An **unknown/pending/blocked** chat sending `/audit` never reaches the audit-kind check at all — it gets the ordinary v1.2 onboarding/denial reply (`access_pending`/`access_denied`), same as any other message from that chat would. All four states share one hard, structurally-proven guarantee: `Database.recent_audit` (a raising double) is **never invoked** in any of them — not "the reply doesn't show data," literally never called. **PASS**, with the coordinator's framing refined for the record.
- **Forged/edge shapes** — `/audit 50` from a member (folded into the parametrized test above, using a high explicit N as the actual message in all four cases) and the Thai alias `ประวัติ` from a stranger (`test_audit_thai_alias_from_a_stranger_triggers_onboarding_not_audit_data`) — and, the converse, `ประวัติ 5` from an already-active member (`test_active_member_audit_thai_alias_is_also_silent`), confirming the Thai alias isn't a less-guarded second path into the view. **PASS.**
- **Owner impersonation angles** — audited by code inspection first: `role="owner"` is written in exactly one place (`attribute_legacy_to_owner`, startup-only, sourced from `.env`'s `secrets.telegram_chat_id`); no in-chat command (`/approve`, `/invite`, or any other) ever writes the `role` column. `test_member_cannot_impersonate_owner_or_leak_audit_via_any_exposed_command` fires a barrage of self-privileged attempts from an ordinary member (self-`/approve`, self-`/invite`, `/users`, `/audit`, `/audit 50`) and confirms: zero replies, `role` stays `"member"`, `access.classify` never returns `"owner"`, and not one attempt writes an audit row. **No viable impersonation vector found — PASS, locked in as a regression test rather than left as an unverified claim.**

### 2. End-to-end capture correctness
`test_two_user_session_capture_attributes_correctly_and_owner_audit_shows_actor_and_you`: a realistic OWNER + MEMBER + brand-new-chat session through the real wiring. Verified:
- The member's own `/target`, `/remind`, `/lang` actions all record `user_id = <member's chat id>`, never the owner's.
- The owner's `/approve <newcomer>` records `user_id = <owner>`, `target_user_id = <newcomer>` — actor and target correctly split.
- The newcomer's very first (unknown→pending) message records `user_pending` with `user_id = target_user_id = <newcomer>`, and — re-confirming AC-P1 through the real gate, not just capture's own direct-call tests — the newcomer's actual message text ("this is a private message with PII in it") appears **nowhere** in the audit row (`new_value` is literally `"pending"`).
- The owner's own `/audit` reply shows "you" **exactly once** (only the `/approve` row is the owner's own action), both other chat ids appear by raw id (no `display_name` captured), and newest-first ordering holds (the last action taken — `/approve` — is the first line after the header). **PASS.**

### 3. AC-A2 fail-open, wired level
`test_audit_write_failure_emits_a_log_line_and_a_later_action_records_normally`: a flaky `Database.insert_audit` double fails on its first call, then recovers. Verified in one test:
- **Action succeeds, reply unchanged** — both `/target water 2000`'s and `/lang th`'s own replies and DB writes (`get_target`, `language_pref`) are completely unaffected by the audit failure.
- **A log line IS emitted** — `core/audit.py:record`'s own `logger.exception("Audit record failed...")` fires exactly once, naming `target_set`, captured via `caplog`. (Needed one harness fix to observe: `main.py`'s own `setup_logging()` calls `logging.basicConfig(force=True)` during a real `async_main` startup, which tears down pytest's `caplog` handler — worked around by monkeypatching `setup_logging` to a no-op for this one test, since console formatting is irrelevant to what's being verified.)
- **Mid-session recovery** — the SECOND action's audit write (no longer forced to fail) succeeds normally: exactly one row exists afterward (`lang_set`), and the earlier failure left no partial/corrupt row and did not poison the recorder for later calls. **PASS.**

### 4. AC-A3 byte-identical gate
`test_ac_a3_spot_check_confirmation_and_undo_text_unchanged_by_audit` and `test_ac_a3_spot_check_reminder_text_unchanged_by_audit`: exact v1.2-era strings, re-confirmed through the audit-wired code path —
- Water confirmation: `"✅ 500 ml logged — today 500 / 2500 ml (20%)"` — byte-identical to `tests/test_v12_integration.py`'s own pin.
- Undo confirmation: starts with `"↩️ Undone"`, shows `"0 / 2500 ml (0%)"` post-removal.
- Reminder text: exact Thai `reminder_water` catalog string (unprompted sends with no `/lang` override resolve to `config.i18n.primary_language`, Thai by default — the same v1.2-era behavior, not audit's doing; my first draft wrongly assumed English and was corrected before this report).
- Each test also confirms the *new* side-effect DID happen (an `undo`/no audit row for the reminder respectively) — proving audit is genuinely wired alongside the unchanged reply, not merely "silent because broken." **PASS.**

### 5. AC-R1 retention
- **`retention_days = 0`** → `test_retention_days_zero_prunes_nothing_at_startup`: a ~10-year-old audit row survives a real `async_main` startup untouched (the `if config.audit.retention_days > 0:` guard skips pruning entirely). **PASS.**
- **Exact boundary** → `test_prune_audit_boundary_row_exactly_at_cutoff_survives_one_second_older_is_pruned`. `db.prune_audit`'s own SQL is `WHERE ts < cutoff_ts` (strict inequality) — a row exactly AT the cutoff must survive; only strictly-older rows are deleted. Exercised directly against `db.prune_audit` (the exact method `main.py`'s startup call invokes) with a caller-chosen deterministic cutoff string, **not** through the full `async_main` wiring — racing `async_main`'s own internal `datetime.now()` call to land a row at an exact wall-clock instant would be flaky by construction (there is no injectable clock at that call site). Three rows (exactly-at, one-second-older, one-second-newer): exactly the older one is pruned; both the at-cutoff and newer rows survive. **PASS.**

### 6. Migration rehearsal
`test_migration_007_rehearsal_on_a_v1_2_shaped_scratch_db`: hand-builds a raw sqlite3 DB matching the exact v1.2-era (post-006) schema — `users`, `logs.user_id`, per-user `habit_targets`, `user_reminder_times`, `user_version=6`, no `audit_log` table — with a real pre-existing owner water log and target override, at the exact path `async_main` will open. Runs the REAL startup (migration 007 + the existing v1.2 attribution/prune sequence) and drives three real messages. Verified: schema lands at version 7; the pre-existing v1.2 data is still fully readable/correct through production code (`/habits` shows the legacy total); `audit_log` starts genuinely empty (only ONE row exists after the rehearsal — the one real post-upgrade `/target` action, not any phantom migration-time entry); and that row's old/new values correctly reflect the pre-existing target override (3000 → 2500) it read before overwriting. **PASS — closest rehearsal of the actual production upgrade this suite can give.**

### 7. Anything §11 assigns to integration not yet covered
Cross-checked SPEC-v1.3.md §11's full "ACs verified during shared-surface/integration" list (AC-A1, AC-A2, AC-A3, AC-R1, AC-C2, AC-C7, AC-V3) against both this pass's new tests and the pre-existing 8: every one has direct wired-level coverage (AC-A1 also independently covered at the pure-migration level by `tests/test_migrations.py::test_v6_shaped_db_migrates_to_v7_audit_log_touching_nothing_existing`, re-exercised here through the full app in item 6 above). No gap found.

## AC coverage (integration-owned, SPEC-v1.3.md §11)
| AC | Test(s) | Status |
|---|---|---|
| **AC-A1** (migration 007, additive, idempotent) | `tests/test_migrations.py::test_v6_shaped_db_migrates_to_v7_*` (pre-existing) + `test_migration_007_rehearsal_on_a_v1_2_shaped_scratch_db` (this pass, through the real app) | **PASS** |
| **AC-A2** (fail-open) | `test_audit_db_failure_leaves_the_triggering_actions_reply_and_write_unchanged` (Luna) + `test_audit_write_failure_emits_a_log_line_and_a_later_action_records_normally` (Vera: log-line + mid-session recovery) | **PASS** |
| **AC-A3** (regression gate) | Full 1511-test suite green + `test_ac_a3_spot_check_confirmation_and_undo_text_unchanged_by_audit`, `test_ac_a3_spot_check_reminder_text_unchanged_by_audit` (Vera) | **PASS** |
| **AC-R1** (retention) | `test_startup_prune_correct_with_a_realistic_mixed_capture_volume` (Luna, 365-day window) + `test_retention_days_zero_prunes_nothing_at_startup`, `test_prune_audit_boundary_row_exactly_at_cutoff_survives_one_second_older_is_pruned` (Vera) | **PASS** |
| **AC-C2** (edit, recorded in `main.py`) | `test_full_flow_nl_target_then_log_then_edit_then_owner_audit_sees_all_newest_first` (Luna) + `test_two_user_session_capture_attributes_correctly_and_owner_audit_shows_actor_and_you` (Vera, cross-user) | **PASS** |
| **AC-C7** (not-audited property) | `test_plain_habit_log_and_read_only_commands_write_no_audit_row` (Luna) + `test_ac_a3_spot_check_reminder_text_unchanged_by_audit` (Vera, reminders are also not a capture site) | **PASS** |
| **AC-V3** (owner-only + hidden) | `test_non_owner_audit_is_silent_and_reveals_nothing`, `test_audit_never_added_to_the_public_command_menu` (Luna) + the 4-state parametrized test, both Thai-alias tests, and the impersonation probe (Vera) | **PASS** |

## Failures
None. 22/22 in `tests/test_v13_integration.py`; 1511/1512 in the full repo suite (the 1 non-pass is the pre-existing, unrelated architectural-boundary *skip*, not a failure).

## Regressions detected
None. Full repo suite: 1511 passed / 0 failed / 1 skipped, up from the stated 1497/0/1 baseline by exactly this pass's 14 new tests, reproduced identically across two consecutive full runs.

## Notable findings during this pass (both resolved before this report, recorded for the log)
1. **My own test-drafting bugs, not product bugs:** two of my first-draft assertions assumed English where the actual (correct, unchanged-since-v1.2) behavior is Thai — (a) an unprompted reminder send with no stored `/lang` preference resolves "auto" to `config.i18n.primary_language` (Thai by default); (b) a reply to an inbound message that itself contains Thai characters (the `ประวัติ` alias) auto-detects Thai, not English. Both fixed before this report; flagged only so nobody re-discovers them as false alarms (the same class of gotcha `TEST-v1.2-integration.md` already documented once).
2. **`caplog` needs a harness workaround for a full `async_main` run**: `main.py`'s `setup_logging()` calls `logging.basicConfig(force=True)`, which strips pytest's `caplog` handler off the root logger. Worked around locally (monkeypatch `setup_logging` to a no-op) for the one test that needed to observe a log line through a real startup; no other test in this file needed it.
3. **Owner-gate precision** (punch-list item 1): the coordinator's framing groups "non-owner, pending, blocked, unknown" together as all producing a "silent no-op" for `/audit`. Only the active-non-owner-member case is actually silent; unknown/pending/blocked chats never reach the audit-kind check at all (the v1.2 gate intercepts them first with its own onboarding/denial reply). Not a defect — this is the CORRECT, spec-conformant behavior (R-A1's gate-before-dispatch ordering) — recorded here so the distinction is explicit rather than assumed uniform.

## Recommendation
**PASS — ready to ship. This gates the v1.3.0 release.**

All 7 integration-owned ACs (A1, A2, A3, R1, C2, C7, V3) pass through the REAL wired `on_message`/`on_callback`/`async_main` path — not module-level direct calls. Every item on the coordinator's 7-point punch list was independently tested and passes, including the two highest-value security/safety probes (a comprehensive owner-impersonation sweep finding no viable vector, and fail-open verified with both an observable log line and a same-session recovery). Zero regressions across the full 1511-test suite, reproduced on two consecutive runs. No production code was touched by this pass.
