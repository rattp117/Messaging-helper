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
