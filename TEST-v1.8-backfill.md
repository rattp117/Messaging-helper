# Test Report — v1.8.0 `backfill` module

## Summary
- Total: 71 (Luna's `tests/test_backfill.py`) + 64 (Vera's `tests/test_v18_backfill_gaps.py`) = **135 tests**
- Passed: 135
- Failed: 0
- Status: **PASS** (module-level slice — see AC boundary notes below)

Scope note: per IMPL-v1.8-backfill.md's own "Maps to acceptance criteria"
section, AC-C2, AC-C3, AC-C6, and the "residual resolves through the real
pipeline" half of AC-C1 are explicitly deferred to `main.py` integration
(not yet built). Those are **not** exercised or failed here — this report
verifies only what `core/backfill.py` can prove standalone. All six ACs are
listed below with their module-level verdict and their deferred-to-integration
remainder stated explicitly.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_backfill.py` (Luna's, reviewed not modified) | 71 | AC-C1 (extraction slice), AC-C4, AC-C5, pure helpers, structural zero-dep proof |
| `tests/test_v18_backfill_gaps.py` (new, Vera's) | 64 | Fills leading/trailing gaps per phrase body; 40-case larger AC-C5 corpus (habit-name/weekday-word collisions); AC-C4 at a non-default cap + weekday/bound interaction + full-week rotation invariant; AST-based purity proof; ts-format cross-check against the real log-write format; bilingual placeholder/tofu checks; explicit pin for the documented design deviation |

## AC coverage (module-level slice)

- **AC-C1** (relative-date parse, EN+TH, zero-LLM) → `test_ac_c1_documented_phrases_resolve_exactly` (Luna, all 6 documented phrases) + `test_ac_c1_positive_strings_still_match_alongside_the_negative_corpus` (Vera) → **PASS (extraction slice)**. Deferred: residual→habit/value resolution through `preparse`/LLM and the actual DB insert are integration's, not yet wired — not tested here, not failed here.
- **AC-C2** (aggregations reflect it) → **DEFERRED to integration**, not applicable at module level. Building block verified: `test_backdated_ts_matches_the_live_log_write_format_exactly` confirms `backdated_ts` produces the exact `ts` shape (`now.isoformat(timespec="seconds")`, cross-checked directly against `main.py`'s live-log write format at `main.py:986`/`1043`) that every `ts LIKE`/`ts BETWEEN` aggregation already expects.
- **AC-C3** (no retro-celebration) → **DEFERRED to integration** — suppressing the milestone/record line is `main.py`'s branch logic, not expressible in `core/backfill.py`. No module-level test applies; correctly not attempted.
- **AC-C4** (bounds) → PASS. Luna: `test_future_bound_returns_out_of_range_future`, `test_too_old_bound_returns_out_of_range_too_old`, `test_exactly_at_max_days_back_is_in_bounds`, `test_one_past_max_days_back_is_out_of_range`, `test_resolved_date_exactly_today_falls_through_as_none`, `test_resolve_days_back_mirrors_extract_dates_bounds`. Vera additions: `test_bounds_at_a_non_default_cap_exactly_and_one_past` (proves the cap isn't hardcoded to 14), `test_weekday_resolution_can_be_out_of_range_against_a_tight_cap` (weekday×bound interaction, not covered by Luna), `test_extract_date_never_produces_a_future_out_of_range` (structural property — see design note below), `test_resolved_date_exactly_today_falls_through_for_th_days_ago_too`.
- **AC-C5** (conservative, zero false positives — load-bearing) → PASS. Luna: 25-case corpus. Vera: 40-case additional corpus (`MORE_ADVERSARIAL_NEGATIVES`) specifically targeting habit-name/weekday-word collisions ("Monday 5 reps", "friday 3 reps", "จันทร์เต็มดวงคืนนี้", "ศุกร์นี้มีนัดหมอ"), near-miss TH forms ("วันหยุดยาว...", "หยุดพัก 2 วัน"), possessive/pluralized EN near-misses ("last Monday's meeting notes", "gym on Mondays"), and both-sided-content cases. Zero false positives across 65 combined negative cases (25 + 40).
- **AC-C6** (undo) → **DEFERRED to integration** for the write+undo wiring itself. Module-level building block confirmed sound: undo operates by row id (`undo_ui.py`), independent of `ts` ordering — `core/backfill.py` needs no undo-specific logic and has none; nothing here blocks it.

## Design deviation ruling

Luna's `extract_date` rejects a date phrase followed by more trailing
content (`"diary 2 days ago: had a rough day"` → `None`), while accepting
the bare form (`"diary 2 days ago"` → matches). I checked SPEC-v1.8.md
§2.4/AC-C1 for a requirement that would demand the richer, continuation-
tolerant form: **AC-C1's phrase list uses only the bare form** ("diary 2
days ago", no colon/continuation), and R-B1/AC-C5's own anchoring
requirement ("whole leading or whole trailing clause") is worded to exclude
exactly this case. The spec's own dispatch note ("a missed backfill is
recoverable; a misfiled log is not") supports the conservative choice.

**Ruling: PASS with note.** No AC literally requires matching the
continuation form; this is a documented, spec-consistent conservative
choice, not a defect. Pinned as a named regression test:
`test_design_deviation_bare_trailing_phrase_matches_but_continuation_does_not`.

## Additional findings (informational, not failures)

- **Structural future-bound property**: `extract_date` can never itself
  return `OutOfRange("future")` — every §2.4 recognized phrase body only
  subtracts days from `today` (yesterday/N-days-ago/past-weekday). The
  `"future"` branch is reachable only via `resolve_days_back` for the LLM's
  optional `date_offset` (R-B5's "subject to the same bounds"). This
  matches the spec: §2.4 lists no future-referring phrase form. Confirmed
  by `test_extract_date_never_produces_a_future_out_of_range` across all
  ten phrase probes.
- **Purity**: Luna's structural test used a source substring scan
  (checking `"storage.db"` etc. don't appear as text). I re-verified with
  an AST-based import walk (`test_ast_verified_no_forbidden_imports` +
  `test_ast_verified_allowed_imports_are_the_expected_small_set`), which is
  robust against import styles a substring scan could miss. Confirmed: the
  only top-level imports are `re`, `dataclasses`, `datetime`, `typing`,
  `__future__`, and `habit_assistant.core.i18n` — no DB/channel/LLM/network
  import anywhere in the module.
- **Bilingual rendering**: `confirmation_prefix`/`bounds_error_text`
  produce no unresolved `{day}`/`{max_days}` placeholders in either
  language, and the Thai outputs contain genuine Thai-range characters (no
  tofu, no `?`/`TODO` stand-ins), verified with a direct Unicode-range
  check (`test_th_output_contains_real_thai_text_not_tofu_or_placeholder`).
- **`backdated_ts` format**: confirmed byte-shape-identical to `main.py`'s
  live-log `ts` (`now.isoformat(timespec="seconds")`, both 19 chars, `T` at
  index 10), and correct on leap day (2028-02-29) and year boundaries
  (2026-12-31 / 2027-01-01).
- **Full-week rotation invariant**: for every possible "today" across a
  full Mon–Sun week (not just the single fixed Tuesday anchor Luna's suite
  uses), asking for the weekday name that IS today resolves 7 days back,
  never 0 — verified for both EN and TH weekday forms in
  `test_weekday_resolution_never_returns_today_across_a_full_week_rotation`.

## Failures (if any)

None.

## Regressions detected

None. `tests/test_backfill.py` (Luna's 71) unchanged and still green.

## Tree state (for the record, not this module's issue)

`git status` at test time shows the full v1.8 parallel-module set in
flight concurrently in this working tree: `channels/base.py`,
`channels/line.py`, `channels/telegram.py`, `config.py`, `core/audit.py`,
`core/audit_view.py`, `core/checkins.py`, `core/commands.py`,
`core/i18n.py`, `core/nudge.py`, `core/reminders.py`, `main.py`,
`storage/db.py`, `storage/migrations.py` all modified, plus new
`core/quicklog.py`, `core/reactions.py`, `core/routines.py` and their test
files. This matches the dispatch context: other Lunas (`quicklog`,
`routines`, `riders`) are mid-flight. `IMPL-v1.8-backfill.md` already
documented ~20 full-suite failures caused by the in-flight migration 011
(`schema_version` 11 vs tests expecting 10) — confirmed structurally
consistent with `storage/migrations.py`/`storage/db.py` showing as
uncommitted working-tree changes. Per dispatch instructions, I ran only
`tests/test_backfill.py` + `tests/test_v18_backfill_gaps.py` +
`tests/test_i18n.py` + `tests/test_config.py` (the directly related subset)
for this module's verdict — **205/205 passed, 0 failed** — and did not run
or score the full suite against `backfill`, since its failures belong to
`routines`' concurrent migration work, not this module.

## Recommendation

**Ready to ship** (module-level slice: AC-C1 extraction/AC-C4/AC-C5 fully
PASS; AC-C2/C3/C6 and AC-C1's pipeline half correctly and explicitly
deferred to the `main.py` integration pass per SPEC-v1.8.md §11). No
production-code changes needed from Luna for this module. Re-run the
deferred AC-C1/C2/C3/C6 slices, plus the full suite, once `main.py`
integration wires `backfill.extract_date` in and `routines`' migration 011
lands (or is reconciled) — that is the integration-pass Vera's job, not a
rework of this module.
