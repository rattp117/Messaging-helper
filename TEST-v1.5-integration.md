# Test Report — v1.5.0 integration (check-ins, DND, preparse, announcements — final release gate)

## Summary
- Scope: integration-owned ACs per SPEC-v1.5.md §11 — **AC-1, AC-2, AC-10, AC-11, AC-12, AC-16, AC-17, AC-18, AC-19, AC-24** — plus re-confirmation of all three parallel modules (`checkins`, `preparse`, `announce`) through the REAL wired `handle_inbound_message`/`on_message`/`on_callback`/`async_main` path.
- Total (`tests/test_v15_integration.py`): **32 tests** (12 Luna + **20 new, this pass**).
- Passed: 32 / 32 (file scope).
- Full repo suite: **2607 passed, 0 failed, 1 skipped, 1 xfailed** (baseline going in: 2587 passed / 0 failed / 1 skipped / 1 xfailed — independently re-confirmed before starting; delta = +20, exactly this pass's additions, reproduced on two consecutive full runs). The 1 xfail is the ruled-on `announce_release` TOCTOU race (`tests/test_announce_gaps.py::test_concurrent_overlapping_calls_send_at_most_once_per_user`, Archi's 2026-08-23 ruling) — left untouched, per instruction. The 1 skip is the same pre-existing conditional architectural-boundary check every prior pass has carried (`tests/test_channels.py:232`).
- Status: **PASS**.
- **RELEASE GATE VERDICT: PASS.** v1.5.0 is ready to ship.

## Test files
| Path | Tests added (this pass) | Covers |
|---|---|---|
| `tests/test_v15_integration.py` | 20 new (of 32 total) | AC-14/AC-15/AC-16 hardening, AC-3–AC-9/AC-13 re-confirmation, AC-10/AC-11/AC-12, AC-20–AC-24, AC-2/AC-17/AC-18/AC-19, AC-1, plus the coordinator's 8-point punch list below |

No production code was modified. All changes are additive to the existing `tests/test_v15_integration.py`: three new imports (`sqlite3`, `load_config`, `i18n`/`target_nl`/`Habit`/`TargetIntent`), two small test-only helpers (`_habit`, `_colliding_registry`, mirroring `tests/test_units.py`'s own convention), two shared-harness additions (`_ScriptedChannel.run_jobs_before_stop` — the same mechanism already used in `tests/test_v12_integration.py` — and a `_RecordingHealthMonitor`/counting-probe `_FakeOllamaClient`), and 20 new test functions/parametrizations. No test opens `data/habits.db` or touches the live Task Scheduler service; every DB is a scratch `tmp_path` SQLite file. Ran via `.venv\Scripts\python.exe -m pytest` (no `uv run`/`uv sync`); the venv needed no repair this session.

## Punch-list — per item

### 1. Pre-parser in production position
- **"500ml" → instant log, zero LLM calls, undo button, streak/milestone suffix** — already proven by Luna's 5 pre-existing tests (unchanged, re-verified green).
- **Target-override rendering** — `test_preparse_confirmation_reflects_a_target_override_not_the_config_default`: with `db.set_target(OWNER, "water", 3000.0)` in place, the preparse-path confirmation shows `500 / 3000`, never the config default `2500`. **PASS** — closes a real gap (no prior test exercised this combination through preparse).
- **Ambiguous text → LLM path unchanged; Ollama-down (simple logs work, complex text defers)** — already proven; re-verified green.
- **Deferral queue never captures preparsed messages** — `test_ollama_down_mixed_sequence_only_ambiguous_messages_enter_the_deferral_queue`: a 4-message mixed sequence (`"500ml"`, `"I drank some water just now"`, `"10min"`, `"another vague message here"`) during a simulated outage. Exactly the two ambiguous messages land in `db.pending_unparsed()`; both preparse hits log instantly and are never queued. **PASS.**
- **Preparse hit does NOT consume/disturb the NL-target gate** — `test_preparse_hit_does_not_disturb_a_subsequent_full_nl_target_message`: a preparse-hit log immediately followed by a full-NL target-setting message (`classify_target_intent` monkeypatched) — the second message still correctly reaches and completes the NL-target gate, writes no second `logs` row, and the target override lands correctly. **PASS** — confirms no shared mutable state leaks between the two paths across calls.

### 2. Check-ins live
- **`/checkin on` → fires at next top-of-hour inside window; DND suppresses; all-goals-met skips** — already proven by Luna's 3 pre-existing tests; re-verified green.
- **Opt-in default holds through real startup (nobody enrolled)** — `test_checkin_opt_in_default_holds_through_real_startup_nobody_enrolled`: a real `async_main` startup with 3 active users (owner included), nobody ever running `/checkin`; `db.get_checkin_window` is `None` for all three, and a top-of-hour tick squarely inside the config default window produces **zero** sends. **PASS.**
- **`/checkin` changes write audit rows (new vocabulary) and `/audit` renders them bilingually** — `test_checkin_setting_changes_write_audit_rows_rendered_bilingually_in_audit`: `/checkin on` → `off` → `default` writes exactly `checkin_set`/`checkin_off`/`checkin_default` (newest-first), and `/audit`'s reply contains the correct localized label for all three, in both languages. **PASS**, with one finding surfaced along the way (see below).

### 3. Announce at startup
- **Fresh version → active users get the note once, in their own language** — `test_announce_sends_each_user_their_own_language`: a Thai-preferring and an English-preferring active user each receive their own language's exact catalog text (`core.release_notes.get_release_note`), not just a substring match. **PASS.**
- **Pending/blocked nothing** — already proven; re-verified green.
- **Approve catch-up, mid-session and across two real startups** — `test_announce_newly_approved_user_mid_session_stays_caught_up_across_two_real_startups`: owner approves a brand-new chat during a real v1.5.0 startup session; the newcomer gets the ordinary `access_granted` welcome but NOT the release note (they weren't active when `announce_release` ran at the top of that same startup); their `last_announced_version` is immediately caught up (R-N5); a genuinely SECOND real `async_main` startup over the same persisted DB confirms nobody — owner or newcomer — receives anything a second time. **PASS.** (See "Notable findings" below for a real testing gotcha this surfaced and fixed.)
- **Second startup silent** — proven both by the pre-existing test and by the two-startup test above.
- **AC-24 complete (latest-version-only)** — `test_announce_user_several_versions_behind_gets_only_the_current_note_once`: a user several versions behind (`last_announced_version="1.2.0"`) receives **exactly one** send at startup, and it is precisely the current version's note — no rollup/backfill of intermediate versions. **PASS.**
- **Bonus — today's actual pinned state** — `test_current_pinned_version_has_no_release_note_and_announces_nothing_today`: with `src/habit_assistant/__init__.py:__version__` still at its real, un-bumped `"1.4.0"`, a real startup today announces nothing to anyone and does not crash (AC-22's own "no catalog entry" path, exercised with the app's actual current constant, not a synthetic one). Explicitly documented as a point-in-time check that will need updating the moment `__version__` is bumped at release — not a permanent AC pin.

### 4. DND matrix final state
- **Reminders** — unchanged since v1.2, not re-tested here (R-D1 explicitly "unchanged"); already covered by the existing v1.2 suite, which stays green.
- **Check-ins** — already proven (AC-6).
- **Daily summary + weekly review, per-user, through the REAL scheduled-job closures** — `test_daily_summary_and_weekly_review_honor_per_user_dnd_through_the_real_jobs`: added the same `run_jobs_before_stop` mechanism `tests/test_v12_integration.py` already established (invokes `_FakeScheduler`'s registered job closures directly, inside the still-open `db` connection). A member in permanent DND has both their daily summary and weekly review suppressed; the un-customized owner's summary and review still fire (AC-10/AC-11's own "byte-identical for un-customized users" clause). **PASS** — this is the first time these two jobs' DND behavior was proven through `main.py`'s actual closures in this test suite (the shared-surface's own `test_dnd_matrix.py` tests the mechanism directly, not the wired job).
- **Ops sends NOT suppressed** — `test_health_alert_and_access_request_notification_not_suppressed_by_owner_dnd`: with the owner in a permanent (`00:00-23:59`) DND window, (a) a direct `HealthMonitor._alert(...)` call still delivers to the owner, and (b) a stranger's first contact still triggers the real `access.handle_gate`'s `access_request` notification to the owner. **PASS.**
- **Announcements NOT suppressed** — already proven (`test_ac24_dnd_is_ignored_...`); re-verified green.

### 5. Unit-collision fix
- **Colliding-unit registry → preparse falls through to LLM** — `test_colliding_unit_registry_falls_through_to_the_llm_through_real_wiring`: a hand-built registry where `water` (alias `min`) and `stretch` (unit `min`) collide; `"10 min"` through the real `handle_inbound_message` reaches a static LLM fake (not silently misattributed to whichever habit registered first) and logs exactly what the LLM says. **PASS.**
- **Precision-first `/target` behavior, confirmed at the wired level** — `test_colliding_unit_target_set_rejects_with_usage_through_real_wiring`: `/target stretch 10min` against the same colliding registry returns a usage reply and leaves `db.get_target(OWNER, "stretch")` at `None` — locks in `IMPL-v1.5-integration.md`'s own documented behavior change (always-reject on a colliding unit, regardless of which habit is explicitly named). **PASS.**
- **No OTHER consumer changed silently** — `test_colliding_unit_edit_trigger_also_falls_through_consistently`: the edit-trigger path (`commands._parse_edit_value`, the other consumer of the shared `core/units.py`) rejects the same colliding token too — a pre-existing `stretch` log is left completely unchanged rather than being silently edited to a misattributed value. **PASS** — confirms both consumers of the shared unit-lookup function moved together, not just `/target`'s.

### 6. Health-probe config
- **300s default, flowing into the real `HealthMonitor` construction** — `test_health_interval_default_300_flows_into_healthmonitor_through_real_startup`: `Config().health.interval_seconds == 300.0`, and a real `async_main` startup constructs `HealthMonitor` (captured via a recording stand-in) with `interval_seconds=300.0`. **PASS.**
- **Live `config.toml`'s pinned 60s still works** — `test_live_config_toml_health_interval_is_pinned_to_60`: `load_config()` called genuinely unpatched, reading the actual deployed file — `interval_seconds == 60.0`, `probe_on_startup is True`, `checkin.enabled is False`. **PASS** — the live config file parses cleanly against the current schema.
- **`probe_on_startup` gate** — `test_probe_on_startup_gate` (parametrized True/False): with the gate `False`, `OllamaClient.probe_schema_support` is never called through a real startup; with it `True` (default), it is. **PASS.**

### 7. AC-M3-style regression
Full 1497→2607-cumulative-test-suite green is the broad proof. Narrowed with `test_preparse_confirmation_byte_identical_to_known_v1_2_era_string`: the exact string `"✅ 500 ml logged — today 500 / 2500 ml (20%)"` — pinned since `tests/test_v12_integration.py`'s own AC-M3 test — is byte-identical whether "500ml" resolves via the LLM (pre-v1.5) or via preparse (v1.5, this test). **PASS.**

### 8. Migration 008 rehearsal
`test_migration_008_rehearsal_on_a_v1_4_shaped_scratch_db`: hand-built a raw sqlite3 DB matching the exact v1.4-era (post-007) schema — `users`, `logs.user_id`, `habit_targets`, `user_reminder_times`, `audit_log`, `user_version=7`, no `checkin_window`/`last_announced_version` columns — with a real pre-existing owner water log and target override, at the exact path `async_main` opens. Ran the REAL startup (migration 008 + the existing attribution/prune/announce sequence) and drove two real messages. Verified: schema lands at version 8; both new nullable columns exist with no backfill (`last_announced_version` stays `None`); the pre-existing v1.4-era data is still fully readable through production code (`/habits` shows the legacy total); and a genuinely new-in-v1.5 write (`/checkin on`) succeeds post-upgrade and the pre-existing target override survived the migration intact. **PASS.**

## AC coverage (integration-owned, SPEC-v1.5.md §11)
| AC | Test(s) | Status |
|---|---|---|
| **AC-1** (migration 008, additive, no backfill) | `tests/test_migrations.py::test_v7_shaped_db_migrates_to_v8_*` (pre-existing) + `test_migration_008_rehearsal_on_a_v1_4_shaped_scratch_db` (this pass, through the real app) | **PASS** |
| **AC-2** (units extraction, byte-identical) | `tests/test_units.py`/`tests/test_commands.py` (pre-existing) + the two colliding-unit consistency tests (this pass) | **PASS** |
| **AC-10** (summary per-user DND) | `tests/test_dnd_matrix.py` (pre-existing, mechanism-level) + `test_daily_summary_and_weekly_review_honor_per_user_dnd_through_the_real_jobs` (this pass, wired-job-level) | **PASS** |
| **AC-11** (weekly-review DND) | Same as above | **PASS** |
| **AC-12** (ops not suppressed) | `test_health_alert_and_access_request_notification_not_suppressed_by_owner_dnd` (this pass) | **PASS** |
| **AC-16** (works Ollama-down) | Luna's 2 pre-existing tests + `test_ollama_down_mixed_sequence_only_ambiguous_messages_enter_the_deferral_queue` (this pass) | **PASS** |
| **AC-17** (health interval) | `test_health_interval_default_300_flows_into_healthmonitor_through_real_startup`, `test_live_config_toml_health_interval_is_pinned_to_60` (this pass) | **PASS** |
| **AC-18** (probe gate) | `test_probe_on_startup_gate` ×2 (this pass) | **PASS** |
| **AC-19** (LLM-min regression) | Full suite green + `test_preparse_confirmation_byte_identical_to_known_v1_2_era_string` (this pass) | **PASS** |
| **AC-24** (announce audience + DND + latest-only) | Luna's 3 pre-existing tests + the mid-session catch-up, several-versions-behind, and per-user-language tests (this pass) | **PASS** |

Every module-owned AC (AC-3–AC-9, AC-13, AC-14, AC-15, AC-20–AC-23) was independently re-confirmed through the real wiring at least once in this pass's additions, on top of each module's own extensive `TEST-v1.5-*.md` coverage (757 `preparse` tests, 127 `checkins` tests, 43 `announce` tests) — no gaps found beyond what's listed above.

## Failures
None. 32/32 in `tests/test_v15_integration.py`; 2607/2609 in the full repo suite (the 2 non-pass entries are the pre-existing, unrelated architectural-boundary *skip* and the Archi-ruled *xfail* — neither is a failure).

## Regressions detected
None. Full repo suite: 2607 passed / 0 failed / 1 skipped / 1 xfailed, up from the stated 2587/0/1/1 baseline by exactly this pass's 20 new tests, reproduced identically across two consecutive full runs.

## Notable findings during this pass (all resolved before this report, or explicitly flagged for the record)

1. **`/audit`'s reply language does not honor the asker's stored `/lang` preference** (pre-existing since v1.3, not touched or introduced by this pass). `main.py:on_message`'s own `lang = i18n.resolve_reply_language(text, config)` call (no `user_pref=`) is reused for the `"audit"` command-kind branch — unlike every other command reply, which routes through `handle_inbound_message`'s own properly-prefed `lang` resolution. Concretely: an owner who ran `/lang th` still gets an **English** `/audit` reply if they type the (all-ASCII) `"/audit"` trigger; only the Thai alias `ประวัติ` (which itself contains Thai characters) auto-detects Thai. Locked in explicitly by `test_checkin_setting_changes_write_audit_rows_rendered_bilingually_in_audit` (both directions verified, not silently worked around). **Not a v1.5 regression and not blocking** — recorded here so it isn't mistaken for new breakage in a future pass, and worth a design note to Archi/Sophia on whether `/audit` should thread the stored preference like `/habits`/`/help` do.
2. **A test-harness-only `__version__` desync**, not a production bug: `__version__` is imported via `from habit_assistant import __version__` independently in both `main.py` (the announce call site) and `core/access.py` (R-N5's newly-approved catch-up write) — each binds its own separate name at import time. Monkeypatching only `main_module.__version__` to simulate a post-release-bump state left `access.py`'s own copy at the real, un-bumped `"1.4.0"`, causing a newly-approved user's catch-up write to use the wrong version in my first draft. Fixed by patching both module-level bindings together in this file's `_run()` helper. In real production this can't happen — a real release bump edits the one source file (`src/habit_assistant/__init__.py`) that both modules import from at process startup, so both always see the same value. Recorded for future test authors simulating a version bump in this codebase.
3. Two of my own first-draft assertions were simply wrong about which language a given reply resolves to (a REPLY like `/habits`/`/audit` auto-detects from the ASCII inbound trigger text → English; an UNPROMPTED send like a daily summary/weekly review with no stored `/lang` resolves "auto" to `config.i18n.primary_language`, Thai by default) — the same class of gotcha `TEST-v1.2-integration.md` and `TEST-v1.3-integration.md` already documented once each. Both fixed before this report.

## Recommendation
**PASS — ready to ship. This gates the v1.5.0 release.**

All 10 integration-owned ACs (1, 2, 10, 11, 12, 16, 17, 18, 19, 24) pass through the REAL wired `handle_inbound_message`/`on_message`/`on_callback`/`async_main` path — not module-level direct calls. Every item on the coordinator's 8-point punch list was independently tested and passes, including the two highest-value correctness gates (the pre-parser's byte-identical confirmation and non-interference with the NL-target gate, and the completed DND suppression matrix verified through the actual scheduled-job closures rather than the mechanism alone). One pre-existing (not new) language-resolution quirk in `/audit` was found and locked in as a documented finding rather than silently worked around. Zero regressions across the full 2607-test suite, reproduced on two consecutive runs. No production code was touched by this pass. The one known, accepted issue in the whole v1.5.0 surface (`announce_release`'s concurrent-invocation TOCTOU race) remains `xfail(strict=False)` per Archi's own 2026-08-23 ruling, exactly as instructed — left untouched.
