# Habit-Tracking Assistant — Development Progress

- **Current version:** 0.1.0
- **Repo:** local-only (user not asked yet — autonomous session; change on request)
- **Status:** Phase 5 — Luna implementing MVP per SPEC.md; Vera to test next
- **Last updated:** 2026-08-19 · **Last commit:** (initial)

## Stack
Python 3.11+ (uv-managed venv) · asyncio · APScheduler (AsyncIOScheduler) · httpx (Telegram long-poll + Ollama) · stdlib sqlite3 (WAL) · pydantic-settings · pytest. Stack dictated by user spec — Irine skipped.

## Deliverables
- [x] SPEC.md — user-provided spec, saved verbatim at repo root (+ AC list added in §11)
- [ ] UX.md — skipped (no UI surface; chat bot + CLI)
- [ ] UI.md — skipped (no UI surface)
- [ ] STACK.md — skipped (stack fully dictated in SPEC.md §1–§4)
- [ ] IMPL.md
- [ ] TEST.md

## Changelog
| Version | Date | Summary | Files | Commit/Tag |
|---|---|---|---|---|
| 0.1.0 | 2026-08-19 | initial scaffold (spec, git, progress) | SPEC.md, .gitignore, VERSION, PROGRESS.md | (pending) |

## Decisions
- 2026-08-19 — Dev machine is Windows Server (no Ollama, no system Python); deploy target is user's Mac Mini. Build cross-platform, test with mocked Ollama/Telegram; launchd plist + README target macOS.
- 2026-08-19 — Repo root = this working directory ("Messaging AI assistant"), not a nested `habit-assistant/` subfolder.
- 2026-08-19 — Skipped Sophia/Maya/Iris/Irine: user's prompt is a complete spec, no UI, stack dictated. SEQUENTIAL mode (build order is a dependency chain).
- 2026-08-19 — Git local-only by default (couldn't ask user mid-run); revisit if user wants a remote.
- 2026-08-19 — Use `uv` for venv + deps (available; system Python absent).

## Open questions / Next steps
- Luna: implement per SPEC.md build order §11 → IMPL.md.
- Vera: test ACs 1–11 (mock Ollama + Telegram) → TEST.md.
- User must supply on the Mac Mini: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, actual Ollama model tag.
- Ask user whether they want a git remote.
