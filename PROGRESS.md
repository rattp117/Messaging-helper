# Habit-Tracking Assistant — Development Progress

- **Current version:** 0.2.0
- **Repo:** local-only (user not asked yet — autonomous session; change on request)
- **Status:** Roadmap program — v0.2.0 released; next: Migrations & Backup (shipping as v0.3.0 — reordered before Resilience per ROADMAP §3 so the deferral queue can be persistent)
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
