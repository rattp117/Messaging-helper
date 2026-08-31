# Test Report — LINE v1.1.0 Readable Approval Flow (Adversarial Access-Control Probe)

Scope: `IMPL-LINE-1.1.0.md` against its own "Maps to acceptance criteria" list, plus adversarial probing of `core/access.py:_resolve_admin_target_chat` (Round 1) and re-verification of Luna's hardening fix against Archi's ruling (Round 2, this update). No production code was modified by Vera in either round.

## FINAL VERDICT: PASS — ready to release `line/v1.1.0`

- All 6 CRITICAL/HIGH/MEDIUM/LOW findings from Round 1 (F1–F6) are **confirmed fixed** by fresh re-runs against the rewritten `_resolve_admin_target_chat` (merged-candidate-pool design), not just re-reading the diff.
- 4 NEW adversarial probes the pool design itself invites (candidate collision — same row via 3 rules, different rows via 3 rules; the 33-vs-32-char creation-eligibility boundary) all resolve **correctly**.
- 2 sanctioned mechanical test repairs applied (rich-menu README parsing broke on an unrelated, concurrent Iris commit; a semantically-stale placeholder-guard test rewritten to the current contract). Both green.
- Full LINE gate (`pytest -m "not telegram_only and not llm_only" -n auto -q`): **5104 passed, 4 skipped, 1 xfailed, 1 failed** — the 1 failure is the pre-existing Monday date-drift flake (`test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge`), confirmed unrelated (grace/habits module, today 2026-08-31 is a Monday, matches its own documented trigger). **No other failures.**
- Telegram-mode gate (`pytest -m telegram_only -q`): **30 passed**, unaffected.
- One INFO-level, non-blocking cosmetic finding (a comment arithmetic slip in `access.py`, does not affect behavior) — noted below, does not change the verdict.

---

## Round 2 — Re-verification of the hardening fix (this update)

### What changed (verified by reading the diffs directly, not by trusting IMPL's summary)

- **`core/access.py:_resolve_admin_target_chat`** rewritten from "first matching rule wins" to a **merged-candidate-pool** design:
  1. Step 1 is now a real `db.get_user(token)` existence check (any status) — no longer a shape guess.
  2. Steps 2/3 (pending exact-name match, pending ≥6-char prefix match) are collected into the **same pool** as step 1, deduped by `chat_id`.
  3. 2+ distinct rows in the pool → ambiguous (lists all candidates + status). Exactly 1 → resolves. 0 → falls to step 4.
  4. Step 4 (legacy "pre-approve a chat id that's never contacted the bot" creation) now requires `_is_full_id_eligible_for_creation`: a `"U"`-shaped token needs `len(token) >= _MIN_FULL_LINE_ID_CHARS` (**33** — a real LINE userId's actual length, `"U"` + 32 hex chars) or it gets the new `admin_no_match` reply instead of silent phantom-row creation.
  5. F5: for `/block` only, an active-user name match is folded in as a second candidate when it would otherwise silently pick a lone pending row — forces ambiguity instead of a silent wrong-target block.
  6. F6: the active-only-match reply (`admin_block_name_is_active`) now fires for both `/approve` and `/block`.
- **`core/commands.py:_match_access`**: `/approve`/`/block` now capture the FULL, outer-trimmed tail (`_full_tail`) instead of the first token (`_first_token`, F4 fix). `/invite` is deliberately unchanged (still first-token-only — no pending row exists for it to name-match against by definition).
- **`core/i18n.py`**: new `admin_no_match` key; `admin_block_name_is_active` copy generalized for both commands; `admin_ambiguous_line` gained a `{status}` suffix (a non-pending row can now appear in an ambiguous set, per F2/F5).

### F1–F6 re-verified fresh against the new code (not just re-reading the diff)

| Finding | Round 1 verdict | Round 2 re-verification | Result |
|---|---|---|---|
| F1 — 17+ char prefix guess silently bypassed resolution, phantom row created (`/approve`) / real target silently untouched (`/block`) | CRITICAL / UNSAFE | Re-ran with a fresh 20-char prefix of a 34-char pending/active id. `/approve`: now resolves via prefix match, real user approved, no phantom row. `/block`: no prefix match against an active user (unaffected — that safety rule is intentional), now gets an honest `admin_no_match` reply instead of a false "blocked" success, no phantom row created | **FIXED** |
| F2 — a token that's simultaneously a real id AND a prefix of a different real id resolved to the wrong real person | CRITICAL / UNSAFE | Re-ran both the pending-vs-pending and active-vs-pending constructions. Both now report **ambiguous**, listing both real candidates by id; neither is touched | **FIXED** |
| F3 — a pending display name that itself looks id-shaped was permanently unreachable by name | CRITICAL / UNSAFE | Re-ran: step 1's real existence check correctly misses the literal name string (no row has that as its actual `chat_id`), step 2 (name match) correctly hits the real pending user | **FIXED** |
| F4 — `/approve`/`/block <full two-word name>` silently mistargeted a different pending user sharing the first word ("Som" vs "Som Chai") | HIGH (routed to Archi — touches `_first_token`'s pinned contract) | Re-ran via `commands.dispatch` (not just `execute_admin` directly) to confirm the full pipeline: `target_chat` is now the full "Som Chai", exact-matches Y only, X is never touched | **FIXED** |
| F5 — `/block <name>` matching both a pending stranger and an active namesake silently blocked the stranger, active person untouched | MEDIUM | Re-ran: now reports ambiguous (both candidates named), neither row touched | **FIXED** |
| F6 — `/approve` fell through to generic usage for an active-only name match, `/block` had a specific reply | LOW | Re-ran: both commands now return the identical `admin_block_name_is_active` reply for the same input shape | **FIXED** |
| F7 — `/users` has no structural total-length cap (only per-row truncation) | INFO, not a blocker | Not in Archi's ruling for this pass; left as-is (confirmed unchanged, still not a blocker at realistic user counts) | **UNCHANGED (accepted)** |

### NEW probes the merged-candidate-pool design itself invites

The pool design's whole point is "collect every rule's hit, then judge" — which raises new questions a first-wins design never had to answer: what happens when ONE token satisfies multiple rules at once, for the same row vs. for different rows? And exactly where is the 33-char creation-eligibility line?

| # | Probe | Expected (per the design) | Observed | Verdict |
|---|---|---|---|---|
| 1 | A token that is simultaneously its own exact id, its own pending display name, AND its own id-prefix (all 3 rules hit the SAME row) | Dedupe to 1 candidate, resolve cleanly — not a false "ambiguous" | Resolved directly, `admin_approved_ack` for that row, no ambiguous reply | **PASS** |
| 2 | A token that is simultaneously P's exact id (step 1), Q's exact display name (step 2), and a prefix of R's id (step 3) — 3 DIFFERENT real rows | Ambiguous, all 3 named, none touched | Ambiguous reply lists P, Q, and R's ids; all three rows' status unchanged | **PASS** |
| 3 | A 33-char `"U"`-shaped token matching nothing else at all | Legacy creation fires (intended — mirrors `/invite`'s existing use case for a real id that's never contacted the bot) | Row created and activated, normal `admin_approved_ack` | **PASS (correct per ruling)** |
| 4 | A 32-char `"U"`-shaped token matching nothing else at all (one char short of the floor) | `admin_no_match`, no row created | Confirmed: no row created, `admin_no_match` reply | **PASS** |

Repro tests (all in `tests/test_line_v110_gaps.py`): `test_token_that_is_exact_id_and_own_name_and_own_prefix_all_at_once_resolves_cleanly`, `test_token_that_is_exact_id_of_p_name_of_q_and_prefix_of_r_is_ambiguous_across_all_three`, `test_33_char_u_token_matching_nothing_else_still_fires_legacy_creation`, `test_32_char_u_token_matching_nothing_else_gets_honest_no_match_creates_no_row`.

### Minor cosmetic finding (informational only, does not affect the verdict)

`core/access.py`'s own comment above `_MIN_FULL_LINE_ID_CHARS = 33` says *"a real LINE userId is always 'U' + 32 hex chars = 34 total"* — this is an arithmetic slip (1 + 32 = 33, not 34). The **code** is correct (`_MIN_FULL_LINE_ID_CHARS = 33` matches a real LINE userId's actual 33-character length, confirmed by the 33-vs-32 boundary probes above); only the prose miscounts. Worth a one-line comment fix next time this function is touched — not urgent, not behavior-affecting, not blocking this release.

### Sanctioned test repairs (mechanical, both green)

1. **`tests/test_line_d_gaps.py::test_richmenu_button_commands_are_real_dispatchable_commands`** — broke because Iris's rich-menu artwork commit (`0f4e310`) rewrote the README's "six cells" table (added a "Tap area" column, dropped the backtick-wrapped command shape the old regex depended on), independently confirmed via `git show --stat 0f4e310` to touch only `assets/richmenu/README.md`, `generate_richmenu.py`, and `richmenu.png` — disjoint from every file this dispatch modifies. Repaired to assert against `channels/line.py:_default_rich_menu_payload()` directly (the actual runtime source LINE calls, stronger and format-proof, per Archi's suggestion) plus a format-tolerant substring check that the README still documents each command. **13/13 passing** in that file.
2. **`tests/test_deploy_line.py::test_richmenu_readme_exists_and_flags_the_placeholder_status`** — semantically stale: it asserted the literal strings `"placeholder"`/`"OQ3"`, which stayed true only because Iris's README deliberately preserved a historical reference (with its own "Editing note" flagging this as unfinished business) to keep the test green rather than because the rich menu is still a placeholder. Renamed to `test_richmenu_readme_documents_the_current_design_tokens_cells_and_regeneration` and rewritten to assert the CURRENT contract: the README documents its design-token system, the six-cell command table (cross-checked against the real payload, not hardcoded twice), and the regeneration instructions. **Passing.**

### Regression after the repairs

| Command | Result |
|---|---|
| `pytest tests/test_line_readable_approval.py tests/test_line_v110_gaps.py tests/test_access.py tests/test_v12_access_gaps.py tests/test_commands.py -q` | **276 passed** (Archi's 272-test exit bar + my 4 new pool-design probes) |
| `pytest tests/test_deploy_line.py tests/test_line_d_gaps.py -q` | **43 passed, 3 skipped** |
| `pytest -m telegram_only -q` | **30 passed, 5233 deselected** |
| `pytest -m "not telegram_only and not llm_only" -n auto -q` (full LINE gate) | **5104 passed, 4 skipped, 1 xfailed, 1 failed** (Monday flake only) |

---

## Round 1 — original adversarial findings (preserved for audit trail; all resolved above)

### Summary
- 22 new tests in `tests/test_line_v110_gaps.py`, all passing at the pytest level, but 6 of them (9 individual test functions) were written to **pin and demonstrate** unsafe resolution outcomes — a green result meant "the bug reproduces as predicted," not "the system defended itself." See the table above for how each was fixed and re-verified.

### AC coverage (IMPL-LINE-1.1.0.md, both rounds)

| # | AC | Round 1 | Round 2 |
|---|---|---|---|
| 1 | `get_profile` fail-open | PASS | Unaffected by hardening — still PASS |
| 2 | Fetch-once-per-user wiring | PASS | Unaffected — still PASS |
| 3 | Owner notification leads with name | PASS | Unaffected — still PASS |
| 4 | Readable `/approve`/`/block` resolution | **FAIL on safety** (F1–F6) | **PASS** — all 6 findings fixed and re-verified fresh, plus 4 new pool-design probes pass |
| 5 | `/users` truncated names | PASS (F7 informational, unchanged) | PASS |
| 6 | Version bump | PASS | PASS |

### Original Resolution-Rule Safety Table and Findings F1–F7

Full detail (mechanism, location, repro tests, severity reasoning) is preserved in this repo's session history / IMPL-LINE-1.1.0.md's own iteration log ("Round 1: adversarial findings" table, Archi ruling section) — not reproduced verbatim here a second time to keep this report from duplicating IMPL's own audit trail. The short form: 3 CRITICAL (F1, F2, F3), 1 HIGH (F4), 1 MEDIUM (F5), 1 LOW (F6), 1 INFO (F7, unchanged/accepted). All CRITICAL/HIGH/MEDIUM/LOW findings are confirmed FIXED in Round 2 above, each with a fresh, independent re-run (not a re-read of the diff) plus new adversarial pressure the fix's own design invited.

## Confirmed-safe properties (Round 1, still holding after the rewrite)

- Exact-name ambiguity (2+ pending sharing a name) — ASCII, Thai, emoji all verified, still passing unchanged.
- Prefix ambiguity within the reachable window — still passing unchanged.
- Short-prefix rejection floor (5 rejected, 6 accepted) — still passing unchanged.
- `/block` can never resolve to an ACTIVE user via name or id-prefix (only the exact full-id path reaches one) — still passing unchanged.
- `get_profile` fail-open on 404, network error, blank name, garbage JSON, timeout — never raises, unaffected by the hardening pass.
- Fetch-once-per-user cap correctly scoped per user — unaffected.
- Telegram-mode `display_name` flow byte-unchanged — 30/30 `telegram_only` green.

## Regressions detected

None, in either round.

## Recommendation

**PASS. Ready to release `line/v1.1.0` to the live server.**

All 6 findings from the adversarial probe are fixed and independently re-verified against fresh test runs (not a re-read of Luna's diff or IMPL's summary) — including new adversarial pressure specifically aimed at the new merged-candidate-pool design's own edge cases (triple-rule collision on one row, triple-rule collision across three distinct rows, the exact 33-vs-32-char creation-eligibility boundary), all of which resolve correctly. The two sanctioned test repairs (rich-menu README parsing, stale placeholder guard) are mechanical, scoped exactly to what Archi authorized, and green. The full LINE gate shows no failures beyond the pre-existing, independently-confirmed-unrelated Monday date-drift flake. The Telegram-mode gate is fully green and untouched.
