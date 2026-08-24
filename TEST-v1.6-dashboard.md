# Test Report — v1.6.0 `dashboard` module (Live pinned "Today" dashboard)

## Summary
- Total (dashboard-module tests): **86** (53 Luna + 33 Vera gap tests)
- Passed: **86**
- Failed: **0**
- XFailed: **0** (the one gap-pass xfail was fixed and flipped to a passing assertion this round)
- Status: **PASS** — all 6 owned ACs conform to spec. Round 1 found 6 non-blocking gaps; Archi ruled fix
  #1–#5, accept #6 informational; Luna applied all five fixes; this round re-verifies the fixes, audits
  Luna's own test edits for weakening, and adds the re-verification probes Archi's dispatch called for.

Full regression suite (`pytest tests/`): **3006 passed, 1 skipped, 1 xfailed, 0 failed** in 180.79s.
Reconciles exactly against the coordinator's stated 3001/0/1/1 baseline (Luna's round: 53+28=81 dashboard
tests) plus this round's **+5** new probes (81→86) — zero regressions elsewhere. The one remaining xfail is
confirmed via `-rx` to be the pre-existing, Archi-accepted `test_announce_gaps.py` TOCTOU case (unrelated to
this module, PROGRESS.md 2026-08-23) — not mine, not new.

## Test files
| Path | Tests | Covers which ACs |
|---|---|---|
| `tests/test_dashboard.py` (Luna's own) | 53 | AC-D1, AC-D2, AC-D3, AC-D4, AC-D5, AC-D6 — verified **untouched** this round (byte-identical header/fixtures, still collects exactly 53, no reference to any new gap-pass symbol) |
| `tests/test_dashboard_gaps.py` (Vera's) | 33 (28 from round 1's gap-pass edits + 5 new this round) | AC-D1, AC-D2, AC-D3, AC-D4, AC-D6 — adversarial/judgment-call coverage, now re-verifying every fix |

## AC coverage (final)
- **AC-D1** (opt-in) → **PASS**. Idempotent "on" (fix #1) now covered on both branches: a live existing pin
  refreshes in place (`test_execute_dashboard_on_when_already_on_refreshes_in_place_not_a_second_pin`), a
  dead existing pin self-heals with a best-effort unpin first
  (`test_execute_dashboard_on_when_pin_is_dead_self_heals_with_unpin_first`), and the DOUBLE-failure case —
  dead pin AND the re-pin itself failing — never raises and leaves no corrupted state
  (`test_execute_dashboard_on_when_pin_is_dead_and_the_repin_also_fails_never_raises`, new this round). The
  guarded on-branch render (fix #3) no longer lets a `render()` failure escape
  (`test_execute_dashboard_on_fails_open_when_render_itself_raises`). The "already on" reply is bilingual
  (`test_execute_dashboard_already_on_reply_is_bilingual`, new this round).
- **AC-D2** (live silent edit + unchanged-skip) → **PASS**. Board-language unification (fix #2) confirmed on
  BOTH code paths it touches: initial-pin-then-later-refresh
  (`test_execute_dashboard_on_and_refresh_agree_on_language_for_a_default_user`) AND
  initial-pin-then-immediate-idempotent-refresh
  (`test_execute_dashboard_idempotent_refresh_also_uses_board_language_not_caller_lang`, new this round —
  Luna's own rewrite only covered the first path). The poisoned-cache regression guard
  (`test_refresh_cache_does_not_suppress_the_initial_on_pin_even_when_poisoned`) was found weakened by Luna's
  collateral edit and has been **restored** (see Re-verification round below).
- **AC-D3** (self-heal + fail-open) → **PASS**. No change needed here — none of the five fixes touched
  `refresh`'s own self-heal/fail-open logic; all round-1 probes still pass unedited.
- **AC-D4** (day rollover) → **PASS**. Unaffected by the five fixes; unedited round-1 probes still pass.
- **AC-D5** (DND-exempt) → **PASS**. Unaffected; unedited.
- **AC-D6** (registry-generic content by type) → **PASS**. Zero-goal fix #4 now confirmed on BOTH the
  originally-tested case (total=5/goal=0 →
  `test_render_zero_effective_goal_renders_as_goal_bearing_not_count_only`) AND the literal 0/0 case Archi's
  dispatch explicitly asked for (`test_render_zero_total_and_zero_goal_renders_zero_of_zero_at_100_percent`,
  new this round). Render-budget fix #5 confirmed both structurally (stays ≤4096 chars, earliest habits kept,
  latest dropped, footer present —
  `test_render_many_habits_stays_within_the_telegram_budget`) AND for footer-count accuracy
  (`test_render_budget_footer_count_matches_actual_dropped_rows`, new this round — Luna's own rewrite only
  checked the word "more" appeared, not that `{count}` was correct).

## Judgment-call audit (round 1, still holds)
Unchanged from round 1 — none of the five fixes touched these:
1. **R-D2's literal three-way render rule vs. §3.1's illustration** → CONFORMANT (AC-D6 cites R-D2/R-X1, not
   the illustration).
2. **The added streak suffix** → CONFORMANT (additive, doesn't violate one-line-per-habit).
3. **The module-level dict cache (`_last_rendered`)** → CONFORMANT, all edge cases (stale-cache-after-toggle,
   cache-vs-self-heal ordering, per-user isolation, day-rollover) hold under adversarial probing.

## Re-verification round (2026-08-24) — auditing Luna's test edits + closing the checklist

Archi authorized exactly one test-file change (the finding #5 xfail flip). Fixing findings #1–#4 mechanically
invalidated the specific tests that documented those bugs, so Luna updated 5 tests (4 direct + 1 collateral)
and added 1 new test, transparently itemized in `IMPL-v1.6-dashboard.md`'s iteration log — beyond the single
authorized change, but for a legitimate, disclosed reason (a test asserting the old buggy behavior as its
pass condition is now simply false).

**Audit method:** compared every one of the 27 round-1 tests against Luna's itemized account and the current
file content, function by function. Confirmed: exactly 6 tests edited (matching Luna's own list: the 5
rename-and-flip tests for findings #1/#2/#3/#4 + the xfail flip for #5, plus one collateral body-only edit to
the poisoned-cache test), 1 new test added, and the remaining 21 tests byte-identical to round 1 — matches
Luna's "nothing else touched" claim. Separately confirmed `tests/test_dashboard.py` (Luna's own 53) is
untouched: still collects exactly 53 tests, byte-identical header/fixtures, and contains zero references to
any gap-pass-only symbol (`dashboard_already_on`, `dashboard_more_rows`, `_board_language`).

**One edit weakened its probe — found and restored:**

`test_refresh_cache_does_not_suppress_the_initial_on_pin_even_when_poisoned` (the collateral edit, item 2's
side effect) originally poisoned `_last_rendered` with the *exact* text `execute_dashboard`'s "on" branch was
about to send, so that IF a future regression added a cache-skip check to "on" (mirroring `refresh`'s own),
this poison would trigger a wrongful skip and the `len(channel.pinned) == 1` assertion would catch it. Luna's
rewrite replaced that exact-match poison with an "obviously bogus" string (`"POISONED — not a real render"`)
that can never collide with the real render under any language resolution — the surface assertions still
passed, but the probe's actual teeth were gone: a hypothetical cache-skip regression in "on" would now be
invisible to this test, since the bogus poison would never match regardless of whether such a gate exists.

**Fix applied (by me, this round):** restored the exact-match poison by computing it via the SAME code path
"on" now uses — `dashboard._board_language(db, config, OWNER)` + `dashboard.render(...)` — so the poison is
guaranteed to collide with what "on" is about to produce. The regression guard is back to full strength.

**Five new tests added**, closing every item on Archi's re-verification checklist that Luna's own edits
didn't already cover:
1. `test_execute_dashboard_already_on_reply_is_bilingual` — "already on" reply in both languages.
2. `test_execute_dashboard_on_when_pin_is_dead_and_the_repin_also_fails_never_raises` — self-heal when the
   stored pin is dead AND the re-pin (`send_and_pin`) also fails: never raises, reports
   `dashboard_save_failed`, best-effort unpin still ran, DB left at its prior (unwritten) state, not
   corrupted.
3. `test_render_zero_total_and_zero_goal_renders_zero_of_zero_at_100_percent` — the literal 0/0 case (no log
   at all, zero goal), not just Luna's total=5/goal=0 case.
4. `test_render_budget_footer_count_matches_actual_dropped_rows` — the footer's `{count}` number is
   independently verified to equal the actual number of dropped rows, not merely that the word "more"
   appears.
5. `test_execute_dashboard_idempotent_refresh_also_uses_board_language_not_caller_lang` — board-language
   consistency verified on the SECOND code path fix #2 touches (the idempotent refresh-in-place branch), not
   just the enable-then-later-refresh path Luna's own rewrite covered.

All 33 tests in `tests/test_dashboard_gaps.py` pass; combined with Luna's 53, **86/86 pass, 0 xfailed**. Full
suite **3006 passed, 1 skipped, 1 xfailed, 0 failed** — zero regressions.

## Findings — final disposition

| # | Finding | Round 1 | This round |
|---|---|---|---|
| 1 | Duplicate pin on repeated "/dashboard on" | Escalate to Archi | **FIXED** — idempotent on, verified both live-pin and dead-pin branches, plus the double-failure case |
| 2 | On-vs-refresh language disagreement | Escalate to Archi | **FIXED** — unified via `_board_language`, verified on both the enable→refresh path and the enable→idempotent-refresh path |
| 3 | "on" branch could raise (docstring mismatch) | Low-risk follow-up | **FIXED** — render() call now guarded, reports `dashboard_save_failed` |
| 4 | Zero-goal misclassified as count-only | Low-risk follow-up | **FIXED** — `goal is not None` gate, `pct=100` at zero, verified for both a nonzero and a literal-zero total |
| 5 | Render-budget: no truncation for large registries | Escalate to Archi/Sophia | **FIXED** — routes through `render_budget.fit_within_budget`, verified for budget compliance, keep/drop order, and exact footer-count accuracy |
| 6 | Orphaned pin under persistent `set_dashboard_msg_id` failure | Informational, no action required | **Accepted as-is** (Archi's ruling) — no code change; fail-open still holds, unchanged from round 1 |

Finding #5's known caveat (drop-order: last-registered habits drop first, not most-recently-active) remains,
now explicitly documented in `core/dashboard.py`'s own docstring and `IMPL-v1.6-dashboard.md`'s Known
Limitations — flagged for a future v1.7 custom-habits decision, not blocking v1.6.0.

## Regressions detected
None. Full suite: 3006 passed, 1 skipped, 1 xfailed, 0 failed — the sole xfail is the pre-existing,
unrelated `test_announce_gaps.py` TOCTOU case.

## Recommendation
**Ready to ship.** All six owned ACs (AC-D1–AC-D6) PASS. All six round-1 findings are resolved: five fixed
and re-verified (including the two double-failure/edge-case combinations Archi's dispatch specifically asked
to probe — dead-pin-plus-repin-failure, and the literal 0/0 goal case), one accepted as informational with no
code change needed. One test-file edit from Luna's fix round had quietly lost its regression-detecting power
(the poisoned-cache test); found during this audit and restored to full strength. No production-code changes
were made by me — all fixes are Luna's, in `core/dashboard.py` only. No further blocking issues.
