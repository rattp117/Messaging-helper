# Implementation — Local Habit-Tracking Assistant (MVP)

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `pyproject.toml` | created | Project metadata, deps (`httpx`, `apscheduler`, `pydantic`, `pydantic-settings`, `tzdata`), dev deps (`pytest`, `pytest-asyncio`), `hatchling` src-layout build, pytest config |
| `config.toml` | created | Non-secret config: reminder times/goal, Ollama base URL + model, unit constants, timezone, DB path, log level, weekly-review schedule |
| `.env.example` | created | Template for `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |
| `.env` | not committed (gitignored) | Left absent for handoff — user fills in real Telegram credentials before first real run |
| `README.md` | created | Windows-first setup + Task Scheduler keepalive (primary), macOS + launchd setup (alternative deploy target), config/CLI/test docs |
| `com.ratt.habit-assistant.plist` | created | macOS LaunchAgent (alternative deploy target), `RunAtLoad` + `KeepAlive` (restart on crash) |
| `start-assistant.ps1` | created | Windows launcher script (resolves venv, execs `python -m habit_assistant.main`) for Task Scheduler registration |
| `src/habit_assistant/__init__.py` | created | Package marker + version |
| `src/habit_assistant/config.py` | created | `Config` (from `config.toml`, `tomllib`) + `Secrets` (from `.env`, `pydantic-settings`); `ConfigError` for clear startup failures |
| `src/habit_assistant/channels/base.py` | created | `Channel` ABC (`send`, `run`) per SPEC §8 |
| `src/habit_assistant/channels/telegram.py` | created | `TelegramChannel`: `send` (POST sendMessage), `run` (long-poll getUpdates loop); raw `httpx` |
| `src/habit_assistant/channels/line.py` | created | Documented stub only — `LineChannel` raises `NotImplementedError`, docstring explains webhook requirement |
| `src/habit_assistant/llm/prompts.py` | created | Extraction system/user prompt (with few-shot examples), diary-reflection prompt, weekly-review prompt |
| `src/habit_assistant/llm/ollama_client.py` | created | `OllamaClient` (`chat_json`, `chat_text`), `EXTRACTION_JSON_SCHEMA`, `ExtractionResult`, `strip_think_and_prose` |
| `src/habit_assistant/storage/models.py` | created | `LogEntry` dataclass |
| `src/habit_assistant/storage/db.py` | created | `Database`: schema/index creation, WAL mode, `insert_log`, `water_total_ml`, `stretch_count`, `diary_count`, `logs_between` |
| `src/habit_assistant/core/parser.py` | created | `parse_message`: calls `OllamaClient.chat_json`, validates against schema, fails closed to `unknown` |
| `src/habit_assistant/core/reminders.py` | created | `REMINDER_TEXTS`, `send_reminder`, `schedule_reminders` (APScheduler cron jobs from `config.toml`) |
| `src/habit_assistant/core/review.py` | created | `compute_weekly_stats`, `format_stats_summary`, `run_weekly_review`; Garmin `TODO` per SPEC §12 |
| `src/habit_assistant/main.py` | created | Wiring: config/secrets load, CLI (`--test-reminder`, `--seed`, `--dry-run`), `handle_inbound_message` (parse → write → confirm), scheduler + inbound loop startup, UTF-8 stdout/stderr fix |
| `tests/` | created (empty) | Directory exists, `pyproject.toml` points `testpaths` here; Vera adds `test_parser.py` / `test_db.py` |

## Design choice: raw `httpx` over `python-telegram-bot`

Went with raw `httpx` for `TelegramChannel`, per SPEC §3's "your call, justify briefly" and the environment brief's minimal-dependency steer. One HTTP client library (`httpx.AsyncClient`) serves both `TelegramChannel` (long-poll `getUpdates` + `sendMessage`) and `OllamaClient` (`/api/chat`), so there's no second async HTTP stack to reason about, and both channels/LLM clients accept an injected `httpx.AsyncClient` for testing (mock transports) without pulling in `python-telegram-bot`'s update/dispatcher machinery, which is more than this MVP's single linear inbound loop needs.

## How it works

1. **Startup** (`main.py:async_main`): loads `config.toml` (`Config`, always succeeds — falls back to defaults) and, for real runs, `.env` (`Secrets` — raises `ConfigError` with a clear message if the token/chat-id are missing, caught and printed to stderr with exit 1). `--seed` and `--dry-run` skip the secrets load entirely since they don't need Telegram.
2. **Inbound message flow**: `TelegramChannel.run` long-polls `getUpdates` and calls `on_message(text)` for every inbound message (never conditioned on whether a reminder preceded it, per SPEC §3). `on_message` wraps `handle_inbound_message`, which calls `core.parser.parse_message` → `OllamaClient.chat_json` (POST `/api/chat`, `stream: false`, `format: <json schema>`, `think: false`) → `strip_think_and_prose` → `json.loads` → schema/enum validation. Any failure at any step (HTTP error, malformed JSON, invalid enum, wrong types) returns `ExtractionResult.unknown()` — the parser never raises, so `TelegramChannel.run`'s per-message try/except is a second line of defense, not the primary one.
3. **Write + confirm**: for `water`/`stretch`/`diary`, `handle_inbound_message` writes one `logs` row (`storage/db.py`) then sends the confirmation text (running total/goal/%, ordinal stretch count, or an LLM-generated one-line reflection for diary) via the injected `Channel`. For `unknown`, it sends the clarifying question and writes no row.
4. **Reminders + weekly review**: `core/reminders.py:schedule_reminders` registers one `AsyncIOScheduler` cron job per configured time (10 jobs with defaults: 6 water + 2 stretch + 1 diary), plus `main.py` registers the weekly-review job directly (Sunday 20:00 by default). The review job calls `core/review.py:run_weekly_review`, which aggregates 7 days of `logs` via `Database`, formats a factual stats block, asks Qwen for a short narrative (`chat_text`, falls back to the plain stats block if the LLM call fails), and sends the combined text.
5. **Channel seam**: `channels/base.py:Channel` is the only channel-related import allowed in `core/`, `storage/`, or `main.py`'s type hints — `main.py` is the sole place that constructs a concrete `TelegramChannel`. Verified with `grep -i "import.*(telegram|line)"` across `core/` and `storage/`: no matches.

## Smoke test done

All commands run from the repo root with the `uv`-managed venv (`uv venv --python 3.12`, `uv pip install -e ".[dev]"`, Python 3.12.13). Full transcripts are in this session's tool history; summarized here.

**Offline / mocked (no live services required):**
- `load_config()` → correct values from `config.toml` (`http://mac-mini:11434`, `qwen3.5:9b-mlx`, reminder times, `goal_ml=2500`). **AC1**
- `load_secrets()` with no `.env` present → `ConfigError`: *"Could not load Telegram credentials from .env (missing: telegram_bot_token, telegram_chat_id)..."*. **AC1**
- `Database` insert + query round-trip (`water_total_ml`, `stretch_count`) on a fresh DB file; `PRAGMA journal_mode` confirmed `wal`. **AC2**
- `TelegramChannel.build_send_request(text)` — inspected `(url, payload)` without sending: `https://api.telegram.org/bot<token>/sendMessage`, `{"chat_id": ..., "text": ...}`. **AC3**
- `python -m habit_assistant.main --seed` → 37 rows inserted across 7 days (`water`×29, `stretch`×3, `diary`×5); `compute_weekly_stats` + `format_stats_summary` produced correct per-day %, totals, streak, diary count; `run_weekly_review` with a fake LLM (`chat_text` returns `None`) still returned the stats block via the fallback path. **AC8, AC9**
- Mocked `OllamaClient` via `httpx.MockTransport` through the real `parse_message` → `OllamaClient.chat_json` → `strip_think_and_prose` pipeline: (1) `<think>...</think>` + surrounding prose wrapped around valid JSON still parses to the correct `water`/500ml result; (2) malformed JSON (`"not even json {{{"`) → `unknown`; (3) `httpx.ConnectError` (simulated network failure) → `unknown`, no crash; (4) invalid `category` enum value (`"Beverage"`) → `unknown`. All 5 assertions passed. **AC4**
- `schedule_reminders` + weekly-review job registration against a fake `Channel` → 10 `AsyncIOScheduler` jobs with correct cron expressions (6 water, 2 stretch, 1 diary, 1 weekly review `day_of_week='sun', hour=20, minute=0`). **AC7**
- `pytest --collect-only` → runs cleanly (0 tests, as expected — Vera adds `tests/test_parser.py` / `tests/test_db.py`). Confirms the import chain and `pyproject.toml` pytest config are sound.

**Live (Ollama reachable at `http://mac-mini:11434` from this box during this session; confirmed via `curl /api/tags` and `/api/version` → 0.32.6):**
- `--dry-run` through the real production path (`parse_message` → real `OllamaClient` → live model) for all 4 required cases plus a diary case:
  - `"ดื่มน้ำ 2 แก้ว"` → `water`, `water_ml=500` (2 × 250ml glass constant). **AC5**
  - `"did 10 min stretch"` → `stretch`, `stretch_min=10`. **AC5**
  - `"500ml"` → `water`, `water_ml=500` (explicit ml). **AC5**
  - `"purple elephants dance sideways"` → `unknown`. **AC4/AC6**
  - `"today was such a tiring but good day"` → `diary`, `diary_text` populated.
- End-to-end `handle_inbound_message` (real Ollama + real SQLite + a fake `Channel` capturing sent text) for a 6-message sequence, confirming exact SPEC §6 formats:
  ```
  ✅ 500 ml logged — today 500 / 2500 ml (20%)
  ✅ 500 ml logged — today 1000 / 2500 ml (40%)
  ✅ 10 min stretch logged — 1st today
  ✅ 5 min stretch logged — 2nd today
  ✅ Saved. Your imagination dances beautifully today!
  🤔 I couldn't quite tell what you meant — was that about water, a stretch break, or today's diary? Try something like '500ml water' or '10 min stretch'.
  ```
  Running water total accumulates correctly across calls; stretch ordinal increments correctly; unknown wrote no row (verified row count unchanged). **AC6**
- `--test-reminder water` against a deliberately fake token → request actually reached `api.telegram.org` and returned `401 Unauthorized` (proves the real network call + URL construction path works; 401 is expected with a fake token). **AC3**

**Bug found and fixed during live smoke testing:** Windows' default console/stdout encoding (`cp1252`) cannot encode the emoji used in reminders/confirmations (💧🧘📓✅🤔📊) and crashed with `UnicodeEncodeError` the moment any such text hit `print`/`logging`. Since the coordinator moved the primary runtime host to this Windows box, this would have crashed the process on the first reminder or confirmation. Fixed in `main.py:setup_logging` by reconfiguring `sys.stdout`/`sys.stderr` to UTF-8 (`errors="replace"`) before `logging.basicConfig`. Verified fixed via `--test-reminder water` (no encoding crash; got as far as the real 401 from Telegram).

**Prompt-engineering finding (not a code bug, but load-bearing — see Known limitations):** the coordinator's directed default model, `qwen3.5:9b-mlx`, runs on this Ollama server's MLX backend, which does **not** enforce the JSON-schema `format` constraint (verified by comparison: the GGUF-backed `qwen3:8b` on the same server returns exactly the 5 required keys; `qwen3.5:9b-mlx` with the identical schema returned an unrelated `{"category": "Beverage", ...}`-shaped payload instead). SPEC §7 requires "JSON schema in `format`", so `ollama_client.py` still sends it (correct behavior, and it works properly against schema-conformant backends/models). To compensate for the non-conformant backend, added 6 few-shot examples to `EXTRACTION_SYSTEM_PROMPT` (`llm/prompts.py`) covering water/glass/bottle/stretch/diary/unknown — this fixed all 5 live test cases above. The parser's enum/type validation (`core/parser.py:_validate`) is the actual safety net regardless of prompt quality: any off-schema response (verified with the live `"Beverage"` response) fails closed to `unknown`, per AC4.

## Maps to acceptance criteria

- AC1 → `config.py:load_config`, `config.py:load_secrets`, `config.py:ConfigError`; wired into `main.py:async_main`. Smoke-tested (see above).
- AC2 → `storage/db.py:Database.__init__` (schema + index + WAL), `insert_log`, `water_total_ml`/`stretch_count`. Smoke-tested.
- AC3 → `channels/base.py:Channel`, `channels/telegram.py:TelegramChannel` (`send`, `run`); no `core/`/`storage/` imports a concrete channel (grep-verified). Smoke-tested (request construction + real 401 round-trip).
- AC4 → `llm/ollama_client.py:OllamaClient.chat_json` (`stream: false`, `format`, `think: false`), `strip_think_and_prose`, `core/parser.py:parse_message`/`_validate` (fail-closed to `unknown` on any error, never raises). Smoke-tested with mocked think-blocks/prose, malformed JSON, connection failure, bad enum, and live against the real server.
- AC5 → `llm/prompts.py:EXTRACTION_SYSTEM_PROMPT` (glass/bottle constants injected from `config.toml` via `parser.py`), validated live against all 3 required example messages.
- AC6 → `main.py:handle_inbound_message` (confirmation formatting for water/stretch/diary/unknown, verbatim per SPEC §6), `main.py:ordinal`. Smoke-tested live, exact string match against spec examples.
- AC7 → `core/reminders.py:schedule_reminders` (per-category cron jobs from `config.toml`), `main.py:async_main` (weekly-review cron job). Smoke-tested: 10 jobs registered with correct cron expressions.
- AC8 → `core/review.py:compute_weekly_stats`/`format_stats_summary`/`run_weekly_review`. Smoke-tested offline (fake LLM) and the aggregation math verified against seeded data.
- AC9 → `main.py:build_arg_parser`, `async_main` (`--test-reminder`, `--seed`, `--dry-run` branches). All 3 smoke-tested.
- AC10 → `.env.example`, `.gitignore` (pre-existing, excludes `.env`/`data/`), `README.md` (Windows-primary + macOS-alternative setup), `com.ratt.habit-assistant.plist`. All present at repo root.
- AC11 → not implemented by Luna per the standard split (Vera writes `tests/test_parser.py`/`tests/test_db.py`); `pytest`/`pytest-asyncio` are in `[project.optional-dependencies].dev`, `pyproject.toml` has `[tool.pytest.ini_options]` (`asyncio_mode = "auto"`, `testpaths = ["tests"]`), `tests/` directory exists, and every seam Vera will need is already dependency-injectable: `OllamaClient(base_url, model, timeout, client=...)`, `TelegramChannel(token, chat_id, poll_timeout, client=...)`, `Database(db_path)`, and `handle_inbound_message(..., clock=...)`. `pytest --collect-only` runs clean (0 items, no import errors).

## Known limitations

- **MLX backend doesn't enforce the `format` JSON schema** (see finding above). Mitigated with few-shot examples + strict server-side validation that fails closed, but this is a soft mitigation, not a guarantee — an adversarial or unusual message could still get an off-schema response from this specific model/backend combination that happens to *coincidentally* match the schema shape with wrong semantic values (e.g. a bogus `water_ml` that isn't obviously wrong to the validator). Correctness of *values* (not just shape) depends on the model actually following instructions; this is inherent to LLM extraction and true of any model, not unique to this bug. If the user finds live accuracy insufficient, switching `[ollama].model` in `config.toml` to a GGUF model (e.g. `qwen3:8b`, confirmed schema-conformant on this server) is a one-line config change, no code change.
- **LINE channel**: stub only, per SPEC §12/§8 — `channels/line.py` raises `NotImplementedError` and documents why (needs a public webhook, unlike Telegram's long-poll).
- **Garmin CSV import**: not implemented; `TODO` left in `core/review.py` per SPEC §12.
- **No real Telegram token available this session** — `TelegramChannel.send`/`run` are verified via request construction (`build_send_request`) and one live round-trip to `api.telegram.org` that correctly reached the server and got `401 Unauthorized` from a deliberately fake token (proves the HTTP path is correct; the 401 itself is expected, not a bug). Full send/receive against a real bot needs the user's real `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env` — `.env` was intentionally left absent at handoff (gitignored either way) so the "missing token" startup error (AC1) is what a fresh clone actually experiences.
- **DB concurrency**: `Database` uses one `sqlite3.Connection` on the event-loop thread, no thread pool — correct for this app's single-process, single-consumer-of-DB shape (inbound loop and scheduler jobs all run on the same asyncio loop, so calls are naturally serialized), but would need revisiting if a future feature added true multi-threading.
- **Windows-primary changes not yet reflected in `SPEC.md`/`PROGRESS.md`**: the coordinator's scope update (Windows as primary host, Ollama at `http://mac-mini:11434`, model `qwen3.5:9b-mlx`) was applied to `config.toml`, `README.md`, and `start-assistant.ps1`, but `SPEC.md` itself (owned by Sophia/Archi) still describes the original Mac-Mini-primary plan. Flagging for Archi to reconcile `PROGRESS.md`/`SPEC.md` if that record matters going forward — didn't touch `SPEC.md` since that's not Luna's file to edit.

## Iteration log

**Round 2 (Vera, `TEST.md`, 131 passed / 1 failed / 1 skipped):** `tests/test_cli.py::test_test_reminder_flag_fails_cleanly_on_401_not_crash` failed — the `--test-reminder` branch in `main.py:async_main` called `await send_reminder(channel, args.test_reminder)` with no `try/except`, so a Telegram API failure (e.g. an unauthorized/expired token → 401) raised a raw unhandled `httpx.HTTPStatusError` instead of failing cleanly like the existing `ConfigError` branches two sections up in the same function. Root cause: that one branch was the only place in `async_main` making a network call without a guard (the `--dry-run` branch's Ollama call was already safe — `parse_message`/`OllamaClient.chat_json` fail closed internally and never raise, confirmed no sibling fix was needed). Fix: added `import httpx` and wrapped the `send_reminder(...)` call in `try/except httpx.HTTPError as exc: print(f"ERROR: ...", file=sys.stderr); <close channel/llm/db>; sys.exit(1)`, mirroring the `ConfigError` pattern exactly (`main.py`, `args.test_reminder` branch). Full suite re-run: `132 passed, 1 skipped` (target met, no regressions).
