# Test Report — Admin Web Portal, STATUS module (AC8–AC14)

> Scope per Archi's dispatch: verify `core/portal/status.py` (`GET /`) against
> `SPEC-LINE-PORTAL.md` AC8–AC14, `UX.md` §3 Flow A / §4 Screen 1 (verdict
> derivation table), `UI.md` §3.3/§3.4/§3.7/§3.20/§5, and Luna's
> `IMPL-PORTAL-status.md`. Priority: probe the VERDICT logic hardest — a
> wrong "healthy" is the worst failure mode for a status page. No
> production code was touched; only tests were written/run.

## Summary

- New adversarial file: `tests/test_portal_status_gaps.py` — **37 tests**, all passing.
- Existing Luna suite: `tests/test_portal_status.py` — **35 tests**, all passing (re-run, unmodified).
- STATUS + shared-surface regression bundle (both files above + `test_portal_server/layout/security/stats/db/integration.py`, `test_config.py`, `test_access.py`, `test_i18n.py`): **285 passed, 0 failed**.
- Full LINE-edition gate (`pytest -m "not telegram_only and not llm_only"`, 5,627 collected): **5,469 passed, 1 failed, 4 skipped, 1 xfailed** in 255.9s. The 1 failure is in `tests/test_portal_audit_gaps.py` (AUDIT track's own adversarial suite, a different Vera pass, flagging a diary-text privacy leak via undo `old_value`) — **not STATUS's scope, not touched, not caused by anything in this pass**. Zero failures anywhere in the STATUS-relevant subset.
- **False-healthy hunt: no false-healthy state found.** Every single-cause state, every worst-of combination, and a simultaneous 4-panel failure all correctly render `verdict warn`/`verdict stop` — never a silent `verdict ok` when something is actually broken. Detail below.
- **Status: PASS** for module STATUS (AC8–AC14). Two cross-cutting findings escalated (non-blocking) — see Findings.

## Test files

| Path | Tests | Covers |
|---|---|---|
| `tests/test_portal_status_gaps.py` (new, this pass) | 37 | Verdict truthfulness matrix (every single-cause + worst-of state from UX.md's table), false-healthy hunt incl. a simultaneous 4-panel failure, per-panel degradation with the other 3 panels proven intact, quota-gauge boundary precision (79/80/99/100/105%), quota↔status structural parity, identity-gate zero-leak on `/`, XSS on ring-buffer/logger-name/backup-filename content, relative-time bucket boundaries, bilingual empty-state combinations |
| `tests/test_portal_status.py` (Luna, re-run unmodified) | 35 | AC8–AC14 base coverage, dead-job/read-failure per-panel handling, needs-you banner, i18n, XSS on job-id/log-message, route wiring |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC8 | version/channel/Ollama tiles | Luna: `test_ac8_shows_version_channel_and_ollama_off`, `test_ac8_ollama_on_when_enabled` | PASS |
| AC9 | uptime from `started_at` | Luna: `test_ac9_uptime_derived_from_started_at`, `test_ac9_uptime_under_an_hour_omits_days_and_hours`. New: `test_uptime_future_started_at_clamps_to_zero_not_negative_or_crash` (clock-skew edge) | PASS |
| AC10 | last-webhook event, relative+absolute, "no events" empty state | Luna: 3 tests. New: 6 relative-time bucket-boundary tests (`test_relative_time_*`, 30s/65s/3550s/3700s/85000s/90000s) + `test_false_healthy_stale_last_webhook_event_does_not_drive_verdict` (30-day-stale pin) | PASS |
| AC11 | scheduler jobs listed, dead-job marker | Luna: 3 tests. New: `test_matrix_dead_scheduler_job_alone_is_stop`, `test_matrix_scheduler_read_exception_alone_is_warn_not_lost` (gap: a raising `get_jobs()`, not just a dead job, is a different failure class the spec names explicitly), `test_precedence_two_stop_causes_named_together`, `test_degradation_scheduler_fails_*` | PASS |
| AC12 | quota gauge used/cap/pct/mode, 3 tiers | Luna: 5 tests. New: 79/80/99/100/105% boundary tests, `test_gauge_db_failure_shows_unavailable_not_a_fake_zero` (fail-closed pin), 2 quota↔status parity tests, `test_degradation_gauge_fails_*` | PASS |
| AC13 | DB/media/backup sizes, backup list | Luna: 3 tests. New: `test_degradation_storage_fails_*`, `test_false_healthy_missing_backups_does_not_drive_verdict`, `test_hostile_backup_filename_windows_legal_chars_is_escaped` | PASS |
| AC14 | recent-errors ring buffer, 3 states | Luna: 4 tests. New: `test_matrix_errors_panel_records_exception_alone_is_warn_not_lost`, `test_matrix_errors_panel_len_exception_alone_is_warn_not_lost` (gap: a broken READ vs. a genuinely empty buffer — the exact false-healthy shape requested), `test_degradation_errors_fails_*`, `test_realistic_exception_message_with_embedded_hostile_user_text_is_escaped`, `test_hostile_logger_name_is_escaped` | PASS |
| Verdict banner (composes AC8–14, no new AC) | 3-state precedence, multi-cause | Luna: 3 tests. New: 7 tests — full single-cause matrix, 2-stop multi-cause, stop-hides-warn, 3-warn multi-cause, kitchen-sink 4-cause | PASS |

**Cross-cutting checks exercised against the STATUS route specifically** (owned by the shared surface, not AC8–14, but verified here per the dispatch note):
- Identity gate on `/`: header-less request and wrong-`owner_login` request both `403`, body byte-identical to `security.FORBIDDEN_BODY`, zero data leak (no version, no verdict, no brand, no nav) — `test_headerless_get_status_is_403_with_zero_data_leak`, `test_wrong_owner_login_get_status_is_403_with_zero_data_leak`. PASS.
- Bilingual, full-page, all-empty-states-simultaneously (fresh install) in both EN and TH — `test_fresh_install_all_empty_states_together_english/thai`. PASS.

## The false-healthy hunt — result

Enumerated the complete UX.md "verdict, precisely" table and probed every cell, plus combinations UX.md's table doesn't explicitly draw but the underlying code path allows:

1. **Every single-cause state** (dead job, quota=80%, quota=100%, ring-buffer non-empty) renders the correct tier alone. PASS.
2. **A panel read that *raises* (not just returns a bad value)** — for all 4 data-bearing panels (scheduler, storage, gauge, errors) — correctly demotes to `verdict warn` and is never lost. This was the actual gap in Luna's own suite: she tests a *dead job* (`next_run_time=None`) and a *raising DB call* for the gauge/storage, but never a raising `scheduler.get_jobs()` or a raising `ring.records()`/`len(ring)`. Both new failure classes were probed and both correctly surface as warn causes (`test_matrix_scheduler_read_exception_alone_is_warn_not_lost`, `test_matrix_errors_panel_records_exception_alone_is_warn_not_lost`, `test_matrix_errors_panel_len_exception_alone_is_warn_not_lost`). No false-healthy.
3. **Worst-of precedence**: stop always wins over warn, and only the winning tier's causes are named in the banner (the lower-severity cause stays visible in its own panel, confirmed via `test_precedence_stop_hides_warn_cause_when_quota_stopped_and_ring_nonempty`). Multi-cause counts are correct at n=2 and n=3. No mis-precedence found.
4. **Kitchen-sink**: all four data sources (scheduler, storage, gauge, errors) made to fail *simultaneously* via independent mocks. Result: `200` (never a 500), `verdict warn`, **"4 things to check"**, all 4 anchors present, `"read this right now."` appears exactly 4 times. No cause was silently dropped even under total degradation (`test_false_healthy_kitchen_sink_all_four_panels_fail_at_once`).
5. **Per-panel isolation**: for each of the 4 panels failing *alone*, the other 3 panels render their **real, non-degraded content** on the same page (a live job id, a real backup filename, a real quota line, the real empty-errors state) — not just "the page didn't 500" (`test_degradation_*_fails_*`, 4 tests).
6. **Documented non-drivers, pinned as regression guards**: a 30-day-stale last-webhook-event and a fresh install with zero backups ever taken both still render `verdict ok` — matching UX.md's explicit "does NOT drive the verdict" list (last-webhook staleness) and the verdict table's own closed set of triggers (backups aren't in it). Neither an over-eager nor a lazy implementation slipped through here.
7. **Quota boundary truthfulness**: 79%→ok, 80%→warn (inclusive boundary), 99%→warn, 100%→stop (inclusive boundary), and — the one place a status page could most plausibly lie — **over 100% (105%) still shows the real, uncapped "105 / 100 (105%)" text**; only the decorative bar clamps visually at 100%. Confirmed both are true simultaneously, not one masking the other.

**Conclusion: zero false-healthy states found in `core/portal/status.py`.** The per-panel `try`/`except` → `panel_failures.append(...)` → `_compute_verdict` pipeline is sound under every combination tested, including simultaneous total failure of all four data sources.

## Findings (non-blocking, escalation candidates)

### 1. Quota-gauge percent formatting diverges between `/` and `/quota` on round numbers — cross-track
`status.py:_format_pct` trims a trailing `.0` (matching `SPEC-LINE-PORTAL.md` §3.2's own raw example `"(1.2%)"` and `UI.md` §3.7's `"(87%)"`), but `core/portal/quota.py:_render_gauge` formats inline as `f"{snap.pct:.1f}"` and never trims. At a round percentage the two pages render **different strings for the identical underlying data** — e.g. Status shows `"80 / 100 (80%)"`, Quota shows `"80 / 100 (80.0%)"` for the exact same push count/cap. Reproduced and pinned in `test_status_and_quota_percent_formatting_diverge_on_round_numbers`. This is **not a STATUS AC12 violation** — `status.py`'s own output matches the spec's literal example — the inconsistency lives in `quota.py`'s own formatting choice. Recommend routing to the QUOTA track (reuse `status.py`'s `_format_pct`, or promote it to `layout.py`).

### 2. Luna's own English "panel unavailable" assertions never actually exercise the English string
`layout.escape()` correctly HTML-escapes an apostrophe (`'` → `&#x27;`), so the catalog string `"Can't read this right now."` renders as `Can&#x27;t read this right now.`. Luna's `test_ac12_gauge_read_failure_renders_unavailable_and_does_not_500` and `test_ac13_storage_read_failure_renders_unavailable_not_a_500` assert `"Can't read this right now." in body or "อ่านข้อมูลส่วนนี้ไม่ได้ตอนนี้" in body` — but both tests use the *default (Thai)* language, so they only ever pass via the Thai branch; the English literal on the left can never match due to the escaping, leaving the English rendering of this exact string effectively unverified in her suite. **Not a production bug** (the escaping is correct and required); it's a dead assertion branch. My own equivalent tests use the escape-safe substring `"read this right now."` throughout. Flagging so Luna can tighten hers if she revisits this file.

### 3. `status.py` reads the wall clock directly rather than through the codebase's injectable-clock convention
`core/timeutil.py`'s own docstring states the established pattern ("this app's usual injectable-clock shape, e.g. `datetime.now` or a test's fixed callable"), and `layout.format_as_of` follows it (`clock: Callable[[], datetime] = datetime.now`). `status.py:_handle_status` instead calls `datetime.now()` inline and threads the result through as a plain `now` parameter. Functionally this is safe — every read within one request shares that single `now`, so there's no intra-request drift — but it means the module can't be given a fixed clock in a test without monkeypatching `status.datetime`, and it's a quiet deviation from an established codebase convention. Low severity; no test failed because of it (the existing/new boundary tests all use generous real-clock margins). Worth a note for whoever next touches this file.

### 4. AUDIT track has one failing adversarial test (observed, not investigated — out of scope)
The full LINE-edition gate surfaced `tests/test_portal_audit_gaps.py::test_audit_detail_cell_leaks_diary_text_via_undo_old_value_MAJOR_FINDING` failing — a different Vera pass's own adversarial suite for the AUDIT module, apparently flagging a genuine privacy leak (diary text via an undo's `old_value`). This is unrelated to STATUS, was not investigated further, and is already self-documented by its own test name. Flagging only so Archi doesn't miss it when collecting track reports.

## Regressions detected

None. Full LINE-edition gate: 5,469 passed / 1 failed (AUDIT-track, unrelated, see Finding 4) / 4 skipped / 1 xfailed, 255.9s. STATUS-relevant subset (285 tests across STATUS + shared surface): 0 failures.

## Recommendation

**Ready to ship** — module STATUS (AC8–AC14) passes all acceptance criteria and the dedicated false-healthy hunt found no wrong-"healthy" state under any single-cause, worst-of, or simultaneous-total-failure combination tested. Two non-blocking items for Archi to route: **escalate Finding 1** (quota % formatting divergence) to whoever owns the QUOTA track, and **note Finding 4** (AUDIT-track's own failing privacy test) when collecting that track's report — neither blocks STATUS.
