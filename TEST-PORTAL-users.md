# Test Report — Admin Web Portal, module USERS (adversarial pass)

> Verifies `IMPL-PORTAL-users.md` (`src/habit_assistant/core/portal/users.py`)
> against `SPEC-LINE-PORTAL.md` AC15-AC21 and UX.md §8 Q6/Q7 (both adopted).
> Scope: **USERS only** — the portal's mutation surface (approve/block/invite).
> This pass is adversarial: it assumes Luna's own 27 tests
> (`tests/test_portal_users.py`) are correct for the happy paths and spends
> its budget on identity-gate×mutation composition, XSS at every render
> site a display name reaches, state-machine coherence, and the UX.md-
> mandated honesty of the approve flash.

## Summary

- Total (USERS-owned): 27 (Luna's, unchanged) + 23 (this pass, new) = **50 tests**
- Passed: 50
- Failed: 0
- **Status: PASS** (all 7 owned ACs, AC15-AC21) — **with 1 escalated finding** that is not an AC15-21 failure but a confirmed violation of an explicit UX.md "must" requirement (see Findings §1).

Full USERS-relevant regression (`test_portal_users.py` + `test_portal_users_gaps.py` + `test_portal_security.py` + `test_portal_server.py` + `test_portal_layout.py` + `test_portal_stats.py` + `test_portal_db.py` + `test_portal_integration.py` + `test_audit.py` + `test_audit_capture_gaps.py` + `test_access.py` + `test_i18n.py` + `test_i18n_literals.py`): **260 passed, 0 failed.**

Full LINE-edition gate (`pytest -m "not telegram_only and not llm_only" -n auto`): **5432 passed, 1 failed, 4 skipped, 1 xfailed.** The 1 failure is `tests/test_portal_audit_gaps.py::test_audit_detail_cell_leaks_diary_text_via_undo_old_value_MAJOR_FINDING` — an **AUDIT-track** finding (another module's own adversarial test file, already present in the tree), **not USERS, not mine**. Per the dispatch note's own exit bar, noting it and moving on. The two `disable_notification` sweep failures Luna flagged in `IMPL-PORTAL-users.md` (attributed to the QUOTA track) are **no longer failing** — resolved elsewhere before this pass ran.

## Test files

| Path | Tests | Covers |
|---|---|---|
| `tests/test_portal_users.py` (Luna's, unchanged) | 27 | AC15-AC21, Q6, Q7, double-submit-approve |
| `tests/test_portal_users_gaps.py` (this pass, new) | 23 | Identity-gate×mutation composition, XSS (6 sites), state-machine coherence (blocked↔active, chat_id targeting), CSRF posture, invite creation-floor boundary, approve-flash honesty (2 tests, both document a real gap) |

## AC coverage

| AC | Requirement | Test(s) | Result |
|---|---|---|---|
| AC15 [M] | Pending users listed with name/chat_id + Approve/Block controls | `test_pending_user_with_name_lists_name_and_id_and_controls`, `test_pending_user_with_no_name_shows_id_as_headline`, `test_pending_card_and_confirm_bodies_escape_script_tag_display_name` | **PASS** |
| AC16 [M] | `POST /users/approve`: pending→active, audit `user_approve`/`source=portal`, `access_granted` push, page reflects it | `test_approve_activates_user_writes_audit_and_sends_push`, `test_approve_push_fires_exactly_once_per_approve`, `test_approve_flash_escapes_script_tag_display_name` | **PASS** (see Finding §1 for a non-AC-gating gap in this same code path) |
| AC17 [M] | `POST /users/block`: →blocked, audit `user_block`/`source=portal` | `test_block_active_user_writes_audit_row`, `test_block_pending_user_writes_audit_row`, `test_block_of_an_already_blocked_user_is_idempotent_friendly`, `test_double_submit_block_is_idempotent_friendly` | **PASS** |
| AC18 [S] | `POST /users/invite`: never-seen valid-shape chat_id → active, `source=portal` | `test_invite_confirmed_creates_active_user_with_portal_source`, `test_double_confirm_invite_is_idempotent_friendly`, `test_get_request_to_invite_path_is_not_allowed`, `test_invite_accepts_shape_valid_id_shorter_than_chat_commands_creation_floor` | **PASS** |
| AC19 [M] | Active rows show last-log, streak, digest opt-out, language pref | `test_active_row_shows_last_log_streak_digest_and_language`, `test_active_row_with_no_logs_shows_never_logged`, `test_active_row_digest_opt_out_renders_off`, `test_active_row_escapes_script_tag_display_name`, `test_display_name_4000_chars_renders_without_crashing`, `test_display_name_with_rtl_override_and_zero_width_chars_renders_escaped` | **PASS** |
| AC20 [M] | No/invalid identity header on `POST /users/*` → 403, no write | `test_post_without_identity_header_is_refused_with_no_write` (×3 routes), `test_get_users_without_identity_header_is_refused`, `test_wrong_owner_login_pinned_refuses_post_with_no_write` (×3 routes, real server), `test_header_less_get_users_refused_before_any_pending_count_read`, `test_no_set_cookie_header_on_any_users_response` | **PASS** |
| AC21 [M] | Missing/unresolvable chat_id → localized inline error, no write, no audit | `test_approve_missing_chat_id_is_rejected_with_no_write`, `test_approve_nonexistent_chat_id_is_rejected_with_no_write`, `test_block_nonexistent_chat_id_is_rejected_with_no_write`, `test_approve_explicit_empty_string_chat_id_is_rejected_with_no_write`, `test_block_explicit_empty_string_chat_id_is_rejected_with_no_write`, `test_invite_invalid_shape_redirects_with_error_and_echoes_typed_value`, `test_invite_invalid_shape_truncates_echoed_value_to_64_chars`, `test_invite_empty_chat_id_is_rejected_with_no_write` | **PASS** |
| Q6 (adopted) | Active (non-owner) rows carry a Block confirm | `test_non_owner_active_row_has_block_control` | **PASS** |
| Q7 (adopted) | Owner row unblockable (UI omission + server-side refusal) | `test_owner_row_renders_as_you_owner_with_no_block_control`, `test_forged_post_block_on_owner_row_is_refused_no_write_no_audit` | **PASS** |
| — | `audit.Source`/`SOURCES` includes `"portal"` (shared-surface gap Luna closed) | `test_portal_is_a_recognized_audit_source` | **PASS** — verified correct; see Ruling §2 below |

## Rulings on Luna's flagged items

### #2 — "Current streak" = MAX across the user's own habit registry

**Ruling: PASS-with-note.** SPEC-LINE-PORTAL.md and UX.md §7 ("Streak {streak}") are both silent on which habit's streak to show in a multi-habit app. I searched both documents for any contradicting language and found none. "Best ongoing streak, fail-open per-user" is a reasonable, defensible interpretation and doesn't contradict anything specced. No test change needed; flagging (as Luna already did) is sufficient — if the user has a different preference (e.g. a specific habit, or sum), that's a product decision for a future spec revision, not a defect today.

### #3 — Approve success flash always claims "messaged," even when the push failed

**Ruling: FAIL against UX.md §3 Flow B — this is a truthfulness fail, not a follow-up.** I disagree with the softer "note as follow-up" lean and back that with the spec text and two adversarial tests.

UX.md §3 Flow B, "Error branches" (line 116), states as an explicit **MUST**, not a preference:

> "If the `access_granted` push fails (LINE API down, quota stopped) → the approve **still succeeded** ... **The flash must say so honestly rather than claiming a message was delivered.** See §7 `portal_flash_approve_nopush`."

UX.md §7 (line 817) gives the exact honest copy Luna was supposed to add:

> **Flash — approved, push failed:** "✅ Approved {name}, but the notification didn't send. They have access but don't know it yet."

I grepped `core/i18n.py`'s `CATALOG` — `portal_flash_approve_nopush` **does not exist**. The only flash key wired to `ok=approve` is `portal_users_flash_approved`, unconditionally: **"✅ Approved {name}. They've been messaged."** (TH: "...และส่งข้อความแจ้งเรียบร้อย" — "...and the notification was sent successfully"). Per the dispatch note's own test ("verify the claim wording isn't actively false, e.g. 'approved and notified' vs 'approved'"): this wording is **not** a hedge like "approved and notified" — it is an unconditional past-tense assertion of a fact that, on a push failure, did not happen. That is an active falsehood, not an optimistic default.

Two new tests prove this concretely, both currently pinning the wrong behavior:
- `test_approve_flash_falsely_claims_messaged_when_push_raises` — a `RaisingChannel` simulates a LINE API outage. `access.approve_user`'s own try/except *does* catch this (its docstring says so explicitly), but swallows it with no signal back to the caller — the flash still says "been messaged."
- `test_approve_flash_falsely_claims_messaged_when_push_is_silently_dropped_by_quota_gate` — **the exact scenario the dispatch note asked about.** I read `channels/line.py:LineChannel._push` (realtime mode, `total >= cap` branch, ~line 428): it does **not** raise — it logs at INFO, maybe fires the once-per-month owner quota-stop alert, and returns having sent nothing. `access.approve_user`'s try/except never even executes its `except` branch, because nothing raised. This is **worse** than the outage case: there isn't even an exception to catch. **Confirmed FINDING:** a user can be approved while quota is hard-stopped, never receive the welcome push, and the owner is told "They've been messaged" with no way to know otherwise from this page.

**Fix required (not owned solely by USERS):** `core/access.py:approve_user` is shared surface (also used by the chat `/approve` command). Giving `handle_approve` the information it needs to pick between `portal_users_flash_approved` and the already-specified `portal_flash_approve_nopush` requires either (a) `approve_user` returning whether the push actually sent (a signature change touching the chat-command caller too), or (b) `Channel.send()` itself returning a real success signal instead of always `None` on a quiet quota-drop (a deeper interface change). Recommend: **hand back to Luna** for the i18n key + flash-selection logic in `users.py`, coordinated with **Archi** for the `approve_user`/`Channel.send` signature question since it's outside USERS' owned files.

### #4 — UI.md's discrete 7-column table vs UX.md's combined "stats line" copy

**Ruling: PASS — Luna made the correct authority call.** I read both documents directly. UI.md §5 Screen 2 (the later, more specific document in the pipeline: Sophia → Maya(UX) → Iris(UI) → Luna) explicitly enumerates 7 discrete columns for the Active table ("Name · Chat ID · Last log · Streak · Digest · Language · action") as the literal markup, and UX.md's own general rule (§ "Design principles," line 728) is "Tables collapse to cards below 600px via `td[data-label]`, **not by rendering the data twice**" — a structural requirement pointing at per-column `data-label` attributes, which is exactly what `layout.td_cell` implements and `users.py` uses correctly for every cell. UX.md's "stats line" combined phrasing (§7, line 801) reads as wireframe-stage shorthand for the *card* presentation, not a contradiction of UI.md's later, markup-authoritative column list. Following UI.md over the earlier UX wireframe note is the correct pipeline behavior (Iris's job is precisely to firm up Maya's wireframe into buildable markup). This is a documentation-hygiene item for Archi/Maya/Iris to reconcile for future clarity, not a code defect — no action needed from Luna.

## Findings

### Finding 1 — Approve flash is unconditionally, actively false on push failure (Severity: High)

See Ruling #3 above for the full analysis, spec citation, and both reproducing tests. This is the primary actionable output of this pass.

- **AC violated:** None of AC15-21 directly (no AC numbers this behavior) — this is a UX.md §3 Flow B violation.
- **Where:** `src/habit_assistant/core/portal/users.py:_build_flash` (redirect-result branch `ok == "approve"`) always resolves `portal_users_flash_approved`; `src/habit_assistant/core/access.py:approve_user` (lines ~289-296) swallows the push outcome with no return signal.
- **Reproduction:** `tests/test_portal_users_gaps.py::test_approve_flash_falsely_claims_messaged_when_push_raises` and `::test_approve_flash_falsely_claims_messaged_when_push_is_silently_dropped_by_quota_gate` (both pass today, pinning the wrong behavior on purpose so CI shows the gap).
- **Suspected cause / fix location:** `core/portal/users.py:handle_approve` + a signature change to `core/access.py:approve_user` (shared surface — needs Archi's coordination since the chat `/approve` command also calls it) + a new `portal_flash_approve_nopush` catalog entry in `core/i18n.py` (copy already fully specified in UX.md §7, line 817 — no design work needed, just wiring).

### Finding 2 — No length cap on `display_name` in the portal listing (Severity: Low, note only)

`core/portal/users.py` applies zero truncation to `display_name` anywhere in the pending/active listing, unlike the chat `/users` command (`core/access.py:_USERS_NAME_MAX_CHARS = 24`, via `render_budget.truncate`). Confirmed safe (escaped, doesn't crash — `test_display_name_4000_chars_renders_without_crashing` passes with a 4000-char name rendering fully and correctly escaped) but a real layout-budget inconsistency versus the chat surface's own established convention. No AC requires truncation here; flagging for Luna/Iris to consider a cap if it comes up in visual QA.

### Finding 3 — Invite's chat_id validation is intentionally looser than the chat command's creation floor (Severity: Informational, no action)

R-USER-4 explicitly specifies `access._CHAT_ID_RE` (16-40 chars after `U`) for Invite, not the chat command's own `_is_full_id_eligible_for_creation` 33-char floor (added in the line/v1.1.0 hardening pass, TEST-LINE-1.1.0.md F1/F2). Confirmed via `test_invite_accepts_shape_valid_id_shorter_than_chat_commands_creation_floor`: a 20-char shape-valid id IS created by Invite. This is spec-compliant as written (R-USER-4 names the looser regex) and defensible on its own merits — Invite is a single-purpose creation form with its own typo-guard interstitial (the exact mitigation the chat command's floor exists to approximate through a text warning instead), so the chat command's prefix-collision rationale doesn't transfer. No action needed; documented so a future spec tightening is a deliberate choice.

## Verified structural facts (adversarial checklist, no dedicated numbered AC)

- **No cookie-based session anywhere on the mutation surface.** `test_no_set_cookie_header_on_any_users_response` confirms no `Set-Cookie` on GET, POST, or the 403 path. Grepped `core/portal/` for `cookie`/`Cookie`/`Set-Cookie` — zero matches. The tailnet+header model (R-SEC-3/4) is genuinely the only auth boundary; there is no second, weaker session-based factor a public site could ride.
- **The public 8080 Funnel app cannot proxy a portal POST.** Grepped `channels/line.py` for `"portal"` — zero matches. `PortalServer` (`core/portal/server.py`) builds and runs its own separate `aiohttp.web.Application` on its own port/task (R-SEC-5/R-SEC-6); the two apps share no router, no code path. Structural, not just configuration-dependent.
- **Approve of a chat_id targets exactly that row, never a different pending one** (`test_approve_targets_only_the_submitted_chat_id_not_a_different_pending_row`) — no index/iteration confusion when 2+ pending rows exist.
- **Approve of an already-blocked user reactivates them** (`test_approve_of_an_already_blocked_user_reactivates_them`) — pinned, PASS-with-note: not reachable from the UI (no Approve control renders for a blocked row), only via a direct owner POST; reasonable and symmetric with Q6's block-an-active-user capability, not contradicted by any AC.
- **Push fires exactly once per approve call** (`test_approve_push_fires_exactly_once_per_approve`); the double-submit/double-confirm idempotency tests confirm it fires again (not de-duplicated) on a genuine second submission — consistent with `test_portal_users.py`'s own pinned double-submit-approve semantics.

## Regressions detected

None. `test_portal_users.py`'s original 27 tests are unmodified and all still pass; the full USERS-relevant regression set (260 tests) and the full LINE-edition gate (5432 passed) show no breakage attributable to this module.

## Recommendation

**Hand back to Luna — 1 finding** (Finding 1: approve-flash honesty, UX.md §3 Flow B). Not a blocker for AC15-21 sign-off (all 7 owned ACs are green), but it is a confirmed violation of an explicit UX "must" requirement with the exact fix already specified in UX.md §7 — recommend fixing before this ships to the owner. The fix touches `core/access.py` (shared surface, used by the chat `/approve` command too), so **route the `approve_user` signature question through Archi** before Luna edits that file. Findings 2 and 3 are informational; no action required.
