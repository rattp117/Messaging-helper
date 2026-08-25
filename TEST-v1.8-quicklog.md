# Test Report — v1.8.0 `quicklog` module (quick-log keyboard + reactions)

## Summary
- Total: 265 tests in the quicklog-relevant subset (61 `test_quicklog.py` + 11 `test_reactions.py` + 38 `test_v18_quicklog_gaps.py` + `test_commands.py` + `test_v18_shared_surface.py`, all foreground)
- Passed: 265 · Failed: 0
- **Status: PASS** (revised — see "Re-verification" below; superseded the initial FAIL from round 1)
- Scope: module-level only (`core/quicklog.py`, `core/reactions.py`, `commands._match_log`, the disjoint `commands.py`/`i18n.py` regions). `main.py` routing/wiring is the later integration pass — see "Deferred to integration" below (unchanged from round 1, not re-litigated here).

## Round 1 (initial review) — recap
First pass found 4 real defects via adversarial probing (`tests/test_v18_quicklog_gaps.py`, then 37 tests, 33 pass / 4 fail), none in Luna's own 72 tests:
1. **(load-bearing)** `handle_log_callback` never consulted the tapping user's stored `/lang` preference — only `source_text` auto-detection — breaking AC-A2 (same confirmation as typing) and AC-A6 (follows the user's language) whenever the keyboard prompt didn't carry a detectable language marker.
2. `_LOG_CALLBACK_RE`'s bare `\d` matched any Unicode decimal digit (not ASCII-only), so a forged Arabic-Indic/Thai-digit payload bypassed validation and wrote a real log row — an AC-A3 gap.
3. `_round_ladder_step` floored a negative goal's rungs to a spurious `1.0` button instead of skipping the habit.
4. `_format_amount` rendered huge+fractional goal rungs in scientific notation (`%g`), producing a dead button whose own callback_data couldn't be tapped.

Recommendation was "hand back to Luna — 4 failures," all confined to `core/quicklog.py`.

## Re-verification (round 2)

### Diff review — each fix confirmed sound
Read `core/quicklog.py` in full (file is untracked/new, so no `git diff` baseline exists — compared directly against my round-1 reading):

1. **Language preference** (lines 222-238, 278): a new same-file `_stored_language_pref(db, user_id)` was added, structurally identical to `main.py:_stored_language_pref` (try `db.get_user`, except `Exception` → log + `"auto"`, else `row["language_pref"]` or `"auto"`). `handle_log_callback` now calls `i18n.resolve_reply_language(source_text, config, user_pref=_stored_language_pref(db, chat_id))` — matches `main.py:740`'s own call shape exactly. **Verified by direct probe** (script, not just the test suite): with a stored `th` preference and an English-only `source_text`, typed and tapped paths now both resolve `"th"`; reverse-checked with a stored `en` preference and a Thai-only prompt — both correctly resolve `"en"` (confirms the fix threads `user_pref` through `resolve_reply_language`'s real precedence — global force > stored pref > text detection — not just "prefer Thai"). A missing `users` row falls back to `"auto"` without crashing (fail-open, matches `main.py`'s own contract).

   **Follow-up note (not a FAIL, per Archi's steer):** this is now the *fourth* independent copy of the same ~10-line helper in this codebase (`main.py`, `core/access.py`, `core/reminders.py`, now `core/quicklog.py`). The existing three already establish the convention, so this isn't a new pattern — but a shared `i18n.stored_language_pref(db, user_id)` (or similar) would remove the silent-drift risk of four copies diverging one small edit at a time. Since `main.py` is the integration seam and this touches multiple modules' files, this is a suggestion for Archi to consider at integration or as a future refactor, not a blocker.

2. **`re.ASCII`** (line 210): `_LOG_CALLBACK_RE` now compiled with `re.ASCII`. Direct probe: both Arabic-Indic (`٥٠٠`) and Thai (`๕๐๐`) digit payloads are now rejected by the regex; the habit-id group (`[a-z0-9_]`) was already ASCII-only and is unaffected. Sound, minimal, correct fix.

3. **`_round_ladder_step`** (lines 89-106): now returns `float(rounded)` unconditionally when `rounded > 0`; only floors to `1.0` when the *rounded* value is non-positive **and** the original `x` was positive; otherwise passes the non-positive value through unchanged so `_goal_ladder`'s `value > 0` guard filters it. Direct probe confirms **all previously-correct positive-goal ladders are unchanged**: `ladder(7)=[2,4,7]`, `ladder(1)=[1]`, `ladder(2500)=[625,1250,2500]`, `ladder(20)=[5,10,20]`, `ladder(2)=[1,2]` (tiny-goal dedup case) — and `ladder(-5)` is now `[]` as required, `ladder(100_000_000)` stays ascending/positive. No regression on the "never hits zero" contract for small positive goals.

4. **`_format_amount`** (lines 68-81): switched from `%g` to fixed-point `.6f` with trailing-zero/dot trim, matching `_LOG_CALLBACK_RE`'s own value grammar (≤15 integer digits, ≤6 decimal digits) on the write side. Direct probe across normal + edge values (`250→"250"`, `600→"600"`, `2.5→"2.5"`, `0.5→"0.5"`, `100.5→"100.5"`, `1200.5→"1200.5"` — confirms `rstrip("0")` doesn't over-strip a value like `1200.5` down into the integer part's own trailing zero, `123456789.123→"123456789.123"` now round-trips through `_LOG_CALLBACK_RE` — previously produced `"1.23457e+08"` and failed to match). No regression on integer-valued amounts (unchanged code path).

### Test run (foreground, no background waits)
`tests/test_quicklog.py` + `tests/test_reactions.py` + `tests/test_v18_quicklog_gaps.py` + `tests/test_commands.py` + `tests/test_v18_shared_surface.py`: **265 passed, 0 failed** (matches Archi's reported 264; delta of 1 is a new defensive test I added during re-verification — see below).

### Test file updates (Vera's own file only — no production code touched)
Updated `tests/test_v18_quicklog_gaps.py`'s 4 finding docstrings from "documents current broken behavior" to "regression guard, fixed" (assertions themselves were already correct-behavior assertions and needed no changes — they now pass because the implementation caught up to them). Also strengthened the language-preference test:
- Added the **reverse-direction** check (English stored pref must still win over a Thai-only prompt) — proves the fix respects the real precedence order, not just "prefer Thai."
- Added `test_handle_log_callback_missing_user_row_falls_back_to_auto_no_crash` — defensive coverage for the new `_stored_language_pref` copy's fail-open path (no `users` row → `"auto"`, no crash).

## AC coverage (final)
- **AC-A1** (keyboard from per-user registry) → **PASS**. All prior findings (negative-goal spurious button, huge-fractional-goal dead button) fixed and now covered as regression guards.
- **AC-A2** (`log:` callback logs, same confirmation + undo + dashboard refresh, incl. language) → **PASS**. Value/type parity (Luna's 6 byte-identical tests) and language parity (Vera's fixed regression guard, both directions) both hold.
- **AC-A3** (ownership + safety) → **PASS**. All malformed/oversized/unicode-digit/SQL-ish/archived-habit/cross-user-write cases rejected correctly.
- **AC-A4** (reaction on typed log, fail-open) → module-level **PASS** (unchanged from round 1). Full wiring **deferred to integration** (unchanged).
- **AC-A5** (reaction scope) → module-level **PASS** (unchanged from round 1). Full contract **deferred to integration** (unchanged).
- **AC-A6** (bilingual, zero-LLM) → **PASS**. Confirmation-follows-language now holds (was the AC-A2-linked failure, now fixed); bilingual labels + zero-LLM structural checks unchanged, still PASS.

## Regressions detected
None. Full quicklog-relevant subset green; no previously-passing test broken by the fix round.

## Deferred to integration (unchanged from round 1 — main.py wiring, per SPEC-v1.8.md §11)
- `/log` command routing to `quicklog.build_keyboard` + `keyboard_prompt_text`/`empty_keyboard_hint`.
- `on_callback` dispatch of the `log:` prefix to `quicklog.handle_log_callback`.
- The actual `reactions.react(...)` call site after a successful typed log (AC-A4 full wiring; AC-A5 full "never for taps/undo/commands/clarify/deferred" contract).
- End-to-end two-user integration scenarios per SPEC-v1.8.md §11 integration order.

## Known non-module limitation (not a module FAIL, unchanged from round 1)
`channel.send_actionable` renders `list[Button]` as a single-row inline keyboard (`channels/telegram.py:build_send_actionable_request`) — a habit-rich registry's quick-log keyboard will render as one long single row rather than multiple rows. Pre-existing shared-surface constraint, not owned by `quicklog.py`/`reactions.py`. For Archi to handle at the integration/shared-surface layer.

## Recommendation
**Ready to ship.** All 6 owned ACs (AC-A1–AC-A6) PASS at the module level; the 4 round-1 findings are fixed and verified both by re-running the original adversarial tests and by independent direct-probe scripts (not just re-running the same assertions). One non-blocking follow-up for Archi: consider a shared `i18n`-level `stored_language_pref` helper instead of a fourth per-file copy, to remove future silent-drift risk across `main.py`/`core/access.py`/`core/reminders.py`/`core/quicklog.py` — appropriate to raise at the `main.py` integration pass, not a gate on this module.
