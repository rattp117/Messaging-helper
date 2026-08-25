# Test Report — v1.8.0 `routines` module (habit stacks)

## Summary
- Total (this pass): 50 tests (new file, `tests/test_v18_routines_gaps.py`)
- Combined with Luna's own `tests/test_routines.py` (43 tests): **93 tests**, 93 passed
- Passed: 93 / Failed: 0
- Findings: 2 (both documented as passing tests with `FINDING` docstrings; neither is a data-integrity, isolation, or write-safety bug)
- Full-suite regression check: **3755 passed, 0 failed, 1 skipped, 1 xfailed** (excludes 4 pre-existing failures in `tests/test_v18_quicklog_gaps.py`, owned by the concurrent quicklog Vera — not touched, not caused by this pass; confirmed present before and after this test file was added)
- **Status: PASS** (with 2 low-severity findings for Luna/Archi to triage — see Findings)

## Scope

Per dispatch: module-level only. `main.py` integration (routing the
`"routine"` `CommandKind`, dispatching `routine:` callbacks through the
real Telegram channel) is the later integration pass and is **not**
exercised here — every test below calls `commands.dispatch` /
`routines.execute_routine` / `routines.handle_routine_callback` directly
against a real on-disk SQLite `Database` and a real `RegistryProvider`
(no DB mocks). See "Deferred to integration" below.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_v18_routines_gaps.py` (new, mine) | 50 | AC-B1 (14), AC-B2 (3), AC-B3 (6), AC-B4 (2), AC-B5 (2), AC-B6 (2), AC-B7 (1), dispatch corpus (15), create-success (3), 2 findings |
| `tests/test_routines.py` (Luna's, reviewed not modified) | 43 | AC-B1..B7, all green |

## AC coverage

| AC | Description | Result |
|---|---|---|
| AC-B1 | Create + validation, every failure leg leaves no write | **PASS** — bad name (33 chars, Thai chars), name collision, empty/malformed items (Luna's + mine), unknown habit token, unparseable/negative/zero/scientific-notation value, colliding-unit token, cap exactly-at-limit +1 rejected, cross-user habit reference rejected, habit archived before creation rejected, uppercase name normalizes (not rejected). One dispatch-layer gap found — see Finding 1. |
| AC-B2 | List: per-user, items + one run-button each, bilingual | **PASS** — isolation confirmed at the list-render level (not just `get_routine`), 32-char name run-button payload = 44 bytes (within 64), 20-routine × 3-item render-budget test stays ≤4096 chars with buttons kept in lockstep with kept lines |
| AC-B3 | Run: all-valid logs today for acting user, one summary, one dashboard refresh, `routine_run` audited, archived/text items skipped+noted, no celebration text but records ARE updated, all-invalid → no dashboard churn / zero rows | **PASS** — dashboard refresh call count asserted `== 1` (valid run) / `== 0` (all-invalid) via monkeypatch spy (stronger than Luna's exception-absence check); text-habit item skip verified independently of archived-habit skip; record row confirmed to change value (500.0) despite no celebration text in reply |
| AC-B4 | Delete: removes routine + items, audited; unknown name → friendly no-op, no write, no audit row | **PASS** — orphan-row check via direct `routine_items` query after delete |
| AC-B5 | Isolation: per-user, `routine:run:<name>` for a non-owned name is a friendly no-op | **PASS** — same-name routines for two users run fully independently (cross-checked both users' sums after both runs); callback isolation test confirms zero log rows AND that owner A's own routine survives untouched |
| AC-B6 | Migration 011: additive, idempotent, stamps 11, touches no existing data | **PASS** — explicit double `run_migrations()` call on the same connection (not just reopen-a-file); before/after full-table diff (dict equality) on a hand-built v10 DB with `logs`+`users`+`habit_records` rows, not just spot-checked fields |
| AC-B7 | Zero-LLM | **PASS** — Luna's static AST-import check (unchanged) + my own dynamic check: poisons `OllamaClient._post` (the one real network chokepoint) and drives create→list→run→delete end-to-end with no exception, proving no code path reachable from routines.py ever calls the LLM client. (My first attempt at this check used a `sys.modules` presence scan, which produced a false positive under the full suite because unrelated tests legitimately import `ollama_client` earlier in the same process — replaced with the behavioral poison-and-drive approach above; noted for transparency, not a routines.py defect.) |

## Deferred to integration (not exercised in this pass, by design)

- `main.py` routing of the `"routine"` `CommandKind` to `execute_routine`.
- `main.py` dispatching `routine:run:` callback-query payloads to `handle_routine_callback` via the real Telegram channel/`on_callback`.
- Any interaction with the real `TelegramChannel.send_actionable` rendering (list-view button rows) — verified here only against the `Channel` ABC contract via `FakeChannel`.
- End-to-end two-user scenarios through the real bot loop (SPEC-v1.8.md §11 integration-order item 3's "A creates + runs a routine while B sees no trace") — module-level isolation is proven here (AC-B5); the full end-to-end wiring is Archi's sequential integration step per Luna's own IMPL.md.

## Findings

### Finding 1 — dispatch-layer gap: a fully bare "`/routine <name> = `" produces no routine-specific reply (AC-B1, low severity)

- **What was tested:** `commands.dispatch("/routine morning = ", ...)` (literally nothing after `=`, not even a stray token).
- **AC referenced:** AC-B1 ("...each of {..., empty items, ...} → a friendly error with no write").
- **Expected (spirit of AC-B1):** a friendly "empty items" error, same as `/routine morning = water` (habit token, no value) already correctly produces via `execute_routine`'s `routine_create_usage` reply.
- **Actual:** `_ROUTINE_SLASH_CREATE_RE`'s `items` capture group is `(?P<items>.+)$`, which requires **at least one character** after `=`. A completely empty tail fails to match the regex at all, so `commands.dispatch` returns `None` — the message never reaches `execute_routine`, and the user gets **no routine-specific feedback whatsoever** (it silently falls through to the general log/LLM path instead).
- **No-write guarantee still holds** — `db.count_routines(OWNER) == 0` in both the empty-tail and single-token cases — so this is not a data-integrity issue, only a missing-friendly-error gap for one specific malformed-input shape.
- **Location:** `src/habit_assistant/core/commands.py:1599` (`_ROUTINE_SLASH_CREATE_RE`) — `items` group needs to accept zero-or-more (or the bare-equals form needs a dedicated branch that still dispatches with `routine_items=None`).
- **Test:** `tests/test_v18_routines_gaps.py::test_create_zero_items_after_equals_no_write` (passing — asserts the actual, gap-documenting behavior; not a failing test).
- **Suggested severity:** low/cosmetic — narrow input shape, no safety impact, fix is a one-line regex change (`.+` → `.*`) plus confirming `_parse_routine_items("")` already returns `None` (it does, via the "no non-empty segments" branch).

### Finding 2 — Thai-alias regexes use `re.IGNORECASE`, widening the name-shape class beyond the documented ASCII-lowercase invariant (informational, not a functional bug)

- **What was tested:** `commands.dispatch("กิจวัตร Morning", ...)`.
- **Expected per the module's own comment** (`core/commands.py`, directly above `_match_routine`): "a routine name is BY DEFINITION restricted to `^[a-z0-9_]+$` (ASCII lower/digits/underscore)".
- **Actual:** `_ROUTINE_TH_RUN_RE`/`_ROUTINE_TH_CREATE_RE`/`_ROUTINE_TH_DELETE_RE` are all compiled with `re.IGNORECASE`, so their `[a-z0-9_]+` name-shape class **also matches uppercase Latin letters** — `"กิจวัตร Morning"` dispatches successfully with `routine_name="Morning"` (uppercase, not lowercased at the dispatch layer).
- **Why this is not a functional bug:** `execute_routine` unconditionally re-normalizes via `_normalize_name` (`.strip().lower()`) before every DB lookup/write, so `"กิจวัตร Morning"` and `"กิจวัตร morning"` resolve to the exact same routine — verified in `test_match_routine_thai_form_uppercase_latin_name_still_normalizes_correctly` (passing).
- **Why it's still worth a note:** it's a real deviation between the documented invariant and the actual regex, and it marginally widens the Thai-prose false-positive surface the comment argues is closed by that exact ASCII-lowercase restriction (an uppercase-Latin-tail sentence is still an extremely narrow surface in genuine Thai prose, so this is not assessed as a practical false-positive risk — the adversarial corpus in both Luna's and my own test files found no such misfire).
- **Location:** `src/habit_assistant/core/commands.py:1604-1606` (the three `_ROUTINE_TH_*_RE` compiles).
- **Suggested action:** optional follow-up — drop `re.IGNORECASE` from the three Thai-alias patterns to match the module's own stated design, or update the comment to acknowledge the intentional case-insensitivity. Not blocking; no test failure, no data-integrity or isolation impact.

## Regressions detected

None. Full suite (excluding the 4 pre-existing, out-of-scope `test_v18_quicklog_gaps.py` failures owned by the concurrent quicklog Vera, confirmed present independent of this pass): **3755 passed, 0 failed, 1 skipped, 1 xfailed**.

## Final relevant-subset numbers

- `tests/test_routines.py` (Luna's): 43 passed
- `tests/test_v18_routines_gaps.py` (mine): 50 passed
- Combined: **93 passed, 0 failed**
- Full suite (minus concurrent-Vera's known-separate quicklog failures): 3755 passed, 0 failed, 1 skipped, 1 xfailed

## Recommendation

**Ready to ship**, with two low-severity findings for Luna/Archi to triage on a follow-up pass (not blocking this module's PASS status):
1. Widen `_ROUTINE_SLASH_CREATE_RE`'s `items` group (or add a dedicated branch) so a fully bare "`/routine <name> = `" reaches `execute_routine`'s friendly usage message instead of silently not dispatching.
2. Optionally drop `re.IGNORECASE` from the three Thai-alias routine regexes to match their own documented ASCII-lowercase design intent (cosmetic; current behavior is safe due to downstream normalization).
