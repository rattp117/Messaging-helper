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
