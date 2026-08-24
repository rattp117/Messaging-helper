# Test Report — v1.6.0 `nudge` module ("Almost there" end-of-day nudge)

## Summary
- Total (nudge scope): 49 tests — 32 pre-existing (`tests/test_nudge.py`, Luna's, unmodified) + 17 (`tests/test_nudge_gaps.py`, Vera's adversarial gap coverage, incl. 1 boundary test added on re-verification)
- Passed: 49
- Failed: 0
- Status: **PASS**
- Full suite regression check: `pytest tests/` → **2960 passed, 0 failed, 1 skipped, 1 xfailed** (146.51s). Zero failures, zero regressions. (Passed count is above both the coordinator's 2674 baseline and Luna's own re-reported 2893, because the dashboard/heatmap/insights parallel tracks continue landing concurrently plus this pass's own +1 boundary test — out of this report's scope to reconcile, per the dispatch note.)

## Re-verification (fan-out fail-open fix)

Luna's fix (`core/nudge.py` ~185-212) was re-verified by reading the code (not just trusting the report) and by re-running both test files plus one added boundary test:

- **Two independently-contained failure stages, confirmed by inspection:**
  - Stage 1 (lines 191-203): `try/except Exception` around `effective_checkin`, `in_dnd_now`, language resolution, and `build_nudge_message` — a failure here logs and `continue`s to the next user, exactly as before.
  - Stage 2 (lines 208-212): a **separate** `try/except Exception` around `await channel.send(user_id, message)` only — a transport failure here logs and `continue`s independently, without re-touching stage 1's outcome. This is the missing piece from the original report, and it now mirrors `core/announce.py:announce_release`'s own two-stage try/except shape exactly, as Luna's docstring update (lines 178-185) claims.
  - No state is shared between the two stages beyond the per-iteration local `message` variable — a stage-1 exception never reaches stage 2 (the `except` `continue`s before `message` is used), and a stage-2 exception can't corrupt stage 1's next-iteration state (each loop iteration re-derives everything from scratch).
- **Nothing is incorrectly marked/skipped for a user's next-day nudge after a send failure:** `core/nudge.py` writes **no** per-user "already sent" state anywhere (unlike `announce_release`, which explicitly leaves a failed user *unmarked* in `db` so a later run retries them) — the *only* thing enforcing "once/day" is R-N1's fixed-minute guard (`hhmm != config.nudge.time`, checked once at the top of the function, before the per-user loop even starts). Added `test_a_failed_send_is_not_retried_later_the_same_day` to confirm this directly: a send failure at 20:00 followed by a healthy-channel re-tick at 20:01 (same day) produces **no** send at all — not a duplicate, not a delayed retry, not a crash on re-tick. This closes the one residual the coordinator flagged. **PASS.**
- Re-ran `pytest tests/test_nudge.py tests/test_nudge_gaps.py -v` → **49 passed** (0 failed), including the previously-failing `test_fail_open_fan_out_one_users_send_failure_does_not_block_the_others`, now green: `OWNER` and `THIRD` (on either side of the failing `MEMBER`) both still get nudged, and `run_due_nudges` itself no longer raises.

## Test files
| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_nudge.py` (Luna's, unmodified) | 32 | AC-N1, AC-N2, AC-N3 |
| `tests/test_nudge_gaps.py` (Vera's, this pass) | 17 | AC-N1 (fail-open fan-out — now **PASS**; same-day no-retry-after-failure boundary — **PASS**), AC-N2 (pre-v1.5 NULL, DND exact/midnight-crossing boundaries), AC-N3 (Thai-default unprompted send, interplay re-confirmation) |

## AC coverage
- **AC-N1** (close, once/day; R-N1/R-N2) → threshold boundaries (incl. float-precision fractional goals, `threshold_pct=100` degenerate case, `threshold_pct=0` rejected by config validation), target-override-changed-mid-day, custom-timezone honored, non-zero-seconds-within-minute honored, fail-open fan-out (a mid-fan-out send failure no longer blocks or aborts the rest), same-day no-retry-after-failure → **PASS**.
- **AC-N2** (opt-in + DND; R-N1/R-N2) → `test_pre_v15_null_checkin_state_never_nudges`, `test_dnd_window_ending_exactly_at_20_00_does_not_suppress`, `test_dnd_window_starting_exactly_at_20_00_suppresses`, `test_midnight_crossing_dnd_window_covering_20_00_suppresses`, `test_midnight_crossing_dnd_window_excluding_20_00_does_not_suppress` (+ Luna's own enablement/window-independence/DND tests) → **PASS**.
- **AC-N3** (registry-generic + bilingual; R-N3/R-X1) → `test_unprompted_send_defaults_to_thai_when_no_lang_pref_was_ever_set`, `test_both_ticks_independently_reach_a_send_at_20_00_with_default_window` (+ Luna's own goal-less/boolean/text/bilingual/isolation/zero-LLM tests) → **PASS**.

## Judgment-call audit (Luna's "Known Limitations" entry)

**Verdict: CONFORMANT (with a note).**

Luna used a uniform `nudge_header` + one `nudge_line`-per-habit shape for both the single- and multi-habit case, rather than switching to the spec's bare single-line wording (`💧 Just 300 ml to hit your water goal today — you've got this.`) when exactly one habit qualifies.

Reading SPEC-v1.6.md §3.3 ("Outputs") directly: the block containing that line is introduced by the comment `# "almost there" nudge (once/day, near end of day):` — the same illustrative-example convention used for every other output in that section (`/records water`, `/trends water`, the celebration line), none of which are treated as byte-exact contracts elsewhere in this codebase. The **normative** text is R-N1/R-N2 (§4) and AC-N1/AC-N2 (§8): "send one encouraging `nudge_close` message naming the remaining amount" / "at most one nudge message per user per day." Neither specifies line count, header presence, or per-habit emoji. Nothing in §3.3 or §8 says the example string must be reproduced verbatim, and R-N2's own "at most one message... even when several habits qualify simultaneously" requirement is naturally satisfied by Luna's header+bullet shape without any single/multi special-casing. This is the same illustrative-example-vs-normative-rule pattern this codebase already treats consistently elsewhere (e.g. `checkin_header`+`checkin_line_progress`, which Luna's own docstring cites as the direct precedent).

No test change needed; flagged only as a documented judgment call, matching Luna's own request in IMPL-v1.6-nudge.md's "Known limitations" for either Vera or Archi to confirm or override. Recommend confirming as-is unless the user has a specific opinion on the single-habit wording.

## Failures (original round — now resolved)

### `test_fail_open_fan_out_one_users_send_failure_does_not_block_the_others` (FAIL on first pass → PASS after fix)
- **What was tested:** 3 active users, all check-in-enabled and squarely "close" on the same habit. The channel raises on `send()` for the middle user (`MEMBER`) only. Per SPEC-v1.6.md §3.4 ("the nudge never raise[s]") and the fail-open fan-out discipline every other minutely tick in this codebase follows (`core/announce.py:announce_release`, which wraps its own `channel.send` in try/except), users before and after the failing one in `db.active_user_ids()` order should still be nudged, and `run_due_nudges` itself should not raise.
- **AC violated (original):** AC-N1 (§4 R-N1's "for each active user" fan-out contract) / SPEC-v1.6.md §3.4 ("the nudge never raise[s]; a ... failure is logged and degraded (fail-open)").
- **Original actual:** `run_due_nudges` raised `RuntimeError: simulated send failure for member-chat-b` and aborted mid-fan-out (`src\habit_assistant\core\nudge.py:202`, `await channel.send(user_id, message)` sat outside the try/except). `THIRD` (processed after the failing `MEMBER`) never ran.
- **Fix applied (Luna):** `channel.send` now has its own `try/except Exception: logger.exception(...); continue` (`core/nudge.py` lines 208-212), independent of the eligibility/build try block (lines 191-203) — mirrors `core/announce.py:announce_release`'s two-stage shape.
- **Re-verification:** confirmed by direct code read (both stages independently contained, no shared mutable state) and by re-running the test — now **PASS**: `OWNER` and `THIRD` both still get nudged, `run_due_nudges` returns normally. A new companion test, `test_a_failed_send_is_not_retried_later_the_same_day`, further confirms a send failure doesn't leave any stray "pending" state that could cause a duplicate/delayed send later the same day (there is no per-user "already sent" DB state at all — the fixed-minute guard alone governs firing) — also **PASS**.

## Regressions detected
None, in either round. The original round's only failure was the fan-out test above, which surfaced a genuine pre-existing gap in `core/nudge.py` (now fixed), not a regression from any other change. Post-fix, all 32 of Luna's own `tests/test_nudge.py` tests and all 17 of `tests/test_nudge_gaps.py` pass, and the full suite (`pytest tests/` → 2960 passed, 0 failed, 1 skipped, 1 xfailed) is green with no failures anywhere, including the dashboard/heatmap/insights tracks.

## Recommendation
**Ready to ship.**

The fail-open fan-out bug found on the first pass is fixed and independently re-verified (code read + full re-run, not just trusting Luna's report): both failure stages (eligibility/build vs. send) are now genuinely independent, and a send failure leaves no state that could cause a wrong outcome on a later tick the same day. All three owned ACs (AC-N1, AC-N2, AC-N3) pass. The header+bullet-vs-single-line judgment call is CONFORMANT (illustrative example, not a normative format lock) and needs no change. No further Luna↔Vera round needed for the `nudge` module.
