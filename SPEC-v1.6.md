# Spec — v1.6.0: Live dashboard · Heatmap · Records · Trends · End-of-day nudge

## 1. Problem statement
Five "wow" features (from `IDEAS-v1.6+.md`), all **zero-LLM, bilingual EN/TH, per-user isolated, and
registry-generic** so v1.7 custom habits inherit every one for free (a hard design rule — §4 R-X1):
1. **Live pinned "Today" dashboard** — a per-user message pinned to the chat that **edits itself in place** as
   they log/undo/edit, an always-visible progress board with zero extra pings.
2. **Consistency heatmap** — a GitHub-style calendar PNG (matplotlib, graceful no-matplotlib fallback).
3. **Personal bests & records** — lifetime records per habit (best day, best week, longest streak),
   celebrated once when broken (gentle, once-per-crossing like milestones).
4. **Deterministic trends** — transparent week-over-week deltas surfaced in the weekly review and `/trends`.
5. **"Almost there" end-of-day nudge** — one kind push when a goal-bearing habit is close, once/day, DND-aware,
   built on the v1.5 check-in tick.

Additive: **migration 009** adds one `users` column (`dashboard_msg_id`) and one table (`habit_records`).
DND/check-in machinery is reused, not rebuilt. Regression gate: the **full suite (2607 baseline) stays
green**; no existing confirmation changes except the additive record-celebration line and the (opt-in)
dashboard. A **v1.6.0 release-notes entry** is required at release (announced by the v1.5 `announce` module).

SemVer: **1.6.0 (MINOR)**.

## 2. Inputs

### 2.1 New commands (deterministic, LLM-free, whole-message-anchored; Thai aliases under the strict anti-false-positive discipline)
```
/dashboard on | off        # enable/disable the pinned live dashboard   (Thai: แดชบอร์ด)
/dashboard                 # show current dashboard state
/heatmap [habit] [weeks]   # calendar heatmap PNG (default: all habits, 12 weeks)   (Thai: ปฏิทิน)
/records [habit]           # my lifetime records                          (Thai: สถิติ)
/trends [habit]            # week-over-week deltas                         (Thai: แนวโน้ม)
```
The **nudge** (#5) has no command of its own — it rides check-in enablement (OQ2, §9). Records/trends are
derived (no setter). Tail grammar mirrors `/history`: an optional registry habit id then an optional integer,
whole-message-anchored, registry/numeric-gated (adversarial-corpus AC).

### 2.2 New channel capability (concrete-default ABC pattern, as always)
`send` currently returns `None`; a pinned dashboard needs the sent message id and edit/pin/unpin. Three new
`Channel` methods, each a **concrete default** that degrades for non-Telegram channels / test fakes:
`send_and_pin`, `edit_message`, `unpin` (§5).

### 2.3 Per-user state
- Dashboard: `users.dashboard_msg_id` (NEW, nullable) — `NULL` = disabled (default); a message-id string =
  enabled with that pinned message.
- Records: `habit_records(user_id, habit_id, record_type, value, achieved_on)` (NEW table).
- Trends/heatmap/nudge: **derived** from the existing `logs`/`habit_targets` — no new storage.

## 3. Outputs

### 3.1 Live dashboard (edited in place, silent)
```
📌 Today — Sun 24 Aug
💧 water    1500 / 2500 ml   ▓▓▓▓▓▓░░░░ 60%
🧘 stretch  ✓ done
📔 diary    — not yet
```
Registry-generic (one line per habit; goal-bearing → progress bar+%, boolean → ✓/–, count-only → count).

### 3.2 Heatmap PNG (language-neutral in-image text)
A calendar grid, one cell per day over N weeks, cell colour = goal-met (or logged-intensity). **In-image text
is numbers + month abbreviations only** (no Thai glyphs → no tofu, §4 R-H3); the caption is bilingual.

### 3.3 Records / trends / nudge / celebration
```
# /records water
🏆 water records
• Best day: 3200 ml (12 Aug)
• Best week: 18.1 L (5–11 Aug)
• Longest streak: 14 days

# on a log that breaks a record (appended to the normal confirmation, once):
🎉 New personal best — longest water streak: 15 days!

# /trends water
📊 water — this week vs last: 2450 → 2780 ml (+13%) · 3 weeks rising 📈

# "almost there" nudge (once/day, near end of day):
💧 Just 300 ml to hit your water goal today — you've got this.
```

### 3.4 Errors / edge
All read-only surfaces (`/heatmap`, `/records`, `/trends`, `/dashboard` show) and the nudge never raise; a
DB/render/edit failure is logged and degraded (fail-open). `/heatmap` with matplotlib missing → a friendly
text fallback (R-H2).

## 4. Behavior rules

### Cross-cutting (shared surface)
- **R-X1** (registry-generic — design rule so v1.7 custom habits inherit all five for free) Every feature
  reads habits, goals, and values through the `HabitRegistry` + `targets.effective_goal` + `db.sum_value`/
  `count`/`count_true` — **no hardcoded habit ids**. An extra configured habit automatically appears in the
  dashboard, heatmap, records, trends, and is nudge-eligible, with **no per-feature code change** (AC-X1).
- **R-X2** (bilingual, per-user, LLM-free) All copy via `core/i18n.py` (EN+TH); every DB read/write scoped to
  the acting `user_id` (U-ISO); zero Ollama calls anywhere in these five features.
- **R-X3** (audit vocabulary) `core/audit.py` `ACTIONS` gains `dashboard_set` / `dashboard_off` — the only
  new user-settable state this release. Records/trends/heatmap/nudge add no settable state (nudge rides
  check-in enablement, OQ2). `/dashboard on|off` records one fail-open audit row (`source="command"`), same
  pattern as `execute_checkin`.
- **R-X4** (release notes) A `RELEASE_NOTES["1.6.0"]` entry (EN+TH) ships in `core/release_notes.py` so the
  v1.5 `announce` step announces this release; added before tagging (release-process step).

### Feature 1 — Live dashboard (module `dashboard`)
- **R-D1** (opt-in — OQ1) `/dashboard on` renders the board (R-D2), calls `channel.send_and_pin(user_id,
  text)`, and stores the returned id in `users.dashboard_msg_id`; `/dashboard off` calls `channel.unpin`,
  clears the column, and confirms; a user with `NULL` (default) has **no** dashboard. Recommended default:
  **opt-in** (consistent with the v1.5 check-in precedent; auto-pinning would fire an unsolicited "pinned a
  message" notification — §9 OQ1).
- **R-D2** (content, registry-generic) `dashboard.render(db, config, registry, lang, user_id, clock) -> str`
  = one line per habit for today: goal-bearing → `today/goal unit` + a bar + %; boolean → ✓/–; count-only →
  count. Deterministic, LLM-free.
- **R-D3** (live edit + throttle) `dashboard.refresh(db, channel, config, registry, user_id, clock)` runs
  after every state change (R-D5 triggers): if `dashboard_msg_id` is `NULL` → return (disabled); render; if
  the rendered text equals the last-sent text (in-process per-user cache) → skip (avoids Telegram's
  "message is not modified" and needless calls); else `channel.edit_message(user_id, msg_id, text)`. Edits
  are **silent** (Telegram `editMessageText` sends no notification).
- **R-D4** (self-healing, fail-open) If `edit_message` returns `False` (the user deleted the pinned message
  → "message to edit not found"), `refresh` recreates it via `send_and_pin` and stores the new id (still
  enabled). Any edit/pin failure is logged and swallowed — **a dashboard problem never breaks the log/undo
  that triggered it** (the confirmation is sent first, the refresh is best-effort after).
- **R-D5** (triggers) `refresh` is called (integration, `main.py`) after: a **log** (incl. pre-parsed +
  recovery), an **undo** (text + button), an **edit**, and a **target change**; plus a **day-rollover**
  refresh at `00:00` in the minutely job for every enabled user (so the board resets to the new day without
  waiting for a log).
- **R-D6** (DND-exempt) Dashboard edits are silent by nature and are **not** subject to DND. Only the
  one-time pin at `/dashboard on` notifies, and that is user-initiated (§9).

### Feature 2 — Consistency heatmap (module `heatmap`)
- **R-H1** `/heatmap [habit] [weeks]` → `heatmap.render(db, config, registry, lang, user_id, habit_id,
  weeks, clock) -> bytes | None` renders a calendar-grid PNG (default: a per-habit set over 12 weeks; cell
  colour = goal-met for goal-bearing habits, else logged-day intensity) and sends via `channel.send_image`.
  Registry-generic; per-user.
- **R-H2** (graceful fallback) matplotlib is optional (as with v1.0 charts): if it is unavailable or a render
  raises, return `None` and the command replies with a friendly text summary / "charts unavailable" message —
  never crashes (mirrors `core/charts.py`'s guard).
- **R-H3** (language-neutral labels — accept the Thai-tofu limitation explicitly) In-image text is limited to
  **numbers and month abbreviations** (`Aug`, `01`…), which render fine in matplotlib's default font; **no
  Thai text goes inside the PNG** (the known tofu-box limitation). The bilingual habit label + explanation
  live in the **caption** (`send_image`'s text), not the image.
- **R-H4** (optional review attach) When `[charts] enabled`, the heatmap MAY be attached to the weekly review
  alongside the existing bar charts (reuses the review's image fan-out). Off by default is acceptable.

### Feature 3 — Personal bests & records (module `insights`)
- **R-R1** (stored, not re-derived) Records live in `habit_records` (one row per `(user_id, habit_id,
  record_type)`), `record_type ∈ {best_day, best_week, longest_streak}`. Stored (not computed-on-read) so
  "beaten?" is a cheap compare and "celebrate once when broken" is exact (mirrors the milestone once-per-
  crossing design). `best_day`/`best_week` = the day/7-day aggregate (sum for numeric/duration, count for
  boolean/text); `longest_streak` = `streaks.compute_streak` (registry-generic).
- **R-R2** (celebrate once) On a log (integration, in the same place the milestone check runs), recompute the
  affected records for that habit; if a value **strictly exceeds** the stored record, update the row and
  append **one** gentle `record_broken` line to that log's confirmation. Strictly-greater + stored-value
  means it fires exactly once per crossing, never repeated for further logs at the same level (same guarantee
  as `crossed_milestone`). Fail-open: a records error never blocks the confirmation.
- **R-R3** `/records [habit]` → `records.render(...)` shows the user's current records (all habits, or one),
  bilingual, per-user. A habit with no records yet shows a friendly "no records yet" line.
- **R-R4** Registry-generic + per-user isolated (records are keyed by `user_id`).

### Feature 4 — Deterministic trends (module `insights`)
- **R-T1** `trends.compute(db, config, registry, user_id, clock)` = per habit, this-week total vs last-week
  total (two rolling 7-day windows, same "week" convention as the review), the signed delta and % change, and
  the run-length of consecutive rising/falling weeks. Pure deterministic aggregation — **zero LLM**.
- **R-T2** Surfaced in the **weekly review** (a one-line-per-habit trend block) and via `/trends [habit]`.
  A gentle "N weeks rising 📈" callout only when the run-length ≥ 2.
- **R-T3** Registry-generic + per-user; insufficient history (no last-week data) renders a graceful "not
  enough history yet" rather than a divide-by-zero or a misleading %.

### Feature 5 — "Almost there" end-of-day nudge (module `nudge`)
- **R-N1** `nudge.run_due_nudges(channel, config, registry, db, clock)` runs on the **same minutely job** as
  the check-in/reminder ticks. It fires only at `[nudge] time` (default `"20:00"`), once/day by construction
  (a single fixed minute). For each active user with **check-ins enabled** (rides `checkins.effective_checkin`
  — OQ2) and **not in DND** (`reminders.in_dnd_now`): for each goal-bearing habit whose today's total is
  **≥ `[nudge] threshold_pct`% of goal but < goal** ("close"), send one encouraging `nudge_close` message
  naming the remaining amount. A met or far-from-goal habit → nothing.
- **R-N2** (non-nagging) At most one nudge message per user per day (the single fixed time); habits already
  met are never nudged; if no habit is "close", nothing is sent (silence, not a "keep going" nag). DND-aware;
  honors the user's language. Fires only for opt-in (check-in-enabled) users, so the default (check-ins off)
  is **no nudge** — opt-in for everyone, consistent with v1.5.
- **R-N3** `close` threshold is `[nudge] threshold_pct` (default 80, configurable); the nudge time is
  `[nudge] time` (default 20:00). Registry-generic + per-user; strictly LLM-free.

## 5. Interfaces (signatures)
```python
# storage/migrations.py
def _migration_009_dashboard_and_records(conn) -> None: ...
#   ALTER TABLE users ADD COLUMN dashboard_msg_id TEXT NULL
#   CREATE TABLE habit_records (user_id TEXT, habit_id TEXT, record_type TEXT, value REAL,
#                               achieved_on TEXT, PRIMARY KEY(user_id, habit_id, record_type))

# storage/db.py
def get_dashboard_msg_id(self, chat_id: str) -> str | None: ...
def set_dashboard_msg_id(self, chat_id: str, message_id: str | None) -> None: ...
def get_records(self, user_id: str, habit_id: str | None = None) -> list[sqlite3.Row]: ...
def get_record(self, user_id: str, habit_id: str, record_type: str) -> float | None: ...
def upsert_record(self, user_id: str, habit_id: str, record_type: str, value: float, achieved_on: str) -> None: ...

# channels/base.py  (ABC — concrete defaults; non-Telegram degrades)
async def send_and_pin(self, chat_id: str, text: str) -> str | None:
    """Send + pin; return the new message id. Default: send(text); return None (no pin)."""
    await self.send(chat_id, text); return None
async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
    """Edit in place; True on success or 'not modified'; False on 'not found'/other. Default: return False."""
    return False
async def unpin(self, chat_id: str, message_id: str) -> None:
    """Unpin (+ best-effort delete). Default: no-op."""
    return None
# TelegramChannel overrides: editMessageText / pinChatMessage / unpinChatMessage(+deleteMessage);
#   sendMessage captures result.message_id; 400 "message is not modified" -> True; "not found" -> False.

# core/dashboard.py  (module `dashboard`)
def render(db, config, registry, lang, user_id, clock) -> str: ...
async def refresh(db, channel, config, registry, user_id, clock=datetime.now) -> None: ...   # R-D3/R-D4
async def execute_dashboard(command, *, db, channel, config, registry, lang, user_id, clock) -> str: ...  # on/off/show

# core/heatmap.py  (module `heatmap`)
def render(db, config, registry, lang, user_id, habit_id, weeks, clock) -> bytes | None: ...
async def execute_heatmap(command, *, db, channel, config, registry, lang, user_id, clock) -> str: ...

# core/records.py  (module `insights`)
RECORD_TYPES = ("best_day", "best_week", "longest_streak")
def update_on_log(db, config, registry, habit, user_id, clock) -> list[tuple[str, float]]: ...  # returns records just broken
def render(db, config, registry, lang, user_id, habit_id=None) -> str: ...

# core/trends.py  (module `insights`)
def compute(db, config, registry, user_id, clock) -> list[...]: ...
def render(db, config, registry, lang, user_id, habit_id=None, clock=datetime.now) -> str: ...
def review_block(db, config, registry, lang, user_id, clock) -> str: ...   # weekly-review integration

# core/nudge.py  (module `nudge`)
async def run_due_nudges(channel, config, registry, db, clock=datetime.now) -> None: ...

# core/commands.py  (extend)
CommandKind = Literal[..., "dashboard", "heatmap", "records", "trends"]   # reuses category + limit fields

# config.py
class NudgeConfig(BaseModel):
    threshold_pct: int = 80
    time: str = "20:00"
# Config gains: nudge: NudgeConfig = NudgeConfig()
```

## 6. Files to touch
**Shared surface (first, sequentially):**
- `storage/migrations.py` — migration 009 (`users.dashboard_msg_id` + `habit_records`).
- `storage/db.py` — dashboard-id + records methods.
- `channels/base.py`, `channels/telegram.py`, `channels/line.py` — `send_and_pin`/`edit_message`/`unpin`.
- `core/audit.py` — `dashboard_set`/`dashboard_off` actions.
- `config.py` + `config.toml` — `[nudge]` (+ any `[dashboard]`/`[heatmap]` toggles).
- `core/commands.py` — `CommandKind` additions + `Command` fields (skeleton); `core/i18n.py` — key-block skeletons.
- `core/release_notes.py` — `RELEASE_NOTES["1.6.0"]` (EN+TH).

**Module `dashboard`:** `core/dashboard.py` + `commands.py` (dashboard kind) + `i18n.py` (dashboard keys) + `tests/test_dashboard.py`.
**Module `heatmap`:** `core/heatmap.py` + `commands.py` (heatmap kind) + `i18n.py` (heatmap keys) + `tests/test_heatmap.py`.
**Module `insights`:** `core/records.py`, `core/trends.py` + `commands.py` (records/trends kinds) + `i18n.py` (records/trends keys) + `tests/test_records.py`, `tests/test_trends.py`.
**Module `nudge`:** `core/nudge.py` + `i18n.py` (nudge keys) + `tests/test_nudge.py`.

**Integration seam (`main.py`):** call `dashboard.refresh` after every log/undo/edit/target-change + a 00:00
day-rollover refresh in the minutely job; call `records.update_on_log` in the log confirmation path (append
the celebration line); add `trends.review_block` to the weekly review; call `nudge.run_due_nudges` in the
minutely job; route `dashboard`/`heatmap`/`records`/`trends` command kinds; add `/dashboard`/`/heatmap`/
`/records`/`/trends` to the public `set_my_commands` menu.

## 7. External dependencies
None new. matplotlib remains the one optional dependency (heatmap reuses it, graceful fallback). No new
Telegram API host — `editMessageText`/`pinChatMessage`/`unpinChatMessage`/`deleteMessage` are standard Bot
API on the same client. Migration 009 additive.

## 8. Acceptance criteria

### Shared / cross-cutting
- **AC-1** (migration 009): Given a v1.5 DB at `user_version=8`, migration 009 adds `users.dashboard_msg_id` + the `habit_records` table, touches no existing data, is idempotent (stamps 9), and the full suite (2607 baseline) stays green. (R-X, R-D1/R-R1 storage)
- **AC-2** (channel methods): Given the new ABC methods, non-Telegram channels/fakes degrade (concrete defaults — `send_and_pin` sends + returns None, `edit_message` returns False, `unpin` no-ops); `TelegramChannel` builds correct `editMessageText`/`pinChatMessage`/`unpinChatMessage` requests, captures `message_id` from `send_and_pin`, maps "not modified"→True and "not found"→False. (R-D3/R-D4, §5)
- **AC-3** (audit + regression): Given `/dashboard on`/`off`, one fail-open `dashboard_set`/`dashboard_off` audit row is recorded; no existing confirmation/behavior changes except the additive record-celebration line + the opt-in dashboard. (R-X3)
- **AC-X1** (registry-generic design rule): Given an extra configured habit, it appears in the dashboard, heatmap, records, and trends, and is nudge-eligible, with no per-feature code change (so v1.7 custom habits inherit all five). (R-X1)
- **AC-X2** (release notes): A `RELEASE_NOTES["1.6.0"]` entry (EN+TH) exists and is announced by the v1.5 `announce` step. (R-X4)
- **AC-X3** (per-user isolation): For every feature, user A's dashboard/heatmap/records/trends/nudge reflect only A's data and are delivered only to A. (R-X2)

### Feature 1 — dashboard
- **AC-D1** (opt-in): Given `/dashboard on`, a message is sent + pinned and its id stored; `/dashboard off` unpins + clears; a default user (NULL) has no dashboard and no updates. (R-D1)
- **AC-D2** (live silent edit): Given an enabled user logs/undoes/edits, the pinned message is edited in place (no new message), silently, reflecting today's per-habit progress; an unchanged render is skipped (no redundant edit). (R-D3/R-D5)
- **AC-D3** (self-heal + fail-open): Given the pinned message was deleted (`edit_message`→False), the next refresh recreates + re-pins and stores the new id; any dashboard failure is logged and never blocks the triggering log/undo. (R-D4)
- **AC-D4** (day rollover): At 00:00, an enabled user's dashboard refreshes to the new day (yesterday's totals cleared). (R-D5)
- **AC-D5** (DND-exempt): Given a user in DND, dashboard edits still occur (silent); only the one-time `/dashboard on` pin notifies. (R-D6)
- **AC-D6** (registry-generic content): The dashboard renders one correct line per configured habit by type. (R-D2/R-X1)

### Feature 2 — heatmap
- **AC-H1** (render + send): Given `/heatmap` (and `/heatmap water 8`), a calendar PNG is rendered per-user and sent via `send_image`; default all-habits/12-weeks. (R-H1)
- **AC-H2** (graceful fallback): Given matplotlib unavailable or a render error, `/heatmap` replies with a friendly text fallback and never crashes. (R-H2)
- **AC-H3** (language-neutral PNG): The PNG contains only numbers/month abbreviations (no Thai glyphs); the bilingual label/explanation is in the caption. (R-H3)
- **AC-H4** (optional review attach): With `[charts] enabled`, the heatmap may attach to the weekly review without breaking the text-only path. (R-H4)

### Feature 3 — records
- **AC-R1** (stored + updated): A log that exceeds a stored `best_day`/`best_week`/`longest_streak` updates that `habit_records` row (value + date). (R-R1)
- **AC-R2** (celebrate once): Beating a record appends one `record_broken` line to that log's confirmation, exactly once per crossing (not repeated for further logs at the same level); fail-open. (R-R2)
- **AC-R3** (`/records`): `/records [habit]` shows current records bilingually, per-user; no-records-yet renders gracefully. (R-R3)

### Feature 4 — trends
- **AC-T1** (`/trends`): `/trends [habit]` shows this-week-vs-last-week total, signed delta + %, deterministically (zero LLM). (R-T1)
- **AC-T2** (review + run-length): The weekly review includes a per-habit trend block; a "N weeks rising 📈" callout appears only when the run-length ≥ 2. (R-T2)
- **AC-T3** (insufficient history): With no last-week data, `/trends` renders "not enough history yet" (no divide-by-zero / misleading %). (R-T3)

### Feature 5 — nudge
- **AC-N1** (close, once/day): At `[nudge] time`, a check-in-enabled user with a goal-bearing habit at ≥`threshold_pct`% but <100% receives one `nudge_close` message naming the remainder; met/far habits → nothing; at most one nudge/user/day. (R-N1/R-N2)
- **AC-N2** (opt-in + DND): A user with check-ins off (default) gets no nudge; a user in DND at the nudge time is suppressed. (R-N1/R-N2)
- **AC-N3** (registry-generic + bilingual): The nudge covers any goal-bearing habit, in the user's language. (R-N3/R-X1)

## 9. Resolved decisions & open questions

**Open questions:** none remaining — both resolved by the user on **2026-08-24**, each as the recommended default (already specced; no other changes).
- **OQ1 — Dashboard enablement (RESOLVED 2026-08-24: opt-in via `/dashboard on`).** Consistent with the v1.5
  check-in precedent; auto-pinning would fire an unsolicited "pinned a message" notification. Baked into
  R-D1/AC-D1.
- **OQ2 — Nudge enablement (RESOLVED 2026-08-24: rides check-in enablement).** One opt-in — `/checkin on` —
  gives the whole gentle-proactive suite (hourly check-ins + the end-of-day almost-there nudge); no separate
  toggle/column. Baked into R-N1/R-N2/AC-N2.

**Decisions recorded (defaults; not load-bearing):**
- **Records are stored** (`habit_records` table), not re-derived on read — makes "beaten?" a cheap compare and
  "celebrate once" exact (mirrors milestones).
- **Heatmap accepts the Thai-tofu limitation** by keeping in-PNG text language-neutral (numbers/month abbrev);
  bilingual copy lives in the caption.
- **Dashboard edits are DND-exempt** (silent by nature); only the one-time pin notifies.
- **Nudge: close = 80%** (`[nudge] threshold_pct`), fires once/day at `[nudge] time` (default 20:00).
- **Trends = deterministic week-over-week** (+ run-length), zero-LLM — a transparent contrast to the existing
  LLM review narrative, which is unchanged.

**Risks:**
- **Dashboard edit volume / rate limits.** Small user base + the unchanged-render skip (R-D3) keep edits
  infrequent; fail-open on 429. The main risk is the many trigger sites in `main.py` — all must call `refresh`
  *after* sending the confirmation, never before, so a dashboard hiccup can't swallow a log.
- **Records touching the log path.** The celebration hook runs alongside the milestone check; must be
  fail-open and additive (the confirmation is byte-identical when no record breaks) — AC-3/AC-R2.
- **Heatmap Thai text** — explicitly out of the PNG (caption only); documented, not a bug.

## 10. Out of scope
- Auto-enabling the dashboard (opt-in only, OQ1); a web/Mini-App dashboard (Telegram message only).
- Thai text *inside* chart/heatmap PNGs (font limitation; caption carries Thai).
- Records beyond best-day/best-week/longest-streak; leaderboards or cross-user records (per-user only).
- LLM-based trend narration or correlation mining (trends are deterministic deltas only).
- A separate `/nudge` toggle (unless OQ2 is answered that way); per-habit nudge thresholds.
- v1.7 **custom habits** — a separate release; v1.6 only guarantees these features are registry-generic so
  custom habits inherit them (R-X1).

## 11. Module split & parallel development
**Total functionals:** 9 — (1) migration 009 + dashboard/records store, (2) channel pin/edit methods,
(3) live dashboard + `/dashboard`, (4) heatmap + `/heatmap`, (5) records + `/records`, (6) trends + `/trends`,
(7) end-of-day nudge, (8) audit vocab + release notes, (9) registry-generic guarantee. Above the threshold.

**Recommendation:** **SEQUENTIAL shared surface, then 4 PARALLEL modules, then integration.** The channel
pin/edit methods, migration 009, db methods, audit vocab, config, and the `CommandKind`/`Command`/i18n
skeletons are a shared surface every module builds on — built first, sequentially. After it lands, four
modules touch **disjoint files** (each owns its `core/*.py`, its command kind(s), and its i18n key block;
`records`+`trends` share the `insights` module since both are "history insight" surfaced in the review):

**Shared surface (first):** migration 009 + db methods; `channels/*` `send_and_pin`/`edit_message`/`unpin`;
`audit.py` dashboard actions; `[nudge]` config; `CommandKind`/`Command`/i18n **skeletons** (so the four
modules never collide on `commands.py`/`i18n.py`); `RELEASE_NOTES["1.6.0"]`.

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| `dashboard` | AC-D1, AC-D2, AC-D3, AC-D4, AC-D5, AC-D6 | `core/dashboard.py`, `commands.py` (dashboard kind), `i18n.py` (dashboard keys), `tests/test_dashboard.py` | shared: channel pin/edit, `db.*_dashboard_msg_id`, audit |
| `heatmap` | AC-H1, AC-H2, AC-H3, AC-H4 | `core/heatmap.py`, `commands.py` (heatmap kind), `i18n.py` (heatmap keys), `tests/test_heatmap.py` | shared: `send_image` (existing), registry |
| `insights` | AC-R1, AC-R2, AC-R3, AC-T1, AC-T2, AC-T3 | `core/records.py`, `core/trends.py`, `commands.py` (records/trends kinds), `i18n.py` (records/trends keys), `tests/test_records.py`, `tests/test_trends.py` | shared: `db.*_record`, `streaks.compute_streak` |
| `nudge` | AC-N1, AC-N2, AC-N3 | `core/nudge.py`, `i18n.py` (nudge keys), `tests/test_nudge.py` | shared: `checkins.effective_checkin`, `reminders.in_dnd_now`, `[nudge]` config |

ACs verified during the shared-surface / integration pass: **AC-1, AC-2, AC-3, AC-X1, AC-X2, AC-X3**
(migration + channel + audit + registry-generic + release notes + isolation, plus the `main.py` triggers:
dashboard refresh sites, records celebration hook, trends review block, nudge job wiring). Every AC belongs
to exactly one owner. **Total: 25 acceptance criteria** (shared/integration 6, `dashboard` 6, `heatmap` 4,
`insights` 6, `nudge` 3).

**Integration order (after the four modules complete):**
1. `main.py`: wire `dashboard.refresh` after every log/undo/edit/target-change + a 00:00 day-rollover refresh;
   the `records.update_on_log` celebration hook in the confirmation path; `trends.review_block` in the weekly
   review; `nudge.run_due_nudges` in the minutely job; route the four command kinds; add the four commands to
   the public menu.
2. Full suite; highest-value gates: **AC-1/AC-3/AC-X3** (additive, regression-clean, isolated), **AC-D3**
   (dashboard fail-open — never breaks a log), **AC-R2** (records once-per-crossing + byte-identical when no
   record breaks).
3. Integration tests: two users — a dashboard edits live on each log and self-heals after a manual delete;
   a record-break appends exactly one celebration line; `/heatmap` renders (and degrades without matplotlib);
   `/trends` shows week-over-week; the 20:00 nudge fires once only for a close, check-in-enabled, non-DND user.
```
