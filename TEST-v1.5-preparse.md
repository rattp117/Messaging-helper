# Test Report — v1.5.0 module `preparse` (deterministic pre-parser)

## Summary
- Scope: AC-14 (byte-identical confirmations), AC-15 (zero false positives). Module code reviewed: `core/preparse.py` only.
- Total (module scope): 166 (Luna's `tests/test_preparse.py`, pre-existing, re-run) + 591 (new, `tests/test_preparse_gaps.py`) = **757 tests**
- Passed: 757
- Failed: 0
- Full suite (regression check): 2569 passed, 1 failed, 1 skipped
- The 1 failure is `tests/test_announce_gaps.py::test_concurrent_overlapping_calls_send_at_most_once_per_user` — the `announce` track's own known TOCTOU-race failure, unrelated to `preparse` (confirmed by reading its traceback: it exercises `announce.announce_release`, never touches `core/preparse.py` or `tests/test_preparse*.py`). Not in this report's scope; not touched.
- **Status: PASS** (AC-14 and AC-15 both fully green), **with two findings recommended for escalation** — see "Findings requiring escalation" below. Neither finding is a regression against the literal SPEC-v1.5.md §8 AC text; both are risks worth a design decision before the pre-parser's scope grows.

## Reconciliation against the coordinator's stated baseline
Dispatch cited **~1934 passed / 1 failed / 1 skipped**. Measured baseline immediately before adding new tests (full suite, environment healthy, before `test_preparse_gaps.py` existed): **1978 passed / 1 failed / 1 skipped** — the gap is explained exactly as Luna's own `IMPL-v1.5-preparse.md` "Note on the baseline count" describes: `test_checkins.py`, `test_dnd.py`, `test_announce.py`, `test_announce_gaps.py` (the two parallel `checkins`/`announce` tracks) landed concurrently with `preparse`'s own work and are counted in this later snapshot. After adding this file's 591 tests: **2569 passed** = 1978 + 591 exactly. Zero regressions anywhere in the suite.

## Test files
| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_preparse.py` (Luna's, pre-existing, re-verified) | 166 (unchanged) | AC-14, AC-15 |
| `tests/test_preparse_gaps.py` (new, this report) | 591 | AC-14, AC-15 (+ structural support for AC-16, not owned here) |

### What `test_preparse_gaps.py` adds, by section
| Section | Tests | Purpose |
|---|---|---|
| 1. Corpus expansion | 5 novel true-positives + 16 novel adversarial = 21 | Thai numerals, full-width digits, trailing period, padded whitespace, embedded newline (all TRUE positives); exclaim, tilde, range, doubled log, glued negative, unregistered "l"/"cc"/"oz"/"litre", scientific notation, comma, zero-width space (leading + embedded), RTL marks, leading emoji, full-width unit letters (all correctly None) |
| 2. Cross-kind safety | 3 | Ambiguous shared-unit-token registries — see Findings below |
| 3. Byte-identical proof audit | 2 | Real streak-milestone crossing + undo button + DB target-override, captured (not silently dropped) and compared LLM-path vs preparse-path |
| 4. Confidence audit | 2 | Structural (AST/source) + behavioral proof nothing downstream branches on `ExtractionResult.confidence` |
| 5. Ollama-down structural proof | 3 | AST-level: no `async def`, no `await`, only `ExtractionResult` imported from `ollama_client` |
| 6. Fuzz | 480 mutations + 80 true-positive grid = 560 | Generated near-miss corpus (6 mutation strategies × 5 numbers × 8 registered units × 2 separators), each analytically guaranteed to break the parser, empirically verified 0/480 false positives before being committed |

## AC coverage
| AC | Test(s) | Status |
|---|---|---|
| AC-14 (pre-parser skips LLM; byte-identical confirmation) | `test_preparse.py::test_ac14_deterministic_parse_produces_the_expected_result` (8 shapes) · `test_ac14_confirmation_is_byte_identical_to_the_llm_path` (8 shapes, text-only) · `test_ac14_every_registered_unit_and_alias_resolves_via_deterministic_parse` · `test_ac14_deterministic_parse_never_touches_an_llm_even_with_a_raising_double_patched_in` · `test_ac14_deterministic_parse_signature_has_no_llm_channel_or_db_parameter` · **new:** `test_preparse_gaps.py::test_ac14_novel_true_positive_shapes_resolve_correctly` (5) · `test_ac14_streak_milestone_and_undo_button_are_byte_identical_between_paths` · `test_ac14_target_override_goal_rendering_and_undo_button_are_byte_identical_between_paths` · `test_ac14_handle_inbound_message_confirmation_code_never_reads_result_confidence` · `test_ac14_confirmation_is_identical_across_the_full_confidence_range` · `test_ac14_fuzz_harness_true_positive_grid_still_resolves` (80) | **PASS** |
| AC-15 (zero false positive / adversarial) | `test_preparse.py::test_ac15_adversarial_corpus_never_produces_a_false_positive` (72×2 registries) · `test_ac15_boundary_huge_and_decimal_values_are_supported_not_false_positives` · **new:** `test_preparse_gaps.py::test_ac15_novel_adversarial_messages_never_produce_a_false_positive` (16) · `test_ac15_fuzz_generated_near_miss_never_produces_a_false_positive` (480) · `test_ac15_finding_*` (3, documents a discovered risk — see Findings) | **PASS** (see Findings for a risk not covered by the literal AC-15 corpus) |
| AC-16 (works Ollama-down) | Not owned by `preparse` per SPEC-v1.5.md §11 (integration/`main.py` wiring). Structural support only: `test_preparse_gaps.py::test_ac16_preparse_module_defines_no_async_function_and_contains_no_await` · `test_ac16_preparse_module_only_imports_extractionresult_from_ollama_client` · `test_ac16_deterministic_parse_signature_and_module_take_no_db_or_channel` | Out of scope — **structurally supported, not independently verified end-to-end** (see "Integration status" below) |

## Findings requiring escalation

### Finding 1 — shared unit tokens across two habits silently misattribute, they do not fall through to `None`
- **What was tested:** `test_ac15_finding_shared_unit_token_across_two_habits_resolves_first_match_not_none`, `test_ac15_finding_water_alias_collision_with_stretch_unit_misattributes_the_log`
- **Root cause:** `core/units.py:build_unit_lookup` (shared surface, extracted verbatim from `commands.py` per R-L5) builds its unit→habit map with `dict.setdefault` — the first-registered habit in `[[habits]]` order silently claims a unit token; a second habit configuring the identical token is invisible to the lookup. `deterministic_parse` inherits this unchanged by design (R-L5 explicitly forbids duplicating unit logic).
- **Concrete reproduction:** A registry with `water` (unit `ml`, alias `min` → 1.0) registered before `stretch` (unit `min`). `deterministic_parse("10 min", registry)` returns `ExtractionResult("water", 10.0, 1.0)` — a duration log clearly meant for `stretch` is logged as 10ml of water. No exception, no `None`, no fallback to the LLM.
- **Why this matters more than a normal false positive:** SPEC-v1.5.md's own framing ("a wrong value is worse than a missed parse", §9) is about a WRONG NUMBER for the RIGHT habit. This is worse — a wrong HABIT entirely, silently corrupting a different habit's history.
- **Does this violate AC-15 as literally written?** No. SPEC-v1.5.md §8 AC-15's corpus is "bare numbers w/o unit, unknown units, sentences, 'from now on 2.5L a day', questions, commands" — it does not name unit-token collisions across configured habits. The shipped default registry (water/stretch/diary) has no such collision today (verified: `test_ac15_non_colliding_default_registry_is_unaffected_by_this_finding`), so this is **latent, not active**, against production `config.toml`.
- **Recommendation:** Escalate to Archi/Sophia as a design question for `core/units.py` (shared surface, would also touch `commands.py`'s pre-existing edit-value parsing and AC-2's byte-identical guard — cross-cutting, not `preparse`'s call to make unilaterally): should `build_unit_lookup` detect a genuine collision (a token claimed by two DIFFERENT habit ids with amiguous multipliers) and either omit it from the lookup (forcing both `deterministic_parse` and `commands.py`'s edit-value parsing to fall through / prompt) or otherwise flag it at config-load time? Given this app's only current authors of `[[habits]]` are Archi's own workflow (not untrusted user input), a cheaper mitigating alternative is a config-validation check at startup that refuses to boot if two numeric/duration habits share a unit token — worth a decision either way before the registry grows past 3 habits.

### Finding 2 — Thai numerals and full-width digits are silently accepted (undocumented, but correct, capability)
- **What was tested:** `test_ac14_novel_true_positive_shapes_resolve_correctly[๕๐๐ml-water-500.0]`, `[５００ml-water-500.0]`
- **Observation:** Python's `\d` (used unqualified in `core/units.VALUE_RE`, no `re.ASCII` flag) matches any Unicode decimal-digit character, and `float()` normalizes them — so `"๕๐๐ml"` (Thai numeral glyphs) and `"５００ml"` (full-width Arabic digits, common from CJK IME auto-conversion) both correctly resolve to `water/500.0`. This is **not a bug** — the value is numerically correct and the unit resolves normally — but it is **undocumented**: neither SPEC-v1.5.md nor `IMPL-v1.5-preparse.md` mention Thai-numeral-glyph or full-width-digit support, and Luna's own 166-test suite never exercised it.
- **Asymmetry worth noting:** full-width DIGITS are silently normalized, but full-width UNIT LETTERS are not (`"５００ＭＬ"` → `None`, confirmed by `test_ac15_novel_adversarial_messages_never_produce_a_false_positive`) — `resolve_unit`'s `.lower()` does not case-fold full-width Latin letters to their ASCII equivalents. Inconsistent, but safe (under-matches, never over-matches) — no action required, noted for awareness only.
- **Recommendation:** No code change required (this is additional correct coverage, not a defect). Suggest Sophia add one line to SPEC-v1.5.md's R-L1 acknowledging Thai-numeral/full-width-digit inputs are in scope by construction, so a future refactor of `VALUE_RE` (e.g. adding `re.ASCII`) doesn't silently regress this without anyone noticing it was ever supported.

## Integration status (context, not this module's own scope)
Per SPEC-v1.5.md §11 and `IMPL-v1.5-preparse.md`'s own "Wiring instructions for integration" section, `deterministic_parse` is **not yet wired into `main.py`** — confirmed by direct grep (`grep -n "preparse" src/habit_assistant/main.py` → zero matches) at the time of this test pass. This is expected and correctly scoped: AC-16 (works Ollama-down, end-to-end) is explicitly the integration step's own AC to close, not `preparse`'s. This report's structural proofs (section 5 above) establish the module is *ready* for that wiring (no LLM/DB/channel coupling anywhere in the file), but AC-16 itself remains unverified end-to-end until integration lands and should be re-tested then.

## Environment note
The shared dev `.venv` (under the OneDrive-synced project folder) was found in a broken/partially-installed state at the start of this session (multiple packages — `pytest`, `numpy`, `matplotlib`, `httpcore`, `anyio`, `apscheduler`, `pydantic-core`, `colorama` — had empty or incomplete installs, unrelated to `preparse`'s own code). Root cause: `uv`'s default hardlink install strategy is incompatible with OneDrive's cloud-file provider (Windows error 396) and its own transactional uninstall/reinstall races against OneDrive's on-access file scanning. Repaired via `UV_LINK_MODE=copy` plus targeted `uv pip install --reinstall` passes for the affected packages; the live Habit Assistant service (confirmed running throughout, several `python.exe` processes under a different security context) was never touched, restarted, or otherwise disturbed — one persistent lock on `matplotlib`'s bundled `DejaVuSans.ttf` was traced to a genuinely busy handle (`Device or resource busy` on direct delete) consistent with that service holding it open, and was worked around (installing everything else first) rather than forced. Full suite now runs clean at 2569/1/1 as reported above. No production code, `data\habits.db`, or the live service were modified.

## Recommendation
**Ready to ship** for AC-14 and AC-15 as written — no failures, zero regressions, adversarial pressure (16 novel edge cases + 480 fuzzed mutations + 2 purpose-built ambiguous registries) found no false positive against the shipped default registry and no divergence in the byte-identical confirmation path (text, undo button, and milestone/target-override rendering all verified identical, not just text as the original suite's `FakeChannel` gap had let slip through unverified).

Two items for Archi to route, both non-blocking for this release:
1. **Finding 1** (shared-unit-token misattribution) → escalate to Archi/Sophia as a `core/units.py` design question before the habit registry grows past its current 3 disjoint-unit habits.
2. **Finding 2** (Thai-numeral/full-width-digit support) → route to Sophia as a one-line spec documentation addition; no code change needed.
