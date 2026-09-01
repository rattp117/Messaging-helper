# Implementation — Admin Web Portal, module USERS

> Branch `line-version`. Consumes `SPEC-LINE-PORTAL.md` §4 R-USER-*/§11 (module
> USERS, ACs 15-21), `UX.md` Flows B/E + Screens 2/3 + §8 Q6/Q7 (both adopted),
> `UI.md` §3.10/§3.12/§3.22 + Screen 2/Screen 3, and `IMPL-PORTAL-shared.md`
> (the shared surface this pass builds on: `PortalServer`/`PortalDeps`,
> `identity_gate`, `layout.py`, `access.approve_user`/`block_user`).

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/portal/users.py` | Created | `GET /users` (pending + active + invite), `POST /users/approve`, `POST /users/block`, two-step `POST /users/invite` (unconfirmed → interstitial, `confirm=yes` → write). |
| `tests/test_portal_users.py` | Created | 27 tests covering AC15-AC21, Q6, Q7, and the double-submit/adversarial cases from the dispatch note. |
| `src/habit_assistant/core/audit.py` | Modified | Added `"portal"` to `Source`/`SOURCES` — a closed-vocabulary gap in the shared surface (see "Known limitations"). |
| `tests/test_audit.py` | Modified | Updated `test_sources_matches_the_spec_vocabulary_exactly` to include `"portal"`. |
| `src/habit_assistant/core/i18n.py` | Modified | +41 `portal_users_*`/`portal_relative_*` keys (per-page microcopy, this module's own pass per the shared IMPL doc's explicit convention). |

## How it works

`register(app, deps)` registers the four routes; each handler reads `deps` from `request.app["portal_deps"]` (set once by `PortalServer.build_app`), matching `security.py`/`_error_middleware`'s own convention rather than closing over `deps` at registration time. `GET /users` renders three independently-fail-open sections (Pending cards, an Active `<table class="collapse">`, an Invite form) built from `db.list_users()`, `db.last_log()`, and `HabitRegistry.for_user(...)` + `streaks.compute_streak` for the per-user streak. Every mutation (`approve`/`block`/the confirmed `invite`) calls `core/access.py`'s shared `approve_user`/`block_user` with `actor=deps.owner_id, source="portal"` — this module never writes to `users`/`audit_log` directly — then redirects `303` back to `/users?ok=...`/`?err=...#flash` (POST-redirect-GET, per `layout.redirect_with_flash`). The owner's own row renders "You (owner)" with no Block control (Q6/Q7), and `POST /users/block` independently refuses `chat_id == deps.owner_id` server-side before any write, regardless of what the UI rendered.

## Smoke test done

- Manual end-to-end run against a real on-disk `Database` + real `PortalServer` (script in scratchpad, not committed): GET /users renders pending/active/invite sections correctly in the owner's resolved language (Thai by default); POST approve/block/invite all wrote the correct DB state, audit rows, and pushes; the owner's row showed no Block control and a forged `POST /users/block {chat_id: OWNER}` was refused with `err=block_owner`; the invite interstitial rendered with no `<nav>` (per UI.md §3.22) and the confirmed POST created the user with `source="portal"`.
- `pytest tests/test_portal_users.py tests/test_portal_security.py tests/test_portal_server.py tests/test_audit.py tests/test_access.py -q` → **131 passed**.
- `pytest tests/test_portal_users.py tests/test_portal_security.py tests/test_portal_server.py tests/test_portal_layout.py tests/test_portal_stats.py tests/test_portal_db.py tests/test_portal_integration.py tests/test_audit.py tests/test_audit_capture_gaps.py tests/test_access.py tests/test_i18n.py tests/test_i18n_literals.py -q` → **237 passed** (full shared-portal-surface regression).
- Full LINE gate: `pytest -m "not telegram_only and not llm_only" -n auto` → **5318 passed, 2 failed, 4 skipped, 1 xfailed**. The 2 failures (`test_refactor_s2_verify.py::test_independent_disable_notification_sweep_matches_test_riders_expectation`, `test_riders.py::test_exactly_five_call_sites_pass_disable_notification_ticks_plus_v19_jobs`) are **not caused by this module** — both are closed-vocabulary sweeps over the whole `src/` tree that now also enumerate `core/portal/quota.py:1` (a `channel.send(..., disable_notification=...)` call site added by the parallel QUOTA Luna's own module). Confirmed by grep: neither `core/portal/users.py` nor `core/audit.py` contains `disable_notification`. Flagging for Archi to route to the QUOTA track or the two sweep tests' own next update — not something this pass should fix (out of my owned files).

## Maps to acceptance criteria

- **AC15** → `_render_pending_section`/`_pending_card` in `users.py` → `test_pending_user_with_name_lists_name_and_id_and_controls`, `test_pending_user_with_no_name_shows_id_as_headline`.
- **AC16** → `handle_approve` → `access.approve_user(..., source="portal")` → `test_approve_activates_user_writes_audit_and_sends_push` (asserts status→active, audit row `action="user_approve"` `source="portal"`, the `access_granted` push landed in `channel.sent_to`, and the redirected page shows the flash).
- **AC17** → `handle_block` → `access.block_user(..., source="portal")` → `test_block_active_user_writes_audit_row`, `test_block_pending_user_writes_audit_row`.
- **AC18** → `handle_invite` (confirmed branch) → `test_invite_confirmed_creates_active_user_with_portal_source`.
- **AC19** → `_active_table`/`_active_row` → `test_active_row_shows_last_log_streak_digest_and_language`, `test_active_row_with_no_logs_shows_never_logged`, `test_active_row_digest_opt_out_renders_off`.
- **AC20** → every handler re-derives nothing from the request except via the already-gated app → `test_post_without_identity_header_is_refused_with_no_write` (parametrized over all 3 POST routes) + `test_get_users_without_identity_header_is_refused`.
- **AC21** → the `db.get_user(chat_id) is None` / empty-`chat_id` checks in `handle_approve`/`handle_block`, and the `_CHAT_ID_RE` check in `handle_invite` → `test_approve_missing_chat_id_is_rejected_with_no_write`, `test_approve_nonexistent_chat_id_is_rejected_with_no_write`, `test_block_nonexistent_chat_id_is_rejected_with_no_write`, `test_invite_invalid_shape_redirects_with_error_and_echoes_typed_value`, `test_invite_invalid_shape_truncates_echoed_value_to_64_chars`, `test_invite_empty_chat_id_is_rejected_with_no_write`.
- **Q6 (adopted)** → `_active_row`'s per-row Block `confirm_disclosure` for non-owner rows → `test_non_owner_active_row_has_block_control`.
- **Q7 (adopted)** → UI omission (`_active_row`'s `is_owner` branch) + server-side refusal (`handle_block`'s `chat_id == deps.owner_id` check) → `test_owner_row_renders_as_you_owner_with_no_block_control` + the adversarial `test_forged_post_block_on_owner_row_is_refused_no_write_no_audit`.
- **Double-submit (dispatch note)** → `test_double_submit_approve_is_idempotent_friendly_not_an_error`, pinning `access.approve_user`'s existing plain-upsert semantics: a second approve of an already-active user is NOT an error — it re-affirms `active`, writes a second audit row (`old_value="active", new_value="active"`), and re-sends the `access_granted` push. Documented as intentional, not a bug, per the dispatch note's own framing ("verify what it does and pin it").

All 7 owned ACs (AC15-AC21) plus both flagged UX rulings (Q6/Q7) are covered.

## Known limitations

- **`audit.Source`/`SOURCES` vocabulary gap, closed by this pass.** `core/access.py:approve_user`/`block_user` type `source` as `audit.Source`, and SPEC-LINE-PORTAL.md R-USERACT-1 explicitly requires `source="portal"` writes (AC16-18) — but `IMPL-PORTAL-shared.md`'s own file list doesn't include `core/audit.py`, and the pre-existing `Source`/`SOURCES` closed vocabulary didn't have `"portal"` in it (Python doesn't enforce `Literal` at runtime, so the writes worked anyway, but `tests/test_audit.py::test_sources_matches_the_spec_vocabulary_exactly` asserted the OLD 5-value set and would have failed the moment any module exercised a portal-sourced write). Fixed with a one-line addition to each of `Source`/`SOURCES` plus the one test assertion that hardcoded the closed set. Flagging to Archi since this is a shared-surface correction, not a USERS-only concern — other modules (AUDIT rendering `source="portal"` rows, for instance) depend on this same fix.
- **"Current streak" (AC19) is ambiguous in a multi-habit app** — SPEC/UX don't say which habit's streak to show. This pass shows the **MAX** `streaks.compute_streak` across the user's own habit registry (base + custom habits) as the single headline number, documented inline in `_current_streak`'s docstring. Reasonable, not spec-mandated; flag if Vera or the user wants a different rule (e.g. a specific habit, or a sum).
- **The approve success flash always says "messaged"**, even though `access.approve_user`'s `access_granted` push failure is caught *inside* that function and never surfaced to the caller (UX Flow B documents a `portal_flash_approve_nopush` variant for this case, i18n key not added here). Observing push success from this module would require a signature change to `approve_user` (shared-surface, out of my owned files) — flagging rather than silently expanding scope. No AC requires this distinction (AC16 only requires the push attempt happens, not that the flash react to its outcome).
- **UX.md's `portal_users_stats_line` combined copy (§7, "Last log {ago} · Streak {streak} · Digest {digest} · {lang}") was not used.** UI.md §5 Screen 2 explicitly lists 7 discrete table columns (Name/Chat ID/Last log/Streak/Digest/Language/action) for the Active list and states the general "collapse to cards via `td[data-label]`, not by rendering the data twice" rule (UX.md line 728) — the two documents conflict on whether phone-width Active rows get a combined single line or the same `.collapse` table CSS-transformed. This pass followed UI.md's explicit column list (the final markup authority) over the wireframe's combined phrasing; flagging for Iris/Vera to confirm or correct.
- Digest column values ("on"/"off") and the Language column (`language_pref.upper()`) are new, small, unlabeled-in-spec strings — reasonable choices, not cited verbatim from any doc.
- The `disable_notification` sweep-test failures noted in "Smoke test done" are **not this module's issue** (traced to `core/portal/quota.py`, a different parallel Luna's file) — flagged, not fixed, consistent with "never touch a file outside your scope."

## Iteration log

None yet — first pass, all owned tests green on the first full run (one self-inflicted test bug fixed before handoff: `test_register_adds_exactly_the_four_spec_routes` didn't account for aiohttp's automatic HEAD-alongside-GET route).
