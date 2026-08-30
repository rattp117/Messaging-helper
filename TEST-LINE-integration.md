# Test Report — LINE edition final release-gate verification (branch `line-version`)

## Summary (ROUND 2 — FINAL)

- Scope: full 30-AC sign-off against `SPEC-LINE.md`, plus targeted probing beyond `tests/test_line_integration.py`'s existing 16 end-to-end tests (per Archi's dispatch), across two rounds — round 1 found two release-blocking defects, round 2 re-verifies Luna's fixes plus fresh boundary/regression probes.
- Test file: `tests/test_line_release_gate.py` — **27 tests, all PASS** (grew from round 1's 17 as fixed-behavior re-verifications, boundary probes, and a two-user gate-journey test were added; see §Round 2 changes to the test file).
- LINE gate (`pytest -m "not telegram_only and not llm_only" -n auto`), **two consecutive runs**: identical both times — **5050 passed, 0 failed, 4 skipped, 1 xfailed**.
- Full unfiltered suite (`pytest -n auto`): **5203 passed, 0 failed, 4 skipped, 1 xfailed**.
- Baseline (pre-existing, Archi-supplied): LINE gate 5023/0/4sk/1xf; full suite 5176/0/4sk/1xf. Both fully reproduced (0 regressions in anything pre-existing) — the only deltas are my own 27 new tests, all passing.
- `python -c "import habit_assistant.main"` — clean.
- **Status: PASS.** All 30 literal SPEC-LINE.md acceptance criteria pass. Both round-1 release-blocking defects are fixed, independently re-verified end-to-end (including the push+ledger attribution Archi specifically asked to confirm), and every fresh round-2 probe (regex boundaries, mixed case, same-chat multi-send, two-user gate journey) holds. Two of Luna's own test-file edits (claimed mechanical fake-signature updates) were audited and confirmed genuinely mechanical, zero assertion impact.
- **Recommendation: Ready to ship.** Tag `line/v1.0.0`.

## Round 2 — what changed and how it was verified

### Production diffs read (all three)

1. **`src/habit_assistant/core/access.py`** — the ONLY change is `_CHAT_ID_RE`, from `^-?\d+$` (Telegram-numeric-only) to `^(?:-?\d+|U[0-9A-Za-z]{16,40})$` (a strict two-shape whitelist: the pre-existing Telegram numeric shape, OR `U` + 16-40 alphanumeric characters). No other line in this file changed — confirmed via full-file diff, not just the docstring's own claim.
   - **Shape assessment (Archi asked for this explicitly):** correctly scoped, not too loose. A real LINE userId is `U` + 32 lowercase-hex chars (33 total) — comfortably inside `[16,40]`. The regex deliberately does NOT restrict to hex: this app's own LINE test fixtures throughout the suite use human-readable non-hex placeholders (`"Uowner..."`, `"Umember..."`), so a hex-only whitelist would have broken dozens of existing tests while gaining no real security benefit — `_CHAT_ID_RE` is an input-shape sanity check (catches typos/garbage before a friendly usage message), not a security boundary; the actual authorization check is `classify(db, chat_id) == "owner"` on the ACTING user, unaffected by this regex either way. **Not too loose**: still rejects every entry in the pre-existing malformed-chat-id corpus (see below), still requires the literal `"U"` prefix (case-sensitive) plus a length inside a plausible band — an attacker gains nothing over the old regex's own openness (arbitrary Telegram-shaped digit strings were always accepted too).
   - **Malformed corpus held:** re-ran `tests/test_v12_access_gaps.py` + `tests/test_access.py` (the pre-existing malformed/valid-chat-id parametrized suites, `"abc"`, `"12ab"`, `""`, `"   "`, `"+123"`, `"12.5"`, `"--123"`, `"123-456"`, `None`, `"not-a-chat-id"` all still rejected; `"99999999999999999999999999"`, `"-987654321"`, `"0"`, `"007"` all still accepted) — **112 passed, 0 failed.**
2. **`src/habit_assistant/channels/line.py`** — `_reply_scope(self, reply_token, owner_chat_id=None)` now stores `ctx["ownerChatId"]`; `_emit` buffers only when `ctx["ownerChatId"] is None or ctx["ownerChatId"] == chat_id`, otherwise pushes immediately (spending quota, incrementing `push_ledger` for the ACTUAL recipient). `owner_chat_id=None` (every bare/test-level call site that predates this fix) preserves the exact pre-fix "buffer everything" behavior — additive, back-compatible, matches Archi's own description exactly.
3. **`src/habit_assistant/channels/line_webhook.py`** — `_dispatch(self, reply_token, owner_chat_id, call)` gained the `owner_chat_id` parameter; both call sites in `process_event` (message and postback) now pass the event's own `user_id` (guaranteed non-empty by the preceding guard). This is the ONE real production wiring change that makes the channel-level fix actually take effect — confirmed by reading `process_event`/`_dispatch` directly, not just the diff summary.

### Two "claimed-mechanical fake-signature" test-file edits — audited

Both files are untracked (no git history to diff against), so audited by direct content read + targeted grep for every touched line:

- **`tests/test_line_webhook.py`**: exactly one line touched — the test harness's own fake `_reply_scope(self, reply_token, owner_chat_id=None)` now accepts (and stores into `ctx["ownerChatId"]`, but never interprets) the new parameter, with a comment explicitly noting the comparison logic lives in `LineChannel._emit`, not this module. Grepped the whole file for `owner_chat_id`/`ownerChatId` — no other occurrence, confirming no assertion anywhere in this file reads or depends on the new field. **Genuinely mechanical.**
- **`tests/test_line_a_gaps.py`** (module A's own Vera-territory file, audited with extra scrutiny since it's this gate's peer, not this gate's own file): the SAME one-line harness-fake-signature update (identical pattern/comment to the file above). Separately, ~12 pre-existing call sites (`channel._reply_scope("rt-...")`) call the REAL `LineChannel._reply_scope` with a single bare argument — these were NOT edited (confirmed: no `owner_chat_id`/`ownerChatId` token appears near any of them), and don't need to be: `owner_chat_id` is keyword-defaulted, so every one of these bare calls still type-checks and still exercises the documented "buffer everything" fallback (`ownerChatId is None`) — none of these tests' own scenarios (5-object cap, network-failure drop, contextvar-reset-on-raise, concurrent-scope isolation) send to a different chat_id, so none of them are testing (or need to test) the new cross-chat routing decision at all; that's this gate's own job. **Genuinely mechanical — zero assertion changes in either file.** Re-ran both files plus `test_line_channel.py`/`test_access.py`/`test_v12_access_gaps.py`: **237 passed, 0 failed.**

### Both release-gate journeys re-run, PASS

- **`test_new_line_user_owner_notified_via_push_and_asker_reply_stays_clean`** (renamed from round 1's failing test): a brand-new LINE user's `/start` now produces a clean asker reply (no leaked owner-facing text) AND the owner receives a real push naming the requester and the `/approve` command — **and** `push_count(OWNER, this_month) == 1` is asserted directly, closing Archi's explicit "verify the ledger counted it" instruction.
- **`test_approve_command_accepts_real_line_userid_shape_end_to_end`** (renamed from round 1's failing test): `/approve <real-LINE-shaped-id>` now flips the target to `active`, the owner's own ack reply carries exactly one message (no leaked cross-user text), and the newly-approved user receives their own push (`access_granted`) with their own ledger incremented — proving the SAME channel-level fix also closes the mirror-image leak at `access.py:322` that round 1 flagged as a compounding risk but hadn't formally tested.

### Fresh round-2 probes (all new)

- **`test_chat_id_regex_boundary_and_case_probes`** (parametrized, 7 cases): `"U"+15 chars` → rejected (one below the 16-char floor); `"U"+16 chars` → accepted (exact floor); a 16-hex-char realistic id → accepted; `"U"+40 chars` → accepted (exact ceiling); `"U"+41 chars` → rejected (one above ceiling); mixed-case alphanumeric → accepted; lowercase `"u"` prefix → rejected (LINE ids are always upper-`"U"`). All 7 pass, exactly matching the regex's own stated bounds.
- **`test_line_channel_bare_reply_scope_call_preserves_documented_default`**: a bare `_reply_scope(token)` call (no `owner_chat_id`) still buffers a cross-chat send rather than pushing it — confirms the additive default is genuinely back-compatible, not silently changed.
- **`test_line_channel_reply_buffer_pushes_cross_chat_sends_when_owner_chat_id_given`**: the fix in isolation — a same-chat_id send stays buffered (lands in the one free reply), a different-chat_id send pushes immediately and increments `push_ledger` for the actual recipient, not the triggering user.
- **`test_line_channel_reply_buffer_still_batches_multiple_same_chat_sends`** (Archi's explicit "SAME user gets both a reply and a would-be second message" probe): two sends to the SAME chat_id as the context's own owner both land in the one batched reply — zero pushes. A stub `db.increment_push` that raises on any call proves this affirmatively (not just "assert push_calls==[]" but "a push would have crashed the test had one occurred").
- **`test_two_user_onboarding_and_approval_gate_journey_no_cross_contamination`** (Archi's explicit "two-user gate journey re-run"): two independent new users onboard via `/start`; each gets a clean reply with no owner-text leak and no mention of the other user; the owner receives exactly 2 independent pushes (ledger `== 2`) correctly naming each requester; the owner approves each independently (approving A does not touch B's own `pending` status); both newly-active users then log independently with correct per-user sums and zero cross-talk.

### Gate runs (post-fix)

```
Run 1: pytest -m "not telegram_only and not llm_only" -n auto  ->  5050 passed, 4 skipped, 1 xfailed, 0 failed
Run 2: pytest -m "not telegram_only and not llm_only" -n auto  ->  5050 passed, 4 skipped, 1 xfailed, 0 failed   (identical to run 1)
Full:  pytest -n auto                                          ->  5203 passed, 4 skipped, 1 xfailed, 0 failed
python -c "import habit_assistant.main"                        ->  clean
```

Module regression check (files touched by the fix + their peers): `tests/test_line_integration.py tests/test_line_a_gaps.py tests/test_line_webhook.py tests/test_line_channel.py tests/test_access.py tests/test_v12_access_gaps.py` → **237 passed, 0 failed.**

---

## Round 1 findings (historical — both now fixed, kept for the audit trail)

Round 1 found two independent, reproducible, release-blocking defects in the onboarding/admin surface, neither covered by any module's own AC list:

1. **`core/access.py`'s `_CHAT_ID_RE = re.compile(r"^-?\d+$")`** accepted only Telegram-shaped numeric chat ids, rejecting every real LINE userId — `/approve`/`/block`/`/invite` were completely non-functional on LINE, making onboarding structurally impossible.
2. **`LineChannel._emit`'s reply buffer was chat_id-blind** — a cross-user send made mid-event (the owner-pending-approval notification, the access-granted notification) was silently folded into the WRONG user's own reply instead of reaching its real target, so the owner was never notified and the asker's reply leaked owner-facing admin text.

Both are fixed as of round 2 (see above). Full original finding detail, suspected-cause file:line pointers, and root-cause isolation methodology are preserved below for reference.

<details>
<summary>Original round-1 finding detail (click to expand)</summary>

### Finding 1 — `/approve`/`/block`/`/invite` reject every real LINE userId (FIXED)

- **Suspected cause (confirmed correct):** `src/habit_assistant/core/access.py:50` — `_CHAT_ID_RE`.
- **Fix:** whitelist extended to `^(?:-?\d+|U[0-9A-Za-z]{16,40})$`. Verified round 2.

### Finding 2 — Cross-user reply-buffer leak (FIXED)

- **Suspected cause (confirmed correct):** `src/habit_assistant/channels/line.py`'s `_emit`/`_reply_scope` (no owning-chat_id awareness).
- **Fix:** `_reply_scope` gained `owner_chat_id`; `_emit` pushes on a chat_id mismatch; `channels/line_webhook.py:_dispatch` threads the event's own `user_id` through. Verified round 2, including the ledger-attribution check Archi specifically requested.

</details>

## 30-AC coverage map

Every AC below is proven **at the wired level** (a real `LineChannel` + real `LineWebhookServer` bound to a real localhost port, real signed HTTP, real SQLite, `httpx.MockTransport`-intercepted LINE API) by either Integration's own `tests/test_line_integration.py` or `tests/test_line_release_gate.py`, unless noted otherwise.

| AC | Owner | Result | Evidence |
|---|---|---|---|
| AC1 | Shared | **PASS** | `test_line_release_gate.py::test_ac1_load_secrets_line_missing_var_raises_configerror_naming_it`, `::test_ac1_load_secrets_line_success_with_all_three_vars`, `::test_ac1_load_secrets_telegram_default_unaffected_by_line_fields` (closes a previously ad-hoc-only verification gap). |
| AC2 | Shared | **PASS** | `tests/test_migrations.py` (26 literal schema-version-bump assertions, schema 013→014). |
| AC3 | Shared | **PASS** | `test_line_release_gate.py::test_ac3_push_ledger_and_opt_out_accessors_round_trip` (closes the same ad-hoc-only gap). |
| AC4 | Shared | **PASS** | Every gate run in this report — 0 failures, `telegram_only`/`llm_only`-marked tests deselected without failing the gate, deterministic across both runs. |
| AC5-AC14 | A | **PASS** | `TEST-LINE-A.md`'s own table + re-confirmed live via `test_line_integration.py`/`test_line_release_gate.py`'s signed-webhook round trips, undo quick-reply, rich menu at startup, media serve/traversal, and — new this round — the cross-chat push/ledger and same-chat batching probes above (extending AC7/AC8's own reply-vs-push distinction to the multi-recipient case). |
| AC15-AC19 | B | **PASS** | `TEST-LINE-B.md`'s own table; AC19's probe/`OllamaClient`-construction half closed by Integration's `test_line_integration.py::test_line_mode_never_constructs_ollama_or_health_and_registers_digest_job` + `core/app.py` code review. |
| AC20-AC25 | C | **PASS** | `TEST-LINE-C.md`'s own table + `test_line_integration.py`'s live digest/ledger/quota-warning/`/review` tests + `test_line_release_gate.py`'s digest boundary/opt-out/double-fire probes. |
| AC26-AC27 | D | **PASS** | `TEST-LINE-D.md`'s own table; the one prior non-blocking hygiene finding (`.env.example` CRLF-at-rest) confirmed fixed (LF-only on disk). |
| AC28 | Integration | **PASS** | `test_line_integration.py`'s wiring-level smoke tests + `test_line_release_gate.py::test_telegram_mode_real_message_round_trip_byte_identical_to_v1_10` (message-level, real inbound "500ml" through the real Telegram-mode wired app). |
| AC29 | Integration | **PASS** | Literal AC wording (log→undo→heatmap-equivalent-image→digest for one active user, cross-user isolation) — `test_line_integration.py` + `test_line_release_gate.py::test_full_journey_log_undo_and_tapfix_clarify_no_llm_end_to_end`/`::test_two_user_isolation_through_full_wired_pipeline`. **Additionally** (beyond the literal AC, per Archi's own broader dispatch): the onboarding/approval PREREQUISITE for reaching an active user in the first place is now also proven end-to-end and clean — `::test_new_line_user_owner_notified_via_push_and_asker_reply_stays_clean`, `::test_approve_command_accepts_real_line_userid_shape_end_to_end`, `::test_two_user_onboarding_and_approval_gate_journey_no_cross_contamination`. |
| AC30 | Integration | **PASS** | `test_line_integration.py::test_dashboard_on_never_persists_a_live_board_on_line`. |

**All 30 literal SPEC-LINE.md acceptance criteria: PASS. The two round-1 defects found beyond the AC list are now fixed and independently re-verified.**

## Self-fix audits (Integration's own two "self-found regression fixes," from round 1 — unaffected by round 2)

Both re-confirmed still faithful; round 2 touched neither `core/app.py`'s `_load_secrets_for_channel` nor `tests/test_refactor_s1_gaps.py`'s clock-seed update.

1. **`_load_secrets_for_channel`**: `load_secrets()` called completely bare for Telegram, `channel_type="line"` only for LINE — confirmed via `test_ac1_load_secrets_*` (3 tests) plus the full 5203-passed suite (no pre-existing zero-arg `load_secrets` fake broke).
2. **`reminders.py:429` clock-seed update**: confirmed equal-strength via diff review (round 1) and a from-scratch regression pin, `test_reminders_pause_suppression_honors_injected_clock_not_real_date` — still **PASS**.

## Deploy-consistency and version/tag readiness (from round 1 — unaffected by round 2, re-confirmed passing this run)

- `test_deploy_consistency_webhook_port_and_callback_path_one_value_everywhere` — **PASS**: port `8080`/`bind_host 127.0.0.1`/`/callback`/`/media/{token}.png` consistent across `Config` defaults, `config.toml.line`, `deploy/habit-assistant-line.service`, `docs/DEPLOY-LINE.md`, and the actual aiohttp route registrations.
- `test_version_consistent_across_files_and_release_note_posture` — **PASS**: `VERSION` == `__init__.py:__version__` == `pyproject.toml` == `"1.0.0-line"`; announce correctly folded into the digest on LINE (no separate push); no phantom `RELEASE_NOTES` entry for a version with nothing to announce an upgrade from (by design).

## Failures

None. All 27 tests in `tests/test_line_release_gate.py` pass; the full and LINE-scoped suites are green, deterministically, across two consecutive runs.

## Regressions detected

None. Every pre-existing test (baseline 5023/5176 passed) still passes. Both self-fixes remain faithful. Both round-1 defect fixes introduced no new failures anywhere in the 5203-test full suite.

## Recommendation

**Ready to ship.** All 30 SPEC-LINE.md acceptance criteria pass. Both release-blocking defects found in round 1 are fixed, independently re-verified end-to-end (including ledger attribution), and stress-tested with fresh boundary/multi-user probes in round 2. The two claimed-mechanical test-file edits are confirmed genuinely mechanical. Two consecutive LINE-gate runs and one full unfiltered run are all clean. **PASS — clear to tag `line/v1.0.0`.**
