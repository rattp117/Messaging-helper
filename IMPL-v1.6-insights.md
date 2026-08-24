# Implementation — v1.6.0 `insights` module (Personal bests & records, Deterministic trends)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/records.py` | Created | Feature 3 (R-R1-R-R4): `update_on_log` (recomputes + upserts `best_day`/`best_week`/`longest_streak` after a log, returns newly-broken records), `format_celebration` (renders the `record_broken` suffix line(s)), `render` (`/records [habit]` view). Also exposes `week_day_strs`/`period_total`/`period_entry_count` — the shared day/week aggregation `core/trends.py` reuses (same `insights` module). |
| `src/habit_assistant/core/trends.py` | Created | Feature 4 (R-T1-R-T3): `compute` (per-habit `HabitTrend`: this-week-vs-last-week totals, delta, %, rising/falling run-length), `render` (`/trends [habit]` view), `review_block` (weekly-review integration surface, mirrors `core/garmin.py:format_garmin_section`'s "return a block, caller appends it" contract). |
| `src/habit_assistant/core/commands.py` | Modified | My section only: `_RECORDS_SLASH_RE`/`_TRENDS_SLASH_RE`, `_build_insights_th_pattern`, `_match_insights_kind`, `_match_records`, `_match_trends` (inserted after the `dashboard` section, before `query intent`); two `dispatch()` routing branches (inserted after the `heatmap` branch, before `_match_help`). Did not touch any other module's section. |
| `src/habit_assistant/core/i18n.py` | Modified | One new catalog block (`record_broken_*`, `records_*`, `trends_*`, `help_records_cmd`, `help_trends_cmd` — 26 new keys, EN+TH) appended after the `heatmap` module's block, before the closing `}`. Did not touch any other module's keys. |
| `tests/test_records.py` | Created | 43 tests: dispatch shape + adversarial corpus, `update_on_log` semantics (strictly-greater, once-per-break, week-boundary/config-tz, streak-record interplay with the real `streaks.compute_streak`, undo-does-not-revert, isolation, fail-open), `format_celebration`, `render` (empty/filled/invalid-habit/registry-generic/isolation/fail-open/zero-LLM). |
| `tests/test_trends.py` | Created | 37 tests: dispatch shape + adversarial corpus, `compute` math (spec-sample numbers, negative delta, no-history, zero-previous-but-real-history, history-gap, rising/falling run-length incl. run-breaks-on-reversal, partial-current-week, boolean aggregation, isolation, registry-generic), `render`, `review_block`. |

No other files touched (migrations, db.py, channels, audit, config, config.toml, main.py, review.py, discoverability.py, dashboard/heatmap/nudge files all untouched by this pass, per scope).

## How it works

A log lands → `main.py`'s integration step will call `records.update_on_log(db, config, registry, habit, user_id, clock)` right after `db.insert_log` (the same spot `streaks.crossed_milestone` already runs, before the per-habit confirmation is sent). It recomputes today's total, this rolling-7-day-week's total (`period_total`, sum for numeric/duration, count for boolean/text — `count_true` for boolean), and the current streak (`streaks.compute_streak`, the real production streak engine — no re-derivation), and upserts any of `best_day`/`best_week`/`longest_streak` that a stored value didn't already beat. Per migration 009's own documented design ("the first log that would otherwise set a record is itself the first 'beaten' crossing"), a brand-new record (no row yet) is itself a crossing — both stored *and* celebrated, matching that already-landed shared-surface decision. The returned `(record_type, value)` pairs feed `format_celebration`, which renders one bilingual line per break for the caller to append to that log's confirmation (`"\n\n" + records.format_celebration(...)`, mirroring `streaks.py`'s own `milestone_suffix` pattern exactly).

`/records`/`/trends` (and their Thai aliases `สถิติ`/`แนวโน้ม`) are recognized by two new `commands.py` matchers that reuse `/history`'s existing `_parse_history_tail` for the slash-form tail grammar (habit filter + trailing int, int discarded) and a small parameterized Thai-alias builder (mirrors `_build_history_th_pattern`'s registry-anchored false-positive hardening). `records.render`/`trends.render` are pure, read-only, registry-generic, wrapped in a top-level fail-open `try/except` (SPEC-v1.6.md §3.4: read-only surfaces never raise) that degrades to a friendly `*_render_failed` message on any DB/compute error. `trends.review_block` is a self-contained block for the weekly review to append, built on the same `compute()` trends.py's own `/trends` view uses, so the two surfaces can never disagree.

## Smoke test done

- Ran `commands.dispatch(...)` directly for `/records`, `/records water`, `/records water 8` (int silently dropped), `สถิติ`, `สถิติน้ำ` (glued → `None`), `/trends`, `แนวโน้ม`, `แนวโน้มเศรษฐกิจแย่ลง` (→ `None`), plus ordinary logs (`500ml`) — all matched/rejected as designed.
- Ran `records.update_on_log` end-to-end against a real on-disk SQLite `Database`: first log establishes+celebrates `best_day`/`best_week` (goal not yet met on that log, so `longest_streak` correctly withheld); a 5-day consecutive `stretch` streak walked day-by-day showed `longest_streak` climbing 1→2→3→4→5 (celebrated every day, confirmed intentional per the migration-009 "first log is itself a crossing" design applied to a still-growing all-time-best streak); confirmed a *new* streak that hasn't yet caught up to an existing higher stored record does **not** re-fire until it truly exceeds it; confirmed `db.soft_delete` (undo) on the record-setting log does **not** revert the stored record.
- Ran `trends.compute`/`render`/`review_block` against real seeded weekly totals: reproduced SPEC-v1.6.md §3.3's own sample numbers exactly (`2450 → 2780 ml (+13%)`); verified a 4-week strictly-increasing sequence yields `rising_weeks == 4`, a 4-week decreasing sequence yields `falling_weeks == 4`; verified a week-boundary gap (no entries at all in the immediately-preceding week) correctly reports `has_history=False` ("not enough history yet") rather than treating older data as last week's; verified a week with real entries summing to `0` (e.g. a `0ml` log) reports `has_history=True` but `pct_change=None` (no divide-by-zero, no misleading %).
- Ran `pytest tests/test_records.py tests/test_trends.py -q` → **80 passed** (43 + 37), 0 failed.
- Ran the **full suite** (`pytest tests/ -q`) four times while the three sibling parallel tracks (`dashboard`/`heatmap`/`nudge`) were still landing concurrent edits in the same working tree. Two of those runs each showed exactly one transient failure, in each case inside a *different* sibling module's own gap-test file (`tests/test_nudge_gaps.py::test_fail_open_fan_out_...`, then later `tests/test_heatmap_gaps.py::test_collision_sweep_...`) — both traced to that module's own source file changing shape *during* the run (confirmed by re-diffing `core/nudge.py`'s docstring/`try` structure and by both failing tests passing cleanly standalone immediately after, with zero code changes from me). **Zero** failures were ever traceable to this pass's own files (`core/records.py`, `core/trends.py`, my `commands.py`/`i18n.py` sections, `tests/test_records.py`, `tests/test_trends.py`) across any run.
- **Final confirmed full-suite run** (all four parallel tracks settled): `pytest tests/ -q` → **2960 passed, 0 failed, 1 skipped, 1 xfailed** in 144.21s. A follow-up targeted run of `tests/test_records.py tests/test_trends.py tests/test_commands.py tests/test_i18n.py tests/test_i18n_literals.py` → **215 passed**, 0 failed — confirms my own additions plus every shared-file test I touch stay green in isolation too.

## Maps to acceptance criteria

- **AC-R1** (stored + updated) → `core/records.py:update_on_log`/`_maybe_break_record` (upserts via `db.upsert_record` only on a genuine improvement); `tests/test_records.py::TestUpdateOnLog` (10 tests covering strictly-greater, equal-does-not-fire, smaller-never-downgrades, multi-type-together).
- **AC-R2** (celebrate once, fail-open) → `update_on_log`'s strictly-greater/first-crossing logic + its whole-body `try/except`; `format_celebration` for the suffix text; `tests/test_records.py::test_fail_open_never_raises_and_returns_empty_list`, `test_second_smaller_log_same_day_does_not_break_or_celebrate_again`, `test_equal_value_does_not_celebrate_strict_inequality`.
- **AC-R3** (`/records` view) → `core/records.py:render`/`_habit_block`/`_record_line`; `commands.py:_match_records`; `tests/test_records.py::TestRender` (12 tests) + `TestDispatchShape` (9 tests).
- **AC-T1** (`/trends` week-over-week + delta/%) → `core/trends.py:compute`/`_compute_one`; `tests/test_trends.py::TestCompute::test_matches_spec_sample_numbers_exactly` + `test_negative_delta_and_pct`.
- **AC-T2** (review block + run-length callout) → `core/trends.py:review_block`, `_run_lengths`, `_format_trend_line`'s rising/falling suffix (gated at `>= 2`); `tests/test_trends.py::TestReviewBlock` + `test_rising_run_length_counts_weeks_in_the_monotonic_run`, `test_falling_run_length`, `test_run_breaks_on_a_flat_or_reversed_week`, `test_single_up_week_run_length_is_two_not_one`.
- **AC-T3** (insufficient history, no divide-by-zero/misleading %) → `_weekly_totals_backward`'s gap-detection (stops the backward walk at the first entry-less week) + `_compute_one`'s `has_history`/`pct_change` gating; `tests/test_trends.py::test_no_history_at_all`, `test_previous_week_zero_total_but_real_history_no_divide_by_zero`, `test_gap_in_history_treated_as_no_last_week_data`.
- **AC-X1** (registry-generic) → both `render`/`compute` iterate `registry` with no hardcoded habit ids; `test_registry_generic_extra_habit_appears_automatically` (records), `test_registry_generic_iterates_every_configured_habit` (trends).
- **AC-X3** (per-user isolation) → every DB call scoped by `user_id`; `test_isolation_two_users_independent_records`/`test_isolation_two_users_see_only_their_own_records` (records), `test_isolation_two_users_independent_trends` (trends).

AC-1/AC-2/AC-3/AC-X2 are shared-surface/integration-verified per SPEC-v1.6.md §11 — not claimed by this module.

## Known limitations

- **First-ever record seeds silently, no celebration** (RESOLVED — see Iteration log below; this bullet is now describing the shipped, ruled-on behavior, not an open question). A habit's very first log for a given record type stores the row (so a later log has a baseline to beat) but never appears in `update_on_log`'s returned list — nothing is celebrated on a fresh baseline.
- **`longest_streak` re-celebrates every day of a still-growing, never-before-matched streak, STARTING FROM DAY 2.** Day 1 seeds silently (see above); day 2 onward, each additional consecutive qualifying day IS a new all-time high over the previous day's stored value, so it re-celebrates daily until the streak either breaks or is later matched to a still-higher pre-existing record. This is a direct consequence of R-R1's own "stored, not re-derived" + "strictly exceeds" design applied to the real `streaks.compute_streak` engine (verified in `test_longest_streak_grows_daily_while_it_is_the_all_time_best`) — distinct from, and layered on top of, the existing milestone-crossing feature (which only fires at specific *configured* lengths). Documented behavior, not a bug; flagging since it's a meaningfully different cadence from milestones.
- **`/records`/`/trends` dates render as plain ISO strings** (`2026-08-24`, `2026-08-18–2026-08-24`) rather than SPEC-v1.6.md §3.3's illustrative `"(12 Aug)"`/`"(5–11 Aug)"` formatting — matches this codebase's own established convention for every other bilingual date already shown (`core/review.py`'s `stats_water_line`/`DayValue.day`), avoiding inventing a new localized month-name formatter. Flagging as a deliberate, precedent-driven deviation from the spec's sample text (not from any binding rule — §3 is described as illustrative output).
- **Best-day/best-week values never unit-auto-scale** (e.g., SPEC-v1.6.md's own sample shows `3200 ml` for a day but `18.1 L` for a week — a unit switch). This app has no ml↔L auto-scaling anywhere else (`habit.unit(lang)` is always the one configured unit); I render both at the habit's configured unit consistently (`18100 ml`, not `18.1 L`). Same "illustrative sample, not a literal contract" reasoning as the date formatting above.
- **`review_block`/`update_on_log`/`format_celebration` are not yet wired into `main.py`/`core/review.py`.** Per SPEC-v1.6.md §11, wiring is the integration pass's job — see "Integration call sites" below for the exact insertion points I've identified.
- Two module-local private `_today` helpers (one each in `records.py`/`trends.py`) are intentionally duplicated rather than shared, matching this codebase's own established convention for this exact "resolve today from an injectable clock + config timezone" shim (`core/checkins.py`/`core/nudge.py` each independently duplicate their own `_today_str`/`_now_hhmm`, per those modules' own docstrings) — not an oversight.

## Integration call sites (for the coordinator's later integration pass — not applied by this pass)

1. **Import**: add `records`, `trends` to `main.py`'s `from habit_assistant.core import (...)` block (alphabetically).

2. **Records celebration hook** — in `handle_inbound_message`, in the *same* per-habit-type confirmation block that already computes `milestone_suffix` (right after `streaks.crossed_milestone`, before the `water`/`stretch`/`diary`/generic confirmation sends, `main.py` around line 887-891):
   ```python
   record_suffix = ""
   broken = records.update_on_log(db, config, registry, habit, user_id, clock=clock)
   if broken:
       record_suffix = "\n\n" + records.format_celebration(broken, habit, lang)
   ```
   Then append `+ record_suffix` alongside the existing `+ milestone_suffix` on every confirmation send (water/stretch/diary/generic — all four call sites, mirroring how `milestone_suffix` is already appended to each).

3. **Trends review block** — in `core/review.py:run_weekly_review`, after the existing `garmin_section` append:
   ```python
   trends_section = trends.review_block(db, config, registry, lang, user_id, clock=lambda: datetime.combine(end_date, datetime.min.time()))
   if trends_section:
       text += f"\n\n{trends_section}"
   ```
   (or thread a `clock` param through `run_weekly_review` the same way `end_date`/`today` already is — either works; `review_block`'s own `compute()` just needs "today" to resolve to the review's own `end_date`).

4. **Command routing** in `handle_inbound_message`, alongside the existing `"history"` branch:
   ```python
   if command.kind == "records":
       reply = records.render(db, config, registry, lang, user_id, habit_id=command.category)
       if dry_run:
           print(reply)
           return
       assert channel is not None, "channel is required outside dry-run"
       await channel.send(user_id, reply)
       return
   if command.kind == "trends":
       reply = trends.render(db, config, registry, lang, user_id, habit_id=command.category, clock=clock)
       if dry_run:
           print(reply)
           return
       assert channel is not None, "channel is required outside dry-run"
       await channel.send(user_id, reply)
       return
   ```
   Same "before the deferral check" placement as `history`/`help`/`habits` (both are deterministic, LLM-free, read-only — work with Ollama down).

5. **Command menu**: add `RECORDS_COMMAND_DESCRIPTIONS`/`TRENDS_COMMAND_DESCRIPTIONS` dicts (mirror `HISTORY_COMMAND_DESCRIPTIONS`) and append `("records", ...)`/`("trends", ...)` to the `command_menu` list.

6. **`/help` text**: append `help_records_cmd`/`help_trends_cmd` (already in the catalog) to `core/discoverability.py:build_help_text`, same "data only, wiring is a later integration-time append" posture as `help_heatmap_cmd`/`help_checkin_cmd`.

## Iteration log

### Round 1 — Vera: PASS (all 6 owned ACs), one escalated judgment call

Vera's `TEST-v1.6-insights.md`: **PASS** on AC-R1, AC-R2, AC-R3, AC-T1, AC-T2, AC-T3 (92 tests total: my 80 + her 12 new gap-audit tests in `tests/test_insights_gaps.py`). No code defects found. She escalated one judgment call rather than failing it outright: whether a habit's first-ever log should be celebrated as its own "beaten crossing" (my original resolution, traced to `storage/migrations.py:_migration_009_dashboard_and_records`'s own docstring) or seeded silently.

**Archi's ruling (2026-08-24): first-ever log must NOT be celebrated.** My original resolution over-read a migration docstring (my own prose, not spec text) rather than the spec itself; R-R2's "strictly exceeds the stored record" presupposes a stored record to exceed, and celebrating literally every fresh habit's first log is structurally noisier than the milestone precedent it claimed to mirror (milestones never fire on a literal day-1 streak — the default milestone list has no "1"). Ruling: `update_on_log` seeds the stored record row **silently** on a first-ever observation (so future comparisons have a baseline) and returns no celebration; celebrations fire only when an ALREADY-stored record is strictly exceeded. Same rule for all three record types — a first week / first streak also seeds silently.

**Fix applied**: `core/records.py:_maybe_break_record` now returns `None` (no celebration) whenever `current is None`, still calling `db.upsert_record` unconditionally so the row exists for the next comparison. Module/function docstrings updated to describe the new rule and cite the ruling instead of the migration docstring.

**My own tests updated** (mine to change — they encoded the overruled behavior):
- `test_first_log_establishes_and_celebrates_best_day_and_best_week` → renamed `test_first_log_seeds_records_silently_without_celebrating`, now asserts `broken == []` on the first call.
- Added `test_second_log_that_exceeds_the_silently_seeded_baseline_celebrates` (new) — proves silent-seeding only suppresses the *first* observation, not celebration generally.
- `test_longest_streak_grows_daily_while_it_is_the_all_time_best` → expected sequence changed from `[1.0, 2.0, 3.0, 4.0, 5.0]` to `[None, 2.0, 3.0, 4.0, 5.0]` (day 1 now seeds silently).
- `test_week_boundary_uses_config_timezone_not_utc` → no longer asserts a celebration on the (first-ever) seeding call; asserts `broken == []` instead, then verifies the seeded row's `achieved_on` date via `db.get_records` as before (the timezone assertion itself is unaffected by the ruling).
- `test_undo_does_not_revert_an_already_celebrated_record` → restructured to seed a baseline on day 1 (silent), then genuinely break the record on day 2 (celebrated), then undo day 2's log and confirm the record still doesn't revert — preserves the test's actual intent (undo-durability) using a scenario the new rule still allows to celebrate.
- `test_multiple_record_types_break_together_in_one_call` → restructured to seed silently on day 1, then break all three record types together on day 2.

All 44 of my own tests pass (43 + 1 new split-out test); `tests/test_trends.py` (37 tests) is untouched and unaffected (trends.py has no first-log-celebration logic).

**Vera's own tests — one conflict found, NOT edited, flagging per instruction**: `tests/test_insights_gaps.py::test_boolean_habit_best_week_uses_count_true_across_the_whole_week` seeds 4 raw log rows directly (bypassing `update_on_log`) and then calls `update_on_log` exactly **once** — making that one call a first-ever observation for `best_week` on that habit — and asserts `("best_week", 3.0) in broken`. Under the new rule this call correctly returns `[]` (silent seed), so this specific test now fails:
```
FAILED tests/test_insights_gaps.py::test_boolean_habit_best_week_uses_count_true_across_the_whole_week
AssertionError: assert ('best_week', 3.0) in []
```
The other 11 of Vera's 12 tests pass unmodified. Per instruction, I have **not** touched this test — reporting it back to Archi for a decision (the test's own intent — proving `best_week` uses `count_true` not raw `count` for boolean habits — is still fully valid and provable; it just needs either a second `update_on_log` call after a baseline exists, or an assertion on the stored `db.get_record` value instead of `broken`).

### Full-suite results after the fix
`pytest tests/test_records.py tests/test_trends.py tests/test_insights_gaps.py -q` → **80 + 37 + 11 passed, 1 failed** (the one flagged conflict above), 0 other failures. Full-suite run pending final reconciliation with the coordinator's stated ~2993-2998 baseline (concurrent `dashboard` round-2 track landing) — see final report to Archi for the confirmed count.
