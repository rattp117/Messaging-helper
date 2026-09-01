# Test Report — Admin Web Portal, RELEASE GATE (line/v1.3.0)

> Final sign-off pass, above the four module Veras (`TEST-PORTAL-status.md`/
> `-users.md`/`-audit.md`/`-quota.md`) and the integration pass's own
> `tests/test_portal_integration.py` (item 9, 5 e2e tests). Verifies
> `IMPL-PORTAL-integration.md`'s 9-item worklist against `SPEC-LINE-PORTAL.md`'s
> full 32 ACs, at the WIRED (real two-listener app) level, plus the
> `TEST-LEDGER-TRIAGE.md` / `IMPL-LEDGER-CLOCK-FIX.md` push-ledger clock fix
> folded into this same release. No production code was touched — tests
> (`tests/test_portal_release_gate.py`, new) and this report only.

## Summary

- Total (this pass): 9 new tests, all passing — `tests/test_portal_release_gate.py`
- Total (full LINE-edition gate, `pytest -m "not telegram_only and not llm_only"`): **5524 passed, 4 skipped, 1 xfailed, 0 failed**, identical across all 3 required runs
- Portal-only serial spot-run (`tests/test_portal_*.py`, 34 files incl. this pass's new file): **340 passed, 0 failed**
- `python -c import ...` smoke test: clean, `__version__ == "1.3.0+line"`
- **Status: PASS.** All 32 ACs green. Every escalated finding from the four module Veras is confirmed CLOSED (evidence below). Two pre-existing low-severity items remain open by design, documented, non-blocking (see "Remaining open items").
- **Verdict: RELEASE — safe to tag and deploy v1.3.0+line.**

## Three full-gate runs (required exit bar)

| Run | Mode | Result | Wall time |
|---|---|---|---|
| 1 | `-n auto` (parallel) | 5524 passed, 4 skipped, 1 xfailed, **0 failed** | 102.1s |
| 2 | `-n auto` (parallel) | 5524 passed, 4 skipped, 1 xfailed, **0 failed** | 98.4s |
| 3 | serial (no `-n`) | 5524 passed, 4 skipped, 1 xfailed, **0 failed** | 266.7s |

Identical pass/skip/xfail counts across all three runs — no flakiness, no worker-interleaving pollution (the global `_reset_daily_digest_claim` autouse fixture added at the integration pass holds under `-n auto`).

Baseline (before this pass added any tests) was independently re-confirmed first: `5515 passed, 4 skipped, 1 xfailed, 0 failed` — exactly matching `IMPL-PORTAL-integration.md`'s own smoke-test claim. This pass's 9 new tests bring the total to 5524.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_portal_release_gate.py` (new, this pass) | 9 | Full owner journey through the real wired app (webhook→notify→approve→log→activity→audit→quota); security-boundary re-proof by ROUTER ENUMERATION on both listeners + cross-port spoofed-header proof; migration 015 on a realistic multi-user seeded DB; Telegram-mode regression (both `enabled` values, parametrized); version-consistency + release-notes-posture pin; digest double-send guard in the second order (scheduled-then-manual) |

## 32-AC coverage map (wired level)

Every AC below is green. Where this pass added new wired-level proof beyond the module Veras' own suites, the new test is cited; otherwise the existing, still-passing module-level test is cited (re-confirmed in this run).

| AC | Tier | Requirement (one line) | Wired-level test(s) | Result |
|---|---|---|---|---|
| AC1 | M | `enabled=false` → nothing binds, LINE suite unchanged | `test_portal_integration.py::test_portal_disabled_by_default_binds_nothing` | PASS |
| AC2 | M | `enabled=true` + Telegram → never constructed | `test_portal_integration.py::test_portal_never_constructs_on_telegram_even_if_enabled` + **`test_portal_release_gate.py::test_telegram_mode_never_gets_the_portal_regardless_of_enabled_startup_clean[True]`** (release-gate re-proof + startup-clean angle) | PASS |
| AC3 | M | no identity header → 403, no admin content | `test_portal_security.py`, `test_portal_app_rejects_headerless_requests_on_every_route` + **`test_portal_release_gate.py::test_portal_router_enumerated_403s_headerless_on_every_real_registered_route`** (enumerated, not a guess-list) | PASS |
| AC4 | M | `owner_login` set, wrong login → 403 | module-level `wrong_owner_login` tests (users/audit/quota/status gaps files) | PASS |
| AC5 | M | correct header (+ matching `owner_login`) → `GET /` 200 | `test_portal_status.py` + **owner-journey test** (status page renders 200 mid-journey) | PASS |
| AC6 | M | forged non-matching `owner_login` → 403, never authorized | `test_portal_security.py` + module-level wrong-login tests | PASS |
| AC7 | M | port collision → `ConfigError`; distinct ports bind | `test_config.py` port-collision validator test | PASS |
| AC8 | M | Status: version/channel/Ollama tiles | `test_portal_status.py::test_ac8_*` | PASS |
| AC9 | M | Status: uptime from `started_at` | `test_portal_status.py::test_ac9_*` | PASS |
| AC10 | S | Status: last-webhook-event / empty state | `test_portal_status.py` + `_gaps.py` (relative-time boundaries) | PASS |
| AC11 | M | Status: scheduler jobs + next-run | `test_portal_status.py` + `_gaps.py` (dead job, raising `get_jobs()`) | PASS |
| AC12 | M | Status: quota gauge used/cap/pct/mode | `test_portal_status.py` + `_gaps.py` (79/80/99/100/105% boundaries) + **owner-journey test** (real live used-count cross-checked against `db.monthly_push_total`) | PASS |
| AC13 | M | Status: DB/media/backup sizes + list | `test_portal_status.py` + `_gaps.py` | PASS |
| AC14 | S | Status: recent-errors ring buffer + empty state | `test_portal_status.py` + `_gaps.py` (raising `records()`/`len()`) | PASS |
| AC15 | M | Users: pending list, name/id + controls | `test_portal_users.py` + `_gaps.py` (XSS) | PASS |
| AC16 | M | Users: approve → active, audit, push, page reflects | `test_portal_users.py` + `_gaps.py` (Finding 1 closure) + `test_portal_integration.py` item-9 e2e + **owner-journey test** (real end-to-end) | PASS |
| AC17 | M | Users: block → blocked, audit | `test_portal_users.py` + `_gaps.py` | PASS |
| AC18 | S | Users: invite → active, `source=portal` | `test_portal_users.py` + `_gaps.py` | PASS |
| AC19 | M | Users: active-row stats | `test_portal_users.py` + `_gaps.py` | PASS |
| AC20 | M | Users: POST w/o header → 403, no write | `test_portal_users_gaps.py` + `test_portal_mutations_require_the_identity_header_through_the_real_app` + **router-enumeration test** (POST routes included) + **spoofed-cross-port test** | PASS |
| AC21 | M | Users: missing/unresolvable `chat_id` → inline error | `test_portal_users_gaps.py` | PASS |
| AC22 | M | Audit: `?page=2` → rows 50..99, pager | `test_portal_audit.py` + `_gaps.py` (off-by-one at exact page size) | PASS |
| AC23 | M | Audit: row field set matches chat `/audit` | `test_portal_audit.py` + `_gaps.py` (`source=portal` vocabulary) | PASS |
| AC24 | M | `/activity`: metadata only, never `raw_message` | `test_portal_audit.py` + `_gaps.py` (exhaustive privacy sweep) + diary-leak closure tests + **owner-journey test** (real log rendered, no raw text) | PASS — MAJOR FINDING CLOSED (see below) |
| AC25 | M | Audit: page-beyond-last clamps, no error | `test_portal_audit_gaps.py` (120-row/3-page dataset, `page=99999`→page 3) | PASS |
| AC26 | M | Quota: monthly totals + current-month breakdown | `test_portal_quota.py` + `_gaps.py` (F1 closure) + **owner-journey test** | PASS |
| AC27 | S | Quota: cap, 80/100% thresholds, fired state | `test_portal_quota.py` + `_gaps.py` | PASS |
| AC28 | S | Quota: digest opt-out + schedule | `test_portal_quota.py` + `_gaps.py` | PASS |
| AC29 | C | `/config`: secrets redacted | `test_portal_quota.py` + `_gaps.py` | PASS |
| AC30 | C | Manual digest: confirm-gated, real fan-out, NO-DOUBLE-SEND | `test_portal_quota.py` + `_gaps.py` (F3 disclosed, F4 closure) + `test_portal_integration.py` item-9 (manual→scheduled) + **`test_digest_run_overlap_guard_scheduled_then_manual_through_real_app`** (scheduled→manual, the missing order) | PASS — both orders now proven at the wired level |
| AC31 | M | Bilingual, no hardcoded literals | `test_i18n.py`, `test_i18n_literals.py` + per-module Thai-specific gaps tests | PASS |
| AC32 | M | All 4 modules registered; disabled ⇒ none exist | `test_portal_integration.py` (all-4-pages test) + **router-enumeration test** (proves `REGISTERED_MODULES` really contains all 4 at release time, by walking the real router, not a hardcoded path list) | PASS |

**32/32 ACs PASS.**

## Findings-closure table (every escalated finding from the four module Veras)

| # | Finding (source report) | Severity | Fix location | Closure evidence | Status |
|---|---|---|---|---|---|
| 1 | Diary/text-habit content leaks via `undo`'s `old_value` (`TEST-PORTAL-audit.md` MAJOR FINDING) | MAJOR | `core/undo_ui.py:50-81` (`_redacted_text_marker`, write-site) + `storage/migrations.py:524-621` (migration 015, historical scrub) | `test_audit_capture.py::test_undo_diary_records_text_value` (flipped to pin the marker) · `test_portal_audit_gaps.py::test_audit_detail_cell_no_longer_leaks_diary_text_via_undo_old_value` · `test_portal_integration.py::test_diary_undo_marker_renders_identically_on_chat_and_portal_audit` (real inline-button undo, real chat `/audit`, real portal `/audit`+`/activity`) · `test_migrations.py`'s 6 new migration-015 tests · **this pass's `test_migration_015_realistic_seeded_db_surgical_scrub_idempotent_schema_stamps_15`** (multi-user, multi-habit-shape DB, real upgrade path, idempotent re-run, schema stamps 15) | **CLOSED** |
| 2 | Approve flash unconditionally claims "messaged" even on push failure (`TEST-PORTAL-users.md` Finding 1, High) | High | `channels/line.py:233-280,399+` (`_emit`/`_push` return a confirmation sentinel) + `core/access.py:231-` (`approve_user` returns `bool`) + `core/portal/users.py:177,401-422` (`_build_flash`/`handle_approve` pick the honest branch) + `core/i18n.py` (`portal_flash_approve_nopush` key) | `test_portal_users_gaps.py::test_approve_flash_honestly_reports_nopush_when_push_raises` / `..._is_silently_dropped_by_quota_gate` · `test_portal_integration.py::test_approve_from_portal_end_to_end_welcome_push_confirmed` / `..._not_confirmed` (both outcomes, real `LineChannel` + real httpx mock outage) · **this pass's owner-journey test** (confirmed-outcome path through the same real wiring) | **CLOSED, both outcomes proven** |
| 3 | Manual digest has no shared guard vs. the independently-scheduled job (`TEST-PORTAL-quota.md` F3, Medium, disclosed-by-design) | Medium | `core/digest.py:461-493,616-` (shared `_DAILY_RUN_CLAIMED` + `run_daily_digest_guarded`) + `core/app.py:670-671` (scheduled job now calls the guarded wrapper) + `core/portal/quota.py:551-,611-` (manual trigger now calls the same guarded wrapper) | `test_portal_integration.py::test_digest_run_overlap_guard_manual_then_scheduled_through_real_app` (order A) · **this pass's `test_digest_run_overlap_guard_scheduled_then_manual_through_real_app`** (order B, previously unproven) — both orders now show 0 double-push at the real wired level. `test_portal_quota_gaps.py::test_manual_digest_run_concurrent_with_scheduled_digest_job_can_double_push` intentionally still calls the RAW, unguarded `digest.run_daily_digest` directly (not the real production wiring) and still shows 4 pushes — **this is not a live gap**, it is a deliberate pin of the low-level function's own documented "no internal dedup, the scheduler owns that" contract (`test_digest.py::test_run_daily_digest_has_no_internal_dedup_the_scheduler_owns_that`, unchanged). Both REAL production call sites are proven guarded. | **CLOSED at the wired level, both orders** |
| 4 | Custom-habit units missing on `/activity`'s Value column (`TEST-PORTAL-audit.md`, Low, cosmetic) | Low | `core/portal/audit.py` (per-request `RegistryProvider(deps.config, deps.db)`, `.for_user(row["user_id"])`) | `test_portal_audit_gaps.py::test_activity_custom_habit_value_renders_with_unit_after_integration_fix` | **CLOSED** |
| 5 | STATUS/QUOTA gauge percent + month-heading formatting diverge (`TEST-PORTAL-status.md` Finding 1 / `TEST-PORTAL-quota.md` F2, Low, cosmetic) | Low | `core/portal/layout.py:226-,240-` (`format_pct`/`format_month_heading` promoted to one shared source) | `test_portal_status_gaps.py::test_status_and_quota_percent_formatting_now_matches_on_round_numbers` · `test_portal_quota_gaps.py::test_gauge_month_heading_format_now_matches_between_status_and_quota_pages` · **this pass's owner-journey test** (the live used-count string cross-checked as identical on `/` and `/quota`) | **CLOSED** |
| 6 | Current month silently absent from month-history table when quiet (`TEST-PORTAL-quota.md` F1, Low) | Low | `core/portal/quota.py:_render_month_panel` (synthesizes a zero row for the current month when missing but history exists) | `test_portal_quota_gaps.py::test_current_month_marker_present_when_current_month_has_zero_pushes_but_history_exists` (+ `..._brand_new_deployment_month_panel_has_no_synthesized_zero_row` proves the OTHER case is correctly unaffected) | **CLOSED** |
| 7 | Mid-fan-out failure vanishes from sent/skipped counts (`TEST-PORTAL-quota.md` F4, Low) | Low | `core/portal/quota.py:_run_digest_now` returns `(sent, skipped, failed, ran)` | `test_portal_quota_gaps.py::test_partial_mid_fanout_failure_is_now_counted_as_failed` | **CLOSED** |
| 8 | Luna's own English "panel unavailable" assertions never exercised the English branch (`TEST-PORTAL-status.md` Finding 2, test-hygiene only) | Info | Test-only; STATUS's own English assertion branches now match on an escape-safe substring (IMPL item 6) | Full STATUS suite green, no dead-assertion branch remains | **CLOSED** |

## Additional release-gate-only proofs (beyond the closure table)

**Security boundary, re-proven by ENUMERATION (not a maintained path list):**
- `test_portal_router_enumerated_403s_headerless_on_every_real_registered_route` — walks the REAL `core/portal/server.py:REGISTERED_MODULES` (all 4 modules + the vendored font route, confirmed present) and probes every GET/POST resource it produces: every single one 403s header-less, every 403 body is stylesheet-free, and the same route succeeds with the correct header (proves the 403 is a real gate, not an absent route). Probed 12 real routes.
- `test_line_webhook_router_enumerated_has_zero_portal_routes` — captures the REAL `web.Application` the public, Funnel-exposed LINE listener builds for itself (via a namespace-local proxy on `channels/line_webhook.py`'s own `web` import — the shared `aiohttp.web` module itself is never touched) and confirms it has **exactly** its own two routes (`/callback`, `/media/{tail}`), nothing portal-shaped.
- `test_spoofed_identity_header_via_the_public_line_port_cannot_reach_portal_handlers` — a spoofed `Tailscale-User-Login` POSTed to the PUBLIC port (8080-shaped) against a portal-shaped path (`/users/approve`) gets `404` (there is no route to reach — the two listeners are structurally separate `Application`s, no shared router) and performs no DB write.
- `/fonts/NotoSansThai-Regular.ttf` confirmed present in the enumerated route set and gated identically to every other portal route (tailnet-only).

**Migration 015, realistic seeded DB:** `test_migration_015_realistic_seeded_db_surgical_scrub_idempotent_schema_stamps_15` — one database, 3 users, a base numeric habit, a base text habit (`diary`), a custom text habit (via `user_habits` join), a custom numeric habit, and a decoy non-`undo` row, frozen at schema v14 via the REAL migration chain, then opened via `Database.__init__` (the real upgrade path a service restart takes). Verified: the 3 at-risk rows scrub to the exact `[text entry removed] (N chars)` marker with zero leaked substrings anywhere in the table; the 5 safe rows are byte-identical, untouched; a direct second invocation of the migration function is a no-op (idempotent); `schema_version == len(MIGRATIONS) == 15`.

**Telegram-mode regression, pinned:** `test_telegram_mode_never_gets_the_portal_regardless_of_enabled_startup_clean`, parametrized over `portal_enabled ∈ {False, True}` — the portal never binds on Telegram either way (R-SEC-1's dual gate), AND startup otherwise stays clean: the channel-agnostic scheduler jobs (`minutely_tick`) still register normally, while the LINE-only `daily_digest` job correctly never registers. Specced default confirmed: Telegram gets no portal, full stop, independent of the flag.

**Version consistency + release-notes posture:** `test_version_consistency_across_the_three_files_and_release_notes_posture_unchanged` — `VERSION`, `pyproject.toml`'s `[project].version`, and `habit_assistant.__version__` all read `"1.3.0+line"` and are asserted equal to each other (not just individually correct). `core/release_notes.py:RELEASE_NOTES` confirmed to carry no `"1.2.0"`/`"1.3.0"`/`"1.3.0+line"` key — this release deliberately does not self-announce (`SPEC-LINE-PORTAL.md` §9 OQ2's own conservative default), and that posture is unchanged by the integration pass.

## Remaining open items (non-blocking, documented, carried forward)

These were already known/documented at the module-Vera or integration level and are **not** regressions introduced by this pass. None gate the release.

- **STATUS Finding 3** (`core/portal/status.py` reads the wall clock directly rather than through this codebase's injectable-clock convention) — low severity, no test failure, not part of the 9-item integration worklist. Recommend a future pass promote it to the `clock=` convention `core/digest.py`/`channels/line.py` already use.
- **Migration 015's documented edge case**: a per-user custom TEXT habit that was hard-deleted before this release has no surviving `user_habits` row to join against, so its historical undo rows are not scrubbed. The built-in `diary` habit (the common case, and the one the MAJOR FINDING reproduced) is unconditionally covered. Documented in the migration's own docstring; not exercised as a false-negative by any test because it is, by definition, unreachable data.
- **F3's residual risk, disclosed by design**: an in-memory, single-process guard (`_DAILY_RUN_CLAIMED`) means a process restart mid-day clears the claim, allowing one further manual/scheduled run that same day. This mirrors `core/digest.py`'s own pre-existing `_DIGEST_DEFERRED_DATES` posture and is disclosed to the owner in the confirm-interstitial copy at trigger time (`UX.md` Flow D). Not a regression; an accepted architectural trade-off for a single-instance deployment.
- **`owner_login`/`public_url` left empty** in the shipped `config.toml.line` — an operator fill-in, not a code defect. The network boundary (`tailscale serve`, never `funnel`) plus `require_identity_header=true` (both still on by default) fully gate the portal even with these unfilled; `deploy/DEPLOY-LINE.md` recommends filling both in.

## Regressions detected

None. All 5524 tests in the full LINE-edition gate pass identically across all 3 required runs. The portal-only serial subset (340 tests, including this pass's new file) is 100% green.

## Recommendation

**PASS — release v1.3.0+line.** All 32 acceptance criteria in `SPEC-LINE-PORTAL.md` §8 are green at the wired level. Every finding escalated by the four module Veras is confirmed closed with reproducing evidence (diary-leak marker + migration scrub, both approve-flash outcomes, digest dedup in both orders, custom-habit units, formatter parity, and both LOW quota gaps). The security boundary holds under router-level enumeration on both listeners, including a cross-port spoofed-header probe. Migration 015 is proven surgical, idempotent, and correctly landed on a realistic multi-user database via the real upgrade path. The Telegram edition is unaffected regardless of the portal flag. Version strings agree across all three files, and this release's deliberate no-self-announce posture is unchanged. Three consecutive full-gate runs (2× parallel, 1× serial) all report 0 failed.

Nothing was committed by this pass, per the tester's mandate — the release commit, version tag, and any push are Archi's Phase 6.5 step.
