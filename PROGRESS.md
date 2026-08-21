# Habit-Tracking Assistant — Development Progress

- **Current version:** 1.2.0
- **Repo:** remote: git@github.com:rattp117/Messaging-helper.git (origin; push every release — user decision 2026-08-21)
- **Status:** ✅ v1.2.0 RELEASED — multi-user: chat-ID identity, /start onboarding + owner-approval allowlist, full per-user isolation (logs/streaks/targets/reminders/quiet/lang), per-user reminder schedules (/remind, minutely tick), /lang + /quiet. All 29 ACs PASS; suite 1325/0/1; integration Vera verdict PASS incl. migration rehearsal. Deployed via service bounce (migration 006 + owner attribution on live DB). Garmin: off until user sets `[garmin] csv_path`.
- **Last updated:** 2026-08-21 · **Last commit:** v1.2.0

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
| 0.8.0 | 2026-08-19 | NL queries: bilingual interrogative routing, LLM intent classification (fail-closed), read-only answers via registry-generic aggregations; ACs 8.1–8.5 PASS (534 tests) | core/query.py, core/commands.py, llm/prompts.py, core/i18n.py, main.py, tests/test_query.py, tests/test_v08_query_gaps.py | v0.8.0 |
| 0.9.0 | 2026-08-19 | Adaptive reminders: goal-met skip (fail-open), quiet hours (opt-in, midnight-crossing), bilingual snooze one-shots; ACs 9.1–9.5 PASS (611 tests) | core/reminders.py, core/commands.py, core/habits.py, core/i18n.py, main.py, config.py, config.toml, tests/test_adaptive_reminders.py, tests/test_v09_gaps.py | v0.9.0 |
| 0.10.0 | 2026-08-19 | Streaks (shared engine w/ review), gentle milestones 3/7/30 once-per-crossing, nightly 21:45 summary; ACs 10.1–10.5 PASS (650 tests) | core/streaks.py, core/review.py, core/reminders.py, core/i18n.py, main.py, config.py, config.toml, tests/test_streaks.py | v0.10.0 |
| 1.0.0 | 2026-08-19 | Capstone: weekly-review chart PNGs (optional matplotlib, graceful fallback), Channel.send_image (sendPhoto), Garmin CSV import (off by default); ACs 1.0.1–1.0.5 PASS (701 tests); stability declared | channels/{base,telegram}.py, core/{charts,garmin,review,i18n}.py, main.py, config.py, config.toml, pyproject.toml, README.md, tests/test_charts.py, tests/test_garmin.py | v1.0.0 |
| 1.0.1 | 2026-08-21 | Ops: Task Scheduler persistence (task "Habit Assistant", boot+logon, S4U, restart-on-failure); launcher now self-logs + single-instance guard (kills orphan pollers → no 409s); elevated bounce script | start-assistant.ps1, bounce-assistant.ps1 | v1.0.1 |
| 1.1.0 | 2026-08-21 | Undo inline button on every confirmation + Telegram command menu (/undo /target /help /habits); per-habit targets: DB override via migration 005, deterministic /target + full-NL fail-closed LLM intent (2.5L→2500ml, EN/TH); /help + /habits discoverability; goal source consolidated into core/targets.effective_goal; all 40 ACs PASS, 959/0/1 incl. 6 repaired legacy flakes | channels/*, core/{targets,targets_command,target_nl,undo_ui,discoverability,commands,i18n,streaks,review,charts,reminders}.py, storage/{db,migrations}.py, llm/prompts.py, main.py, 12 test files | v1.1.0 |
| 1.2.0 | 2026-08-21 | Multi-user: users table + migration 006 (sanctioned schema break: habit_targets rebuilt UNIQUE(user_id,habit_id)) + startup owner attribution; access gate (pending/approve/block, owner admin cmds, polite denial, notify-once); per-chat channel + callback ownership; per-user targets/streaks/review/summary/undo isolation (U-ISO); per-user reminder times (/remind + minutely tick, no-restart); /lang + /quiet (Thai aliases hardened: whitelist/registry-anchored after 2 Vera rounds); menu → 8 public cmds; all 29 ACs PASS, 1325/0/1 | storage/*, channels/*, core/{access,preferences,schedules,reminders,commands,i18n,...}.py, main.py, config.toml, 20+ test files | v1.2.0 |

## Decisions
- 2026-08-19 — **User update: runtime host is this Windows box** (24/7), with Ollama remote at `http://mac-mini:11434` (verified reachable). Default model `qwen3.5:9b-mlx`. Windows keep-alive via Task Scheduler + launcher script; launchd plist kept as alternative macOS deploy.
- 2026-08-19 — Dev machine is Windows Server (no local Ollama, no system Python); build cross-platform, unit tests mock Ollama/Telegram, live extraction smoke-tested against mac-mini.
- 2026-08-19 — Repo root = this working directory ("Messaging AI assistant"), not a nested `habit-assistant/` subfolder.
- 2026-08-19 — Skipped Sophia/Maya/Iris/Irine: user's prompt is a complete spec, no UI, stack dictated. SEQUENTIAL mode (build order is a dependency chain).
- 2026-08-19 — Git local-only by default (couldn't ask user mid-run); revisit if user wants a remote.
- 2026-08-19 — Use `uv` for venv + deps (available; system Python absent).

- 2026-08-21 — Service persistence: Task Scheduler task "Habit Assistant" registered (elevated via UAC; boot + logon triggers, S4U as Demo, restart 99×/1min, IgnoreNew). Stop-ScheduledTask orphans the python tree → launcher now has a single-instance kill sweep; elevated `bounce-assistant.ps1` for clean release bounces.

## Open questions / Next steps
- Known issue (non-blocking, from v1.0.0 TEST.md): Thai text inside chart PNGs renders as tofu boxes (matplotlib default font lacks Thai glyphs). Follow-up: bundle a Thai-capable font (e.g. Noto Sans Thai). Captions/text unaffected.
- Vera: TEST.md for ACs 1–11 (in flight).
- On Vera PASS: write real creds to `.env` (token from user; chat ID 1574572064 captured from /start), release v0.1.0 (commit + tag), start the bot on this box (Task Scheduler / start-assistant.ps1).
- Sophia (in flight): `ROADMAP.md` — next 10 versions of improvements. User has pre-approved implementing all 10 sequentially (Luna↔Vera per version, one release each); pause only for versions Sophia flags as needing user decisions.
- Ask user whether they want a git remote.
