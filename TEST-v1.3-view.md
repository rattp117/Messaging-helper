# Test Report — v1.3.0 Audit log, module `audit-view`

## Summary
- Scope: AC-V1, AC-V2 (module-owned per SPEC-v1.3.md §11). AC-V3 (owner-gating/menu-hiding) is
  integration-owned — verified here only at the composition level, not failed for `main.py` being unwired.
- Total in `tests/test_audit_view.py`: **82 tests** (58 Luna round 1 + 16 Vera round 1 + 5 Luna round 2 fix
  verification + 3 Vera round 2 re-verification probes)
- Passed: 82 / Failed: 0
- Full suite (`pytest -q`, whole repo): **1489 passed, 0 failed, 1 skipped** (baseline before this feature was
  1440 passed / 1 skipped — **zero regressions** across two iteration rounds)
- Status: **PASS**

## Iteration history

**Round 1:** Vera wrote 16 adversarial tests on top of Luna's 58. One failed:
`test_render_recent_50_rows_of_realistic_remind_edits_exceeds_telegram_limit` — a realistic (not
pathological) 50-row `/audit` with ordinary 4-time `remind_set` edits produced a 5,828-char reply, 42% over
Telegram's 4,096-char `sendMessage` cap, with no truncation or chunking anywhere in the pipeline. Verdict was
FAIL, handed back to Luna.

**Round 2 (this pass):** Luna fixed it entirely inside `core/audit_view.py` — no production changes outside
that one file, no changes to `main.py`, `commands.py`, or the shared/capture modules:
1. `_truncate`/`_MAX_VALUE_CHARS = 60` inside `_humanize_stored_value` — caps each rendered old/new value.
2. `_fit_within_budget`/`_TELEGRAM_MESSAGE_BUDGET = 4096` — `render_recent` always checks the fully-joined
   message against the real Telegram limit and, on overflow, drops the **oldest shown rows** (the tail of the
   newest-first list) one at a time until it fits, appending a bilingual `audit_more_rows` footer
   ("… {count} more" / "… อีก {count} รายการ", i18n key count now 18).

Luna added 5 tests; I re-read the fix line-by-line, re-ran her worst-case reproduction (matched: 4,034 chars
vs. the previous 5,828/17,400), and added 3 of my own probes targeting the specific mechanisms below. All 82
tests in the file pass; full suite is clean at 1489/0/1.

## Re-verification of the two mechanisms (this pass)

| Question | Finding |
|---|---|
| **Does the budget check account for the footer's own length?** | Yes. `_fit_within_budget` (audit_view.py:194-221) appends the footer to `parts` **before** computing `len(candidate)` on every iteration of the drop loop — the footer is never added "for free" after the length check. Confirmed by code read and by `test_fit_within_budget_footer_reports_the_correct_dropped_count` (Luna) and my own `test_render_recent_all_rows_dropped_footer_count_matches_total` passing. |
| **Does truncation apply to both old and new?** | Yes. `_detail` (line 188) calls `_humanize_stored_value` independently on `row['old_value']` and `row['new_value']` — both go through `_truncate`, not just one side. Confirmed by `test_humanize_stored_value_truncates_a_long_scalar_string`/`..._json_list` (Luna) and by direct code read. |
| **Mid-word/mid-UTF-8 truncation safe for Thai — no broken surrogates/mojibake?** | Confirmed safe. `_truncate` slices a Python `str` by **codepoint index** (`text[:max_chars-1]`), never by UTF-8 byte offset — this structurally cannot produce an invalid UTF-8 sequence or a lone surrogate, since Python string slicing only ever yields valid codepoint sequences. I constructed the worst realistic case — a cut landing exactly between a Thai base consonant (kept) and its combining tone mark (dropped), e.g. truncating `"ก"*58 + "น้ำ"` at 60 chars lands right after `'น'` (base) and before `'้'` (Mai Tho, combining class 107) — and verified: no `U+FFFD` replacement character anywhere in the result, and the result round-trips cleanly through `.encode("utf-8").decode("utf-8")`. The only artifact at a worst-case boundary is a **dropped combining mark** (a bare consonant instead of a fully-composed grapheme) — cosmetic, not corruption, and moot in practice since `old_value`/`new_value` are always numbers/timestamps/status words/language codes in this codebase, never Thai prose. New test: `test_truncate_thai_combining_mark_at_boundary_produces_valid_unicode`. |

## Additional probes (this pass, beyond Luna's own fix-verification tests)

- **A single pathological row whose one value alone exceeds the budget even truncated:** `_humanize_stored_
  value`'s 60-char cap only bounds `old_value`/`new_value` — the **actor field** (`_actor_display`, returning
  a stored `display_name` verbatim) is never truncated, and nothing in `db.upsert_user`/this module enforces a
  length cap on `display_name` at the DB layer (unreachable via real Telegram data, since Telegram itself caps
  first/last name at 64 chars each — but nothing stops a hand-inserted or future-caller value from being much
  larger). I inserted a synthetic 6,000-char `display_name` and recorded one row against it: `render_recent`
  correctly drops that single row entirely (the drop loop's "pop until it fits" logic works even when there is
  only one row to drop) and returns just `header + "… 1 more"` — 37 chars, nowhere near the 4096 budget. No
  crash, no truncated-mid-row leakage of the 6,000-char string into the output. New test:
  `test_render_recent_single_row_with_pathological_actor_name_still_fits_budget`.
- **Footer accuracy when exactly 0 rows fit:** constructed 5 rows, each individually oversized via the same
  huge-`display_name` technique, forcing the drop loop to empty `kept` all the way to zero. Result: exactly 2
  lines (header + footer), footer reads `"… 5 more"` — the count exactly matches the total rows fetched, not
  an off-by-one from a partial drop, and the loop terminates (no hang) via the `not kept` defensive floor.
  New test: `test_render_recent_all_rows_dropped_footer_count_matches_total`.
- Re-ran Luna's own worst-case reproduction independently (50 rows × full `MAX_REMINDER_TIMES=24` schedules in
  both old and new, long actor chat id, Thai localization) and got the same result she reported: **4,034
  chars**, under budget, footer present (`"…"` in the last line) — confirms her fix is a genuine structural
  bound, not tuned narrowly to my original reproduction case.

## AC coverage

| AC | Result |
|---|---|
| **AC-V1** (recent 20, newest-first, bilingual, `ts·actor·action·entity·old→new·source`, owner's own rows render "you", works with Ollama down) | **PASS** — 20+ tests across rendering, localization, actor fallback, vocabulary-drift robustness (unknown action/source), null-column handling, LLM-free proof, Thai timestamp correctness. |
| **AC-V2** (`/audit 5` → ≤5 rows; above-cap → 50; non-numeric N → default 20) | **PASS** — parsing/capping contract fully covered (11+ dispatch-shape cases, extended Thai-alias false-positive corpus, English slash-form boundary shapes) **and** the message-length gap found in round 1 is now closed: per-value truncation + a structural total-message budget guarantee every `/audit` reply (any row count, any content, any language) fits Telegram's 4096-char `sendMessage` limit, verified against the original failing case, Luna's independent worst-case, and two further pathological constructions (single oversized row; all-rows-dropped) above. |
| **AC-V3** (owner-only + hidden from menu) — integration-owned | **Composition PASS**, unchanged from round 1 — the exact wiring `IMPL-v1.3-view.md` hands to `main.py` is proven sound (`test_owner_gating_*`). `main.py` itself still has no `command.kind == "audit"` route (confirmed by inspection — expected, integration's own scope per SPEC-v1.3.md §11, not scored against this module). |

## Failures

None.

## Regressions detected

None, across both rounds. Baseline 1440 passed/1 skipped → round 1 added 16 tests (15 pass, 1 fail, 0
regressions) → round 2 (Luna's fix + her 5 tests + my 3 re-verification tests) → **1489 passed, 0 failed, 1
skipped**. Every pre-existing test, including all of round 1's, still passes unmodified.

## Recommendation

**Ready to ship.** Both module-owned acceptance criteria (AC-V1, AC-V2) pass, the message-length finding from
round 1 is closed by a structurally sound fix (per-value truncation + a real total-length budget guarantee,
not a value tuned to one reproduction), the fix is scoped entirely to `core/audit_view.py` with no ripple into
other modules, Thai-text truncation is confirmed safe against Unicode corruption, and the full 1489-test suite
is green with zero regressions. AC-V3's remaining half (confirming `/audit` is actually absent from the real
`command_menu` and that `main.py` routes `command.kind == "audit"` correctly) is out of this module's scope
and awaits the integration step, as already flagged in `IMPL-v1.3-view.md`.
