# Test Report — v1.1.0 `undo-ui` module (undo discoverability)

## Summary
- Total: 38 tests in `tests/test_undo_ui.py` (36 by Luna + 2 boundary tests added by Vera in this re-verification pass)
- Passed: 38
- Failed: 0
- Status: **PASS** (AC1/AC2/AC5 remain DEFERRED to integration, as designed — see below)

**Re-verification note:** this report was updated after Luna fixed the `OverflowError`
finding below. Her fix: `_SQLITE_MAX_INTEGER = 2**63 - 1` bounds check in
`core/undo_ui.py`, applied right after parsing `data` (before `db.get_log` is ever
called), rejecting out-of-range ids identically to a regex-malformed payload. She chose
a bounds check over a `\d{1,18}` regex cap so a legitimate 19-digit id up to the SQLite
max still resolves correctly (verified below with an explicit boundary test). Vera's
original 3 tests were left unmodified per the coordinator's note; the original failing
test now passes as a regression guard, and 2 new boundary tests were added to pin the
exact edge (max valid int64 vs. one past it).

Scope is the `undo-ui` module only (SPEC-v1.1.md §11): AC1, AC2, AC5, AC7, AC8, AC9, AC11.
`main.py` wiring is deliberately deferred to integration per spec §11 — AC1/AC2/AC5 are
verified at the module-level "building block" per the dispatch instructions, not end-to-end.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_undo_ui.py` (Luna) | 33 | AC1 (building block), AC5 (building block), AC7, AC8, AC9, AC11 |
| `tests/test_undo_ui.py` (Vera, round 1) | 3 | AC9 hardening (`undo:-1`, astronomically large id — found the `OverflowError`), AC8 cross-path idempotency |
| `tests/test_undo_ui.py` (Vera, round 2 — re-verification) | 2 | AC9 boundary pin: `9223372036854775807` (max valid int64, in range) vs. `9223372036854775808` (one past max, out of range) |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC1 | `test_command_menu_entries_has_undo_for_both_languages` | **PASS (module-level)** — `command_menu_entries()` returns correctly localized `/undo` entries for `en`+`th`. **DEFERRED to integration**: the actual `set_my_commands` call at startup, and merging with `targets_command`'s `/target` entries, live in `main.py`'s integration wiring (not yet landed — confirmed absent by reading `main.py`). Module-level building block is correct. |
| AC2 | documented in IMPL "Wiring for the integrator" | **DEFERRED to integration** — the `try/except` around the startup `set_my_commands` call is `main.py`'s own code, not expressible at this module's boundary. Nothing to test yet; correctly flagged by Luna rather than claimed done. |
| AC5 | `test_button_and_milestone_suffix_travel_as_one_actionable_message` | **PASS (module-level)** — a single `send_actionable` call carries both the milestone-suffixed text and exactly one button. **DEFERRED to integration** for the real end-to-end milestone-crossing path (needs `main.py`'s confirmation call sites switched to `send_actionable`). |
| AC7 | `test_handle_undo_callback_soft_deletes_and_sends_confirmation`, `test_handle_undo_callback_detects_thai_language_from_source_text`, `test_handle_undo_callback_forced_language_overrides_source_text` | **PASS** |
| AC8 | `test_handle_undo_callback_already_deleted_sends_already_undone_and_no_second_delete`, `test_handle_undo_callback_missing_row_sends_already_undone`, **+Vera:** `test_handle_undo_callback_idempotent_after_text_undo_command` (cross-path: `/undo` text command then button-tap on the same row) | **PASS** |
| AC9 | Luna's 7-way `@pytest.mark.parametrize` (`"foo"`, `"undo:abc"`, `"undo:"`, `"undo:12abc"`, `"undo: 12"`, `"UNDO:12"`, `""`) + logging assertion, **+Vera:** `test_handle_undo_callback_negative_id_no_write_no_send`, `test_handle_undo_callback_astronomically_large_id_does_not_raise` (now a regression guard for the fixed `OverflowError`, asserts no DB write), `test_handle_undo_callback_id_at_sqlite_max_int_is_a_normal_missing_id` (boundary: `2**63-1` stays in range, reaches `db.get_log`, gets `already_undone`), `test_handle_undo_callback_id_one_past_sqlite_max_int_is_ignored` (boundary: `2**63` is rejected, no send) | **PASS** |
| AC11 | 8 byte-identical shape tests × 2 languages (water, stretch, diary-generic-fallback, generic-numeric-with-goal, generic-numeric-without-goal, generic-duration, generic-boolean, water-with-target-override) | **PASS** — 16/16 assertions, confirmed independently by re-running |

Also spot-checked outside the test file (see Failures/notes): Thai confirmation text renders correctly with no mojibake (`↩️ ยกเลิกแล้ว — ลบ น้ำ 500 มล. วันนี้เหลือ 0 / 2500 มล. (0%)`).

## Failures (if any)
None remaining.

### Resolved: `test_handle_undo_callback_astronomically_large_id_does_not_raise` (was FAIL, now PASS)
- **What was tested:** an adversarial `callback_query.data` — `"undo:999999999999999999999999999999999"` — a digit string that *syntactically* matches `undo:<int>` (so it clears `_UNDO_CALLBACK_RE`) but is far outside SQLite's 64-bit signed `INTEGER` range.
- **Original failure:** `handle_undo_callback` raised `OverflowError: Python int too large to convert to SQLite INTEGER` from `db.get_log(log_id)` (`core/undo_ui.py:215` at the time, `storage/db.py:201`), uncaught at the module level (though shielded from crashing the bot end-to-end by `channels/telegram.py`'s blanket `try/except` around `on_callback`, per R-U4).
- **Fix verified:** `core/undo_ui.py:47` now defines `_SQLITE_MAX_INTEGER = 2**63 - 1`; `handle_undo_callback` (line ~224) checks `log_id > _SQLITE_MAX_INTEGER` immediately after parsing and *before* calling `lang = i18n.resolve_reply_language(...)` or `db.get_log(log_id)` — out-of-range ids are logged (`"Ignoring undo callback_query data with an out-of-range log id"`) and the function returns, with **no DB call of any kind**. Confirmed by reading the code (no `db.get_log` call is reachable for an out-of-range id) and by the passing regression test, which additionally asserts a co-seeded row's `deleted_at` stays untouched.
- **Boundary values verified with 2 new tests:**
  - `9223372036854775807` (`2**63 - 1`, the exact SQLite max) is correctly treated as **in range** — it reaches `db.get_log`, finds no row (nothing was seeded at that id), and gets the normal `already_undone` reply. This confirms Luna's design choice (bounds check, not a `\d{1,18}` regex cap) actually delivers on its stated intent: a legitimate 19-digit id up to the max still resolves.
  - `9223372036854775808` (`2**63`, one past the max) is correctly treated as **out of range** — silently ignored, nothing sent, no DB call.
  - Both pass.

## Regressions detected
None. Full suite: `849 passed, 7 failed, 1 skipped` (857 total, up from 855 due to the 2 new boundary tests). The 7 failures are exactly the same documented pre-existing, unrelated flakes as every prior run in this workflow (`test_adaptive_reminders.py` ×3 date-drift, `test_v09_gaps.py` ×3 date-drift, `test_charts.py::test_version_is_consistent...` ×1 stale VERSION pin) — confirmed by name-for-name comparison against the baseline. Nothing in `undo_ui`, `channels`, or the callback path regressed. The parallel `targets` module's test files continue to coexist in the suite (out of this report's scope) without contributing any failures.

## Recommendation
**Ready to ship.** All ACs owned by this module (AC7, AC8, AC9, AC11) pass, including the adversarial hardening this pass added: cross-path idempotency (`/undo` text command then button-tap on the same row), negative ids, and the full SQLite-int64 boundary (in range vs. one-past-max) around the now-fixed `OverflowError`. AC1, AC2, and the end-to-end half of AC5 remain correctly DEFERRED to the integration Vera pass — this is by design per `SPEC-v1.1.md` §11 (`main.py`'s wiring — startup `set_my_commands`, `send_actionable` on confirmations, `on_callback` routing, `_execute_undo` delegation — has not landed yet), not a gap in this module. Module-level building blocks for all three are verified correct.
