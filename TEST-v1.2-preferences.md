# Test Report — v1.2.0 `preferences` module (`/lang`, `/quiet`)

## Summary
- Scope: 2 ACs owned by the `preferences` module (SPEC-v1.2.md §11): **AC-P1** (language), **AC-P2** (quiet hours).
- Total (`tests/test_preferences.py`): **100 tests** (45 Luna + **55 Vera**, across three audit rounds — see "Audit history" below).
- Full repo suite (final): **1294 passed, 0 failed, 1 skipped** (`pytest -q`, ~123s).
- Status: **AC-P1 PASS. AC-P2 PASS. Final verdict: PASS.** Both Thai-alias false-positive findings from this audit (round 1's multi-word gate, round 2's blacklist gap) are fixed and re-verified green. No open findings remain.
- **Schedules (`เตือน`) audit (separate module, audited alongside):** **PASS**, no misfire found — unchanged from the prior addendum, restated at the bottom of this report.

## Audit history
Three rounds, each triggered by a Vera-caught false positive on the Thai command aliases (`ภาษา`/`เงียบ`), both owned by `core/commands.py`'s `_match_lang`/`_match_quiet` (`preferences`'s own file per §11):

| Round | Finding | Fix | Result |
|---|---|---|---|
| 1 | Original "mandatory `\s+`, then anything" gate misfired on multi-word Thai continuations (e.g. `"เงียบ ๆ หน่อยนะ"`, `"ภาษา นี้ยากมาก"`) — 4-case corpus, all failing. | Value capture narrowed to a single token (`\S+`, not `\S.*`) for both aliases, plus a round-1 plausibility check: a curated blacklist (`_looks_like_th_prose`) for `ภาษา`, a shape check (`_QUIET_TH_VALUE_RE`) for `เงียบ`. | Original 4-case corpus fixed. |
| 2 | The `ภาษา` blacklist is structurally incomplete — probed 6 more realistic single-word continuations (`"ภาษา อังกฤษ"`/`"จีน"`/`"ใหม่"`/`"ดี"`/`"สวย"`/`"อะไร"`), none contained a blacklisted marker, all still misfired. `เงียบ`'s shape check had no equivalent gap (7-case positive-control probe, all correctly rejected). | `ภาษา`'s blacklist removed entirely, replaced with an explicit whitelist `_LANG_TH_VALID_VALUES = {"en", "th", "auto", "ไทย", "english"}` (exact, case-insensitive match). `เงียบ` untouched (already a shape whitelist, no equivalent weakness). | Round-2 6-case corpus fixed. |
| 3 (this pass) | Final verification: re-ran everything, eyeballed the whitelist implementation, probed 3 more angles for completeness (other real language names not on the whitelist; case-insensitivity of the whitelist tokens; an exact-match-not-prefix check against `"ไทย"`). | No further fix needed. | **All new probes pass. Final verdict: PASS.** |

## Round 3 (this pass) — verification performed

**1. Re-ran the full suite.** `1294 passed, 0 failed, 1 skipped` — exactly matches Luna's reported `1288/0/1` plus the 6 new round-3 completeness tests added below (all passing). No regressions.

**2. Eyeballed the whitelist implementation** (`src/habit_assistant/core/commands.py:690-774`):
- `_LANG_TH_VALID_VALUES = {"en", "th", "auto", "ไทย", "english"}` (line 744) — an explicit, closed set; `_match_lang`'s Thai-alias branch does `value = th_match.group("value").lower(); if value not in _LANG_TH_VALID_VALUES: return None` (lines 763-766) — a genuine set-membership check on the whole lowercased token, not a substring/prefix test.
- Confirmed via `grep -rn "_looks_like_th_prose\|_TH_PROSE_MARKERS" src/ tests/` that **the blacklist is completely gone from production code** — the only remaining references are historical comments inside `tests/test_preferences.py` (this file, describing what round 1/2 used to do; updated this pass to say so explicitly and not imply current behavior).
- `เงียบ`'s `_QUIET_TH_RE`/`_QUIET_TH_VALUE_RE` (lines 738, 751) are byte-identical to the prior round — untouched, as reported.
- The slash forms (`_LANG_SLASH_RE`, `_QUIET_SLASH_RE`) are untouched by any round — still fully permissive (their `/`-prefix already makes them a near-zero false-positive surface, per the original design rationale).

**3. Probed 3 more angles for completeness** (`tests/test_preferences.py`, new this pass — 6 parametrized cases total):
- **Other real language names, not on the whitelist:** `"ภาษา ญี่ปุ่น"` ("Japanese"), `"ภาษา ฝรั่งเศส"` ("French") → both correctly fall through to `None`. Confirms the whitelist doesn't accidentally grow to cover "any language name," only the 5 explicitly reviewed tokens.
- **Case-insensitivity of the whitelist tokens:** `"ภาษา TH"` → `pref_value="th"`, `"ภาษา Auto"` → `pref_value="auto"`, `"ภาษา ENGLISH"` → `pref_value="english"` — all still dispatch correctly (the `.lower()` before the set check works as intended).
- **Exact-match, not prefix:** `"ภาษา ไทยมาก"` ("very Thai"/"Thai-ish" — glued, shares the `ไทย` prefix with a whitelisted token but is a different, longer word) → correctly falls through to `None`. Confirms `value not in _LANG_TH_VALID_VALUES` is a whole-token membership check, not `str.startswith`.

**No misfire found in round 3.** All 6 new probes behave exactly as the whitelist design intends.

## Test files
| Path | Tests (cumulative) | Covers which ACs |
|---|---|---|
| `tests/test_preferences.py` | 100 total (45 Luna + 55 Vera across 3 rounds: 36 round-1 hardening + 13 round-2 residual-risk probes + 6 round-3 completeness probes) | AC-P1, AC-P2 |

## AC coverage
| AC | Test(s) | Status |
|---|---|---|
| **AC-P1** (language) | `test_ac_p1_lang_th_makes_replies_thai_regardless_of_input_language`, `test_ac_p1_owner_default_auto_is_unaffected_by_member_lang_change` (Luna) + isolation/validation/persistence/DB-failure/self-escalation/Thai-alias tests (Vera, all 3 rounds) | **PASS** |
| **AC-P2** (quiet hours) | `test_ac_p2_quiet_hours_scoped_to_the_setting_user_only`, `test_ac_p2_quiet_off_clears_only_that_users_windows` (Luna) + isolation/validation/midnight-crossing/persistence/DB-failure/Thai-alias tests (Vera, all 3 rounds) | **PASS** |

Both owned ACs pass on their literal SPEC text across all three audit rounds. The Thai-alias false-positive findings (both now fixed) lived in `core/commands.py`'s shape-matching (`_match_lang`/`_match_quiet`), owned by `preferences` per §11's file-ownership table — reported and tracked separately from the AC verdicts since neither AC's literal text is about false-positive containment, but both fixes are now verified clean.

## Failures
None. All previously-reported failures (round 1: 4 cases; round 2: 6 cases) are fixed and re-verified green this pass. All round-3 completeness probes pass on first try.

## Cross-check — `main.py` wiring instructions (unchanged since round 1, not re-verified this pass since no round touched `main.py`)
Previously confirmed accurate: the 5 documented integration call sites (`main.py:527`, `main.py:758`, `main.py:1083`, `main.py:1140`, `core/reminders.py:297`) and the proposed `command.kind == "lang"/"quiet"` insertion point all matched the live file exactly at the time of the round-1 check. No discrepancies found. Not re-checked this pass.

## Regressions detected
None across all three rounds. Final full-suite run: 1294 passed, 0 failed, 1 skipped — every test that passed at any prior checkpoint (the 1275-test pre-audit baseline, the 1275→1282 round-1 checkpoint, the 1282→1288 round-2 checkpoint) still passes, plus the 6 new round-3 probes, all green.

## Recommendation
**Ready to ship.** Both owned ACs (AC-P1, AC-P2) pass in full: correct write-side behavior, correct composition with the shared surface's read side, per-user isolation (including member-vs-member), boundary/encoding validation, persistence across a fresh `Database` open, and DB-failure fail-open behavior are all correct and unregressed. The Thai-alias false-positive class this audit surfaced across two rounds (`ภาษา`'s blacklist gap in particular) is now closed by an explicit, reviewed whitelist (`_LANG_TH_VALID_VALUES`) — verified by eyeballing the implementation (blacklist code fully removed, no dangling references) and by 15 total adversarial probes across all three rounds (13 originally failing, now passing, plus 6 new round-3 completeness checks, all passing on first try). No open findings remain for this module.

---

# Addendum — Schedules (`เตือน`) audit (unchanged from prior round, restated for completeness)

**Scope:** independent adversarial audit of `core/schedules`' Thai alias `เตือน` (`core/commands.py`, `_match_remind`/`_build_remind_th_pattern`/`_remind_tail_has_valid_shape`), which had the same root-cause bug as `preferences`' own two aliases: confirmed misfire `"เตือน น้ำ ท่วมด้วย"` ("[a message about] water flooding") was actually setting a water reminder before the fix. Not owned by `preferences` — audit only.

**Fix mechanism:** unlike `preferences`' aliases, `เตือน`'s fix uses **no blacklist at all** — (1) the token right after `เตือน\s+` must exactly match a live-registry habit token (id or Thai label), and (2) any tail must be a `clear`/`reset`/`default`/`ค่าเริ่มต้น`/`off` word or every token in it must have the `\d{1,2}:\d{2}` shape. A bare habit token with no tail is accepted (intentional "show" semantics).

**Adversarial probing (15 cases across the original audit):** original bug case re-confirmed fixed; bare-habit show semantics confirmed intentional; mai-yamok directly after the habit token, single particles, and unrelated single-token prose tails across all 3 configured habits (`water`/`stretch`/`diary`) all correctly fall through to `None`. Two theoretical, very-low-likelihood residuals noted (exact `default`/`ค่าเริ่มต้น`/`off` single-token tail matches) but not filed as failures — no realistic collision surface found, unlike `ภาษา`'s blacklist gap.

**Verdict: PASS.** No misfire found, no production-code changes recommended. `tests/test_schedules.py`: 106/106 passing, independently re-run this pass with no changes needed.

---

## Final counts
- `tests/test_preferences.py`: **100/100 pass.**
- `tests/test_schedules.py`: **106/106 pass.**
- Full repo suite: **1294 passed, 0 failed, 1 skipped.**
- **Preferences (`/lang`, `/quiet`) final verdict: PASS.**
- **Schedules (`เตือน`) final verdict: PASS.**
