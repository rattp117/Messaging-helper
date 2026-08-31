# Spec — LINE edition v1.2.0: dashboard-in-reply + real-time proactive mode

> Branch product (`line-version`), edition SemVer. **Target baseline:** `1.1.0+line` (the readable-approval feature — `LineChannel.get_profile`, name/prefix-based `/approve` — is landing in parallel and is treated as already present). **This release:** `1.2.0+line`, tag `line/v1.2.0`. Two user-approved features (2026-08-31), LLM stays permanently OFF (`config.ollama.enabled=false`, out of scope). Telegram edition on `main` unaffected. Baseline design: `SPEC-LINE.md` (reply-buffer, digest, quota ledger).

## 1. Problem statement

Restore two Telegram-edition behaviors on the LINE edition, each configurable and each defaulting to a safe, backward-compatible position. **Feature A — dashboard-in-reply:** after every successful log confirmation, append a compact "Today" board as an extra message object in the *same free reply*, so a LINE user gets the always-visible-progress benefit of the Telegram live pinned dashboard without any pinning (LINE cannot pin/edit) and without spending push quota (replies are free). **Feature B — real-time proactive mode:** an opt-in `[digest] mode = "realtime"` that re-enables the per-time proactive sends the LINE branch currently collapses into one daily digest (reminders, hourly check-ins, almost-there nudge, daily summary, weekly review, release announcement), sending each as a LINE **Push** with authoritative `push_ledger` accounting — guarded by a configurable hard monthly cap (`push_cap`) with an 80% owner warning and a 100% hard stop that never touches replies. The user has accepted the paid-LINE-plan implication of realtime. Success = both features work bilingually, per-user-isolated, fail-open at every send site; the release is **byte-identical to `1.1.0+line`** when `[line] dashboard_in_reply=false` **and** `[digest] mode="digest"`; and the Telegram edition is byte-unchanged. No schema migration (verified: `push_ledger` + `users.digest_opt_out` already exist from migration 014).

## 2. Inputs

Inbound LINE events are unchanged (`SPEC-LINE.md §2.1`). The only new inputs are three config knobs and their runtime effects.

### 2.1 Config additions (`config.toml`)
```toml
[line]
# ... existing keys unchanged ...
dashboard_in_reply = true        # NEW — Feature A. Default true. Append the compact
                                 #   "Today" board to every successful log confirmation
                                 #   reply. false = byte-identical to 1.1.0 replies.

[digest]
# ... existing keys unchanged (enabled, time, warn_cap=280, include_weekly_review_day) ...
mode = "digest"                  # NEW — Feature B. "digest" (default, current behavior)
                                 #   | "realtime". realtime re-enables per-time pushes.
push_cap = 15000                 # NEW — Feature B. Hard monthly cap on TOTAL proactive
                                 #   pushes across all users (realtime only). Owner warned
                                 #   at 80%, non-owner pushes hard-stopped at 100%.
```

- `dashboard_in_reply` mounts on the existing `LineConfig`. `mode` and `push_cap` mount on the existing `DigestConfig`. All defaulted; an absent key uses the class default (same convention as every prior config addition — `config.py`).
- `warn_cap` (existing, default 280) keeps its **digest-mode-only** meaning (the in-digest owner warning line, `R-C7`). It is *not* reused by realtime — realtime uses `push_cap` (see §4 R-Q\*). The two knobs match two different economics (digest ≈ 1 push/user/day → free tier ~300/mo; realtime = many pushes → paid plan).

### 2.2 Runtime inputs the new behavior reads
- **Feature A** reads the active per-event reply context (`channels/line.py:_REPLY_CONTEXT`) and `db.get_dashboard_msg_id` (still `NULL` on LINE — the board rides the reply, never a pin).
- **Feature B** reads `db.monthly_push_total(yyyymm)` on the push path, `config.digest.mode`/`push_cap`, and each sender's existing per-user DND state (`reminders.in_dnd_now` / `effective_quiet_windows`). `self.owner_user_id` (already on `LineChannel`) distinguishes owner vs non-owner pushes.

## 3. Outputs

### 3.1 Feature A — reactive reply with the board appended (free)
One reply call (`POST /message/reply`), the confirmation object **plus** a trailing board object; the `undo` quick reply is consolidated onto the last object so it stays visible (§4 R-A4/R-A5):
```jsonc
{ "replyToken": "0f37...", "messages": [
  { "type": "text", "text": "💧 water +500 ml — 1,500 / 2,500 ml today (60%)" },
  { "type": "text",
    "text": "📊 Today · Sun 31 Aug\n💧 water  ▓▓▓▓▓▓░░░░ 60%  · streak 4d\n🧘 stretch  1  · streak 2d\n📔 diary  0",
    "quickReply": { "items": [
      { "type": "action", "action": { "type": "postback", "label": "↩︎ Undo", "data": "undo:98765" } }
    ] } }
]}
```
No push, no `push_ledger` increment. With `dashboard_in_reply=false` the second object is absent and the reply is byte-identical to 1.1.0 (undo rides the confirmation object, as today).

### 3.2 Feature B — realtime proactive push (costs quota)
Each re-enabled per-time send is a Push (`POST /message/push`), one per send, each incrementing `push_ledger` (§4 R-Q1). Example reminder:
```jsonc
{ "to": "U4af...", "messages": [ { "type": "text", "text": "💧 Time for some water!" } ] }
```

### 3.3 Feature B — owner quota notifications (realtime only)
- **80% warn** (once/month, owner push): `i18n.t("push_quota_warn", …, total, cap, pct)`.
- **100% stop alert** (once/month, owner push): `i18n.t("push_quota_stop", …, cap)`. After this, every **non-owner** proactive push is dropped (no send, no increment) until the month rolls over; **replies are unaffected — the bot keeps working reactively**, and the owner keeps receiving their own proactive pushes.

### 3.4 Error / degradation
- Board render failure (Feature A): logged and swallowed inside `dashboard.refresh`'s existing single-`try` fail-open body — never blocks the confirmation that already went out.
- `monthly_push_total` read failure at the quota gate: **fail-open** (allow the push), logged — a transient DB hiccup must not silence all proactive traffic; the authoritative ledger increment self-corrects the count on the next good read (bounded overspend — see §9 OQ3).
- Every re-enabled sender keeps its own per-user `try/except` fail-open posture (unchanged).

## 4. Behavior rules

Rule ids: `R-A*` Feature A (dashboard-in-reply), `R-Q*` Feature B quota, `R-R*` Feature B realtime jobs, `R-S*` shared surface, `R-I*` integration.

### Shared surface (built first)
- **R-S1** Add `LineConfig.dashboard_in_reply: bool = True`.
- **R-S2** Add `DigestConfig.mode: Literal["digest","realtime"] = "digest"` (field validator rejects any other string) and `DigestConfig.push_cap: int = 15000` (field validator: positive int). `warn_cap` unchanged.
- **R-S3** Add `Channel.append_board(self, chat_id: str, text: str) -> None` to the ABC as a **concrete no-op default** (a channel with a live pinned dashboard — Telegram — shows the board that way and needs nothing here). No abstract-method change; every existing channel keeps conforming.
- **R-S4** Refactor `LineChannel`'s push path into two methods: `_send_push(chat_id, messages)` (the raw `POST /message/push` + `db.increment_push` on success — today's `_push` body, ungated) and `_push(chat_id, messages)` (the gated proactive entry `_emit` calls, applying the realtime quota gate R-Q\* then delegating to `_send_push`). In digest mode the gate is a pass-through, so `_push` is byte-identical to today (`R-I5`).
- **R-S5** i18n: add bilingual keys `dashboard_line_auto`, `push_quota_warn`, `push_quota_stop` (every key has both `en` and `th`). Existing `digest_quota_warning` unchanged.

> The R-S3/R-S4 channel edits (`channels/base.py`, `channels/line.py`) are the *only* files Feature A and Feature B both touch. Building them in the shared surface first keeps the two feature modules on disjoint files (§11).

### Feature A — dashboard-in-reply (`R-A*`)
- **R-A1 (append site = every `dashboard.refresh`)** In `core/dashboard.py:refresh`, before the existing pin/edit machinery (which is inert on LINE — `dashboard_msg_id` is always `NULL`), add: when `config.channel.type == "line"` **and** `config.line.dashboard_in_reply`, render the compact board (`dashboard.render`, resolving `_board_language`) and call `await channel.append_board(user_id, board_text)`. This one hook reaches **every** state-changing reactive site because they all already call `dashboard.refresh` after confirming: the four enumerated log confirmations — (a) typed log (`routing.handle_inbound_message` final block), (b) quick-log tap (`quicklog.handle_log_callback`), (c) clarify tap (`clarify.handle_clarify_callback`), (d) routine run (`routines.execute_routine`) — **plus** undo, edit, `/target set|clear`, `/cadence`, `/pause`/`/resume` (each of which refreshes the board too; appending the current board after any change the user just made is correct and consistent). The whole append sits inside `refresh`'s existing single-`try` fail-open body.
- **R-A2 (backfill excluded)** A backfilled log (`backfill_date is not None`) does **not** append the board — the typed-log site already skips `dashboard.refresh` for backfills (a backdated row never changes today's board). No special-casing needed; it falls out of R-A1.
- **R-A3 (`append_board` never spends quota)** `LineChannel.append_board(chat_id, text)`: append a `{"type":"text","text":text}` object to the **active** reply buffer only — read `_REPLY_CONTEXT`; if there is **no** active context (a scheduled call, e.g. `dashboard_day_rollover_job`) return immediately and send **nothing** (never a push). Respect the same owner-match as `_emit` (`ctx["ownerChatId"] is None or == chat_id`). Append only when the buffer already holds ≥1 object (never emit a board-only reply). The base `Channel.append_board` is a no-op (Telegram).
- **R-A4 (board is the last object; drop-first precedence)** The board is appended **after** the confirmation, so it is the last buffer object. `_flush_reply` truncation is unchanged (keep the first `_MAX_REPLY_MESSAGES`=5, drop the tail) — so when a reply would exceed 5 objects the **board is dropped first and the confirmation is never dropped** (R-A4 precedence falls straight out of the existing tail-truncation because the board is last).
- **R-A5 (quickReply consolidation — undo stays visible)** LINE displays only the **last** message object's `quickReply`, and renders quick replies as a bottom-of-screen row (not bound to a bubble). So in `_flush_reply`, **after** the ≤5 truncation, consolidate: if the final message object has no `quickReply`, copy onto it the `quickReply` of the latest earlier object that has one (if any). Result: the confirmation's `undo` button relocates onto the trailing board object and stays visible; if the board was dropped by R-A4, the confirmation is again last and keeps its own `quickReply` with no move. Consolidation only *acts* when the last object lacks a `quickReply` and an earlier one has it — a condition that never arises in current (board-off) LINE flows, so `dashboard_in_reply=false` stays byte-identical (R-A7).
- **R-A6 (one board per reply)** `append_board` appends at most one board object per reply context: it holds a reference to the board object it created in the context and, on a second call within the same event, updates that object's `text` in place rather than appending a duplicate.
- **R-A7 (byte-identical when off)** With `dashboard_in_reply=false`, R-A1 skips the append entirely, R-A5 consolidation is a no-op, and every LINE reply is byte-for-byte what 1.1.0 sends.
- **R-A8 (compact render reuse)** Reuse `core/dashboard.py:render` verbatim — it is already budget-disciplined (`fit_within_budget` at `TELEGRAM_MESSAGE_BUDGET`=4096, safely under LINE's 5,000-char text-object limit) and registry-generic. "Compact" = the existing one-line-per-habit Today board (no heatmap/records/trends). No new renderer.
- **R-A9 (`/dashboard` honesty on LINE)** In `execute_dashboard`, when `config.channel.type == "line"`, short-circuit any `on|off|<bare>` to return the bilingual `dashboard_line_auto` note ("your Today board rides along automatically with each log — no pinning on LINE"), replacing the misleading `dashboard_unsupported` reply. No per-user state, no write. (Per-user opt-out is out of scope — see §9 OQ1 and §10.)
- **R-A10 (Telegram unaffected)** On Telegram (`channel.type != "line"`) R-A1 skips the append (base `append_board` no-op is never even reached), and the live pinned dashboard behaves byte-identically.

### Feature B — realtime jobs (`R-R*`)
Master switch: `config.digest.mode`. The suppression gates the LINE branch installed in `core/jobs.py` (each currently `if config.channel.type == "line": return/continue`) change to **suppress only in digest mode**: `if config.channel.type == "line" and config.digest.mode != "realtime": …`. Telegram (`type != "line"`) is untouched by every gate. Enumerated disposition of each suppressed proactive surface:

| # | Surface (site) | Digest mode (unchanged) | **Realtime disposition** |
|---|---|---|---|
| **R-R1** | Per-time reminders (`jobs.minutely_tick`→`reminders.run_due_reminders`) | suppressed; folded into digest due-reminders line | **Push at each configured time.** Existing `send_reminder` quiet-hours + goal-met-skip + pause suppression all re-apply (DND-gated, R-R7). Each sent reminder → 1 push, ledger +1. |
| **R-R2** | Hourly check-ins (`minutely_tick`→`checkins.run_due_checkins`) | suppressed (unavailable, R-C3) | **Push hourly** within the user's window (opt-in via `/checkin on`, default off). DND-gated. Up to 13 pushes/day/user — paid-plan cost, noted (§9). |
| **R-R3** | Almost-there nudge (`minutely_tick`→`nudge.run_due_nudges`) | suppressed; folded | **Push at `[nudge].time`.** Rides `/checkin` enablement, DND-gated. |
| **R-R4** | Daily summary (`jobs.daily_summary_job`) | suppressed; folded | **Push at `[gamification].daily_summary_time`.** DND-gated. |
| **R-R5** | Weekly review (`jobs.weekly_review_job`) | suppressed; on-demand `/review` | **Push on review day** — text push + one push per chart image (media URL). DND-gated. `/review` on-demand still available. |
| **R-R6** | Release announcement (`app.py:announce.announce_release`) | skipped; folded into digest item (e) | **Re-enabled** (push fan-out). Inert-by-version in practice until a `RELEASE_NOTES` key matches the branch version — the same property the digest fold already has (§9 OQ2); the gate flips, no push is *asserted*. |
| **R-R7 (DND)** | all of R-R1..R-R5 | — | **Per-user DND/quiet-hours gates every solicited realtime push** — satisfied by each sender's existing `in_dnd_now` / `effective_quiet_windows` check (Telegram semantics port straight over; no new DND code). A user inside their window gets no push. |
| **R-R8 (grace)** | Grace notification (`jobs.grace_tick` send) | send suppressed, `evaluate_grace` write kept | **Send stays suppressed in realtime too** (grace_tick's send gate remains `type=="line"`, mode-independent). Rationale (gentleness + quota): grace is "quiet forgiveness," Telegram sends it silent + DND-bypass, but LINE has **no** silent-send (a push notifies), so a 00:05 grace push would buzz the user at midnight and cost quota for the lowest-value moment. The protected streak instead surfaces for free via the dashboard-in-reply (Feature A) and the next reminder/summary. The `evaluate_grace` **write still runs unconditionally** (unchanged). |
| **R-R9 (wrapped)** | Month-end wrapped auto-send (`jobs.wrapped_auto_job`) | suppressed; on-demand `/wrapped` | Gated by `config.wrapped.auto_send` (default false). In realtime, if the operator sets `auto_send=true`, the month-end card pushes (DND-gated); default false → no push. |
| **R-R10 (digest inert)** | Daily digest (`digest.run_daily_digest`) | THE one push | **Inert in realtime** (mode-exclusive, to avoid double-report + double-spend): `run_daily_digest` early-returns when `config.digest.mode == "realtime"`, before any read/send. The job stays registered (no `app.py` registration change); the guard makes it a no-op. |

### Feature B — push quota cap (`R-Q*`), realtime only
- **R-Q1 (authoritative accounting unchanged)** Every proactive push still increments `push_ledger` exactly once on success, in `LineChannel._send_push` (R-C6 unchanged) — realtime adds no per-sender bookkeeping.
- **R-Q2 (gate placement)** The quota gate lives in `LineChannel._push` (the single proactive-push chokepoint `_emit` funnels every no-reply-context send through), so it covers **every** realtime surface uniformly regardless of caller. In digest mode the gate is a pure pass-through (byte-identical, R-I5).
- **R-Q3 (hard stop for non-owner at 100%)** In realtime, before sending: let `total = db.monthly_push_total(yyyymm)`, `cap = config.digest.push_cap`. If `chat_id == self.owner_user_id` → always allow (the owner keeps receiving; the owner is the operator). Else if `total >= cap` → **drop this push** (no `_send_push`, no increment, no raise; log at INFO), and fire the once-per-month owner stop alert (R-Q5).
- **R-Q4 (owner 80% warn, once/month)** In realtime, on an *allowed* non-owner push, if `total >= int(cap * 0.8)` and `total < cap` and the owner has not yet been warned this `yyyymm`, send the owner one `push_quota_warn` push (via `_send_push`, ledger +1, not re-entering the gate) and mark the month warned. The ratio 0.8 is fixed and documented (may become a config knob later).
- **R-Q5 (owner 100% alert, once/month)** The first time R-Q3 drops a non-owner push in a `yyyymm`, send the owner one `push_quota_stop` alert (via `_send_push`) and mark the month stopped, so the owner is told exactly once that the cap is reached and replies still work.
- **R-Q6 (once-per-month guards are in-memory)** The warned/stopped month sets are process-lifetime `LineChannel` instance state (same posture as `digest.py:_DIGEST_DEFERRED_DATES` and `routing.py:_sweep_in_progress`; safe under the single-instance deployment assumption). A mid-month restart may re-warn/re-alert once — acceptable (informational, rare). No migration, no persisted flag.
- **R-Q7 (gate fail-open)** A `monthly_push_total` read exception inside the gate is logged and treated as "allow" — availability over a rare bounded overspend (§9 OQ3). The subsequent successful increment/read re-engages the cap.
- **R-Q8 (replies never gated)** The gate is only on the push path; the reply path (`_flush_reply`) is never touched, so reactive replies (logs, undo, `/heatmap`, dashboard-in-reply) keep working at and past the cap.

### Integration (`R-I*`)
- **R-I1** `core/jobs.py`: change the five suppression gates from `type=="line"` to `type=="line" and mode!="realtime"` for `minutely_tick`, `weekly_review_job`, `daily_summary_job`, `wrapped_auto_job`; **leave `grace_tick`'s send gate as `type=="line"`** (R-R8). No other jobs.py logic changes.
- **R-I2** `core/digest.py`: add the `mode=="realtime"` early-return to `run_daily_digest` (R-R10).
- **R-I3** `core/app.py` (the reserved integration file): change the startup announcement gate from `if config.channel.type != "line":` to `if config.channel.type != "line" or config.digest.mode == "realtime":` so realtime re-enables the fan-out (R-R6). No other app.py change (digest job stays registered unconditionally on LINE; the R-I2 guard makes it inert in realtime).
- **R-I4 (no migration)** Verified: `push_ledger`, `users.digest_opt_out`, and every accessor (`increment_push`/`push_count`/`monthly_push_total`/`set_digest_opt_out`/`digest_opt_out`) already exist (migration 014). v1.2.0 adds **no** migration; opening a 1.1.0 DB is a no-op.
- **R-I5 (combined byte-identical gate)** With `dashboard_in_reply=false` **and** `mode="digest"`: replies are byte-identical (R-A7), all jobs suppress exactly as today (R-I1 gate resolves to the current `type=="line"` suppression), the digest job runs unchanged (R-I2 guard not taken), and the push path has no active quota gate (R-Q2 pass-through). Telegram (`type!="line"`) is byte-unchanged regardless of `mode`/`dashboard_in_reply`.

## 5. Interfaces (signatures)

```python
# src/habit_assistant/config.py
class LineConfig(BaseModel):
    # ... existing ...
    dashboard_in_reply: bool = True                    # R-S1

class DigestConfig(BaseModel):
    # ... existing (enabled, time, warn_cap, include_weekly_review_day) ...
    mode: Literal["digest", "realtime"] = "digest"     # R-S2 (+ field_validator)
    push_cap: int = 15000                              # R-S2 (+ positive field_validator)

# src/habit_assistant/channels/base.py
class Channel(ABC):
    async def append_board(self, chat_id: str, text: str) -> None:   # R-S3 — base no-op default
        return None

# src/habit_assistant/channels/line.py  (LineChannel)
async def append_board(self, chat_id: str, text: str) -> None: ...           # R-A3/R-A6
async def _send_push(self, chat_id: str, messages: list[dict]) -> None: ...  # R-S4 — raw push + ledger++
async def _push(self, chat_id: str, messages: list[dict]) -> None: ...       # R-S4/R-Q2 — quota gate → _send_push
def _quota_allows(self, chat_id: str, yyyymm: str) -> bool: ...              # R-Q3..R-Q7 helper
# _flush_reply gains the R-A5 quickReply consolidation (signature unchanged)

# src/habit_assistant/core/dashboard.py
async def refresh(db, channel, config, registry, user_id, clock=datetime.now) -> None: ...  # R-A1 hook added
async def execute_dashboard(command, *, db, channel, config, registry, lang, user_id, clock=datetime.now) -> str: ...  # R-A9 LINE branch

# src/habit_assistant/core/digest.py
async def run_daily_digest(db, channel, config, provider, *, clock=datetime.now, scheduler=None) -> None: ...  # R-R10 realtime early-return
```

## 6. Files to touch

**Shared surface (sequential, first)**
- `src/habit_assistant/config.py` — `LineConfig.dashboard_in_reply`; `DigestConfig.mode` + `push_cap` (+ validators).
- `src/habit_assistant/channels/base.py` — `append_board` no-op default (R-S3).
- `src/habit_assistant/channels/line.py` — `_send_push`/`_push` split + quota gate (R-S4/R-Q\*); `append_board` (R-A3/R-A6); `_flush_reply` quickReply consolidation (R-A5); in-memory month guards (R-Q6).
- `src/habit_assistant/core/i18n.py` — `dashboard_line_auto`, `push_quota_warn`, `push_quota_stop` (en+th).

**Feature A**
- `src/habit_assistant/core/dashboard.py` — `refresh` append hook (R-A1/R-A2); `execute_dashboard` LINE copy (R-A9).

**Feature B**
- `src/habit_assistant/core/jobs.py` — mode-gated suppression on the five jobs; grace_tick send gate unchanged (R-I1/R-R\*).
- `src/habit_assistant/core/digest.py` — realtime-inert guard in `run_daily_digest` (R-I2/R-R10).

**Integration**
- `src/habit_assistant/core/app.py` — announcement gate for realtime (R-I3).
- `VERSION` + `src/habit_assistant/__init__.py` — `1.2.0+line`.
- `config.toml.line` (deploy template) + `config.toml` doc comments — document the three new knobs.
- Tests: `tests/test_line_channel.py` (append_board, consolidation, quota gate), `tests/test_dashboard*.py` (append hook), `tests/test_digest.py` / `tests/test_jobs*` (realtime dispositions), a new `tests/test_line_v12_integration.py` (R-I5 + e2e).

**Deliberately NOT touched** (reached unchanged through the seam/gates): `reminders.py`, `checkins.py`, `nudge.py`, `grace.py`, `review.py`, `wrapped.py`, `announce.py`, `storage/*` (no migration), `routing.py`, `quicklog.py`, `clarify.py`, `routines.py`.

## 7. External dependencies

None new. Runtime deps unchanged (`aiohttp>=3.9`, `httpx`, `apscheduler`, `pydantic(-settings)`, optional `matplotlib`). **LINE Messaging API facts this design relies on:** Reply API is free/uncounted and Push counts (unchanged); **only the last message object's `quickReply` is displayed**, and quick replies render as a single bottom-of-screen row not bound to a bubble (this is what makes R-A5's relocation visually identical and correct); ≤5 message objects per reply/push; text ≤5,000 chars (the 4,096-budget board is safe). `push_cap` default 15000 is operator-tunable to match the LINE plan the user purchases.

## 8. Acceptance criteria

> Given / When / Then. Module ownership in brackets; every §4 rule is covered.

**Feature A**
- **AC1** *[Shared]* Given `config.toml` setting `[line] dashboard_in_reply` and `[digest] mode`/`push_cap`, When config loads, Then the values bind with defaults `true`/`"digest"`/`15000`; an unknown `mode` string or a non-positive `push_cap` raises `ConfigError`. (R-S1/R-S2)
- **AC2** *[A]* Given LINE + `dashboard_in_reply=true`, When a user types a log that confirms, Then exactly **one reply** call is made carrying the confirmation object **and** a trailing board object, with **no push** and **no `push_ledger`** increment. (R-A1/R-A3/R-A8)
- **AC3** *[A]* Given AC2's two-object reply, When it is flushed, Then the `undo` `quickReply` is present on the **last** (board) object and absent from the confirmation object. (R-A5)
- **AC4** *[A]* Given LINE + on, When a quick-log tap, a clarify tap, and a routine run each confirm, Then each appends the board to its own reply (all four enumerated sites). (R-A1)
- **AC5** *[A]* Given a reply whose buffer would exceed 5 objects with the board appended, When flushed, Then the **board** is dropped (never a confirmation object) and the `undo` `quickReply` lands on the last surviving object. (R-A4/R-A5)
- **AC6** *[A]* Given `dashboard_in_reply=false`, When any log confirms, Then no board object is appended and the reply is byte-identical to 1.1.0 (undo on the confirmation object). (R-A7)
- **AC7** *[A]* Given the scheduled `dashboard_day_rollover_job` (no reply context), When `refresh`→`append_board` runs for each active user, Then **nothing** is sent and `push_ledger` is unchanged. (R-A3)
- **AC8** *[A]* Given a backfilled typed log ("500ml yesterday"), When it confirms, Then no board is appended. (R-A2)
- **AC9** *[A]* Given two `dashboard.refresh` calls within one event, When flushed, Then the reply contains **at most one** board object. (R-A6)
- **AC10** *[A]* Given LINE, When `/dashboard on|off|<bare>` is sent, Then the reply is the bilingual `dashboard_line_auto` note (not `dashboard_unsupported`), with no write. (R-A9)
- **AC11** *[A]* Given Telegram, When any log confirms, Then no `append_board` effect occurs and the live pinned dashboard is byte-identical to today. (R-A10/R-S3)

**Feature B**
- **AC12** *[B]* Given `mode="realtime"`, When a user's configured reminder time arrives (user not in DND, goal unmet), Then a reminder **push** is sent and `push_ledger` for their month increments by one. (R-R1/R-Q1)
- **AC13** *[B]* Given `mode="realtime"` and a user inside their effective quiet-hours window, When a reminder/check-in/nudge/summary/review would fire, Then **no push** is sent to that user (DND gates every solicited realtime push). (R-R7)
- **AC14** *[B]* Given `mode="realtime"`, When the hourly check-in / nudge time / daily-summary time / weekly-review day arrive for an eligible, non-DND user, Then each fires as a push with a ledger increment; check-ins require `/checkin on`. (R-R2/R-R3/R-R4/R-R5)
- **AC15** *[B]* Given `mode="realtime"`, When `grace_tick` runs and bridges a habit, Then the `evaluate_grace` write happens but **no grace push** is sent (send stays suppressed). (R-R8)
- **AC16** *[B]* Given `mode="realtime"`, When the digest job fires, Then it returns immediately and sends **no** digest push. (R-R10)
- **AC17** *[B]* Given `mode="realtime"` and `monthly_push_total >= push_cap`, When a proactive push is attempted for a **non-owner**, Then it is dropped (no send, no increment) while an **owner** proactive push in the same state still sends; a concurrent reply for either user still succeeds. (R-Q3/R-Q8)
- **AC18** *[B]* Given `mode="realtime"` and `monthly_push_total` crossing `int(push_cap*0.8)` (but `< push_cap`), When the next allowed non-owner push occurs, Then the owner receives **exactly one** `push_quota_warn` push that month. (R-Q4)
- **AC19** *[B]* Given `mode="realtime"` and the cap reached, When the first non-owner push is dropped that month, Then the owner receives **exactly one** `push_quota_stop` alert that month. (R-Q5)
- **AC20** *[B]* Given `mode="digest"`, When any proactive push flows through `_push`, Then **no** quota gate runs, the `warn_cap` in-digest owner warning is unchanged, and the push path is byte-identical to 1.1.0. (R-Q2/R-I5)
- **AC21** *[B]* Given `mode="realtime"`, When a user replies to a reminder push with a bare number, Then it is **not** attributed to that reminder (reply-to-reminder stays inert — LINE carries no reply-ref) and falls through to normal parsing. (R-R7 note / §10)
- **AC22** *[Integration]* Given Telegram (`type!="line"`) with `mode` set to either value, When any job runs, Then behavior is byte-unchanged (mode is a LINE-only knob). (R-I1/R-I5)

**Integration**
- **AC23** *[Integration]* Given `dashboard_in_reply=false` **and** `mode="digest"`, When the full LINE flow runs (log→reply, all scheduled jobs, digest push), Then every observable output is byte-identical to 1.1.0. (R-I5)
- **AC24** *[Integration]* Given a 1.1.0 DB, When v1.2.0 opens it, Then the schema version is unchanged and no migration runs. (R-I4)
- **AC25** *[Integration]* Given `mode="realtime"`, end-to-end for one user: a reminder time fires (push, ledger+1), then the user logs (free reply with board appended, undo on last object), then near cap the owner is warned, then at cap a **second** user's proactive push is blocked while the owner is still served and both users' replies work — and no user's data appears in another's output. (R-Q\*/R-R\*, U-ISO)

## 9. Risks & open questions

- **OQ1 — Feature A per-user opt-out.** LINE has no working per-user dashboard opt-in (the Telegram `dashboard_msg_id` mechanism is inert — pins fail), so v1.2.0 ships dashboard-in-reply as a **global** `[line] dashboard_in_reply` default (ON), no per-user toggle. **Argument for the LINE-specific default flipping Telegram's opt-in:** the Telegram opt-in existed to avoid an intrusive *persistent pinned message*; a board appended to a reply the user's own action already triggered is ephemeral, free, and notifies no one, so opt-in friction isn't warranted. **Default if unanswered:** ship global-ON; a per-user opt-out would need a new `users` column (migration 015) and is deferred (§10). **Who answers:** user (only if they want per-user control now). Non-blocking.
- **OQ2 — Realtime announcement is inert by version.** `announce_release` (and the digest fold) skip any version not in `RELEASE_NOTES`; the branch version is `1.x.y+line` (no matching key), so R-R6 flips the gate but pushes nothing until a `RELEASE_NOTES` entry is keyed to the LINE version. **Default:** flip the gate (parity), assert no push. **Who answers:** Archi at release, if LINE-version release notes are wanted (out of scope here).
- **OQ3 — Quota-gate failure disposition.** R-Q7 fails **open** (allow) on a `monthly_push_total` read error, trading a rare bounded overspend for availability; the alternative (fail-closed, block all non-owner pushes on a DB hiccup) protects money but can silence realtime traffic on a transient error. **Default:** fail-open (bounded — the authoritative ledger self-corrects on the next read). **Who answers:** user, since realtime is a paid plan and this is a money-vs-availability call. If they prefer fail-closed, it is a one-line flip.
- **OQ4 — Check-in push volume in realtime.** Hourly check-ins can be up to 13 pushes/day/user (opt-in). At scale this is the dominant quota consumer; `push_cap` + the owner warn/stop are the guardrails, but the operator should size `push_cap` to their LINE plan and consider a narrower default check-in window. **Non-blocking**, documented.
- **Risk — realtime reminder timeliness returns, midnight grace does not.** Realtime restores "ping me at 08:00" (the digest's biggest UX loss), but grace stays silent (R-R8) — a deliberate gentleness+quota trade. Documented so it isn't read as a bug.

## 10. Out of scope

- Any LLM behavior (LLM stays OFF — `SPEC-LINE.md §10`).
- **Not portable to LINE, documented:** reply-to-reminder attribution (LINE webhooks carry no reply-to metadata — stays inert even when reminders push in realtime); message reactions; silent sends (`disable_notification` has no LINE equivalent — every realtime push notifies per the user's LINE settings, which is *why* grace isn't pushed at midnight).
- Per-user opt-out of dashboard-in-reply (OQ1 — would need migration 015; deferred).
- A realtime-mode digest, or folding grace/announcements into a realtime push (realtime is send-in-real-time; digest is mode-exclusive).
- LINE Flex Messages / carousels for the board (plain text object, as the digest already is).
- A configurable warn ratio (fixed 0.8) or a persisted (cross-restart) once-per-month owner-alert guard.
- Changing digest-mode behavior in any way (it must stay byte-identical).

## 11. Module split & parallel development

**Total functionals:** 2 — (A) dashboard-in-reply, (B) real-time proactive mode (bundling job re-enablement + push-quota cap + digest-inert). Sub-capabilities: config surface, channel reply-side (append/consolidate), channel push-side (quota gate), jobs re-enablement, digest-inert — 5 pieces across 2 features.

**Recommendation:** **SEQUENTIAL.** Under the 5-functional threshold, and the two features share one file (`channels/line.py` — A's `append_board`/`_flush_reply`, B's `_push` gate), so the bulk of the work is an inherently sequential shared surface; the remaining fan-out is two thin modules whose parallel coordination overhead isn't worth it. Build order: **shared surface (config + i18n + `channels/base.py` + `channels/line.py`) → Feature A (`core/dashboard.py`) → Feature B (`core/jobs.py` + `core/digest.py`) → integration (`core/app.py`, byte-identical + e2e verification)**. A and B are independent given the shared channel layer, so A and B may be tested as soon as each is built.

**Optional PARALLEL (if Archi wants speed):** the split *is* clean once the shared surface absorbs all channel edits, because it leaves the two feature modules on **disjoint files**:

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| **Shared surface** (sequential, first) | AC1 | `config.py`, `channels/base.py`, `channels/line.py` (append_board, `_send_push`/`_push` split + quota gate, `_flush_reply` consolidation, month guards), `core/i18n.py` | — |
| **A · dashboard-in-reply** | AC2–AC11 | `core/dashboard.py` | shared `append_board`, `dashboard_in_reply`, `_flush_reply` consolidation |
| **B · realtime** | AC12–AC21 | `core/jobs.py`, `core/digest.py` | shared `_push` quota gate, `mode`/`push_cap` |
| **Integration** (sequential, last) | AC22–AC25 | `core/app.py`, `VERSION`, `__init__.py`, integration tests | A + B complete |

Every AC belongs to exactly one module. Because both features' channel changes are hoisted into the shared surface, A (`dashboard.py`) and B (`jobs.py`+`digest.py`) never touch the same file — so if parallelized, the two Luna+Vera pairs run with disjoint ownership after the shared surface lands.

**Integration order (after A + B):** (1) flip the `app.py` announcement gate (R-I3); (2) verify R-I5 byte-identical gate (`dashboard_in_reply=false` + `mode="digest"` → 1.1.0 outputs, Telegram unchanged); (3) run the realtime e2e (AC25) against a fake webhook/queue: reminder→push(+ledger), log→free reply with board, owner warn near cap, non-owner hard-stop at cap, two-user isolation; (4) confirm the LINE gate `pytest -m "not telegram_only and not llm_only"` is green with no new migration.
