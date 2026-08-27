# Test Report — SPEC-v1.10.md "Never lose a log", Module M1 (unparsed closure + tap-to-fix clarify)

## Summary
- **Round 2 (final) verdict: PASS.** The one critical finding from round 1 is fixed and independently re-verified adversarially; the render-budget finding is fixed and re-verified.
- M1-relevant subset (`test_clarify.py` + `test_unparsed_closure.py` + `test_v110_m1_gaps.py` + `test_v110_shared_surface.py`): **145 passed, 0 failed** (target was 139/0; +6 fresh round-2 probe tests, all passing)
- Full repo suite (this run, tree includes concurrent M2/M3/integration work): **4845 passed, 0 failed, 1 skipped, 3 xfailed**
- No production code was modified by Vera at any point. `docs/` was not touched.

## Round 1 recap (for context)
Round 1 found one critical issue: `core/clarify.py:handle_clarify_callback` resolved the tapped *habit* against the tapping user's own registry but never checked that the tapped *row* belonged to the tapping user — `storage/db.py:resolve_unparsed`/`mark_unparsed_state` have no `user_id` term in their CAS at all. An adversarial exploit test proved a completely unrelated active user could forge `clarify:<victim's row_id>:water:<value>` and silently reclassify the victim's row, with the confirmation misdirected to the attacker. A secondary finding: `send_closure`/`offer_clarify` quoted the raw message with no render-budget truncation, unlike every sibling module — a near-4096-char raw message could push the closure notification past Telegram's own `sendMessage` limit.

## Round 2 — Luna's fixes, independently re-verified

### Fix 1: row-ownership pre-check (`core/clarify.py:handle_clarify_callback`)
Luna added, mirroring `core/undo_ui.py:handle_undo_callback` line-for-line:
```python
row = db.get_log(row_id)
if row is None or row["category"] != "unparsed" or row["user_id"] != chat_id:
    await channel.send(chat_id, i18n.t("clarify_already_handled", lang))
    return
```
placed immediately after payload/bounds validation, before the habit lookup and the CAS call. `storage/db.py` was **not** touched (the CAS predicates still have no `user_id` term — the fix is entirely in the caller).

**Her argument, assessed explicitly (as Archi asked):** *"`user_id` is immutable on a `logs` row, so a pre-check has no race window the CAS must also cover."* **Confirmed correct**, on two independent grounds:
1. **The field genuinely doesn't change for any row this check can ever apply to.** I grepped every `UPDATE logs SET ...` in `storage/db.py`: exactly one statement ever touches `user_id` — `attribute_legacy_to_owner`'s `UPDATE logs SET user_id = ? WHERE user_id IS NULL`. That method is called only three times, all at process startup in `core/app.py` (never from a request-handling path), and only affects pre-v1.2 legacy rows with a NULL `user_id`. A row can only ever reach `category='unparsed', unparsed_state='awaiting_clarify'` via the v1.10 sweep/live-offer path, both of which write through `insert_log`/`LogEntry`, where `user_id` is required and non-optional (SPEC-v1.2.md R-D1: "never `None` for a new write"). So no row this check gates was ever, or could ever be, in scope for that one mutation. `user_id` is therefore write-once for the entire population this check runs against.
2. **Even setting that aside, there's no interleaving window in this single-threaded asyncio app.** `storage/db.py`'s own docstring: *"Not thread-safe by design — the app is a single asyncio process; all DB calls happen on the event-loop thread."* Between the ownership-check's `db.get_log(row_id)` and the later `db.resolve_unparsed(...)` call inside one `handle_clarify_callback` invocation there is **no `await`** — the habit lookup and type/value checks in between are all synchronous — so no other coroutine can interleave into that gap regardless of what it does.

This correctly leaves `unparsed_state` (genuinely mutable, genuinely racy under concurrent taps/sweeps) as CAS-only, while moving only the ownership dimension (immutable) to a cheap pre-check — the right separation of concerns, and consistent with the one other place in this codebase that already does exactly this (`undo_ui`).

**Independent re-verification (not just re-running her own tests):**
- Re-ran both original round-1 CRITICAL exploit tests unmodified — both now **PASS** (the row stays untouched; the confirmation is refused).
- **Fresh probe — forged tap on a foreign `closed` row:** refused, no write, friendly no-op. PASS.
- **Fresh probe — forged tap on a foreign `awaiting_llm` row** (never even offered — a disjoint state from `awaiting_clarify`, so this was already covered by the CAS alone, but now caught earlier by the ownership check first — defense in depth): refused, no write. PASS.
- **Fresh probe — the row's real owner's own legitimate tap, run immediately after two different strangers' forged taps against the same row both failed:** succeeds normally, full `recovered_water` confirmation + Undo button, row correctly reclassified. Confirms the fix refuses only cross-user forgeries and doesn't collaterally lock out the legitimate owner. PASS.
- **Tightened, not just re-run:** the original `test_CRITICAL_forged_tap_sends_confirmation_to_the_attacker_not_the_owner` had an assertion (`recipients == {MALLORY}`) that would have passed under *either* the buggy or fixed behavior — not a meaningful post-fix regression guard. Strengthened to assert `channel.actionable == []` (never a real, button-carrying confirmation) and the sole message is the friendly `clarify_already_handled` no-op. PASS.
- Converted the one self-obsoleted "attack succeeds" test into a positive guard, `test_multiple_forged_taps_from_different_strangers_are_all_refused_owners_own_tap_still_wins`: two *different* strangers (not just one) both forge-tap the same foreign row, both refused with zero writes, then the real owner's own tap still succeeds. PASS.

### Fix 2: render-budget truncation (`core/clarify.py`, `_QUOTE_MAX_CHARS = 200`)
Luna added `_quote()` (wrapping the shared `core/render_budget.py:truncate`) at both `send_closure` and `offer_clarify`'s message-construction sites — applied **only** to the text embedded in the outbound message, never to the text passed into `tier1_guesses`/`offer_clarify`'s own guess recomputation (confirmed by reading the diff: `tier1_guesses(text, ...)` still receives the untruncated `text`, only `i18n.t(..., text=_quote(text))` is bounded) — so truncation cannot change which guesses are offered, only how much of the raw message is quoted back to the user.

**Independent re-verification:**
- Converted the self-obsoleted overflow-finding test into its positive form: a 4000-char raw message's closure notification now measures **397 chars**, well under `TELEGRAM_MESSAGE_BUDGET = 4096`, and under a tight `< 500` sanity bound (confirms it's genuinely fixed, not just barely squeaking under the limit). PASS.
- **Fresh probe — exact boundary, 200 chars:** `render_budget.truncate`'s own contract is `len(text) <= max_chars` → returned as-is. Confirmed a 200-char raw message is quoted byte-for-byte, no ellipsis. PASS.
- **Fresh probe — one past the boundary, 201 chars:** confirmed truncation to exactly `text[:199] + "…"` (199 kept characters, never 200 kept + ellipsis, which would overshoot to 201). PASS.
- **Fresh probe — Thai combining-mark boundary ("does the truncated quote still render sanely mid-character?"):** built a 300-codepoint string of alternating base-consonant + combining-tone-mark pairs (`"ก่" * 150`) straddling the 200-char cut, driven through the real `send_closure`/`i18n.t` path (not `render_budget.truncate` in isolation). No exception; output is well-formed, UTF-8-encodable text; the cut is a plain Python `str` slice, which is always codepoint-safe (can never split a single Unicode codepoint, unlike a raw byte slice) — worst case is the very last kept character loses a trailing tone mark before the ellipsis, a cosmetic non-issue, not a defect. PASS.

## Test files (final)

| Path | Tests | Notes |
|---|---|---|
| `tests/test_clarify.py` (Luna) | 50 | Unchanged since round 1 |
| `tests/test_unparsed_closure.py` (Luna) | 10 | Unchanged since round 1 |
| `tests/test_v110_m1_gaps.py` (Vera) | 42 | Round 1: 36. Round 2: +6 fresh probes (foreign-closed-row, foreign-awaiting-llm-row, owner's-own-tap-still-works, exact/off-by-one truncation boundary, Thai combining-mark truncation); 2 tests converted from bug-documenting to positive regression-guard form (renamed, not deleted); 1 test's assertion tightened (same name) since it had gone vacuous post-fix |

## AC coverage (final)

| AC | Result |
|---|---|
| AC5 (zombie loop killed) | **PASS** (deferred integration-level re-proof still applies, unchanged from round 1 — see below) |
| AC6 (closure once, no-guess) | **PASS** (same deferred-slice caveat) |
| AC7 (tier-1 guesses deterministic) | **PASS** — unaffected by either fix; re-confirmed clean in this run |
| AC8 (guess offer + state) | **PASS** (same deferred-slice caveat) |
| AC9 (live LLM-unknown) | **DEFERRED to integration**, unchanged — still genuinely unimplemented outside `offer_clarify` itself (see below) |
| AC10 (clarify tap = ordinary log) | **PASS** — the row-ownership gap that failed this in round 1 is fixed and adversarially re-verified (original exploit + 3 fresh probes + 2 tightened/converted tests, all green) |
| AC11 (sweep-vs-tap race guard) | **PASS** — unaffected by either fix (the CAS mechanics were never the problem); re-confirmed clean |

## Failures
None.

## Regressions detected
None. Full repo suite: 4845 passed / 0 failed / 1 skipped / 3 xfailed. Every file in the M1-relevant subset is green; no other test anywhere in the tree failed.

## Deferred AC slices (unchanged from round 1 — need the wired `core/routing.py` integration seam)
Per SPEC-v1.10.md §11, M1 (including this round's fix) never touches `routing.py`. These are proven at the strongest level available pre-integration, not through the real wired sweep/callback dispatch yet:
- **AC5/AC8 slice:** "no LLM call on a subsequent sweep" is proven via `db.pending_unparsed()`'s own query exclusion, not via an assertion that the *wired* `reparse_pending_unparsed` actually branches on `clarify.tier1_guesses` instead of always calling `parse_message`.
- **AC6 slice:** "closure sent exactly once, ever" is proven via a hand-driven sweep-simulation helper, not the real loop's own CAS-then-send ordering.
- **AC9 (full AC):** the "no guesses → generic clarifying_question + `/log` keyboard" branch has no M1 function to call (by spec design) and does not exist anywhere in `handle_inbound_message` yet.
- **`on_callback` routing:** no `clarify:` prefix branch exists in `core/routing.py:on_callback` yet — confirmed still true in this round's re-read. **This is now unblocked for wiring** — the row-ownership fix must be (and is) in place before this goes live, since wiring the callback without it would have shipped the vulnerability to production.

## Recommendation
**Ready to ship — M1 track closed.** Both round-1 findings are fixed, confirmed by reading the actual diff (not just re-running the original tests), and independently re-verified with fresh adversarial probes beyond the original exploit (foreign-row states other than the one originally tested, the happy path, and the exact truncation boundary including a Thai-script edge case). The immutability argument underlying fix 1 was checked against the actual codebase (one mutation site, startup-only, out of scope for any row this check applies to) rather than taken on faith. M1-relevant subset: 145/145. Full suite: 4845/0 failed. Integration may proceed to wire `core/routing.py`'s `reparse_pending_unparsed`/`handle_inbound_message`/`on_callback` against `core/clarify.py` — the deferred slices above are integration's own remaining work, not a reason to hold this track.
