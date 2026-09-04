# Test Report — living-streak display fix (line/v1.3.2)

## Summary
- Total: 13 tests already written by Luna (8 in `tests/test_streaks.py` + 1 regression test each in `test_dashboard.py`/`test_digest.py`/`test_discoverability.py`/`test_wrapped.py`/`test_portal_users_gaps.py`) + **11 new gap-pass tests** (`tests/test_streak_display_gaps.py`) = **24 tests** exercising this fix directly, run as part of the full suite below.
- New tests added by Vera: 11, all passed.
- Full LINE gate (`pytest -m "not telegram_only and not llm_only" -n auto`): **5559 passed, 4 skipped, 1 xfailed, 0 failed.**
- Full suite, no marker filter (Luna's own exit-bar command, `pytest -n auto -q`): **5707 passed, 4 skipped, 1 xfailed, 0 failed.** (5696 baseline + 11 new = 5707 — reconciles exactly against IMPL-STREAK-DISPLAY.md's own claimed count.)
- Status: **PASS**

## Keep/switch audit verdict

Audited every row of IMPL-STREAK-DISPLAY.md's caller-by-caller table directly against the current source (not just the doc's own claims):

| # | Site | Claimed | Verified |
|---|---|---|---|
| 1 | `streaks.py:crossed_milestone` | Keep `compute_streak` | **Confirmed** — no `display_streak` reference; still calls `compute_streak(db, config, habit, today, user_id)` at line 384. |
| 2 | `streaks.py:compute_daily_summary` | Switch | **Confirmed** — line 454, `display_streak(db, config, habit, today, user_id)`; `today` param is caller-resolved (digest.py's own documented contract: "`now` is expected already resolved to `config.app.timezone` by the caller"). |
| 3/4 | `dashboard.py:render` (cadence + goal/boolean/count branches) | Switch | **Confirmed** — lines 222 and 226, both via `streaks.display_streak`; `today` resolved through `timeutil.today_in_timezone(clock, config.app.timezone)`. |
| 5 | `digest.py:_grace_bridged` | Keep | **Confirmed** — line 190, still `streaks.compute_streak(db, config, habit, day_before_yesterday, user_id)`, mirroring `grace.py`'s own re-derivation as documented. |
| 6 | `grace.py:evaluate_grace` | Keep | **Confirmed** — line 184, unchanged `compute_streak(..., day_before_yesterday, ...)`. |
| 7 | `records.py:update_on_log` | Keep | **Confirmed** — line 174, unchanged `compute_streak(db, config, habit, today, user_id)`. Independently regression-tested below (not just grep-confirmed). |
| 8 | `review.py:_compute_habit_stats` | Keep | **Confirmed** — line 122, unchanged `compute_streak(db, config, habit, end_date, user_id)`. Independently regression-tested below. |
| 9 | `wrapped.py:_streak_text` | Switch | **Confirmed** — line 195, `streaks.display_streak`; `today` threaded from `render()`/`execute_wrapped()`'s own `timeutil.today_in_timezone` call. |
| 10 | `portal/users.py:_current_streak` | Switch | **Confirmed** — line 136, `streaks.display_streak`; `today` via `timeutil.today_in_timezone(lambda: now, deps.config.app.timezone)`. |
| 11 | `discoverability.py:build_habits_overview` | Switch | **Confirmed** — line 248 (cadence branch only, as documented — the non-cadence branch shows no streak at all). |

`core/confirmation.py`: **confirmed untouched** — `suffix()` only calls `streaks.crossed_milestone` (which internally uses `compute_streak`, never `display_streak`) and `records.update_on_log`. No bare streak number is rendered anywhere in `confirmation.py`'s templates. IMPL's own investigation (the real surface is `dashboard.refresh`'s trailing board, reached via `dashboard-in-reply`) is correct and is exactly what test 1 below exercises end-to-end.

**Verdict: keep/switch audit PASS — every row matches the doc, no drift, no silent extra switch.**

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_streak_display_gaps.py` (new) | 11 | E2E real-flow keep/switch boundary; truth-table extremes (first-ever day, grace+pending via a real caller, paused+met via a real caller, honest double-miss zero, cadence Monday re-verified independently via `dashboard.render`); boundary audits (`records.py` storage, `crossed_milestone` side-effect-freedom, `review.py` exactness); timezone/clock discipline across every switched caller |

(Luna's own 13 tests across `test_streaks.py`/`test_dashboard.py`/`test_digest.py`/`test_discoverability.py`/`test_wrapped.py`/`test_portal_users_gaps.py` were read and re-run but not modified — see "AC coverage" below for how they map.)

## Probe results

### 1. Real live scenario, end-to-end through the wired reply flow (dashboard-in-reply included), keep/switch boundary in one test
`test_e2e_webhook_reply_shows_living_streak_then_milestone_fires_exactly_at_crossing` — drives the REAL webhook (`_running_line_app`, genuine signed HTTP POST, real SQLite, real routing/confirmation/dashboard-refresh chain, no mocks beyond the outbound LINE API transport). Water, goal 2500, met D-2/D-1. First log today (1250ml, partial): confirmation carries no milestone line; trailing dashboard-in-reply board shows `streak 2d`. Second log (another 1250ml, crosses 2500): board shows `streak 3d`, **and** the milestone suffix ("3-day water streak") fires on this exact confirmation, not the first one. **PASS.**

### 2. Truth-table extremes
- First-ever log day (no history, today qualifies) → `display_streak == 1`. **PASS.**
- Yesterday grace-bridged + today pending, verified through `dashboard.render` (a real caller, not the raw function) → row shows `streak 2d`. **PASS.**
- Paused today + met yesterday, verified through `dashboard.render` → row shows `streak 2d` **and** the pause marker on the same line (coherent pin, not a false "0 + paused"). **PASS.**
- Yesterday AND today both logged but genuinely below goal (no pause/grace) → `display_streak == 0` (distinguishes "logged but short" from "not logged yet"). **PASS.**
- Cadence, Monday, last week genuinely failed — re-verified independently through `dashboard.render`'s own cadence branch (not the raw function `test_streaks.py` already covers) → row shows `weekly streak 0 week(s)`, no resurrection. **PASS.**
- Cadence, Monday, last week met — the positive contrast, same real-caller path → row shows `weekly streak 2 week(s)` (pass-through). **PASS.**

### 3. Boundary audits
- `records.py`'s stored `longest_streak` stays governed by `compute_streak`'s exact contract: pre-seeded record = 1, real 2-day run through yesterday, today partial. `display_streak` for this exact data reports 2 (confirmed, so the test isn't vacuous); `compute_streak(today)` is 0; `records.update_on_log` therefore upserts nothing and the stored record stays at 1 — no false celebration off the living number. **PASS.**
- `crossed_milestone` is unaffected by any number of intervening `display_streak` calls (sandwiched 25 calls on each side around two `crossed_milestone` invocations, `was_qualified_before=False` then `True`) — correct crossing detection and correct once-per-crossing suppression both hold throughout. **PASS.**
- `review.py`'s duration-habit streak stays exact (0, not the living 2) for the identical partial-today data `display_streak` reports 2 for — confirmed via `review._compute_habit_stats` directly, interleaved with `display_streak` calls to rule out any leakage. **PASS.**

### 4. Timezone / clock discipline
Every real call site in this codebase threads a naive, `datetime.now`-shaped injectable clock (confirmed by inspection of `routing.py`/`dashboard.py`/`wrapped.py`/`portal/users.py`/`digest.py` — none ever construct an aware one). Under that real shape, `dashboard.render` (tz-normalized via `timeutil.today_in_timezone`) and `discoverability.build_habits_overview` (raw `clock().date()`) agree on "today" and on the same cadence habit's live streak number. **PASS.**

**Informational finding (not a FAIL, not introduced by this patch):** `discoverability.build_habits_overview` resolves `today` as a bare `clock().date()` rather than routing through `timeutil.today_in_timezone(clock, tz)` like every other switched surface (`dashboard.render`, `wrapped.render`/`execute_wrapped`, `portal/users._current_streak` all explicitly `astimezone()` an aware clock to `config.app.timezone` first). This is pre-existing — `display_streak` was substituted in at the same already-resolved `today` variable `compute_streak` used before it, no different call shape — and is inert in production today because no real call site ever constructs a timezone-aware clock (confirmed by inspection above). Directly demonstrated as a latent risk against the two raw resolvers at a real UTC/Bangkok midnight-crossing boundary (2026-08-23 20:00 UTC = 2026-08-24 03:00 Bangkok): `timeutil.today_in_timezone` correctly returns 2026-08-24; `build_habits_overview`'s raw `clock().date()` would return 2026-08-23. Flagging for Archi/Luna as a pre-existing structural inconsistency worth a follow-up hardening pass (route `build_habits_overview` through `timeutil.today_in_timezone` too, for defense-in-depth), not a blocker for this release.

## Mutation-test verification (tests have teeth)

To confirm the new tests actually catch the bug class they claim to, `streaks.display_streak` was monkeypatched at runtime (via a throwaway pytest plugin in a subprocess — **no production file was ever edited**; the file was deleted immediately after) back to the pre-fix shape (`return compute_streak(db, config, habit, today, user_id)`, i.e. no yesterday-fallback at all) and the streak-relevant subset re-run:

- **Correctly failed** (regression caught): `test_streaks.py`'s own `test_display_streak_today_pending_falls_back_to_yesterdays_unbroken_run` and `test_display_streak_grace_bridged_yesterday_shows_the_held_streak`; my new `test_e2e_webhook_reply_shows_living_streak_then_milestone_fires_exactly_at_crossing`, `test_dashboard_render_yesterday_grace_bridged_today_pending_shows_unbroken_streak`, `test_records_longest_streak_stays_governed_by_compute_streak_not_display_streak`, `test_review_duration_streak_stays_exact_not_living_regardless_of_display_streak_calls`; plus the 4 pre-existing regression tests in `test_dashboard.py`/`test_digest.py`/`test_wrapped.py`/`test_portal_users_gaps.py`. 10 failures total, exactly the tests whose scenario reaches the fallback branch.
- **Correctly still passed** (not this bug class): the paused-today test (never reaches the fallback branch by design), both cadence tests (cadence has no fallback either way), the first-ever-day and honest-double-miss tests (`s_today` already correct without a fallback), the milestone side-effect-freedom test (doesn't depend on the fallback), and the timezone test (cadence-only). This is the expected, correct split — confirms the new tests target the right invariant, not a vacuous one.

After the probe, `git diff --stat src/habit_assistant/core/streaks.py` was re-checked and matches Luna's original patch exactly (71 insertions / 1 deletion, unchanged) — confirming no residue.

## AC coverage (dispatch's own probe list)

| Probe | Test(s) | Result |
|---|---|---|
| Live scenario end-to-end through real wired reply flow, dashboard-in-reply, keep/switch boundary in one test | `test_e2e_webhook_reply_shows_living_streak_then_milestone_fires_exactly_at_crossing` | PASS |
| First-ever log day | `test_display_streak_first_ever_log_day_qualified_no_history_shows_one` | PASS |
| Yesterday grace-bridged + today pending (unbroken number) | `test_streaks.py::test_display_streak_grace_bridged_yesterday_shows_the_held_streak` (Luna) + `test_dashboard_render_yesterday_grace_bridged_today_pending_shows_unbroken_streak` (real-caller re-verify) | PASS |
| Paused today + met yesterday (coherent pin) | `test_streaks.py::test_display_streak_paused_today_shows_the_held_streak_directly` (Luna) + `test_dashboard_render_paused_today_met_yesterday_pins_streak_and_pause_marker_coherently` (real-caller re-verify) | PASS |
| Yesterday AND today both unqualified → honest 0 | `test_display_streak_yesterday_and_today_both_genuinely_unqualified_shows_honest_zero` | PASS |
| Cadence Monday, last week failed → 0 (negative control, independently re-verified) | `test_streaks.py::test_display_streak_cadence_never_resurrects_a_genuinely_broken_week` (Luna) + `test_dashboard_render_cadence_monday_last_week_failed_does_not_resurrect_streak` (real-caller re-verify) | PASS |
| Cadence Monday, last week met → pass-through | `test_streaks.py::test_display_streak_cadence_pass_through_when_prior_weeks_still_active` (Luna) + `test_dashboard_render_cadence_monday_last_week_met_passes_through_living_value` (real-caller re-verify) | PASS |
| Records longest-streak unchanged under new display | `test_records_longest_streak_stays_governed_by_compute_streak_not_display_streak` | PASS |
| Milestone fires once per crossing, not re-fired by display calls | `test_crossed_milestone_unaffected_by_repeated_display_streak_calls_between` | PASS |
| Review's historical numbers byte-identical | `test_review_duration_streak_stays_exact_not_living_regardless_of_display_streak_calls` | PASS |
| Timezone: "today" anchor, same clock discipline as callers | `test_timezone_anchor_agrees_across_switched_callers_under_the_apps_real_naive_clock` | PASS (see informational finding above) |
| Exit bar: streak-relevant subset + full LINE gate `-n auto` → 0 failed | Both gate forms run in full | PASS |

## Failures (if any)

None.

## Regressions detected

None. Full suite (both the documented `-m "not telegram_only and not llm_only"` gate and Luna's own bare `-n auto -q` exit-bar command) is green at 0 failed in each form, counts reconciling exactly against IMPL-STREAK-DISPLAY.md's own claimed baseline (5696 + 11 new = 5707).

## Recommendation

**Ready to ship.** The keep/switch boundary holds under an independent, adversarial re-audit (not just re-reading Luna's own claims): every switch site was grep-confirmed against source, every keep site was grep-confirmed AND, for the two most exactness-sensitive ones (`records.py`, `review.py`), regression-tested directly with data constructed so the two functions' outputs provably diverge (proving these tests aren't vacuous). The one open item is informational only (`discoverability.py`'s `today` resolution bypassing `timeutil.today_in_timezone`) — pre-existing, inert in production, not introduced by this patch; worth a follow-up hardening pass but not a release blocker.
