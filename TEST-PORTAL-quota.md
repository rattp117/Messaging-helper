# Test Report — Admin Web Portal, module QUOTA (AC26–AC30)

> Verifies `IMPL-PORTAL-quota.md` (Luna) against `SPEC-LINE-PORTAL.md` §4
> R-QUOTA-1..5, `UX.md` (Screens 4–5, Flow C, Flow D), `UI.md` (§3.7 gauge,
> §3.10/§3.22 table/interstitial), and the dispatch note's own load-bearing
> NO-DOUBLE-SEND requirement. Branch `line-version`, worktree-only.

## Summary

- Total: 57 tests (28 Luna + 29 new adversarial)
- Passed: 57
- Failed: 0
- Status: **PASS** (all owned ACs green; 2 reportable FINDINGs, both non-blocking — see below)
- Full LINE gate (`pytest -m "not telegram_only and not llm_only" -n auto`): **5498 passed, 4 skipped, 1 xfailed, 1 failed** — the 1 failure is `tests/test_portal_audit_gaps.py::test_audit_detail_cell_leaks_diary_text_via_undo_old_value_MAJOR_FINDING`, owned by the sibling AUDIT track (AC22–25), untouched by this pass, not a QUOTA regression.

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_portal_quota.py` (Luna's own) | 28 | AC26, AC27, AC28, AC29, AC30, NO-DOUBLE-SEND (baseline) |
| `tests/test_portal_quota_gaps.py` (this pass) | 29 | AC26, AC27, AC29, AC30, NO-DOUBLE-SEND (adversarial), identity gate, XSS, bilingual, gauge parity, integration-gap pin |

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC26 (monthly totals + current-month per-user breakdown) | Luna: `test_quota_page_shows_monthly_history_and_current_month_marker`, `test_quota_page_shows_current_month_per_user_breakdown_sorted_desc`, empty-state pair. Mine: `test_month_history_caps_at_12_months_even_with_15_months_of_ledger_data`, `test_yyyymm_key_used_by_quota_matches_the_real_ledger_write_clock`, `test_current_month_marker_absent_when_current_month_has_zero_pushes_but_history_exists`, `test_brand_new_deployment_month_panel_has_no_synthesized_zero_row` | **PASS** (FINDING F1 below — non-blocking) |
| AC27 (active cap, 80/100% thresholds, warn/stop fired) | Luna: 4 tests. Mine: `test_stopped_quota_message_is_visible_on_the_page_not_just_a_redirect_code`, `test_mid_fanout_no_per_user_quota_recheck_all_candidates_sent_even_past_cap`, `test_gauge_month_heading_format_diverges_between_status_and_quota_pages` | **PASS** (FINDING F2 below — non-blocking) |
| AC28 (per-user digest opt-out + schedule time/mode) | Luna: 3 tests. Mine: `test_xss_via_display_name_in_byuser_and_digest_panels_is_escaped`, bilingual tests | **PASS** |
| AC29 (effective config renders, secrets redacted) | Luna: 6 tests. Mine: `test_config_hostile_value_in_a_non_secret_field_is_escaped_not_executed`, `test_config_habit_list_with_secret_looking_label_never_renders_at_all`, `test_no_secrets_field_structurally_reachable_from_the_portal`, `test_config_page_forced_thai_renders_thai_secrets_note` | **PASS** |
| AC30 (manual trigger, confirm-gated, real fan-out, result summary) | Luna: 6 tests. Mine: `test_partial_mid_fanout_failure_is_not_counted_as_sent_or_skipped`, `test_digest_confirm_interstitial_forced_thai_renders_thai_irreversibility_copy` | **PASS** (FINDING F3 below — non-blocking) |
| NO-DOUBLE-SEND (dispatch note, load-bearing) | Luna: 5 tests (replay, second-visit, unrecognized token, concurrent double-POST, two-distinct-tokens race). Mine: 6 tests — see "Double-send-window trace" below | **PASS** (FINDING F3 for the ONE genuine gap found — see below) |
| Identity gate, all 3 routes incl. POST (shared surface, exercised here) | `test_identity_gate_blocks_headerless_get_quota`, `..._get_config`, `..._post_digest_run_unconfirmed`, `..._post_digest_run_confirmed`, `..._blocks_wrong_owner_login_on_all_three_routes`, `..._allows_correct_owner_login_on_all_three_routes` | **PASS** |
| OQ4 ruling (warn/stop state source) | Read `quota.py:_quota_snapshot` + module docstring | **RULING CONFIRMED SOUND** — see below |

## Double-send-window trace (the load-bearing item)

Six angles probed beyond Luna's own gather/replay tests, all against the
REAL `_manual_digest_lock` + `_manual_digest_runs` + `_pending_digest_tokens`
guard, using real `asyncio.gather` races (one with an injected `await
asyncio.sleep()` in a custom channel double to force genuine interleaving
on a fast local test server, since the stock `RecordingLineChannel` double
has no internal suspension point):

1. **Token minted but never confirmed, used the next day** → honored as a
   legitimate first confirm for the new day (tokens are one-time-use, not
   same-day-use). **Pinned as intended**, not a bug.
2. **A token already CONSUMED by a successful run, replayed across a day
   rollover** → refused (`Already sent` page, no send). The token is
   discarded from `_pending_digest_tokens` on use, independent of the
   day-keyed marker, so a spent token stays spent forever. **PASS.**
3. **Marker date rollover, 23:59 → 00:01** → a manual run at 23:59 and a
   second one at 00:01 the next calendar minute are two independent,
   legitimate "first run of the day" events (different `_today_str()`
   keys); a same-day retry in between is correctly refused. **Pinned as
   correct** — the guard's contract is "at most one extra manual run per
   calendar day," not a rolling window.
4. **Simulated process restart mid-day** (module globals cleared, same
   day) → a second manual run is then allowed. **PINNED, ACCEPTED, not a
   defect** — this is in-memory, process-lifetime state by design
   (mirrors `core/digest.py:_DIGEST_DEFERRED_DATES`'s own documented
   single-instance posture), consistent with the `habit-assistant-line.
   service` unit's own no-multi-worker assumption.
5. **An unconfirmed POST arriving genuinely mid-flight during an in-flight
   confirmed run** (forced via a slow-channel double so the interleave is
   real, not a race that just happens not to trigger) → still yields
   exactly one real send; the second confirm blocks on the lock, then
   sees the marker the first run just set. **PASS.**
6. **The manual trigger racing the SEPARATE, unguarded SCHEDULED digest
   job** (`core/jobs.py`'s own `CronTrigger` → `core/digest.py:
   run_daily_digest`, called directly, with no knowledge of `quota.py`'s
   guards at all) → **FINDING F3, reproduced**: every active, digest-on
   user gets pushed TWICE in that window (one push per independent run).
   See below.

## Findings

### F1 — Current month silently absent from the month-history table when it has zero pushes but prior months have data
- **Severity:** LOW (UX/diagnostic-quality, not money-adjacent)
- **AC:** AC26 / UX.md Flow C ("is this month anomalous, or is this just growth?")
- **What was tested:** `test_current_month_marker_absent_when_current_month_has_zero_pushes_but_history_exists`
- **Input:** pushes recorded for 2026-01 and 2026-02 only; current month (2026-09 at test time) has zero `push_ledger` rows.
- **Expected (UX.md Screen 4):** the diagnostic block is meant to always let the owner compare "this month" against history; the brand-new-deployment empty state explicitly describes "one row for the current month with 0."
- **Actual:** `_render_month_panel` (`core/portal/quota.py:253-276`) renders exactly `db.monthly_push_history()`'s own rows — a plain `GROUP BY yyyymm` over `push_ledger`. When no row exists yet for the current month, there is no row, no "0", and no "← this month" marker anywhere in the By-month table (the marker only attaches to a row that exists). The gauge panel elsewhere on the same page does still show the current month correctly (it reads `monthly_push_total` directly) — only the month-history table has this gap.
- **Suspected cause:** `quota.py:262-274`, the `for r in rows` loop never synthesizes a zero row for `current = _current_yyyymm()` when it's missing from `rows`.
- **Note:** the *brand-new-deployment* case (zero rows entirely) is handled by a dedicated empty-state branch and is fine; this only affects an established deployment having one quiet current month. Not re-counted as a second finding — `test_brand_new_deployment_month_panel_has_no_synthesized_zero_row` documents that the literal "row with 0" UX.md describes isn't actually synthesized there either, same root cause.

### F2 — Quota gauge diverges from STATUS's own gauge on month-heading format and percent formatting (cosmetic, structural)
- **Severity:** LOW / COSMETIC
- **AC:** AC27 / UI.md §3.7 ("the SAME 3-state gauge component")
- **What was tested:** `test_gauge_month_heading_format_diverges_between_status_and_quota_pages` (mine); `tests/test_portal_status_gaps.py::test_status_and_quota_percent_formatting_diverge_on_round_numbers` (sibling STATUS track, cross-checked from QUOTA's side and confirmed reproducible here too).
- **Input:** identical `db`/`config` state, both `/` (Status) and `/quota` fetched.
- **Expected:** `quota.py`'s own module docstring claims the gauge "reuses module STATUS's own `portal_status_quota_*` catalog strings verbatim (one source of truth for one shared component)."
- **Actual:** two independent divergences for the identical live numbers:
  - Heading: STATUS renders `now.strftime("%b %Y")` (e.g. "Aug 2026"); QUOTA renders `_current_yyyymm()` = `"%Y-%m"` (e.g. "2026-08") — both passed to the same i18n key `portal_status_quota_heading`.
  - Percent: STATUS's `_format_pct` trims a trailing `.0` (e.g. "80%"); QUOTA formats inline as `f"{snap.pct:.1f}"` and never trims (e.g. "80.0%").
  - The **numbers themselves** (`used`/`cap`) are identical and consistent across both pages — this is a formatting-helper-reuse gap, not a data-correctness gap.
- **Correction to IMPL-PORTAL-quota.md:** its "Known limitations" section states *"STATUS's own gauge (which this page's gauge copy is shared with) makes the identical simplification"* re: the raw `YYYY-MM` heading — this is not accurate against the actual `status.py:316` source (`%b %Y`, not raw ISO). Flagging so the claim isn't propagated at integration.
- **Suspected cause:** `core/portal/quota.py:224` and `core/portal/status.py:300,316` independently compute/format `month`/`pct` instead of both calling one shared helper in `layout.py`.

### F3 — Manual "Send digest now" has no shared guard against the separately-scheduled digest job (double-push possible in that window)
- **Severity:** MEDIUM — real, reproduced, money-adjacent (double quota spend + duplicate user-facing messages), but **disclosed by design**, not concealed
- **AC:** AC30 / the dispatch note's own NO-DOUBLE-SEND requirement, at its outer boundary
- **What was tested:** `test_manual_digest_run_concurrent_with_scheduled_digest_job_can_double_push`
- **Input:** 1 owner + 1 member, both digest-on, default digest-mode config. `asyncio.gather` of (a) the manual confirmed `POST /quota/digest-run` and (b) a direct call to `core/digest.py:run_daily_digest` with the same `db`/`channel`, exactly mirroring what `core/jobs.py`'s own `CronTrigger` callback does.
- **Expected (UX.md Flow D, stated as an accepted risk, not eliminated):** *"If today's scheduled digest already went out, people will get it twice."*
- **Actual:** confirmed exactly that — both the owner and the member received 2 pushes each (4 total from 2 independent full runs), because `quota._manual_digest_lock`/`_manual_digest_runs`/`_pending_digest_tokens` are entirely local to `core/portal/quota.py`'s own module state; `core/digest.py:run_daily_digest`'s own docstring is explicit that the ordinary immediate-send path has "no internal dedup, the scheduler owns that" (confirmed against `tests/test_digest.py::test_run_daily_digest_has_no_internal_dedup_the_scheduler_owns_that`).
- **Why this is not scored as a QUOTA-track defect:** a real fix requires a guard SHARED between `core/portal/quota.py` and `core/jobs.py`/`core/digest.py` — none of which are in this module's owned files (`SPEC-LINE-PORTAL.md` §11: QUOTA owns `core/portal/quota.py` + its own test file only). UX.md's own copy already discloses this exact risk to the owner at confirm time, and Q3's own framing ("mechanism is open, behavior [replay-safety] is not") was scoped to the manual path's own replay-safety, which IS solid (see the 6-point trace above) — safety against the independent scheduled job was never in scope for this guard.
- **Recommendation:** worth a cross-cutting follow-up at Archi/integration level (e.g., a `db`-backed "digest sent today" marker shared by both call sites) if the owner finds the duplicate-push risk unacceptable in practice; out of scope for this track to fix.

### F4 — A mid-fan-out send failure vanishes from both the "sent" and "skipped" counts
- **Severity:** LOW (reporting-honesty gap, not money-adjacent — no double-send, no over-spend)
- **AC:** AC30 / UX.md Flow D error branch ("never claim a clean run after a partial one")
- **What was tested:** `test_partial_mid_fanout_failure_is_not_counted_as_sent_or_skipped`
- **Input:** 1 owner + 2 members, all digest-on; a channel double that raises for one member mid-fan-out.
- **Expected:** result banner accounts for every candidate (sent + skipped = candidates).
- **Actual:** `Location: /quota?ran=2.0` — 2 sent, 0 skipped, for 3 real candidates. The failed member is silently absent from the arithmetic: `core/digest.py:_send_one_user_digest`'s own fail-open `try/except` catches the send exception and returns `False`, but `quota.py:_digest_candidates`'s `skipped` count is computed once, BEFORE the run, from `digest_opt_out` only, and is never updated for an in-run failure.
- **Suspected cause:** `core/portal/quota.py:_run_digest_now` (`quota.py:500-515`) — `skipped` is a static pre-run count, not derived from the actual run outcome.
- **Note:** root behavior (fail-open, skip-and-continue) lives in shared `core/digest.py`, not QUOTA's own file; only the REPORTING gap (the banner not reflecting it) is QUOTA-owned.

## OQ4 ruling assessment (warn/stop state source)

`quota.py:_quota_snapshot` derives `warn_fired`/`stop_fired` purely from
`total` vs the active cap, per SPEC-LINE-PORTAL.md §9 OQ4's own named
fallback ("derive purely from total vs cap and show the thresholds
without the 'already fired' flag"), rather than reading `LineChannel`'s
private `_quota_warned_months`/`_quota_stopped_months` in-memory sets (no
accessor exists; adding one is out of this pass's owned files). **Ruling
assessed as sound**: the derived value is always live and correctly
reflects a month rollover without a restart, whereas the in-memory flag
(once-per-process-lifetime) would under-report "not fired" after a month
rolled over mid-uptime. No counter-evidence found.

## Quota-gate interplay findings (beyond the double-send trace)

- **Refusal is visible, not just a status code:** `test_stopped_quota_message_is_visible_on_the_page_not_just_a_redirect_code` follows the 303 and confirms the localized "Push cap reached" text actually renders — **PASS**.
- **Mid-fan-out, cap-1 with 3 users:** **pinned, not a bug** — neither `quota.py` (single pre-check only) nor `channels/line.py:LineChannel._push` (its cap gate is REALTIME-mode-only; digest mode is "a pure pass-through," per that file's own docstring) re-checks the cap per user. All 3 candidates send; the reported count is honest (no silent partial cutoff), just an overrun past cap — consistent with digest mode's pre-existing, system-wide lack of a per-push cap (the automatic scheduled digest has the identical characteristic).

## Registration / integration note

Confirmed structurally (`test_quota_register_not_yet_in_server_registered_modules_known_gap`): `core/portal/server.py:REGISTERED_MODULES` is `[status.register]` only. `quota.register` (like `users.register`/`audit.register`) is **not yet wired into the real portal app** — this is a known, pre-existing gap across all three parallel modules, not something this track failed on; this pass registered `quota.register` directly in its own test harness (mirroring both Luna's own `tests/test_portal_quota.py` and the sibling `tests/test_portal_status_gaps.py`). **Inconsistent precedent for integration to settle:** STATUS self-registered into `REGISTERED_MODULES` at build time; USERS/AUDIT/QUOTA did not. A pin test is left in place so this becomes a visible, self-updating signal (it will start failing, on purpose, the moment integration appends `quota.register`).

## Regressions detected

None. Full regression sweep (`tests/test_portal_quota.py`, `test_portal_quota_gaps.py`, `test_portal_status.py`, `test_portal_status_gaps.py`, `test_portal_server.py`, `test_portal_security.py`, `test_portal_layout.py`, `test_portal_db.py`, `test_portal_integration.py`, `test_portal_deploy.py`, `test_i18n.py`, `test_config.py`, `test_access.py`, `test_digest.py`): **383 passed, 0 failed**. Full LINE gate: **5498 passed, 4 skipped, 1 xfailed, 1 failed** (the 1 failure is the sibling AUDIT track's own, unrelated, pre-existing finding test — see Summary).

## Recommendation

**Ready to ship** — every owned AC (AC26–AC30) is green, and the load-bearing NO-DOUBLE-SEND guard held under every probed angle for the manual trigger's own replay-safety (6/6 scenarios correct). Four non-blocking findings recorded for Luna/Archi awareness:
- **F3 (MEDIUM)** is the one worth Archi's attention as a cross-cutting follow-up decision (shared manual/scheduled dedup) — it is disclosed to the owner today, not hidden, and not fixable inside this module's owned files.
- **F1, F2, F4 (LOW)** are cosmetic/diagnostic-quality gaps, safe to fix opportunistically or defer.

No production code was modified by this pass.
