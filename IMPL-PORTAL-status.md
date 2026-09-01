# Implementation — Admin Web Portal, STATUS module

> Branch `line-version`. Consumes `SPEC-LINE-PORTAL.md` §4 R-STATUS-*/§8
> AC8-AC14 (module STATUS's own scope per §11), `UX.md` §3 Flow A / §4
> Screen 1 (Maya), `UI.md` §3.3/§3.4/§3.7/§3.20/§5 Screen 1 (Iris), and
> `IMPL-PORTAL-shared.md` (the shared surface this pass builds on:
> `core/portal/{server,security,layout,stats}.py`).

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/core/portal/status.py` | Created | `GET /` handler + renderers: tiles, verdict banner, needs-you block, scheduler/storage/quota-gauge/recent-errors panels, per-panel failure handling, `register(app, deps)`. |
| `tests/test_portal_status.py` | Created | 35 tests covering AC8-AC14, verdict precedence/rendering, needs-you conditionality, bilingual rendering, XSS escaping, and route wiring through the real identity gate. |
| `src/habit_assistant/core/i18n.py` | Modified (append-only) | Added the `portal_status_*` key block (~45 keys) at the end of `CATALOG`, after the `portal_relative_*` set USERS added (reused directly rather than duplicated). |
| `src/habit_assistant/core/portal/server.py` | Modified (2 lines) | Imported `status` and appended `status.register` to `REGISTERED_MODULES` — the sanctioned integration point `IMPL-PORTAL-shared.md` documents. No other line in this file touched. |

## How it works

`register(app, deps)` adds one `GET /` route whose handler (`_handle_status`) resolves the owner's language, then builds five sections in order: tiles (version/channel/Ollama/uptime/last-event — pure attribute reads, can't fail), the needs-you banner (conditional on `layout.pending_count(deps.db)` ≥ 1), and three data-bearing panels (scheduler, storage, quota gauge) each wrapped in its own `try`/`except` — a failure renders that one panel as "unavailable" (`layout.panel_or_unavailable`'s own fallback shape, reused as a local string since I also need the pass/fail *signal*, which that shared builder doesn't expose to its caller) and is recorded into a `panel_failures` list. The recent-errors panel reads the in-memory ring buffer the same way for symmetry, though it can't practically fail. `_compute_verdict` then combines dead scheduler jobs (`next_run_time is None`), the quota percentage, ring-buffer non-emptiness, and `panel_failures` into the verdict banner per UX's precedence table (stop > warn > ok; only the winning tier's causes are named, each linked — an in-page anchor for scheduler/storage/errors, `/quota` for anything quota-related). Every dynamic value passes through `layout.escape()` or a self-escaping shared builder (`tile`/`panel`/`dl`/`tag`/`td_cell`/`mono`/`empty`) before reaching the page.

## Smoke test done

- `python -c "from habit_assistant.core.portal import status"` — clean import, no circular-import issue with `server.py` (which imports `status` at module level; `status.py` only imports `PortalDeps` under `TYPE_CHECKING`).
- `python -c "from habit_assistant.core.portal.server import REGISTERED_MODULES; print(REGISTERED_MODULES)"` — confirms `status.register` is wired in.
- **Live end-to-end runs** (real `aiohttp` `TestClient`/`TestServer`, real `Database`, real files on disk, through the real `identity_gate`): three scripted scenarios, each written to disk and inspected —
  1. Dead scheduler job + real WAL/SHM sidecar files + one real backup file → `200`, `verdict stop` naming `daily_digest ไม่ได้ตั้งเวลาไว้`, storage panel shows real byte sizes and the backups `<details>` collapse.
  2. 85% quota in `digest` mode + a `WARNING` ring-buffer record, forced `en` → `200`, `<html lang="en">`, multi-cause `verdict warn` with a `<ul>` of two correctly-linked causes (`#errors`, `/quota`), gauge shows `85 / 100 (85%) · digest`.
  3. Fresh restart (`RuntimeStats()` defaults, empty ring buffer, one live job) → `verdict ok`, "No events since the service restarted" tile, empty-state errors panel with the "clears on every restart" note.
- `py_compile` on `status.py`, `server.py`, `i18n.py`, and the test file — clean.
- `pytest tests/test_portal_status.py` — **35 passed**.
- `pytest tests/test_portal_status.py tests/test_portal_server.py tests/test_portal_layout.py tests/test_portal_security.py tests/test_portal_stats.py tests/test_portal_db.py tests/test_portal_integration.py tests/test_config.py tests/test_access.py` (STATUS + the full shared surface) — **225 passed**, no regressions.
- `pytest tests/test_i18n.py` — **23 passed** (catalog consistency: every key has both `en`/`th`, matching placeholders).
- **Full LINE gate**: `pytest -m "not telegram_only and not llm_only" -n auto` → **5381 passed, 2 failed, 4 skipped, 1 xfailed** (104s). The 2 failures (`test_riders.py::test_exactly_five_call_sites_pass_disable_notification_ticks_plus_v19_jobs`, `test_refactor_s2_verify.py::test_independent_disable_notification_sweep_matches_test_riders_expectation`) are **pre-existing regression-guard tests that enumerate every `disable_notification=` call site in the codebase** — `core/portal/quota.py` (the QUOTA module, a different parallel Luna's track, landed mid-session) added one new call site these two tests don't yet expect. This is entirely outside STATUS's scope (`status.py` never calls `channel.send`) — **flagging, not fixing**, per the parallel-track boundary Archi set. All portal-prefixed test files (`test_portal_status.py` + the other three modules' own suites, now also present in the tree) pass cleanly in this same run.

## Maps to acceptance criteria

- **AC8** → `status.py:_render_tiles` (version/channel/Ollama tiles) — `test_ac8_shows_version_channel_and_ollama_off`, `test_ac8_ollama_on_when_enabled`.
- **AC9** → `status.py:_format_uptime` + `_render_tiles` — `test_ac9_uptime_derived_from_started_at`, `test_ac9_uptime_under_an_hour_omits_days_and_hours`.
- **AC10** → `status.py:_render_tiles` (`last_event_at is None` branch) + `_format_ago` — `test_ac10_no_events_shows_localized_empty_state`, `test_ac10_last_event_shows_relative_and_absolute`, `test_ac10_last_event_in_thai_uses_relative_ago_copy`.
- **AC11** → `status.py:_build_scheduler_body` — `test_ac11_lists_every_job_id_with_next_run_time`, `test_ac11_dead_job_renders_not_scheduled_tag_and_drives_stop_verdict`, `test_ac11_empty_scheduler_shows_empty_state_not_a_crash`.
- **AC12** → `status.py:_build_gauge` — `test_ac12_gauge_shows_used_cap_pct_and_mode_realtime`, `test_ac12_gauge_uses_warn_cap_in_digest_mode`, `test_ac12_gauge_warn_tier_at_80_percent`, `test_ac12_gauge_stop_tier_at_100_percent_drives_stop_verdict`, `test_ac12_gauge_read_failure_renders_unavailable_and_does_not_500`.
- **AC13** → `status.py:_build_storage_body` — `test_ac13_shows_db_size_media_size_and_backup_list`, `test_ac13_no_backups_shows_localized_fallback_not_a_timestamp`, `test_ac13_storage_read_failure_renders_unavailable_not_a_500`.
- **AC14** → `status.py:_build_errors_body` — `test_ac14_empty_ring_buffer_shows_localized_empty_state`, `test_ac14_populated_ring_buffer_renders_rows_and_drives_warn_verdict`, `test_ac14_at_capacity_shows_the_dropped_note`, `test_ac14_error_level_gets_stop_tag_warning_gets_warn_tag`.

Also covered, though not separately numbered ACs (UX.md/UI.md describe them as composing only AC8-AC14 data, "adding no new data source"):
- **Verdict banner** (3 states, precedence, single vs multi-cause rendering) → `status.py:_compute_verdict`/`_verdict_html` — `test_verdict_ok_when_nothing_is_wrong`, `test_verdict_stop_wins_over_warn_when_both_present`, `test_verdict_multi_cause_renders_a_ul_with_each_link`.
- **Needs-you banner** → `status.py:_render_needs_you` — `test_needs_you_absent_when_no_pending_users`, `test_needs_you_present_when_pending_users_exist`.
- **R-I18N-1/AC31** (bilingual, no hardcoded literals) → every string in `status.py` routes through `i18n.t()` — `test_page_renders_english_when_forced`, `test_page_renders_thai_by_default`.
- **XSS discipline** (UI.md §9.2 contract 14) → `test_hostile_job_id_is_escaped_everywhere_it_appears`, `test_hostile_log_message_is_escaped`.
- **R-INT-1 wiring** → `test_status_registered_via_the_real_registered_modules_list`, `test_status_route_requires_identity_header`.

## Known limitations

- **"Ollama" label and its on/off value are left untranslated** in both languages. Iris's own Thai-rendered Screen 1 sample in `UI.md` (§5, ~line 595) keeps `Ollama<b>off</b>` literally unchanged even in the Thai render, so I followed her example rather than inventing a translation she didn't give. Flagging for Vera/Iris to confirm this reading is intended.
- **`_unavailable_body`** duplicates (as a plain string, ~4 lines) the exact fallback markup `layout.panel_or_unavailable` already builds internally, reusing the same `portal_panel_unavailable`/`portal_panel_unavailable_hint` shared keys. This was necessary because this module needs the pass/fail *signal* (to feed the verdict computation) that `panel_or_unavailable` doesn't expose to its caller — only the rendered HTML. I did not modify `layout.py` to add that signal (out of my sanctioned scope for shared files); if a second module needs the same signal, promoting this to a shared `layout.py` helper would be a reasonable follow-up for Archi/Irine to consider.
- **Scheduler-job read (`deps.scheduler.get_jobs()`) and dead-job detection happen together**, inside one `try`/`except`, rather than being decomposed further — if `get_jobs()` itself raises, `dead_jobs` is empty (not partially populated), which is the correct, simple behavior; the panel just shows "unavailable" and contributes a generic warn cause instead of a specific dead-job stop cause.
- **Byte-size and uptime formatting are this module's own small helpers** (`_format_bytes`, `_format_uptime`) — no existing shared formatter for either exists elsewhere in the codebase to reuse (confirmed via search).
- **Backup/media directory sizing is non-recursive** (top-level files only), matching the existing convention in `channels/line_webhook.py:cleanup_expired_media` (a flat `*.png`-per-token layout, no subdirectories).
- **Two pre-existing, unrelated test failures** noted above (from `core/portal/quota.py`, a different parallel track) — not investigated or touched, per the parallel-track file-ownership boundary.
- Nothing committed — left for Archi, per the standard workflow.
