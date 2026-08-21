# Test Report — v1.1.0 integration (undo menu + per-habit targets)

## Summary
- Scope: the 13 shared/integration-owned ACs from `SPEC-v1.1.md` §11 (AC3, AC4, AC6, AC10, AC12,
  AC21, AC22, AC23, AC24, AC25, AC26, AC31, AC33) **plus** the end-to-end halves of AC1, AC2, AC5
  (previously module-level-only per `IMPL-v1.1-undo-ui.md`/`TEST-v1.1-undo-ui.md`), which
  `IMPL-v1.1-integration.md`'s wiring now makes fully testable. 16 ACs total in this report.
- New/repaired tests this pass: `tests/test_v11_integration.py` (12 new tests), plus 6 date-drift
  flakes repaired in `tests/test_adaptive_reminders.py` (3) and `tests/test_v09_gaps.py` (3).
- Full repo suite: **881 passed, 0 failed, 1 skipped** (the 1 skip is the pre-existing, expected
  `tests/test_channels.py:232` "only core/ wires the Channel ABC directly" skip — not a gap).
  Confirmed stable across 2 consecutive full runs.
- Status: **PASS**

## Test files

| Path | Tests added/changed | Covers |
|---|---|---|
| `tests/test_v11_integration.py` (new) | 12 | AC1, AC2, AC3, AC4 (×2), AC5, AC6 end-to-end; AC29/AC30 routing order (×2); AC31 full round-trip; AC33 (×2) |
| `tests/test_adaptive_reminders.py` (repaired) | 3 of 14 fixed (date-drift → frozen clock) | Regression guard only — not new AC coverage, restores a previously-green pre-v1.1 suite |
| `tests/test_v09_gaps.py` (repaired) | 3 of 28 fixed (date-drift → frozen clock) | Regression guard only — same as above |

Not re-tested here (already covered and unaffected by this pass, confirmed via the full-suite green run): AC7–AC11 (`tests/test_undo_ui.py`), AC13–AC20/AC27–AC30/AC32/AC34 (`tests/test_targets.py`/`tests/test_target_nl.py`), AC21–AC23/AC25/AC26/reminder-streak half of AC31 (`tests/test_v11_shared_surface.py`), AC10/AC12 (`tests/test_channels.py`/`tests/test_migrations.py`).

## AC coverage

| AC | Test(s) | Status |
|---|---|---|
| AC1 | `test_ac1_startup_registers_undo_and_target_in_both_languages` — drives the real `async_main`, captures the merged `set_my_commands` dict, asserts `/undo` and `/target` both present in `en` and `th`, and that the two languages' copy actually differs | **PASS** (end-to-end; was module-level-only) |
| AC2 | `test_ac2_set_my_commands_transport_error_at_startup_never_crashes` — fake channel's `set_my_commands` raises `ConnectionError`; `async_main` still reaches `channel.run()` | **PASS** (end-to-end; was module-level-only) |
| AC3 | `test_ac3_log_confirmation_carries_exactly_one_undo_button_with_correct_id` (direct `handle_inbound_message`) + `test_ac6_callback_query_routes_through_real_on_callback_and_soft_deletes` (via real `async_main`) | **PASS** |
| AC4 | `test_ac4_clarifying_question_carries_no_button`, `test_ac4_deferred_ack_carries_no_button` + static verification: `grep` confirms `core/reminders.py`, `core/health.py`, and `main.py`'s `weekly_review_job`/`daily_summary_job` call only plain `channel.send`, never `send_actionable` | **PASS** |
| AC5 | `test_ac5_milestone_crossing_confirmation_carries_both_suffix_and_button` — a real 3-day milestone crossing through `handle_inbound_message`; one `send_actionable` call, milestone line in the text, exactly one button | **PASS** (end-to-end; was module-level-only) |
| AC6 | `test_ac6_callback_query_routes_through_real_on_callback_and_soft_deletes` — real `async_main`'s `on_callback` closure, fed the exact `callback_data` captured off a real confirmation's button, soft-deletes the real DB row; a second `on_message` afterward proves callback routing didn't break normal message handling | **PASS** (end-to-end; channel-level `_offset`/`answerCallbackQuery` mechanics already covered by `tests/test_channels.py`, unaffected by this pass) |
| AC10 | (unchanged — `tests/test_channels.py`) | **PASS** (re-confirmed via full-suite green) |
| AC12 | (unchanged — `tests/test_migrations.py`) | **PASS** (re-confirmed via full-suite green) |
| AC21 | (unchanged — `tests/test_v11_shared_surface.py`) | **PASS** |
| AC22 | (unchanged — `tests/test_v11_shared_surface.py`) | **PASS** |
| AC23 | (unchanged — `tests/test_v11_shared_surface.py`) | **PASS** |
| AC24 | Full suite: 881 passed / 0 failed / 1 skipped, no override present anywhere unintended | **PASS** |
| AC25 | (unchanged — `tests/test_v11_shared_surface.py`) | **PASS** |
| AC26 | (unchanged — `tests/test_v11_shared_surface.py`) | **PASS** |
| AC31 | Reminder-skip/streak half: `tests/test_v11_shared_surface.py` (unchanged). Full round-trip via real routing: `test_ac31_nl_set_goal_on_goalless_habit_then_consumers_reflect_it_end_to_end` — a full-NL intent sets `stretch`'s goal through real `handle_inbound_message` routing (writes no log row), then a subsequent stretch log immediately qualifies via the real `streaks.day_qualifies`/`compute_daily_summary` | **PASS** |
| AC33 | `test_ac33_ollama_down_skips_nl_step_entirely_and_defers_as_unparsed_log` (spies on both `looks_like_target_phrasing` and `classify_target_intent` to prove neither is even called while down; message persists as `category='unparsed'`) + `test_ac33_deterministic_target_command_still_works_during_ollama_outage` (`/target water 2500` still sets the override with the same health monitor down) | **PASS** |

Also verified (not separate ACs, but explicitly requested wire-up checks):
- **Message → NL-gate → parser ordering**: `test_nl_target_hit_short_circuits_before_parser_and_writes_no_log_row` (a spy on `parse_message` raises if called; an NL hit never reaches it) and its converse `test_gate_miss_falls_through_to_parser_and_logs_normally` (an ordinary log message DOES reach `parse_message` and IS written) — both pass.
- **A NL target hit writes no log row**: same test above, plus the AC31 round-trip test — both assert `SELECT COUNT(*) FROM logs == 0` immediately after the NL-set reply.
- **Callback routing doesn't break normal message handling**: `test_ac6`'s tail sends a second, ordinary `"500ml"` message through the real `on_message` closure after the callback fires, and asserts it still gets a normal confirmation with its own button.

## Deviations review (`IMPL-v1.1-integration.md` §"Known limitations / deviations")

1. **Local `TARGET_COMMAND_DESCRIPTIONS` in `main.py`** (since `core/targets_command.py` never grew its own `command_menu_entries()`). Reviewed: sound. `grep` confirms no such function exists in `targets_command.py`; `main.py`'s dict mirrors `undo_ui.UNDO_COMMAND_DESCRIPTIONS`'s own "no i18n catalog key for Bot API menu copy" rationale exactly. `test_ac1_startup_registers_undo_and_target_in_both_languages` proves the merged output is correct and bilingual. Not a scope violation — this is exactly the kind of glue the integration step owns.
2. **Six repaired duck-typed test-double "channel" classes** (`test_charts.py`, `test_reminders.py`, `test_streaks.py`, `test_fallback.py`, `test_resilience.py`, and their `_FakeTelegramChannel`/`_RecordingChannel` fakes gaining `send_actionable`/`set_my_commands`). Reviewed: sound. All 6 files' full test suites pass cleanly (confirmed via the full-suite green run); the fix pattern (adding the missing methods directly, degrading to `send` exactly like the `Channel` ABC's own defaults) is consistent with `channels/base.py`'s documented degrade-to-`send` contract and doesn't change any of those files' other assertions.

## Regressions detected
None after fixes. Before this pass: `tests/test_adaptive_reminders.py::test_send_reminder_skipped_when_goal_already_met`, `::test_send_reminder_goal_exactly_met_is_skipped`, `::test_send_reminder_updates_state_only_when_actually_sent`, and `tests/test_v09_gaps.py::test_goal_met_reminder_skipped_via_real_scheduled_job_and_logged`, `::test_goal_exactly_met_is_skipped_via_real_scheduled_job_matching_documented_ge`, `::test_skip_if_goal_met_false_disables_only_that_habit_others_still_skip` were failing — all 6 seeded a hardcoded `"2026-08-19T09:00:00"` log timestamp and relied on `core/reminders.py:_today_str`'s real `datetime.now(...)` landing on that same date; as real time moved past 2026-08-19 the seeded log stopped being "today", so the goal-met skip silently stopped firing. Root cause confirmed identical in both files (not a logic bug in `_goal_already_met`/goal resolution — pure date drift).

**Fix applied** (test files only, no production code touched): both files already had a `_freeze_reminders_clock(monkeypatch, hour, minute)` helper (used pre-v1.1 only for the quiet-hours tests) that monkeypatches `core/reminders.py`'s own `datetime.now(tz)` via a fixed-clock subclass. Extended it to accept an optional `day` (defaulting to real `date.today()` instead of a hardcoded literal), and added a `_frozen_today_ts(hour)` helper returning an ISO timestamp on that same frozen "today". The 6 repaired tests now call `_freeze_reminders_clock(monkeypatch, <hour>, <minute>)` and seed their water/sleep logs via `_frozen_today_ts(...)` instead of the hardcoded string — so the seeded log's date and `_today_str`'s "today" are always the same value, by construction, regardless of when the suite runs. The pre-existing quiet-hours tests that already called `_freeze_reminders_clock` are unaffected (they only care about time-of-day, not date) and continue to pass. Verified stable across 2 consecutive full-suite runs.

## Recommendation
**Ready to ship.** All 16 ACs in this report's scope — including the previously-deferred end-to-end halves of AC1/AC2/AC5, the full-round-trip half of AC31, and AC33's outage-routing guarantee — pass against the real `main.py` wiring, not mocks of it. Both documented integration deviations (local target command-menu dict, six repaired test doubles) were reviewed and are sound, contained to test infrastructure or intentional glue code, with no spec or behavioral gap. The 6 date-drift flakes are repaired (test-only change, same clock-injection pattern the codebase already used for quiet-hours) and verified stable. Full suite: **881 passed, 0 failed, 1 skipped** (the one skip is the pre-existing, expected `Channel`-ABC-ownership skip). This gates the v1.1.0 undo+targets release; the `discoverability` module (`/help`, `/habits`) is out of scope for this pass — it hasn't been built yet.
