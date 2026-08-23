# Test Report — v1.5.0 `announce` module (release announcements)

## Summary
- Module-scope tests (`test_announce.py` + `test_announce_gaps.py`): **43 total** — 42 passed, 1 failed
- Full project suite (`tests/`): **1936 total** — 1934 passed, 1 failed, 1 skipped
- Status: **FAIL** (1 finding — see below; all 4 numbered ACs owned by this module pass)
- Regressions: **none** — the only failure is a brand-new adversarial test Vera added; every pre-existing test (including the full access-related suite) is unchanged and green

## Baseline reconciliation
Dispatch baseline was **1663 passed / 0 failed / 1 skipped** (announce module only, before `checkins`/`preparse` landed). This tree already has `checkins`/`preparse`/`dnd_matrix`/`units` modules landed (untracked files present at session start), so:
- Before adding this pass's tests: **1912 passed / 0 failed / 1 skipped** — consistent growth from 1663 (+249 tests from the concurrently-landed `checkins`/`preparse` tracks, out of this module's scope, all green, not touched).
- After adding `tests/test_announce_gaps.py` (23 new tests): **1934 passed / 1 failed / 1 skipped**. The single failure is `test_concurrent_overlapping_calls_send_at_most_once_per_user`, a new test, not a regression.

Access-related suite re-run in isolation (per dispatch instruction to verify no other `access.py` behavior changed): `test_access.py`, `test_v12_access_gaps.py`, `test_v12_integration.py`, `test_audit.py`, `test_audit_capture.py`, `test_audit_capture_gaps.py` → **222 passed**, matching Luna's own IMPL-v1.5-announce.md figure exactly.

## Test files
| Path | Tests added | Covers |
|---|---|---|
| `tests/test_announce.py` (Luna's, pre-existing) | 20 | AC-20, AC-21, AC-22, AC-23, catalog shape, structural LLM-free/no-DND proofs |
| `tests/test_announce_gaps.py` (Vera's, new) | 23 | AC-20, AC-21, AC-22, AC-23 — adversarial/gap angles: partial-failure fan-out, version-comparison semantics, catalog integrity, concurrency, invite-branch write failure, blocked-then-reapproved |

## AC coverage
| AC | Description | Test(s) | Result |
|---|---|---|---|
| AC-20 | Announce on new version, per-user language, marked on success | `test_ac20_active_user_receives_note_and_is_marked`, `test_ac20_per_user_language`, `test_null_last_announced_version_pre_v15_user_receives_current_note` | **PASS** |
| AC-21 | Once per version per user; failed send left unmarked and retried | `test_ac21_already_at_version_is_skipped`, `test_ac21_failed_send_leaves_unmarked_and_is_retried_next_call`, `test_ac21_db_read_error_on_the_gate_check_fails_open_and_still_sends`, `test_fan_out_one_user_fails_others_sent_and_marked`, `test_fan_out_one_user_fails_next_run_resends_only_that_user`, `test_fan_out_all_users_fail_nobody_marked_no_crash`, `test_fan_out_all_users_fail_then_all_succeed_no_duplicates`, `test_double_call_in_a_row_sends_at_most_once_per_user` | **PASS** (sequential retry semantics hold — see "Additional finding" below for a related-but-distinct concurrent-call race not covered by AC-21's literal text) |
| AC-22 | No catalog entry → no sends, never raises | `test_ac22_no_catalog_entry_sends_nothing_and_never_raises`, `test_get_release_note_unknown_version_returns_none_never_raises`, `test_get_release_note_malformed_version_arguments_never_raise`, `test_get_release_note_unrecognized_language_returns_none` | **PASS** |
| AC-23 | Newly-approved user caught up to current version; later releases still reach them | `test_ac23_approve_catches_a_newly_approved_user_up_to_the_current_version`, `test_ac23_invite_is_the_same_alias_and_also_catches_up`, `test_ac23_a_later_version_bump_does_announce_to_a_caught_up_user`, `test_ac23_block_does_not_touch_last_announced_version`, `test_ac23_approve_write_failure_does_not_crash_and_still_reports_save_failed`, `test_ac23_invite_write_failure_does_not_crash_and_still_acks`, `test_blocked_then_reapproved_user_gets_no_back_announcement_but_future_release_reaches_them` | **PASS** |
| AC-24 | Audience + DND + latest-only | Not owned by this module (§11: "verified at the startup-loop integration") | **N/A — out of scope**, unit-level pieces it depends on are pinned: `test_pending_and_blocked_users_receive_nothing`, `test_latest_version_only_no_rollup_for_a_user_several_versions_behind`, `test_announce_module_never_calls_in_dnd_now` |

Every numbered AC this module owns (AC-20, AC-21, AC-22, AC-23) is **PASS**.

## Failures (if any)

### `test_concurrent_overlapping_calls_send_at_most_once_per_user`
- **What was tested:** two `announce_release(db, channel, config, "1.5.0")` calls launched concurrently via `asyncio.gather` against the *same* `db`/`channel`, simulating a double-startup race rather than a clean sequential retry. The fake `channel.send` includes a real `await asyncio.sleep(0)` yield point (a `FakeChannel` with no true suspension point lets one call run to completion before the other starts even under `gather`, masking the race — a real Telegram HTTP send always has such a yield point).
- **AC violated:** No single numbered AC states this explicitly. Closest: **AC-21** / **R-N2**/**R-N3** ("marking-on-success gives idempotency — a restart at the same version announces nothing more"), which the spec frames around *sequential* startups. This finding is the literal "startup racing" adversarial angle from the dispatch ("announce_release called twice in a row (double startup, or startup racing) — exactly one send per user total").
- **Input:** Two concurrent calls to `announce_release` for the same never-announced `OWNER`/`MEMBER` pair, same DB, same channel.
- **Expected:** Each user receives at most one send total across both overlapping calls (per the dispatch's explicit "exactly one send per user total" requirement).
- **Actual:** `OWNER` received 2 sends.
- **Stack trace / output:**
  ```
  AssertionError: OWNER received 2 sends from two concurrent announce_release calls
  assert 2 <= 1
  ```
  Reproduced deterministically 5/5 runs (not flaky — CPython's cooperative asyncio scheduling makes the interleaving order repeatable for this shape of test).
- **Suspected cause:** `src/habit_assistant/core/announce.py:68-103` — the per-user "already announced?" read (`db.get_last_announced_version(user_id) == version`, line 70) and the "mark as announced" write (`db.set_last_announced_version(user_id, version)`, line 103) are not atomic and hold no lock. Under two overlapping `announce_release` invocations, both tasks can pass the gate-check for the same user before either has sent (because `await channel.send(...)` at line 93 is a genuine suspension point for any real network channel), so both proceed to send. This is a textbook TOCTOU race, not a logic bug in the single-invocation path — the sequential-retry case (AC-21 as literally written, and this report's `test_double_call_in_a_row_sends_at_most_once_per_user`) is unaffected and passes.

**Why this is flagged as "Escalate" rather than "send back to Luna":** the spec never states whether `announce_release` must be safe under *concurrent* invocation, or whether that safety is assumed to come from the surrounding process architecture instead (the project's Task Scheduler launcher already enforces a single-instance guard per the team's own ops notes — if two overlapping startups of the *same process* can never happen, this race may be unreachable in production and the fix would be unnecessary defensive code). This is a design-intent question SPEC-v1.5.md doesn't answer, not an ambiguity Vera should resolve by inventing an answer.

## Regressions detected
None. Full suite before this pass: 1912 passed / 0 failed / 1 skipped. After adding `test_announce_gaps.py`: 1934 passed / 1 failed / 1 skipped — the delta is entirely the one new finding above; every previously-passing test (1912, including the full access-related suite re-run separately at 222 passed) is still green.

## Recommendation
**Escalate to Archi — spec gap discovered.** All four ACs this module owns (AC-20, AC-21, AC-22, AC-23) pass cleanly, including every adversarial angle from the dispatch except one: whether `announce_release` must be safe under truly concurrent/overlapping invocation. The spec (R-N2/R-N3) only characterizes sequential-startup idempotency, which holds. Archi/the user should decide: (a) accept the race as unreachable given the existing single-instance-guard process architecture and close this with a documented known-limitation note, or (b) have Luna add a lock (e.g. an `asyncio.Lock` per `announce_release` call, or a `SELECT ... WHERE last_announced_version != ?` compare-and-set at the DB layer) to make the function safe under concurrent callers regardless of process topology. Either way, this should not block shipping v1.5.0 on its own — it does not affect any of the four numbered ACs and only manifests under a call pattern (two truly overlapping `announce_release` invocations) that the single-instance-guarded startup path does not currently produce.
