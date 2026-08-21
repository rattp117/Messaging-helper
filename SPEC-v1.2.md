# Spec — v1.2.0: Multi-user support ("one bot, many people")

## 1. Problem statement
Today the assistant is single-tenant: `TelegramChannel` pins one `chat_id` for every send, every `logs`/
`habit_targets` row is unscoped, streaks/targets/reminders/summaries all assume a single person, and the
owner's identity lives only in `.env`. v1.2.0 makes `@rattapon_bot` serve many people from one process:
anyone the owner authorizes gets a **fully independent experience** — their own logs, streaks, milestones,
per-habit targets, reminders (**including their own reminder times**), snooze, quiet-hours, daily summary,
weekly review (+charts), language preference, and undo — partitioned by **Telegram chat ID**. Strangers who
find the bot are politely refused
until the owner approves them. The **habit catalog stays global** (everyone tracks the same `[[habits]]`).
The current single user (the chat ID in `.env`) becomes the **owner**, their existing data is attributed to
their identity on first startup, and their experience is **byte-identical** to v1.1 (AC-M3). Success: a
second authorized person can talk to the same bot and never sees, affects, or is affected by anyone else's
data; the owner notices no change.

SemVer: **1.2.0 (MINOR)** — no owner-visible behavior change; the internal schema **breaks the additive-only
guarantee** (habit_targets is rebuilt with a composite key) — flagged here and to be stated in the release
notes. `--backup` runs pre-release and a backup already exists.

## 2. Inputs

### 2.1 Inbound update now carries a chat identity
Every Telegram update is attributed to a **user = the chat ID it came from** (`message.chat.id`, or
`callback_query.message.chat.id` for a button tap), as a string. The channel threads this identity into the
handler; no core code may assume a single global user.

```
update.message.chat.id              → "1574572064"  (owner)  /  "88899900" (a member)
update.callback_query.message.chat.id + .data → ("88899900", "undo:1234")
update.message.from.first_name       → optional display name captured at onboarding
```

### 2.2 Owner identity
`secrets.telegram_chat_id` (from `.env`, unchanged) is the **owner**. It is the single source of truth for
"who owns pre-v1.2 data" and "who may run admin commands". Available only at `async_main` (after
`load_secrets`) — **not** inside the migration runner (which only receives a `sqlite3.Connection`). This
constraint shapes the migration/attribution split in §4 R-M2.

### 2.3 New commands (deterministic, LLM-free, anchored — same discipline as `/undo`/`/target`)
```
/start                       # onboarding — anyone (unknown → request access; active → welcome)
/approve <chat_id>           # owner-only — grant a pending/blocked user access
/block <chat_id>             # owner-only — revoke access
/users                       # owner-only — list users + role/status
/invite <chat_id>            # owner-only — pre-authorize a chat id before they message  (alias of /approve)
/lang en | th | auto         # per-user language preference       (Thai alias: ภาษา)
/quiet HH:MM-HH:MM[,…] | off  # per-user quiet-hours windows        (Thai alias: เงียบ)
/remind <habit> 08:00 12:00  # per-user reminder times for a habit (Thai alias: เตือน)
/remind <habit>              # show my effective times for that habit (custom | default | off)
/remind <habit> default      # clear my override → fall back to the global config times (also: reset | clear)
/remind <habit> off          # explicitly no reminders for that habit, for me
```

### 2.4 Existing data
`logs` (v1.1 shape, up to migration 005) and `habit_targets` (PK `habit_id`) with the owner's history. All
of it must attribute to the owner and keep working unchanged (AC-M2/AC-M3).

## 3. Outputs

### 3.1 Per-user, per-chat sends
Every confirmation, reminder, summary, review, chart, undo, target reply, and query answer is delivered to
the **acting user's chat**, computed from the inbound update (interactive) or from the active-user fan-out
(scheduled). No send is addressed to a pinned global chat except operator/health alerts (owner only, §4 R-O1).

### 3.2 Onboarding / access replies (bilingual)
```
# unknown user, first contact:
👋 Hi! This is a private habit bot. I've asked the owner to approve you — you'll hear back soon.
# (owner simultaneously receives:)
🔔 <name> (chat 88899900) asked for access. Approve with: /approve 88899900

# after /approve 88899900 (the approved user receives):
✅ You're in! Just type things like "500ml" or "10 min stretch". Send /help to see everything.

# blocked / not-yet-approved user messaging again:
⏳ You're not approved to use this bot yet.

# non-owner attempting an admin command:
🤔 (falls through as an unknown command — admin commands are owner-only and invisible to others)
```

### 3.3 Owner `/users`
```
👥 Users:
• 1574572064 — owner · active · lang auto
• 88899900 — member · active · lang th
• 55544433 — member · pending
```

### 3.4 Everything else unchanged in shape
Log confirmations, `/help`, `/habits`, `/target`, streak lines, summaries, reviews, charts render exactly as
v1.1 — only **scoped to the acting user** and delivered to their chat. `/habits` and `/target` show that
user's own targets/totals; `/lang`/`/quiet` acknowledge the new preference.

### 3.5 Error responses
No new tracebacks reach any user. An admin command with a malformed/unknown chat id → a friendly usage
message. A callback whose log is not owned by the tapping chat → refused politely, spinner still dismissed
(§4 R-C3). Every access decision is fail-safe: on any lookup error the user is treated as **not active**
(deny) rather than accidentally granted.

## 4. Behavior rules

### Identity, migration & attribution (shared surface)

- **R-M1** Migration **006** is additive schema for `logs` and adds the `users` table, but **rebuilds
  `habit_targets`** to be per-user — this is the sanctioned break of the additive-only guarantee (release
  notes). Specifically (all inside the version-006 transaction):
  - `CREATE TABLE users (chat_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'member', status TEXT NOT
    NULL DEFAULT 'pending', display_name TEXT, language_pref TEXT NOT NULL DEFAULT 'auto',
    quiet_hours_json TEXT, snooze_default_minutes INTEGER, created_at TEXT NOT NULL DEFAULT
    (datetime('now','localtime')))`.
  - `ALTER TABLE logs ADD COLUMN user_id TEXT NULL` + `CREATE INDEX idx_logs_user ON logs(user_id, category, ts)`.
  - Rebuild `habit_targets` → `(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, habit_id TEXT NOT NULL,
    goal REAL NOT NULL, updated_at TEXT, UNIQUE(user_id, habit_id))`; copy every existing row across with
    `user_id = NULL` (the migration cannot know the owner — R-M2 fills it). `UNIQUE(user_id, habit_id)`
    tolerates the transient NULLs (SQLite treats NULLs as distinct in a UNIQUE index).
  - `CREATE TABLE user_reminder_times (user_id TEXT NOT NULL, habit_id TEXT NOT NULL, time TEXT NOT NULL,
    PRIMARY KEY (user_id, habit_id, time))` — the per-user reminder-times store (R-S4). Empty at creation:
    every user (owner included) falls back to the global config times until they customize, so this table
    being empty preserves v1.1 reminder behavior (AC-M3).
  - Re-running 006 is a no-op (idempotent, guarded by `user_version`).
- **R-M2** **Startup attribution** (`db.attribute_legacy_to_owner(owner_chat_id)`, called once in
  `async_main` right after `load_secrets`, identity-aware, **idempotent**): (a) upsert the owner's `users`
  row as `role='owner', status='active'` (never downgrade an existing owner row); (b)
  `UPDATE logs SET user_id = :owner WHERE user_id IS NULL`; (c) `UPDATE habit_targets SET user_id = :owner
  WHERE user_id IS NULL`. After the first run no NULL rows remain, so subsequent runs change nothing. This
  is where "all existing rows attribute to the owner" happens — deliberately **outside** the migration,
  because the migration has no access to `.env` (§2.2).
- **R-M3** (owner unchanged — regression guard) After migrate + attribute, for the owner acting on their
  existing data, every v1.1 output (confirmations, undo, streaks, milestones, targets, daily summary,
  weekly review + charts, `/habits`, `/help`) is **byte-identical** to v1.1. The whole v1.1 test suite,
  re-run with the owner as the acting user, stays green.

### Access control & onboarding (module `access`)

- **R-A1** Every inbound update is gated **before** any logging/LLM/command work by
  `access.classify(db, chat_id) -> owner|active|pending|blocked|unknown` (owner ⊂ active). Only `active`
  users reach the normal pipeline (§4 R-C*). Fail-safe: a lookup error classifies as **not active** (deny).
- **R-A2** An `unknown` chat: create a `users` row `status='pending'` (capturing `display_name` when the
  update provides one), reply `access_pending` to them, and send `access_request` to the **owner** (naming
  the chat id + how to approve). The triggering message is **not logged**, and **no LLM call** is made.
- **R-A3** A `pending` or `blocked` chat: reply `access_pending` / `access_denied` respectively; do not
  process further. (`blocked` may be configured to silently ignore — default: polite `access_denied`.)
- **R-A4** Owner-only admin commands (`/approve`, `/block`, `/users`, `/invite`) are recognized by
  `commands.dispatch` (new anchored kinds) but **only executed when the acting user's role is `owner`**;
  for anyone else they fall through as an unknown message (invisible — no "permission denied" that reveals
  the command exists). `/approve <id>`/`/invite <id>` set that user `active` (creating the row if needed)
  and send `access_granted` to the approved chat; `/block <id>` sets `blocked`; `/users` lists all users.
- **R-A5** `/start`: for an `active` user, reply a welcome/intro (points at `/help`); for `unknown`, run the
  R-A2 pending flow; for `pending`/`blocked`, the R-A3 reply. `/start` is the one message an unknown user
  may send that produces a friendly onboarding reply rather than silence.

### Channel layer & per-chat threading (shared surface)

- **R-C1** The `Channel` ABC sends become **per-recipient**: `send(chat_id, text)`,
  `send_image(chat_id, image, caption)`, `send_actionable(chat_id, text, buttons)`. `TelegramChannel` puts
  `chat_id` in each Bot API payload (replacing the pinned `self._chat_id`, now kept only as
  `owner_chat_id` for defaulting/health). `set_my_commands` stays global; `answer_callback_query` unchanged.
  `line.py` stub and every test fake update to the new signatures. The "no concrete-channel import in
  core/" seam is preserved.
- **R-C2** `Channel.run(on_message, on_callback)` extracts the chat id from each update and passes it first:
  `on_message(chat_id, text)` and `on_callback(chat_id, data, source_text, callback_id)`. `main.py`'s
  handlers thread `chat_id` as the acting `user_id` through `handle_inbound_message` and the undo callback.
- **R-C3** (per-chat callback ownership) The undo button's `callback_data` stays `undo:<log_id>`, but
  `handle_undo_callback` now **verifies `db.get_log(log_id).user_id == tapping chat_id`** before soft-
  deleting. A tap on a log the chat does not own → no delete, a polite `already_undone`/not-found reply, and
  the spinner is still dismissed (the loop's `answer_callback_query` is unchanged). A user can never undo
  another user's entry.

### Data scoping (shared surface)

- **R-D1** `LogEntry` gains `user_id: str`. `db.insert_log` writes it. Every read/aggregation/mutation
  method gains a leading `user_id` (or, for row-addressed ops, enforces ownership): `sum_value`, `count`,
  `count_true`, `water_total_ml`, `stretch_count`, `diary_count`, `logs_between`, `last_log`,
  `get_target`, `set_target`, `clear_target`, `all_targets` all filter by `user_id`. `soft_delete`/
  `update_value`/`get_log` are keyed by `log_id` but callers must own-check via `get_log`.
  `pending_unparsed()` stays **global** (returns deferred rows for all users) but each row now carries
  `user_id`, so the recovery re-parse addresses each confirmation to the right chat (R-D3).
- **R-D2** `habit_targets` is per-user: `get_target(user_id, habit_id)`, `set_target(user_id, habit_id,
  goal)` (upsert `ON CONFLICT(user_id, habit_id)`), `clear_target(user_id, habit_id)`,
  `all_targets(user_id)`. `targets.effective_goal(db, habit, config, user_id)` reads the acting user's
  override, else the config base (unchanged resolution). Every current caller of `effective_goal`
  (streaks, review, charts, reminders, main confirmations/undo/edit, discoverability) threads `user_id`.
- **R-D3** Every core aggregation/formatter threads `user_id`: `streaks.day_qualifies/compute_streak/
  crossed_milestone/compute_daily_summary/run_daily_summary`; `review.compute_weekly_stats/run_weekly_review/
  render_weekly_review_charts`; `charts.render_habit_chart/render_weekly_charts`; `query.answer_question`;
  `discoverability.build_habits_overview`; `undo_ui.send_undo_confirmation/handle_undo_callback`;
  `targets_command.execute_target`. Each reads/writes only the acting user's rows.
- **R-D4** (isolation invariant — one shared database, strictly partitioned) All users share **one SQLite
  database** (`data/habits.db`); users are **never** split into separate files or tables-per-user. Instead,
  **every user-owned row carries a `user_id` (the chat ID)** and **every read/aggregate/mutate is filtered
  by the acting user's `user_id`** — logs, targets, streak/summary/review aggregations, reminder-times,
  quiet-hours, language, snooze state, and undo. For any two active users A ≠ B, no operation by A ever
  reads, counts, shows, or writes B's rows, and vice versa; A's confirmations/summaries/reviews/charts/
  `/habits`/queries reflect **only** A's own data, delivered **only** to A's chat. This is the single most
  important property (AC-U-ISO). Enforcement is structural, not by convention:
  - No scoped `db` method may run a `logs`/`habit_targets`/`user_reminder_times` query without a `user_id`
    (or, for row-addressed ops like undo, an ownership check via `get_log(...).user_id`, R-C3).
  - Every core aggregation/formatter/scheduler-fanout call threads the acting `user_id` (R-D2/R-D3/R-S*).
  - Verified by tests that seed two users with overlapping habit ids on the same DB and assert
    cross-visibility is exactly zero across every surface (logs total, streak, milestone, target, daily
    summary, weekly review, chart data, `/habits`, query answer, undo, reminder firing).

### Scheduling (shared surface) — per-user reminder times (OQ2 answered: per-user schedules)

- **R-S1** (reminder scheduling redesign) Per-habit reminders fire from a single **minutely tick** job, not a
  fixed cron-per-config-time. One `CronTrigger(second=0)` job (`coalesce=True, max_instances=1,
  misfire_grace_time` small) runs `reminders.run_due_reminders(...)` every minute: it computes the current
  wall-clock `HH:MM` in `config.app.timezone` (global tz, R-S5) and, for each `db.active_user_ids()` × each
  habit in the registry, sends that habit's reminder to that user **iff** the current minute is in the
  user's *effective reminder times* for that habit (R-S4). Chosen over per-`(user,habit,time)` cron jobs
  because it needs **no scheduler rebuild** when a user runs `/remind` — the next tick reads the store live
  (R-S6) — and no per-user job lifecycle on approve/block. `coalesce`+small grace prevent a paused/restarted
  process from replaying a missed minute and double-sending. **Owner unchanged:** with an empty
  `user_reminder_times`, every user (owner included) falls back to the global config times, so the owner's
  reminders fire at exactly the same minutes as v1.1 (AC-M3 holds until they customize).
- **R-S2** `send_reminder(channel, chat_id, habit, language, db, config, state)` addresses `channel.send
  (chat_id, …)`; the goal-met skip uses `effective_goal(db, habit, config, chat_id)` + `sum_value(chat_id,
  …)`; quiet-hours uses that user's effective windows (R-P2). Both suppression checks (quiet-hours,
  goal-met) run inside `send_reminder`, so a **custom-time** reminder is suppressed under the same rules as a
  config-time one (AC-S6). `ReminderState.last_habit_id` becomes a **per-user map** (`dict[chat_id,
  habit_id]`); snooze reads the snoozing user's entry and reschedules a one-off addressed to their chat —
  works against a custom-time-fired habit and never affects another user (AC-U-SNOOZE/AC-S6).
- **R-S3** Daily summary and weekly review + charts keep their **single global time** from config
  (`gamification.daily_summary_time`, `weekly_review`) and fan out to active users; a user with **no logs in
  the window is skipped** (nothing to summarize/review) so brand-new users aren't sent empty recaps. (Only
  per-*habit reminder* times are per-user in v1.2; summary/review times stay global — §10.)
- **R-S4** (effective reminder times) `reminders.effective_reminder_times(db, config, habit, user_id) ->
  list[str]`: read `db.get_reminder_times(user_id, habit.id)` and resolve — **no rows** → the global
  `habit.reminder_times` (fallback/default); rows equal to the single sentinel `["off"]` → `[]` (explicitly
  no reminders for that user+habit); otherwise → the stored `HH:MM` list (custom), sorted and de-duplicated.
  This is the one resolver the tick (R-S1) and the `/remind` show/set paths (R-S5) both consult, so they can
  never diverge.
- **R-S5** (`/remind` setter — module `schedules`) `schedules.execute_remind(command, *, db, config,
  registry, lang) -> str` performs, for a valid goal/registry habit id:
  - **set** `/remind <habit> 08:00 12:00 …`: each token must match `^([01]\d|2[0-3]):[0-5]\d$` (reuse the
    config `_HHMM_RE`); reject any invalid token with `remind_invalid_time` (no write); de-dupe; enforce a
    sane cap (≤ 24 times); then `db.set_reminder_times(user_id, habit_id, times)` (delete-then-insert).
    Reply `remind_set` listing the new times. Times are wall-clock in the global `config.app.timezone`.
  - **off** `/remind <habit> off`: store the `["off"]` sentinel; reply `remind_off`.
  - **default** `/remind <habit> default|reset|clear`: `db.clear_reminder_times(user_id, habit_id)` (delete
    all rows) → fall back to config; reply `remind_cleared` naming the config times.
  - **show** `/remind <habit>` (no times): reply `remind_show` with the effective times + their source
    (custom / default / off).
  - unknown/invalid habit → `remind_invalid_habit`; a non-reminderable habit (none in the shipped catalog,
    but a `text` habit with no config `reminder_times` is allowed to gain custom ones) → still settable.
    Full natural-language phrasing for `/remind` is **deferred** (§10) — the deterministic command + Thai
    alias `เตือน` is the v1.2 surface.
- **R-S6** (no restart) A `/remind` set/off/default takes effect **without restarting the process and without
  rebuilding any scheduler job** — the minutely tick (R-S1) reads `user_reminder_times` live on its next
  run, so the change is reflected within one minute (AC-S4).

### Preferences (module `preferences`)

- **R-P1** Per-user **language**: `users.language_pref ∈ {auto, th, en}` (default `auto`). Reply-language
  resolution consults the acting user's pref instead of the global `config.i18n.language`; `auto` matches
  the inbound message (interactive) or the user's implied primary (unprompted → `config.i18n.
  primary_language`, default Thai). `/lang en|th|auto` (Thai alias `ภาษา`) sets it; owner default stays
  `auto` (so the owner is unaffected — R-M3).
- **R-P2** Per-user **quiet hours**: `users.quiet_hours_json` (NULL = inherit `config.quiet_hours.windows`).
  Reminder/summary/review fan-out evaluates the acting user's effective windows. `/quiet HH:MM-HH:MM[,…]`
  sets them, `/quiet off` clears to "no quiet hours for me" (an explicit empty list, distinct from NULL-
  inherit). Snooze default likewise per-user (`users.snooze_default_minutes`, NULL = inherit
  `config.snooze.default_minutes`).

### Operations & LLM load

- **R-O1** Health/ops alerts (`HealthMonitor`) are delivered to the **owner only** (operator concern) — a
  member never receives Ollama/Telegram down alerts. `HealthMonitor` takes `owner_chat_id` and sends there.
- **R-O2** (shared Ollama concurrency — a note, not a new mechanism) The inbound loop awaits `on_message`
  **one update at a time**, so no two extraction/query/target-NL calls to the single remote Ollama run
  concurrently from the inbound path; user B's message naturally waits behind user A's LLM call. Fan-out
  jobs likewise loop sequentially. No new queue/pool is added in v1.2; if the allowlist grows large enough
  that head-of-line latency hurts, a bounded worker pool is a future change (OQ4).

## 5. Interfaces (signatures)

```python
# channels/base.py  (ABC — per-recipient sends; run threads chat_id)
class Channel(ABC):
    async def send(self, chat_id: str, text: str) -> None: ...
    async def send_image(self, chat_id: str, image: bytes, caption: str) -> None: ...
    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None: ...
    async def set_my_commands(self, commands: dict[str, list[tuple[str, str]]]) -> None: ...
    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None: ...
    async def run(
        self,
        on_message: Callable[[str, str], Awaitable[None]],                 # (chat_id, text)
        on_callback: Callable[[str, str, str, str], Awaitable[None]] | None = None,  # (chat_id, data, source_text, cb_id)
    ) -> None: ...

# storage/models.py
@dataclass(slots=True)
class LogEntry:
    id: int | None
    user_id: str            # NEW — the owning chat id
    ts: str
    category: str
    value_num: float | None
    value_text: str | None
    raw_message: str
    source: str = "reply"
    created_at: str | None = None
    habit_type: str | None = None

# storage/migrations.py
def _migration_006_multiuser(conn: sqlite3.Connection) -> None: ...   # appended to MIGRATIONS

# storage/db.py  (representative — user_id added throughout)
def attribute_legacy_to_owner(self, owner_chat_id: str) -> None: ...
def sum_value(self, user_id: str, habit_id: str, day: str) -> float: ...
def last_log(self, user_id: str, category: str | None = None) -> sqlite3.Row | None: ...
def get_log(self, log_id: int) -> sqlite3.Row | None: ...            # row carries user_id for own-checks
def get_target(self, user_id: str, habit_id: str) -> float | None: ...
def set_target(self, user_id: str, habit_id: str, goal: float) -> None: ...
# users table
def get_user(self, chat_id: str) -> sqlite3.Row | None: ...
def upsert_user(self, chat_id: str, *, role: str | None = None, status: str | None = None,
                display_name: str | None = None) -> None: ...
def set_user_language(self, chat_id: str, pref: str) -> None: ...
def set_user_quiet_hours(self, chat_id: str, windows_json: str | None) -> None: ...
def list_users(self) -> list[sqlite3.Row]: ...
def active_user_ids(self) -> list[str]: ...
# per-user reminder times (R-S4/R-S5)
def get_reminder_times(self, user_id: str, habit_id: str) -> list[str]: ...   # [] = none stored; ["off"] = explicit off
def set_reminder_times(self, user_id: str, habit_id: str, times: list[str]) -> None: ...  # delete-then-insert
def clear_reminder_times(self, user_id: str, habit_id: str) -> None: ...       # delete all → config fallback

# core/targets.py
def effective_goal(db: Database, habit: Habit, config: Config, user_id: str) -> float | None: ...

# core/access.py  (NEW — module `access`)
Access = Literal["owner", "active", "pending", "blocked", "unknown"]
def classify(db: Database, chat_id: str) -> Access: ...
async def handle_gate(db, channel, config, owner_chat_id, chat_id, display_name, text, *, lang) -> bool:
    """Returns True if the caller is active (proceed to the normal pipeline); otherwise handles the
    onboarding/refusal reply itself and returns False."""
async def execute_admin(command, *, db, channel, config, owner_chat_id, chat_id, lang) -> None: ...

# core/reminders.py  (per-user + tick)
@dataclass
class ReminderState:
    last_habit_id: dict[str, str] = field(default_factory=dict)   # chat_id -> habit id
async def send_reminder(channel, chat_id: str, habit, language, db=None, config=None, state=None) -> None: ...
def effective_quiet_windows(db, config, chat_id) -> list[tuple[str, str]]: ...
def effective_reminder_times(db, config, habit, user_id: str) -> list[str]: ...   # R-S4
async def run_due_reminders(channel, config, registry, db, state, clock=datetime.now) -> None:
    """The minutely tick (R-S1): for the current HH:MM (config tz), send each active user each habit whose
    effective times include this minute. Replaces the per-config-time cron fan-out of schedule_reminders."""

# core/schedules.py  (NEW — module `schedules`)
async def execute_remind(command, *, db, config, registry, lang) -> str: ...   # set/show/default/off, validation (R-S5)

# core/commands.py  (extend)
CommandKind = Literal[..., "start", "approve", "block", "users", "invite", "lang", "quiet", "remind"]
# Command gains: target_chat: str | None (admin ops), pref_value: str | None (/lang, /quiet),
#                times: list[str] | None (/remind — [] show, ["off"] off, ["default"] reset, else HH:MM list)

# core/i18n.py
def resolve_reply_language(inbound_text, config, user_pref: str = "auto") -> Language: ...
def resolve_unprompted_language(config, user_pref: str = "auto") -> Language: ...
```

## 6. Files to touch

**Shared surface (built first, sequentially):**
- `storage/migrations.py` — migration 006 (users table, `logs.user_id`, `habit_targets` rebuild, **`user_reminder_times` table**).
- `storage/db.py` — `user_id` on every method; users/preferences methods; **reminder-times methods**; `attribute_legacy_to_owner`.
- `storage/models.py` — `LogEntry.user_id`.
- `channels/base.py`, `channels/telegram.py`, `channels/line.py` — per-recipient sends; `run` threads chat id.
- `core/targets.py` — `effective_goal(..., user_id)`.
- `core/streaks.py`, `core/review.py`, `core/charts.py`, `core/query.py`, `core/undo_ui.py`,
  `core/targets_command.py`, `core/discoverability.py` — thread `user_id`.
- `core/reminders.py` — per-user `ReminderState`, per-user `send_reminder`, effective quiet windows,
  **`effective_reminder_times` resolver + `run_due_reminders` minutely tick** (replaces the per-config-time
  cron fan-out of `schedule_reminders`).
- `core/health.py` — send alerts to `owner_chat_id`.
- `core/i18n.py` — per-user language resolution.
- `main.py` — `on_message(chat_id, …)`/`on_callback(chat_id, …)`; call `attribute_legacy_to_owner`;
  wire the access gate; **register the minutely reminder tick + the (global-time) summary/review fan-out
  jobs**; address every send by chat id; owner-only admin routing.
- `config.toml` — commented note documenting multi-user + owner-approval + `/lang`/`/quiet`/`/remind`.
- All existing test fakes/fixtures (`tests/test_*`) — new channel signatures + `user_id` in seeded rows.

**Module `access` (parallel, after shared surface):**
- `core/access.py` — NEW: `classify`, `handle_gate`, `execute_admin`, onboarding/pending/notify flow.
- `core/commands.py` — admin/onboarding kinds (`start`/`approve`/`block`/`users`/`invite`) + role gate.
- `core/i18n.py` — access/onboarding copy (EN+TH) *(shared file; access owns these keys)*.
- `tests/test_access.py` — NEW.

**Module `preferences` (parallel, after shared surface):**
- `core/commands.py` — `lang`/`quiet` kinds + parsing *(shared file; disjoint keys from `access`/`schedules`)*.
- `core/preferences.py` — NEW: `/lang`, `/quiet` execution (writes `users` prefs).
- `core/i18n.py` — preference-command copy (EN+TH) *(shared file; preferences owns these keys)*.
- `tests/test_preferences.py` — NEW.

**Module `schedules` (parallel, after shared surface):**
- `core/commands.py` — `remind` kind + parsing (habit + `HH:MM…`/`off`/`default`/show) *(shared file; disjoint keys)*.
- `core/schedules.py` — NEW: `execute_remind` (set/show/default/off + times validation, R-S5).
- `core/i18n.py` — reminder-command copy (EN+TH) *(shared file; schedules owns these keys)*.
- `tests/test_schedules.py` — NEW.

*(Note: `core/commands.py` and `core/i18n.py` are touched by all three parallel modules. The command-kind
enum + `Command` field additions and the i18n key-block skeletons are added in the shared surface first;
each module then fills only its own disjoint kinds/keys — see §11.)*

## 7. External dependencies
None new. Same stack: Python 3.11+, stdlib `sqlite3` (WAL), `httpx`, APScheduler, pydantic-settings,
matplotlib (optional, charts). Telegram Bot API methods already in use (`sendMessage`/`sendPhoto`/
`setMyCommands`/`answerCallbackQuery`); multi-user only changes the `chat_id` per call. One remote Ollama,
shared (R-O2).

## 8. Acceptance criteria

### Migration & owner-unchanged
- **AC-M1**: Given a v1.1 DB at `user_version=5`, When it opens, Then migration 006 creates `users`, adds `logs.user_id` + index, and rebuilds `habit_targets` to `UNIQUE(user_id, habit_id)`; existing `logs`/`habit_targets` values are preserved; re-opening applies nothing (idempotent, stamps 6). (R-M1)
- **AC-M2**: Given migration 006 applied and the owner chat id from `.env`, When `attribute_legacy_to_owner` runs at startup, Then the owner `users` row exists (`role=owner, status=active`) and every previously-NULL `logs`/`habit_targets` row now has `user_id = owner`; running it again changes nothing. (R-M2)
- **AC-M3** (owner unchanged): Given the owner's migrated data, When the owner logs/undoes/queries/reviews, Then output is byte-identical to v1.1; the full v1.1 suite re-run as the owner stays green. (R-M3)

### Access control & onboarding
- **AC-A1**: Given an unknown chat messages the bot, Then a `pending` user row is created, they receive `access_pending`, the owner receives `access_request` (with the chat id + approve hint), and the message is neither logged nor sent to the LLM. (R-A2)
- **AC-A2**: Given the owner sends `/approve 88899900`, Then that user becomes `active`, receives `access_granted`, and can subsequently log normally. (R-A4)
- **AC-A3**: Given the owner sends `/block 88899900`, Then that user becomes `blocked` and their next message gets `access_denied` and is not processed. (R-A3/R-A4)
- **AC-A4**: Given a non-owner sends `/approve …` (or `/block`/`/users`/`/invite`), Then it is not executed and reveals nothing (falls through as an unknown message). (R-A4)
- **AC-A5**: Given the owner sends `/users`, Then all users are listed with role + status. (R-A4)
- **AC-A6**: Given `/start` from an active user → welcome/intro; from an unknown user → the R-A2 pending flow. (R-A5)
- **AC-A7** (fail-safe): Given a `users` lookup raises, When a message arrives, Then the caller is treated as not active (denied), never granted. (R-A1)

### Per-chat channel & isolation
- **AC-C1**: Given user A logs "500ml", Then the confirmation is delivered to A's chat id (not the owner's / not a global pin), and the stored `logs` row has `user_id = A`. (R-C1/R-C2/R-D1)
- **AC-C2** (callback ownership): Given a log owned by chat A, When chat B taps an undo button carrying that log's id, Then the row is not deleted, B gets a not-found/already-removed reply, and the spinner is dismissed; when A taps it, the row is soft-deleted. (R-C3)
- **AC-U-ISO** (isolation invariant): Given two active users A and B with seeded logs/targets, When A runs `/habits`, a query, a streak, `/undo`, daily summary, or weekly review, Then only A's rows are read/affected; B's data is never visible to A and vice versa. (R-D4)

### Per-user features
- **AC-U1** (targets): Given A sets `/target water 2000`, Then A's effective water goal is 2000 while B's is unchanged (B's own override or the config default). (R-D2)
- **AC-U2** (streaks/milestones): Given A and B with different histories, Then each one's streak/milestone is computed only from their own logs. (R-D3)
- **AC-U3** (daily summary fan-out): Given the summary time fires with A and B active, Then each receives their own summary from their own data; a user with no logs that day is skipped. (R-S3)
- **AC-U4** (weekly review fan-out): Given the review time fires, Then each active user with data gets their own review + charts from their own logs; a user with no week data is skipped. (R-S3)
- **AC-U5** (reminders per-user skip): Given the reminder tick fires at a minute due for both A and B, When A has met the habit's goal and B has not, Then A's reminder is skipped and B's is sent — each evaluated against their own total + effective goal. (R-S2)
- **AC-U-SNOOZE**: Given A and B each had a reminder fire, When A sends "snooze 30", Then only A's last-reminded habit is rescheduled to A's chat; B is unaffected. (R-S2)
- **AC-P1** (language): Given B sends `/lang th`, Then B's replies are Thai regardless of input language; the owner (default `auto`) is unaffected. (R-P1)
- **AC-P2** (quiet hours): Given B sets `/quiet 22:00-07:00`, Then B's reminders in that window are suppressed while A's (different/none) are not; `/quiet off` clears B's windows. (R-P2)

### Scheduling (per-user reminder times), ops & concurrency
- **AC-S1** (tick + owner unchanged): Given the minutely reminder tick and a user with **no** custom times, Then that user's reminders fire at exactly the global config times (owner byte-identical to v1.1, AC-M3); the reminders come from a single minutely tick job, not one cron per config time. (R-S1)
- **AC-S2** (per-user set): Given A sends `/remind water 08:00 12:00`, Then A's water reminders fire at 08:00 and 12:00 and **not** at the old config times; B's water reminders (unchanged) still fire at the config times; other habits unaffected. (R-S4/R-S5)
- **AC-S3** (show / default / off): Given `/remind water` it reports A's effective times + source (custom/default/off); given `/remind water default` it deletes A's override and reverts to config times; given `/remind water off` A gets **no** water reminders while B still does. (R-S4/R-S5)
- **AC-S4** (no restart): Given A changes `/remind water …` while the process runs, Then the next tick uses the new times with **no restart and no scheduler-job rebuild**. (R-S6)
- **AC-S5** (validation): Given `/remind water 25:99` (or any non-`HH:MM` token), Then it is rejected with `remind_invalid_time`, nothing is written; duplicate times are de-duped and a sane cap is enforced. (R-S5)
- **AC-S6** (interplay): Given A has a custom water time, Then that reminder still honors A's per-user quiet-hours and goal-met skip (fires only if not in a quiet window and the goal isn't met), and a subsequent "snooze 30" from A reschedules that custom-time habit for A only. (R-S2)
- **AC-O1**: Given Ollama/Telegram goes down, Then the health alert is sent to the owner only; a member never receives it. (R-O1)
- **AC-X1**: Given two inbound messages from different users arrive in one poll batch, Then they are handled sequentially (no concurrent Ollama extraction from the inbound loop). (R-O2)

## 9. Resolved decisions & risks

**Answered by the owner (2026-08-21) — baked into the spec:**
- **OQ1 — Onboarding/approval UX (RESOLVED: notify + approve).** A stranger's first message auto-creates a
  `pending` row and notifies the owner, who approves with `/approve <chat_id>` (R-A2/R-A4). A one-tap inline
  approve button on the notification is deferred (§10).
- **OQ2 — Reminder times (RESOLVED: per-user schedules).** Each person sets their own reminder times per
  habit from chat. Implemented as a `user_reminder_times` store (R-M1), an `effective_reminder_times`
  resolver with config fallback (R-S4), a deterministic `/remind` setter/viewer with off/default (R-S5,
  Thai alias `เตือน`), and a **minutely-tick scheduler** that consults effective times live so a change
  takes effect with no restart or job rebuild (R-S1/R-S6). Times are validated `HH:MM`, wall-clock in the
  global `config.app.timezone`; quiet-hours + goal-met + snooze all still apply (R-S2/AC-S6). **Full
  natural-language phrasing for `/remind` is deferred** (§10) to keep v1.2 bounded. Owner behavior is
  unchanged until they customize (empty store → config times, AC-M3/AC-S1).
- **OQ3 — `blocked` behavior (RESOLVED: polite denial).** A blocked user gets `access_denied` (R-A3).

**Confirmed design decisions (recorded; push back if wrong):**
- **Habit catalog stays global** — everyone tracks the same `[[habits]]`; per-user differentiation is via
  per-user targets only. Per-user habit sets are explicitly out of scope (§10). *(Confirmed per your
  guidance — no push-back.)*
- **Additive-only break is accepted** — `habit_targets` is rebuilt (composite key). Flagged for release
  notes; `--backup` runs pre-release. *(Per your guidance.)*
- **Owner = the chat id in `.env`.** No new secret; the owner is auto-active with `role='owner'`.

**Risks:**
- **Wide mechanical refactor.** Threading `user_id` through every db/core/channel signature and updating ~15
  test fakes is large and error-prone. Mitigation: it is the sequential shared surface, gated by AC-M3
  (owner byte-identical) and AC-U-ISO (isolation) as hard guards; the change is mechanical, not algorithmic.
- **Attribution vs migration split.** The owner id is unavailable in the migration runner, so data
  attribution lives in a startup step (R-M2). Risk: a code path that opens the DB without running
  attribution (e.g. `--seed`, `--migrate`) would leave NULL `user_id` rows. Mitigation: `--seed` stamps the
  owner id too; `--migrate` only reports versions (NULLs are filled on the next normal startup, and every
  scoped query treats NULL as "belongs to nobody" so it is never mis-served). Flag: confirm `--seed`/CLI
  tools attribute to owner.
- **Shared-Ollama latency** under many users (R-O2) — sequential head-of-line blocking. Acceptable for a
  small allowlist; OQ4 (below) if it grows.
- **OQ4 (non-blocking):** if the user base grows beyond a handful, consider a bounded concurrency pool for
  LLM calls. Not needed for v1.2.

## 10. Out of scope
- Per-user habit **catalogs** (everyone tracks the same `[[habits]]`; only targets differ per user).
- **Natural-language phrasing for `/remind`** (e.g. "remind me about water at 8 and noon") — deferred; the
  deterministic `/remind <habit> HH:MM…` command + Thai alias is the v1.2 surface (OQ2).
- Per-user **timezones** — reminder times are wall-clock in the single global `config.app.timezone`.
- Per-user **summary/review times** — only per-*habit reminder* times are per-user; the daily-summary and
  weekly-review times stay global (R-S3).
- Group chats / multi-user *within one chat* — identity is one person per private chat id.
- Roles beyond `owner`/`member` (no per-permission granularity, no multi-owner).
- A one-tap inline **approve button** on the owner's access-request notification (deterministic `/approve`
  only in v1.2; the button is a nice-to-have follow-on).
- Data **export/delete per user** (GDPR-style) and re-parenting a user's data to another chat id.
- Migrating away the legacy `config.reminders.water.goal_ml` base (unchanged from v1.1).

## 11. Module split & parallel development

**Total functionals:** 11 — (1) migration 006 + attribution, (2) users table + access classification,
(3) onboarding + owner admin commands, (4) per-chat channel refactor, (5) `user_id` data scoping across
all core aggregations, (6) per-user snooze/quiet-hours + summary/review fan-out, (7) per-user language
preference, (8) per-user callback ownership, (9) owner-only ops alerts, (10) **per-user reminder-times store
+ minutely-tick scheduler**, (11) **`/remind` setter surface**. Above the 5-functional threshold.

**Recommendation:** **Large SEQUENTIAL shared surface, then 3 PARALLEL modules, then integration.** The
scoping refactor (migration + attribution + `user_id` through db/core, the per-chat channel refactor, the
per-user fan-out, the minutely-tick reminder scheduler, and per-user language *resolution*) is one deeply
interdependent unit — nearly every file changes signature, and the isolation invariant (AC-U-ISO) and
owner-unchanged guard (AC-M3) can only be proven once it is coherent. It must be built and made green
**first, sequentially**. Only after it lands are there three genuinely independent modules that own disjoint
new files and disjoint command kinds: `access` (onboarding/allowlist/admin), `preferences` (`/lang`,
`/quiet` setters), and `schedules` (`/remind` setter). All three add to `core/commands.py` and
`core/i18n.py`, so the **command-kind enum, `Command` fields, and the i18n key-block skeletons are created
in the shared surface first**; each module then fills only its own disjoint kinds/keys.

**Shared surface (built first, sequentially — the bulk of v1.2):**
- Migration 006 (users, `logs.user_id`, `habit_targets` rebuild, `user_reminder_times`) +
  `attribute_legacy_to_owner`; `LogEntry.user_id`; every `db` method scoped by `user_id`; users/preferences/
  reminder-times db methods.
- Per-recipient channel sends + `run` chat-id threading (base/telegram/line + all test fakes).
- `user_id` threaded through targets/streaks/review/charts/query/undo_ui/targets_command/discoverability.
- Per-user `ReminderState` + `send_reminder(chat_id, …)` + effective quiet windows +
  **`effective_reminder_times` resolver + `run_due_reminders` minutely tick**; global-time summary/review
  fan-out; owner-only health alerts; scheduler wiring in `main.py` (tick job + summary/review jobs).
- Per-user language **resolution** in `core/i18n.py` (the *read* side; the `/lang` *write* is module
  `preferences`).
- `main.py`: `on_message(chat_id, …)`/`on_callback(chat_id, …)`, attribution call, and the access-gate
  **seam** (calls into `access.handle_gate`, landed at integration).
- The `CommandKind` enum + `Command` field additions + i18n key-block skeletons (so the three parallel
  modules don't collide on those two shared files).

| Module | Owned ACs | Owned files | Depends on |
|---|---|---|---|
| `access` | AC-A1, AC-A2, AC-A3, AC-A4, AC-A5, AC-A6, AC-A7 | `core/access.py`, `core/commands.py` (start/approve/block/users/invite kinds), `core/i18n.py` (access keys), `tests/test_access.py` | shared: users db methods, per-chat channel, owner_chat_id, command-enum skeleton |
| `preferences` | AC-P1, AC-P2 | `core/preferences.py`, `core/commands.py` (lang/quiet kinds), `core/i18n.py` (pref keys), `tests/test_preferences.py` | shared: users prefs db methods, per-user language/quiet resolution, command-enum skeleton |
| `schedules` | AC-S2, AC-S3, AC-S5 | `core/schedules.py`, `core/commands.py` (remind kind), `core/i18n.py` (remind keys), `tests/test_schedules.py` | shared: reminder-times db methods, `effective_reminder_times` resolver, command-enum skeleton |

ACs verified during the shared-surface / integration pass (not owned by a parallel module): **AC-M1,
AC-M2, AC-M3** (migration + attribution + owner-unchanged), **AC-C1, AC-C2, AC-U-ISO** (channel + scoping +
isolation), **AC-U1, AC-U2, AC-U3, AC-U4, AC-U5, AC-U-SNOOZE** (per-user features across scoped consumers),
**AC-S1, AC-S4, AC-S6, AC-O1, AC-X1** (tick scheduler / no-restart / interplay / ops / concurrency). Every
AC belongs to exactly one owner. **Total: 29 acceptance criteria** (shared/integration 17, `access` 7,
`preferences` 2, `schedules` 3).

**Integration order (after the three modules complete):**
1. Wire `access.handle_gate` into `main.py`'s `on_message` (gate before the normal pipeline) and route
   admin / `/lang` / `/quiet` / `/remind` command kinds to `access.execute_admin` / `preferences` /
   `schedules.execute_remind`.
2. Run the full suite; AC-M3 (owner byte-identical against the v1.1 suite) + AC-U-ISO (two-user isolation)
   are the highest-value gates.
3. Integration tests: two-user end-to-end — A and B onboard (B via owner `/approve`), each logs, each sees
   only their own `/habits`/streak/summary/review; A's undo button is inert for B (AC-C2); the reminder tick
   fires A and B at their own effective times, skipping A when its goal is met (AC-U5/AC-S6); A's `/remind`
   change takes effect next tick with no restart (AC-S4); A's `/lang th` and `/quiet` affect only A; health
   alert reaches only the owner.
```
