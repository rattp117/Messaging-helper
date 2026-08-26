# Test Report — v1.9.0 "Life happens" + Recap wrapped card — FINAL RELEASE GATE

## Summary

- Baseline before this pass: 4229 passed / 0 failed / 1 skipped / 1 xfailed.
- New tests added this pass: **27** (`tests/test_v19_release_gate.py`, all independently designed — no test file from any prior Luna/Vera was modified).
- Final full-suite run (`.venv\Scripts\python.exe -m pytest -q`, `PYTHONPATH=src`, foreground): **4256 passed, 0 failed, 1 skipped, 1 xfailed** (188.8s).
- 30/30 acceptance criteria: **PASS**.
- **Status: PASS — release gate cleared. Recommend: ship v1.9.0.**

No production code was touched. `data\habits.db` was never opened — every test here uses a scratch `tmp_path` SQLite file.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_v19_release_gate.py` (new, this pass) | 27 | AC3 (wired-level burst-delta isolation), AC9 (milestone week-wording — previously untested anywhere), AC17 (available→used wired transition — previously untested), AC20 (all 5 sites incl. the weekly-review trends-block second call site — previously untested; + actual, not assumed, fail-open posture per site), cross-feature interactions (cadence×pause, grace×cadence, pause×cadence×wrapped, backfill×pause/grace, undo×cadence), "system" audit source + no regression to existing actions, menu counts (22/27, both languages), `/help` render-budget headroom, grace message unconditional silence, `RELEASE_NOTES["1.9.0"]` readiness |

Pre-existing v1.9 test files (unmodified, re-verified as part of this gate's full-suite run): `tests/test_cadence.py` (49) + `tests/test_v19_cadence_gaps.py` (85 total per module Vera's report) · `tests/test_grace.py` (15) + `tests/test_v19_grace_gaps.py` · `tests/test_pause.py` (57) + `tests/test_v19_pause_gaps.py` (59, across 3 rounds) · `tests/test_wrapped.py` (65) + `tests/test_v19_wrapped_gaps.py` (21) · `tests/test_v19_shared_surface.py` (79) · `tests/test_v19_integration.py` (12, wired-level).

## 30-AC coverage map

| AC | Test(s) | Result |
|---|---|---|
| AC1 (migration 012 additive/idempotent) | `test_v19_shared_surface.py::test_migration_012_creates_all_three_tables_idempotently`, `::test_migration_012_touches_no_existing_data` | **PASS** |
| AC2 (compute_streak byte-identical at every named call site) | `test_v19_shared_surface.py`'s `test_gate_*` (vs. a literal pre-v1.9 reference impl) + `::test_gate_review_records_dashboard_call_sites_agree_across_a_year_boundary`; reinforced `test_v19_release_gate.py::test_ac3_ordinary_log_stays_byte_identical_even_with_celebrate_burst_enabled` | **PASS** |
| AC3 (HARD BYTE-IDENTICAL GATE) | Full suite: 4256/0/1/1xf (superset of 3799 v1.8.1 baseline). Wired-level burst-delta isolation: `test_v19_release_gate.py::test_ac3_milestone_confirmation_delta_is_exactly_the_documented_burst_append`, `::test_ac3_milestone_confirmation_fully_byte_identical_when_celebrate_burst_disabled`, `::test_ac3_dashboard_review_summary_records_carry_no_v19_marker_for_an_unengaged_user`, `::test_ac3_ordinary_log_stays_byte_identical_even_with_celebrate_burst_enabled` | **PASS — THE RELEASE GATE** |
| AC4 (classify_day NEUTRAL/MISSED) | `test_v19_shared_surface.py::test_classify_day_qualified_neutral_missed` + 3 siblings; reinforced by `test_v19_release_gate.py`'s backfill tests (real `classify_day` calls) and the paused-cadence-week test | **PASS** |
| AC5 (config defaults) | `test_v19_shared_surface.py`'s config-defaults section | **PASS** |
| AC6 (font byte-identical + Thai glyphs) | `test_v19_shared_surface.py`'s font section (empirical byte-for-byte PNG proof) + `test_wrapped.py`/`test_v19_wrapped_gaps.py::test_noto_sans_thai_is_actually_registered_in_the_font_manager` | **PASS** |
| AC7 (`/cadence` set/off/validate/audit) | `test_cadence.py` + `test_v19_cadence_gaps.py` (N=1/N=7 boundaries, off-idempotent) | **PASS** |
| AC8 (`/addhabit cadence=<N>w` atomic) | `test_cadence.py` + `test_v19_cadence_gaps.py` (malformed shapes, 1w/7w boundaries) | **PASS** |
| AC9 (week wording everywhere) | `test_v19_integration.py::test_cadence_week_met_shows_week_wording_in_dashboard_habits_and_review` (dashboard/`/habits`+weekly-review) + **`test_v19_release_gate.py::test_ac9_cadence_habit_milestone_crossing_uses_week_wording_at_the_wired_level`** (milestone — closes a hole nobody had tested) + `test_cadence.py`'s records integration test (view/celebration unit) | **PASS** |
| AC10 ("X of N this week") | `test_v19_integration.py` + `test_cadence.py::test_weekly_progress_*`/`::test_cadence_status_line_*` | **PASS** |
| AC11 (rest days don't break cadence streak) | `test_cadence.py::test_three_per_week_rest_days_do_not_break_the_streak` + `test_v19_cadence_gaps.py`'s year-boundary test | **PASS** |
| AC12 (records week-count storage/celebration) | `test_cadence.py::test_records_stores_and_celebrates_a_week_count_for_a_cadence_habit` | **PASS** |
| AC13 (grace bridges a genuine miss) | `test_grace.py::test_bridges_a_single_miss_and_the_held_streak_reads_as_preserved` + `test_v19_integration.py`'s nightly-tick test + `test_v19_release_gate.py`'s cross-feature grace tests | **PASS** |
| AC14 (kind message once, gentle, never repeats) | `test_grace.py` + `test_v19_integration.py`'s nightly-tick test (silent, once) + **`test_v19_release_gate.py::test_grace_message_is_always_silent_even_when_silent_proactive_is_false`** (send-side specifically) | **PASS** |
| AC15 (second miss same week not bridged) | `test_grace.py::test_second_miss_same_week_not_bridged_streak_breaks_normally` | **PASS** |
| AC16 (cadence habit never bridged) | `test_grace.py::test_cadence_habit_never_bridged` + `test_v19_cadence_gaps.py` + **`test_v19_release_gate.py::test_grace_bridge_stays_historical_after_the_habit_later_gets_a_cadence_no_new_bridges`** (a habit that HAD a bridge, then gets cadence added — the historical row persists, no new ones appear) | **PASS** |
| AC17 (`/habits` grace balance + enabled=false byte-identical) | `test_grace.py::test_grace_status_line_available_then_used`/`::test_grace_status_line_empty_when_disabled` + `test_v19_release_gate.py::test_ac3_dashboard_review_summary_records_carry_no_v19_marker_for_an_unengaged_user` (🛟 present in `/habits` for everyone, absent everywhere else) + **`::test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge`** (wired available→used transition — previously untested) | **PASS** |
| AC18 (audit exactly-once, bilingual) | `test_grace.py::test_audit_row_recorded_once_and_renders_bilingually` + `test_v19_integration.py`'s nightly-tick test + **`test_v19_release_gate.py::test_system_audit_source_renders_bilingually_and_existing_actions_are_unchanged`** (both languages + no regression to a pre-existing `command`-sourced row rendered on the same page) | **PASS** |
| AC19 (`/pause`/`/resume` write+confirm+audit, idempotent) | `test_pause.py::TestAC19PauseResumeBasics` | **PASS** |
| AC20 (proactive suppression, all 5 sites) | `test_v19_integration.py` (4 sites in one flow) + **`test_v19_release_gate.py`'s 5 dedicated per-site tests** (`test_pause_gating_{reminders,checkins,nudge,daily_summary,weekly_review}_site_excludes_only_the_paused_habit`) + **`::test_pause_gating_weekly_review_trends_block_also_excludes_only_the_paused_habit`** (review.py's SECOND, previously-untested call site) + **`::test_pause_gating_fail_open_posture_actually_observed_at_each_site`** (see "Findings" below) | **PASS** (with a findings note, not a gate failure) |
| AC21 (pause holds streak across gap) | `test_pause.py::TestAC21EngineHoldsStreakAcrossPause` + round-2/3 early-resume truncate tests + **`test_v19_release_gate.py::test_paused_cadence_habit_week_is_held_neutral_not_broken`** (the cadence×pause combination, not previously exercised) | **PASS** |
| AC22 (dashboard/`/habits` ⏸ marker + voluntary log still logs) | `test_v19_integration.py::test_dashboard_and_habits_show_the_pause_marker_and_held_streak` + `test_pause.py`'s status-reply tests (round-3 truncate-filter fix) | **PASS** |
| AC23 (reactive celebration during pause still fires) | `test_pause.py::TestAC22AC23VoluntaryLogDuringPauseStillQualifies` + `test_v19_integration.py`'s voluntary-log step + **`test_v19_release_gate.py::test_paused_cadence_habit_suppresses_proactive_but_a_voluntary_log_still_counts_toward_the_week`** (cadence-specific: a paused day's real log still counts toward "X of N this week", not just "still logs") | **PASS** |
| AC24 (over-cap/invalid-date rejection) | `test_pause.py::TestAC24Validation` | **PASS** |
| AC25 (`/wrapped`/`/recap`, month tail, PNG+caption) | `test_wrapped.py` + `test_v19_integration.py::test_wrapped_command_through_real_dispatch_sends_a_png` | **PASS** |
| AC26 (per-user, registry-generic, cadence-aware, reuses records/trends/heatmap) | `test_wrapped.py` + `test_v19_wrapped_gaps.py` (real `RegistryProvider` leak-resistance) + **`test_v19_release_gate.py::test_wrapped_card_for_a_user_with_paused_and_cadence_and_custom_habit_simultaneously`** (all three features on one card at once — not previously exercised) | **PASS** |
| AC27 (Thai glyphs, fallback never raises) | `test_wrapped.py` + `test_v19_wrapped_gaps.py` (font-manager registration proof, clipping-bug geometry regression) | **PASS** |
| AC28 (month-end auto-send default off, silent, pause/DND-aware) | `test_v19_integration.py::test_wrapped_auto_send_default_off_is_a_true_no_op`/`::test_wrapped_auto_send_enabled_sends_silent_card_and_skips_fully_paused_user` | **PASS** |
| AC29 (emoji-burst gated by celebrate_burst) | `test_wrapped.py` + `test_v19_wrapped_gaps.py` (module-level contract) + **`test_v19_release_gate.py`'s AC3 burst-delta tests** (the real `main.py` append wiring, end-to-end, both enabled and disabled) | **PASS** |
| AC30 (RELEASE_NOTES + `/help` + menu) | `test_v19_integration.py::test_ac30_*` (release note exists+announces, help mentions, public menu = 22) + **`test_v19_release_gate.py::test_public_and_owner_menu_counts_both_languages`** (owner = 27, both languages, and the owner menu is a strict superset of the public one) + `::test_help_text_has_the_four_new_command_lines_and_grace_capability_line_bilingual_within_budget` + `::test_release_notes_1_9_0_is_announce_ready_bilingual_and_non_empty` + `::test_release_notes_1_9_0_actually_announces_once_per_active_user` | **PASS** |

**30/30 PASS.** No acceptance criterion is untestable, ambiguous, or unimplemented.

## Deferred-slice hole-check — what was actually closed by this pass

Per the dispatch, I verified (not assumed) that every slice the module Veras had explicitly deferred to integration is now real:

- **AC9/AC10 renderer wording**: confirmed live in `/habits`, `/dashboard`, weekly-review's duration line (pre-existing `test_v19_integration.py` test), **and now the milestone confirmation line too** (`test_ac9_cadence_habit_milestone_crossing_uses_week_wording_at_the_wired_level` — nobody had tested `milestone_reached_weeks` firing through the real confirmation path before this pass; it works correctly).
- **AC13–14 send side**: the grace nightly tick genuinely sends, once, silently, regardless of `silent_proactive` (both `test_v19_integration.py` and my own dedicated test).
- **AC17 `/habits` line**: present for every daily habit by default (AC17's own wording), and now proven to transition from "available" to "used {weekday}" wording after a real bridge (`test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge` — previously only proven with hand-built ledger state at the module level, never through a real nightly tick + a second `/habits` read).
- **AC20 all-5-sites gating**: all five sites individually proven to suppress ONLY the paused habit (including `review.py`'s second, previously-untested call site — the embedded trends block inside the weekly-review narrative, distinct from the stats-section call site the existing suite already covered).
- **AC22 ⏸ marker**: unchanged from `test_v19_integration.py`'s own proof; re-confirmed clean on this run.
- **AC28 config-gated auto-send default-off**: unchanged, re-confirmed.
- **AC30**: public=22 was already proven; **owner=27, both languages, is newly proven in this pass** (it also happens to already be pinned in `tests/test_v18_release_gate.py`'s own literal, which stayed green — this pass adds an independent, v1.9-scoped confirmation rather than relying solely on that older file).

## Cross-feature interaction probes (beyond any single module's own scope)

All new, in `tests/test_v19_release_gate.py`:

1. **Paused cadence habit — week held NEUTRAL across a fully-paused prior week**, with a control proving the identical unpaused shape breaks the streak (`test_paused_cadence_habit_week_is_held_neutral_not_broken`).
2. **Paused cadence habit — proactive suppression + a voluntary log still counts toward "X of N this week"**, not just "still logs" (`test_paused_cadence_habit_suppresses_proactive_but_a_voluntary_log_still_counts_toward_the_week`).
3. **Grace bridge stays historical after the habit later gets a cadence; no new bridges afterward** (`test_grace_bridge_stays_historical_after_the_habit_later_gets_a_cadence_no_new_bridges`).
4. **`/wrapped` card with a paused + a cadence + a custom habit simultaneously** — real PNG, no crash (`test_wrapped_card_for_a_user_with_paused_and_cadence_and_custom_habit_simultaneously`).
5. **The 00:05 grace job vs. an actively-paused habit — no bridge, no message** (`test_grace_tick_never_bridges_a_paused_daily_habit_no_ledger_row_no_message`).
6. **Backfill into an already-paused day** (real wired dispatch, `"3000ml 3 days ago"`) **and into an already-grace-protected day** (real ledger row + a real backdated insert) — both flip from NEUTRAL to QUALIFIED, per Rule 16 (`test_backfill_into_an_already_paused_day_counts_as_qualified_not_neutral`, `test_backfill_style_insert_into_a_grace_protected_day_counts_as_qualified_not_neutral`).
7. **Undo of a log that had made a cadence week MET** — the week un-meets, and the streak drops from 1 to 0 (`test_undo_of_a_log_that_had_made_the_cadence_week_met_drops_the_streak`).

All 7 scenarios: **PASS**.

## Failures

None.

## Regressions detected

None. Full suite: 4256 passed / 0 failed / 1 skipped / 1 xfailed, up from the 4229/0/1/1xf baseline by exactly the 27 tests this pass added.

## Findings for the record (none block release)

1. **Fail-open posture is inconsistent across the 5 pause-gating sites** — verified directly with an `active_pauses`-raising DB wrapper, not assumed (`test_pause_gating_fail_open_posture_actually_observed_at_each_site`):
   - `reminders.send_reminder` — explicitly wrapped in try/except; a pause-read error is logged and the reminder sends anyway (habit-granular fail-open, matches this codebase's `_goal_already_met`/AC9.5 convention).
   - `nudge.build_nudge_message` — no internal try/except of its own, BUT `nudge.run_due_nudges`'s pre-existing (v1.6, not v1.9-added) per-user try/except catches it one level up: the tick survives, only that one user's nudge is skipped for the day.
   - `checkins.build_checkin_message`, `streaks.compute_daily_summary`, `review.compute_weekly_stats` — no protection at all. A pause-read error propagates out of the function; for `checkins` and the two proactive fan-out jobs in `main.py` (`daily_summary_job`, `weekly_review_job`) that call these with no per-user try/except of their own around the call, this would abort that entire tick for **every** active user that run, not just the one whose read failed.
   - Not spec-mandated (R15 never states a fail-open requirement for the pause check itself), and `db.active_pauses` is a plain, low-risk `SELECT` no different from a dozen other unprotected reads elsewhere in this codebase — so this is not filed as a blocking defect. Flagging because the blast radius differs meaningfully by site (one habit/user vs. every active user that tick) and Archi may want it evened out in a follow-up.
2. **Grace defaults ON, unlike every other v1.9 feature.** `[grace] enabled = true` by default (AC5, spec-mandated) means an existing v1.8.1 user, without doing anything, will have their very first genuine miss on any daily habit auto-bridged the first night `grace_tick` runs after upgrade — a real (if silent, if kind) behavior change with zero opt-in, unlike `cadence`/`pause`/`wrapped` (all require an explicit command) and unlike `wrapped.auto_send` (explicitly defaulted off "to avoid surprise proactive image sends," per its own config docstring). This is fully spec-compliant and by design (R8), not a bug — flagging purely so the rollout's first-night behavior for existing users is a conscious choice, not a surprise.
3. **Dead i18n catalog keys**: `dashboard_line_goal_weeks`/`dashboard_line_boolean_weeks`/`dashboard_line_count_weeks` (en+th) are defined but never referenced — `dashboard.py`'s cadence branch renders through `cadence.cadence_status_line` instead, never reaching these. Harmless, zero behavior impact.
4. **Pre-existing, not new to v1.9** (confirmed by `wrapped`'s own Vera and re-confirmed here by reading both modules): `core/wrapped.py:_build_fallback_text` and `core/heatmap.py:_build_fallback_text` never apply `core/render_budget.py`'s message-length guard. Noted per Archi's own dispatch instruction, not a gate.
5. **Shelved, documented**: `/resume <habit>` does not "smart-split" an active all-habits pause into per-habit rows (the literal, tested, and now-truthful-reply reading of R13). A one-function change in `core/pause.py:execute_resume` if product wants the smarter behavior later.

## Recommendation

**PASS — ship v1.9.0.** All 30 acceptance criteria pass, the hard byte-identical gate (AC3) holds at both the engine level (full 4256-test suite, 0 failures) and the wired confirmation-text level (the only delta for an unengaged user is the AC29-documented, default-on celebration burst — verified to vanish completely when `celebrate_burst` is disabled). Every previously-deferred slice (AC9's milestone wording, AC17's wired available→used transition, AC20's fifth call site) is now closed with a passing test. No regressions. The five findings above are informational — none require a code change before shipping.
