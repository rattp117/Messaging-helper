# Test Report — v1.8.1 gap-fix (`/help` coverage) + release-prep test adaptation

## Summary
- Patch verdict: **PASS**
- Full suite: 3799 passed / 0 failed / 1 skipped / 1 xfailed (was 3798/0/1/1 before this session — the +1 is the new `1.8.1`-pinned announce no-op test added below)
- Status: **PASS**

## Part 1 — Patch verification (`/help` gap-fix)

### Rendered output, both languages
Rendered `build_help_text(Config(), "en")` and `build_help_text(Config(), "th")` directly and inspected the three new sections (last of 24 sections in both languages):

- EN: `👇 One-tap quick-log: /log pops a keyboard of your habits, tap once to log (Thai: "บันทึก").` / `📋 Bundle a habit stack into one command: /routine morning = water 500, stretch 10, then /routine morning to run it (Thai: "กิจวัตร ...").` / `📅 Log for a past day: add "yesterday", "3 days ago", or "on Monday" to your log — up to 14 day(s) back.`
- TH: `👇 บันทึกด่วนแบบแตะเดียว: พิมพ์ /log เพื่อเปิดปุ่มนิสัยของคุณ แตะครั้งเดียวก็บันทึกได้ (หรือ "บันทึก")` / `📋 รวมชุดนิสัยไว้ในคำสั่งเดียว: /routine morning = water 500, stretch 10 แล้วพิมพ์ /routine morning เพื่อรัน (หรือ "กิจวัตร ...")` / `📅 บันทึกย้อนหลังได้: เติม "เมื่อวาน", "3 วันที่แล้ว" หรือ "วันจันทร์" ต่อท้ายข้อความบันทึก — ย้อนหลังได้สูงสุด 14 วัน`

Findings:
- Real Thai in both variants — no tofu (`�`), no placeholder text, no mixed-script leakage into the wrong variant. Emoji-led, imperative, one-liner shape matches every neighboring `help_*` entry (e.g. `help_addhabit_cmd`, `help_snooze`).
- `/log` line names the Thai alias (`บันทึก`) in **both** the EN and TH renders — matches the `help_undo`/`help_lang`/etc. convention of always surfacing the Thai alias regardless of reply language.
- `/routine` line covers both create (`/routine morning = water 500, stretch 10`) and run (`then /routine morning to run it`) — both syntaxes present in one line, both languages.
- `help_backfill` shows the three recognized phrase shapes ("yesterday" / "N days ago" / "on <weekday>", and their Thai equivalents) and interpolates `config.backfill.max_days_back` live: re-rendered with `Config.model_validate({"backfill": {"max_days_back": 7}})` and confirmed the line reads "...up to 7 day(s) back." / "...ย้อนหลังได้สูงสุด 7 วัน" (both languages), never the hardcoded default.

### Structural diff (nothing else moved)
`git diff` on both changed source files confirms **pure appends, zero deletions/modifications**:
- `core/discoverability.py`: 12 lines added (3 `lines.append(...)` calls + a comment block), 0 removed.
- `core/i18n.py`: 35 lines added (3 catalog entries), 0 removed.

### Render budget (Telegram 4096-char limit)
Measured with the same metric this codebase's own `tests/test_announce_gaps.py::test_catalog_entries_fit_telegram_limit_with_margin` uses (`len(text)`, i.e. Python character count against Telegram's `sendMessage` char cap — not UTF-8 byte length): EN 2251 chars, TH 2236 chars, both with the `max_days_back=7` variant landing within 1–2 chars of the same. Both comfortably inside 4096, in fact inside the same file's own 500-char safety margin convention (would fit even release-note-strictness).

### Announce fail-open claim
Confirmed at `core/announce.py:65-66` (`if version not in RELEASE_NOTES: return`) — `announce_release` returns before any `db.active_user_ids()` read or `db.set_last_announced_version` write. This was **already covered generically** by `tests/test_announce.py::test_ac22_no_catalog_entry_sends_nothing_and_never_raises` (asserts `channel.sent == []` and `get_last_announced_version(...) is None` for a synthetic unknown version). Added a second, version-pinned test proving the exact real-world case:

`tests/test_announce.py::test_ac22_v181_gap_fix_patch_has_no_catalog_entry_and_announces_nothing` — asserts `"1.8.1" not in release_notes.RELEASE_NOTES` (so the test fails loudly if a future contributor adds an entry without updating this test), then calls `announce.announce_release(db, channel, config, "1.8.1")` directly and asserts zero sends and zero writes for both an owner and an active member. **PASS.**

### Luna's 5 new tests (`tests/test_discoverability.py`)
All reviewed and re-run: `/log`/`/routine`/backfill-syntax mentions in both languages, `max_days` tracking live config (not hardcoded), and the structural "strict append after `/delhabit`, every pre-existing section still present" check. All 5 pass; style matches the file's existing convention (real `Config`, concrete substring checks, no mocks).

**Part 1 verdict: PASS.** No code changes needed.

## Part 2 — Release-prep test adaptation

### The problem
`tests/test_v15_integration.py::test_current_pinned_version_announces_to_active_users_today` hard-pinned `current_version == "1.8.0"` and asserted only the "announces" branch. Both would break the moment Archi bumps `__version__` to `"1.8.1"`, because that patch deliberately ships with **no** `RELEASE_NOTES` entry (announce silently no-ops, per Part 1's finding above).

### The fix
Restructured into two halves, both derived from the real `release_notes.RELEASE_NOTES` catalog rather than a literal version string:

- **Half A** (`_latest_entry_bearing_version()`, a new module-level helper that picks the newest SemVer-tuple key actually present in `RELEASE_NOTES`, currently `"1.8.0"`): proves the "announces and marks caught up" behavior against a version guaranteed to have a real entry — stable across a no-entry patch bump.
- **Half B**: drives today's *actual* `__version__` constant through the same real `async_main` startup wiring, against a freshly-seeded user (not reused from Half A, so this is a genuine end-to-end probe, not a tautology). Branches on `current_version in release_notes.RELEASE_NOTES`: if present, asserts the announce-and-mark behavior (mirrors Half A); if absent (the "1.8.1" shape), asserts silence — zero sends, `get_last_announced_version(...) is None`.

No hardcoded equality anywhere in the adapted test — a future `v1.9.0` release with a notes entry, or a future `v1.8.2` patch without one, exercises the correct branch automatically with zero test edits. Docstring's history note updated with an "UPDATED AGAIN at v1.8.1" entry explaining the restructuring and why (matches the file's own established "UPDATED AGAIN at vX.Y.Z" convention used by its two prior updates).

### Verification (both release shapes)
Ran the single adapted test with `src/habit_assistant/__init__.py:__version__` transiently edited (via Edit tool, then reverted — confirmed by an empty `git diff` on that file afterward, production code untouched in the final state):

| `__version__` state | Shape exercised | Result |
|---|---|---|
| `"1.8.1"` (simulated post-bump) | Half B falls into the "no entry → silent no-op" branch | **PASS** (1 passed) |
| `"1.8.0"` (real, unpatched, current) | Half B falls into the "entry present → announces" branch | **PASS** (1 passed) |

## Test files

| Path | Tests added/changed | Covers |
|---|---|---|
| `tests/test_discoverability.py` | 5 added (Luna) | `/help` v1.8.1 gap-fix content, live config interpolation, structural append-only check |
| `tests/test_announce.py` | 1 added (Vera) | Version-pinned proof that `announce_release("1.8.1")` is a silent no-op (no sends, no writes) |
| `tests/test_v15_integration.py` | 1 adapted in place (Vera) | `test_current_pinned_version_announces_to_active_users_today` — now survives both the pre-bump (1.8.0, has entry) and post-bump (1.8.1, no entry) release shapes without a literal edit |

## AC coverage (v1.8.1 gap-fix scope, per IMPL-v1.8.1.md)

1. Bilingual `/help` additions for `/log`, `/routine`, backfill syntax → `test_help_v181_mentions_log_command_in_both_languages`, `test_help_v181_mentions_routine_command_in_both_languages`, `test_help_v181_mentions_backfill_syntax_in_both_languages`, `test_help_v181_backfill_max_days_read_live_from_config_not_hardcoded`, `test_help_v181_new_lines_appear_after_delhabit_and_structure_otherwise_unchanged` → **PASS**
2. Announce-machinery tolerance (no `RELEASE_NOTES["1.8.1"]` entry → silent no-op) → `test_ac22_v181_gap_fix_patch_has_no_catalog_entry_and_announces_nothing` (new) + `test_ac22_no_catalog_entry_sends_nothing_and_never_raises` (pre-existing, generic) → **PASS**
3. Render budget / structural integrity (Telegram 4096-char limit, pure-append diff) → verified manually above, consistent with `test_catalog_entries_fit_telegram_limit_with_margin`'s own metric → **PASS**
4. Release-prep: suite survives Archi's upcoming version bump to 1.8.1 → `test_current_pinned_version_announces_to_active_users_today` (adapted), verified at both `__version__` states → **PASS**

## Failures (if any)

None.

## Regressions detected

None. Full suite: 3799 passed, 0 failed, 1 skipped, 1 xfailed (baseline was 3798/0/1/1; the delta is exactly the one new test added in Part 1).

## Recommendation

**Ready to ship.** Archi can proceed to bump `__version__` to `"1.8.1"` (Phase 6.5) — `tests/test_v15_integration.py::test_current_pinned_version_announces_to_active_users_today` is now verified to pass unedited both before and after that bump.
