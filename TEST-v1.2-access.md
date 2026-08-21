# Test Report — v1.2.0 `access` module (onboarding + owner-only admin)

## Summary
- Scope: 7 ACs owned by the `access` module (SPEC-v1.2.md §11): **AC-A1** – **AC-A7**.
- Total (`tests/test_access.py` + `tests/test_v12_access_gaps.py`): **112 tests** (37 Luna + **75 new, this pass**).
- Passed: 112 / 112 (module scope).
- Failed: 0.
- Full repo suite: **1259 passed, 4 failed, 1 skipped** (baseline 1114 passed / 1 skipped). The 4 failures are **pre-existing and out of this module's scope** — `tests/test_preferences.py`'s Thai-alias-misfire cluster on `/lang`/`/quiet` (module `preferences`, already discovered and reported by that module's own Vera pass in `TEST-v1.2-preferences.md`, recommendation "hand back to Luna"). Verified they touch none of `access`'s owned files (`core/access.py`, the `access` kinds in `core/commands.py`, the `access` keys in `core/i18n.py`) and reproduce identically with or without this pass's additions. **No regression attributable to this pass.**
- Status: **PASS** — all 7 owned ACs pass; module is ready to ship.

## Test files
| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_access.py` (Luna) | 37 | AC-A1, AC-A2, AC-A3, AC-A4, AC-A5, AC-A6, AC-A7 (primary/happy paths + `dispatch` shape recognition) |
| `tests/test_v12_access_gaps.py` (Vera, new this pass) | 75 | AC-A1, AC-A3, AC-A4, AC-A5, AC-A6, AC-A7 (adversarial hardening — see below) |

New tests added this pass, by angle:
- **AC-A4 hardening (non-owner refusal, every state):** `test_execute_admin_noop_for_non_active_non_owner_status` (pending/blocked member attempting admin ops), `test_execute_admin_noop_for_unknown_chat` (a chat with no `users` row at all).
- **Security-critical fail-safe, extended past `classify`:** `test_execute_admin_fails_safe_when_role_lookup_errors_even_for_the_real_owner` — a DB error on the owner's own re-check during `execute_admin` must still deny, not silently allow.
- **Owner self-block:** `test_owner_blocking_self_does_not_revoke_owner_classify` — documents that `role="owner"` is authoritative over `status` (R-A1 "owner ⊂ active"), so `/block <owner>` writes `status='blocked'` but `classify`/`handle_gate` still treat the owner as active. By design, not a defect.
- **Idempotency / round-trips:** `test_approving_already_active_user_is_idempotent`, `test_block_then_reapprove_restores_active_access`, `test_invite_reactivates_an_existing_pending_row`.
- **`/approve`/`/block` chat-id validation:** `test_approve_rejects_malformed_or_missing_chat_id` (9 cases: empty, whitespace, `+123`, `12.5`, `--123`, `123-456`, `abc`, `12ab`, `None`), `test_approve_accepts_huge_negative_zero_and_leading_zero_chat_ids` (4 cases: 26-digit number, negative/group-chat-shaped, `"0"`, `"007"`), `test_block_rejects_malformed_chat_id_without_writing`, `test_dispatch_approve_trailing_whitespace_only_still_matches_bare_form`.
- **AC-A1 structural proof — zero logs, zero LLM calls for a gated-off message:** `test_handle_gate_signature_has_no_llm_reference` (the function has no parameter an LLM could ever be reached through), `test_handle_gate_never_writes_a_log_row_for_a_non_proceeding_caller` (parametrized unknown/pending/blocked — `db.insert_log` monkeypatched to raise if ever called, plus a direct `SELECT COUNT(*) FROM logs` check), `test_handle_gate_unknown_path_survives_a_write_failure_on_the_pending_row`.
- **AC-A1 spam check:** `test_owner_notified_exactly_once_across_many_repeat_messages_while_pending` — 6 messages (including a `/start`) from the same still-pending stranger; owner gets exactly 1 notification, stranger gets 6 replies.
- **AC-A6 completeness:** `test_start_from_pending_chat_gets_access_pending`, `test_start_from_blocked_chat_gets_access_denied` (Luna's own tests covered active/unknown; pending/blocked were untested).
- **`classify` defensive fallback:** `test_classify_unexpected_status_value_falls_back_to_pending` (an out-of-band `status` value, e.g. from a future migration, must not crash or grant).
- **Bilingual coverage (no KeyError / mojibake):** `test_approve_grants_access_in_thai_when_target_prefers_thai`, `test_admin_usage_reply_in_thai`, `test_admin_save_failed_reply_both_languages_on_db_error`, `test_users_list_in_thai_has_no_keyerror_or_mojibake`, `test_start_welcome_in_thai`, `test_every_access_catalog_key_formats_cleanly_both_languages` (all 11 keys this module owns, both languages).
- **Cross-track dispatch precedence:** `test_other_track_commands_are_not_shadowed_by_access_patterns` (14 cases: undo, target, help, habits, remind + Thai alias, lang + Thai alias, quiet + Thai alias, snooze, query), `test_access_commands_are_not_shadowed_by_any_other_track` (5 cases), `test_access_near_misses_do_not_false_positive` (7 near-miss strings: `/startup`, `/usersome`, `/approve123`, `/blocking 5`, `/invited`, `restart`, `please /users`).
- **Finding (low severity, documented, not a fail):** `test_execute_admin_start_branch_has_no_defense_in_depth_role_check` — see "Findings" below.

## AC coverage
| AC | Test(s) | Status |
|---|---|---|
| **AC-A1** (unknown → pending row, `access_pending` + owner `access_request`, message neither logged nor LLM'd) | Luna: `test_handle_gate_unknown_creates_pending_and_notifies_owner`, `test_handle_gate_unknown_no_display_name_falls_back_to_chat_id`. Vera: `test_handle_gate_signature_has_no_llm_reference`, `test_handle_gate_never_writes_a_log_row_for_a_non_proceeding_caller` (×3), `test_handle_gate_unknown_path_survives_a_write_failure_on_the_pending_row`, `test_owner_notified_exactly_once_across_many_repeat_messages_while_pending` | **PASS** |
| **AC-A2** (`/approve` → active, `access_granted`, can log normally) | Luna: `test_execute_admin_approve_grants_access_and_notifies_target`, `test_end_to_end_owner_approves_a_stranger_who_can_then_proceed`. Vera: `test_approving_already_active_user_is_idempotent`, `test_block_then_reapprove_restores_active_access`, `test_invite_reactivates_an_existing_pending_row`, `test_approve_accepts_huge_negative_zero_and_leading_zero_chat_ids`, `test_approve_grants_access_in_thai_when_target_prefers_thai` | **PASS** |
| **AC-A3** (`/block` → blocked, next message `access_denied`, not processed) | Luna: `test_execute_admin_block_revokes_access`, `test_handle_gate_blocked_chat_denied`. Vera: `test_owner_blocking_self_does_not_revoke_owner_classify`, `test_start_from_blocked_chat_gets_access_denied` | **PASS** |
| **AC-A4** (non-owner admin command → not executed, reveals nothing) | Luna: `test_execute_admin_admin_commands_invisible_to_non_owner`. Vera: `test_execute_admin_noop_for_non_active_non_owner_status` (×2), `test_execute_admin_noop_for_unknown_chat`, `test_execute_admin_fails_safe_when_role_lookup_errors_even_for_the_real_owner` | **PASS** |
| **AC-A5** (`/users` → role + status listing) | Luna: `test_execute_admin_users_lists_everyone`. Vera: `test_users_list_in_thai_has_no_keyerror_or_mojibake` | **PASS** |
| **AC-A6** (`/start`: active → welcome; unknown → pending flow) | Luna: `test_execute_admin_start_active_user_gets_welcome`, `test_end_to_end_start_from_unknown_runs_pending_flow`. Vera: `test_start_from_pending_chat_gets_access_pending`, `test_start_from_blocked_chat_gets_access_denied`, `test_start_welcome_in_thai` | **PASS** |
| **AC-A7** (fail-safe: lookup error → not active, never granted) | Luna: `test_classify_fails_safe_on_lookup_error`, `test_handle_gate_fails_safe_on_lookup_error`. Vera: `test_execute_admin_fails_safe_when_role_lookup_errors_even_for_the_real_owner`, `test_classify_unexpected_status_value_falls_back_to_pending` | **PASS** |

Plus, not AC-numbered but required by the dispatch scope: `test_every_access_catalog_key_formats_cleanly_both_languages`, `test_other_track_commands_are_not_shadowed_by_access_patterns`, `test_access_commands_are_not_shadowed_by_any_other_track`, `test_access_near_misses_do_not_false_positive`, `test_dispatch_approve_trailing_whitespace_only_still_matches_bare_form`, `test_approve_rejects_malformed_or_missing_chat_id`, `test_block_rejects_malformed_chat_id_without_writing`, `test_admin_usage_reply_in_thai`, `test_admin_save_failed_reply_both_languages_on_db_error` — all **PASS**.

## Failures (module scope)
None. 112/112 pass.

## Judgment-call audit (per dispatch instructions)

### Known Limitation #2 — AC-A4 "falls through as an unknown message" implemented as a silent no-op
**Verdict: CONFORMANT.**

Reasoning:
- §5's own interface literally declares `async def execute_admin(...) -> None` — there is no return channel through which `execute_admin` could signal "not handled, please re-route to the LLM parser" back to its caller. A literal re-route is not constructible from the spec's own given signature without adding a mechanism the spec doesn't describe.
- Structurally, `commands.dispatch` already fully commits to recognizing `/approve`/`/block`/`/users`/`/invite` syntax as a `Command` (never `None` for that shape — mirrors `/target`'s own "recognized shape → always a `Command`" convention documented in `core/commands.py`). By the time `execute_admin` is reached, the message has already left the `dispatch() is None → fall through to parser` path for good; there is no natural mechanism for it to re-enter that path later.
- AC-A4's own pass condition is exactly two things: "not executed" and "reveals nothing." A silent no-op satisfies both — arguably more strictly than routing to the LLM parser would, since a parser fallback could itself produce a visible reply (e.g. a clarifying question), which is a *different* kind of "something happened" signal a truly invisible design should avoid.
- §3.2's illustrative example for this case (`🤔 (falls through as an unknown command — admin commands are owner-only and invisible to others)`) is formatted as a parenthetical description, unlike every other entry in that block which shows literal quoted bot copy — supporting the reading that this line describes the *effect* ("behaves as if the command doesn't exist"), not literal required reply text.
- Verified behaviorally by `test_execute_admin_admin_commands_invisible_to_non_owner` (Luna) and `test_execute_admin_noop_for_non_active_non_owner_status`/`test_execute_admin_noop_for_unknown_chat` (Vera, extended to pending/blocked/unknown callers, not just active non-owner members).

No escalation needed — this is a defensible, spec-consistent interpretation, transparently flagged by Luna.

### Known Limitation #3 — copy-mapping `access_pending` vs. `access_denied`, owner acks
**Verdict: CONFORMANT** (for the copy mapping); **acceptable, non-blocking addition** (for the owner acks).

Reasoning: R-A2 (§4) mandates `access_pending` for the unknown-first-contact case; R-A3 mandates `access_pending`/`access_denied` respectively for `pending`/`blocked` repeat contacts. Reading R-A2 and R-A3 together (not just §3.2's two illustrative example captions in isolation), there is no actual contradiction in the *operative* rule text: every `pending`-state contact — whether transitioning from `unknown` on first message or repeating while still `pending` — gets `access_pending`; only `blocked` gets `access_denied`. That is exactly what `core/access.py` implements (`handle_gate`'s `unknown` and `pending` branches both send `i18n.t("access_pending", lang)`; only the fallback/`blocked` branch sends `access_denied`). Verified by `test_handle_gate_unknown_creates_pending_and_notifies_owner`, `test_handle_gate_pending_repeat_message`, `test_handle_gate_blocked_chat_denied` (Luna) and `test_start_from_pending_chat_gets_access_pending`/`test_start_from_blocked_chat_gets_access_denied` (Vera).

The added owner-facing `admin_approved_ack`/`admin_blocked_ack` replies are additive UX beyond R-A4's literal text (which only specifies the *target* chat's `access_granted`) but do not change any AC's pass/fail condition and are consistent with every other command in this codebase confirming back to its own caller (undo, edit, target, snooze). No objection.

## Findings (informational, non-blocking)

### `execute_admin`'s `"start"` branch has no defense-in-depth role/status check
Every other kind `execute_admin` handles (`approve`/`block`/`users`/`invite`) redundantly re-checks `classify(db, chat_id) == "owner"` even though the caller is documented to already be gated ("belt-and-suspenders," per the function's own docstring). The `"start"` branch has no equivalent check at all — it unconditionally sends `start_welcome` to whoever called it, relying entirely on the precondition that `handle_gate` already returned `True` for that chat. Under the documented integration wiring this precondition always holds (an unknown/pending/blocked chat's `/start` is fully absorbed by `handle_gate` and never reaches `commands.dispatch`/`execute_admin` at all), so this is **not reachable in the wired system as designed** and does not violate AC-A6 as written. It is, however, the one command in this module without the same defense-in-depth every other command has, and the leaked information (a static "Welcome back!" string, not any state change or data) is low severity. Demonstrated by `test_execute_admin_start_branch_has_no_defense_in_depth_role_check`. Suggest Luna add the same one-line re-check for consistency, at her discretion — not a blocker.

## Regressions detected
None caused by this pass. `tests/test_commands.py`, `tests/test_i18n.py`, `tests/test_i18n_literals.py` (the three shared files `access` touches or depends on) re-verified independently: **133/133 pass**. Full repo suite: 1259 passed / 4 failed / 1 skipped — the 4 failures are entirely inside `tests/test_preferences.py` (module `preferences`'s own Thai-alias-misfire cluster, independently discovered and already reported to Luna in `TEST-v1.2-preferences.md`); confirmed unrelated to any file this module owns and present identically with or without this pass's test additions.

## Recommendation
**Ready to ship** — module `access`. All 7 owned ACs (AC-A1–AC-A7) pass across 112 tests (0 failures), including the adversarial security-critical, fail-safe, bilingual, and cross-track angles requested beyond Luna's own primary-path suite. One low-severity, non-blocking hardening suggestion for Luna (`"start"` branch defense-in-depth) and two judgment calls audited as CONFORMANT are documented above for Archi's awareness. The 4 full-suite failures belong to the `preferences` module and are already in that module's own hand-back queue — not a gate on this module.
