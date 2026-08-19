# Habit-Tracking Assistant — Development Progress

- **Current version:** 0.7.0
- **Repo:** local-only (user not asked yet — autonomous session; change on request)
- **Status:** Roadmap program — v0.7.0 (Multi-Habit, the pivot) released; next: v0.8.0 NL Queries and v0.9.0 Adaptive Reminders (independent per ROADMAP §3)
- **Last updated:** 2026-08-19 · **Last commit:** (initial)

## Stack
Python 3.11+ (uv-managed venv) · asyncio · APScheduler (AsyncIOScheduler) · httpx (Telegram long-poll + Ollama) · stdlib sqlite3 (WAL) · pydantic-settings · pytest. Stack dictated by user spec — Irine skipped.

## Deliverables
- [x] SPEC.md — user-provided spec, saved verbatim at repo root (+ AC list added in §11)
- [ ] UX.md — skipped (no UI surface; chat bot + CLI)
- [ ] UI.md — skipped (no UI surface)
- [ ] STACK.md — skipped (stack fully dictated in SPEC.md §1–§4)
- [x] IMPL.md — repo root; full MVP implemented, live-smoke-tested against mac-mini Ollama
- [x] TEST.md — repo root; final status PASS, all 11 ACs (132 passed / 1 skipped)
- [x] ROADMAP.md — repo root; Sophia's 10-version plan v0.2.0 → v1.0.0

## Changelog
| Version | Date | Summary | Files | Commit/Tag |
|---|---|---|---|---|
| 0.1.0 | 2026-08-19 | MVP release: Telegram bot + Qwen extraction (bilingual) + SQLite + reminders + weekly review; all 11 ACs PASS | src/habit_assistant/*, tests/*, config.toml, README, plist, start-assistant.ps1 | v0.1.0 |
| 0.2.0 | 2026-08-19 | Extraction reliability: model fallback chain, startup schema probe, confidence threshold 0.55; ACs 2.1–2.5 PASS (160 tests) | llm/ollama_client.py, config.py, config.toml, core/parser.py, main.py, tests/test_fallback.py | v0.2.0 |
| 0.3.0 | 2026-08-19 | Migrations (user_version runner) + backup/restore/retention + --migrate/--backup/--restore CLI; ACs 4.1–4.5 PASS (186 tests) | storage/migrations.py, storage/db.py, core/backup.py, main.py, config.py, config.toml, tests/test_migrations.py, tests/test_backup.py | v0.3.0 |
| 0.4.0 | 2026-08-19 | Resilience: long-poll backoff, health monitor (alert once/outage), Ollama retry, persistent unparsed-deferral + startup backlog re-parse; ACs 3.1–3.5 PASS (203 tests) | channels/telegram.py, llm/ollama_client.py, core/health.py, storage/{db,migrations}.py, main.py, config.py, config.toml, tests/test_resilience.py | v0.4.0 |
| 0.5.0 | 2026-08-19 | Command layer + undo/edit (bilingual, LLM-free router), soft-delete via migration 003, aggregations exclude deleted; ACs 5.1–5.5 PASS (252 tests) | core/commands.py, storage/{db,migrations}.py, main.py, tests/test_commands.py | v0.5.0 |
| 0.6.0 | 2026-08-19 | Bilingual output: en/th message catalog, auto language detection, Thai primary for unprompted sends, localized weekly review; ACs 6.1–6.5 PASS (318 tests) | core/i18n.py, main.py, core/{reminders,review,health}.py, llm/prompts.py, config.py, config.toml, tests/test_i18n*.py, tests/test_bilingual_confirmations.py, tests/test_v060_bilingual_gaps.py | v0.6.0 |
| 0.7.0 | 2026-08-19 | Multi-habit pivot: [[habits]] config, HabitRegistry, generic extraction/DB/review/reminders, migration 004; parallel build (shared surface + 3 modules); all ACs 7.1–7.5 PASS (463 tests) | core/habits.py, config.py, config.toml, llm/{ollama_client,prompts}.py, core/{parser,commands,reminders,review}.py, storage/*, main.py, SPEC-v0.7.md, tests (7 new/rewritten files) | v0.7.0 |

## Decisions
- 2026-08-19 — **User update: runtime host is this Windows box** (24/7), with Ollama remote at `http://mac-mini:11434` (verified reachable). Default model `qwen3.5:9b-mlx`. Windows keep-alive via Task Scheduler + launcher script; launchd plist kept as alternative macOS deploy.
- 2026-08-19 — Dev machine is Windows Server (no local Ollama, no system Python); build cross-platform, unit tests mock Ollama/Telegram, live extraction smoke-tested against mac-mini.
- 2026-08-19 — Repo root = this working directory ("Messaging AI assistant"), not a nested `habit-assistant/` subfolder.
- 2026-08-19 — Skipped Sophia/Maya/Iris/Irine: user's prompt is a complete spec, no UI, stack dictated. SEQUENTIAL mode (build order is a dependency chain).
- 2026-08-19 — Git local-only by default (couldn't ask user mid-run); revisit if user wants a remote.
- 2026-08-19 — Use `uv` for venv + deps (available; system Python absent).

## Open questions / Next steps
- Vera: TEST.md for ACs 1–11 (in flight).
- On Vera PASS: write real creds to `.env` (token from user; chat ID 1574572064 captured from /start), release v0.1.0 (commit + tag), start the bot on this box (Task Scheduler / start-assistant.ps1).
- Sophia (in flight): `ROADMAP.md` — next 10 versions of improvements. User has pre-approved implementing all 10 sequentially (Luna↔Vera per version, one release each); pause only for versions Sophia flags as needing user decisions.
- Ask user whether they want a git remote.
