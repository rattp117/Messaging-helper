# Test Report — v1.7.0 `habitdef` track (`/addhabit` / `/delhabit`)

## Summary
- Total: 224 tests (101 Luna's `tests/test_habitdef.py` + 123 new adversarial tests in `tests/test_v17_habitdef_gaps.py`)
- Passed: 224
- Failed: 0
- Status: **PASS**

Full project suite (all tracks, including the parallel `sweep` track's `tests/test_v17_isolation_sweep.py`):
**3323 passed, 1 skipped, 1 xfailed, 0 failed** (was 3200 passed at Luna's handoff; delta is exactly my 123 new tests). No regressions.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_habitdef.py` (Luna's, unmodified) | 101 | AC-H1–AC-H6, all R-V1–R-V5/R-C1/R-C2 |
| `tests/test_v17_habitdef_gaps.py` (new, mine) | 123 | AC-H1–AC-H6 adversarial probes (see below) |

`tests/test_v17_habitdef_gaps.py` sections: pipe-grammar edge cases (12 tests: duplicate keys, empty/whitespace-only values, stray pipes, no-`=` tails, full-width chars, tab/newline normalization, overlong values, unknown extra keys, case handling via Thai-alias trigger) · exhaustive `reserved_trigger_words()` sweep (every word in the set tried as both an id and a label — 51 label tests + 33 ASCII-id tests + 6 cross-check tests, not a sample) · unit-collision interplay with a base habit's own preparse token (2 tests) · Thai-language confirmation/error replies (6 tests — Luna's own execute_* tests are almost all `lang="en"`) · audit old/new-value content (2 tests) · larger Thai-alias adversarial corpus (8 tests) · AC-5 byte-identical regression re-check at this module's own surface (7 tests).

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC-H1 (create) | `test_habitdef.py::test_execute_addhabit_creates_row_and_confirms_bilingually` (byte-identical to spec §3.1's own EN example) + `test_v17_habitdef_gaps.py::test_execute_addhabit_success_reply_is_thai_when_lang_is_th` (Thai-language confirmation, byte-checked) | **PASS** |
| AC-H2 (validation) | `test_habitdef.py`'s ~30 parametrized validation tests + `test_v17_habitdef_gaps.py`'s grammar-edge-case suite (duplicate keys, empty values, full-width id chars all correctly rejected, whitespace normalization, cap boundary at exactly 20/21) — every invalid case confirmed no-write | **PASS** |
| AC-H3 (label/id collision safety) | `test_habitdef.py`'s sampled reserved-word tests + `test_v17_habitdef_gaps.py`'s **exhaustive** sweep of all 51 words in `reserved_trigger_words()` tried as id and as label (en/th), plus a cross-check that each real trigger still dispatches as its own command (not swallowed by addhabit/delhabit) | **PASS** |
| AC-H4 (unit collision degrades) | `test_habitdef.py::test_addhabit_colliding_unit_is_excluded_from_preparse_lookup_ac_h4` + `test_v17_habitdef_gaps.py::test_colliding_custom_unit_also_disables_the_base_habits_own_preparse_token` (confirms the two-way exclusion also affects the BASE habit's own token for that user, and is correctly per-user-isolated) | **PASS** |
| AC-H5 (delete semantics) | `test_habitdef.py`'s archive/hard-delete/already-archived/undone-log-still-counts tests + `test_v17_habitdef_gaps.py`'s Thai-language delete/archive replies | **PASS** |
| AC-H6 (`/habits`) | `test_habitdef.py`'s owner-vs-member isolation + archived-omitted tests + `test_v17_habitdef_gaps.py::test_habits_overview_shows_thai_label_for_thai_input_th_lang` | **PASS** |

Scope boundary honored per instructions: `main.py` routing and `/help` menu wiring (post-both-tracks integration step, SPEC-v1.7.md §11) are **not** tested here and are **not** counted as failures — tested at the `commands.dispatch` + `execute_*` layer only, as directed.

## Adversarial findings (all confirmed correct — no bugs)

- **Pipe grammar duplicate keys**: last occurrence wins (e.g. `id=first|...|id=second` → `"second"`), no crash, no ambiguity error. Not spec-mandated behavior either way; documented as the observed contract.
- **Empty required-key values** (`en=` with nothing after it): correctly treated the same as a missing key → `addhabit_usage`, no write. Confirmed at both the dispatch layer (`fields["en"] == ""`) and validation layer.
- **Full-width characters in id** (full-width digits `１２３`, full-width Latin `ｒｅａｄｉｎｇ`): correctly rejected — `_HABIT_ID_RE`'s `^[a-z0-9_]+$` is ASCII-only by construction, so these never match. `addhabit_invalid_id`, as expected.
- **Whitespace normalization**: internal tabs/newlines in an id collapse to `_` exactly like spaces (`_normalize_id`'s `\s+` regex), not just the ASCII-space case Luna's own tests exercised.
- **Exhaustive reserved-word sweep**: all 51 words currently in `commands.reserved_trigger_words()` (both EN and TH literals, across every command in the app) are rejected as both an id and a label, with zero exceptions found. The single-source-of-truth claim in R-V3 holds up under full enumeration, not just the ~7-word sample in Luna's own suite.
- **Unit-collision cross-effect (worth flagging explicitly, not a bug)**: creating a custom habit with a unit token that collides with a BASE habit's own unit (e.g. `unit=ml` colliding with `water`'s `ml`) excludes **both sides** from the per-user preparse lookup — `water`'s own "500ml" no longer preparses for that user either, falling through to the LLM. This is the existing v1.5 `units.build_unit_lookup` two-way exclusion rule (unchanged), now correctly operating over the per-user registry exactly as R-V4 specifies ("now operating over the per-user registry"). Confirmed correctly per-user-isolated: a second user who never created the colliding habit is unaffected. This is expected/spec-normative behavior, not a defect — flagging because it's a real, easy-to-miss behavioral consequence of creating a custom habit with a common unit name (e.g. a user who names a unit "min" also degrades `stretch`'s own preparse for themselves).
- **Thai-language replies**: spot-checked success, reserved-word-error, cap-reached, archived, and deleted replies all render correctly in Thai via `i18n.t(..., "th", ...)`, byte-matched against the catalog.
- **Audit old→new content (observation, not a failure)**: `execute_addhabit`'s `habit_create` row does capture `new_value=<type>` (e.g. `"duration"`), but `execute_delhabit`'s `habit_archive`/`habit_delete` rows pass neither `old_value` nor `new_value` (`core/habitdef.py:412-414`) — both come back `None`. AC-7's own text only requires the action + localized label (both present and correct), so this is **not** a spec violation. But it means `/audit`'s trail can't show what a deleted/archived habit *was* (type/label) from that row alone, unlike e.g. `checkin_set`'s audit rows which always carry before/after window values (the pattern `execute_checkin` "mirrors" per R-A1's own text). Worth a one-line follow-up if Archi/the user wants audit parity, but does not block release.
- **Thai-alias false-positive corpus**: expanded to 8 additional ordinary-Thai sentences containing เพิ่ม/ลบ/นิสัย individually, glued, reordered, and split by whitespace — zero false positives, and `ลบนิสัย` confirmed to never collide with undo's own `ลบ` trigger in either direction.
- **AC-5 regression re-check**: ordinary log messages (incl. the AC-6 Thai-numeral/full-width lock cases `๕๐๐ มล`/`５００ml`) and a zero-`user_habits` owner's registry remain byte-identical to pre-v1.7 behavior at this module's own dispatch surface.

## Failures (if any)

None.

## Regressions detected

None. Full suite: 3323 passed / 0 failed / 1 skipped / 1 xfailed (same skip/xfail set as Luna's handoff baseline).

## Recommendation

**Ready to ship** — all 6 owned ACs (AC-H1–AC-H6) PASS, no regressions, adversarial probing found no defects (only one non-blocking audit-content observation, documented above for optional follow-up). The `main.py`/menu integration step remains correctly out of this track's scope per SPEC-v1.7.md §11 and is unaffected by this report.
