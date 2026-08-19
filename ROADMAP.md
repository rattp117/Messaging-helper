# ROADMAP — Local Habit-Tracking Assistant

> Ten successive, independently shippable versions on top of the v0.1.0 MVP.
> Grounded in the current source under `src/habit_assistant/`, `SPEC.md`, and `IMPL.md`.
> Every item honors the hard constraints: **local-first** (SQLite + diary text never leave the
> user's machines; LLM only to `http://mac-mini:11434`), a **clean `Channel` seam**
> (nothing in `core/`/`storage/` imports a concrete channel), **bilingual Thai + English**,
> **minimal deps**, **single long-running process**, **single user**.

---

## 1. The ten versions at a glance

| Version | Name | One-line value |
|---|---|---|
| **v0.2.0** | Extraction Reliability & Model Fallback | Stop mis-logging: detect the MLX schema gap, fall back to a schema-conformant model, clarify on low confidence. |
| **v0.3.0** | Runtime Resilience & Self-Monitoring | Survive Ollama/Telegram outages 24/7 — retry, degrade gracefully, alert once, auto-recover. |
| **v0.4.0** | Migrations & Backup/Restore | Versioned schema changes without data loss, plus one-command DB backup/restore. |
| **v0.5.0** | Command Layer & Edit/Undo | "undo last" / "ยกเลิกอันล่าสุด" and correcting a mistaken entry, via a clean command seam. |
| **v0.6.0** | Bilingual Output & Message Catalog | Every reply, reminder, and review speaks Thai or English to match how the user wrote. |
| **v0.7.0** | Multi-Habit Extensibility | Define arbitrary habits (sleep, steps, meds…) in config — no code change per habit. |
| **v0.8.0** | Natural-Language Queries | "how much water this week?" / "อาทิตย์นี้ยืดกี่ครั้ง" answered on demand. |
| **v0.9.0** | Adaptive Reminders, Snooze & Quiet Hours | Skip reminders already satisfied; respect DND windows; snooze on request. |
| **v0.10.0** | Streaks, Gentle Gamification & Daily Summary | Per-habit streaks, opt-in encouragement, an end-of-day recap. |
| **v1.0.0** | Insights: Charts-as-Images + Garmin Import | Weekly review with chart images and Garmin hydration cross-check — declared stable. |

**Numbering note.** I recommend the capstone be **1.0.0**, not v0.11.0. By that point the app is
resilient, bilingual, multi-habit, and self-serviceable — a mature product the user relies on daily,
which is what a 1.0 signals. All pre-1.0 steps bump MINOR (per the project's SemVer policy, pre-1.0
features and breaking changes both bump MINOR).

**Deferred on purpose (not in the ten — see §4):** LINE channel (needs a public endpoint — an infra
decision that breaks the "no inbound from the cloud" posture), and voice-message transcription
(needs a second local service + model; not served by Ollama). Both are real, both are flagged with
open questions, and either can be slotted in if the user prioritizes it.

---

## 2. Per-version detail

Legend: **⚑ NEEDS USER INPUT** marks a decision required before implementation starts.

---

### v0.2.0 — Extraction Reliability & Model Fallback

**Goal.** Make logged data trustworthy. `IMPL.md` flags the load-bearing problem: the default
`qwen3.5:9b-mlx` on the MLX backend **ignores the JSON-schema `format`**, so today correctness rests
entirely on few-shot prompting + fail-closed validation. An off-schema-but-plausible response can
mis-log a value.

**User value.** "When I say '2 แก้ว' it records 500 ml — reliably — and when it isn't sure, it asks
instead of guessing."

**Scope (files).**
- `llm/ollama_client.py` — add a startup schema-conformance probe (`probe_schema_support()`): send a
  known message with the schema, check whether the reply is exactly the 5 required keys; add a
  `chat_json` fallback that tries models in order.
- `config.toml` / `config.py` — `[ollama] models = ["qwen3.5:9b-mlx", "qwen3:8b"]` (ordered fallback
  chain, replacing the single `model`; keep `model` as back-compat alias), plus
  `confidence_threshold = 0.55`.
- `core/parser.py` — treat a valid category with `confidence < threshold` as `unknown` (clarify).
- `llm/prompts.py` — no structural change; keep few-shot as belt-and-suspenders.

**Acceptance criteria.**
1. AC2.1: On startup, the client probes each configured model once and logs, per model, whether it
   honors `format` (exact-keys check). Probe failure never crashes startup.
2. AC2.2: `chat_json` tries models in configured order; if the first returns off-schema JSON (fails
   `_validate`), it retries the next model; result is the first schema-valid extraction, else `unknown`.
3. AC2.3: A valid extraction whose `confidence` is below `confidence_threshold` yields the clarifying
   question and writes no row (mockable, deterministic).
4. AC2.4: With a single-model config (`models = ["x"]`), behavior is identical to v0.1.0 for
   schema-valid responses (no regression).
5. AC2.5: All new paths fail closed to `unknown` on transport/HTTP error — the inbound loop never
   raises.

**New dependencies.** None.

**Risks.** Fallback multiplies latency on bad responses (bounded: N models × timeout — cap N at 2).
The probe adds ~1 startup call per model. A schema-conformant fallback model must actually be pulled
on the mac-mini or the chain is a no-op.

**⚑ NEEDS USER INPUT.** Which fallback model to `ollama pull` on the mac-mini. Default: `qwen3:8b`
(IMPL.md confirms it's schema-conformant on that server). Confirm it's installed.

---

### v0.3.0 — Runtime Resilience & Self-Monitoring

**Goal.** It's a 24/7 unattended process. Today a `getUpdates` failure or an Ollama outage is handled
per-message but there's no health visibility and no user-facing alert. Make outages survivable and
visible.

**User value.** "If the Mac Mini's Ollama goes down, I get told once, my messages still get queued or
acknowledged, and it recovers by itself — I'm never silently losing data."

**Scope (files).**
- `channels/telegram.py` — wrap the long-poll loop with exponential backoff on transport errors
  (don't tight-loop on a network blip); preserve `offset` so no updates are dropped/duplicated.
- `llm/ollama_client.py` — bounded retry with backoff; surface an "LLM unavailable" state.
- `core/health.py` (new) — a monitor task: periodic reachability checks of Ollama (`/api/version`)
  and Telegram (`getMe`); track UP/DOWN transitions; "alert once per transition, not per failure."
- `main.py` — register the monitor as an asyncio task; on inbound while LLM is DOWN, acknowledge and
  **defer** (store `raw_message` with `category='unparsed'`) rather than drop, and re-parse on recovery.
- `core/commands.py` seam is NOT yet present — a `/status` command is deferred to v0.5.0; here the
  alert is push-only.

**Acceptance criteria.**
1. AC3.1: Simulated Telegram transport errors trigger exponential backoff (e.g. 1s→2s→4s, capped)
   and the loop recovers without dropping the polling `offset` (no missed/duplicated updates).
2. AC3.2: When Ollama transitions DOWN→ (stays down), exactly **one** alert is sent to the channel;
   no repeat alert until it goes UP and DOWN again.
3. AC3.3: A message received while the LLM is unavailable is acknowledged and persisted as
   `unparsed` (raw text kept), then automatically re-parsed and confirmed when Ollama returns.
4. AC3.4: When Telegram is unreachable, the failure is logged and retried; the process stays alive
   (no crash, no exit).
5. AC3.5: Health checks are read-only network calls to the two already-allowed hosts only — no new
   outbound destinations.

**New dependencies.** None.

**Risks.** The `unparsed`/re-parse path needs a schema column to exist safely → it's cleaner *after*
migrations (v0.4). **Mitigation:** ship v0.3 with an in-memory deferral queue (lost on restart), and
persist it properly once v0.4 lands — or reorder v0.4 before v0.3 (see §3). Alert-on-Telegram-down is
inherently limited (the alert channel may be the thing that's down) — log is the fallback.

**⚑ NEEDS USER INPUT.** Is a Telegram push + local log sufficient for outage alerts, or is a
secondary channel wanted (e.g. a local Windows notification)? Default: Telegram-when-reachable + log,
no third channel (keeps it local-first and dependency-free).

---

### v0.4.0 — Migrations & Backup/Restore

**Goal.** The DB layer currently runs `CREATE TABLE IF NOT EXISTS` only — there is no way to evolve
the schema without risking the user's data. Every downstream feature (soft-delete for undo,
`unparsed` deferral, multi-habit) needs schema changes. Build the foundation once.

**User value.** "Schema upgrades never lose my history, and I can back up and restore my whole diary
with one command."

**Scope (files).**
- `storage/migrations.py` (new) — a `user_version`-based migration runner (SQLite `PRAGMA
  user_version`): an ordered list of migration functions applied inside a transaction, idempotent,
  logged. No third-party migration framework.
- `storage/db.py` — call the runner on `__init__` instead of the inline `executescript`; keep WAL.
- `core/backup.py` (new) — `backup()` uses SQLite's online backup API to write a timestamped copy
  under `data/backups/`; `restore(path)` validates and swaps atomically.
- `main.py` — CLI: `--backup`, `--restore <file>`, `--migrate` (apply pending, then exit).
- `config.toml` — `[backup] dir = "data/backups"`, optional `retain = 14`.

**Acceptance criteria.**
1. AC4.1: A fresh DB reports schema version 0→N after startup; migrations run exactly once and are
   idempotent (second startup applies nothing).
2. AC4.2: A DB created by v0.1.0 (baseline schema) migrates forward with all existing `logs` rows
   intact (row count and values unchanged).
3. AC4.3: `--backup` produces a restorable copy while the DB is in use (WAL); the copy opens and
   queries identically to the source.
4. AC4.4: `--restore <file>` replaces the live DB atomically; a corrupt/invalid file is rejected with
   a clear error and leaves the current DB untouched.
5. AC4.5: `retain` prunes backups older than the configured count/age; never deletes the newest.

**New dependencies.** None (stdlib `sqlite3` online-backup API). **Rejected:** Alembic — it's bound to
SQLAlchemy, which this project deliberately doesn't use; a 30-line `user_version` runner is sufficient
and keeps the minimal-deps constraint.

**Risks.** Restore is destructive by nature — guard with a confirmation flag and an automatic
pre-restore backup. WAL checkpointing must be handled so a backup isn't missing recent writes.

**⚑ NEEDS USER INPUT.** None required (sensible defaults). Optional: preferred backup retention.

---

### v0.5.0 — Command Layer & Edit/Undo

**Goal.** Introduce a **command-dispatch seam** in front of the LLM parser, then implement undo/edit.
Today `handle_inbound_message` sends every message straight to `parse_message`; there's no way to
correct a mis-log. This seam is the shared surface that NL queries (v0.8), snooze (v0.9), and
`/status`/`/backup` commands ride on.

**User value.** "ยกเลิกอันล่าสุด" / "undo last" removes my last entry; "แก้เป็น 300ml" / "make that
300ml" corrects it — in Thai or English.

**Scope (files).**
- `core/commands.py` (new) — a small router: match leading `/command` **and** bilingual natural
  phrases (undo/ยกเลิก, edit/แก้, delete/ลบ) before falling through to `parse_message`. Pure function,
  no channel import.
- `storage/db.py` + a migration — soft-delete (`deleted_at` column) so undo is reversible and
  auditable; `last_log()`, `soft_delete(id)`, `update_value(id, ...)`.
- `main.py` — route inbound through `commands.dispatch()` first; on a command, act and confirm; else
  parse as today.
- `llm/prompts.py` — optional: let the LLM disambiguate an edit target/value when the phrasing is
  fuzzy (still validated/clamped in code).

**Acceptance criteria.**
1. AC5.1: "undo last", "ยกเลิกอันล่าสุด", and `/undo` all soft-delete the most recent non-deleted log
   and confirm what was removed; running totals reflect the removal.
2. AC5.2: Undo with no prior entry today returns a friendly "nothing to undo" message, writes nothing.
3. AC5.3: An edit command ("make that 300ml" / "แก้เป็น 300 มล.") updates the last matching entry's
   value and re-confirms the new daily total.
4. AC5.4: A soft-deleted row is excluded from all aggregations (daily totals, weekly review) but
   remains in the table (auditable).
5. AC5.5: A normal habit message ("500ml", "ดื่มน้ำ 2 แก้ว") still routes to the parser unchanged —
   the command layer only intercepts recognized commands (no false positives on the smoke-test set).

**New dependencies.** None.

**Risks.** Command/parse disambiguation is the classic ambiguity source — keep the command matcher
conservative (explicit verbs only) so it never swallows a real log. Depends on v0.4 migrations for
`deleted_at`.

**⚑ NEEDS USER INPUT.** Should undo be hard-delete or soft-delete (recoverable)? Default: **soft**
(reversible, auditable, plays well with backups).

---

### v0.6.0 — Bilingual Output & Message Catalog

**Goal.** The parser handles Thai + English input, but **every outbound string is English-only**
(confirmations in `main.py`, `REMINDER_TEXTS`, the clarifying question, weekly-review labels in
`review.py`). Close the bilingual gap and centralize copy.

**User value.** "When I write in Thai, it answers in Thai. When I write in English, it answers in
English."

**Scope (files).**
- `core/i18n.py` (new) — a message catalog keyed by string id → `{en, th}`, with `.format()` params;
  a lightweight language detector (presence of Thai Unicode block, with a `config` override
  `language = "auto" | "th" | "en"`).
- `main.py` — replace inline confirmation strings with catalog lookups; track the language of the last
  inbound message to choose reply language in `auto` mode.
- `core/reminders.py` — `REMINDER_TEXTS` → catalog (reminders default to the configured/primary
  language since they're unprompted).
- `core/review.py` — weekly-review labels + the LLM narrative prompt localized (ask Qwen to write the
  narrative in the target language).

**Acceptance criteria.**
1. AC6.1: A Thai input ("ดื่มน้ำ 2 แก้ว") produces a Thai confirmation; an English input ("500ml")
   produces an English confirmation — same structured result, localized copy.
2. AC6.2: Every user-facing string in `main.py`, `reminders.py`, and `review.py` resolves through the
   catalog — a test asserts no hard-coded user-facing literal remains in those modules.
3. AC6.3: `language = "th"` / `"en"` forces all output to that language regardless of input; `"auto"`
   matches the input's detected language.
4. AC6.4: The weekly-review narrative is generated in the target language and stays factual
   (no medical advice — existing constraint preserved).
5. AC6.5: The detector classifies mixed Thai+English by presence of any Thai character → Thai, and
   pure-ASCII → English (deterministic, unit-tested).

**New dependencies.** None.

**Risks.** Reminder language for `auto` mode is ambiguous (nothing to detect from) — resolve via
config primary language. Catalog drift — enforce the "no literals" test in AC6.2.

**⚑ NEEDS USER INPUT.** Default reply language for **unprompted** reminders in `auto` mode. Default:
Thai (user's primary environment is `Asia/Bangkok`) — confirm.

---

### v0.7.0 — Multi-Habit Extensibility  *(the pivot)*

**Goal.** Generalize the hardcoded `water | stretch | diary` triad into **config-defined habits**.
Today the category set is baked into the parser enum, prompts, DB category strings, confirmations, and
reminders. Make habits data, not code.

**User value.** "I can add 'sleep hours', 'steps', 'meds taken' by editing config — the bot reminds,
parses, confirms, and reviews them just like water."

**Scope (files).**
- `config.toml` / `config.py` — a `[[habits]]` array: each habit has `id`, `type`
  (`numeric | duration | text | boolean`), `unit`, optional `goal`, `reminder_times`, bilingual
  `label`, and parse hints (e.g. water's glass/bottle constants become per-habit).
- `llm/ollama_client.py` + `llm/prompts.py` — build the extraction schema/enum and few-shot prompt
  **dynamically** from the habit list.
- `core/parser.py` — validate against the dynamic habit set; per-type value validation.
- `storage/db.py` + migration — `logs.category` now stores any habit id; add a `habit_type` or keep
  value in `value_num`/`value_text` by type. Existing water/stretch/diary rows map cleanly.
- `core/reminders.py` — schedule from each habit's `reminder_times`.
- `main.py` — confirmations become type-driven (numeric-with-goal → "X / goal (%)"; duration →
  "N min, Kth today"; text → reflection; boolean → "done").
- `core/review.py` — aggregate generically per habit.

**Acceptance criteria.**
1. AC7.1: With the default config (water/stretch/diary expressed as habits) behavior is
   byte-identical to v0.6.0 confirmations and reminders (no regression).
2. AC7.2: Adding a new `[[habits]]` entry (e.g. `sleep`, numeric, unit "h", goal 8) makes the bot
   parse "นอน 7 ชม.", store it, confirm against goal, and include it in the weekly review — with
   **zero code changes**.
3. AC7.3: The Ollama extraction schema/enum is generated from config; an input matching no configured
   habit → `unknown`.
4. AC7.4: Per-type validation holds: numeric/duration reject ≤0; boolean stores done/not-done; text
   requires non-empty (mirrors current `_validate` rules per type).
5. AC7.5: Migration maps all pre-v0.7 `logs` rows to the new representation with values intact;
   weekly review over old data is unchanged.

**New dependencies.** None.

**Risks.** This is the largest change — it touches parser, prompts, storage, reminders, review, and
confirmations at once. **Strongly SEQUENTIAL internally.** More configured habits = larger extraction
schema = more load on the (already schema-weak) MLX model → v0.2's fallback chain is a prerequisite,
which is why it ships first. Confirmation formats for arbitrary types need careful bilingual templates
(depends on v0.6).

**⚑ NEEDS USER INPUT.** (a) Which habits beyond water/stretch/diary does the user actually want at
launch? (b) Are the four types (numeric/duration/text/boolean) enough? Default: ship the existing
three generalized, document how to add more, don't invent habits.

---

### v0.8.0 — Natural-Language Queries

**Goal.** Let the user ask about their own data conversationally. Rides the v0.5 command seam as a new
"query" intent; reads aggregates the DB already computes.

**User value.** "how much water this week?" / "อาทิตย์นี้ยืดไปกี่ครั้ง" / "did I journal yesterday?"
answered instantly, in the user's language.

**Scope (files).**
- `core/commands.py` — detect query intent (bilingual question patterns + an LLM intent classifier
  that returns `{habit, metric, timeframe}` as structured JSON, validated in code).
- `core/query.py` (new) — map `{habit, metric, timeframe}` → a read-only DB aggregation
  (reuse `logs_between`, daily totals); format a bilingual answer via the v0.6 catalog.
- `storage/db.py` — add any missing read helpers (e.g. range sums per habit) — read-only.
- `llm/prompts.py` — a query-intent prompt.

**Acceptance criteria.**
1. AC8.1: "how much water this week?" returns the correct 7-day water sum computed from the DB
   (verified against seeded data), in English.
2. AC8.2: "อาทิตย์นี้ยืดกี่ครั้ง" returns the correct weekly stretch count, in Thai.
3. AC8.3: Timeframes "today / yesterday / this week / last 7 days" (and Thai equivalents) map to the
   correct date ranges (timezone-aware, `Asia/Bangkok`).
4. AC8.4: A query about an unconfigured habit or an unparseable question returns a friendly "I can't
   answer that yet" — never a wrong number, never a crash.
5. AC8.5: Query handling is strictly read-only — no `logs` row is written by a query.

**New dependencies.** None.

**Risks.** Intent-vs-log ambiguity ("500ml" is a log, "how much water?" is a query) — the command
router must classify before the extractor; keep the query matcher anchored on interrogatives. LLM
intent errors → always validate the returned `{habit, metric, timeframe}` against known values and
fail closed.

**⚑ NEEDS USER INPUT.** None. (Reasonable default timeframe vocabulary; extendable.)

---

### v0.9.0 — Adaptive Reminders, Snooze & Quiet Hours

**Goal.** Make reminders considerate. Skip a reminder whose goal is already met; honor do-not-disturb
windows; let the user snooze. Built generically over v0.7 habits (goal-aware).

**User value.** "Don't nag me for water at 20:30 if I already hit 2500 ml. Don't ping me during my
1pm nap. If I say 'snooze 30' / 'เลื่อน 30 นาที', ask me again later."

**Scope (files).**
- `core/reminders.py` — before sending, check the habit's goal state (read DB); skip if met (for
  goal-bearing habits). Add quiet-hours suppression.
- `config.toml` / `config.py` — `[quiet_hours] windows = [["13:00","14:00"], ["23:00","07:00"]]`;
  per-habit `skip_if_goal_met = true`.
- `core/commands.py` — snooze command (bilingual): reschedule a one-off reminder N minutes out.
- `main.py` — wire the one-off snooze job into the existing `AsyncIOScheduler`.

**Acceptance criteria.**
1. AC9.1: A water reminder scheduled when the daily goal is already met is **not** sent (logged as
   skipped); when the goal is not met, it *is* sent.
2. AC9.2: Any reminder whose fire time falls inside a quiet-hours window is suppressed (including
   windows that cross midnight).
3. AC9.3: "snooze 30" / "เลื่อน 30 นาที" schedules a single follow-up reminder ~30 min later for the
   relevant habit; it fires once and does not recur.
4. AC9.4: `skip_if_goal_met = false` for a habit disables adaptive skipping for that habit only.
5. AC9.5: Adaptive checks are read-only DB reads on the event loop; no scheduler job crashes if the
   DB read fails (fail-open: send the reminder rather than swallow it).

**New dependencies.** None.

**Risks.** Midnight-crossing windows and DST-free but TZ-correct comparisons need care (project is
fixed `Asia/Bangkok`, no DST — simpler). "Fail-open on read error" is a deliberate choice so a DB
hiccup never silences all reminders.

**⚑ NEEDS USER INPUT.** Default quiet-hours windows (or leave empty and let the user set them).
Default: empty (opt-in) so the bot's behavior doesn't change silently.

---

### v0.10.0 — Streaks, Gentle Gamification & Daily Summary

**Goal.** Add lightweight motivation and an end-of-day recap. Streaks already exist for stretch in the
weekly review — generalize to per-habit streaks and surface milestones gently; add a daily summary
message.

**User value.** "A short recap at night, a quiet '7-day water streak 🎉' when I earn it — encouraging,
never guilt-tripping, and I can turn it off."

**Scope (files).**
- `core/streaks.py` (new) — per-habit streak computation (goal-met days for goal habits; any-entry
  days otherwise); milestone thresholds (3/7/30…).
- `core/reminders.py` / `main.py` — a daily-summary scheduled job (e.g. 21:45, after diary): today's
  per-habit totals vs goal + active streaks, via the v0.6 catalog.
- `main.py` confirmations — optionally append a milestone line when a streak threshold is crossed.
- `config.toml` — `[gamification] enabled = true`, `milestones = [3,7,30]`, `daily_summary_time`.

**Acceptance criteria.**
1. AC10.1: A per-habit streak counts consecutive days meeting the habit's condition (goal-met for goal
   habits), verified against seeded data; a gap resets it.
2. AC10.2: Crossing a configured milestone appends exactly one encouragement line to the next
   confirmation (once per crossing, not repeated).
3. AC10.3: The daily summary fires at the configured time with correct per-habit totals/goal% and
   current streaks, in the user's language.
4. AC10.4: `gamification.enabled = false` suppresses all milestone lines and (optionally) the daily
   summary — no behavioral leakage.
5. AC10.5: Streak/summary computation is read-only and reuses v0.7 aggregation (no divergent math from
   the weekly review).

**New dependencies.** None.

**Risks.** Gamification tone is personal — some users dislike streak pressure; hence the opt-out and a
"gentle" copy register. Streak definition must match the weekly review's to avoid contradictory
numbers (share one function).

**⚑ NEEDS USER INPUT.** Tone/opt-in: gamification **on** or **off** by default, and milestone
thresholds. Default: on but gentle, `milestones = [3,7,30]`.

---

### v1.0.0 — Insights: Charts-as-Images + Garmin Import  *(declare stable)*

**Goal.** Turn the weekly review into something you *look at*, and close the spec's own §12 TODO:
join the user's Garmin hydration export against `water` logs. Then declare the config/schema/API
stable at 1.0.

**User value.** "My Sunday review comes with a water chart and a stretch chart, and it cross-checks my
self-reported water against what Garmin recorded — all offline."

**Scope (files).**
- `core/charts.py` (new) — render per-habit weekly PNGs (water vs goal bars, stretch counts) offline;
  return image bytes.
- `channels/base.py` — extend the ABC with `send_image(bytes, caption)` (default implementation:
  fall back to `send(caption)` so LINE/others aren't forced to implement it); `channels/telegram.py`
  implements it via `sendPhoto`. **Seam stays clean** — still no concrete channel imported in `core/`.
- `core/review.py` — attach chart images to the weekly review; **remove the Garmin `TODO`** and add
  `garmin.py`.
- `core/garmin.py` (new) — parse a Garmin hydration CSV (stdlib `csv`), join by date against `water`
  logs, report agreement/discrepancy in the review. File path + column mapping from config; the CSV is
  read locally, never uploaded.
- `config.toml` — `[charts] enabled`, `[garmin] csv_path`, `column_map`.

**Acceptance criteria.**
1. AC1.0.1: The weekly review sends a water chart image (and a stretch chart) with a caption; when
   `charts.enabled = false` or rendering fails, it falls back to the text review (no crash).
2. AC1.0.2: `send_image` on `TelegramChannel` posts via `sendPhoto`; a channel without an image
   implementation degrades to a text send — verified without touching `core/`.
3. AC1.0.3: A sample Garmin hydration CSV is parsed and joined by date against `water` logs; the review
   reports per-day self-reported vs Garmin totals and flags discrepancies beyond a threshold.
4. AC1.0.4: A missing/malformed Garmin CSV is handled gracefully — the review still sends, noting
   Garmin data was unavailable.
5. AC1.0.5: All chart rendering and CSV parsing happen locally; no new outbound host is contacted
   (charts are bytes over the existing Telegram send).

**New dependencies.** **`matplotlib`** (charts) — the one new dependency in the whole roadmap.
Justification: it's the standard offline PNG chart renderer, produces images locally with no network,
and there's no lighter option that yields legible charts. **Mitigation for the minimal-deps
constraint:** gate it behind `[charts] enabled` and keep a text-only fallback, so the dep is optional.
Garmin import uses stdlib `csv` — no dep.

**Risks.** `matplotlib` pulls `numpy` (heavier install) — acceptable given it's the sole new dep and
optional. Garmin export format varies by locale/device — column mapping must be configurable and a
real sample is needed.

**⚑ NEEDS USER INPUT.** (a) OK to add `matplotlib` (optional, chart-only)? Default: yes, gated.
(b) A **sample Garmin hydration CSV** (headers + a few rows) and its units — needed to fix the column
map. Default assumption: a `Date, Hydration(ml)`-style export; confirm.

---

## 3. Ordering rationale, independence & parallelization

**Why this sequence — foundations before features.**
- **v0.2 → v0.3 (reliability first).** The MVP's two biggest known risks are *wrong data* (MLX schema
  gap, `IMPL.md`'s load-bearing finding) and *unattended fragility*. Fix trust in the data, then trust
  in uptime, before adding surface area.
- **v0.4 (migrations) before anything that changes schema.** Undo (v0.5 `deleted_at`), the v0.3
  deferral persistence, and multi-habit (v0.7) all need safe schema evolution. Build it once.
- **v0.5 (command seam) before v0.8/v0.9 commands.** NL queries, snooze, and `/status`/`/backup` all
  dispatch through the same router — establish it once, conservatively.
- **v0.6 (bilingual output) before v0.7 (multi-habit).** Multi-habit generates lots of new
  user-facing copy; the catalog pattern must exist first so every habit carries bilingual labels.
- **v0.7 (multi-habit) is the pivot.** Placing it here means v0.8–v1.0 (queries, adaptive reminders,
  streaks, charts) are each built **generically over habits, once** — instead of hard-coding
  water/stretch/diary logic and refactoring it later. The cost is that v0.7 depends on migrations
  (v0.4) and the fallback chain (v0.2, because a bigger dynamic schema stresses the weak model more) —
  both already shipped by then.
- **v0.8 → v1.0 build on the pivot** and increase in scope/visibility, ending at charts + Garmin as a
  natural "look at my data" capstone worthy of 1.0.

**Independent enough to reorder or parallelize** (disjoint files, no data dependency):
- **v0.3 (Resilience) ⟂ v0.4 (Migrations)** — different modules. Recommended tweak: if the team wants
  v0.3's deferral queue *persisted* rather than in-memory, run **v0.4 before v0.3**. Otherwise they're
  independent and could run in parallel tracks.
- **v0.6 (Bilingual output)** touches `main.py`/`reminders.py`/`review.py` copy only — it can move
  earlier and run alongside v0.3/v0.4, as long as it lands **before v0.7**.
- **v0.8 (NL queries)** and **v0.9 (Adaptive reminders)** are mutually independent (query.py vs
  reminders.py) — order between them is free; both depend on v0.5 (+ v0.7 for genericity).

**Must stay sequential** (real data dependencies): v0.2 before v0.7 (model robustness) · v0.4 before
v0.5/v0.7 (schema) · v0.5 before v0.8/v0.9 (command seam) · v0.6 before v0.7 (copy catalog) · v0.7
before v0.8/v0.9/v0.10/v1.0 (habit generality) · v0.10/v1.0 reuse v0.7 aggregation.

**Internal parallelism.** Every version except **v0.7** is small enough to be a SEQUENTIAL one-Luna
build. **v0.7 (multi-habit)** is the one candidate for an internal PARALLEL split once its shared
surface (the `[[habits]]` config model + dynamic schema builder + migration) is built first — then
parser, reminders, review, and confirmations can fan out. Sophia should re-spec v0.7 with a full
§11 module split when it's picked up.

---

## 4. Consolidated open questions for the user

**Blocking a specific version (resolve before that version starts):**
1. **(v0.2)** Which schema-conformant fallback model to `ollama pull` on the mac-mini?
   *Default:* `qwen3:8b` (IMPL-confirmed). Confirm it's installed.
2. **(v0.3)** Outage alerting: Telegram push + local log enough, or add a secondary local
   notification? *Default:* Telegram + log only.
3. **(v0.5)** Undo = soft-delete (recoverable) or hard-delete? *Default:* soft-delete.
4. **(v0.6)** Reply language for **unprompted** reminders in `auto` mode? *Default:* Thai.
5. **(v0.7)** Which habits beyond water/stretch/diary do you want at launch, and are the four value
   types (numeric/duration/text/boolean) enough? *Default:* ship the three generalized; add none
   speculatively.
6. **(v0.9)** Default quiet-hours windows? *Default:* none (opt-in).
7. **(v0.10)** Gamification default on or off, and milestone thresholds? *Default:* on-but-gentle,
   `[3,7,30]`.
8. **(v1.0)** OK to add optional `matplotlib` for charts? And please share a **sample Garmin hydration
   CSV** (headers + a few rows, with units). *Default:* matplotlib gated behind `[charts] enabled`;
   Garmin column map assumed `Date, Hydration(ml)` until a sample arrives.

**Deferred-feature decisions (not in the ten — needed only if you want them slotted in):**
9. **LINE channel.** Reshaped, not rejected: the `Channel` seam is ready and `line.py` already
   documents the design. It's **deferred** because inbound LINE needs a **public HTTPS webhook**
   (static IP + TLS, or a Cloudflare/ngrok tunnel) — which introduces an inbound-from-cloud path that
   sits in tension with the local-first posture, and is an infra/security decision, not a code one.
   *Question:* Do you want LINE, and can you provide a public endpoint (or accept a tunnel)? If yes,
   it becomes a self-contained version (send via push API + a minimal signed-webhook receiver) that
   changes **no** `core/` code.
10. **Voice-message transcription.** Reshaped, not rejected. **Feasibility:** Ollama does **not** do
    speech-to-text, so this needs a *second* local service (e.g. `faster-whisper` / `whisper.cpp`) —
    a new heavyweight dependency + model download + extra RAM on the mac-mini, and arguably a second
    process (violating the single-process default). It stays local (no cloud), so it's constraint-
    compatible, just costly. *Question:* Is a second local STT service acceptable? If yes, it's a
    self-contained version: download the Telegram voice file → local transcribe → feed the transcript
    into the existing parse path.

**Carryover from `PROGRESS.md`:**
11. **Git remote** — still unanswered: local-only, or create + push to a remote? (Affects release
    workflow from v0.2 onward.)
