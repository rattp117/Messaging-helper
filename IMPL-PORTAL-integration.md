# Implementation — Admin Web Portal, Final Integration (line/v1.3.0)

> Branch `line-version`, worktree-only. Closes the accumulated cross-module
> worklist Archi compiled from SPEC-LINE-PORTAL.md §11 integration order,
> all four IMPL-PORTAL-\*.md/TEST-PORTAL-\*.md reports, IMPL-LEDGER-CLOCK-FIX.md,
> and TEST-LEDGER-TRIAGE.md. Nine items, each cited by its own source
> report. Nothing committed — left for Archi.

## Files changed

| Path | Created/modified | Description |
|---|---|---|
| `src/habit_assistant/core/portal/server.py` | Modified | Registered `users.register`/`audit.register`/`quota.register` alongside `status.register` in `REGISTERED_MODULES` (item 1); added the "integration owns this list" ruling comment. |
| `src/habit_assistant/core/undo_ui.py` | Modified | Added `_redacted_text_marker()`; `send_undo_confirmation` now records `"[text entry removed] (N chars)"` instead of raw text for `habit_type == "text"` or `category == "diary"` rows (item 2, MAJOR). |
| `src/habit_assistant/storage/migrations.py` | Modified | Added `_migration_015_scrub_diary_undo_audit_rows` — this codebase's first data-touching migration; scrubs historical `audit_log.old_value` for `action='undo'` text-habit rows (item 2). |
| `src/habit_assistant/core/portal/audit.py` | Modified | `/activity`'s Value column now resolves units via a per-row `RegistryProvider` (`row["user_id"].for_user`) instead of one shared base-only registry — custom habits now show their unit (item 3). |
| `src/habit_assistant/channels/line.py` | Modified | `_push`/`_emit`/`send` now return a confirmation sentinel on an actual send, `None` on a silent quota-gate drop (previously `None` unconditionally) — the mechanism item 4 needed (Finding 1). |
| `src/habit_assistant/core/access.py` | Modified | `approve_user` returns `bool` (push confirmed); `execute_admin`'s approve/invite branch picks `admin_approved_ack` vs the new `admin_approved_ack_nopush` (item 4). |
| `src/habit_assistant/core/portal/users.py` | Modified | `handle_approve` picks `ok=approve` vs `ok=approve_nopush`; `_build_flash` renders `portal_flash_approve_nopush` for the latter (item 4). |
| `src/habit_assistant/core/i18n.py` | Modified | Added `portal_flash_approve_nopush` (UX.md §7 exact wording), `admin_approved_ack_nopush`, `portal_digest_result_with_failed` keys. |
| `src/habit_assistant/core/digest.py` | Modified | Added `claim_daily_digest_run`/`release_daily_digest_claim`/`daily_digest_run_claimed_at` (shared same-day guard) and `run_daily_digest_guarded` — both real call sites now use the guarded wrapper (item 5). |
| `src/habit_assistant/core/app.py` | Modified (1 line) | `_digest_job` calls `digest.run_daily_digest_guarded` instead of `run_daily_digest` — net zero new lines (line-count ceiling respected). |
| `src/habit_assistant/core/portal/quota.py` | Modified | Digest-run flow rewritten onto the shared guard (drops the local `_manual_digest_runs` dict); `_run_digest_now` returns `(sent, skipped, failed, ran)` — F4 honest failed count; `_render_month_panel` synthesizes a zero-row for a quiet current month — F1; gauge heading/percent now use `layout.format_month_heading`/`layout.format_pct` — F2. |
| `src/habit_assistant/core/portal/status.py` | Modified | Gauge heading/percent now call the shared `layout.format_month_heading`/`layout.format_pct` instead of a private `_format_pct` (F2, shared with quota.py). |
| `src/habit_assistant/core/portal/layout.py` | Modified | Added `format_pct`/`format_month_heading` (promoted from `status.py`, item 6/F2). |
| `config.toml.line` | Modified | `[portal] enabled = true` (was `false`) — this deployment's reference config now ships the feature turned on (item 7/8). |
| `deploy/setup.sh` | Unmodified this pass | Already prints `tailscale serve --bg` conditionally (pre-existing, shared-surface pass) — verified sufficient, no changes needed. |
| `docs/DEPLOY-LINE.md` | Modified | Added §9 step 4, "Verify the security boundary actually holds" — a concrete `curl -i http://127.0.0.1:8081/` check expecting `403` (item 7). |
| `VERSION`, `pyproject.toml`, `src/habit_assistant/__init__.py` | Modified | `1.2.0+line` → `1.3.0+line` (item 8). |
| `tests/conftest.py` | Modified | Added `_reset_daily_digest_claim` autouse fixture clearing `digest._DAILY_RUN_CLAIMED` — required globally once the shared guard sits on the real scheduled-job path, not just the portal-quota test files. |
| `tests/test_portal_integration.py` | Modified (large addition) | 5 new end-to-end tests through the REAL two-listener app: all 4 pages + a mutation route (header required), both approve-flash-honesty outcomes, the digest-run overlap guard, the diary-undo marker on chat+portal audit (item 9). |
| `tests/test_migrations.py` | Modified | +6 tests for migration 015 (scrub, custom-habit join, idempotency, non-undo/NULL untouched); `== 14` → `== 15` schema-version literals bumped throughout (52 lines, script-assisted). |
| `tests/test_audit_capture.py` | Modified | Flipped `test_undo_diary_records_text_value` to pin the redacted-marker contract; added the habit-type-unstamped companion test. |
| `tests/test_portal_audit_gaps.py` | Modified | Flipped the MAJOR-FINDING diary-leak test to drive the real `undo_ui.send_undo_confirmation` path and assert the leak is closed; flipped the custom-habit-unit gap test to assert the unit now renders. |
| `tests/test_portal_quota_gaps.py` | Modified | Flipped F1 (current-month marker), F2 (gauge parity), F3's own concurrent-double-push test, F4 (partial-failure accounting); updated fixtures/tests referencing the removed `_manual_digest_runs` to use the shared `digest._DAILY_RUN_CLAIMED`; renamed the interleaved-request test to match the tighter guard. |
| `tests/test_portal_quota.py` | Modified | `ran=` assertions updated to the 3-field `sent.skipped.failed` shape; fixture updated to the shared guard. |
| `tests/test_portal_status.py`, `tests/test_portal_status_gaps.py` | Modified | Fixed the two dead English-assertion branches (apostrophe-escaping, Vera Finding 2); flipped the percent-formatting-divergence test to assert parity. |
| `tests/test_portal_users.py`, `tests/test_portal_users_gaps.py` | Modified | `FakeChannel`/local channel doubles now return a non-None confirmation sentinel on success; flipped both Finding-1 tests to assert the honest flash. |
| `tests/test_access.py`, `tests/test_announce.py` | Modified | Local `FakeChannel`s now return a confirmation sentinel; added `approve_user` return-value assertions and a chat-side honest-ack test. |
| `tests/test_v12_access_gaps.py` | Modified | Added `admin_approved_ack_nopush` to the catalog-key-formats-cleanly parametrize list. |
| `tests/test_portal_deploy.py` | Modified | `enabled = false` → `enabled = true` assertion for `config.toml.line`; added the identity-header-verification-step coverage test. |
| `tests/test_line_channel.py` | Modified | Flipped `assert result is None` → `is not None` for a confirmed push (R-A6 contract note updated). |
| `tests/test_line_release_gate.py` | Modified | Flipped the double-fire "documented risk" test to assert the new shared-guard protection; bumped the version-consistency literal to `1.3.0+line`. |
| `tests/test_heatmap.py`, `tests/test_history.py`, `tests/test_commands.py`, `tests/test_multi_habit_integration.py`, `tests/test_routines.py`, `tests/test_v110_shared_surface.py`, `tests/test_v12_integration.py`, `tests/test_v13_integration.py`, `tests/test_v15_integration.py`, `tests/test_v16_integration.py`, `tests/test_v18_routines_gaps.py`, `tests/test_v19_shared_surface.py`, `tests/test_wrapped.py`, `tests/test_line_v12_integration.py` | Modified | `schema_version`/`len(MIGRATIONS)` literals `14` → `15` (migration 015 added) — script-assisted, verified no unrelated `14` literal (e.g. `max_days_back`) touched. |

## How it works

**Item 1 (registration precedent).** `REGISTERED_MODULES` now lists all four page modules; a comment on the list itself states the settled rule — integration alone owns this list, page modules never self-register (STATUS's earlier self-registration is the one grandfathered exception, not the model).

**Item 2 (diary leak, MAJOR).** The leak had two independent surfaces: the ongoing write site and the historical rows already on disk. `core/undo_ui.py:_redacted_text_marker` closes the write site — every future text-habit undo stores `"[text entry removed] (N chars)"`, never the content, matching the exact privacy boundary `db.recent_logs_metadata` already enforces at the SQL layer for `/activity`. `storage/migrations.py`'s migration 015 closes the historical surface — an `UPDATE ... WHERE action='undo' AND (entity='diary' OR a matching user_habits.type='text')`, idempotent (`NOT LIKE` guard) and defensive against a hand-built partial-schema test fixture missing `audit_log` entirely.

**Item 3 (custom-habit units).** `/activity` now builds a `RegistryProvider(deps.config, deps.db)` once per request and resolves each row's unit via `provider.for_user(row["user_id"])` instead of one shared base-only registry — a custom habit's log now shows its configured unit, cached per-user within the request.

**Item 4 (approve-flash honesty).** `LineChannel.send()`'s contract changed from "always `None`" to "a confirmation sentinel on an actual send, `None` on a silent realtime-quota drop" — LINE still has no *real* per-message id (R-A6's own historical note stays true in spirit), but the caller can now tell "confirmed" from "silently dropped" for the first time. `access.approve_user` surfaces this as a `bool` return; both the portal flash (`portal_flash_approve_nopush`) and the chat ack (`admin_approved_ack_nopush`) use it.

**Item 5 (digest dedup).** `core/digest.py` gained a module-level `_DAILY_RUN_CLAIMED` marker, consulted (not enforced inside `run_daily_digest` itself — that function's own "no internal dedup" contract and test stay unchanged) by a new wrapper, `run_daily_digest_guarded`. Both real call sites — `core/app.py`'s scheduled `daily_digest` job and `core/portal/quota.py`'s manual trigger — now go through the wrapper instead of calling `run_daily_digest` directly, so whichever runs first on a calendar day claims it and the other backs off honestly.

**Item 6 (LOWs).** F1: `_render_month_panel` synthesizes a `{yyyymm: current, total: 0}` row when the current month is otherwise absent from `db.monthly_push_history()` but prior months exist. F2: `layout.format_pct`/`format_month_heading` are now the ONE formatter both `status.py` and `quota.py`'s gauges call. F4: `_run_digest_now` now returns `(sent, skipped, failed, ran)`, `failed = max(0, goes_to - sent)` (0 in realtime mode, a correct no-op there); the portal reports `portal_digest_result_with_failed` when `failed > 0`. STATUS's dead-assertion branches now match on an escape-safe substring.

**Item 7 (deploy).** `deploy/setup.sh`'s conditional `tailscale serve --bg` printing was already complete from the shared-surface pass — verified, not touched. `docs/DEPLOY-LINE.md` gained a concrete verification step (a header-less `curl` against `127.0.0.1:8081` expecting `403`). `config.toml.line` now ships `[portal] enabled = true`.

**Item 8 (version).** `1.2.0+line` → `1.3.0+line` in all three files plus the two tests that pin the literal.

**Item 9 (integration tests).** `tests/test_portal_integration.py` gained 5 new tests driven through the REAL `core/app.py:async_main` wiring (real `LineChannel`, mocked httpx transport, real `PortalServer`, real `FakeScheduler` recording the real job registration) — every page + a mutation route with/without the identity header, both approve-flash outcomes (a per-recipient `fail_push_for` set on the mock LINE API simulates the outage), the digest-run overlap guard (manual trigger via real HTTP, then the real scheduled job's own `job.func()`), and the diary-undo marker rendering identically on the real chat `/audit` command and the real portal `/audit`+`/activity` routes.

## Smoke test done

```
python -c "import habit_assistant, habit_assistant.main, habit_assistant.core.app, ...
            habit_assistant.channels.line, habit_assistant.storage.migrations, ..."
  -> __version__ = 1.3.0+line
  -> IMPORT CLEAN

pytest -m "not telegram_only and not llm_only" -n auto -q
  -> 5515 passed, 4 skipped, 1 xfailed, 0 failed (95.26s)

pytest tests/test_portal_*.py -q   (serial, no -n auto — the exit bar's own spot-run)
  -> 331 passed, 0 failed (19.45s)
```

All three exit-bar requirements hold: full LINE gate → 0 failed; serial portal-subset spot-run → 0 failed; `python -c import` clean.

## Iteration log (bugs found and fixed during this pass, before handoff)

- **Frozen-clock test breakage**: `_run_digest_now` initially called `digest.run_daily_digest_guarded(...)` without threading `clock=`, so a test that monkeypatches `quota.datetime` (not `digest.datetime`) desynced the guard's own day computation from `quota.py`'s pre-check. Fixed by passing `clock=datetime.now` explicitly (resolved from `quota.py`'s own, possibly-patched `datetime` name) — documented inline so a future reader doesn't reintroduce it.
- **Realtime-mode false "failed" count**: F4's `failed = goes_to - sent` naively reported every digest-on candidate as "failed" when `config.digest.mode == "realtime"` (where `run_daily_digest` correctly no-ops, 0 sent by design). Fixed with an explicit realtime-mode branch returning `(0, skipped, 0, True)`.
- **Migration 015 crashed on synthetic partial-schema test fixtures**: four pre-existing migration-rehearsal tests (`test_v19_shared_surface.py`, `test_v18_routines_gaps.py`, `test_routines.py`, `test_v110_shared_surface.py`) hand-build a minimal "vN-shaped" DB (only the tables their own assertions need) and stamp `PRAGMA user_version` directly rather than replaying the real chain — `audit_log` was absent in their fixtures. Fixed with a defensive `sqlite_master` existence check before the `UPDATE`, a no-op in that case (a real sequential upgrade always has `audit_log` by migration 015, since migration 007 always runs first).
- **Global test-suite pollution from the new shared guard**: `digest._DAILY_RUN_CLAIMED` is now touched by any test exercising the real scheduled `daily_digest` job, not just the portal-quota test files that had their own local reset fixture. Five tests in `test_line_integration.py`/`test_line_release_gate.py` (outside my originally-scoped files) started failing under `-n auto` from cross-test leakage within a worker. Fixed with a new global `autouse` fixture in `tests/conftest.py`.
- **Character-count typos**: three hand-computed `len(...)` literals in new migration/audit-capture tests were off by one (counted "had a good day" as 13/15 instead of 14, "a private secret" as 17 instead of 16) — caught by the test run itself, fixed against `len()`'s actual output.
- **`test_digest_immediate_path_double_fire_sends_twice_documented_risk`**: this pre-existing test's own docstring explicitly anticipated this exact fix ("if this now sends only once... update this test to match") — flipped per its own instructions once the shared guard closed the gap it was pinning.

## Known limitations

- **Migration 015's "hard-deleted custom text habit" edge case** (documented in its own docstring): a per-user custom text habit that has since been hard-deleted has no surviving `user_habits` row to join against, so its historical undo rows are not scrubbed by the migration. The built-in `diary` habit (the common case, and the one Vera's finding reproduced) is unconditionally covered via the `entity = 'diary'` branch, which doesn't depend on any habit-definition row surviving.
- **F4's `failed` count is a conservative estimate, not a precise classifier** (documented in `_run_digest_now`'s own docstring): `run_daily_digest` has no per-user result channel back to its caller, so a candidate with genuinely "nothing to say today" (rare in practice) is indistinguishable from one whose send failed, and both count toward `failed`. Reporting an honest, fully-accounted-for total was judged better than the previous silent gap.
- **`owner_login`/`public_url` left empty in `config.toml.line`** — these need a real Tailscale identity/hostname only the operator has; `enabled = true` alone is safe (network boundary + `require_identity_header` still gate every route) but the deploy docs recommend filling both in.
- **Nothing committed** — left for Archi's Phase 6.5 (version bump/changelog already applied to the three version files per item 8's own instruction, but the release commit + tag is Archi's step).
