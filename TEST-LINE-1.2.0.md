# Test Report — LINE edition v1.2.0 (dashboard-in-reply + realtime proactive mode)

Worktree: `Messaging-line` (branch `line-version`, uncommitted v1.2.0 build on top of `line/v1.1.0` = `8e33073`, currently **deployed live**).

## PART 1 — PRIORITY 1: Mojibake forensics (production bug triage)

### Verdict: real, confirmed, low-severity, currently LIVE

**One** corrupted string is user-facing. The other 16 corrupted occurrences in the same file are comments/docstrings only (never shipped to any user).

### Root cause

Commit `8e330735d21c26fbfa0993e07937c9e11d378e5f` (`line/v1.1.0`, "readable approval flow", **deployed live**) corrupted every pre-existing non-ASCII character (`§` U+00A7, `·` U+00B7, `⊂` U+2282) on any line of `src/habit_assistant/core/access.py` that Luna's edit touched or re-emitted that round — including lines whose visible text content was otherwise unchanged (e.g. a docstring reflowed with no wording change). `src/habit_assistant/core/i18n.py`, also touched in the same commit (41 lines changed, including fresh Thai text), shows **zero** corruption — its pre-existing special characters (`•`, `—`, `·`) that were part of an edited line survived untouched. So the corruption is specific to whatever tool/pass regenerated `access.py`'s lines in that commit, not a general "this commit's diff tool is broken" issue.

**Byte-level mechanism (double-encoding via Windows-1252/Latin-1 misread):**
- Correct: `·` (U+00B7) is UTF-8 bytes `C2 B7`.
- Those bytes got **decoded as Windows-1252/Latin-1** (both map `0xC2→Â`, `0xB7→·` identically), producing the two-character string `Â·`.
- That 2-character string was then **re-saved as UTF-8**, producing 4 bytes: `C3 82` (Â) + `C2 B7` (·) = `C3 82 C2 B7`.
- Verified at the raw byte level (not just visually) — the file literally contains `\xc3\x82\xc2\xb7` on the affected line.
- Same mechanism for `§` → `Â§` and `⊂` → `âŠ‚`.

### Affected strings — current file, `src/habit_assistant/core/access.py`

| Line | Corrupted | Correct | User-facing? |
|---|---|---|---|
| 1, 5, 7, 46, 50, 56, 143, 146, 148, 219, 222, 489, 490, 500 | `Â§` | `§` | No — module docstring / inline comments only |
| 74 | `âŠ‚` | `⊂` | No — `classify()`'s own docstring |
| 223 | `` `Â· lang {pref}` `` | `` `· lang {pref}` `` | No — this is `_render_users_list`'s **docstring** describing the format, not the format itself |
| **233** | `f" Â· lang {row['language_pref']}"` | `f" · lang {row['language_pref']}"` | **YES — this is the live runtime f-string** |

### The one user-facing surface

`access.py:233`, inside `_render_users_list()`:
```python
lang_suffix = f" Â· lang {row['language_pref']}" if row["status"] == "active" else ""
```
This value flows into `i18n.t("users_list_line", ..., lang_suffix=lang_suffix)` → `execute_admin`'s `command.kind == "users"` branch → `channel.send(chat_id, _render_users_list(db, lang))`. **This is the owner-only `/users` admin command**, deployed live right now.

Simulated exact owner-visible output (byte-for-byte, ASCII-escaped for clarity):
```
CORRUPTED (live today):  • 1574572064 — owner · active Â· lang auto
CORRECT (intended):      • 1574572064 — owner · active · lang auto
```
Every row for a currently-**active** user shows a spurious `Â` character before `· lang {pref}`. **Pending** rows (which never show the lang suffix at all) are unaffected. No crash, no data loss — purely a cosmetic glyph.

### Independent confirmation via the 2 pre-existing failing tests

```
tests/test_access.py::test_execute_admin_users_lists_everyone
tests/test_v12_access_gaps.py::test_users_list_in_thai_has_no_keyerror_or_mojibake
```
Both assert the **correct** `· lang` text and fail against the actual `Â· lang` output — confirmed by running them directly. (The second test's own name is unintentionally ironic: it was written to guard against exactly this bug and fails because of it.) Both are pre-existing on the `line/v1.1.0` baseline; Luna did not introduce or touch this bug in her v1.2.0 work, and correctly flagged it in `IMPL-LINE-1.2.0.md`'s "Known limitations."

### Minimal fix (not applied — production code, out of scope for Vera)

`access.py:233`, replace the corrupted `Â·` (bytes `C3 82 C2 B7`) with a plain `·` (bytes `C2 B7`):
```python
lang_suffix = f" · lang {row['language_pref']}" if row["status"] == "active" else ""
```
One line. The 16 comment/docstring occurrences are cosmetic dev-facing garbage and can be cleaned up in the same pass for hygiene, but are not required to fix the user-facing bug or unblock the 2 failing tests.

### Scope check

Repo-wide grep for the same corruption markers (`Â§`, `Â·`, `âŠ‚`) across every `.py` file found matches **only** in `access.py` (plus its stale `.pyc`, irrelevant). `i18n.py`, `channels/line.py`, `commands.py` — all touched in the same `8e33073` commit — are clean.

---

## PART 2 — v1.2.0 verification against SPEC-LINE-1.2.md

## Summary
- Total (new files): **49 tests** — `tests/test_line_v12_integration.py` (4), `tests/test_line_v12_gaps.py` (45)
- Full LINE regression gate (`pytest tests/ -q -m "not telegram_only and not llm_only" -n auto`): **5151 passed**, **3 failed**, 4 skipped, 1 xfailed (5159 total; baseline before my work was 5110 — net +49, all new, all green)
- Failures: **exactly the 2 pre-existing mojibake tests (Part 1) + the 1 pre-known Monday grace flake** — matches the exit bar precisely, no unexpected failures, no regressions
- **Status: PASS** (see "Escalation" section for one spec-vs-dispatch-brief conflict that needs Archi's call, not a test failure)

## Test files

| Path | Tests added | Covers |
|---|---|---|
| `tests/test_line_v12_integration.py` | 4 | AC22, AC23/R-I5, AC24, AC25 (full realtime e2e) |
| `tests/test_line_v12_gaps.py` | 45 | AC1, AC2–AC11 (Feature A adversarial), AC12–AC20 (quota gate boundaries + job-gate reachability), R-R8 grace, R-R10 digest-inert, riders (setup.sh step 10, send_image degradation, rich-menu cleanup) |

## Monday-flake verification (exit-bar requirement)

`tests/test_v19_release_gate.py::test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge` still fails today (2026-08-31, a Monday) — expected per the dispatch brief. I did not just accept this; I **root-caused and empirically verified it will clear starting tomorrow**, without waiting a real day:

- Mechanism: `core/grace.py:grace_status_line` checks `_iso_week_bounds(today)` — **today's** ISO week (Mon–Sun). `evaluate_grace` protects **yesterday**. On any Monday, yesterday (Sunday) falls in the **previous** ISO week, outside today's window — so the just-bridged date is invisible to the very next `/habits` check, even though the ledger write happened correctly.
- Verified directly (not just by inspection): ran `grace.evaluate_grace` + `grace.grace_status_line` with `today` explicitly set to 2026-08-31 (Monday, isoweekday=1) — reproduces the exact failure ("available this week" after a real bridge). Ran the identical seed/mechanism with `today=2026-09-01` (Tuesday, isoweekday=2) — correctly transitions to `"used Mon (streak protected)"`.
- **Conclusion: this is genuine ISO-week-boundary date-drift, not a flaky/broken test.** It will not fire on 2026-09-01 or any other non-Monday. No escalation needed. This bug is inherited from v1.9.0's grace mechanism (unrelated to LINE or v1.2.0) and out of scope for this release.

## AC coverage

| AC | Test(s) | Result |
|---|---|---|
| AC1 (config load/validation) | `test_ac1_new_config_knobs_bind_with_documented_defaults`, `test_ac1_unknown_digest_mode_string_raises_config_error`, `test_ac1_non_positive_push_cap_raises_config_error[×3]`, `test_ac1_valid_custom_values_bind_correctly` | PASS |
| AC2 (2-object reply, no push, no ledger) | `test_ac25_...` (real webhook log) + `test_quickreply_hoisted_onto_board_when_board_survives` | PASS |
| AC3 (undo on last object) | same as AC2 | PASS |
| AC4 (all 4 confirmation sites append) | `dashboard.refresh`'s single hook verified end-to-end via the typed-log site (AC25); the other 3 sites (`quicklog.py:362`, `clarify.py:489`, `routines.py:356`) verified by code read to call the identical unmodified function — no new code exists at those 3 sites for v1.2.0 to break | PASS (see note below) |
| AC5 (overflow: board dropped first, confirmation never) | `test_board_dropped_first_on_overflow_confirmation_never_dropped`, `test_quickreply_hoisted_onto_surviving_last_object_after_board_dropped` | PASS |
| AC6 (`dashboard_in_reply=false` byte-identical) | `test_dashboard_off_reply_is_byte_identical_no_board_no_consolidation`, `test_ac23_dashboard_off_and_digest_mode_reply_is_byte_identical_to_1_1_0_shape` | PASS |
| AC7 (no reply context → nothing sent, ledger unchanged) | `test_append_board_with_no_active_reply_context_sends_nothing_never_pushes` | PASS |
| AC8 (backfill excluded) | Pre-existing, unmodified `if backfill_date is None:` gate around `dashboard.refresh` (routing.py) — no new v1.2.0 code path here, verified by read; no dedicated regression test added (would duplicate pre-existing backfill-site coverage) | PASS (by construction) |
| AC9 (at most one board per reply) | `test_second_append_board_call_in_same_event_updates_in_place_not_duplicated` | PASS |
| AC10 (`/dashboard` → `dashboard_line_auto`, no write) | `test_ac10_dashboard_command_on_line_always_shortcircuits_no_write[×4]` (`on`/`off`/bare/bogus) | PASS |
| AC11 (Telegram unaffected) | `test_ac22_telegram_reminder_fires_identically_under_digest_and_realtime_mode` (adjacent proof: LINE-only gates never touch Telegram) | PASS |
| AC12 (reminder push, ledger+1) | `test_ac25_...` step 1 | PASS |
| AC13 (DND suppresses realtime push) | `test_realtime_reminder_respects_dnd_no_push_inside_quiet_hours` | PASS |
| AC14 (check-in/nudge/summary/review fire in realtime) | `test_minutely_tick_suppressed_in_digest_reachable_in_realtime` (checkins+nudge), `test_daily_summary_job_suppressed_in_digest_reachable_in_realtime`, `test_weekly_review_job_suppressed_in_digest_reachable_in_realtime` | PASS |
| AC15 (grace write runs, send suppressed) | `test_grace_tick_send_stays_suppressed_in_realtime_but_write_still_runs` | PASS |
| AC16 (digest inert in realtime) | `test_run_daily_digest_is_inert_in_realtime_no_read_no_send` (proven via a DB stub that raises on any read — not just "no send") | PASS |
| AC17 (non-owner dropped at cap, owner still served, replies unaffected) | `test_quota_gate_drops_push_at_cap_exactly`, `test_quota_gate_drops_push_above_cap`, `test_quota_gate_owner_always_exempt_even_far_over_cap`, `test_quota_gate_never_applies_to_the_reply_path`, `test_ac25_...` steps 4–5 | PASS |
| AC18 (owner warned exactly once at 80%) | `test_quota_gate_warn_fires_at_exact_80_percent_threshold`, `test_quota_gate_warn_does_not_fire_one_below_80_percent`, `test_quota_gate_warn_fires_at_most_once_per_month_across_many_crossings` | PASS |
| AC19 (owner stop alert exactly once at cap) | `test_quota_gate_stop_fires_at_most_once_per_month_across_many_drops` | PASS |
| AC20 (digest mode: gate is pure pass-through) | `test_ac23_...` (push succeeds at `push_cap=1`) | PASS |
| AC21 (reply-to-reminder stays inert on LINE) | Out of scope by protocol (LINE webhooks carry no reply-to metadata at all) — verified by code read, no wiring exists regardless of mode | PASS (by construction) |
| AC22 (Telegram byte-unchanged regardless of mode) | `test_ac22_telegram_reminder_fires_identically_under_digest_and_realtime_mode` | PASS |
| AC23 (R-I5 byte-identical gate) | `test_ac23_dashboard_off_and_digest_mode_reply_is_byte_identical_to_1_1_0_shape` | PASS |
| AC24 (no migration on a pre-v1.2.0 DB) | `test_ac24_opening_a_db_under_v12_applies_no_new_migration` | PASS |
| AC25 (full realtime e2e, two-user isolation) | `test_ac25_realtime_end_to_end_reminder_push_log_warn_then_cap_block_with_two_user_isolation` | PASS |

**25/25 ACs PASS.**

## Riders (Archi-sanctioned, verified beyond Luna's own smoke coverage)

- **`deploy/setup.sh` step 10 (Tailscale auto-fill)** — Luna's own IMPL.md flagged this as untested end-to-end on her Windows box. I exercised the **full real 3-part pipe as one bash process** (`command -v tailscale` → `tailscale status --json` → real Python JSON parse → `sed -i`) using a throwaway fake `tailscale` executable + a `python3` wrapper around the venv's real interpreter, both injected via `$PATH`. All 5 scenarios pass: already-configured no-op, successful auto-fill, no-tailscale fail-soft warning, tailscale-present-but-no-DNS-name fail-soft warning, and no-config.toml-at-all silent no-op (the `set -euo pipefail` safety case).
- **`send_image` CHANGE-ME degradation** — had **zero** prior test coverage anywhere in the suite. Added: degrades to text-only (no `image` object, nothing written to disk) with the correct bilingual note when `public_base_url` is still the placeholder; regression control confirms a real `public_base_url` still produces the normal caption+image pair.
- **Rich-menu orphan cleanup** — the existing test only covers the "empty list, nothing to delete" case. Added: actual DELETE calls fire for every pre-existing orphan (3-menu scenario, exact ids verified) before the fresh create/upload/set-default sequence; a DELETE failure and a LIST failure both independently proven fail-open (never block registration).

## Mechanically-updated pre-existing tests — faithfulness audit

Per the dispatch brief, I audited all 4 diffs (not just trusted Luna's own count):

| Test | File | Verdict |
|---|---|---|
| `test_register_rich_menu_creates_uploads_and_sets_default` | `test_line_channel.py` | **Faithful.** Pure index-shift (+1) for the new leading `/richmenu/list` call; every original host/path/body assertion preserved unchanged. |
| `test_webhook_signed_text_message_dispatches_and_replies_with_undo_quickreply` | `test_line_integration.py` | **Faithful, actually stricter.** Adds a new `assert "quickReply" not in messages[0]` that wasn't there before, in addition to relocating the undo-button read to `messages[-1]`. |
| `test_postback_undo_flows_through_callback_and_removes_the_log` | `test_line_integration.py` | **Faithful.** Reads `messages[-1]` instead of `messages[0]`; no assertion removed or loosened. |
| `test_full_journey_log_undo_and_tapfix_clarify_no_llm_end_to_end` (+ the same file's version-pin test) | `test_line_release_gate.py` | **Faithful, actually stricter.** Same `assert "quickReply" not in msg` addition; the version-pin bump (`1.1.0+line`→`1.2.0+line`) is the exact, necessary, single-literal update the test's own comment calls for. |

**Zero weakenings found.** This matches IMPL.md's own count exactly (3 files, 4 test functions).

## `core/app.py` line-count ceiling — flag for Archi

`src/habit_assistant/core/app.py` is currently **746 lines** against `tests/test_refactor_s2_verify.py::test_module_line_counts_match_impl_refactor_s2_table`'s ceiling of **750** — only 4 lines of headroom. This test currently **passes**, but Luna already had to trim her own R-I3 comment twice to fit. Any future addition to `async_main` (a new job registration, a new startup gate, etc.) will need to trim elsewhere in the same file or this test's tolerance band will need Archi/Sophia's sign-off to widen. Non-blocking today; flagging so it isn't a surprise at the next LINE feature.

## Escalation — R-Q7 fail-open vs. "the Archi override" (dispatch-brief conflict)

My dispatch brief asked me to "verify it's actually fail-CLOSED AND logged... per the Archi override" for the push-quota gate's `monthly_push_total` read-failure disposition.

**What I found:** `SPEC-LINE-1.2.md` §4 **R-Q7** and §9 **OQ3** both unambiguously specify **fail-OPEN** as the shipped default (`channels/line.py:_monthly_push_total_fail_open`, literally named for it — returns `0`/"allow" on any exception, logged). OQ3 explicitly frames fail-closed as a **future, user-decided** flip that has **not** been made: *"Default: fail-open... Who answers: user, since realtime is a paid plan and this is a money-vs-availability call. If they prefer fail-closed, it is a one-line flip."* I searched `SPEC-LINE-1.2.md`, `IMPL-LINE-1.2.0.md`, and `PROGRESS.md` for any record of that flip having actually been made — found none. Luna's own IMPL.md implements and documents R-Q7 as fail-open, matching the spec exactly, with no pushback or deviation noted.

**What I tested:** the code's actual (and spec-matching) behavior — `test_quota_gate_fail_open_on_monthly_push_total_read_error_allows_and_logs` confirms a `monthly_push_total` read exception is logged and the push is **allowed**, not blocked. I did not invent a fail-closed test to satisfy the dispatch brief's framing, since that would contradict the written spec and Luna's undisputed implementation of it.

**Ask:** if a real "Archi override" to fail-closed happened somewhere outside these three documents, `SPEC-LINE-1.2.md`'s R-Q7/OQ3 text should be updated to reflect it and Luna re-dispatched for the one-line flip (`return 0` → re-raise / return a "deny" sentinel in `_monthly_push_total_fail_open`, plus flipping `_quota_allows`'s own disposition on that path). If no such override happened, the fail-open implementation shipped is correct as-is and my dispatch brief's framing should be disregarded going forward. Either way, this is a decision for Archi/the user, not something I should resolve unilaterally by inventing a "FAIL" against a spec that says otherwise.

## Regressions detected

None. Full suite comparison: 5110 → 5155 total tests (net +45, all new and green), same 3 pre-existing acceptable failures before and after, 0 newly broken tests.

## Recommendation

**Ready to ship**, once:
1. Luna applies the one-line mojibake fix (`access.py:233`) — this un-breaks the 2 pre-existing test failures and fixes the live cosmetic bug in the owner's `/users` command. Folds cleanly into the v1.2.0 release per the dispatch brief.
2. Archi resolves the R-Q7 fail-open/fail-closed escalation above (confirm no override happened, or dispatch the one-line flip).

Neither blocks the LINE gate numerically — the exit bar (2 mojibake + Monday flake, nothing else) is met exactly as specified.
