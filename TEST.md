# Test Report — Local Habit-Tracking Assistant (MVP)

## Summary
- Total: 133 tests
- Passed: 132
- Failed: 0
- Skipped: 1 (intentional — a parametrized sanity-check variant that only applies to `core/`, see below)
- Status: **PASS**

Full suite run: `uv run pytest -q` → `132 passed, 1 skipped in ~3s`, on Windows, fully offline/mocked (no network required to pass the suite). Supplementary live checks against the reachable `mac-mini:11434` Ollama server and `api.telegram.org` were run separately (not pytest dependencies) — see "Live spot-checks" below.

**Iteration note:** the first pass found one gap (AC9 — `--test-reminder` crashed with a raw traceback on a Telegram HTTP error instead of failing cleanly). Luna fixed it in `src/habit_assistant/main.py`; the fix was independently re-verified — see "Verification of Luna's fix" below. All 11 ACs are now green.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_parser.py` | 33 | AC4, AC5, AC11 |
| `tests/test_db.py` | 22 | AC2, AC8 (aggregation math) |
| `tests/test_config.py` | 8 | AC1 |
| `tests/test_channels.py` | 12 | AC3 |
| `tests/test_confirmations.py` | 26 | AC6 |
| `tests/test_reminders.py` | 8 | AC7 |
| `tests/test_review.py` | 7 | AC8 (narrative + delivery) |
| `tests/test_cli.py` | 9 | AC9 |
| `tests/test_deliverables.py` | 8 | AC10 |
| `tests/conftest.py` | — | shared fixture: restores root-logger state after tests that call `main.async_main` (it calls `logging.basicConfig(force=True)`, which otherwise leaks a stale stdout-bound handler into later tests) |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC1 | Typed config from `config.toml` + `.env`; missing token → clear error | `tests/test_config.py` (8 tests: toml load, defaults on missing file, malformed toml, invalid values, missing/partial secrets, error message content, `ConfigError` is `RuntimeError`) | **PASS** |
| AC2 | SQLite schema + index on first run, WAL mode; insert/query round-trip | `tests/test_db.py` (schema columns, index existence, WAL mode, idempotent reopen, insert/query round-trips for water/diary, `NOT NULL raw_message` constraint, day-boundary correctness for `water_total_ml`/`stretch_count`/`diary_count`, `logs_between` inclusive bounds + ordering) | **PASS** |
| AC3 | `Channel` ABC; `TelegramChannel` send/run; no `core/`/`storage/` imports a concrete channel | `tests/test_channels.py` (ABC is abstract, `TelegramChannel` isa `Channel`, `build_send_request` shape, `send` via mocked transport + HTTP-error propagation, `run` long-poll: on_message per update + offset advance, skips textless updates, on_message exceptions don't crash the loop; AST-based seam scan of every `.py` in `core/` and `storage/`) | **PASS** |
| AC4 | Parser: `POST /api/chat`, `stream:false`, JSON-schema `format`; validates §7 schema; strips `<think>`/prose; fails closed to `unknown`, never crashes | `tests/test_parser.py` (request shape assertion incl. `format==EXTRACTION_JSON_SCHEMA`/`think:false`; think-block + prose stripping incl. end-to-end; malformed JSON, connection error, HTTP 500, invalid category enum, missing keys, extra keys, non-numeric/zero/negative `water_ml`/`stretch_min`, empty `diary_text`, non-numeric confidence — all fail closed without raising) | **PASS** |
| AC5 | Bilingual normalization: Thai glass/bottle + English + explicit ml, constants configurable | `tests/test_parser.py` (mocked: water/glass-Thai/bottle/explicit-ml/stretch/diary cases; a request-inspection test proving custom `glass_ml`/`bottle_ml` values reach the system prompt) **+ live spot-check** (see below) | **PASS** |
| AC6 | Confirmations verbatim per §6 (running water total/%, stretch ordinal, diary reflection); `unknown` → clarifying question, no DB row | `tests/test_confirmations.py` (exact string match for water/stretch/diary/unknown incl. running-total accumulation, configurable goal, ordinal helper parametrized 1st/2nd/3rd/4th/11th–13th/21st–23rd/101st/111th, diary reflection fallback, DB-write correctness per category, unknown writes zero rows even interleaved with valid logs, `--dry-run` writes nothing, one true end-to-end test through the real parser with a mocked Ollama transport) | **PASS** |
| AC7 | Reminders fire at configured times via `AsyncIOScheduler`; weekly review Sunday 20:00; all times from `config.toml` | `tests/test_reminders.py` (`send_reminder` text-per-category + invalid-category `ValueError`; job count/ids from default + custom config; cron hour/minute/timezone fields match config; job `args` bind the right channel+category; `replace_existing` doesn't duplicate once the scheduler is started; **one integration test drives the real `async_main`** with `AsyncIOScheduler`/`TelegramChannel` mocked out, confirming the weekly-review cron job is registered with `day_of_week`/`hour`/`minute` taken from `config.toml`, alongside the per-category reminder jobs) | **PASS** |
| AC8 | Weekly review aggregates 7 days (adherence %, totals/avg, stretch streak, diary count) and sends a narrative via the channel | `tests/test_db.py` (`compute_weekly_stats`/`format_stats_summary` against a deterministic 7-day seed: per-day %, weekly total/avg, stretch total, **trailing streak correctness** — including a day that breaks the streak mid-week — diary count, empty-week all-zero case, custom goal) + `tests/test_review.py` (`run_weekly_review` composes stats+narrative, falls back to a plain stats block when the LLM returns `None`/`""`, passes the stats summary into the LLM prompt, system prompt encodes the "no medical advice" constraint, the returned text is exactly what a `Channel.send` call would push) | **PASS** |
| AC9 | CLI flags `--test-reminder <cat>`, `--seed`, `--dry-run` work | `tests/test_cli.py` (argparse shape/defaults/choices; `--seed` run as a real offline subprocess with `cwd=tmp_path` — inserts rows, only water/stretch/diary categories; `--dry-run` driven through `async_main` with a mocked Ollama client — prints structured output, writes zero DB rows; `--test-reminder` driven through `async_main` with a mocked `TelegramChannel` — sends the right reminder text; `--test-reminder` on an HTTP failure now exits(1) cleanly (fixed, re-verified — see below); missing-secrets path exits(1) with a clear stderr message) **+ live spot-checks** (see below) | **PASS** (fixed; was FAIL in the first pass) |
| AC10 | `.env.example`, `.gitignore` (excludes `.env`, `data/`), README (macOS setup), `.plist` exist | `tests/test_deliverables.py` (file existence + content checks for all of the above, plus `pyproject.toml` pytest wiring and the `LineChannel` stub's webhook documentation) | **PASS** |
| AC11 | Parser tests (mocked Ollama) + DB tests pass on Windows dev box | Full suite run via `uv run pytest -q` on this Windows box, `.venv` created with `uv venv --python 3.12` per the environment brief | **PASS** (132/133, 1 intentional skip) |

## Failures (if any)

None currently. One was found and fixed during this test round — see "Verification of Luna's fix" below for the full before/after detail (kept for the record rather than deleted).

## Regressions detected

None. Re-running the full suite after Luna's fix shows the same 132 tests passing as before (131 that were already green, plus the 1 that was failing), with the same 1 intentional skip — no previously-passing test broke.

## Verification of Luna's fix

**Original finding (first pass):** `test_test_reminder_flag_fails_cleanly_on_401_not_crash` (`tests/test_cli.py`) — `--test-reminder` on a Telegram HTTP failure (e.g. 401 from a bad token) raised an unhandled `httpx.HTTPStatusError` out of `async_main` instead of failing cleanly, unlike the existing `ConfigError` pattern.

**Luna's fix, independently reviewed** (`src/habit_assistant/main.py`):
- `import httpx` added at module level (line 16).
- The `args.test_reminder` branch (lines 177–189) now wraps `await send_reminder(channel, args.test_reminder)` in:
  ```python
  try:
      await send_reminder(channel, args.test_reminder)
  except httpx.HTTPError as exc:
      print(f"ERROR: Failed to send test reminder: {exc}", file=sys.stderr)
      await channel.aclose()
      await llm.aclose()
      db.close()
      sys.exit(1)
  await channel.aclose()
  await llm.aclose()
  db.close()
  return
  ```
- This matches the pattern recommended in the prior version of this report: mirrors the `except ConfigError as exc: print(...); sys.exit(1)` handling already used for the two `load_config()`/`load_secrets()` branches, catches the broader `httpx.HTTPError` (covers connection failures too, not just 4xx/5xx status), and correctly still tears down `channel`/`llm`/`db` before exiting on the error path.
- Everything else in `main.py` is byte-identical to what was reviewed in the first pass — confirmed via file mtimes (`main.py` last modified 17:32:24, after every file under `tests/`, so nothing in `tests/` was touched by this change) and a full re-read of the file end to end. No test file was modified to make this pass.

**Independent re-verification performed:**
1. Read the full diff area of `main.py` (imports + the `--test-reminder` branch) and confirmed no other code paths changed.
2. Ran the full suite fresh: `uv run pytest -q` → `132 passed, 1 skipped in 3.03s`, 0 failed.
3. Ran the specific previously-failing test in isolation: `uv run pytest -q tests/test_cli.py::test_test_reminder_flag_fails_cleanly_on_401_not_crash -v` → `1 passed`.

---

# Test Report — v0.2.0 (Extraction Reliability & Model Fallback)

## Summary
- Total: 160 tests (132 v0.1.0 + 28 new)
- Passed: 160
- Failed: 0
- Skipped: 1 (same intentional skip as v0.1.0, unaffected)
- Status: **PASS**

Full suite run: `.venv\Scripts\python.exe -m pytest -q` → `160 passed, 1 skipped in 5.63s`, on Windows, fully offline/mocked (no network required to pass the suite). Baseline re-run of the untouched v0.1.0 suite (before adding any v0.2.0 test) confirmed `132 passed, 1 skipped`, matching Luna's reported baseline exactly — so all 28 new tests are net-additive, zero regressions. Supplementary live spot-checks against the reachable `mac-mini:11434` Ollama server were run separately (not pytest dependencies, not part of the pass/fail gate) — see "Live spot-checks" below. Per the live-environment rules for this round, the production bot process, `data/habits.db`, `.env`, and Telegram were left untouched throughout — no real Telegram calls were made by any test or spot-check.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_fallback.py` (new) | 28 | AC2.1, AC2.2, AC2.3, AC2.4, AC2.5 |
| `tests/test_confirmations.py` (fixture audit only, no new tests) | 0 | — (see "Fixture audit" below) |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC2.1 | Startup probe checks each configured model once, logs `format` conformance (exact-keys), never crashes on probe failure | `tests/test_fallback.py`: `test_probe_reports_conformant_model_true`, `test_probe_reports_non_conformant_model_false`, `test_probe_extra_key_is_non_conformant`, `test_probe_missing_key_is_non_conformant`, `test_probe_unreachable_model_reports_false_and_does_not_raise`, `test_probe_http_error_status_reports_false_and_does_not_raise`, `test_probe_checks_every_configured_model_independently`, `test_probe_unexpected_exception_does_not_raise` (8 tests: conformant / off-schema / extra-key / missing-key / unreachable / HTTP-500 / multi-model independence / non-httpx exception — probe never raises in any case) | **PASS** |
| AC2.2 | `chat_json` tries models in order; first schema-valid result wins; all off-schema → `unknown` | `tests/test_fallback.py`: `test_fallback_first_model_off_schema_second_model_wins` (request-order asserted via captured requests), `test_fallback_first_model_unparseable_json_second_model_wins`, `test_fallback_all_models_off_schema_yields_unknown`, `test_fallback_first_model_valid_second_model_never_called` (first-match-wins: model-b raises `AssertionError` if called at all) | **PASS** |
| AC2.3 | Valid extraction with `confidence < threshold` → clarifying question, no row written; mockable, deterministic | `tests/test_fallback.py`: `test_below_threshold_confidence_yields_clarifying_question_and_no_row`, `test_at_threshold_confidence_is_logged` (boundary: `==` threshold passes, confirming `<` not `<=`), `test_above_threshold_confidence_is_logged`, `test_custom_threshold_from_config_is_honored` (non-default threshold from `Config` actually changes the outcome), `test_below_threshold_diary_also_clarifies_and_writes_no_row` (cross-category) — all driven through the real `handle_inbound_message` handler path against a real on-disk `Database`, asserting both the channel message AND `db.logs_between(...)` row count | **PASS** |
| AC2.4 | Single-model config (`models = ["x"]` or bare-string `model`) behaves identically to v0.1.0 for schema-valid responses | `tests/test_fallback.py`: `test_ollama_config_model_chain_defaults_to_single_model_list`, `test_ollama_config_model_chain_prefers_models_when_set`, `test_ollama_client_rejects_empty_model_list`, `test_ollama_client_accepts_bare_string_model_and_calls_it`, `test_bare_string_model_behaves_identically_to_list_of_one` (bare-string and 1-element-list clients produce byte-identical `ExtractionResult`s for the same response) **+ the full pre-existing 132-test v0.1.0 suite passes unmodified**, since every v0.1.0 test constructs `OllamaClient(base_url, "qwen3.5:9b-mlx", ...)` with a bare string | **PASS** |
| AC2.5 | All new paths (probe, fallback chain) fail closed to `unknown` on transport/HTTP error; inbound loop never raises | `tests/test_fallback.py`: `test_chat_json_all_models_connect_error_fails_closed`, `test_chat_json_all_models_http_500_fails_closed`, `test_chat_json_first_model_connect_error_second_model_recovers`, `test_probe_schema_support_total_outage_does_not_raise`, `test_handler_path_survives_total_llm_outage_no_exception_escapes` (full `handle_inbound_message` path, real mocked `OllamaClient`, both models unreachable → clarifying question, zero rows, no exception), `test_chat_json_none_result_still_fails_closed_through_parser` | **PASS** |

## Fixture audit — `tests/test_confirmations.py:patch_parse_message`

Luna's `IMPL.md` flags this as the one test-file edit in the v0.2.0 diff: `fake_parse_message`'s signature grew a 5th parameter, `confidence_threshold=None`, because `handle_inbound_message` now calls `parse_message(text, llm, glass_ml, bottle_ml, config.ollama.confidence_threshold)` with 5 positional args — the old 4-parameter stub would raise `TypeError: fake_parse_message() takes 4 positional arguments but 5 were given` the moment any `test_confirmations.py` test ran.

Audited directly against `git diff v0.1.0 -- tests/test_confirmations.py`:
```diff
- async def fake_parse_message(text, llm, glass_ml, bottle_ml):
+ async def fake_parse_message(text, llm, glass_ml, bottle_ml, confidence_threshold=None):
      return result
```
That is the entire diff to this file — one line. The new parameter is never read inside the function body; `result` is still the same fixed `ExtractionResult` the test configured via `patch_parse_message(monkeypatch, result)`, completely bypassing real confidence-threshold logic (by design — `test_confirmations.py`'s job is confirmation-string formatting, not extraction/threshold behavior, per its own module docstring). No assertion in the file changed. Confirmed by re-running the full `test_confirmations.py` file (26 tests) both before and after this change conceptually: it's a pure call-signature compatibility shim, not a behavior change or a weakened assertion. Verdict: **legitimate, mechanical fix — kept as-is, no rewrite needed.**

## Failures (if any)

None.

## Regressions detected

None. All 132 v0.1.0 tests pass unmodified (same assertions as the v0.1.0 baseline). The only touched test file (`test_confirmations.py`) has a fixture-signature change only, audited above as non-behavioral.

## Live spot-checks (supplementary, not part of the pass/fail gate)

Run against the real Ollama server at `http://mac-mini:11434` (reachable this session), via an ad hoc script, no production files touched:
- `probe_schema_support()` against the real `["qwen3.5:9b-mlx", "qwen3:8b"]` chain → `{'qwen3.5:9b-mlx': True, 'qwen3:8b': True}` — both currently report conformant, consistent with Luna's `IMPL.md` note that live behavior now differs from the v0.1.0-documented gap.
- Live `parse_message("500ml", ...)` through the real fallback-aware `chat_json` → `water`, `500` ml, confidence `0.95` — correct, first model used, no fallback needed.
- A client pointed at a non-existent host (`http://nonexistent-host-xyz:11434`, single-model chain) → `parse_message` returned `ExtractionResult.unknown()` cleanly (DNS failure surfaced as `getaddrinfo failed`, logged and swallowed, no exception propagated) — live confirmation of AC2.5's fail-closed behavior on a real transport error, not just a mocked one.

## Recommendation

**Ready to ship.** All 5 acceptance criteria (AC2.1-AC2.5) are covered and green; the full 160-test suite (132 v0.1.0 + 28 new) passes with the same single intentional skip as before; the one test-file fixture edit is audited as mechanical and non-weakening; live spot-checks against the real Ollama server corroborate the mocked results, including a real (not simulated) transport failure.
4. Confirmed no stray `.env` or `data/` was left in the repo (`git status --short` shows only the expected untracked deliverable files; `.env` does not exist).

No live re-test against the real `api.telegram.org` 401 was repeated for this verification round (the mocked regression test added in the first pass exercises the exact same `httpx.HTTPError` → clean-exit path deterministically and offline; the original live 401 round-trip from the first pass, quoted in the prior version of this report, already established that Telegram really does return 401 for a bad token — that fact hasn't changed).

## Live spot-checks (supplementary evidence, not pytest dependencies)

Ollama reachable at `http://mac-mini:11434` (confirmed via `/api/version` → `0.32.6`) during the first test pass. Telegram credentials remain absent from `.env` per Luna's intentional handoff state — no real Telegram sends were made in this verification round.

**AC5 — live bilingual/unit extraction** (via the real `parse_message` → real `OllamaClient` → live `qwen3.5:9b-mlx`):

| Message | Result |
|---|---|
| `ดื่มน้ำ 2 แก้ว` | `water`, `water_ml=500` (2 × 250ml glass) |
| `did 10 min stretch` | `stretch`, `stretch_min=10` |
| `500ml` | `water`, `water_ml=500` (explicit ml) |
| `1 ขวดน้ำ` | `water`, `water_ml=600` (1 × 600ml bottle) |
| `purple elephants dance sideways` | `unknown` (fail-closed for gibberish) |

All 5 match spec/AC5 exactly.

**AC9 — live `--dry-run`** (`--dry-run "500ml"`, run from an isolated scratch directory so no repo files were touched): real Ollama call returned `{'category': 'water', 'water_ml': 500, 'stretch_min': None, 'diary_text': None, 'confidence': 0.95}`, printed to stdout, no DB write attempted (dry-run path). Confirms the live wiring works end-to-end; the mocked suite covers the same contract offline.

**AC9 — live `--test-reminder` 401 path (first pass only):** reproduced the original crash against the real `api.telegram.org` with a temporary, immediately-deleted `.env` (copied from `.env.example`'s placeholder values, never committed) — this is what surfaced the AC9 gap in the first place. Not repeated in this verification round since the offline regression test covers the same code path deterministically.

## Recommendation

**Ready to ship.** All 11 acceptance criteria (AC1–AC11) pass. 132/133 tests green, 1 intentional skip, 0 failures, 0 regressions. The one gap found in the first pass (AC9 `--test-reminder` error handling) was fixed by Luna and independently re-verified against the exact test that caught it, plus a full clean suite run.

---

# Test Report — v0.3.0 (Migrations & Backup/Restore)

Implements `ROADMAP.md`'s "v0.4.0 — Migrations & Backup/Restore" section, ACs 4.1–4.5 (Archi reordered it ahead of Runtime Resilience; ships as **v0.3.0** per `IMPL.md`'s note that ROADMAP §3 itself recommends this swap).

## Summary
- Total: 186 tests (160 baseline + 26 new)
- Passed: 186
- Failed: 0
- Skipped: 1 (same pre-existing intentional skip as baseline, unrelated to this version)
- Status: **PASS**

Full suite run: `.venv\Scripts\python.exe -m pytest -q` → `186 passed, 1 skipped in 14.61s`, on Windows, fully offline (subprocess CLI tests spawn the local `python -m habit_assistant.main` entry point only — no Ollama/Telegram network calls). Baseline re-confirmed independently before writing any new test: `160 passed, 1 skipped`, matching Luna's `IMPL.md`-reported baseline exactly.

**Live-environment compliance:** every test in `tests/test_migrations.py` and `tests/test_backup.py` operates exclusively on `tmp_path` (pytest's per-test scratch dir) or CLI subprocesses with `cwd=tmp_path` — `config.toml`'s relative `db_path`/`[backup].dir` resolve against the scratch dir, never the repo. `data/habits.db`'s mtime (17:35, before this session's test run) and the live bot process (PID 15804, `Responding: True`) were both confirmed unaffected after the full run. No `.env` was read or written by any new test. No commit was made.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_migrations.py` | 9 | AC4.1, AC4.2 |
| `tests/test_backup.py` | 17 | AC4.3, AC4.4, AC4.5 (+ CLI checks for `--migrate`/`--backup`/`--restore` across all three ACs) |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC4.1 | Fresh DB reports schema version 0→N after startup; migrations run exactly once and are idempotent (second startup applies nothing) | `tests/test_migrations.py`: `test_fresh_db_migrates_to_latest_version`, `test_second_open_of_migrated_db_applies_nothing` (also confirms the previously-inserted row survives the no-op reopen), `test_current_version_reports_pragma_user_version`, `test_run_migrations_on_bare_connection_returns_from_and_to_version` — plus runner-contract tests against synthetic migration lists (independent of `MIGRATIONS`' current content): `test_run_migrations_applies_only_pending_migrations`, `test_run_migrations_is_noop_when_already_at_target_version`, `test_run_migrations_rolls_back_failed_migration_and_reraises` (a failing migration rolls back cleanly, DB left at the last-good version, retry only re-runs the failed migration onward — matches `IMPL.md`'s documented per-migration rollback design) + CLI `tests/test_backup.py::test_cli_migrate_creates_db_and_is_idempotent` (`--migrate` twice via subprocess: first run creates `data/habits.db` and prints `0 -> N`, second prints `N -> N`) | **PASS** |
| AC4.2 | A DB created by v0.1.0 (baseline schema) migrates forward with all existing `logs` rows intact (row count and values unchanged) | `tests/test_migrations.py::test_v010_shaped_db_migrates_forward_preserving_rows` (hand-built v0.1.0-shaped DB via the exact old inline `SCHEMA` DDL, raw `sqlite3.connect`/`executescript`, `user_version` left at SQLite's implicit default of 0; 3 seeded rows across all three categories; opened through the real `Database` class; asserts row count *and* every column value byte-identical before/after, not just count) + `test_v010_shaped_db_index_and_wal_still_correct_after_migration` (index + WAL mode intact post-migration) | **PASS** |
| AC4.3 | `--backup` produces a restorable copy while the DB is in use (WAL); the copy opens and queries identically to the source | `tests/test_backup.py::test_backup_while_source_connection_open_includes_latest_committed_write` — real `Database` connection held open for the entire test (confirmed via `PRAGMA journal_mode` == `wal`), 3 rows inserted, `backup()` called from a second connection while the source stays open; backup contains all 3 rows byte-identical to source, including the most recent write; a 4th row inserted *after* the backup call is confirmed absent from the snapshot (proves point-in-time semantics, not a live view); source DB remains fully queryable/correct afterward; backup file reopens and queries identically through the real `Database` class + `test_backup_raises_when_source_db_does_not_exist` + CLI `test_cli_backup_runs_without_telegram_secrets_and_writes_restorable_file` (`--seed` then `--backup` via subprocess, no `.env`/secrets present, file contains the seeded rows) | **PASS** |
| AC4.4 | `--restore <file>` replaces the live DB atomically; a corrupt/invalid file is rejected with a clear error and leaves the current DB untouched | `tests/test_backup.py::test_restore_swaps_db_and_creates_automatic_pre_restore_backup` (old data replaced by archive's new data; an automatic pre-restore backup containing the *old* data appears in `backup_dir` before the swap) + `test_restore_rejects_garbage_bytes_and_leaves_db_untouched` (junk bytes → `BackupError`, DB bytes byte-for-byte unchanged, no automatic backup triggered since validation fails first) + `test_restore_rejects_valid_sqlite_file_missing_logs_table` (syntactically valid SQLite, wrong schema → rejected, DB untouched) + `test_restore_rejects_missing_archive_file` + `test_restore_onto_nonexistent_db_skips_pre_restore_backup` (fresh install case) + CLI: `test_cli_restore_without_yes_refuses_and_leaves_db_untouched` (`--restore` without `--yes` → exit 1, stderr mentions `--yes`, DB bytes unchanged), `test_cli_restore_rejects_corrupt_file_leaving_live_db_untouched` (exit 1, stderr has `ERROR`, live DB unchanged), `test_cli_restore_valid_backup_with_yes_succeeds` (exit 0, restored row count matches the backup's) | **PASS** |
| AC4.5 | `retain` prunes backups down to the configured count; never deletes the newest | `tests/test_backup.py::test_prune_backups_keeps_only_the_newest_retain_count` (6 fake backups with distinct sortable timestamps, `retain=3` → exactly the 3 newest survive) + `test_prune_backups_never_deletes_the_newest_even_at_retain_zero` + `test_prune_backups_never_deletes_the_newest_at_negative_retain` (pathological `retain` values still keep exactly 1, the newest) + `test_prune_backups_on_empty_or_missing_dir_is_a_noop` + `test_backup_auto_prunes_after_each_write_when_retain_given` (5 sequential `backup(..., retain=2)` calls → exactly 2 remain) | **PASS** |

## BOM audit — `pyproject.toml`

`IMPL.md`'s "Known limitations" flags a blocking, out-of-scope fix: `pyproject.toml` had a UTF-8 BOM (`EF BB BF`) at byte 0 (present since v0.2.0 per `git show v0.2.0:pyproject.toml`), which made `tomllib`/`pytest` fail to start at all. Luna stripped it byte-level.

Verified directly against `git diff v0.2.0 -- pyproject.toml`: the only textual change is the BOM removal on line 1 (`-﻿[project]` → `+[project]`). Confirmed at the byte level: fetched `v0.2.0`'s `pyproject.toml` via `git show`, stripped its 3-byte BOM prefix in Python, and diffed line-by-line (`difflib.unified_diff`) against the current working-tree file — **zero content differences**; the only byte-count discrepancy (691 vs 723 bytes) is `git show`'s LF-normalized output vs. the working tree's CRLF line endings (this repo's `core.autocrlf`, unrelated to Luna's change and present on every other file in the diff). **Verdict: content-identical apart from the BOM, as claimed.**

## Failures (if any)

None.

## Regressions detected

None. All 160 baseline tests (v0.1.0 + v0.2.0) pass unmodified — same assertions, same count, same single intentional skip. No existing test file was touched; both new test files are additions only.

## Recommendation

**Ready to ship.** All 5 acceptance criteria (AC4.1–AC4.5) are covered and green. The full 186-test suite (160 baseline + 26 new) passes with the same single intentional skip as before — 0 failures, 0 regressions. The out-of-scope `pyproject.toml` BOM fix Luna made to unblock testing is audited as content-identical apart from the BOM. Production DB (`data/habits.db`, PID 15804) and `.env` were verified untouched throughout.

---

# Test Report — v0.4.0 (Runtime Resilience & Self-Monitoring)

Implements `ROADMAP.md`'s "v0.3.0 — Runtime Resilience & Self-Monitoring" section (AC3.1–AC3.5), **shipped as v0.4.0** per Archi's reorder (Migrations first, so this version's deferral queue is persistent from the start rather than in-memory). Built on top of the released v0.3.0 migration runner (`data/habits.db` schema version 2, migration `_migration_002_category_index`).

## Summary
- Total: 203 tests (186 baseline + 17 new)
- Passed: 203
- Failed: 0
- Skipped: 1 (same pre-existing intentional skip as baseline, unrelated to this version)
- Status: **PASS**

Full suite run: `.venv\Scripts\python.exe -m pytest -q` → `203 passed, 1 skipped in 26.07s`, on Windows, fully offline (`httpx.MockTransport` for every Telegram/Ollama/health-check request; real on-disk SQLite via `tmp_path` for persistence tests). Baseline re-confirmed independently before writing any new test: `186 passed, 1 skipped`, matching Luna's `IMPL.md`-reported baseline exactly.

**Live-environment compliance:** every test in `tests/test_resilience.py` runs against a mocked `httpx.MockTransport` (no real network call to Telegram or Ollama) or a `tmp_path`-backed `Database` (never `data/habits.db`). Confirmed `data/habits.db`'s mtime (`1787135716.401662`) identical before and after the full run, and the live bot process (PID 1660) still present and untouched. No `.env` was read or written by any new test. No commit was made.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_resilience.py` | 17 | AC3.1, AC3.2, AC3.3, AC3.4, AC3.5, migration 002 |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC3.1 | Simulated Telegram transport errors trigger exponential backoff (capped), recovering without dropping the polling offset (no missed/duplicated updates) | `test_backoff_grows_and_caps_across_consecutive_transport_errors` (4 consecutive `httpx.ConnectError`s → captured `asyncio.sleep` calls `== [1.0, 2.0, 4.0, 4.0]`, cap respected on the 4th; `channel._offset` stays `None` — never touched by failure-only runs) + `test_backoff_resets_to_initial_after_a_successful_poll` (2 failures → `[1.0, 2.0]`, success resets, next 2 failures → `[1.0, 2.0]` again, not `[4.0, 8.0]`) + `test_offset_never_advances_on_failure_and_recovery_resumes_from_correct_offset` (success carrying `update_id=10` → 3 consecutive failures → success carrying `update_id=20`; asserts the `offset` query param sent on *every one* of the 3 failing requests, and on the recovering request, is exactly `"11"` — proving the offset the retry path sends is never corrupted by the failures in between; `received == ["500ml", "10 min stretch"]`, each exactly once; final `channel._offset == 21`) | **PASS** |
| AC3.2 | Ollama DOWN→(stays down) sends exactly one channel alert; no repeat alert until UP then DOWN again | `test_exactly_one_alert_per_up_to_down_transition_no_repeat_while_still_down` (7-cycle sequence up→down×3→up→down×2; asserts `channel.sent == [OLLAMA_DOWN_MESSAGE, OLLAMA_DOWN_MESSAGE]` — exactly 2 alerts, one per transition, not 5 for 5 failing checks — and `on_ollama_recovered` fired exactly once) + `test_no_new_alert_while_still_down_new_alert_only_after_up_then_down_again` (up→down×5, never recovers in-run: `channel.sent == [OLLAMA_DOWN_MESSAGE]`, exactly one) + `test_alert_still_logged_when_channel_send_itself_fails` (channel's `.send` raises `RuntimeError` on the DOWN transition: `run_once()` does not propagate the exception, `caplog` still shows `OLLAMA_DOWN_MESSAGE` logged as the fallback record) | **PASS** |
| AC3.3 | A message received while the LLM is unavailable is acknowledged and persisted as `unparsed` (raw text kept), then automatically re-parsed and confirmed when Ollama returns | `test_deferred_message_acks_writes_unparsed_row_and_never_calls_llm` (`_NeverCalledLLM` raises `AssertionError` if ever invoked — proves the LLM is never touched; `channel.sent == [DEFERRED_ACK_MESSAGE]`; one `category='unparsed'` row, raw text preserved, `value_num`/`value_text` both `None`) + `test_deferred_row_excluded_from_aggregations_while_pending` (`water_total_ml`/`stretch_count`/`diary_count` all `0` while pending) + `test_deferred_row_persists_across_database_close_and_reopen` (real `tmp_path` file: `Database` closed then **reopened at the same path**, simulating a full process restart — `pending_unparsed()` still returns the row; this is the PERSISTENT-not-in-memory requirement, explicitly verified against disk, not mocked) + `test_reparse_on_recovery_reclassifies_confirms_and_reincludes_in_aggregations` (`reparse_pending_unparsed` with a now-succeeding LLM stub: `🔁 Recovered: 500 ml logged...` sent, row flips `unparsed`→`water` with `value_num==500.0` and original `raw_message` intact, `pending_unparsed()` empty, `water_total_ml` now `500.0`) + `test_startup_backlog_reparsed_with_no_in_process_transition` (a row inserted directly — simulating what a *previous process run* left behind — is picked up by `reparse_pending_unparsed` called on its own, with **no `HealthMonitor` ever constructed** in the test, proving the startup call site doesn't depend on an in-process DOWN→UP transition) + `test_reparse_leaves_genuinely_unparseable_row_as_unparsed` (still-`unknown` re-parse result leaves the row as `unparsed`, sends no recovery confirmation, per `IMPL.md`'s documented "not retried until next recovery" behavior) | **PASS** |
| AC3.4 | Telegram unreachable → logged and retried, process stays alive (no crash, no exit) | `test_telegram_unreachable_is_logged_and_retried_without_crashing` (6 consecutive `httpx.ConnectError`s: `caplog` shows exactly 6 `"Telegram getUpdates failed"` warnings, one per failure, none propagated as an unhandled exception — only the test's own sentinel on the 7th call ends the loop) — also demonstrated by every AC3.1 test above, each of which drives `channel.run` through multiple consecutive failures without the loop exiting or raising anything but the deliberate stop sentinel | **PASS** |
| AC3.5 | Health checks are read-only calls to the two already-allowed hosts only — no new outbound destination | `test_only_allowed_hosts_contacted_across_all_resilience_paths` — one shared `httpx.MockTransport`/`AsyncClient` wired into a `TelegramChannel`, an `OllamaClient`, and a `HealthMonitor` together; the handler raises `AssertionError` on any host outside `{mac-mini, api.telegram.org}` (fails fast on a leak, doesn't just record it); exercises `TelegramChannel.send`, one `getUpdates` poll cycle, `OllamaClient.chat_json` (including its transport-retry path — first attempt fails, second succeeds), and `HealthMonitor.run_once` (both `/api/version` and `/getMe`) against the *same* client; final `hosts_hit == {"mac-mini", "api.telegram.org"}`, nothing else | **PASS** |
| Migration 002 | `_migration_002_category_index` applies on a fresh DB, applies forward from a v1-only DB, and is idempotent | `test_migration_002_creates_category_index_on_a_fresh_db` (`schema_version == len(MIGRATIONS) == 2`, `idx_logs_category` present) + `test_migration_002_applies_forward_from_a_v1_db` (bare connection stamped at `user_version=1` via `MIGRATIONS[:1]` only, index absent; running the full `MIGRATIONS` list applies migration 002 specifically, `(1, 2)` from/to, index now present) + `test_migration_002_is_idempotent` (`Database` reopened at the same path a second time: `schema_version_before == schema_version == 2`, nothing re-applied, index still present) | **PASS** |

## Failures (if any)

None.

## Regressions detected

None. All 186 baseline tests (v0.1.0–v0.3.0) pass unmodified — same assertions, same count, same single intentional skip. No existing test file was touched; `tests/test_resilience.py` is an addition only.

## Recommendation

**Ready to ship.** All 5 acceptance criteria (AC3.1–AC3.5) plus migration 002 are covered and green. The full 203-test suite (186 baseline + 17 new) passes with the same single intentional skip as before — 0 failures, 0 regressions. AC3.3's PERSISTENT (not in-memory) deferral queue requirement is explicitly verified with a real close/reopen of an on-disk `Database`. AC3.5's "only the two allowed hosts" requirement is verified with a fail-fast assertion inside the mocked transport handler shared across all three resilience components (Telegram, Ollama, health monitor), not just a post-hoc host-set check. Production DB (`data/habits.db`, PID 1660) was verified untouched (identical mtime) throughout; no real Telegram call was made; no commit was made.

---

# Test Report — v0.5.0 (Command Layer & Edit/Undo)

Implements `ROADMAP.md`'s "v0.5.0 — Command Layer & Edit/Undo" section (AC5.1–AC5.5): a conservative, LLM-free, whole-message-anchored command router (`core/commands.py`) in front of the parser, undo (soft-delete) and edit built on it, and migration 003 (`deleted_at` column). Built on top of the released v0.4.0 (203 passed / 1 skipped baseline).

## Summary
- Total: 252 tests (203 baseline + 49 new)
- Passed: 252
- Failed: 0
- Skipped: 1 (same pre-existing intentional skip as baseline, unrelated to this version)
- Status: **PASS**

Full suite run: `.venv\Scripts\python.exe -m pytest -q` → `252 passed, 1 skipped in 27.51s`, on Windows. Baseline re-confirmed independently before writing any new test: `203 passed, 1 skipped`, matching Luna's `IMPL.md`-reported baseline exactly.

**Live-environment compliance:** every new test runs against a `tmp_path`-backed `Database` (never `data/habits.db`) and either a `_NeverCalledLLM` stand-in (raises `AssertionError` if the command path ever touches it — proves the "no LLM call anywhere in this version's new code" claim) or a monkeypatched `habit_assistant.main.parse_message` (bypasses the real LLM entirely, same pattern `test_confirmations.py` already uses). No real Telegram call, no `.env` read/written. Production bot (PID 5388) and `data/habits.db` confirmed present/untouched (mtime check) before and after the run. No commit was made.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_commands.py` | 49 | AC5.1, AC5.2, AC5.3, AC5.4, AC5.5, migration 003, LLM-down command execution |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC5.1 | `/undo`, "undo last", "ยกเลิกอันล่าสุด" each soft-delete the most recent non-deleted log via the real `handle_inbound_message` path; confirmation names what was removed; totals drop; second undo removes the next-most-recent | `test_undo_phrasing_soft_deletes_most_recent_log` (parametrized over all 3 phrasings: confirmation names the removed row, `water_total_ml` drops by exactly that row's value, raw `SELECT` shows the row still present with `deleted_at` set) + `test_second_undo_removes_the_next_most_recent_log` (two sequential `/undo` calls remove newest-then-next-newest, in that order, total reaches 0, both rows still physically present) + `test_undo_confirmation_names_stretch_entry_and_updates_count` + `test_undo_confirmation_names_diary_entry` (category-specific confirmation wording and post-undo aggregate, for stretch and diary respectively) | **PASS** |
| AC5.2 | Undo with no prior entry / all-deleted history → friendly message, zero writes | `test_undo_on_empty_history_sends_friendly_message_and_writes_nothing` (`NOTHING_TO_UNDO_MESSAGE`, raw row count `0`) + `test_undo_on_all_deleted_history_sends_friendly_message_and_writes_nothing` (one entry, undone once, then undone again — second call sends the same friendly message and the raw row count is unchanged from after the first undo, i.e. no phantom write) | **PASS** |
| AC5.3 | "make that 300ml" / "แก้เป็น 300 มล." update the last matching entry in place and re-confirm the new total; edit with no matching entry handled gracefully | `test_edit_updates_last_matching_water_entry_and_reconfirms_total` (parametrized over both the English and the literal Thai ROADMAP example: row count stays `1` — updated in place, not a new row — new total and exact confirmation string match) + `test_edit_updates_only_the_last_matching_entry_not_earlier_ones` (two water rows, edit touches only the most recent) + `test_edit_updates_last_matching_stretch_entry` (unit-driven category dispatch: "min" → stretch) + `test_edit_with_no_matching_category_entry_sends_friendly_message_and_writes_nothing` (only a stretch row on file, a water-shaped edit finds no target: `NOTHING_TO_EDIT_MESSAGE`, the unrelated stretch row is byte-unchanged) + `test_edit_on_empty_history_sends_friendly_message_and_writes_nothing` | **PASS** |
| AC5.4 | A soft-deleted row stays in the table (auditable) but is excluded from every aggregation: water totals, stretch count/ordinal, diary count, weekly review stats, `pending_unparsed` | `test_soft_deleted_row_remains_in_table_but_excluded_from_water_total` (raw `SELECT` finds the row with non-NULL `deleted_at`; `water_total_ml` excludes it) + `test_soft_deleted_row_excluded_from_stretch_count_and_ordinal` (a soft-deleted earlier stretch session doesn't inflate the ordinal — a fresh session correctly reports "1st today", not "2nd") + `test_soft_deleted_row_excluded_from_diary_count` + `test_soft_deleted_row_excluded_from_pending_unparsed` + `test_soft_deleted_rows_excluded_from_weekly_review_stats` (`compute_weekly_stats` — the same aggregation the weekly review sends — excludes both a soft-deleted water row and a soft-deleted stretch row) + `test_soft_delete_never_issues_a_hard_delete` (raw row count identical before/after `soft_delete`) | **PASS** |
| AC5.5 | A normal habit message routes to the parser unchanged — the command layer has zero false positives, even on messages that look commandish | `test_dispatch_returns_none_for_normal_habit_messages` + `test_normal_habit_messages_reach_parser_exactly_once`, both parametrized over a 10-message adversarial corpus: `"ดื่มน้ำ 2 แก้ว"`, `"500ml"`, `"did 10 min stretch"`, `"today I had to undo a mistake at work"` (English "undo" mid-sentence), `"เลิกงานแล้ว เหนื่อยมาก"` (Thai "เลิก" — a substring of the undo trigger "ยกเลิก" — mid-sentence), `"made 3 bottles of juice"`, `"I need to delete some old photos later"` (English "delete" mid-sentence), `"ยกเลิกการนัดหมายพรุ่งนี้"` (Thai undo trigger as a *prefix* of a longer sentence, not the whole message), `"change it to feeling better today"` (matches the edit-trigger prefix but the tail doesn't parse as `NUMBER [+ UNIT]`), `"I finally decided to cancel my gym membership"`. For every one: `commands.dispatch(...) is None` **and** a call-counting `parse_message` stub was invoked exactly once with the unmodified original text. Plus `test_command_layer_does_not_intercept_a_normal_log_even_after_commands_ran` (undo then edit run first in the same DB/session; a subsequent plain "500ml" still reaches the parser exactly once and logs normally) | **PASS** |
| (supplementary) | Commands work while the LLM is DOWN — dispatch precedes the v0.4.0 availability check | `test_undo_executes_while_llm_is_down_not_deferred` + `test_edit_executes_while_llm_is_down_not_deferred` (a `_FrozenHealthMonitor(ollama_up=False)` is passed in; the command still executes directly — DB updated, confirmation sent, **not** `DEFERRED_ACK_MESSAGE`, no `unparsed` row written) + `test_normal_message_while_llm_down_is_still_deferred_not_intercepted` (control case: a genuinely normal message under the same DOWN condition still takes the v0.4.0 deferral path unchanged — proves the new dispatch step didn't broaden scope) + `test_undo_command_in_dry_run_does_not_write_or_require_channel` (`--dry-run`-style call with `channel=None` doesn't write and doesn't raise) | **PASS** |
| (supplementary) | Migration 003 (`deleted_at`): fresh DB → version 3; forward from v2 with rows intact; idempotent | `test_fresh_db_migrates_to_schema_version_3` (pinned literal — see audit below) + `test_migration_003_applies_forward_from_a_v2_db_with_rows_intact` (bare connection stamped at `user_version=2` via `MIGRATIONS[:2]`, a pre-existing row inserted, then the full `MIGRATIONS` list applied: `(2, 3)` from/to, `deleted_at` column now present, the pre-existing row's values byte-identical and its own `deleted_at` is `NULL` — not retroactively marked deleted) + `test_migration_003_creates_deleted_at_index` + `test_migration_003_is_idempotent` (`Database` reopened at the same path: `schema_version_before == schema_version == 3`, prior row survives) | **PASS** |

## Audit — Luna's edits to `tests/test_db.py` and `tests/test_resilience.py`

Read both diffs against the v0.4.0 baseline in full (`git diff v0.4.0 -- tests/test_db.py tests/test_resilience.py`). Four tests touched:

1. **`test_db.py::test_logs_table_created_with_expected_columns`** — expected column set gained `"deleted_at"`. Correct and necessary: migration 003 genuinely adds this column to every DB, so the old assertion (missing it) would now fail honestly. Not weakened — it's strictly a superset check against the new true schema, with an inline comment pointing at migration 003. **No change needed.**
2. **`test_resilience.py::test_migration_002_creates_category_index_on_a_fresh_db`** — `schema_version == len(MIGRATIONS) == 2` → `schema_version == len(MIGRATIONS)`. Test intent is "does a fresh DB apply the category index," not "how many migrations exist" — the version comparison is incidental scaffolding, not the assertion under test. Reasonable.
3. **`test_resilience.py::test_migration_002_applies_forward_from_a_v1_db`** — the "apply migration 002 onto a v1 DB" step switched from `run_migrations(conn, migrations=MIGRATIONS)` (the full, now-longer list) to `run_migrations(conn, migrations=MIGRATIONS[:2])`. This is the right fix, not a workaround: the test's name and docstring say it's specifically about the 001→002 transition, and running the full list would silently also apply 003, changing the asserted `(from_version, to_version) == (1, 2)` into a lie about what the test actually exercises. **Correctly scoped, not weakened.**
4. **`test_resilience.py::test_migration_002_is_idempotent`** — same `len(MIGRATIONS)` substitution as #2, same reasoning: the assertion under test is "reopening applies nothing," not "there are N migrations."

**Judgment call on `len(MIGRATIONS)`-style assertions:** these are self-referential — `schema_version` is *computed from* `len(MIGRATIONS)` inside `run_migrations`, so `schema_version == len(MIGRATIONS)` cannot fail no matter how many migrations exist or are added later; it only checks that the version-stamping wiring is internally consistent (which is a legitimate thing to check as an incidental assertion in a test that's really about something else, e.g. index creation). What it does **not** provide is a genuine regression guard on "the current migration count / target version is exactly N" — a future PR that silently drops a migration from the list, or adds one without meaning to, would sail through all four of these tests unnoticed. That guard did exist before (the literal `2`), just narrowly and by accident (it would have broken on every future additive migration regardless of correctness, which is why Luna's fix was still the right call for tests #2–#4).

**Verdict: no assertion was weakened in a way that hides a real bug, and no rewrite of Luna's four tests is needed.** But the missing "pinned" regression guard is real, so I supplied it independently in the new test file rather than editing Luna's: `tests/test_commands.py::test_fresh_db_migrates_to_schema_version_3` asserts both `len(MIGRATIONS) == 3` and `database.schema_version == 3` as **literals**, with a docstring explaining why a bare `== len(MIGRATIONS)` comparison isn't enough on its own. This is deliberately a separate, dedicated test file from `test_resilience.py`/`test_migrations.py`, so it's the natural place a v0.6.0+ migration would also need to bump its own pinned literal — same self-documenting mechanism, now actually load-bearing.

## Failures (if any)

None.

## Regressions detected

None. All 203 baseline tests (v0.1.0–v0.4.0) pass unmodified — same assertions (per the audit above), same count, same single intentional skip. No existing test file was rewritten to force a pass; `tests/test_commands.py` is an addition only.

## Recommendation

**Ready to ship.** All 5 acceptance criteria (AC5.1–AC5.5) plus migration 003 are covered and green, including the explicit "commands survive an LLM outage" property IMPL.md calls out (dispatch runs before the v0.4.0 health-monitor check) and a 10-message adversarial false-positive corpus for AC5.5 covering both English and Thai command-word substrings inside otherwise-normal messages. The full 252-test suite (203 baseline + 49 new) passes with the same single intentional skip as before — 0 failures, 0 regressions. Luna's `test_db.py`/`test_resilience.py` edits were audited line-by-line and found correctly scoped, not weakened; the one real gap found (no pinned regression guard on the migration count) was closed with a new dedicated test rather than by touching Luna's files. Production bot (PID 5388) and `data/habits.db`/`.env` were verified untouched (present, no live Telegram/LLM calls made by any new test) throughout; no commit was made.

---

# Test Report — v0.6.0 (Bilingual Output & Message Catalog)

Implements `ROADMAP.md`'s "v0.6.0 — Bilingual Output & Message Catalog" section (AC6.1–AC6.5): a bilingual message catalog + language detector (`core/i18n.py`), every user-facing string in `main.py`/`core/reminders.py`/`core/review.py` (and, additionally, `core/health.py`) routed through it, and a `config.toml` `[i18n]` override (`language`/`primary_language`). Built on top of the released v0.5.0 (252 passed / 1 skipped baseline).

## Summary
- Total: 318 tests (290 baseline-after-Luna's-changes + 28 new from Vera)
- Passed: 318
- Failed: 0
- Skipped: 1 (same pre-existing intentional skip, unrelated to this version)
- Status: **PASS**

Full suite run: `.venv\Scripts\python.exe -m pytest -q` → `318 passed, 1 skipped in 28.69s`, on Windows. Luna's reported post-implementation baseline re-confirmed independently, before writing any new test: `290 passed, 1 skipped`, matching `IMPL.md`'s "Smoke test done" section exactly.

**Live-environment compliance:** every pytest test runs against a `tmp_path`-backed `Database` (never `data\habits.db`) with a `FakeChannel`/`FakeLLM`/monkeypatched `parse_message` — no real Telegram call anywhere in the pytest suite. Two supplementary live-Ollama spot-checks were run directly against `http://mac-mini:11434` (confirmed reachable via `curl /api/version` → `0.32.6`), independent of Luna's own `smoke_live_v060.py` — different diary text, different weekly-stats numbers — to corroborate AC6.4's load-bearing claim without relying solely on Luna's self-reported smoke test. Production bot (PID 18648) and `data\habits.db` confirmed present/untouched (mtime `2026-08-19 17:35:16`, identical before the pytest run, after the pytest run, and after the live spot-checks) throughout. No commit was made.

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_i18n.py` (Luna) | 15 | AC6.3 (resolvers), AC6.5 (detector), catalog integrity |
| `tests/test_i18n_literals.py` (Luna) | 5 | AC6.2 (no-hardcoded-literal static scan + 2 meta-tests) |
| `tests/test_bilingual_confirmations.py` (Luna) | 11 | AC6.1, AC6.3, AC6.4-adjacent (end-to-end via `handle_inbound_message`) |
| `tests/test_v060_bilingual_gaps.py` (Vera, new) | 28 | AC6.1 (byte-identical English regression + Thai numeric correctness), AC6.2 (`core/health.py` catalog wiring + independent scanner corroboration), AC6.4 (system-prompt language directive), AC6.5 (detector edge cases from this task's brief + mixed-language end-to-end) |
| `tests/test_cli.py`, `tests/test_reminders.py`, `tests/test_review.py`, `tests/test_commands.py` (Luna, modified) | 0 net new (9 expectations changed) | see audit below |

## AC coverage

| AC | Description | Tests | Result |
|---|---|---|---|
| AC6.1 | Thai input → Thai confirmation; English input → English confirmation; same structured result, localized copy — covering water/stretch/diary confirmations, undo/edit, and the clarify message, in both directions | Luna: `test_thai_input_produces_thai_water_confirmation`, `test_english_input_produces_english_water_confirmation`, `test_thai_and_english_input_yield_same_structured_data_different_copy`, `test_thai_input_produces_thai_stretch_confirmation`, `test_thai_input_produces_thai_clarifying_question`, `test_thai_undo_command_gets_thai_confirmation`. Vera (gap-fill): `test_english_water_confirmation_byte_identical_to_v050`, `test_english_stretch_confirmation_byte_identical_to_v050`, `test_english_diary_confirmation_byte_identical_to_v050`, `test_english_diary_confirmation_uses_v050_fallback_when_llm_empty`, `test_english_clarifying_question_byte_identical_to_v050`, `test_english_undo_water_confirmation_byte_identical_to_v050`, `test_english_undo_nothing_message_byte_identical_to_v050`, `test_english_edit_water_confirmation_byte_identical_to_v050`, `test_english_edit_nothing_message_byte_identical_to_v050` (expected strings copied verbatim from the v0.5.0 source, not derived from the catalog — a real regression-catcher, not a tautology), `test_thai_undo_confirmation_has_correct_numbers`, `test_thai_edit_confirmation_has_correct_numbers` (numeric correctness, independent of catalog wording) | **PASS** |
| AC6.2 | Every user-facing string in `main.py`, `reminders.py`, `review.py` resolves through the catalog — no hard-coded literal remains | Luna: `test_no_hardcoded_literal_passed_to_channel_send` (AST scan, parametrized over the 3 scoped files), `test_scanner_itself_actually_detects_a_planted_literal`, `test_scanner_does_not_flag_catalog_lookups_or_variables`. Vera (gap-fill, `core/health.py` was flagged in-scope by this task's brief even though ROADMAP's own file list omits it): `test_health_monitor_default_language_ollama_alert_matches_english_catalog_entry`, `test_health_monitor_thai_language_ollama_alert_is_localized`, `test_health_monitor_thai_language_telegram_alert_is_localized`, `test_health_alert_ids_are_present_in_catalog_for_both_languages`. Plus an independent, separately-implemented AST scanner (not importing Luna's) re-run against the real production files (`test_v060_scoped_source_files_are_actually_clean_per_independent_scanner_copy`) and two more adversarial plants (`test_scanner_catches_a_keyword_argument_literal`, `test_scanner_catches_multiple_offenders_in_one_module`), plus one documented (non-failing) known-limitation test (`test_known_limitation_variable_indirection_is_not_caught_by_either_scanner`) | **PASS** (see known-limitation note below) |
| AC6.3 | `language="th"`/`"en"` forces output regardless of input; `"auto"` matches input for replies and falls back to `primary_language` for unprompted sends | Luna: `test_resolve_reply_language_auto_matches_input`, `test_resolve_reply_language_forced_th_overrides_english_input`, `test_resolve_reply_language_forced_en_overrides_thai_input`, `test_resolve_unprompted_language_auto_uses_primary_language`, `test_resolve_unprompted_language_auto_respects_primary_language_override`, `test_resolve_unprompted_language_forced_overrides_primary`, `test_forced_th_language_overrides_english_input`, `test_forced_en_language_overrides_thai_input`, `test_forced_th_language_applies_to_clarifying_question_too`, plus the changed `test_test_reminder_flag_sends_correct_reminder_text_offline` and `test_schedule_reminders_job_args_bind_correct_category_and_channel` (unprompted → primary language `"th"` under default config, exercised through real production wiring). Vera (gap-fill): the `core/health.py` tests above additionally prove `language` is honored end-to-end for a third unprompted-send call site beyond reminders/review | **PASS** |
| AC6.4 | Weekly-review narrative generated in the target language, stays factual (no medical advice, unchanged constraint); fallback stats block also localized | Luna: changed `test_run_weekly_review_includes_narrative_and_stats`, `test_run_weekly_review_falls_back_when_llm_returns_none`, `test_run_weekly_review_falls_back_when_llm_returns_empty_string`, `test_run_weekly_review_passes_stats_summary_to_llm_prompt`, `test_run_weekly_review_defaults_to_today_when_not_given` (Thai stats block + Thai fallback narrative under default config); unchanged `test_run_weekly_review_system_prompt_forbids_medical_advice`; `test_diary_reflection_prompt_carries_the_resolved_language_instruction` (diary, not review). Vera (gap-fill — the *system*-prompt language directive for the *review* narrative specifically wasn't asserted anywhere): `test_weekly_review_system_prompt_carries_thai_directive_by_default`, `test_weekly_review_system_prompt_carries_english_directive_when_forced`. Supplementary (not part of the pass/fail gate): two independent live-Ollama spot-checks (see below) confirm the real Qwen model returns genuinely Thai, factual prose (hydration/stretching suggestions, zero medical claims) for inputs Luna's own smoke test didn't use | **PASS** |
| AC6.5 | Detector: any Thai character anywhere → Thai; pure-ASCII → English; deterministic | Luna: `test_detect_language_any_thai_char_wins` (11 cases incl. mixed Thai+English, single Thai char, empty string), `test_detect_language_is_deterministic`. Vera (gap-fill — the exact edge-case strings named in this task's brief): `test_detect_language_edge_cases_from_task_brief` (`"ดื่มน้ำ 500ml"` → th, `"500"` → en, `"💧"` → en, `"💧 500"` → en, `""` → en, `"   "` → en), plus `test_mixed_thai_english_input_produces_thai_reply_end_to_end` — the mixed-language string driven all the way through the real `handle_inbound_message` to a verified Thai reply, not just a unit-level detector call | **PASS** |

## Audit — the 9 changed expectations in `tests/test_cli.py`, `tests/test_reminders.py`, `tests/test_review.py`, `tests/test_commands.py`

Read every diff in full (`git diff v0.5.0 -- tests/test_cli.py tests/test_reminders.py tests/test_review.py tests/test_commands.py`) against `IMPL.md`'s own enumerated list of 9. Verdict per item — **all 9 are legitimate consequences of AC6.1/AC6.3/AC6.4, none are weakened assertions**:

| # | Test | Old → New | Verdict |
|---|---|---|---|
| 1 | `test_cli.py::test_test_reminder_flag_sends_correct_reminder_text_offline` | `REMINDER_TEXTS["stretch"]` (English) → `i18n.t("reminder_stretch", "th")` (Thai) | **KEEP.** `--test-reminder` is an unprompted send; default `Config()` (`primary_language="th"`) genuinely resolves to Thai through real production wiring (`main.py`'s `i18n.resolve_unprompted_language(config)`). Confirmed by tracing `async_main`'s `--test-reminder` branch in the diff — not a test-side shortcut. |
| 2 | `test_reminders.py::test_schedule_reminders_job_args_bind_correct_category_and_channel` | `job.args == (channel, "water")` → `(channel, "water", "th")` | **KEEP.** `schedule_reminders` genuinely grew a third bound arg (confirmed in `core/reminders.py`'s diff); the test tracks the real new signature, doesn't paper over anything. |
| 3–7 | `test_review.py`: `test_run_weekly_review_includes_narrative_and_stats`, `..._falls_back_when_llm_returns_none`, `..._falls_back_when_llm_returns_empty_string`, `..._passes_stats_summary_to_llm_prompt`, `..._defaults_to_today_when_not_given` | Literal English labels/fallback (`"📊 Weekly Review"`, `"Water total: 2500 ml"`, `"Here is your weekly summary."`, etc.) → `i18n.t(..., "th", ...)` equivalents | **KEEP (all 5).** `run_weekly_review` is an unprompted send; default `Config()` resolves Thai. Re-derived the expected Thai strings independently (not just trusting the diff) via `i18n.t` in a throwaway REPL check during audit — they match what the test file now asserts. The LLM-narrative-passthrough assertion (`"Great week! Keep up the water habit." in text`) is untouched in all 5, correctly — narrative text is opaque to localization, it just passes through. |
| 8 | `test_commands.py::test_undo_phrasing_soft_deletes_most_recent_log[ยกเลิกอันล่าสุด]` | `"300" in sent` + `"500" not in sent.split("Today")[0]` → `removed_description in sent` (built from `i18n.t("describe_log_water", lang, value_num=300.0)`) | **KEEP, with a supplementary test added.** The old split-on-"Today" trick doesn't generalize to Thai (no English "Today" marker in the Thai phrasing) — the replacement is language-agnostic and, if anything, a *stricter* check (must match the exact formatted phrase, not just contain the digit "300" anywhere). One real gap: the new assertion no longer explicitly proves the *older* 500ml row's value doesn't leak into the "what was removed" clause — Vera's `test_thai_undo_confirmation_has_correct_numbers` closes this by asserting both "300" (removed) and "500"+"20%" (correct remaining total) are present with the right roles, computed independently of any catalog lookup. |
| 9 | `test_commands.py::test_edit_updates_last_matching_water_entry_and_reconfirms_total[แก้เป็น 300 มล.]` | Single hardcoded English string (incorrectly applied to both parametrized phrasings pre-v0.6.0-fix) → per-language expected string built from `i18n.t("edit_updated_water", lang, ...)` | **KEEP.** Pre-v0.6.0 this assertion was only correct because *all* output was English regardless of input (v0.5.0's explicit scope note); once Thai input gets Thai output, the old single-literal assertion would fail honestly on the Thai-triggered case. The fix is exact-equality (`channel.sent == [expected]`), stricter than a substring check. Cross-checked against `describe_log_water`/`edit_updated_water` catalog entries directly — correct. |

**No item required a rewrite.** One item (#8) got a supplementary independent test rather than a rewrite, since the original replacement was correct but left a narrow gap in what it proved.

## Known limitation carried forward from `IMPL.md` (not a defect)

`test_i18n_literals.py`'s AST scanner (and Vera's independent copy) only flags a literal/f-string passed **directly** to `.send(...)` — a literal assigned to a variable first (`msg = "..."; await channel.send(msg)`) is invisible to both, confirmed identically by `test_known_limitation_variable_indirection_is_not_caught_by_either_scanner` (documents the gap, does not assert it against production code). This is not evidence any real AC6.2-scoped call site does this — a manual read of the `main.py`/`reminders.py`/`review.py` diffs found none — but it means the scanner's guarantee is "no *direct* literal," not "no literal reachable at all." Flagging for Luna/Archi as a possible future hardening (def-use tracing), same as `IMPL.md` itself already flags it as extensible.

## Failures (if any)

None.

## Regressions detected

None. All 290 tests from Luna's post-implementation baseline pass unmodified (9 audited expectation changes are correct updates, not regressions-in-disguise — see audit above). No existing test file was rewritten by Vera; `tests/test_v060_bilingual_gaps.py` is an addition only.

## Live spot-checks (supplementary, not part of the pass/fail gate)

Run independently of Luna's `smoke_live_v060.py`, against the same real, reachable Ollama server (`http://mac-mini:11434`, confirmed via `curl /api/version` → `0.32.6`), with **different inputs** than Luna used (different diary text, different weekly-stats numbers/zero-stretch case) — corroboration, not a re-run of the same evidence:

1. Diary reflection, Thai directive, input `"วันนี้ไปวิ่งตอนเช้า อากาศดีมาก รู้สึกสดชื่น"` → `"สุดยอดมากครับ การตื่นตัวแบบนี้ทำให้วันใหม่สดใสแน่นอน"` — confirmed Thai via `i18n.detect_language`.
2. Weekly-review narrative, Thai directive, stats block with 3200ml water (128%) and **zero** stretch sessions → a genuinely Thai, factual narrative acknowledging the water goal, correctly noting the zero-stretch week without inventing numbers, suggesting a bedtime reminder and short movement breaks — no medical claims. Confirmed Thai via `i18n.detect_language`.

Both PASSED. Production bot (PID 18648) and `data\habits.db` mtime unchanged before/after (verified separately from the pytest-suite check).

## Recommendation

**Ready to ship.** All 5 acceptance criteria (AC6.1–AC6.5) are covered and green. The 9 changed pre-existing test expectations were audited individually against the ROADMAP ACs and found to be legitimate consequences of Thai becoming the default primary/detected language, not weakened assertions — 8 kept as-is, 1 (`test_commands.py`'s Thai undo case) kept with a supplementary test added to close a narrow gap rather than a rewrite. Coverage was extended in 4 places Luna's own tests left thin: (a) byte-identical-to-v0.5.0 English regression checks for confirmations/undo/edit/clarify, not just "routes through the catalog"; (b) `core/health.py`'s catalog wiring, which ROADMAP's file list omits but this task's brief explicitly included; (c) the weekly-review narrative's *system*-prompt language directive, previously unasserted; (d) the exact detector edge-case strings named in this task's brief, plus an end-to-end mixed-language reply check. Two independent live-Ollama spot-checks (different inputs than Luna's) corroborate AC6.4's real-model claim. The full 318-test suite (290 baseline + 28 new) passes with the same single intentional skip — 0 failures, 0 regressions. Production bot (PID 18648), `data\habits.db`, and `.env` were verified untouched throughout (no real Telegram call anywhere; the only live network calls were the two explicitly-permitted Ollama spot-checks against scratch inputs); no commit was made.

---

# Test Report — v0.7.0 (Multi-Habit Extensibility)

## Summary

- Scope: the shared surface + all three parallel leaf modules (M1 extraction, M2 reminders, M3 review) + the integration wiring pass, judged jointly against `SPEC-v0.7.md` §8's AC1–AC17 and, at the ROADMAP level, AC7.1–AC7.5. This is the **release-gate** pass — it supersedes M1's own (never separately Vera-scoped) audit, and consolidates M2's/M3's module-scoped `TEST-v0.7-M2.md`/`TEST-v0.7-M3.md` (both already `PASS`, left as standalone files, not re-litigated below except where this pass adds independent evidence).
- **Full suite, run by this pass, from a clean checkout of the current tree:** **463 passed, 1 skipped, 0 failed** (`.venv\Scripts\python.exe -m pytest -q -rs`). The 1 skip is `tests/test_channels.py:231`, confirmed to be the pre-existing v0.1.0-era skip ("only core/ wires the Channel ABC directly") — present in every baseline run since v0.1.0, unrelated to v0.7.0. This reconciles exactly against the reported baseline: Luna's integration pass reported 432 passed / 1 skipped / 0 failed; this pass adds 31 new tests (12 in `test_ac17_v060_byte_identical_composite.py` + 18 in `test_multi_habit_integration.py` + 1 added to `test_parser.py`), and 432 + 31 = 463.
- **Status: PASS.**

## Test files (this pass's own additions)

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_ac17_v060_byte_identical_composite.py` (new) | 12 | AC17 — water (en, th-glass, en-bottle, th-bottle), stretch (en, th), diary (en, th), unknown (en, th), undo, edit, all driven through the REAL `handle_inbound_message` → `parse_message` → registry-built schema/prompt chain (network boundary mocked via `httpx.MockTransport`, not `main.parse_message` itself), asserted against literals hand-copied from `git show v0.6.0:src/habit_assistant/core/i18n.py` |
| `tests/test_multi_habit_integration.py` (new) | 18 | AC11 — a `sleep` (numeric+goal) and synthetic `meds` (boolean) habit added via `Config`/`HabitConfig` data only (zero production-code edits): extraction, confirmation (both languages, matching SPEC §3.2's own examples verbatim), reminder job registration + generic-template firing, weekly-review inclusion (both languages, alongside built-ins), undo/edit where type-appropriate, and migration-004-backfilled legacy rows aggregating alongside brand-new habit rows in one `compute_weekly_stats` call |
| `tests/test_parser.py` (1 test added to Luna's file) | 1 | AC8-adjacent audit fix — `test_arbitrary_custom_unit_alias_value_reaches_the_prompt`, closing a coverage gap found during the old→new rewrite audit (see below) |

No other test file was modified by this pass. `TEST-v0.7-M2.md` and `TEST-v0.7-M3.md` remain the module-scoped reports for M2/M3 and are not duplicated here.

## Audit — M1's test rewrites + integration Luna's touched tests (old → new, per this task's mandate)

Compared every file in `IMPL-v0.7-M1.md`'s own old→new table (`test_parser.py`, `test_fallback.py`, `test_commands.py`, new `test_prompts.py`) and every file in `IMPL.md`'s "v0.7.0 — Integration" §"Every test touched, old → new" table (`test_resilience.py`, `test_v060_bilingual_gaps.py`, `test_commands.py`, `test_db.py`, `test_cli.py`, `test_reminders.py`, `test_confirmations.py`) directly against `git show v0.6.0:tests/<file>`, line by line (`diff -u` + manual read of every changed `assert` line), rather than trusting either IMPL.md's or the module Vera's own characterization. M1 never got its own scoped Vera pass (flagged explicitly in this task's brief), so its audit is folded fully into this one.

**Result: no weakened assertion found anywhere in the audited set, with one coverage-gap exception, fixed below.**

- `test_fallback.py`, `test_resilience.py`, `test_v060_bilingual_gaps.py`, `test_confirmations.py`, `test_reminders.py`, `test_db.py`, `test_cli.py`: every changed line is a mechanical call-shape/accessor-shape update (`ExtractionResult(cat, water_ml, stretch_min, diary_text, conf)` → `(cat, value, conf)`; `compute_weekly_stats(db, config, end_date)` → `(db, config, registry, end_date)` + `stats.water_total_ml` → `stats.get("water").total`; `chat_json(sys, usr, schema)` → `(..., valid_categories)`; etc.) — every literal expected *value* (string, number, boolean) is byte-for-byte identical to its v0.6.0 counterpart. Two tests were correctly *unskipped* (`test_reminders.py::test_async_main_registers_weekly_review_job_from_config`, `test_confirmations.py::test_end_to_end_water_confirmation_with_mocked_ollama`) once their blocking boundary closed — a strengthening, not a weakening.
- `test_commands.py`: the `ADVERSARIAL_MESSAGES` false-positive corpus (10 entries, AC5.5) is byte-for-byte identical, in the same order, to `git show v0.6.0`'s copy — confirmed by direct diff of the list literal, not just "the test still exists." The 3 migration-count assertions Luna fixed (`3`→`4`/`len(MIGRATIONS)`) are legitimate (migration count is now genuinely 4). New AC12 sections are pure additions.
- `test_prompts.py` (new file, M1): covers the default registry, an added `sleep` habit, boolean/text synthetic habits, and the schema-flat/prompt-grows contrast, matching AC8 exactly.
- **One coverage gap found and fixed:** `test_parser.py`'s old `test_unit_constants_are_configurable` (v0.6.0) proved *configurability* by asserting **non-default** injected values (`glass_ml=450`, `bottle_ml=900`) reached the prompt. Its v0.7 replacement, `test_registry_unit_aliases_reach_the_prompt`, only ever asserts the **default** registry's own values (`250`/`600`) reach the prompt — every test in the file (including the new `test_prompts.py`) makes the same choice. This is a real regression in what's proven: a bug that hardcoded `"250"`/`"600"` into the prompt template instead of reading `habit.unit_aliases` would pass every existing v0.7 test. Not a wrong *value* (nothing asserts a false thing), but a silently narrower *guarantee* than the test it replaced — exactly the "smell" this task's brief asked me to rewrite. **Fixed:** added `tests/test_parser.py::test_arbitrary_custom_unit_alias_value_reaches_the_prompt`, which builds a synthetic habit with a deliberately non-realistic alias multiplier (`{"cup": 337}`) and asserts `"337"` reaches the generated prompt — restoring the original test's configurability guarantee under the v0.7 registry-driven contract. Verified failing against a hypothetical hardcoded-250/600 implementation by inspection (the real implementation reads `habit.unit_aliases` generically, so it passes); verified passing against the shipped `llm/prompts.py`.

No other smell found. `bool_status_done`/`_not_done`/`recovered_text` (additive i18n entries beyond SPEC-v0.7.md §5's illustrative list, per `IMPL.md` Known limitation 2) are exercised by existing tests (`test_confirmations.py`'s boolean AC9 cases) and don't need dedicated new coverage beyond what's already there.

## AC coverage — SPEC-v0.7.md §8 (AC1–AC17)

| AC | Track | Verdict | Evidence |
|---|---|---|---|
| AC1 (config/HabitConfig validation) | shared | PASS | `test_config.py` (defaults reproduce builtins, 9 validator-rejection cases, `ConfigError` e2e) |
| AC2 (HabitRegistry) | shared | PASS | `test_habits.py` (16 tests) |
| AC3 (migration 004) | shared | PASS | `test_migrations.py::test_v3_shaped_db_migrates_to_v4_with_habit_type_backfilled` (byte-for-byte row preservation, idempotent) + this pass's `test_multi_habit_integration.py::test_migration_004_backfilled_legacy_rows_aggregate_alongside_new_habit_rows` |
| AC4 (generic aggregations) | shared | PASS | `test_db.py` (`sum_value`/`count`/`count_true`, soft-delete exclusion, wrapper parity) |
| AC5 (flat-size schema) | shared | PASS | `test_extraction_schema.py` (3-vs-30-habit size independence) |
| AC6 (category matching / fail-closed) | M1 | PASS | `test_parser.py` (56 tests, audited above) |
| AC7 (per-type validation) | M1 | PASS | `test_parser.py`'s per-type validation section (numeric/duration ≤0, text empty, boolean truthy/falsy/uncoercible, confidence gate) |
| AC8 (dynamic prompt) | M1 | PASS | `test_prompts.py` (5 tests) + this pass's `test_arbitrary_custom_unit_alias_value_reaches_the_prompt` (gap fix) |
| AC9 (built-in byte-identical + generic templates) | shared | PASS | `test_confirmations.py` (built-in cases unchanged + 9 new AC9 generic-template cases matching SPEC §3.2 verbatim), `test_bilingual_confirmations.py` |
| AC11 (zero-code new habit, e2e) | integration | PASS | this pass's `test_multi_habit_integration.py` (18 tests) + live smoke (below) |
| AC12 (edit generalization) | M1 | PASS | `test_commands.py` AC12 section (exact SPEC examples, garbled-tail corpus, ambiguous-unit first-match) |
| AC13 (reminders byte-identical) | M2 | PASS | `test_reminders.py` (per `TEST-v0.7-M2.md`, independently re-verified there against the live catalog, not just Luna's claim) |
| AC14 (new-habit reminders) | M2 | PASS | `test_reminders.py` (per `TEST-v0.7-M2.md`) + this pass's `test_multi_habit_integration.py` (generic-template branch, the sibling of M2's custom-`reminder_text` branch) |
| AC15 (review byte-identical) | M3 | PASS | `test_review.py` + `TEST-v0.7-M3.md`'s independent byte-identical reconstruction against the real `git show v0.6.0:core/review.py` (character-for-character match, both languages) |
| AC16 (generic review + AC7.5 rows) | M3 | PASS | `test_review.py`, `test_v07_m3_review_extra.py` (8 tests) + this pass's migration-alongside-new-rows test |
| AC17 (composite byte-identical) | integration | PASS | full suite (463/1/0) + this pass's dedicated `test_ac17_v060_byte_identical_composite.py` (12 tests, literals pinned from `git show v0.6.0`, driven through the real pipeline, not derived from the current catalog) |

## AC coverage — ROADMAP.md v0.7.0 (AC7.1–AC7.5), the release-gate criteria

| ROADMAP AC | Meaning | Maps to (SPEC-v0.7.md) | Verdict | Evidence |
|---|---|---|---|---|
| **AC7.1** | Default config (water/stretch/diary) is byte-identical to v0.6.0 across confirmations, reminders, and the weekly review, jointly | AC9, AC13, AC15, AC17 | **PASS** | Pre-v0.7 corpus passes unmodified in assertion value (audited above); M3's independent v0.6.0-module reconstruction (`TEST-v0.7-M3.md`); this pass's `test_ac17_v060_byte_identical_composite.py` — 12 tests, literals hand-copied from `git show v0.6.0`, run through the real (not bypassed) parser/confirmation chain |
| **AC7.2** | Adding a habit via `[[habits]]` config requires zero code changes and works end to end (parse, store, confirm, remind, review) | AC11 | **PASS** | `test_multi_habit_integration.py` — 18 tests covering every stage listed, for both a numeric+goal (`sleep`) and boolean (`meds`) habit added purely as config data; live-Ollama smoke below independently confirms the extraction stage against the real model |
| **AC7.3** | Extraction (schema, prompt, category matching) is generated from the registry, not hardcoded per habit | AC5, AC6, AC8 | **PASS** | `test_extraction_schema.py`, `test_parser.py`, `test_prompts.py`; one coverage gap found and closed (custom-alias configurability, see audit above) |
| **AC7.4** | Per-type validation (numeric/duration/text/boolean) replaces the old per-category validation, with no regression | AC7 | **PASS** | `test_parser.py`'s per-type section, audited line-by-line against `SPEC-v0.7.md` §4 R8 — all cases present, no value drift from the v0.6.0-carried-forward subset |
| **AC7.5** | Storage/migration generalizes without losing or misclassifying historical rows | AC3, AC4, AC16 | **PASS** | `test_migrations.py` (byte-for-byte row preservation across the 3→4 migration, idempotent), `test_review.py::test_pre_v070_rows_with_null_habit_type_aggregate_correctly`, this pass's migration-alongside-new-rows test (legacy-backfilled and brand-new rows aggregate together in one `compute_weekly_stats` call, not just independently) |

## AC17 composite byte-identical — verification detail

Per this task's explicit instruction, the expected strings in `test_ac17_v060_byte_identical_composite.py` are typed in by hand from `git show v0.6.0:src/habit_assistant/core/i18n.py`'s catalog templates (formatted by hand with the scenario's known parameters), not produced by calling the *current* `i18n.t(...)` or any other current-code helper — confirmed by inspection of the test file (no `i18n` import, no catalog lookup anywhere in it). The 12 scenarios (water: en-plain, th-glass-alias, en-bottle-alias, th-bottle-alias; stretch: en, th; diary: en, th; unknown: en, th; undo: en; edit: en) each drive the real `handle_inbound_message` → `parse_message` → registry-built schema/prompt → real `core/parser.py._validate` chain, with only the network boundary (`httpx.MockTransport` under a real `OllamaClient`) mocked — not `main.parse_message` itself, so this is strictly stronger evidence than the pre-existing corpus's `patch_parse_message` monkeypatch style. All 12 pass.

Separately, `TEST-v0.7-M3.md` already did an even deeper version of this for the review path specifically: it loaded the actual `git show v0.6.0:core/review.py` module via `importlib` and ran it side by side against the current module on identically-seeded databases, comparing output character-for-character in both languages — also `MATCH` in every case. Between that and this pass's confirmation/undo/edit coverage, AC7.1's "jointly byte-identical" claim now has direct evidence for every one of confirmations, undo, edit, and review — reminders' byte-identical claim is independently verified in `TEST-v0.7-M2.md` against the live catalog.

## AC11 zero-code new habit — verification detail

`tests/test_multi_habit_integration.py`'s `SLEEP`/`MEDS` `HabitConfig` instances are the only "new" things in the file — every production import (`parse_message`, `handle_inbound_message`, `schedule_reminders`, `send_reminder`, `compute_weekly_stats`, `run_weekly_review`, `dispatch`) is used completely unmodified. The file covers, per the task's explicit checklist:

- **Extraction (mocked LLM):** real `parse_message` against the real registry-built schema/prompt (network boundary mocked), for both `sleep` (Thai, numeric) and `meds` (English, boolean).
- **Logging:** DB rows confirmed to carry `category='sleep'`/`habit_type='numeric'`/`value_num=7.0` and `category='meds'`/`habit_type='boolean'`/`value_num∈{0.0,1.0}`.
- **Confirmation, generic templates, both languages:** `sleep`'s en/th strings match `SPEC-v0.7.md` §3.2's own worked example verbatim (`✅ 7 h logged — today 7 / 8 h (88%)` / `✅ บันทึกนอน 7 ชม. แล้ว — วันนี้ 7 / 8 ชม. (88%)`); `meds`'s English string matches §3.2's `meds` example verbatim (`✅ meds — done today`); Thai boolean confirmed too.
- **Reminder job registration:** `schedule_reminders` registers `reminder_sleep_07:00` and `reminder_meds_09:00` alongside the untouched 9 built-in jobs; both fire through the generic `reminder_generic` template (the sibling of `TEST-v0.7-M2.md`'s AC14 coverage, which used a custom `reminder_text` — this pass deliberately covers the no-`reminder_text` branch instead of duplicating it).
- **Weekly review inclusion:** both `compute_weekly_stats` (per-habit numbers) and `run_weekly_review` (full rendered text, both languages) include `sleep`/`meds` alongside the three built-ins, via the generic `stats_generic_numeric_total`/`stats_generic_count_summary` templates.
- **Undo/edit where type-appropriate:** `sleep` (numeric+goal) supports both undo and edit (generic templates verified byte-for-byte against the catalog's own format strings); `meds` (boolean) supports undo only — confirmed edit is correctly *never* dispatched for a boolean habit (`core/commands.py`'s unit-lookup only ever includes numeric/duration habits, per `SPEC-v0.7.md` §10's explicit "editing text/boolean is out of scope").
- **Migration-004 backfill aggregating alongside:** a hand-built v3-shaped DB (pre-v0.7 schema, no `habit_type` column) with real legacy water rows is opened through the real `Database` class (migrates 3→4, backfills `habit_type='numeric'`), then `sleep`/`meds` rows are inserted through the same now-migrated DB; one `compute_weekly_stats` call aggregates the migrated-legacy water total (`800.0` = `500+300`) and the brand-new `sleep` total (`13.0` = `7+6`) and `meds` done-day count (`1`) together, confirming the backfill isn't inert — it participates in the same aggregation query path new-habit rows do.

## Live smoke (Ollama)

Ollama was reachable this session (`curl http://mac-mini:11434/api/version` → `{"version":"0.32.6"}`, matching M1's own earlier live check). Ran 3 real extractions through the real `parse_message`/`OllamaClient` against the real `config.toml`'s model chain (`qwen3.5:9b-mlx` → `qwen3:8b`), using an in-memory registry only (never opened `data/habits.db`, never touched `.env`, no Telegram):

| Input | category | value | confidence |
|---|---|---|---|
| `"500ml"` | `water` | `500.0` | `0.98` |
| `"did 10 min stretch"` | `stretch` | `10.0` | `0.95` |
| `"นอน 7 ชม."` (sleep, zero code change) | `sleep` | `7.0` | `0.95` |

All 3 correct. The `sleep` result is the direct, live confirmation of AC7.2/AC11's central claim: a habit that exists only as in-memory `HabitConfig` data, added to a registry built from the real loaded config, parses correctly against the real model with no code change anywhere. M1's `IMPL-v0.7-M1.md` already live-verified 8/8 cases (including a live-discovered-and-fixed diary-prompt wording issue, re-verified 5/5 after the fix) during its own module pass; this pass's 3 cases are corroborating spot-checks with fresh inputs, not a re-run of the same evidence, per the "Ollama intermittent, don't block on it" guidance — it happened to be reachable this session so the check was run rather than skipped.

## Failures (if any)

None.

## Regressions detected

None. Full suite: 463 passed, 1 skipped (the documented pre-existing v0.1.0-era skip), 0 failed. No test that passed before this pass now fails; no assertion value was weakened (see audit above; the one coverage gap found was closed, not left open).

## Live-environment / safety checks

- Production bot (PID 13956) confirmed running via `tasklist` both before and after all work in this pass.
- `data/habits.db` mtime unchanged throughout (all tests use `tmp_path`; the live-smoke script above never imports `storage.db.Database` or touches `config.app.db_path`).
- `.env` not read or written by this pass.
- No real Telegram call made anywhere (no `TelegramChannel` instantiated in this pass's new tests or scripts).
- No git commit made (per instruction). `git status` at the end of this pass shows only Luna's pre-existing `src/`/test-file modifications (already present at session start) plus this pass's 2 new test files and the 1-test addition to `test_parser.py` — no production file touched by this pass, consistent with Vera's role.

## Recommendation

**Ready to ship — v0.7.0 Multi-Habit Extensibility, overall status PASS.** All 17 SPEC-v0.7.md ACs and all 5 ROADMAP.md AC7.1–AC7.5 release-gate criteria are green, with direct test evidence (not just "the module Vera said so") for every one, including two ACs (AC11, AC17) that had no dedicated end-to-end test before this pass and now do. The old→new test-rewrite audit covering M1 (which never got its own scoped Vera pass) plus every file the integration Luna touched found no weakened assertions and exactly one coverage-gap smell, which was fixed in place. Full suite: 463 passed / 1 skipped (pre-existing, documented) / 0 failed. Production bot, live DB, and `.env` confirmed untouched throughout; live Ollama smoke corroborates the central AC7.2 claim against the real model. No blockers for Archi to proceed to release (version bump, `PROGRESS.md` update, commit + tag).

---

# Test Report — v0.8.0 Natural-Language Queries

> Scope: ROADMAP.md §"v0.8.0" (AC8.1–AC8.5), tested against Luna's uncommitted working-tree changes (`git diff v0.7.0 -- src tests` / `git status --porcelain`: `core/commands.py`, `core/i18n.py`, `llm/prompts.py`, `main.py`, `tests/test_commands.py` modified; `core/query.py`, `tests/test_query.py` new). Not committed (per task instruction — v0.7.0 is still the latest tag).

## Summary

- Baseline (v0.7.0, before this task's own additions): **508 passed, 1 skipped** — confirmed by re-running `.venv\Scripts\python.exe -m pytest -q` against Luna's tree as handed off, matching `IMPL.md`'s own claimed number exactly.
- Luna's own v0.8.0 tests: 45 (25 in `tests/test_query.py`, 20 in the new "ROADMAP.md v0.8.0" section of `tests/test_commands.py`) — all reviewed line-by-line, all pass.
- Diff audit: confirmed **zero existing tests modified** — `git diff v0.7.0 -- tests/test_commands.py` shows only a new `import json` line and pure additions (new `QUERY_MESSAGES`/test functions/`_StaticQueryLLM` class appended after the existing suite); no existing assertion, fixture, or test body touched. `tests/test_query.py` is entirely new. Claim verified, not just trusted.
- Vera's supplementary gap tests (this pass): **26 new**, in `tests/test_v08_query_gaps.py` (see "Focus areas" below for what they close).
- **Total this pass: 534 passed, 1 skipped, 0 failed** (508 baseline + 26 new; Luna's 45 are already inside the 508). The 1 skip is the same pre-existing v0.1.0-era `tests/test_channels.py:231` skip.
- **Status: PASS.**

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_query.py` (Luna) | 25 | AC8.1, AC8.2, AC8.3, AC8.4, AC8.5 |
| `tests/test_commands.py` (Luna, new section) | 20 | AC8.1–AC8.4 routing/detection (query-vs-log classification), precedence ordering |
| `tests/test_v08_query_gaps.py` (Vera, this pass) | 26 | AC8.3 (midnight-boundary bucketing), AC8.4 (transport exception, non-dict payload, off-schema keys, Thai can't-answer), AC8.5 (write-method spy), false-positive sweep, undo/edit-before-query precedence, documented trailing-`?` grammar, full precedence chain incl. LLM-down |

## AC coverage

| AC | Requirement | Verdict | Evidence |
|---|---|---|---|
| **AC8.1** | "how much water this week?" → correct 7-day sum from seeded DB, English | **PASS** | `test_query.py::test_ac81_how_much_water_this_week_english` (exact boundary: seeded row one day outside the window excluded); live Ollama spot-check below, 4/4 correct this session |
| **AC8.2** | "อาทิตย์นี้ยืดกี่ครั้ง" → correct weekly stretch count, Thai | **PASS** | `test_query.py::test_ac82_weekly_stretch_count_thai` (exact boundary, Thai output byte-matched against catalog) |
| **AC8.3** | Timeframes (today/yesterday/this week/last 7 days, + Thai) map to correct date ranges, `Asia/Bangkok`-aware | **PASS** | `test_query.py`'s 6 timeframe tests + `test_v08_query_gaps.py::test_log_at_2350_yesterday_and_0010_today_bucket_correctly` (new: two log rows either side of local midnight land in disjoint today/yesterday buckets, not just "today stays today" as Luna's own boundary test proved) + `test_date_range_for_timeframe_today_and_yesterday_are_disjoint` + `test_clock_injection_makes_the_boundary_deterministic_across_repeated_calls` (3x identical output, same fixed clock — not flaky by construction; see "Clock injection" note below) |
| **AC8.4** | Unconfigured habit or unparseable question → friendly can't-answer, never a wrong number, never a crash | **PASS** | Luna's 5 can't-answer cases (unknown category, unconfigured habit id, malformed JSON, bad-status transport, invalid metric) + this pass's 4 additions: a genuine `httpx.ConnectError` (transport-level exception, not just a bad status code — `test_transport_exception_yields_cant_answer_not_a_crash`), a syntactically-valid-JSON-but-wrong-shape bare list (`test_llm_returns_a_json_array_instead_of_object_yields_cant_answer`), an off-schema-keys object (`test_llm_returns_extra_and_missing_keys_yields_cant_answer`), and a **Thai-language** can't-answer confirmation (`test_thai_unconfigured_habit_question_yields_thai_cant_answer` — Luna's 5 can't-answer tests were all English input). Live Ollama spot-check (below) independently confirms two unconfigured-habit adversarial cases (English + Thai "coffee", an untracked habit) both correctly fail closed against the real model, not just the mocked transport. |
| **AC8.5** | Query handling is strictly read-only — no `logs` row written by a query | **PASS** | Luna's 3 row-count tests (success/failure/5-repeats) + this pass's stronger structural proof: `write_spy_db` fixture monkeypatches all 4 `Database` write methods (`insert_log`, `soft_delete`, `update_value`, `reclassify_log`) to raise `AssertionError` if called at all, exercised across a successful query, a failed query, a transport-error query, and a direct `answer_question` call — none raised, i.e. none of the 4 write methods is *reachable* from the query path, not merely "happened to net out to zero rows" |

Every AC8.1–8.5 is **PASS**. No untestable/ambiguous AC found — nothing escalated to Sophia.

## Focus-area findings (per this task's brief)

1. **AC8.1/8.2 end-to-end** — already solid in Luna's own suite (real `handle_inbound_message`, mocked LLM via `httpx.MockTransport` over a real `OllamaClient`, seeded temp DB, exact catalog-template string match). No gap found; not re-duplicated.
2. **AC8.3 midnight boundary** — genuine gap closed. Luna's own timezone test (`test_answer_uses_the_configured_timezone_not_utc`) proved a single 23:30-Bangkok timestamp stays "today" rather than rolling to UTC's next day, but never proved two entries *either side* of local midnight land in *different* buckets. Added `test_log_at_2350_yesterday_and_0010_today_bucket_correctly` (seeds 23:50-Aug-18 and 00:10-Aug-19 water rows, queries both "today" and "yesterday", asserts each returns only its own row). Clock injection is already fully supported (`clock=` kwarg threaded from `handle_inbound_message` → `query.answer_question` → `_today_in_timezone`) and deterministic — no flakiness risk to flag; added a 3x-repeat determinism test as a regression guard.
3. **AC8.4 adversarial** — closed 4 real gaps: a genuine transport-level exception (`httpx.ConnectError`, distinct from Luna's bad-HTTP-status-503 case — exercises `OllamaClient._post`'s exception path, not its status-code path), a non-dict JSON payload (bare list — exercises `_validate_intent`'s `isinstance(data, dict)` guard directly), an off-schema-keys object, and a Thai-language can't-answer confirmation (Luna's 5 can't-answer tests were all English-input). All 4 collapse to the same bilingual `query_cant_answer` fallback, never a number, never an unhandled exception. Live-model spot-check (below) independently confirms the unconfigured-habit case both in English and Thai.
4. **AC8.5 write-method reachability** — closed the gap between "row count nets out unchanged" (Luna's tests) and "no write method is *reachable at all*" (this task's ask): a monkeypatched spy on all 4 `Database` write methods, raising if touched, passes clean across success/failure/transport-error/direct-call paths.
5. **False-positive sweep (other direction)** — `"500ml"` and `"ดื่มน้ำ 2 แก้ว"` are already covered by Luna's reused `ADVERSARIAL_MESSAGES` corpus (both in `test_commands.py` and cross-checked again in `test_v08_query_gaps.py::NOT_QUERY_MESSAGES`). Added further prose cases with a bare "how"/"did" that don't satisfy the anchored `\bhow\s+(much|many)\b` / `\b(did|have|has)\s+i\b` patterns (e.g. `"not sure how I feel about tomorrow"`, `"did it rain today"`) — all correctly `None` (not routed as query). **Thai "กี่" mid-sentence:** could not construct a genuine false positive — unlike English "how", Thai "กี่" has no non-interrogative reading in ordinary usage (it *is* the "how many" question particle), so any occurrence is, by the language's own grammar, correctly question-shaped; documented as such rather than silently skipped (`test_thai_question_particle_ki_always_signals_a_question_by_design`). **Undo/edit precedence:** proved structurally (not just by absence of a naturally-overlapping message, since undo/edit's own trigger patterns are mutually exclusive with query's anchors by construction) by monkeypatching `_match_query` to always return `True` and confirming `dispatch()` still returns `kind="undo"`/`"edit"` for genuine undo/edit messages — 3 tests, English undo, edit, and Thai undo. **Trailing "?" on a log-like message:** confirmed it *does* route as query (`"should I go for a run tomorrow?"` → `Command(kind="query")`, then end-to-end → the can't-answer fallback, never logged as a diary row). **Grammar/documentation check: this matches IMPL.md exactly** — `IMPL.md`'s v0.8.0 "Known limitations" #5 explicitly documents "a trailing `?`/`？` alone is sufficient to route a message to the query path" and calls out the diary-ends-in-`?` trade-off by name. Held the code to what's documented per this task's instruction: **no grammar/behavior mismatch found** — this is intended, documented behavior, not a FAIL. (It is, however, a real UX trade-off worth Archi/the user knowing about if diary entries commonly end in questions — already flagged by Luna, not new.)
6. **Precedence chain overall** — `commands.dispatch()`'s own ordering (undo → edit → query → fall-through) is structurally verified above. At the `handle_inbound_message` level, added `test_query_is_answered_not_deferred_while_ollama_is_reported_down`: with `health_monitor.ollama_up=False` *and* a genuinely-failing LLM transport (`httpx.ConnectError`), a query is answered with the can't-answer fallback — **not** deferred to the v0.4.0 unparsed-queue path (`channel.sent != [DEFERRED_ACK_MESSAGE]`, `db.pending_unparsed() == []`). This matches `main.py`'s own code structure (the `command.kind == "query"` branch returns before the `health_monitor.ollama_up` check is ever reached) and is exactly the intended behavior per the task brief ("it's read-only and shouldn't defer") — **it does not defer.** A companion test (`test_query_answers_successfully_while_ollama_is_reported_down_if_llm_call_itself_succeeds`) confirms the distinction holds even when the down-flag is stale (LLM call itself succeeds): the real answer is returned, not a deferral or a false can't-answer.

## Live Ollama supplementary checks

Ollama was reachable this session (`curl http://mac-mini:11434/api/version` → `{"version":"0.32.6"}`). Ran `classify_query_intent` through the real model chain against the real `config.toml` registry (read-only interpreter script, no `data/habits.db`, no `.env`, no Telegram):

| Input | Result | Correct? |
|---|---|---|
| `"how much coffee did I drink this week?"` (unconfigured habit, English) | `None` | Yes — AC8.4 |
| `"วันนี้กินกาแฟไปกี่แก้ว"` (unconfigured habit, Thai) | `None` | Yes — AC8.4 |
| `"should I go for a run tomorrow?"` (trailing-`?`, not about tracked data) | `None` | Yes — consistent with the documented grammar |
| `"how much water this week?"` (AC8.1 sanity) | `QueryIntent(habit_id='water', metric='sum', timeframe='this_week')` | Yes — AC8.1 |

4/4 correct. This independently corroborates Luna's own live spot-check (which covered AC8.1/AC8.2 3/3 each) with the two adversarial unconfigured-habit cases (English and Thai) Luna's live pass didn't specifically target, plus the trailing-`?` grammar case.

## Failures (if any)

None.

## Regressions detected

None. Full suite: 534 passed, 1 skipped, 0 failed. `git diff v0.7.0 -- tests/test_commands.py` confirmed by direct inspection to contain zero deletions/modifications to pre-existing test bodies — the "0 existing tests modified" claim in `IMPL.md`'s "Smoke test done" is verified, not assumed.

## Live-environment / safety checks

- Production bot (PID 3264) confirmed running (`Get-Process -Id 3264`) both before and after this pass.
- `data/habits.db` / `.env` `LastWriteTime` (2026-08-19 17:35) unchanged across this pass — every test in `test_v08_query_gaps.py` and Luna's `test_query.py` uses a `tmp_path`-scoped `Database`; the live spot-check script never imports/constructs a `Database` against the real config path.
- No real Telegram call made anywhere (no `TelegramChannel` instantiated by any new test or script).
- No git commit made (per instruction) — `git status` still shows only Luna's pre-existing uncommitted changes plus this pass's new `tests/test_v08_query_gaps.py`.

## Recommendation

**Ready to ship — v0.8.0 Natural-Language Queries, overall status PASS.** All 5 ROADMAP.md AC8.1–AC8.5 are green with direct test evidence, including several failure modes (genuine transport exception, non-dict JSON, Thai can't-answer, write-method-reachability spy, LLM-down precedence) beyond what Luna's own 45 tests already covered. One documentation/behavior cross-check was explicitly performed per the task brief (trailing-`?` routing) and found **no mismatch** — `IMPL.md` documents the exact behavior the code exhibits. No spec gaps, no untestable ACs, no regressions. Full suite: 534 passed / 1 skipped (pre-existing, documented) / 0 failed. Production bot, live DB, and `.env` confirmed untouched throughout. No blockers for Archi to proceed to release.

---

# Test Report — v0.9.0 Adaptive Reminders, Snooze & Quiet Hours

> Scope: ROADMAP.md §"v0.9.0" (AC9.1–AC9.5), tested against Luna's uncommitted working-tree changes (`git diff v0.8.0 -- src tests config.toml`: `config.py`, `config.toml`, `core/habits.py`, `core/reminders.py`, `core/commands.py`, `core/i18n.py`, `main.py`, `tests/test_commands.py`, `tests/test_config.py`, `tests/test_reminders.py` modified; `tests/test_adaptive_reminders.py` new). Not committed (per task instruction — v0.8.0 is still the latest tag).

## Summary

- Baseline (v0.8.0, before this task's own additions): **583 passed, 1 skipped** — confirmed by re-running `.venv\Scripts\python.exe -m pytest -q -rs` against Luna's tree as handed off, matching `IMPL.md`'s own claimed number exactly.
- Luna's own v0.9.0 tests: 49 (14 in `tests/test_adaptive_reminders.py`, 8 in `tests/test_config.py`, 27 in `tests/test_commands.py`) — all reviewed line-by-line, all pass. They exercise the adaptive checks and snooze at the **unit** level (`send_reminder(...)` called directly with `db=`/`config=`; `_execute_snooze` via a `_FakeSnoozeScheduler` that only records `add_job` calls, never actually fires a job).
- Diff audit: confirmed **exactly 2** pre-existing test expectations changed, matching `IMPL.md`'s own claim precisely — `git diff v0.8.0 -- tests/test_reminders.py` shows only `test_schedule_reminders_job_args_bind_correct_habit_and_language` and `test_schedule_reminders_adds_one_job_for_a_new_habit_with_reminder_times`, each with `job.args` widened from a 3-tuple `(channel, habit, lang)` to a 6-tuple `(channel, habit, lang, None, config, None)` — a direct, correct consequence of `schedule_reminders` now binding `db`/`config`/`state`. No other assertion in that file (or anywhere else in the pre-existing suite) touched. Claim verified, not just trusted.
- Vera's supplementary gap tests (this pass): **28 new**, in `tests/test_v09_gaps.py` (see "Focus areas" below for what they close) — the real-send-path, real-scheduler, and audit/false-positive gaps Luna's unit-level suite didn't hit.
- **Total this pass: 611 passed, 1 skipped, 0 failed** (583 baseline + 28 new; Luna's 49 are already inside the 583). The 1 skip is the same pre-existing v0.1.0-era `tests/test_channels.py:231` skip.
- **Status: PASS.**

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_adaptive_reminders.py` (Luna) | 14 | AC9.1, AC9.2, AC9.4, AC9.5 (unit-level: quiet-hours boundary math, goal-met skip/send/exactly-met, `skip_if_goal_met=False` override, fail-open, backward-compat, `ReminderState` update-on-send-only) |
| `tests/test_commands.py` (Luna, new section) | 27 | AC9.3 (snooze detection incl. 11 bilingual phrasings, adversarial-corpus non-match, 6 `_FakeSnoozeScheduler`-based `main.py` integration tests) |
| `tests/test_config.py` (Luna, new tests) | 8 | AC9.2/AC9.4 config surface (`QuietHoursConfig`, `SnoozeConfig`, `HabitConfig.skip_if_goal_met`, end-to-end TOML load) |
| `tests/test_v09_gaps.py` (Vera, this pass) | 28 | AC9.1 (real scheduled-job path + logged skip), AC9.2 (real-job midnight/multi-window/boundary + snoozed-followup-in-quiet-hours), AC9.3 (fired-vs-logged target disambiguation, real one-shot job lifecycle), AC9.4 (two-habit differential), AC9.5 (real-job fail-open, zero-write spy, scheduler continuity), backward-compat audit, false-positive sweep |

## AC coverage

| AC | Requirement | Verdict | Evidence |
|---|---|---|---|
| **AC9.1** | A reminder for a goal-bearing habit whose daily goal is already met is not sent (logged as skipped); when not met, it is sent | **PASS** | Luna: `test_adaptive_reminders.py::test_send_reminder_skipped_when_goal_already_met` / `test_send_reminder_sent_when_goal_not_met` / `test_send_reminder_goal_exactly_met_is_skipped` (direct `send_reminder(...)` calls). Vera: `test_v09_gaps.py::test_goal_met_reminder_skipped_via_real_scheduled_job_and_logged` / `test_goal_not_met_reminder_sent_via_real_scheduled_job` / `test_goal_exactly_met_is_skipped_via_real_scheduled_job_matching_documented_ge` — through `schedule_reminders`' real registered job (`scheduler.get_job(...)` then `await job.func(*job.args)`, the same call shape APScheduler itself uses), with `caplog` asserting the skip is actually logged ("goal already met" in the log record), not just silent. Boundary held to the documented `>=` contract (`core/reminders.py:_goal_already_met`), not re-derived. |
| **AC9.2** | Any reminder whose fire time falls inside a quiet-hours window is suppressed, including windows crossing midnight | **PASS** | Luna: `test_in_quiet_hours_same_day_window` / `test_in_quiet_hours_midnight_crossing_window` / `test_in_quiet_hours_no_windows_never_suppresses` (unit) + 2 integration tests (frozen clock, direct `send_reminder` call). Vera: `test_midnight_crossing_window_suppresses_only_inside_via_real_job` (parametrized 23:30/06:30 suppressed, 12:00 not, through a real scheduled job) + `test_multiple_quiet_hours_windows_each_suppress_independently` (two simultaneous windows, 4 time points) + `test_same_day_window_boundary_is_half_open_via_real_job` (parametrized: 13:00 suppressed/start-inclusive, 12:59 not, 14:00 not/end-exclusive, 13:59 suppressed — matches `_in_quiet_hours`'s own documented `[start, end)` contract) + `test_snoozed_followup_is_also_suppressed_when_it_lands_in_quiet_hours` (schedules a real snooze follow-up job, freezes the reminders-module clock to 23:30 at the moment the job actually fires, confirms only the snooze confirmation was ever sent — the follow-up text itself was suppressed). |
| **AC9.3** | "snooze 30" / "เลื่อน 30 นาที" schedules a single follow-up reminder ~30 min later for the relevant habit; fires once, does not recur | **PASS** | Luna: 11 bilingual dispatch-classification tests + 6 `main.py`-integration tests via `_FakeSnoozeScheduler` (no-prior-reminder fallback, explicit/default minutes, Thai confirmation + `run_date`, dry-run, LLM-free). Vera: `test_snooze_targets_most_recently_fired_reminder_not_most_recently_logged_habit` (fires water's reminder via `send_reminder` to set the target, then logs an unrelated stretch entry through the normal inbound path — confirms `ReminderState` is untouched by the plain log, and the subsequent snooze still targets water, not stretch — the exact "fired vs. logged" distinction the brief called out) + `test_snooze_scheduled_job_fires_once_and_is_removed_from_scheduler` (a **real** `AsyncIOScheduler.start()`, the job actually fires via its `DateTrigger`, `scheduler.get_job(job_id)` confirmed `None` afterward — a genuine one-shot, not just an assertion on the trigger's type). |
| **AC9.4** | `skip_if_goal_met = false` for a habit disables adaptive skipping for that habit only | **PASS** | Luna: `test_send_reminder_skip_if_goal_met_false_disables_adaptive_skip` (single habit, direct call). Vera: `test_skip_if_goal_met_false_disables_only_that_habit_others_still_skip` — a 2-habit registry (water: override `False`, over goal; sleep: default `True`, also over goal), both reminders fired through real scheduled jobs against the same `db`: water's reminder sends, sleep's stays silent — proves the override is genuinely per-habit, not global, in the same registry/process. |
| **AC9.5** | Adaptive checks are read-only DB reads; no scheduler job crashes if the DB read fails (fail-open: send the reminder) | **PASS** | Luna: `test_send_reminder_fails_open_when_db_read_raises` (direct call, `_RaisingDatabase` stand-in). Vera: `test_db_read_raises_mid_check_reminder_still_sent_via_real_job_and_logged` (through a real scheduled job, `caplog` confirms the error is logged — "goal read failed" — not just swallowed silently) + `test_adaptive_checks_perform_zero_db_writes` (monkeypatched spy on all 4 `Database` write methods — `insert_log`/`soft_delete`/`update_value`/`reclassify_log` — none touched during a normal goal-met check) + `test_scheduler_keeps_processing_other_jobs_after_one_jobs_db_read_raises` (water's job raises-and-fails-open, a completely independent stretch job on the same scheduler still fires normally afterward — one job's DB hiccup doesn't poison the scheduler). |

Every AC9.1–9.5 is **PASS**. No untestable/ambiguous AC found — nothing escalated to Sophia.

## Focus-area findings (per this task's brief)

1. **AC9.1 real send path** — closed the gap between "the pure function skips correctly" (Luna) and "the thing APScheduler actually calls at 08:00 skips correctly, and logs it" (this task's ask). Fetched the real job APScheduler registered (`scheduler.get_job("reminder_water_08:00")`) and invoked it the same way the scheduler would (`await job.func(*job.args)`), with `caplog` asserting the skip is logged, not just silent. Boundary (`total >= habit.goal`) held to what `core/reminders.py:_goal_already_met` and `IMPL.md` both document — no independent reinterpretation.
2. **AC9.2 midnight-crossing + boundaries + multi-window** — Luna's unit tests already nail `_in_quiet_hours`'s pure math (including both midnight-crossing directions and the `[start, end)` half-open contract). Extended through the real job-firing path (not just the helper function) and added: 2 simultaneous windows firing independently, and — the requested extra — **the snoozed one-off is itself suppressed if it lands in quiet hours**. This last one required freezing `habit_assistant.core.reminders.datetime` (same technique Luna's own suite uses) to the moment the follow-up job actually executes, confirming the snoozed reminder is not a special case: it goes through the exact same `send_reminder` quiet-hours gate as a cron-triggered one, per `IMPL.md`'s own "How it works" narrative.
3. **AC9.3 snooze target correctness + job lifecycle** — closed 2 real gaps. First, the brief's specific concern that snooze could accidentally target "the most recently *logged* habit" instead of "the most recently *fired reminder*": constructed a scenario where water's reminder fires (updating `ReminderState`), then the user logs an unrelated stretch entry through the normal message path — confirmed `ReminderState.last_habit_id` is untouched by the log (only `send_reminder`'s own successful send updates it), and the next snooze still correctly targets water. Second, Luna's own 6 integration tests use a `_FakeSnoozeScheduler` that only *records* `add_job` calls — it never actually runs anything, so "fires once, does not recur" was asserted only by inspecting the trigger's type, not by observing a real firing. Added a test using a genuine `AsyncIOScheduler.start()` (with a `clock` engineered so the `DateTrigger`'s `run_date` lands ~300ms after `start()`, avoiding a real 30-minute wait) that lets the job actually fire and then confirms it's gone from `scheduler.get_jobs()` afterward, with the follow-up's own `send_reminder` side effect (the Thai water reminder text) observed in the channel.
4. **AC9.4 per-habit isolation** — Luna's test proves the override works for a single habit in isolation. Added a 2-habit scenario (one `skip_if_goal_met=False`, one left at the `True` default, both goal-met) scheduled and fired through the same registry/db/scheduler, proving the override doesn't leak — the other habit's default skip behavior is unaffected.
5. **AC9.5 fail-open + no writes + scheduler continuity** — Luna proves the read failure doesn't raise and doesn't suppress. Added: (a) the same proof through a real scheduled job with `caplog` confirming the failure is actually logged (not silently caught with no trace), (b) a write-method spy (same pattern as the v0.8.0 gap suite's AC8.5 check) proving the adaptive checks never call any of the 4 DB write methods even on a successful, non-raising read, and (c) a scheduler-continuity test showing one job's DB exception doesn't affect a second, independent job on the same scheduler instance.
6. **Backward-compatibility audit (`send_reminder`'s additive params)** — `test_send_reminder_three_arg_call_matches_v080_catalog_text_for_every_habit_shape` calls `send_reminder(channel, habit, "en")` with exactly the pre-v0.9 3-positional-arg shape, across all 5 text-resolution paths (`water`/`stretch`/`diary` built-ins, a custom `reminder_text` habit, and a type-generic fallback habit), seeding the DB with an over-goal water total that *would* suppress if `db`/`config` were passed — confirming the omitted-params path is a true no-op, not just "usually skips the check." Cross-checked against `git show v0.8.0:src/habit_assistant/core/reminders.py`: the pre-v0.9 body's text-resolution branch (`if habit.id in BUILTIN_IDS: ... else: ...`) is structurally unchanged in v0.9, and both new adaptive-check blocks are gated on `config is not None` (quiet hours) / `db is not None and config is not None` (goal-met) — so a 3-arg call provably executes neither block. The claim in `IMPL.md` ("no-ops unless passed — pre-v0.9 behavior byte-identical") holds.
7. **False-positive sweep** — `commands.dispatch()` on `"เลื่อนเวลานัดหมอ"` (postpone a doctor's appointment) and `"I snoozed my alarm today"` (a diary-shaped sentence), plus two additional constructed cases (`"ขอเลื่อนประชุมพรุ่งนี้ด้วยครับ"`, `"just hit snooze on my phone alarm twice"`), all correctly return `None`/non-`snooze` — `_SNOOZE_EN_RE`/`_SNOOZE_TH_RE` are whole-message-anchored (`^...$`), so "snooze"/"เลื่อน" appearing mid-sentence never matches, matching the same conservative philosophy already proven for undo/edit/query. Confirmed these same 4 messages still reach the parser exactly once through `handle_inbound_message` (not silently swallowed by the new command kind). **Precedence** (undo/edit → snooze → query) confirmed structurally — `test_dispatch_precedence_undo_edit_before_snooze_before_query` exercises one genuine example of each kind end-to-end; note (same as the v0.8.0 report's own finding) that undo/edit/snooze/query's trigger patterns don't naturally overlap by construction, so this is a regression guard against a future reordering rather than proof of an achievable ambiguous case today.

## Failures (if any)

None.

## Regressions detected

None. Full suite: 611 passed, 1 skipped, 0 failed. `git diff v0.8.0 -- tests/test_reminders.py` confirmed by direct inspection to contain exactly the 2 documented `job.args` tuple-shape updates and nothing else — the "0 other tests changed" claim in `IMPL.md`'s "Existing test expectation changes" section is verified, not assumed.

## Live-environment / safety checks

- Production bot (PID 4064) confirmed running (`Get-Process -Id 4064`) both before and after this pass.
- `data/habits.db` / `.env` `LastWriteTime` (2026-08-19 17:35) unchanged across this pass — every test in `test_v09_gaps.py` (and Luna's `test_adaptive_reminders.py`/`test_commands.py`) uses a `tmp_path`-scoped `Database` or no DB at all; nothing in this pass constructs a `Database` against the real config path.
- No real Telegram call made anywhere (no `TelegramChannel` instantiated by any new test; all channels are the local `FakeChannel` stand-in).
- No git commit made (per instruction) — `git status` still shows only Luna's pre-existing uncommitted changes plus this pass's new `tests/test_v09_gaps.py`.

## Recommendation

**Ready to ship — v0.9.0 Adaptive Reminders, Snooze & Quiet Hours, overall status PASS.** All 5 ROADMAP.md AC9.1–AC9.5 are green with direct test evidence through the real scheduled-job path (not just the unit-level function calls Luna's own 49 tests already covered), including: logged goal-met skips, midnight-crossing/multi-window/half-open quiet-hours boundaries verified via real job firing, a snoozed follow-up proven to still respect quiet hours, snooze-target correctness disambiguated from "most recently logged" vs. "most recently fired," a genuine one-shot job lifecycle observed end-to-end on a real `AsyncIOScheduler`, per-habit `skip_if_goal_met` isolation across a 2-habit registry, fail-open with logging plus a zero-DB-write spy plus scheduler continuity after a failure, a backward-compatibility audit pinning the additive-params claim against every habit text-resolution shape, and a false-positive sweep on the two specific diary-shaped sentences the brief named. The documented 2-test `tests/test_reminders.py` expectation change was independently re-diffed against `v0.8.0` and found to be exactly what `IMPL.md` claims — nothing else in the pre-existing suite was touched. No spec gaps, no untestable ACs, no regressions. Full suite: 611 passed / 1 skipped (pre-existing, documented) / 0 failed. Production bot, live DB, and `.env` confirmed untouched throughout. No blockers for Archi to proceed to release.

---

# Test Report — v0.10.0 Streaks, Gentle Gamification & Daily Summary

> Scope: ROADMAP.md §"v0.10.0" (AC10.1–AC10.5), tested against Luna's uncommitted working-tree changes (`git diff v0.9.0 -- src config.toml`: `core/streaks.py` new; `core/review.py`, `core/reminders.py`, `config.py`, `config.toml`, `core/i18n.py`, `main.py` modified). Luna wrote **zero** tests this round — deliberate and documented in `IMPL.md` ("Vera's streak tests (not yet written by this pass)"), only a manual interpreter smoke script. This entire report's evidence is Vera's own new `tests/test_streaks.py`. Not committed (per task instruction — v0.9.0 is still the latest tag).

## Summary

- Baseline (v0.9.0, before this task's own additions): **611 passed, 1 skipped** — matches `IMPL.md`'s own claimed smoke-test number exactly, confirmed by running the full suite before adding `test_streaks.py`.
- New tests this pass (Vera): **39**, all in `tests/test_streaks.py` (Luna added none — see scope note above).
- **Total this pass: 650 passed, 1 skipped, 0 failed** (611 baseline + 39 new). The 1 skip is the same pre-existing v0.1.0-era `tests/test_channels.py:231` skip.
- **Status: PASS.**

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_streaks.py` (Vera, new) | 39 | AC10.1 (15, incl. a 5-way parametrize), AC10.2 (3), AC10.3 (6), AC10.4 (5), AC10.5 (7, incl. a 6-way parametrize), regression (2), audit (1) |

## AC coverage — ROADMAP.md v0.10.0 (AC10.1–AC10.5)

| AC | Requirement | Verdict | Evidence |
|---|---|---|---|
| **AC10.1** | Per-habit streak counts consecutive days meeting the habit's condition (goal-met for goal habits, exact-goal counts), verified against seeded data; a gap resets it | **PASS** | `test_goal_habit_day_exactly_at_goal_qualifies` / `test_goal_habit_day_just_below_goal_does_not_qualify` / `test_goal_habit_day_above_goal_qualifies` (the `>=` boundary held exactly, incl. the "exactly at goal counts" wording). `test_nongoal_numeric_any_entry_qualifies_regardless_of_size` / `test_boolean_only_truthy_entry_counts_as_a_done_day` / `test_duration_any_entry_qualifies` / `test_text_type_any_entry_qualifies` (all 4 habit-type qualification rules). `test_compute_streak_gap_resets_across_all_habit_types` — parametrized (numeric-goal / numeric-nogoal / duration / boolean / text): 5 qualifying days with a 2-day gap, trailing run of 3 correctly isolated from an earlier run, for every type. `test_goal_habit_partial_today_does_not_qualify_but_past_run_is_preserved` / `test_nongoal_habit_partial_today_entry_still_qualifies` (today-partial semantics diverge correctly by habit shape, per `core/streaks.py`'s own docstring). `test_duration_multiple_sessions_same_day_counts_as_one_streak_day`. |
| **AC10.2** | Crossing a configured milestone appends exactly one encouragement line to the next confirmation (once per crossing, not repeated) | **PASS** | `test_milestone_crossing_sequence_3_then_no_repeat_then_7` — through the **real** `handle_inbound_message`: a log crossing day 3 appends the milestone line exactly once; a second log the same day (already-qualifying) appends nothing; days 4–6 seeded, day 7's log crosses again and appends its own line. `test_streak_reaching_a_non_milestone_number_produces_no_line` — a genuine crossing (day_qualifies DOES flip) that lands on streak 4 (not in `[3,7,30]`) produces no line, distinguishing "didn't flip" from "flipped but isn't a milestone." `test_milestone_line_is_thai_for_thai_input` — Thai input produces the Thai confirmation AND the Thai milestone line, exact string match via `i18n.t`. |
| **AC10.3** | The daily summary fires at the configured time with correct per-habit totals/goal% and current streaks, in the user's language | **PASS** | `test_run_daily_summary_content_default_thai` — 3 habit types (numeric+goal partial, duration multi-session, text) in one summary, exact-string match against every `daily_summary_*` catalog entry (Thai, primary-language default). `test_run_daily_summary_respects_forced_language_english` — same content mechanism, forced English. `test_daily_summary_includes_every_registered_habit_even_with_zero_entries` — zero-entry habits still render (0/0), per IMPL.md's documented "honest recap" choice. `test_async_main_registers_daily_summary_job_at_configured_time` — a custom `daily_summary_time="22:10"` produces a `CronTrigger(hour=22, minute=10)` registered as job id `daily_summary`, via the same `_FakeScheduler` pattern `test_reminders.py` already uses for the weekly-review job. `test_daily_summary_job_suppressed_during_quiet_hours` / `test_daily_summary_job_sends_when_not_quiet_hours` — the real job function, invoked through `async_main`'s real wiring with the wall clock frozen (`_FixedDatetime`, matching `test_adaptive_reminders.py`'s own technique) to a moment inside/outside a configured quiet-hours window. |
| **AC10.4** | `gamification.enabled = false` suppresses all milestone lines and (optionally) the daily summary — no behavioral leakage | **PASS** | `test_daily_summary_flag_defaults_independently_of_enabled_flag` — the two config flags default independently regardless of each other's value. `test_gamification_disabled_suppresses_milestone_lines` — `enabled=False` through a real 3-day crossing: the underlying streak genuinely reaches 3 (streak math is never gated, AC10.5) but zero milestone lines are sent. `test_gamification_disabled_does_not_affect_daily_summary_content` — same disabled config, the daily summary is unaffected. `test_daily_summary_disabled_job_sends_nothing` — `daily_summary=False`, the real registered job sends nothing. `test_milestones_still_fire_when_daily_summary_disabled` — same config, milestone lines still fire on the live confirmation path. Full 2×2 independence proven, not assumed. |
| **AC10.5** | Streak/summary computation is read-only and reuses v0.7 aggregation (no divergent math from the weekly review) | **PASS** | `test_review_and_streaks_module_agree_on_duration_streak_length` — parametrized streak lengths `[1,3,7,10,15,40]`, comparing `compute_weekly_stats(...).get("yoga").streak`, `streaks.compute_streak(...)`, and `streaks.compute_daily_summary(...)[0].streak` on identical seeded data — all three agree exactly for every length, **including 4 lengths that exceed the review's 7-day window** (the specific "contradictory numbers" failure mode this AC guards against). `test_streaks_module_is_provably_read_only` — a `Database` subclass whose 4 write methods (`insert_log`/`reclassify_log`/`soft_delete`/`update_value`) raise `AssertionError`, run through every public `core/streaks.py` entry point plus `compute_weekly_stats` itself; nothing raised. |

Every AC10.1–10.5 is **PASS**. No untestable/ambiguous AC found — nothing escalated to Sophia.

## Regression audit — `core/review.py`'s streak refactor (pre-v0.10 byte-identical, superset beyond 7 days)

`test_duration_streak_matches_v090_algorithm_when_streak_fits_in_7day_window` reimplements the literal removed inline loop from `git diff v0.9.0 -- src/habit_assistant/core/review.py` (`for c in reversed(counts): if c > 0: streak += 1 else: break`, pinned directly against that diff hunk rather than re-checking out the old file) and confirms it agrees exactly with the new `streaks.compute_streak`-based output for a streak that fits inside the 7-day window (a gap-interrupted 3-day trailing run). `test_duration_streak_beyond_7_days_is_a_documented_bugfix_not_a_regression` confirms the old algorithm's window-clamp ceiling (7, for an unbroken 10-day streak) versus the new algorithm's true length (10) — the divergence is strictly "reports more," matching `IMPL.md`'s own characterization ("a bugfix, not a regression — no existing test asserted the old clamping as intentional behavior"). Combined with the full pre-existing suite (`tests/test_review.py`, `tests/test_v07_m3_review_extra.py`) passing unmodified at 650/650, this is the practical form of "pin against `git show v0.9.0` output" available without introducing a second import path for the old module.

## Audit — deferred-reparse scope trim (IMPL.md "Known limitations" #3)

`test_reparse_pending_unparsed_does_not_check_milestones` seeds 2 prior goal-met water days plus a pending `'unparsed'` row that would be water's 3rd consecutive goal-met day once recovered — a genuine milestone-3 crossing on the live path. Recovering it via the real `reparse_pending_unparsed` (gamification enabled) produces **no crash**, reclassifies the row correctly (`category='water'`, `value_num=2500.0`), and sends exactly the fixed `recovered_water` catalog line with **no milestone suffix** — confirmed absence, not a wrong/garbled line. `streaks.compute_streak` independently confirms the streak did reach 3, so the omission is cosmetic (no in-the-moment celebration) rather than a data-correctness bug — the next weekly review or daily summary will still report it accurately. Matches `IMPL.md`'s characterization exactly: "a deliberate scope trim, not an oversight."

## Failures (if any)

None.

## Regressions detected

None. Full suite: 650 passed, 1 skipped, 0 failed (611 baseline + 39 new, matching exactly — no baseline test's outcome changed).

## Live-environment / safety checks

- Production bot (PID 12356) confirmed running (`Get-Process -Id 12356`) both before and after this pass.
- `data/habits.db` / `.env` `LastWriteTime` (2026-08-19 17:35:16 / 17:35:09) unchanged across this pass — every test in `tests/test_streaks.py` uses a `tmp_path`-scoped `Database`; nothing constructs a `Database` against the real config path.
- No real Telegram or Ollama call made anywhere — all channels are the local `FakeChannel`/`_FakeTelegramChannel` stand-ins, all LLM interaction is bypassed via `patch_parse_message`/`FakeLLM` (the one `async_main` path that does construct a real `OllamaClient` against `config.ollama.base_url` fails closed on the unreachable host within the existing `probe_schema_support`/`retry_attempts` bounds, exactly as `tests/test_reminders.py`'s own pre-existing `async_main` job-registration test already does).
- No git commit made (per instruction) — only `tests/test_streaks.py` (new) and this `TEST.md` append are this pass's filesystem changes.

## Recommendation

**Ready to ship — v0.10.0 Streaks, Gentle Gamification & Daily Summary, overall status PASS.** All 5 ROADMAP.md AC10.1–AC10.5 are green, entirely on Vera's own new test suite since Luna deliberately shipped none this round. Streak arithmetic is verified across all 4 habit types (goal-exact-counts, any-entry, done-days, gap-reset, today-partial semantics for both goal and non-goal habits). Milestone crossing is verified through the real `handle_inbound_message` across a full 3→7 sequence with no-repeat-same-day and non-milestone-number cases, plus bilingual Thai output. The daily summary is verified for content correctness (per-habit-type, per-language), scheduled registration at a custom time, and quiet-hours suppression through the real `async_main` wiring with a frozen clock. `gamification.enabled`/`daily_summary` independence is proven as a full 2×2, not assumed. AC10.5's "no divergent math" is proven directly — three independent call sites (weekly review, direct streak call, daily summary) agree exactly across 6 streak lengths including several beyond the review's 7-day window — plus a write-method-spy proof that the entire module is read-only. The `core/review.py` refactor is confirmed byte-identical to the old v0.9.0 algorithm for in-window streaks and a documented, strictly-additive superset beyond 7 days (not a regression). The deferred-reparse scope trim is audited and confirmed benign (no crash, no wrong line, only an absent celebration). No spec gaps, no untestable ACs, no regressions. Full suite: 650 passed / 1 skipped (pre-existing, documented) / 0 failed. Production bot, live DB, and `.env` confirmed untouched throughout. No blockers for Archi to proceed to release.

---

# Test Report — v1.0.0 Insights: Charts-as-Images + Garmin Import (capstone)

> Scope: ROADMAP.md section "v1.0.0 - Insights: Charts-as-Images + Garmin Import" (AC1.0.1-AC1.0.5), tested against Luna's uncommitted working-tree changes (`git diff v0.10.0 -- src config.toml pyproject.toml README.md VERSION`: `core/charts.py` new, `core/garmin.py` new; `channels/base.py`, `channels/telegram.py`, `config.py`, `core/i18n.py`, `core/review.py`, `main.py`, `config.toml`, `pyproject.toml`, `VERSION`, `README.md` modified). Luna wrote **zero** formal tests this round -- deliberate and documented in `IMPL.md` ("a channel.send_image call-count assertion on the main.py wiring" is explicitly deferred to Vera), only a manual interpreter smoke script (`smoke_v1_0_0.py`, not checked in). This entire report's evidence is Vera's own new `tests/test_charts.py` + `tests/test_garmin.py`. Not committed (per task instruction -- v0.10.0 is still the latest tag).

## Summary

- Baseline (v0.10.0, before this task's own additions): **650 passed, 1 skipped** -- reconfirmed by running the full suite before adding any v1.0.0 test file.
- New tests this pass (Vera): **51** -- 25 in `tests/test_charts.py`, 26 in `tests/test_garmin.py` (Luna added none -- see scope note above).
- **Total this pass: 701 passed, 1 skipped, 0 failed** (650 baseline + 51 new). The 1 skip is the same pre-existing v0.1.0-era `tests/test_channels.py:231` skip.
- **Status: PASS.** All 5 acceptance criteria (AC1.0.1-AC1.0.5) are green. One audit-item failure was found and fixed mid-pass (see "Re-verification" below): `src/habit_assistant/__init__.py`'s `__version__` string was left at `"0.10.0"` while `VERSION`/`pyproject.toml` were correctly bumped to `1.0.0`. Luna applied the one-line fix (`__version__ = "1.0.0"`, no other file touched); Vera independently re-read the file, confirmed the fix, re-ran `test_version_is_consistent_across_version_file_pyproject_and_init` (now PASS), and re-ran the full suite twice for reproducibility -- clean both times, no regressions.

## Test files

| Path | Tests added | Covers which ACs |
|---|---|---|
| `tests/test_charts.py` (Vera, new) | 25 | AC1.0.2 (6: TelegramChannel multipart shape, mocked-transport send, HTTP-error propagation, ABC default degrade, ABC-attribute sanity, LineChannel regression), AC1.0.1 (14: real-PNG rendering for numeric/duration/boolean, text-habit skip, render-failure catch, matplotlib-absent simulation incl. no-per-call-spam, import-guard reload, `render_weekly_review_charts` enabled/disabled/caption-agreement/matplotlib-absent, text-then-images ordering at both the direct-call and real-`async_main` level, chart-render-exception fail-open in `main.py` itself), Audit (2: VERSION/pyproject/`__init__.py` consistency, README install-line sanity) |
| `tests/test_garmin.py` (Vera, new) | 26 | AC1.0.3 (13: CSV parse incl. same-day summing/custom column_map/missing-column/missing-file/blank-row-skip/non-numeric/non-ISO-date, join+discrepancy-flag beyond vs. within threshold, exact-threshold boundary, missing-Garmin-day defaults to zero, bilingual section rendering), AC1.0.4 (6: 4-way parametrized whole-file failure incl. bilingual "unavailable" note, ragged-row tolerance distinguished from whole-file failure, full `run_weekly_review` still sends on a broken CSV, broad-except re-check), AC1.0.5 (3: Garmin path never touches a forbidden-HTTP-handler transport, no-network-import grep-equivalent, full charts+Garmin+Telegram review contacts only `api.telegram.org`), Regression (1: `run_weekly_review` byte-identical to the pinned v0.10.0 return expression when Garmin is unconfigured) |

## AC coverage -- ROADMAP.md v1.0.0 (AC1.0.1-AC1.0.5)

| AC | Requirement | Verdict | Evidence |
|---|---|---|---|
| **AC1.0.1** | The weekly review sends a water chart image (and a stretch chart) with a caption; `charts.enabled=false` or rendering failure falls back to text (no crash) | **PASS** | `test_render_habit_chart_numeric_produces_real_png_bytes` / `_duration_` / `_boolean_` -- real matplotlib output, PNG magic-number header confirmed, not a stub. `test_render_weekly_charts_skips_text_habits_but_charts_the_rest` -- default registry (water/stretch/diary) produces exactly 2 charts, diary (text) correctly excluded, registry order preserved. `test_render_weekly_review_charts_enabled_returns_captioned_real_pngs` / `_disabled_returns_empty_list` -- the `[charts] enabled` gate. `test_render_habit_chart_render_failure_is_caught_and_returns_none` -- a monkeypatched `_render_bar_chart` raising `RuntimeError` is swallowed, returns `None`, doesn't propagate. `test_render_habit_chart_matplotlib_absent_returns_none_and_warns_once` -- `MATPLOTLIB_AVAILABLE=False` simulated across 3 consecutive render calls: all 3 degrade to `None`, but the "missing matplotlib" warning fires exactly once (`caplog`-verified), not per-call spam. `test_charts_module_import_guard_never_raises_when_matplotlib_hidden` -- reloads `core/charts.py` with `matplotlib`/`matplotlib.pyplot` hidden from `sys.modules`; the module-level `try/except ImportError` sets `MATPLOTLIB_AVAILABLE=False` instead of raising at import time (the actual guard mechanism, one level below the render-call-level test above -- cited for AC1.0.1 and the Audit's install-guard item per the task brief's "one implementation, cite it for both"). `test_weekly_review_job_sequence_sends_text_then_chart_images` / `_charts_disabled_is_text_only` -- mirrors `main.py`'s `weekly_review_job` closure exactly (same call order: `run_weekly_review` then `channel.send` then `render_weekly_review_charts` then `channel.send_image` per pair), matching `tests/test_review.py`'s own established "mirrors main.py's closure" convention. `test_async_main_weekly_review_job_sends_text_then_images_no_real_network` -- the SAME wiring exercised through the real `async_main`/registered-job path (`job.func()`, per `tests/test_streaks.py`'s daily-summary-job-invocation pattern), with `OllamaClient` also monkeypatched so zero real network I/O of any kind occurs (stricter than the pre-existing `test_reminders.py` precedent, which tolerates a fast-failing real `OllamaClient` against `localhost:11434`): `channel.calls[0] == "send"`, `"send_image"` present, every image is a real PNG. `test_async_main_weekly_review_job_chart_render_exception_still_sends_text` -- `main.render_weekly_review_charts` monkeypatched to raise; `main.py`'s own belt-and-suspenders `try/except` (not just `charts.py`'s internal one) catches it -- `channel.calls == ["send"]`, zero images, no crash. |
| **AC1.0.2** | `send_image` on `TelegramChannel` posts via `sendPhoto`; a channel without an image implementation degrades to text -- verified without touching `core/` | **PASS** | `test_build_send_image_request_shape` -- `(url, data, files)` exactly `.../sendPhoto`, `{"chat_id":..., "caption":...}`, `files["photo"]==("chart.png", bytes, "image/png")`. `test_send_image_posts_multipart_to_send_photo_endpoint_with_mocked_transport` -- a real `send_image` call against `httpx.MockTransport`: POST to `/sendPhoto` on host `api.telegram.org`, multipart body contains `chart.png`, the caption text, and the PNG magic bytes. `test_send_image_raises_on_http_error_status` -- HTTP error status propagates as `httpx.HTTPStatusError` (mirrors `test_send_raises_on_http_error_status`'s existing pattern for plain `send`). `test_channel_abc_default_send_image_degrades_to_plain_send_without_touching_core` -- a bare `Channel` subclass overriding only `send`/`run` (this test file imports nothing from `core/`): `channel.send_image(...)` results in exactly `["caption text"]` reaching `.send`. `test_channel_send_image_default_is_defined_on_the_abc_itself` -- confirms the default lives on `Channel.__dict__`, not something each subclass must reimplement. `test_line_channel_stub_still_imports_and_is_a_valid_channel_subclass` -- regression check on the ABC extension: `LineChannel` is still a valid `Channel` subclass (not blocked by `abc.ABCMeta` for a missing abstract `send_image`, since it's a concrete default, not `@abstractmethod`); instantiating it raises the documented, intentional `NotImplementedError` from the stub's own `__init__`, not an ABC-abstractness `TypeError` -- confirming the ABC extension didn't change the stub's error surface. |
| **AC1.0.3** | A sample Garmin hydration CSV is parsed and joined by date against `water` logs; the review reports per-day self-reported vs. Garmin totals and flags discrepancies beyond a threshold | **PASS** | `test_parse_garmin_csv_default_column_map_reads_date_and_hydration` / `_sums_multiple_same_day_rows` / `_custom_column_map_is_honored` (a UK-style `day,water_ml` header, config-overridden) / `_skips_rows_with_blank_date_or_hydration`. `test_build_garmin_report_joins_by_date_and_computes_correct_totals` -- 7 known, distinct self-reported/Garmin values per day, exact `pytest.approx` match, zero discrepancy. `test_build_garmin_report_flags_discrepancy_beyond_threshold_not_within` -- a 1900ml-diff day (beyond a 300ml threshold) is flagged with the warning marker on that line only; a 100ml-diff day is not. `test_build_garmin_report_day_at_exact_threshold_is_not_flagged` -- the strict `>` boundary: exactly 300ml at a 300ml threshold does NOT flag. `test_build_garmin_report_missing_garmin_day_defaults_to_zero` -- a review-window day absent from the CSV still gets a comparison row with `garmin_ml=0`. `test_format_garmin_section_renders_in_both_languages` -- en/th catalog headers present, texts differ, all 7 day strings present in both. Custom column_map, discrepancy math, and threshold boundary are each independently exercised -- no untestable AC found. |
| **AC1.0.4** | A missing/malformed Garmin CSV is handled gracefully -- the review still sends, noting Garmin data was unavailable | **PASS** | `test_build_garmin_report_degrades_gracefully_for_every_broken_input` -- 4-way parametrized whole-file failure (missing file, empty file, non-numeric hydration value, wrong column names with no config override): every case yields `available=False`, empty `comparisons`, and the bilingual `garmin_unavailable` note in both languages, never raises. `test_build_garmin_report_tolerates_individually_malformed_rows_in_an_otherwise_good_file` -- a different, more forgiving case correctly distinguished from the whole-file-failure set: a file with one ragged short row and one row with extra trailing columns still parses (`available=True`), with only the genuinely-blank row's day defaulting to 0 -- this corrects an initial test-authoring assumption of Vera's own (see note below) that a per-row malformed line would fail the whole file; it doesn't, by design (`parse_garmin_csv`'s own blank-field guard). `test_run_weekly_review_still_sends_and_notes_garmin_unavailable_on_broken_csv` -- the full `run_weekly_review` (not just `build_garmin_report` in isolation) with a nonexistent CSV path still returns full text containing the Thai "unavailable" note, no exception escapes. `test_build_garmin_report_malformed_csv_does_not_raise_out_of_build_report` -- direct re-check of the broad `except Exception` boundary. |
| **AC1.0.5** | All chart rendering and CSV parsing happen locally; no new outbound host is contacted (charts are bytes over the existing Telegram send) | **PASS** | `test_garmin_parse_and_join_never_invokes_http_transport` -- sanity/tripwire test plus a signature check confirming `garmin.build_garmin_report`/`parse_garmin_csv` accept no `client`/transport parameter at all (no HTTP capability to invoke in the first place). `test_garmin_module_has_no_network_imports` -- `inspect.getsource(garmin)` contains none of `httpx`/`requests`/`socket`/`urlopen`/`aiohttp` (grep-equivalent, matching `IMPL.md`'s own claimed audit). `test_full_weekly_review_with_charts_and_garmin_contacts_only_telegram_host` -- the decisive end-to-end proof: a full charts-enabled + Garmin-configured review (text send + N `send_image` calls) through a real `TelegramChannel` backed by a captured `httpx.MockTransport`; every single captured request's host is asserted to be exactly `api.telegram.org`, no other host -- if `core/charts.py` or `core/garmin.py` had made any stray network call, it would have bypassed this transport entirely (raising a connection error, not silently succeeding) or, had a client been threaded through, shown up as a non-Telegram host in the captured set. Neither happened. |

Every AC1.0.1-1.0.5 is **PASS**. No untestable/ambiguous AC found -- nothing escalated to Sophia.

## Regression -- `run_weekly_review` byte-identical to v0.10.0 when Garmin unconfigured

`test_run_weekly_review_byte_identical_to_v0100_when_garmin_unconfigured` reconstructs the literal v0.10.0 return expression (`f"{header}\n\n{summary}\n\n{narrative}"`, pinned directly against `git show v0.10.0:src/habit_assistant/core/review.py`'s tail, per the same technique `tests/test_streaks.py`'s own v0.10.0 regression section used) and asserts the new `run_weekly_review`'s actual output is exactly equal -- not just "contains" -- to that reconstruction, under the default config (`garmin.csv_path=""`). Combined with the full pre-existing `tests/test_review.py` suite (7 tests, all charts/Garmin-config-agnostic since they use `Config()` defaults) passing unmodified, this confirms `core/review.py`'s Garmin-section-append is correctly a no-op whenever Garmin isn't configured -- charts, meanwhile, never touch `run_weekly_review`'s text output at all (a separate function, `render_weekly_review_charts`, that `main.py` calls afterward), so there is no charts-side regression risk to this text function by construction.

## Failures

None as of the final pass. One was found and fixed mid-pass -- see "Re-verification" immediately below for the full detail (kept, not deleted, so the audit trail is visible).

## Re-verification -- `__init__.py` version fix

**Original failure (first pass):** `test_version_is_consistent_across_version_file_pyproject_and_init` -- `VERSION` and `pyproject.toml`'s `[project] version` were correctly `"1.0.0"`, but `src/habit_assistant/__init__.py`'s `__version__` was still `"0.10.0"` (`git log --oneline -- src/habit_assistant/__init__.py` confirmed this file was bumped at every prior release, v0.1.0 through v0.10.0, but `git diff v0.10.0 -- src/habit_assistant/__init__.py` showed zero diff this release -- a one-file omission, not a design defect). Not one of AC1.0.1-AC1.0.5 directly; it was the task brief's Audit item 7, and a load-bearing precondition for ROADMAP.md's own "declare stable" framing of this release.

**Fix applied by Luna:** `src/habit_assistant/__init__.py:1` changed from `__version__ = "0.10.0"` to `__version__ = "1.0.0"`. No other file touched (confirmed via `git status --short`: only `src/habit_assistant/__init__.py` newly shows as modified compared to the first pass).

**Vera's independent re-verification:**
1. Re-read `src/habit_assistant/__init__.py` directly -- confirmed it now reads `__version__ = "1.0.0"`.
2. Re-ran `tests/test_charts.py::test_version_is_consistent_across_version_file_pyproject_and_init` in isolation -- **PASS**.
3. Re-ran the full suite twice for reproducibility -- both runs: **701 passed, 1 skipped, 0 failed**, identical numbers both times.
4. Re-confirmed the live-environment guardrails: production bot (PID 12496, same `StartTime` 2026-08-19 22:41:31, never restarted), `data/habits.db`/`.env` `LastWriteTime` unchanged (17:35:16 / 17:35:09), `git status --short` shows only the expected file set (Luna's pre-existing uncommitted diff + the `__init__.py` fix + Vera's 2 new test files + this `TEST.md`) -- no commit made.

## Regressions detected

None. Full suite: 701 passed, 1 skipped, 0 failed (650 baseline + 51 new, all green -- no regression in any pre-existing v0.1.0-v0.10.0 test, and the one v1.0.0-introduced failure is now fixed and independently re-verified).

## Live-environment / safety checks

- Production bot (PID 12496, started 2026-08-19 22:41:31) confirmed running (`Get-Process -Id 12496`) both before and after this pass -- same PID, same `StartTime`, never restarted.
- `data/habits.db` / `.env` `LastWriteTime` (2026-08-19 17:35:16 / 17:35:09) confirmed byte-for-byte unchanged before/after this pass -- every DB in `tests/test_charts.py`/`tests/test_garmin.py` is a `tmp_path`-scoped `Database`, and every Garmin CSV is a `tmp_path`-scoped scratch file; nothing constructs a `Database` against the real config path or reads a real Garmin export.
- No real Telegram call made anywhere -- every `TelegramChannel` in this pass is backed by `httpx.MockTransport` or a `_FakeTelegramChannel`/bare-`Channel`-subclass test double.
- No real Ollama call made anywhere -- `run_weekly_review`'s LLM parameter is always a `FakeLLM`, and the one `async_main` integration path additionally monkeypatches `main_module.OllamaClient` itself (not just the transport), so zero real socket I/O of any kind is attempted -- stricter than the pre-existing `test_reminders.py`/`test_streaks.py` precedent, which tolerates a real (but fast-failing, unreachable-host) `OllamaClient` construction.
- No git commit made (per instruction) -- only `tests/test_charts.py` (new), `tests/test_garmin.py` (new), the `__init__.py` version fix (Luna), and this `TEST.md` append/update are this pass's filesystem changes; `git status --short` confirms no other file was touched.

## Capstone note -- closing the 10-version roadmap

This is the 10th and final version in ROADMAP.md's arc (v0.2.0 to v1.0.0), and the suite has grown from 132 tests at the v0.1.0 MVP baseline (per this file's own opening section) to 701 tests here, all passing -- every intermediate version's Vera pass added its own dedicated AC-mapped test file (`test_streaks.py` at v0.10.0: +39; `test_v09_gaps.py` at v0.9.0; and so on back to the original 9-file split), and every one of those files still passes unmodified today, which is itself the strongest evidence the "byte-identical unless documented otherwise" discipline this project held to across 10 versions actually worked -- nothing quietly broke on the way here. Three of the last four versions (v0.9.0, v0.10.0, v1.0.0) shipped with zero formal tests from Luna, smoke-script-only, by design -- Vera's pass has been the entire formal AC suite for each, and each has landed clean (v0.9.0: PASS; v0.10.0: PASS; v1.0.0: PASS on all 5 ACs, with one Luna↔Vera round-trip on an unrelated audit item, now closed).

**Is v1.0.0 ready to declare "stable" per ROADMAP.md's own framing?** Yes. All 5 acceptance criteria are green with direct test evidence (real PNG bytes, not stubs; a real multipart Telegram request shape; a real CSV join with a correctly-flagged discrepancy; every documented broken-Garmin-file shape degrading gracefully; a host-boundary proof that nothing new reaches the network); the `config.toml`/schema stability README claims that anchor the "1.0" designation aren't contradicted by anything found here; and the version-string inconsistency that would have undercut the "stable, versioned" claim itself has been fixed and independently re-verified. Nothing outstanding.

## Recommendation

**Ready to ship -- v1.0.0 Insights: Charts-as-Images + Garmin Import, overall status PASS.** All 5 ROADMAP.md AC1.0.1-AC1.0.5 are **PASS** with direct, non-mocked-where-avoidable evidence: real matplotlib-rendered PNG bytes (magic-number-verified) for numeric/duration/boolean habits with text habits correctly skipped; the `[charts] enabled` gate, a render-failure catch, and a matplotlib-absent simulation all confirmed to degrade to text-only without crashing and without per-call log spam; the exact same text-then-images wiring proven both as a direct call and through the real `async_main`/registered-job path with zero real network I/O; a Telegram `sendPhoto` multipart request shape confirmed via mocked transport; the ABC's default `send_image`-degrades-to-`send` behavior proven without touching `core/`; the `LineChannel` stub confirmed still a valid, unaffected `Channel` subclass; a Garmin CSV parsed, joined by date, and discrepancy-flagged with an exact `>` threshold boundary and a custom column_map honored; every documented broken-CSV shape (missing/empty/malformed-rows/non-numeric/wrong-columns) degrading gracefully with a bilingual note, distinguished correctly from the separate case of an otherwise-good file with a few tolerable ragged rows; and a definitive host-boundary test proving a full charts+Garmin+Telegram review contacts only `api.telegram.org`. The one audit-item failure found this pass (`src/habit_assistant/__init__.py`'s `__version__` stuck at `"0.10.0"`) was fixed by Luna with a one-line change and independently re-verified by Vera -- re-read the file directly, re-ran the specific test (PASS), and re-ran the full suite twice for reproducibility. No spec gaps, no untestable ACs, no regressions anywhere in the pre-existing v0.1.0-v0.10.0 suite. Full suite: 701 passed / 1 skipped (pre-existing, documented) / 0 failed -- independently reconfirmed by Archi with a synchronous `pytest -q` run on top of Vera's own. Production bot, live DB, and `.env` confirmed untouched throughout. No blockers for Archi to proceed to release.

**Non-blocking observation (Archi, post-verification):** the full-suite run surfaces repeated matplotlib `UserWarning: Glyph ... missing from font(s) DejaVu Sans` for Thai characters, raised from `core/charts.py:103` (`fig.tight_layout()`) whenever a chart is rendered with a Thai-language habit label/title. This means chart PNGs rendered in Thai will show missing-glyph boxes ("tofu") for the Thai text baked into the image itself -- the axis/title text specifically, not the Telegram caption or the Garmin section text (both are plain-text catalog strings, unaffected, and separately verified bilingual above). No AC1.0.1-AC1.0.5 requirement covers in-chart glyph fidelity, so this is not a release blocker, but it sits in tension with the project's standing "bilingual Thai + English" constraint for a Thai-primary user viewing charts. Suggested follow-up (out of scope for v1.0.0): configure a Thai-capable font (e.g. Noto Sans Thai) for matplotlib's `font.family`, or fall back to English-only chart text when Thai glyph support isn't available. Flagging for the user's prioritization, not looping Luna for this release.
