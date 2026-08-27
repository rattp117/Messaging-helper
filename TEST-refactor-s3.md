# Test Report — Refactor Stage 3 (dispatch table + EASY dedup, v1.9.3)

## Summary

- Total (full suite): 4486 collected — 4484 passed, 1 skipped, 1 xfailed, 0 failed
- New in this stage: `tests/test_refactor_s3.py` — 149/149 passed
- Baseline (pre-Stage-3, i.e. after v1.9.2): 4335 passed / 1 skipped / 1 xfailed (confirmed: 4335 + 149 = 4484, exact match)
- `git status` confirms zero pre-existing test files modified; only `tests/test_refactor_s3.py` is new
- **Status: PASS**

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_refactor_s3.py` (new, Luna's, unmodified, re-verified) | 149 | AC9 (102-case golden precedence corpus + 3 invariant tests + 4 structural-guard tests), AC10 (per-cluster regression + source-sweep tests for all 5 EASY clusters) |
| Pre-existing 4335-test suite (unmodified) | 0 new | AC-G1, AC-G2 (byte-identical regression net) |

My own verification work below (independent probe script, source diffs, direct inspection of `_MATCHERS`/`_assert_dispatch_invariants`) is not a new test file — it's an audit of Luna's claims — but every finding is reproducible via the commands shown.

## AC coverage

| AC | Test(s) / verification | Result |
|---|---|---|
| **AC9** — golden precedence corpus (≥40 cases, all 3 rule-14 invariants + audit/history boundary), table reproduces the old if-chain exactly | `test_refactor_s3.py` golden corpus (102 cases, parametrized) + 3 dedicated invariant tests + 4 structural-guard tests, all green. Independently re-verified: (1) diffed the actual pre-Stage-3 `dispatch()` (`git show HEAD:.../commands.py`, lines 2129-2343) against `_MATCHERS`' row order — **exact match**, 27 rows, same order, including `pause`/`resume` (which SPEC-REFACTOR.md rule 14's own prose summary omits — see "Findings" below, not a Luna defect); (2) ran my own probe script exercising the cadence-vs-query stem, `"change it to xyz"` (edit-trigger-matches/tail-garbled), `ประวัติ`/`ย้อนหลัง` (audit vs history boundary), and plain logs (`"500ml"`, `"10 min stretch"`, `"2 glasses of water today"`) — all matched required behavior; (3) read `_assert_dispatch_invariants` directly — its 4 assertions (no duplicate kinds, `query` last, `cadence` index < `query` index, `edit` is the sole `triggered`-row) correctly encode all 3 rule-14 invariants. | **PASS** |
| **AC10** — 5 EASY clusters consolidated, byte-identical, per-cluster regression corpus | Per-cluster `test_refactor_s3.py` tests all green (regression + source-sweep). Independently re-verified via `git diff` against `HEAD` for every one of the 14 modified consumer files: (a) language-pref → `user_prefs.stored_language_pref` (checked `checkins.py` diff + `user_prefs.py` source — fail-open behavior byte-identical); (b) `_today*`/`_now_hhmm` → `core/timeutil.py` (checked `checkins.py`, `heatmap.py`'s kept wrapper, `reminders.py`'s two deliberately-untouched exceptions, `records.py`, `trends.py` diffs — call-site semantics unchanged, only the implementation moved); (c) `ordinal`/`_ordinal` — confirmed already consolidated in Stage 2, nothing for Stage 3 to do, locked by `test_ordinal_has_exactly_one_definition_across_the_whole_src_tree`; (d) 7 Thai-alias builders → `_registry_th_tokens` (read the helper + `_build_target_th_set_pattern`'s diff — token-collection logic extracted verbatim, trigger literals/group shape untouched); (e) `week_days` → `core/timeutil.week_days` (checked `charts.py`, `garmin.py`, `review.py`, `records.py`, `trends.py` diffs — identical one-liner formula extracted verbatim). All 5 clusters: canonical implementation used, duplicate `def`s independently grepped as gone, behavior unchanged. | **PASS** |
| **AC-G1** — full suite green, byte-identical, zero unsanctioned test edits | Full suite (foreground, `PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q`, no `-n`, no `uv`): **4484 passed, 1 skipped, 1 xfailed, 0 failed** in 202.03s — exact match to baseline (4335) + 149 new. `git status --porcelain` confirms only `tests/test_refactor_s3.py` is new under `tests/`; no `M` against any pre-existing test file. | **PASS** |
| **AC-G2** — byte-identical output probe; `dispatch` stays a pure function of `(text, registry)` | `commands.py` inspected directly: `_MatcherEntry` is `@dataclass(frozen=True, slots=True)`; `_MATCHERS` is built once at module import with no closures capturing mutable state; `_ignore_registry`/`_bool_matcher`/`_resolve_snooze`/`_resolve_edit` are pure adapters with no I/O. No `global`, no cache, no DB/channel import anywhere in the diff. Targeted regression run: `test_checkins.py`, `test_nudge.py`, `test_announce.py`, `test_dashboard.py`, `test_dashboard_gaps.py`, `test_cadence.py`, `test_v19_cadence_gaps.py`, `test_records.py`, `test_charts.py`, `test_garmin.py`, `test_v07_m3_review_extra.py`, `test_heatmap.py`, `test_heatmap_gaps.py`, `test_insights_gaps.py`, `test_v16_integration.py`, `test_v17_habitdef_gaps.py`, `test_commands.py` — **834/834 passed** (these are exactly the modules whose byte-pinned string/PNG-output tests are the primary evidence for `checkins.build_checkin_message`, `nudge.build_nudge_message`, `dashboard.render`, `heatmap.render`, `trends.render`, `records.update_on_log`, `charts.render_habit_chart`, `garmin.build_garmin_report`, `review.compute_weekly_stats`). Stage 1/2 byte-identity gate files (`test_ac17_v060_byte_identical_composite.py`, `test_refactor_s1_gaps.py`, `test_refactor_stage1_tick.py`) also passed as part of the full suite. | **PASS** |

Every AC in this stage's scope (SPEC-REFACTOR.md §8: AC9, AC10, plus cross-cutting AC-G1/AC-G2) is covered above. Stage 1's ACs (AC1–AC5), Stage 2's ACs (AC6–AC8), and Stage 4's ACs (AC11–AC13) are out of scope and untouched (confirmed `core/routing.py`, `core/i18n.py`, `storage/db.py`, `tests/conftest.py` all show no diff against `HEAD`).

## Commands run

```
cd "C:\Users\Demo\OneDrive - Ngow Hock Agency Co,Ltd\Claude-Cowork\Messaging AI assistant"
export PYTHONPATH=src
.venv/Scripts/python.exe -m pytest -q tests/test_refactor_s3.py
  # 149 passed in 1.01s

.venv/Scripts/python.exe -m pytest -q tests/test_checkins.py tests/test_nudge.py tests/test_announce.py \
  tests/test_v17_habitdef_gaps.py tests/test_heatmap.py tests/test_heatmap_gaps.py tests/test_insights_gaps.py \
  tests/test_v16_integration.py tests/test_commands.py
  # 560 passed in 14.92s

.venv/Scripts/python.exe -m pytest -q tests/test_dashboard.py tests/test_dashboard_gaps.py tests/test_cadence.py \
  tests/test_v19_cadence_gaps.py tests/test_records.py tests/test_charts.py tests/test_garmin.py \
  tests/test_v07_m3_review_extra.py
  # 274 passed in 11.15s

.venv/Scripts/python.exe -m pytest -q
  # 4484 passed, 1 skipped, 1 xfailed in 202.03s
```

## Independent AC9 probe (not from Luna's corpus)

```python
commands.dispatch('กี่ครั้งต่อสัปดาห์ น้ำ 3', reg)  # -> Command(kind='cadence', category='water', value_num=3.0)  [invariant i]
commands.dispatch('change it to xyz', reg)          # -> None  [edit trigger matched, tail garbled, terminal]
commands.dispatch('change it to what?', reg)        # -> None  [same, trailing "?" does NOT leak to query — invariant ii]
commands.dispatch('edit that to banana', reg)        # -> None
commands.dispatch('ประวัติ', reg)                     # -> Command(kind='audit')     [not query, not history]
commands.dispatch('ย้อนหลัง', reg)                    # -> Command(kind='history')   [not query, not audit — invariant iii]
commands.dispatch('ประวัติศาสตร์ไทย', reg)             # -> None  [Thai word "history" as prose, not a command]
commands.dispatch('500ml', reg)                      # -> None
commands.dispatch('10 min stretch', reg)             # -> None
commands.dispatch('ดื่มน้ำ 2 แก้ว', reg)               # -> None
commands.dispatch('2 glasses of water today', reg)   # -> None
commands.dispatch('is water good for health?', reg)  # -> Command(kind='query')  [genuine trailing-"?" query]
```

All 12 results match the behavior required by SPEC-REFACTOR.md rule 14 and by inspection of the (unchanged) individual `_match_*` regexes.

## Findings

**1. SPEC-REFACTOR.md rule 14's own prose "Order (27 branches)" list is imprecise — not a Luna defect.**
The spec text reads: `...routine → cadence → query → help → habits` — this both omits `pause`/`resume` (which exist in the codebase since v1.9.0, the SPEC's own stated target version) and places `query` *before* `help`/`habits`, which directly contradicts the spec's own invariant (iii) two sentences later ("`query`... must stay last"). I compared this against the actual pre-Stage-3 code (`git show HEAD:src/habit_assistant/core/commands.py`, the real if-chain Luna converted) and Luna's `_MATCHERS` table matches the **real code** exactly — 27 rows, `undo → edit → snooze → target → remind → access → audit → lang → quiet → checkin → dnd → dashboard → history → heatmap → records → trends → wrapped → addhabit → delhabit → log → routine → cadence → pause → resume → help → habits → query` — with `query` genuinely last. Luna implemented the correct, actual, ground-truth behavior; the spec's prose summary is just an inaccurate paraphrase. Not blocking — flagging for Archi/Sophia as a documentation cleanup, not a code defect.

**2. Minor scale claim in IMPL-refactor-s3.md is imprecise.** The report claims "Net -90 lines" for `commands.py`; actual is 2510 → 2451 = -59 lines (still a net reduction, and `git diff --stat` shows -433/+261 across the whole diff for that file, which nets differently than a raw line-count delta because of docstring/comment churn). Cosmetic only, does not affect any AC.

No functional defects found. No regressions detected.

## Regressions detected

None.

## Recommendation

**Ready to ship.** AC9, AC10, AC-G1, AC-G2 all PASS. Full suite green at the exact expected count (4484/0/1/1xf). Zero pre-existing test files touched. Independent re-derivation of the golden corpus's key invariant cases, direct source diffs of all 14 dedup-consumer files, and direct inspection of `_MatcherEntry`/`_MATCHERS`/`_assert_dispatch_invariants` all corroborate Luna's IMPL-refactor-s3.md claims. Clear to hand back to Archi for the v1.9.3 release (commit + tag + PROGRESS.md update). The two findings above are cosmetic documentation notes, not release blockers.
