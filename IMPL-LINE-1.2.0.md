# Implementation — LINE edition v1.2.0: dashboard-in-reply + real-time proactive mode

## Files changed

| Path | Created/Modified | Description |
|---|---|---|
| `src/habit_assistant/config.py` | Modified | `LineConfig.dashboard_in_reply: bool = True` (R-S1); `DigestConfig.mode: Literal["digest","realtime"]="digest"` + `push_cap: int=15000` + positive-int validator (R-S2). |
| `src/habit_assistant/channels/base.py` | Modified | `Channel.append_board(chat_id, text) -> None` concrete no-op default (R-S3). |
| `src/habit_assistant/channels/line.py` | Modified | `_push` split into `_send_push` (raw push + ledger, verbatim pre-1.2.0 body) and a new gated `_push` (realtime quota gate, R-S4/R-Q2); `_monthly_push_total_fail_closed`/`_quota_allows`/`_maybe_alert_quota_warn`/`_maybe_alert_quota_stop` helpers (R-Q3-R-Q7, **fail-closed per Archi ruling 2026-08-31, round 2**); `append_board` override (R-A3/R-A6); `_flush_reply` quickReply consolidation (R-A5); in-memory `_quota_warned_months`/`_quota_stopped_months` guards (R-Q6). **Riders:** `send_image` CHANGE-ME degradation (bilingual text instead of a broken image link); `_cleanup_stale_rich_menus` + call from `register_rich_menu` (orphan cleanup). |
| `src/habit_assistant/core/access.py` | Modified (round 2) | Fixed 17 pre-existing mojibake occurrences (`Â§`→`§`, `Â·`→`·`, `âŠ‚`→`⊂`), including the one live bug in `_render_users_list`'s `lang_suffix`. Unrelated to Feature A/B; Archi fold-in. |
| `SPEC-LINE-1.2.md` | Modified (round 2) | §4 R-Q7, §9 OQ3, §3.4, and the §1 problem statement updated to record the fail-closed ruling (Archi-sanctioned spec-doc edit). |
| `src/habit_assistant/core/i18n.py` | Modified | New keys `dashboard_line_auto`, `push_quota_warn`, `push_quota_stop` (R-S5), `line_public_url_unconfigured` (CHANGE-ME rider). |
| `src/habit_assistant/core/dashboard.py` | Modified | `refresh` gained the R-A1 append-board hook (before the pin/edit machinery); `execute_dashboard` gained the R-A9 LINE short-circuit to `dashboard_line_auto`. |
| `src/habit_assistant/core/jobs.py` | Modified | Four suppression gates (`minutely_tick`, `weekly_review_job`, `daily_summary_job`, `wrapped_auto_job`) changed from `type=="line"` to `type=="line" and mode!="realtime"` (R-I1); `grace_tick`'s send gate left unchanged (R-R8, documented). |
| `src/habit_assistant/core/digest.py` | Modified | `run_daily_digest` early-returns when `mode=="realtime"`, before any read/send (R-I2/R-R10). |
| `src/habit_assistant/core/app.py` | Modified | Startup announcement gate: `type != "line" or mode == "realtime"` (R-I3/R-R6). Comment trimmed for the pre-existing `test_module_line_counts_match_impl_refactor_s2_table` tolerance band (see Iteration log). |
| `VERSION`, `src/habit_assistant/__init__.py`, `pyproject.toml` | Modified | `1.1.0+line` → `1.2.0+line`. |
| `config.toml.line` | Modified | Documents `mode`/`push_cap` under `[digest]` and `dashboard_in_reply` under `[line]`, both set to their defaults. |
| `config.toml` | Modified | Commented-out documentation block for the three new LINE-only knobs (N/A on Telegram), mirroring the existing `[audit]`/`[custom_habits]` convention. |
| `deploy/setup.sh` | Modified | **Rider 1(a):** new step 10 auto-fills `[line].public_base_url` from `tailscale status --json`'s `Self.DNSName` when the CHANGE-ME placeholder is present; fail-soft (loud warning, placeholder left alone) if tailscale/python is unavailable or returns nothing useful. |
| `docs/DEPLOY-LINE.md` | Modified | Documents setup.sh's new step 9 (auto-fill) numbered-list entry and a note in the manual-config section. |
| `tests/test_line_channel.py` | Modified | Updated `test_register_rich_menu_creates_uploads_and_sets_default` for the new leading `/v2/bot/richmenu/list` cleanup call (rider 2). |
| `tests/test_line_integration.py` | Modified | Updated 2 pre-1.2.0 tests (`test_webhook_signed_text_message_dispatches_and_replies_with_undo_quickreply`, `test_postback_undo_flows_through_callback_and_removes_the_log`) for the new default 2-object reply shape (board appended, undo relocated to the last object). |
| `tests/test_line_release_gate.py` | Modified | Same reply-shape update for `test_full_journey_log_undo_and_tapfix_clarify_no_llm_end_to_end`; version-pin literals bumped to `1.2.0+line`. |
| `tests/test_line_v12_gaps.py` | Modified (round 2) | Vera's own file — flipped exactly one test (`test_quota_gate_fail_closed_on_monthly_push_total_read_error_drops_and_logs`, renamed from `..._fail_open_...`) to the fail-closed expectation, citing the Archi ruling; updated the module docstring's "fail-open vs fail-closed" note to reflect resolution. No other test in this file touched. |

**Riders implemented alongside the spec** (Archi-sanctioned, 2026-08-31): the `public_base_url` CHANGE-ME guard (deploy-time auto-fill + runtime degradation) and rich-menu orphan cleanup — both cited above, in `channels/line.py` and `deploy/setup.sh`.

## How it works

**Feature A (dashboard-in-reply):** `core/dashboard.py:refresh` is already called by every state-changing reactive site (typed log, quick-log tap, clarify tap, routine run, undo, edit, target/cadence/pause/resume). One new hook there — gated on `config.channel.type=="line"` and `config.line.dashboard_in_reply` — renders the compact board and calls `channel.append_board(user_id, text)` *before* the (permanently inert on LINE) pin/edit machinery. `LineChannel.append_board` appends a `{"type":"text",...}` object to the active `_REPLY_CONTEXT` buffer (or no-ops with no active context / no earlier buffer content — never a push, never a board-only reply), holding a reference so a second call in the same event updates the same object in place instead of duplicating it. `_flush_reply` truncates to 5 objects (unchanged — the board rides last, so it's dropped first) and then relocates `quickReply` onto the final surviving object, since LINE only renders the *last* object's own quick-reply row.

**Feature B (real-time proactive mode):** `LineChannel._push` (renamed the raw push send to `_send_push`) is now the single gated chokepoint every no-reply-context send funnels through (via `_emit`). In digest mode it's a pure pass-through. In realtime mode, non-owner sends check `db.monthly_push_total(yyyymm)` (a GLOBAL total across every user, including the owner) against `config.digest.push_cap`; at/over cap the push is dropped (no send, no ledger increment) and a once/month owner "stop" alert fires; at/over 80% an allowed push also triggers a once/month owner "warn" alert. Both alerts go out via `_send_push` directly (never re-entering the gate) and therefore also count against the same global cap. `core/jobs.py`'s five suppression gates flip from `type=="line"` to `type=="line" and mode!="realtime"` for four jobs (reminders/check-ins/nudge via `minutely_tick`, weekly review, daily summary, wrapped auto-send) — `grace_tick`'s send stays suppressed unconditionally (R-R8, gentleness+quota). `core/digest.py:run_daily_digest` and `core/app.py`'s startup announcement gate complete the mode-exclusivity wiring (R-I2/R-I3).

## Smoke test done

1. **Full LINE regression gate**, twice (before and after fixes below):
   `pytest tests/ -q -m "not telegram_only and not llm_only" -n auto`
   Final result: **5102 passed, 3 failed (all pre-existing, see Iteration log), 4 skipped, 1 xfailed** in ~84s.
2. **Config load/validation smoke** (`python -c ...`): confirmed `Config().line.dashboard_in_reply is True`, `.digest.mode=="digest"`, `.digest.push_cap==15000`; confirmed `Config.model_validate({"digest":{"mode":"bogus"}})` and `{"push_cap":0}` both raise `ValidationError`, and `load_config()` wraps that into `ConfigError` (AC1).
3. **Module import smoke**: all touched modules (`channels.line`, `channels.base`, `core.dashboard`, `core.jobs`, `core.digest`, `core.app`) import cleanly; confirmed `LineChannel` now exposes `_send_push`, `_push`, `append_board`, `_quota_allows`.
4. **Standalone async smoke script** (`LineChannel` + `dashboard.refresh` directly, mirroring `tests/test_line_channel.py`'s own fixtures) covering: AC2/AC3 (2-object reply, undo relocated), AC5 (overflow drops the board, confirmation + its quickReply survive), AC6 (`dashboard_in_reply=false` → single object, byte-shape unchanged), AC7 (no reply context → nothing sent, ledger unchanged), AC9 (two `refresh` calls in one event → still one board object), AC10 (`/dashboard` on/off/bare/bogus all → `dashboard_line_auto`, no write), AC17-AC19 (quota gate: 80% owner warn fires exactly once, 100% owner stop fires exactly once, non-owner drop confirmed via unchanged ledger, owner push always succeeds), AC20 (digest mode: `push_cap=1` never triggers the gate across 3 pushes — proven pass-through). All passed.
5. **Riders**, verified directly: `send_image` against a `public_base_url` still containing `CHANGE-ME` → ERROR logged, reply degrades to `[caption, line_public_url_unconfigured]` text objects, no `image` type object ever sent. `deploy/setup.sh` step 10 extracted and run standalone (mirroring `tests/test_deploy_line.py`'s own `_run_step_6` convention): "already configured" path leaves a real URL untouched; "no tailscale" path leaves the placeholder and logs a loud warning; the JSON `Self.DNSName` extraction snippet verified directly against a real `tailscale status --json`-shaped payload. `bash -n deploy/setup.sh` passes; file stays LF-only/no-BOM.

### Round 2 verification (after Vera's pass + Archi's two fold-ins)

- **Exit-bar selection** (foreground, no background waits): `pytest tests/test_line_v12_gaps.py tests/test_line_v12_integration.py tests/test_access.py tests/test_line_readable_approval.py -q` → **108 passed, 0 failed** in 14.19s.
- **Full LINE gate** (foreground): `pytest tests/ -q -m "not telegram_only and not llm_only" -n auto` → **5153 passed, 1 failed, 4 skipped, 1 xfailed** in 91.58s. The one failure is `test_v19_release_gate.py::test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge` — the pre-known Monday date-drift flake, unrelated to this release, accepted per Archi's message (self-clears at midnight tonight per Vera's simulation).
- Both mojibake tests (`test_execute_admin_users_lists_everyone`, `test_users_list_in_thai_has_no_keyerror_or_mojibake`) confirmed green; the flipped quota-gate test confirmed green in isolation before the full run.

## Maps to acceptance criteria

- **AC1** → `config.py:LineConfig.dashboard_in_reply` / `DigestConfig.mode`/`push_cap` (+ validators); wrapped by `load_config`'s existing `ConfigError` translation.
- **AC2** → `dashboard.py:refresh` (append hook) + `channels/line.py:append_board`/`_emit` (no push path touched).
- **AC3** → `channels/line.py:_flush_reply` (quickReply consolidation).
- **AC4** → `dashboard.py:refresh`'s single hook, reached by every confirmation call site (`routing.py`, `quicklog.py`, `clarify.py`, `jobs.py`'s dashboard rollover isn't a confirmation site but shares the same `refresh` call — routine run confirms via `routing.py`/`routines.py`'s own existing `refresh` call, unmodified).
- **AC5** → `channels/line.py:_flush_reply` (existing 5-object truncation, unchanged, + new consolidation).
- **AC6** → `dashboard.py:refresh`'s `config.line.dashboard_in_reply` gate.
- **AC7** → `channels/line.py:append_board`'s `ctx is None` early return.
- **AC8** → falls out of `routing.py`'s pre-existing `if backfill_date is None: await dashboard.refresh(...)` — no change needed (R-A2).
- **AC9** → `channels/line.py:append_board`'s `ctx.get("boardObj")` in-place-update branch.
- **AC10** → `dashboard.py:execute_dashboard`'s LINE short-circuit.
- **AC11** → `channels/base.py:Channel.append_board` no-op default; `dashboard.py:refresh`'s gate never reaches Telegram.
- **AC12-AC14** → `core/jobs.py`'s four flipped gates (existing per-user DND/goal-met/pause suppression in `reminders.py`/`checkins.py`/`nudge.py`/`review.py`/`streaks.py` unchanged, now reachable).
- **AC15** → `core/jobs.py:grace_tick` unchanged (R-R8).
- **AC16** → `core/digest.py:run_daily_digest`'s realtime early-return.
- **AC17** → `channels/line.py:_push`'s owner-bypass + cap check.
- **AC18** → `channels/line.py:_maybe_alert_quota_warn` + `_quota_warned_months`.
- **AC19** → `channels/line.py:_maybe_alert_quota_stop` + `_quota_stopped_months`.
- **AC20** → `channels/line.py:_push`'s `mode != "realtime"` pass-through branch.
- **AC21** → out of scope by design (§10), unaffected — no reply-to-reminder wiring exists on LINE regardless of mode.
- **AC22** → every `core/jobs.py`/`app.py` gate is `type=="line" and ...`; Telegram (`type!="line"`) is never touched by any of them, by construction.
- **AC23** → verified indirectly: `dashboard_in_reply=false` byte-identical smoke-tested (AC6); `mode="digest"` byte-identical smoke-tested (AC20); full pre-existing LINE integration suite (`test_line_integration.py`, `test_line_release_gate.py`, `test_line_c_gaps.py`, etc.) passes unmodified except the 2+1 tests updated for the new *default* (dashboard_in_reply=true) shape — none of those updates touch the `dashboard_in_reply=false`/`mode="digest"` combination itself.
- **AC24** → no migration added (`storage/migrations.py` untouched); the full test run's own migration log confirms schema stays at version 14.
- **AC25** → covered piecemeal by the AC12-AC19 smoke tests + the pre-existing `test_two_user_isolation_through_full_wired_pipeline`/U-ISO suite (unmodified, still passing) — Vera's own `tests/test_line_v12_integration.py` now carries the dedicated single end-to-end realtime walkthrough (per §6); see Round 2 verification above.

## Known limitations

- **(Round 1, now superseded) Vera's own test files were not yet written.** They are now: `tests/test_line_v12_gaps.py` (adversarial gap-probe) and `tests/test_line_v12_integration.py` (e2e walkthrough, per TEST-LINE-1.2.0.md) both exist and are part of the exit-bar test selection below.
- **(Round 1, now fixed) Mojibake in `core/access.py`.** Was flagged here as pre-existing/out-of-scope; fixed this round per Archi's explicit fold-in instruction — see the Round 2 iteration log entry above.
- **One pre-known flake, exactly as flagged in my original dispatch brief**: `tests/test_v19_release_gate.py::test_ac17_habits_line_transitions_from_available_to_used_after_a_real_grace_bridge` fails today (2026-08-31 is a Monday) — an ISO-week-boundary interaction in the grace mechanism unrelated to LINE v1.2.0. Per Archi's round-2 message, Vera's own simulation shows it self-clears at midnight tonight; accepted as today's only remaining LINE-gate failure.
- **`core/app.py` is right at the pre-existing `test_module_line_counts_match_impl_refactor_s2_table` tolerance ceiling** (746/750 lines) after trimming my own R-I3 comment down twice to fit. Any future addition to `async_main` will need to trim elsewhere or this test's tolerance band will need Archi/Sophia's sign-off to widen — flagging so it isn't a surprise.
- **`deploy/setup.sh` step 10's success path (real `tailscale`+`python3` auto-fill) could only be smoke-tested piecewise on this Windows dev box** (the fail-soft "no tailscale" path and the "already configured" path run end-to-end via a real bash subprocess; the JSON-parsing snippet was verified directly against a real `tailscale status --json`-shaped payload) — the full three-part pipe was not exercised as one process on this box (no Linux venv layout / no real `tailscale` binary here). Recommend Vera (or a real Linux/VPS run) exercise it end-to-end before the next real deploy.

## Iteration log (pre-handoff, self-found via smoke testing)

- **Failure:** `tests/test_line_channel.py::test_register_rich_menu_creates_uploads_and_sets_default` — asserted the *first* captured request was `/v2/bot/richmenu` (create).
  **Root cause:** expected consequence of rider 2 (orphan cleanup) — `register_rich_menu` now calls `_cleanup_stale_rich_menus` (a `GET /v2/bot/richmenu/list`) before create.
  **Fix:** updated the test's index assertions to account for the new leading list call (no DELETE follows in this fixture, since the default handler returns an empty `richmenus` list).

- **Failure:** `tests/test_line_integration.py::test_webhook_signed_text_message_dispatches_and_replies_with_undo_quickreply`, `::test_postback_undo_flows_through_callback_and_removes_the_log`, and `tests/test_line_release_gate.py::test_full_journey_log_undo_and_tapfix_clarify_no_llm_end_to_end` — all asserted a single-object reply with `quickReply` on `messages[0]`.
  **Root cause:** expected consequence of Feature A shipping `dashboard_in_reply=true` by default (AC2/AC3) — every log confirmation now carries a trailing board object, and `quickReply` relocates to the last object.
  **Fix:** updated all three to expect 2 objects, `quickReply` absent from the confirmation and present on the trailing board object.

- **Failure:** `tests/test_refactor_s2_verify.py::test_module_line_counts_match_impl_refactor_s2_table` — `core/app.py`'s AST-derived line count (759) exceeded the test's own generous 750-line tolerance ceiling.
  **Root cause:** my first R-I3 comment draft was ~18 lines; since this metric is `end_lineno`-based, any insertion earlier in a large function shifts every later line number, inflating the count even though no logic changed.
  **Fix:** trimmed the comment twice (down to 6 lines) until the file settled at 746 lines, comfortably under the ceiling.

- **Failure:** `tests/test_line_release_gate.py::test_version_consistent_across_files_and_release_note_posture` — pinned the literal `"1.1.0+line"`.
  **Root cause:** expected consequence of the version bump this release requires (`VERSION`/`__init__.py`/`pyproject.toml` → `1.2.0+line`); the test's own docstring says "a future bump must update this literal too."
  **Fix:** updated both pinned literals to `1.2.0+line`.

No spec pushback and no stack pushback were needed in round 1 — Sophia's `SPEC-LINE-1.2.md` design (shared surface first, then A, then B) mapped cleanly onto the existing `channels/line.py`/`core/dashboard.py`/`core/jobs.py`/`core/digest.py` structure with no structural friction.

### Round 2 (Vera's pass + Archi fold-ins, 2026-08-31)

- **Deviation (honestly owned):** my original dispatch brief explicitly stated *"OQ3 OVERRIDE: the quota gate fails CLOSED on a ledger read error for non-owner pushes (consistent with the digest's accepted fail-closed rule; log the error for operator visibility). Sophia's spec text says fail-open — implement fail-closed and note the override in your IMPL with this ruling cited."* Round 1 shipped the spec's own written fail-open text instead (`channels/line.py:_monthly_push_total_fail_open`, `logger.exception(... "fail-open, R-Q7")`) — I missed the ARCHI RULINGS section of my own dispatch brief when implementing R-Q7, and my round-1 IMPL.md did not flag the deviation. Vera independently caught the same conflict (`tests/test_line_v12_gaps.py`'s own escalation note, citing TEST-LINE-1.2.0.md's forensics section) and correctly pinned the code's actual (fail-open) behavior rather than inventing an untested fail-closed expectation — the right call given what shipped.
  **Fix (this round, per Archi's reaffirmed ruling 2026-08-31):** renamed `_monthly_push_total_fail_open` → `_monthly_push_total_fail_closed` (now returns `int | None`, `None` on any read exception, logged at `ERROR` — loud, not `exception`-level informational); `_quota_allows` and `_push` both now drop (return `False` / return early, no send, no ledger increment) on `None` rather than treating it as an all-clear `0`. Owner pushes and the reply path (`_flush_reply`, R-Q8) are structurally unaffected — the helper is only ever consulted for a non-owner `chat_id`. Updated `SPEC-LINE-1.2.md` §4 R-Q7, §9 OQ3, §3.4, and the §1 problem statement to record the ruling (marked `RESOLVED by Archi ruling 2026-08-31`, with the superseded fail-open rationale kept for context). Flipped the one test that pinned the old behavior, `tests/test_line_v12_gaps.py::test_quota_gate_fail_closed_on_monthly_push_total_read_error_drops_and_logs` (renamed from `..._fail_open_..._allows_and_logs`), citing the ruling in its own docstring — no other test in that file touched.

- **Fold-in (unrelated to Feature A/B, pre-existing bug):** `core/access.py` carried 17 mojibake occurrences (`Â§` for `§`, `Â·` for `·`, `âŠ‚` for `⊂` — a double-UTF-8-encoding artifact) across comments/docstrings plus the one live-deployed bug, `_render_users_list`'s `lang_suffix = f" Â· lang {row['language_pref']}"` (line 233, committed in `8e33073`, the v1.1.0 readable-approval release, confirmed via `git blame` — predates this release entirely). Fixed all 17 with the Edit tool (never Write/PowerShell, per the encoding-unsafe-write root cause) via three targeted replacements (`Â§`→`§`, `Â·`→`·`, and the one `âŠ‚`→`⊂` triplet); confirmed via a full non-ASCII character-frequency scan of the file that no corrupted bytes remain. `tests/test_access.py::test_execute_admin_users_lists_everyone` and `tests/test_v12_access_gaps.py::test_users_list_in_thai_has_no_keyerror_or_mojibake` now pass.
